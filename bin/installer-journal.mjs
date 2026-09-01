import { createHash, randomUUID } from "node:crypto";
import {
  closeSync,
  existsSync,
  fchmodSync,
  fsyncSync,
  linkSync,
  lstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { TextDecoder } from "node:util";

export const INSTALLER_JOURNAL_RELATIVE_PATH =
  ".herdr-orchestrator/install-journal.json";
export const INSTALLER_HARNESSES = Object.freeze([
  "droid",
  "grok",
  "codex",
  "pi",
  "claude",
  "hermes",
]);

const DIGEST_PATTERN = /^[a-f0-9]{64}$/;
const MAX_PID = 2_147_483_647;
const PID_PATTERN = /^[1-9][0-9]{0,9}$/;
const INSTALLER_HARNESS_SET = new Set(INSTALLER_HARNESSES);
const TRANSACTION_ID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const JOURNAL_TEMPORARY_PATTERN =
  /^\.install-journal\.([0-9a-f-]{36})\.([0-9]+)\.([0-9a-f-]{36})\.tmp$/;
const JOURNAL_OWNER_PATTERN =
  /^\.install-journal\.([0-9a-f-]{36})\.([0-9]+)\.([0-9a-f-]{36})\.owner$/;

function sha256(content) {
  return createHash("sha256").update(content).digest("hex");
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function stateMatches(left, right) {
  return left.kind === right.kind && (
    left.kind === "absent"
    || (
      left.digest === right.digest
      && (
        left.mode === undefined
        || right.mode === undefined
        || left.mode === right.mode
      )
    )
  );
}

function persistedStateEqual(left, right) {
  return (
    left.kind === right.kind
    && (
      left.kind === "absent"
      || (
        left.digest === right.digest
        && Object.hasOwn(left, "mode") === Object.hasOwn(right, "mode")
        && left.mode === right.mode
      )
    )
  );
}

function stateForContent(content, mode) {
  return {
    digest: sha256(content),
    kind: "regular",
    mode,
  };
}

function absentState() {
  return { kind: "absent" };
}

function validateState(value) {
  if (!isPlainObject(value) || !["absent", "regular"].includes(value.kind)) {
    throw new Error("installer_journal_invalid");
  }
  if (value.kind === "regular") {
    if (
      typeof value.digest !== "string"
      || !DIGEST_PATTERN.test(value.digest)
      || (
        Object.hasOwn(value, "mode")
        && (
          !Number.isInteger(value.mode)
          || value.mode < 0
          || value.mode > 0o7777
        )
      )
    ) {
      throw new Error("installer_journal_invalid");
    }
    return {
      digest: value.digest,
      kind: "regular",
      ...(Object.hasOwn(value, "mode") ? { mode: value.mode } : {}),
    };
  }
  if (Object.hasOwn(value, "digest") || Object.hasOwn(value, "mode")) {
    throw new Error("installer_journal_invalid");
  }
  return { kind: "absent" };
}

function isManagedRelativePath(relativePath) {
  if (
    typeof relativePath !== "string"
    || relativePath.length === 0
    || relativePath.startsWith("/")
    || relativePath.includes("\\")
    || relativePath.split("/").includes("..")
    || relativePath === INSTALLER_JOURNAL_RELATIVE_PATH
  ) {
    return false;
  }
  return (
    relativePath.startsWith(".herdr-orchestrator/")
    || relativePath.startsWith(".agents/skills/herdr-orchestrator/")
    || relativePath === ".orchestrator/.gitignore"
  );
}

function validateTargetShape(target) {
  if (!isPlainObject(target) || typeof target.scope !== "string") {
    throw new Error("installer_journal_invalid");
  }
  if (target.scope === "project") {
    if (!isManagedRelativePath(target.path)) {
      throw new Error("installer_journal_invalid");
    }
  } else if (
    target.scope === "git-exclude"
    && typeof target.path === "string"
    && isAbsolute(target.path)
  ) {
    // The current Git resolution is checked before this path is used.
  } else {
    throw new Error("installer_journal_invalid");
  }
  return {
    path: target.path,
    scope: target.scope,
  };
}

function targetKey(target) {
  return `${target.scope}:${target.path}`;
}

function targetLabel(target) {
  return target.scope === "project" ? target.path : `git-exclude:${target.path}`;
}

function fsyncDirectory(path) {
  const descriptor = openSync(path, "r");
  try {
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
}

function ensureDirectoryDurable(path) {
  try {
    const status = lstatSync(path);
    if (status.isSymbolicLink() || !status.isDirectory()) {
      throw new Error(`installer_directory_invalid: ${path}`);
    }
    return;
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw error;
    }
  }
  const parent = dirname(path);
  if (parent === path) {
    throw new Error(`installer_directory_not_found: ${path}`);
  }
  ensureDirectoryDurable(parent);
  try {
    mkdirSync(path);
  } catch (error) {
    if (error.code !== "EEXIST") {
      throw error;
    }
    const status = lstatSync(path);
    if (status.isSymbolicLink() || !status.isDirectory()) {
      throw new Error(`installer_directory_invalid: ${path}`);
    }
    return;
  }
  fsyncDirectory(parent);
}

function writeAll(descriptor, content) {
  let offset = 0;
  while (offset < content.length) {
    const written = writeSync(descriptor, content, offset, content.length - offset);
    if (written === 0) {
      throw new Error("installer_write_stalled");
    }
    offset += written;
  }
}

function replacementMode(path) {
  try {
    const status = lstatSync(path);
    if (status.isSymbolicLink() || !status.isFile()) {
      throw new Error(`installer_target_not_regular: ${path}`);
    }
    return status.mode & 0o7777;
  } catch (error) {
    if (error.code === "ENOENT") {
      return 0o666 & ~process.umask();
    }
    throw error;
  }
}

function assertJournalTargetSafe(path) {
  try {
    const status = lstatSync(path);
    if (status.isSymbolicLink()) {
      throw new Error("installer_journal_symlink");
    }
    if (!status.isFile()) {
      throw new Error("installer_journal_not_regular");
    }
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw error;
    }
  }
}

function createDurableTemporary(path, content, mode) {
  let descriptor;
  let created = false;
  let ready = false;
  try {
    descriptor = openSync(path, "wx", mode);
    created = true;
    writeAll(descriptor, content);
    fchmodSync(descriptor, mode);
    fsyncSync(descriptor);
    closeSync(descriptor);
    descriptor = undefined;
    ready = true;
  } catch (error) {
    if (descriptor !== undefined) {
      closeSync(descriptor);
    }
    if (created && !ready) {
      try {
        unlinkSync(path);
        fsyncDirectory(dirname(path));
      } catch (cleanupError) {
        if (cleanupError.code !== "ENOENT") {
          throw cleanupError;
        }
      }
    }
    throw error;
  }
}

function atomicReplace(path, content, temporaryPath, mode) {
  const parent = dirname(path);
  ensureDirectoryDurable(parent);
  let temporaryCreated = false;
  try {
    createDurableTemporary(
      temporaryPath,
      content,
      mode,
    );
    temporaryCreated = true;
    renameSync(temporaryPath, path);
    fsyncDirectory(parent);
  } catch (error) {
    if (temporaryCreated) {
      try {
        unlinkSync(temporaryPath);
        fsyncDirectory(parent);
      } catch (cleanupError) {
        if (cleanupError.code !== "ENOENT") {
          throw cleanupError;
        }
      }
    }
    throw error;
  }
}

function setRegularFileMode(path, mode) {
  const descriptor = openSync(path, "r");
  try {
    fchmodSync(descriptor, mode);
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
  fsyncDirectory(dirname(path));
}

function assertProjectTargetSafe(project, relativePath) {
  let current = project;
  const components = relativePath.split("/");
  for (let index = 0; index < components.length; index += 1) {
    current = join(current, components[index]);
    try {
      const status = lstatSync(current);
      if (status.isSymbolicLink()) {
        throw new Error(`managed_path_symlink: ${relativePath}`);
      }
      if (index < components.length - 1 && !status.isDirectory()) {
        throw new Error(`managed_path_ancestor_not_directory: ${relativePath}`);
      }
      if (index === components.length - 1 && !status.isFile()) {
        throw new Error(`managed_path_not_regular: ${relativePath}`);
      }
    } catch (error) {
      if (error.code !== "ENOENT") {
        throw error;
      }
    }
  }
}

function targetTemporaryPath(path, journal, operation) {
  return join(
    dirname(path),
    `.${basename(path)}.herdr-${journal.transaction_id}-${operation.id}.tmp`,
  );
}

function observeTemporary(path, label) {
  try {
    const status = lstatSync(path);
    if (status.isSymbolicLink() || !status.isFile()) {
      throw new Error(`installer_recovery_conflict: temporary:${label}`);
    }
    return stateForContent(readFileSync(path), status.mode & 0o7777);
  } catch (error) {
    if (error.code === "ENOENT") {
      return absentState();
    }
    throw error;
  }
}

function resolveTarget(project, target, context) {
  if (target.scope === "project") {
    assertProjectTargetSafe(project, target.path);
    return join(project, target.path);
  }
  if (
    context.gitExcludePath === null
    || resolve(context.gitExcludePath) !== resolve(target.path)
  ) {
    throw new Error("installer_recovery_conflict: git-exclude-location");
  }
  context.assertGitExcludeSafe(project, target.path);
  return target.path;
}

function observeTarget(project, target, context) {
  const path = resolveTarget(project, target, context);
  if (!existsSync(path)) {
    return absentState();
  }
  const status = lstatSync(path);
  return stateForContent(readFileSync(path), status.mode & 0o7777);
}

function decodeDesiredContent(operation) {
  if (operation.desired.kind === "absent") {
    if (operation.desired_content_base64 !== null) {
      throw new Error("installer_journal_invalid");
    }
    return null;
  }
  if (typeof operation.desired_content_base64 !== "string") {
    throw new Error("installer_journal_invalid");
  }
  const content = Buffer.from(operation.desired_content_base64, "base64");
  if (
    content.toString("base64") !== operation.desired_content_base64
    || sha256(content) !== operation.desired.digest
  ) {
    throw new Error("installer_journal_invalid");
  }
  return content;
}

function validateInventory(value) {
  if (!isPlainObject(value)) {
    throw new Error("installer_journal_invalid");
  }
  const inventory = {};
  for (const [key, item] of Object.entries(value)) {
    if (!isPlainObject(item)) {
      throw new Error("installer_journal_invalid");
    }
    const target = validateTargetShape(item.target);
    if (targetKey(target) !== key) {
      throw new Error("installer_journal_invalid");
    }
    inventory[key] = {
      state: validateState(item.state),
      target,
    };
  }
  return inventory;
}

function validateCommandResult(value, command) {
  if (value === null && command !== "uninstall") {
    return null;
  }
  if (
    command !== "uninstall"
    || !isPlainObject(value)
    || typeof value.ok !== "boolean"
    || !["removed", "retained", "unavailable"].includes(value.local_exclude)
    || !Array.isArray(value.preserved)
    || value.preserved.some((path) => !isManagedRelativePath(path))
    || new Set(value.preserved).size !== value.preserved.length
  ) {
    throw new Error("installer_journal_invalid");
  }
  return {
    local_exclude: value.local_exclude,
    ok: value.ok,
    preserved: [...value.preserved],
  };
}

function validateJournal(value) {
  if (
    !isPlainObject(value)
    || value.schema_version !== 1
    || value.package !== "herdr-orchestrator"
    || typeof value.transaction_id !== "string"
    || !TRANSACTION_ID_PATTERN.test(value.transaction_id)
    || !["install", "upgrade", "uninstall"].includes(value.command)
    || typeof value.package_version !== "string"
    || !Array.isArray(value.harnesses)
    || value.harnesses.length === 0
    || value.harnesses.some((item) => !INSTALLER_HARNESS_SET.has(item))
    || new Set(value.harnesses).size !== value.harnesses.length
    || typeof value.install_skill !== "boolean"
    || !Array.isArray(value.operations)
    || !isPlainObject(value.progress)
    || !Number.isInteger(value.progress.completed_operations)
    || !["applying", "verified"].includes(value.progress.phase)
  ) {
    throw new Error("installer_journal_invalid");
  }
  const priorInventory = validateInventory(value.prior_inventory);
  const desiredInventory = validateInventory(value.desired_inventory);
  const commandResult = validateCommandResult(
    value.command_result ?? null,
    value.command,
  );
  const priorKeys = Object.keys(priorInventory).sort();
  const desiredKeys = Object.keys(desiredInventory).sort();
  if (JSON.stringify(priorKeys) !== JSON.stringify(desiredKeys)) {
    throw new Error("installer_journal_invalid");
  }
  const operationTargets = new Set();
  const operations = value.operations.map((item, index) => {
    if (!isPlainObject(item) || item.id !== `operation-${index + 1}`) {
      throw new Error("installer_journal_invalid");
    }
    const target = validateTargetShape(item.target);
    const key = targetKey(target);
    if (operationTargets.has(key) || priorInventory[key] === undefined) {
      throw new Error("installer_journal_invalid");
    }
    operationTargets.add(key);
    const original = validateState(item.original);
    const desired = validateState(item.desired);
    if (
      !persistedStateEqual(original, priorInventory[key].state)
      || !persistedStateEqual(desired, desiredInventory[key].state)
    ) {
      throw new Error("installer_journal_invalid");
    }
    const operation = {
      desired,
      desired_content_base64: item.desired_content_base64,
      id: item.id,
      original,
      target,
    };
    decodeDesiredContent(operation);
    return operation;
  });
  const regularStates = [
    ...Object.values(priorInventory).map((item) => item.state),
    ...Object.values(desiredInventory).map((item) => item.state),
    ...operations.flatMap((operation) => [operation.original, operation.desired]),
  ].filter((state) => state.kind === "regular");
  if (
    new Set(regularStates.map((state) => Object.hasOwn(state, "mode"))).size > 1
  ) {
    throw new Error("installer_journal_invalid");
  }
  if (
    value.progress.completed_operations < 0
    || value.progress.completed_operations > operations.length
  ) {
    throw new Error("installer_journal_invalid");
  }
  for (const key of priorKeys) {
    const changed = !stateMatches(
      priorInventory[key].state,
      desiredInventory[key].state,
    );
    if (changed !== operationTargets.has(key)) {
      throw new Error("installer_journal_invalid");
    }
  }
  const manifestOperation = operations.findIndex(
    (operation) => (
      operation.target.scope === "project"
      && operation.target.path === ".herdr-orchestrator/manifest.json"
    ),
  );
  if (manifestOperation >= 0 && manifestOperation !== operations.length - 1) {
    throw new Error("installer_journal_invalid");
  }
  return {
    command: value.command,
    command_result: commandResult,
    desired_inventory: desiredInventory,
    harnesses: [...value.harnesses],
    install_skill: value.install_skill,
    operations,
    package: value.package,
    package_version: value.package_version,
    prior_inventory: priorInventory,
    progress: {
      completed_operations: value.progress.completed_operations,
      phase: value.progress.phase,
    },
    schema_version: value.schema_version,
    transaction_id: value.transaction_id,
  };
}

function serializeJournal(journal) {
  return {
    command: journal.command,
    command_result: journal.command_result,
    desired_inventory: journal.desired_inventory,
    harnesses: journal.harnesses,
    install_skill: journal.install_skill,
    operations: journal.operations,
    package: journal.package,
    package_version: journal.package_version,
    prior_inventory: journal.prior_inventory,
    progress: journal.progress,
    schema_version: journal.schema_version,
    transaction_id: journal.transaction_id,
  };
}

function immutableJournalIntent(journal) {
  const value = serializeJournal(journal);
  delete value.progress;
  return JSON.stringify(value);
}

function journalPath(project) {
  return join(project, INSTALLER_JOURNAL_RELATIVE_PATH);
}

function journalContent(journal) {
  return Buffer.from(`${JSON.stringify(serializeJournal(journal), null, 2)}\n`);
}

function journalTemporaryPath(project, transactionId) {
  return join(
    dirname(journalPath(project)),
    `.install-journal.${transactionId}.${process.pid}.${randomUUID()}.tmp`,
  );
}

function journalOwnerPath(project, transactionId) {
  return join(
    dirname(journalPath(project)),
    `.install-journal.${transactionId}.${process.pid}.${randomUUID()}.owner`,
  );
}

function listJournalArtifacts(project, pattern, kind) {
  const directory = dirname(journalPath(project));
  let names;
  try {
    names = readdirSync(directory);
  } catch (error) {
    if (error.code === "ENOENT") {
      return [];
    }
    throw error;
  }
  return names.flatMap((name) => {
    const match = pattern.exec(name);
    if (
      match === null
      || !TRANSACTION_ID_PATTERN.test(match[1])
      || !TRANSACTION_ID_PATTERN.test(match[3])
    ) {
      return [];
    }
    const pid = Number(match[2]);
    if (
      !PID_PATTERN.test(match[2])
      || !Number.isSafeInteger(pid)
      || pid > MAX_PID
    ) {
      return [{
        invalid: true,
        kind,
        path: join(directory, name),
        pid: null,
        transactionId: match[1],
      }];
    }
    return [{
      invalid: false,
      kind,
      path: join(directory, name),
      pid,
      transactionId: match[1],
    }];
  });
}

function listJournalTemporaries(project) {
  return listJournalArtifacts(project, JOURNAL_TEMPORARY_PATTERN, "temporary");
}

function listJournalOwners(project) {
  return listJournalArtifacts(project, JOURNAL_OWNER_PATTERN, "owner");
}

function persistJournal(project, journal) {
  const path = journalPath(project);
  assertJournalTargetSafe(path);
  atomicReplace(
    path,
    journalContent(journal),
    journalTemporaryPath(project, journal.transaction_id),
    replacementMode(path),
  );
}

function readJournalAtPath(path) {
  assertJournalTargetSafe(path);
  if (!existsSync(path)) {
    return null;
  }
  try {
    const content = new TextDecoder("utf-8", { fatal: true }).decode(readFileSync(path));
    return validateJournal(JSON.parse(content));
  } catch (error) {
    if (
      error.message === "installer_journal_symlink"
      || error.message === "installer_journal_not_regular"
    ) {
      throw error;
    }
    throw new Error("installer_journal_invalid");
  }
}

function readPublishedJournal(project) {
  return readJournalAtPath(journalPath(project));
}

function readOwnedJournalArtifact(artifact, kind) {
  let journal;
  try {
    journal = readJournalAtPath(artifact.path);
  } catch {
    throw new Error(
      `installer_journal_${kind}_conflict: ${basename(artifact.path)}`,
    );
  }
  if (
    journal === null
    || journal.transaction_id !== artifact.transactionId
  ) {
    throw new Error(
      `installer_journal_${kind}_conflict: ${basename(artifact.path)}`,
    );
  }
  return journal;
}

function readOwnedJournalTemporary(temporary) {
  return readOwnedJournalArtifact(temporary, "temporary");
}

function readJournalOwner(owner) {
  return readOwnedJournalArtifact(owner, "owner");
}

function processIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error.code === "ESRCH") {
      return false;
    }
    if (error.code === "EPERM") {
      return true;
    }
    throw error;
  }
}

