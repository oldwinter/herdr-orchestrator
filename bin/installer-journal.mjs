import { createHash, randomUUID } from "node:crypto";
import {
  closeSync,
  existsSync,
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

const DIGEST_PATTERN = /^[a-f0-9]{64}$/;
const TRANSACTION_ID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const JOURNAL_TEMPORARY_PATTERN =
  /^\.install-journal\.([0-9a-f-]{36})\.([1-9][0-9]*)\.([0-9a-f-]{36})\.tmp$/;
const TEST_ADAPTER = Symbol.for(
  "herdr-orchestrator.installer-test-adapter.v1",
);

function sha256(content) {
  return createHash("sha256").update(content).digest("hex");
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function stateMatches(left, right) {
  return left.kind === right.kind && (
    left.kind === "absent" || left.digest === right.digest
  );
}

function stateForContent(content) {
  return {
    digest: sha256(content),
    kind: "regular",
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
    if (typeof value.digest !== "string" || !DIGEST_PATTERN.test(value.digest)) {
      throw new Error("installer_journal_invalid");
    }
    return {
      digest: value.digest,
      kind: "regular",
    };
  }
  if (Object.hasOwn(value, "digest")) {
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

function durableMutation(label, mutationPath = null) {
  const adapter = globalThis[TEST_ADAPTER];
  if (typeof adapter?.durableMutation === "function") {
    adapter.durableMutation({
      label,
      path: mutationPath,
    });
  }
}

function beforeJournalClaim() {
  const adapter = globalThis[TEST_ADAPTER];
  if (typeof adapter?.beforeJournalClaim === "function") {
    adapter.beforeJournalClaim();
  }
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
  durableMutation(`directory:created:${path}`);
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
      return 0o666;
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

function createDurableTemporary(path, content, mode, label) {
  let descriptor;
  let created = false;
  let ready = false;
  try {
    descriptor = openSync(path, "wx", mode);
    created = true;
    writeAll(descriptor, content);
    fsyncSync(descriptor);
    closeSync(descriptor);
    descriptor = undefined;
    ready = true;
    durableMutation(`temporary:${label}`);
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

function atomicReplace(path, content, temporaryPath, label) {
  const parent = dirname(path);
  ensureDirectoryDurable(parent);
  let temporaryCreated = false;
  try {
    createDurableTemporary(
      temporaryPath,
      content,
      replacementMode(path),
      label,
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
  durableMutation(label, path);
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
    return stateForContent(readFileSync(path));
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
  return stateForContent(readFileSync(path));
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
    || value.harnesses.some((item) => typeof item !== "string")
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
      !stateMatches(original, priorInventory[key].state)
      || !stateMatches(desired, desiredInventory[key].state)
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

function listJournalTemporaries(project) {
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
    const match = JOURNAL_TEMPORARY_PATTERN.exec(name);
    if (
      match === null
      || !TRANSACTION_ID_PATTERN.test(match[1])
      || !TRANSACTION_ID_PATTERN.test(match[3])
    ) {
      return [];
    }
    return [{
      path: join(directory, name),
      pid: Number(match[2]),
      transactionId: match[1],
    }];
  });
}

function persistJournal(project, journal, label) {
  const path = journalPath(project);
  assertJournalTargetSafe(path);
  atomicReplace(
    path,
    journalContent(journal),
    journalTemporaryPath(project, journal.transaction_id),
    label,
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

function readOwnedJournalTemporary(temporary) {
  let journal;
  try {
    journal = readJournalAtPath(temporary.path);
  } catch {
    throw new Error(
      `installer_journal_temporary_conflict: ${basename(temporary.path)}`,
    );
  }
  if (
    journal === null
    || journal.transaction_id !== temporary.transactionId
  ) {
    throw new Error(
      `installer_journal_temporary_conflict: ${basename(temporary.path)}`,
    );
  }
  return journal;
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
  durableMutation(
    `journal:temporary:removed:${temporary.transactionId}`,
  );
}

function assertOriginalInventory(project, journal, context) {
  const conflicts = [];
  for (const item of Object.values(journal.prior_inventory)) {
    const actual = observeTarget(project, item.target, context);
    if (!stateMatches(actual, item.state)) {
      conflicts.push(targetLabel(item.target));
    }
  }
  if (conflicts.length > 0) {
    throw new Error(
      `installer_recovery_conflict: ${conflicts.sort().join(",")}`,
    );
  }
}

function recoverPublishedJournal(project, context) {
  let journal = readPublishedJournal(project);
  const temporaries = listJournalTemporaries(project);
  if (journal !== null) {
    for (const temporary of temporaries) {
      const candidate = readOwnedJournalTemporary(temporary);
      if (processIsAlive(temporary.pid)) {
        throw new Error("installer_transaction_active");
      }
      if (candidate.transaction_id !== journal.transaction_id) {
        removeOwnedJournalTemporary(temporary);
        continue;
      }
      removeOwnedJournalTemporary(temporary);
    }
    return journal;
  }
  if (temporaries.length === 0) {
    return null;
  }
  if (temporaries.some((temporary) => processIsAlive(temporary.pid))) {
    throw new Error("installer_transaction_active");
  }
  if (temporaries.length !== 1) {
    throw new Error("installer_journal_temporary_conflict: multiple");
  }
  const temporary = temporaries[0];
  journal = readOwnedJournalTemporary(temporary);
  assertOriginalInventory(project, journal, context);
  try {
    linkSync(temporary.path, journalPath(project));
  } catch (error) {
    if (error.code === "EEXIST") {
      throw new Error("installer_transaction_active");
    }
    throw error;
  }
  fsyncDirectory(dirname(journalPath(project)));
  durableMutation("journal:published:recovered", journalPath(project));
  removeOwnedJournalTemporary(temporary);
  return journal;
}

function publishInitialJournal(project, journal) {
  const path = journalPath(project);
  const parent = dirname(path);
  ensureDirectoryDurable(parent);
  assertJournalTargetSafe(path);
  const temporary = {
    path: journalTemporaryPath(project, journal.transaction_id),
    pid: process.pid,
    transactionId: journal.transaction_id,
  };
  let created = false;
  try {
    createDurableTemporary(
      temporary.path,
      journalContent(journal),
      0o666,
      "journal:published",
    );
    created = true;
    try {
      linkSync(temporary.path, path);
    } catch (error) {
      if (error.code === "EEXIST") {
        throw new Error("installer_transaction_active");
      }
      throw error;
    }
    fsyncDirectory(parent);
    durableMutation("journal:published", path);
  } finally {
    if (created && existsSync(temporary.path)) {
      removeOwnedJournalTemporary(temporary);
    }
  }
}

function inspectEndpoints(project, journal, context) {
  const conflicts = [];
  const states = {};
  for (const [key, item] of Object.entries(journal.prior_inventory)) {
    const actual = observeTarget(project, item.target, context);
    const desired = journal.desired_inventory[key].state;
    if (stateMatches(actual, desired)) {
      states[key] = "desired";
    } else if (stateMatches(actual, item.state)) {
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
      && temporary.digest !== operation.desired.digest
    ) {
      conflicts.push(`temporary:${targetLabel(operation.target)}`);
    }
  }
  return {
    conflicts: conflicts.sort(),
    states,
  };
}

function preflightEndpoints(project, journal, context) {
  const { conflicts } = inspectEndpoints(project, journal, context);
  if (conflicts.length > 0) {
    throw new Error(`installer_recovery_conflict: ${conflicts.join(",")}`);
  }
}

function applyOperation(project, journal, operation, context) {
  const actual = observeTarget(project, operation.target, context);
  const path = resolveTarget(project, operation.target, context);
  if (operation.desired.kind === "regular") {
    const temporaryPath = targetTemporaryPath(path, journal, operation);
    const temporary = observeTemporary(
      temporaryPath,
      targetLabel(operation.target),
    );
    if (stateMatches(actual, operation.desired)) {
      if (temporary.kind === "regular") {
        unlinkSync(temporaryPath);
        fsyncDirectory(dirname(temporaryPath));
        durableMutation(`temporary:cleaned:${targetLabel(operation.target)}`);
      }
      return;
    }
    if (!stateMatches(actual, operation.original)) {
      throw new Error(`installer_recovery_conflict: ${targetLabel(operation.target)}`);
    }
    if (temporary.kind === "regular") {
      renameSync(temporaryPath, path);
      fsyncDirectory(dirname(path));
      durableMutation(
        `target:${operation.id}:${targetLabel(operation.target)}`,
        path,
      );
      return;
    }
    const content = decodeDesiredContent(operation);
    atomicReplace(
      path,
      content,
      temporaryPath,
      `target:${operation.id}:${targetLabel(operation.target)}`,
    );
  } else {
    if (stateMatches(actual, operation.desired)) {
      return;
    }
    if (!stateMatches(actual, operation.original)) {
      throw new Error(`installer_recovery_conflict: ${targetLabel(operation.target)}`);
    }
    unlinkSync(path);
    fsyncDirectory(dirname(path));
    durableMutation(`target:${operation.id}:${targetLabel(operation.target)}`);
  }
}

function verifyDesiredInventory(project, journal, context) {
  const conflicts = [];
  for (const item of Object.values(journal.desired_inventory)) {
    const actual = observeTarget(project, item.target, context);
    if (!stateMatches(actual, item.state)) {
      conflicts.push(targetLabel(item.target));
    }
  }
  if (conflicts.length > 0) {
    throw new Error(`installer_recovery_conflict: ${conflicts.sort().join(",")}`);
  }
}

function verifyDesiredInventoryBeforeManifest(project, journal, context) {
  const conflicts = [];
  for (const item of Object.values(journal.desired_inventory)) {
    if (
      item.target.scope === "project"
      && item.target.path === ".herdr-orchestrator/manifest.json"
    ) {
      continue;
    }
    const actual = observeTarget(project, item.target, context);
    if (!stateMatches(actual, item.state)) {
      conflicts.push(targetLabel(item.target));
    }
  }
  if (conflicts.length > 0) {
    throw new Error(`installer_recovery_conflict: ${conflicts.sort().join(",")}`);
  }
}

function completeJournal(project, journal, context) {
  preflightEndpoints(project, journal, context);
  for (let index = 0; index < journal.operations.length; index += 1) {
    const operation = journal.operations[index];
    if (
      operation.target.scope === "project"
      && operation.target.path === ".herdr-orchestrator/manifest.json"
    ) {
      verifyDesiredInventoryBeforeManifest(project, journal, context);
    }
    applyOperation(project, journal, operation, context);
    if (journal.progress.completed_operations < index + 1) {
      journal.progress.completed_operations = index + 1;
      persistJournal(project, journal, `journal:progress:${index + 1}`);
    }
  }
  verifyDesiredInventory(project, journal, context);
  if (journal.progress.phase !== "verified") {
    journal.progress.phase = "verified";
    persistJournal(project, journal, "journal:verified");
  }
  unlinkSync(journalPath(project));
  fsyncDirectory(dirname(journalPath(project)));
  durableMutation("journal:removed");
  return {
    command: journal.command,
    command_result: journal.command_result,
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

export function installerFileState(content) {
  return content === null ? absentState() : stateForContent(content);
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
  const journal = recoverPublishedJournal(project, normalizedContext);
  if (journal === null) {
    return { active: false, recovered: false };
  }
  return {
    active: true,
    ...completeJournal(project, journal, normalizedContext),
  };
}

export function inspectInstallerJournal(context) {
  const project = resolve(context.project);
  const temporaries = listJournalTemporaries(project);
  let journal = readPublishedJournal(project);
  let publication = "published";
  const journalTemporaryConflicts = [];
  if (journal === null && temporaries.length > 0) {
    publication = "temporary";
    try {
      journal = readOwnedJournalTemporary(temporaries[0]);
    } catch {
      journalTemporaryConflicts.push(
        `journal-temporary:${basename(temporaries[0].path)}`,
      );
    }
  }
  if (journal === null) {
    if (journalTemporaryConflicts.length > 0) {
      return {
        active: true,
        conflicts: journalTemporaryConflicts,
        invalid: true,
        publication,
      };
    }
    return { active: false };
  }
  const normalizedContext = contextWithDefaults(context);
  for (const temporary of temporaries) {
    try {
      const candidate = readOwnedJournalTemporary(temporary);
      if (candidate.transaction_id !== journal.transaction_id) {
        journalTemporaryConflicts.push(
          `journal-temporary:${basename(temporary.path)}`,
        );
      }
    } catch {
      journalTemporaryConflicts.push(
        `journal-temporary:${basename(temporary.path)}`,
      );
    }
  }
  if (temporaries.length > 1 && publication === "temporary") {
    journalTemporaryConflicts.push("journal-temporary:multiple");
  }
  const inspection = inspectEndpoints(project, journal, normalizedContext);
  return {
    active: true,
    command: journal.command,
    conflicts: [
      ...inspection.conflicts,
      ...journalTemporaryConflicts,
    ].sort(),
    desired_inventory: journal.desired_inventory,
    harnesses: [...journal.harnesses],
    install_skill: journal.install_skill,
    journal_temporaries: temporaries.map((item) => basename(item.path)).sort(),
    package_version: journal.package_version,
    progress: { ...journal.progress },
    publication,
    states: inspection.states,
    transaction_id: journal.transaction_id,
  };
}

export function runInstallerTransaction(spec) {
  const project = resolve(spec.project);
  if (
    readPublishedJournal(project) !== null
    || listJournalTemporaries(project).length > 0
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
  beforeJournalClaim();
  publishInitialJournal(project, journal);
  return completeJournal(project, journal, normalizedContext);
}