function removeOwnedJournalTemporary(temporary) {
  unlinkSync(temporary.path);
  fsyncDirectory(dirname(temporary.path));
}

function removeJournalOwner(owner) {
  unlinkSync(owner.path);
  fsyncDirectory(dirname(owner.path));
}

function adoptJournalOwner(project, artifact) {
  const owner = {
    path: journalOwnerPath(project, artifact.transactionId),
    pid: process.pid,
    transactionId: artifact.transactionId,
  };
  try {
    renameSync(artifact.path, owner.path);
  } catch (error) {
    if (["EEXIST", "ENOENT"].includes(error.code)) {
      throw new Error("installer_transaction_active");
    }
    throw error;
  }
  fsyncDirectory(dirname(owner.path));
  return owner;
}

function adoptLegacyPublishedJournal(project, journal) {
  const owner = {
    path: journalOwnerPath(project, journal.transaction_id),
    pid: process.pid,
    transactionId: journal.transaction_id,
  };
  try {
    renameSync(journalPath(project), owner.path);
  } catch (error) {
    if (["EEXIST", "ENOENT"].includes(error.code)) {
      throw new Error("installer_transaction_active");
    }
    throw error;
  }
  fsyncDirectory(dirname(owner.path));
  return owner;
}

function isLegacyModeJournal(journal) {
  const regularStates = [
    ...Object.values(journal.prior_inventory).map((item) => item.state),
    ...Object.values(journal.desired_inventory).map((item) => item.state),
  ].filter((state) => state.kind === "regular");
  return (
    regularStates.length > 0
    && regularStates.every((state) => state.mode === undefined)
  );
}

function legacyOriginalStateMatches(actual, original, journal, target, desired) {
  if (!stateMatches(actual, original)) {
    return false;
  }
  if (
    !isLegacyModeJournal(journal)
    || original.kind !== "regular"
    || actual.kind !== "regular"
  ) {
    return true;
  }
  // A mode-less journal cannot prove the historical mode of a project file
  // slated for deletion. The caller may mark a digest-matching deletion as
  // an explicit preserved participant; metadata and Git-exclude replacements
  // remain replayable and carry the live mode.
  return (
    isManifestTarget(target)
    || target?.scope !== "project"
    || desired?.kind !== "absent"
  );
}

function isManifestTarget(target) {
  return (
    target.scope === "project"
    && target.path === ".herdr-orchestrator/manifest.json"
  );
}

function legacyPreservedTargets(project, journal, context) {
  const preserved = new Set();
  if (!isLegacyModeJournal(journal)) {
    return preserved;
  }
  for (const operation of journal.operations) {
    if (
      operation.desired.kind !== "absent"
      || operation.original.kind !== "regular"
      || operation.target.scope !== "project"
      || isManifestTarget(operation.target)
    ) {
      continue;
    }
    const actual = observeTarget(project, operation.target, context);
    if (actual.kind === "regular" && stateMatches(actual, operation.original)) {
      preserved.add(targetKey(operation.target));
    }
  }
  if ([...preserved].some((key) => key.startsWith("project:"))) {
    if (journal.command === "uninstall") {
      for (const operation of journal.operations) {
        if (
          operation.target.scope === "git-exclude"
          && operation.desired.kind === "regular"
        ) {
          preserved.add(targetKey(operation.target));
        }
      }
    }
  }
  return preserved;
}

function legacyProjectPreservationConflict(journal, preservedTargets) {
  if (journal.command === "uninstall") {
    return [];
  }
  return [...preservedTargets]
    .filter((key) => key.startsWith("project:"))
    .filter((key) => {
      const item = journal.prior_inventory[key];
      return item !== undefined && !isManifestTarget(item.target);
    })
    .map((key) => targetLabel(journal.prior_inventory[key].target))
    .sort();
}

function listLiveManagedEntries(project, journal) {
  const known = new Set([
    ...Object.values(journal.prior_inventory),
    ...Object.values(journal.desired_inventory),
  ]
    .filter((item) => item.target.scope === "project")
    .map((item) => item.target.path));
  const transactionToken = journal.transaction_id;
  const operationTemporaryNames = new Set();
  for (const operation of journal.operations) {
    if (operation.desired.kind !== "regular" || operation.target.scope !== "project") {
      continue;
    }
    operationTemporaryNames.add(
      basename(
        targetTemporaryPath(
          join(project, operation.target.path),
          journal,
          operation,
        ),
      ),
    );
  }
  const ignored = (name, relativePath) => (
    relativePath === INSTALLER_JOURNAL_RELATIVE_PATH
    || (
      operationTemporaryNames.has(name)
      || (
        (JOURNAL_OWNER_PATTERN.test(name) || JOURNAL_TEMPORARY_PATTERN.test(name))
        && name.includes(transactionToken)
      )
    )
  );
  const entries = [];
  const visit = (relativeDirectory) => {
    const directory = join(project, relativeDirectory);
    let directoryEntries;
    try {
      const status = lstatSync(directory);
      if (status.isSymbolicLink() || !status.isDirectory()) {
        const name = basename(relativeDirectory);
        if (!known.has(relativeDirectory) && !ignored(name, relativeDirectory)) {
          entries.push(relativeDirectory);
        }
        return;
      }
      directoryEntries = readdirSync(directory, { withFileTypes: true });
    } catch (error) {
      if (error.code === "ENOENT") {
        return;
      }
      throw error;
    }
    for (const entry of directoryEntries) {
      const child = `${relativeDirectory}/${entry.name}`;
      if (entry.isDirectory() && !entry.isSymbolicLink()) {
        visit(child);
      } else if (!known.has(child) && !ignored(entry.name, child)) {
        entries.push(child);
      }
    }
  };
  for (const root of [
    ".herdr-orchestrator",
    ".orchestrator",
    ".agents/skills/herdr-orchestrator",
  ]) {
    visit(root);
  }
  return entries.sort();
}

function legacyExcludeCouplingConflict(project, journal, context, preservedTargets) {
  if (journal.command !== "uninstall") {
    return null;
  }
  if (!isLegacyModeJournal(journal)) {
    return null;
  }
  const hasGitExcludeParticipant = Object.values(journal.prior_inventory)
    .some((item) => item.target.scope === "git-exclude");
  if (!hasGitExcludeParticipant) {
    return null;
  }
  const unjournaled = listLiveManagedEntries(project, journal);
  if (unjournaled.length > 0) {
    return unjournaled.map((path) => `git-exclude:${path}`).join(",");
  }
  const projectKeys = [...preservedTargets].filter((key) => {
    const item = journal.prior_inventory[key];
    return (
      key.startsWith("project:")
      && item !== undefined
      && !isManifestTarget(item.target)
    );
  });
  const excludeKey = [...preservedTargets].find((key) => {
    const item = journal.prior_inventory[key];
    return item?.target.scope === "git-exclude";
  });
  if (projectKeys.length === 0 || excludeKey === undefined) {
    return null;
  }
  const retainedProjects = projectKeys.filter((key) => {
    const item = journal.prior_inventory[key];
    const actual = observeTarget(project, item.target, context);
    return stateMatches(actual, item.state);
  });
  if (retainedProjects.length === 0) {
    const exclude = journal.prior_inventory[excludeKey];
    return targetLabel(exclude.target);
  }
  return null;
}

function assertLegacyExcludeCoupling(project, journal, context, preservedTargets) {
  const conflict = legacyExcludeCouplingConflict(
    project,
    journal,
    context,
    preservedTargets,
  );
  if (conflict !== null) {
    throw new Error(`installer_recovery_conflict: ${conflict}`);
  }
}

function recoverPublishedJournal(project) {
  let journal = readPublishedJournal(project);
  const owners = listJournalOwners(project);
  const temporaries = listJournalTemporaries(project);
  const artifacts = [...owners, ...temporaries];
  if (journal === null && artifacts.length === 0) {
    return null;
  }
  const invalidArtifact = artifacts.find((artifact) => artifact.invalid);
  if (invalidArtifact !== undefined) {
    throw new Error(
      `installer_journal_${invalidArtifact.kind}_conflict: ${basename(invalidArtifact.path)}`,
    );
  }
  if (artifacts.some((artifact) => processIsAlive(artifact.pid))) {
    throw new Error("installer_transaction_active");
  }
  const ownerRecords = owners.map((owner) => ({
    artifact: owner,
    journal: readJournalOwner(owner),
  }));
  const temporaryRecords = temporaries.map((temporary) => ({
    artifact: temporary,
    journal: readOwnedJournalTemporary(temporary),
  }));
  const intentReference = journal ?? ownerRecords[0]?.journal
    ?? temporaryRecords[0]?.journal;
  if (
    intentReference !== undefined
    && [...ownerRecords, ...temporaryRecords].some(
      (record) => (
        immutableJournalIntent(record.journal)
        !== immutableJournalIntent(intentReference)
      ),
    )
  ) {
    throw new Error("installer_journal_invalid");
  }
  if (
    journal !== null
    && [...ownerRecords, ...temporaryRecords].some(
      (record) => record.journal.transaction_id !== journal.transaction_id,
    )
  ) {
    throw new Error("installer_journal_owner_conflict: transaction");
  }
  let claimRecord = null;
  if (journal === null) {
    if (ownerRecords.length === 1) {
      claimRecord = ownerRecords[0];
    } else if (ownerRecords.length === 0 && temporaryRecords.length === 1) {
      claimRecord = temporaryRecords[0];
    } else if (ownerRecords.length === 0) {
      throw new Error("installer_journal_owner_conflict: missing");
    } else {
      throw new Error("installer_journal_owner_conflict: multiple");
    }
    journal = claimRecord.journal;
  } else {
    const matchingOwners = ownerRecords.filter(
      (record) => record.journal.transaction_id === journal.transaction_id,
    );
    if (matchingOwners.length > 1) {
      throw new Error("installer_journal_owner_conflict: multiple");
    }
    if (matchingOwners.length === 1) {
      claimRecord = matchingOwners[0];
    } else {
      const matchingTemporaries = temporaryRecords.filter(
        (record) => record.journal.transaction_id === journal.transaction_id,
      );
      if (matchingTemporaries.length === 1) {
        claimRecord = matchingTemporaries[0];
      } else if (matchingTemporaries.length > 1) {
        throw new Error("installer_journal_owner_conflict: multiple");
      } else if (isLegacyModeJournal(journal)) {
        claimRecord = null;
      } else {
        throw new Error("installer_journal_owner_conflict: missing");
      }
    }
  }
  const owner = claimRecord === null
    ? adoptLegacyPublishedJournal(project, journal)
    : adoptJournalOwner(project, claimRecord.artifact);
  if (!existsSync(journalPath(project))) {
    try {
      linkSync(owner.path, journalPath(project));
    } catch (error) {
      if (error.code === "EEXIST") {
        throw new Error("installer_transaction_active");
      }
      throw error;
    }
    fsyncDirectory(dirname(journalPath(project)));
  }
  for (const record of [...ownerRecords, ...temporaryRecords]) {
    if (record === claimRecord) {
      continue;
    }
    if (record.artifact.path.endsWith(".owner")) {
      removeJournalOwner(record.artifact);
    } else {
      removeOwnedJournalTemporary(record.artifact);
    }
  }
  return { journal, owner };
}

function publishInitialJournal(project, journal) {
  const path = journalPath(project);
  const parent = dirname(path);
  ensureDirectoryDurable(parent);
  assertJournalTargetSafe(path);
  const owner = {
    path: journalOwnerPath(project, journal.transaction_id),
    pid: process.pid,
    transactionId: journal.transaction_id,
  };
  let linked = false;
  try {
    createDurableTemporary(
      owner.path,
      journalContent(journal),
      replacementMode(path),
    );
    try {
      linkSync(owner.path, path);
    } catch (error) {
      if (error.code === "EEXIST") {
        throw new Error("installer_transaction_active");
      }
      throw error;
    }
    linked = true;
    fsyncDirectory(parent);
    return owner;
  } finally {
    if (!linked && existsSync(owner.path)) {
      removeJournalOwner(owner);
    }
  }
}

function inspectEndpoints(project, journal, context, preservedTargets = new Set()) {
  const conflicts = [];
  const preserved = [];
  const states = {};
  for (const [key, item] of Object.entries(journal.prior_inventory)) {
    const actual = observeTarget(project, item.target, context);
    const desired = journal.desired_inventory[key].state;
    if (
      stateMatches(actual, desired)
      && !(preservedTargets.has(key) && item.target.scope === "git-exclude")
    ) {
      states[key] = "desired";
    } else if (preservedTargets.has(key)) {
      if (stateMatches(actual, item.state)) {
        states[key] = "preserved";
        preserved.push(targetLabel(item.target));
      } else {
        states[key] = "conflict";
        conflicts.push(targetLabel(item.target));
      }
    } else if (
      legacyOriginalStateMatches(
        actual,
        item.state,
        journal,
        item.target,
        desired,
      )
    ) {
      states[key] = "original";
    } else {
      states[key] = "conflict";
      conflicts.push(targetLabel(item.target));
    }
  }
  for (const operation of journal.operations) {
    if (operation.desired.kind !== "regular") {
      continue;
    }
    const path = resolveTarget(project, operation.target, context);
    const temporary = observeTemporary(
      targetTemporaryPath(path, journal, operation),
      targetLabel(operation.target),
    );
    if (
      temporary.kind === "regular"
      && !stateMatches(temporary, operation.desired)
    ) {
      conflicts.push(`temporary:${targetLabel(operation.target)}`);
    }
  }
  return {
    conflicts: conflicts.sort(),
    preserved: preserved.sort(),
    states,
  };
}

function preflightEndpoints(project, journal, context, preservedTargets = new Set()) {
  const { conflicts } = inspectEndpoints(project, journal, context, preservedTargets);
  if (conflicts.length > 0) {
    throw new Error(`installer_recovery_conflict: ${conflicts.join(",")}`);
  }
}

function applyOperation(project, journal, operation, context, preservedTargets = new Set()) {
  const actual = observeTarget(project, operation.target, context);
  const path = resolveTarget(project, operation.target, context);
  if (operation.desired.kind === "regular") {
    const temporaryPath = targetTemporaryPath(path, journal, operation);
    const temporary = observeTemporary(
      temporaryPath,
      targetLabel(operation.target),
    );
    if (
      stateMatches(actual, operation.desired)
      && !(preservedTargets.has(targetKey(operation.target))
        && operation.target.scope === "git-exclude")
    ) {
      if (temporary.kind === "regular") {
        unlinkSync(temporaryPath);
        fsyncDirectory(dirname(temporaryPath));
      }
      return;
    }
    if (preservedTargets.has(targetKey(operation.target))) {
      if (!stateMatches(actual, operation.original)) {
        throw new Error(`installer_recovery_conflict: ${targetLabel(operation.target)}`);
      }
      if (temporary.kind === "regular") {
        unlinkSync(temporaryPath);
        fsyncDirectory(dirname(temporaryPath));
      }
      return;
    }
    if (!legacyOriginalStateMatches(
      actual,
      operation.original,
      journal,
      operation.target,
      operation.desired,
    )) {
      throw new Error(`installer_recovery_conflict: ${targetLabel(operation.target)}`);
    }
    if (temporary.kind === "regular") {
      if (
        operation.desired.mode === undefined
        && actual.kind === "regular"
        && temporary.mode !== actual.mode
      ) {
        setRegularFileMode(temporaryPath, actual.mode);
      }
      renameSync(temporaryPath, path);
      fsyncDirectory(dirname(path));
      return;
    }
    const content = decodeDesiredContent(operation);
    atomicReplace(
      path,
      content,
      temporaryPath,
      operation.desired.mode
        ?? (actual.kind === "regular" ? actual.mode : replacementMode(path)),
    );
  } else {
    if (stateMatches(actual, operation.desired)) {
      return;
    }
    if (preservedTargets.has(targetKey(operation.target))) {
      if (!stateMatches(actual, operation.original)) {
        throw new Error(`installer_recovery_conflict: ${targetLabel(operation.target)}`);
      }
      return;
    }
    if (!legacyOriginalStateMatches(
      actual,
      operation.original,
      journal,
      operation.target,
      operation.desired,
    )) {
      throw new Error(`installer_recovery_conflict: ${targetLabel(operation.target)}`);
    }
    unlinkSync(path);
    fsyncDirectory(dirname(path));
  }
}

function verifyDesiredInventory(project, journal, context, preservedTargets = new Set()) {
  const conflicts = [];
  for (const [key, item] of Object.entries(journal.desired_inventory)) {
    const actual = observeTarget(project, item.target, context);
    if (!stateMatches(actual, item.state)) {
      const preservedOriginal = (
        preservedTargets.has(key)
        && stateMatches(actual, journal.prior_inventory[key].state)
      );
      const preservedDesired = (
        preservedTargets.has(key)
        && item.target.scope !== "git-exclude"
        && stateMatches(actual, item.state)
      );
      if (
        !preservedOriginal
        && !preservedDesired
      ) {
        conflicts.push(targetLabel(item.target));
      }
    }
  }
  if (conflicts.length > 0) {
    throw new Error(`installer_recovery_conflict: ${conflicts.sort().join(",")}`);
  }
}

function verifyDesiredInventoryBeforeManifest(
  project,
  journal,
  context,
  preservedTargets = new Set(),
) {
  const conflicts = [];
  for (const [key, item] of Object.entries(journal.desired_inventory)) {
    if (isManifestTarget(item.target)) {
      continue;
    }
    const actual = observeTarget(project, item.target, context);
    if (!stateMatches(actual, item.state)) {
      const preservedOriginal = (
        preservedTargets.has(key)
        && stateMatches(actual, journal.prior_inventory[key].state)
      );
      const preservedDesired = (
        preservedTargets.has(key)
        && item.target.scope !== "git-exclude"
        && stateMatches(actual, item.state)
      );
      if (
        !preservedOriginal
        && !preservedDesired
      ) {
        conflicts.push(targetLabel(item.target));
      }
    }
  }
  if (conflicts.length > 0) {
    throw new Error(`installer_recovery_conflict: ${conflicts.sort().join(",")}`);
  }
}

function completeJournal(project, journal, context, owner) {
  const preservedTargets = legacyPreservedTargets(
    project,
    journal,
    context,
  );
  const legacyProjectPreserved = legacyProjectPreservationConflict(
    journal,
    preservedTargets,
  );
  if (legacyProjectPreserved.length > 0) {
    throw new Error(
      `installer_recovery_conflict: ${legacyProjectPreserved.join(",")}`,
    );
  }
  preflightEndpoints(project, journal, context, preservedTargets);
  assertLegacyExcludeCoupling(project, journal, context, preservedTargets);
  for (let index = 0; index < journal.operations.length; index += 1) {
    const operation = journal.operations[index];
    if (operation.target.scope === "git-exclude") {
      assertLegacyExcludeCoupling(project, journal, context, preservedTargets);
    }
    if (
      isManifestTarget(operation.target)
    ) {
      assertLegacyExcludeCoupling(project, journal, context, preservedTargets);
      verifyDesiredInventoryBeforeManifest(
        project,
        journal,
        context,
        preservedTargets,
      );
    }
    applyOperation(project, journal, operation, context, preservedTargets);
    if (journal.progress.completed_operations < index + 1) {
      journal.progress.completed_operations = index + 1;
      persistJournal(project, journal);
    }
  }
  verifyDesiredInventory(project, journal, context, preservedTargets);
  const retainedPreservedTargets = new Set();
  for (const key of preservedTargets) {
    const item = journal.prior_inventory[key];
    if (item === undefined) {
      throw new Error("installer_recovery_conflict: preserved_target_missing");
    }
    const actual = observeTarget(project, item.target, context);
    if (
      !stateMatches(actual, item.state)
      && (
        item.target.scope === "git-exclude"
        || !stateMatches(actual, journal.desired_inventory[key].state)
      )
    ) {
      throw new Error(`installer_recovery_conflict: ${targetLabel(item.target)}`);
    }
    if (stateMatches(actual, item.state)) {
      retainedPreservedTargets.add(key);
    }
  }
  assertLegacyExcludeCoupling(project, journal, context, preservedTargets);
  if (journal.progress.phase !== "verified") {
    journal.progress.phase = "verified";
    persistJournal(project, journal);
  }
  unlinkSync(journalPath(project));
  fsyncDirectory(dirname(journalPath(project)));
  removeJournalOwner(owner);
  const preservedPaths = [...retainedPreservedTargets]
    .filter((key) => key.startsWith("project:"))
    .map((key) => key.slice("project:".length))
    .filter((path) => path !== ".herdr-orchestrator/manifest.json")
    .sort();
  const preservedExclude = [...retainedPreservedTargets]
    .some((key) => key.startsWith("git-exclude:"));
  const commandResult = journal.command_result === null
    ? null
    : {
        ...journal.command_result,
        local_exclude: preservedExclude
          ? "retained"
          : journal.command_result.local_exclude,
        ok: journal.command_result.ok && preservedPaths.length === 0,
        preserved: [
          ...new Set([
            ...journal.command_result.preserved,
            ...preservedPaths,
          ]),
        ].sort(),
      };
  return {
    command: journal.command,
    command_result: commandResult,
    preserved: preservedPaths,
    recovered: true,
    transaction_id: journal.transaction_id,
  };
}

function contextWithDefaults(context) {
  return {
    assertGitExcludeSafe: context.assertGitExcludeSafe,
    gitExcludePath: context.gitExcludePath ?? null,
  };
}

export function installerFileState(content, priorState = null) {
  if (content === null) {
    return absentState();
  }
  const mode = priorState?.kind === "regular"
    ? priorState.mode
    : 0o666 & ~process.umask();
  return stateForContent(content, mode);
}

export function observeInstallerTarget(project, target, context) {
  return observeTarget(
    resolve(project),
    validateTargetShape(target),
    contextWithDefaults(context),
  );
}

export function reconcileInstallerJournal(context) {
  const project = resolve(context.project);
  const normalizedContext = contextWithDefaults(context);
  const recovered = recoverPublishedJournal(project);
  if (recovered === null) {
    return { active: false, recovered: false };
  }
  return {
    active: true,
    ...completeJournal(
      project,
      recovered.journal,
      normalizedContext,
      recovered.owner,
    ),
  };
}

export function inspectInstallerJournal(context) {
  const project = resolve(context.project);
  const owners = listJournalOwners(project);
  const temporaries = listJournalTemporaries(project);
  const invalidArtifact = [...owners, ...temporaries].find(
    (artifact) => artifact.invalid,
  );
  if (invalidArtifact !== undefined) {
    return {
      active: true,
      conflicts: [
        `journal-${invalidArtifact.kind}:${basename(invalidArtifact.path)}`,
      ],
      invalid: true,
      preserved: [],
      publication: invalidArtifact.kind,
    };
  }
  let journal = readPublishedJournal(project);
  let publication = "published";
  const journalTemporaryConflicts = [];
  const possibleClaims = owners.length > 0 ? owners : temporaries;
  if (journal === null && possibleClaims.length > 0) {
    publication = owners.length > 0 ? "owner" : "temporary";
    try {
      journal = owners.length > 0
        ? readJournalOwner(owners[0])
        : readOwnedJournalTemporary(temporaries[0]);
    } catch {
      journalTemporaryConflicts.push(
        `journal-${publication}:${basename(possibleClaims[0].path)}`,
      );
    }
  }
  if (journal === null) {
    if (journalTemporaryConflicts.length > 0) {
      return {
        active: true,
        conflicts: journalTemporaryConflicts,
        invalid: true,
        preserved: [],
        publication,
      };
    }
    return { active: false };
  }
  const normalizedContext = contextWithDefaults(context);
  for (const owner of owners) {
    try {
      const candidate = readJournalOwner(owner);
      if (immutableJournalIntent(candidate) !== immutableJournalIntent(journal)) {
        throw new Error("installer_journal_invalid");
      }
      if (candidate.transaction_id !== journal.transaction_id) {
        journalTemporaryConflicts.push(
          `journal-owner:${basename(owner.path)}`,
        );
      }
    } catch (error) {
      if (error.message === "installer_journal_invalid") {
        throw error;
      }
      journalTemporaryConflicts.push(
        `journal-owner:${basename(owner.path)}`,
      );
    }
  }
  for (const temporary of temporaries) {
    try {
      const candidate = readOwnedJournalTemporary(temporary);
      if (immutableJournalIntent(candidate) !== immutableJournalIntent(journal)) {
        throw new Error("installer_journal_invalid");
      }
      if (candidate.transaction_id !== journal.transaction_id) {
        journalTemporaryConflicts.push(
          `journal-temporary:${basename(temporary.path)}`,
        );
      }
    } catch (error) {
      if (error.message === "installer_journal_invalid") {
        throw error;
      }
      journalTemporaryConflicts.push(
        `journal-temporary:${basename(temporary.path)}`,
      );
    }
  }
  const matchingOwnerCount = owners.filter(
    (owner) => owner.transactionId === journal.transaction_id,
  ).length;
  if (matchingOwnerCount === 0) {
    journalTemporaryConflicts.push("journal-owner:missing");
  }
  if (matchingOwnerCount > 1 || (owners.length > 1 && publication === "owner")) {
    journalTemporaryConflicts.push("journal-owner:multiple");
  }
  if (temporaries.length > 1 && publication === "temporary") {
    journalTemporaryConflicts.push("journal-temporary:multiple");
  }
  const preservedTargets = legacyPreservedTargets(
    project,
    journal,
    normalizedContext,
  );
  const inspection = inspectEndpoints(
    project,
    journal,
    normalizedContext,
    preservedTargets,
  );
  const legacyProjectConflicts = legacyProjectPreservationConflict(
    journal,
    preservedTargets,
  );
  const legacyExcludeConflict = legacyExcludeCouplingConflict(
    project,
    journal,
    normalizedContext,
    preservedTargets,
  );
  return {
    active: true,
    command: journal.command,
    conflicts: [
      ...inspection.conflicts,
      ...legacyProjectConflicts,
      ...(legacyExcludeConflict === null ? [] : [legacyExcludeConflict]),
      ...journalTemporaryConflicts,
    ].sort(),
    desired_inventory: journal.desired_inventory,
    harnesses: [...journal.harnesses],
    install_skill: journal.install_skill,
    journal_owners: owners.map((item) => basename(item.path)).sort(),
    journal_temporaries: temporaries.map((item) => basename(item.path)).sort(),
    package_version: journal.package_version,
    preserved: inspection.preserved,
    progress: { ...journal.progress },
    publication,
    states: inspection.states,
    transaction_id: journal.transaction_id,
  };
}

export function runInstallerTransaction(spec) {
  const project = resolve(spec.project);
  const journalOwners = listJournalOwners(project);
  const journalTemporaries = listJournalTemporaries(project);
  const invalidArtifact = [...journalOwners, ...journalTemporaries].find(
    (artifact) => artifact.invalid,
  );
  if (invalidArtifact !== undefined) {
    throw new Error(
      `installer_journal_${invalidArtifact.kind}_conflict: ${basename(invalidArtifact.path)}`,
    );
  }
  if (
    readPublishedJournal(project) !== null
    || journalOwners.length > 0
    || journalTemporaries.length > 0
  ) {
    throw new Error("installer_transaction_active");
  }
  const transactionId = randomUUID();
  const priorInventory = {};
  const desiredInventory = {};
  const participantByKey = new Map();
  for (const item of spec.participants) {
    const target = validateTargetShape(item.target);
    const key = targetKey(target);
    if (priorInventory[key] !== undefined) {
      throw new Error("installer_transaction_invalid");
    }
    const original = validateState(item.original);
    const desired = validateState(item.desired);
    priorInventory[key] = {
      state: original,
      target,
    };
    desiredInventory[key] = {
      state: desired,
      target,
    };
    participantByKey.set(key, {
      desiredContent: item.desiredContent,
      original,
      desired,
      target,
    });
  }
  const operations = [];
  for (const [key, item] of participantByKey) {
    if (stateMatches(item.original, item.desired)) {
      continue;
    }
    const desiredContent = item.desiredContent;
    const desired = item.desired;
    const contentBase64 = desiredContent === null
      ? null
      : Buffer.from(desiredContent).toString("base64");
    if (
      (desired.kind === "regular") !== (desiredContent !== null)
      || (desiredContent !== null && sha256(desiredContent) !== desired.digest)
    ) {
      throw new Error("installer_transaction_invalid");
    }
    operations.push({
      desired,
      desired_content_base64: contentBase64,
      id: `operation-${operations.length + 1}`,
      original: priorInventory[key].state,
      target: item.target,
    });
  }
  const journal = validateJournal({
    command: spec.command,
    command_result: spec.commandResult ?? null,
    desired_inventory: desiredInventory,
    harnesses: spec.harnesses,
    install_skill: spec.installSkill,
    operations,
    package: "herdr-orchestrator",
    package_version: spec.packageVersion,
    prior_inventory: priorInventory,
    progress: {
      completed_operations: 0,
      phase: "applying",
    },
    schema_version: 1,
    transaction_id: transactionId,
  });
  const normalizedContext = contextWithDefaults(spec);
  for (const item of Object.values(journal.prior_inventory)) {
    const actual = observeTarget(project, item.target, normalizedContext);
    if (!stateMatches(actual, item.state)) {
      throw new Error(`installer_transaction_stale: ${targetLabel(item.target)}`);
    }
  }
  if (journal.operations.length === 0) {
    return {
      command: journal.command,
      command_result: journal.command_result,
      recovered: false,
      transaction_id: null,
    };
  }
  const owner = publishInitialJournal(project, journal);
  return completeJournal(project, journal, normalizedContext, owner);
}
