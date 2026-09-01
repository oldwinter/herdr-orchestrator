const MODULE_SUFFIX = "/bin/installer-journal.mjs";
const CLI_MODULE_SUFFIX = "/bin/herdr-orchestrator.mjs";

const TEST_RUNTIME = String.raw`
import { createHash as __faultCreateHash } from "node:crypto";
import {
  appendFileSync as __faultAppendFileSync,
  existsSync as __faultExistsSync,
  mkdirSync as __faultMkdirSync,
  readFileSync as __faultReadFileSync,
  writeFileSync as __faultWriteFileSync,
} from "node:fs";
import { join as __faultJoin } from "node:path";

const __faultWaitArray = new Int32Array(new SharedArrayBuffer(4));
let __faultMutationCount = 0;

function __faultRequestedMutation(name) {
  const value = Number(process.env[name]);
  return Number.isInteger(value) && value > 0 ? value : null;
}

function __faultWaitAtBarrier(directory) {
  __faultMkdirSync(directory, { recursive: true });
  __faultWriteFileSync(__faultJoin(directory, String(process.pid) + ".ready"), "");
  const release = __faultJoin(directory, "release");
  const deadline = Date.now() + 10_000;
  while (!__faultExistsSync(release)) {
    if (Date.now() >= deadline) {
      throw new Error("installer_test_barrier_timeout");
    }
    Atomics.wait(__faultWaitArray, 0, 0, 10);
  }
}

function __faultBeforeJournalClaim() {
  if (process.env.HERDR_ORCHESTRATOR_TEST_FAIL_ON_JOURNAL_CLAIM === "1") {
    throw new Error("installer_test_unexpected_journal_claim");
  }
  const barrier = process.env.HERDR_ORCHESTRATOR_TEST_JOURNAL_CLAIM_BARRIER;
  if (barrier) {
    __faultWaitAtBarrier(barrier);
  }
}

function __faultPauseAfterPreservedDiscovery() {
  const barrier = process.env.HERDR_ORCHESTRATOR_TEST_PRESERVED_DISCOVERY_BARRIER;
  if (barrier) {
    __faultWaitAtBarrier(barrier);
  }
}

function __faultPauseAfterPlanning() {
  const barrier = process.env.HERDR_ORCHESTRATOR_TEST_PLANNING_BARRIER;
  if (barrier) {
    __faultWaitAtBarrier(barrier);
  }
}

function __faultOperationId(temporaryPath) {
  return /-(operation-[1-9][0-9]*)\.tmp$/.exec(temporaryPath)?.[1] ?? null;
}

function __faultTemporaryLabel(path, temporaryPath) {
  const operationId = __faultOperationId(temporaryPath);
  return operationId === null
    ? "temporary:journal:" + path
    : "temporary:target:" + operationId + ":" + path;
}

function __faultTargetLabel(path, temporaryPath) {
  const operationId = __faultOperationId(temporaryPath);
  return operationId === null
    ? "journal:updated:" + path
    : "target:" + operationId + ":" + path;
}

function __faultMutation(label, path = null) {
  __faultMutationCount += 1;
  const mutationLog = process.env.HERDR_ORCHESTRATOR_TEST_MUTATION_LOG;
  if (mutationLog) {
    let fingerprint = null;
    if (path !== null && __faultExistsSync(path)) {
      fingerprint = __faultCreateHash("sha256")
        .update(__faultReadFileSync(path))
        .digest("hex");
    }
    __faultAppendFileSync(
      mutationLog,
      JSON.stringify({ fingerprint, label }) + "\n",
    );
  }
  const pausePrefix =
    process.env.HERDR_ORCHESTRATOR_TEST_PAUSE_AT_LABEL_PREFIX;
  const pauseBarrier = process.env.HERDR_ORCHESTRATOR_TEST_PAUSE_BARRIER;
  if (pausePrefix && pauseBarrier && label.startsWith(pausePrefix)) {
    __faultWaitAtBarrier(pauseBarrier);
  }
  if (
    __faultRequestedMutation(
      "HERDR_ORCHESTRATOR_TEST_REWRITE_AFTER_MUTATION",
    ) === __faultMutationCount
    || label.startsWith(
      process.env.HERDR_ORCHESTRATOR_TEST_REWRITE_AT_LABEL_PREFIX ?? "\u0000",
    )
  ) {
    if (path === null) {
      throw new Error("installer_test_rewrite_unavailable: " + label);
    }
    __faultWriteFileSync(
      path,
      Buffer.from("installer test user edit after durable mutation\n"),
    );
  }
  if (
    __faultRequestedMutation(
      "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AFTER_MUTATION",
    ) === __faultMutationCount
    || label === process.env.HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL
    || label.startsWith(
      process.env.HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL_PREFIX ?? "\u0000",
    )
  ) {
    process.stderr.write("installer_test_interruption: " + label + "\n");
    process.exit(86);
  }
}
`;

function replaceExact(source, before, after, count = 1) {
  const pieces = source.split(before);
  if (pieces.length - 1 !== count) {
    throw new Error(
      `installer_fault_loader_anchor_mismatch: expected ${count}, found ${pieces.length - 1}`,
    );
  }
  return pieces.join(after);
}

function instrument(source) {
  let result = source;
  result = replaceExact(
    result,
    "  fsyncDirectory(parent);\n}\n\nfunction writeAll",
    "  fsyncDirectory(parent);\n"
      + "  __faultMutation(`directory:created:${path}`);\n"
      + "}\n\nfunction writeAll",
  );
  result = replaceExact(
    result,
    "    createDurableTemporary(\n"
      + "      temporaryPath,\n"
      + "      content,\n"
      + "      mode,\n"
      + "    );\n"
      + "    temporaryCreated = true;",
    "    createDurableTemporary(\n"
      + "      temporaryPath,\n"
      + "      content,\n"
      + "      mode,\n"
      + "    );\n"
      + "    __faultMutation(__faultTemporaryLabel(path, temporaryPath), temporaryPath);\n"
      + "    temporaryCreated = true;",
  );
  result = replaceExact(
    result,
    "    renameSync(temporaryPath, path);\n    fsyncDirectory(parent);",
    "    renameSync(temporaryPath, path);\n"
      + "    fsyncDirectory(parent);\n"
      + "    __faultMutation(__faultTargetLabel(path, temporaryPath), path);",
  );
  result = replaceExact(
    result,
    "  const preservedTargets = legacyPreservedTargets(\n"
      + "    project,\n"
      + "    journal,\n"
      + "    context,\n"
      + "  );\n"
      + "  const legacyProjectPreserved = legacyProjectPreservationConflict(",
    "  const preservedTargets = legacyPreservedTargets(\n"
      + "    project,\n"
      + "    journal,\n"
      + "    context,\n"
      + "  );\n"
      + "  __faultPauseAfterPreservedDiscovery();\n"
      + "  const legacyProjectPreserved = legacyProjectPreservationConflict(",
  );
  result = replaceExact(
    result,
    "        setRegularFileMode(temporaryPath, actual.mode);\n",
    "        setRegularFileMode(temporaryPath, actual.mode);\n"
      + "        __faultMutation(\n"
      + "          `temporary:target:mode-adjusted:${targetLabel(operation.target)}`,\n"
      + "          temporaryPath,\n"
      + "        );\n",
  );
  result = replaceExact(
    result,
    "function removeOwnedJournalTemporary(temporary) {\n"
      + "  unlinkSync(temporary.path);\n"
      + "  fsyncDirectory(dirname(temporary.path));\n"
      + "}",
    "function removeOwnedJournalTemporary(temporary) {\n"
      + "  unlinkSync(temporary.path);\n"
      + "  fsyncDirectory(dirname(temporary.path));\n"
      + "  __faultMutation(`journal:temporary:removed:${temporary.transactionId}`);\n"
      + "}",
  );
  result = replaceExact(
    result,
    "function removeJournalOwner(owner) {\n"
      + "  unlinkSync(owner.path);\n"
      + "  fsyncDirectory(dirname(owner.path));\n"
      + "}",
    "function removeJournalOwner(owner) {\n"
      + "  unlinkSync(owner.path);\n"
      + "  fsyncDirectory(dirname(owner.path));\n"
      + "  __faultMutation(`journal:owner:removed:${owner.transactionId}`);\n"
      + "}",
  );
  result = replaceExact(
    result,
    "  fsyncDirectory(dirname(owner.path));\n  return owner;",
    "  fsyncDirectory(dirname(owner.path));\n"
      + "  __faultMutation(`journal:owner:adopted:${owner.transactionId}`, owner.path);\n"
      + "  return owner;",
    2,
  );
  result = replaceExact(
    result,
    "    fsyncDirectory(dirname(journalPath(project)));\n  }\n"
      + "  for (const record of [...ownerRecords, ...temporaryRecords]) {",
    "    fsyncDirectory(dirname(journalPath(project)));\n"
      + "    __faultMutation(\"journal:published:recovered\", journalPath(project));\n"
      + "  }\n"
      + "  for (const record of [...ownerRecords, ...temporaryRecords]) {",
  );
  result = replaceExact(
    result,
    "    createDurableTemporary(\n"
      + "      owner.path,\n"
      + "      journalContent(journal),\n"
      + "      replacementMode(path),\n"
      + "    );\n"
      + "    try {",
    "    createDurableTemporary(\n"
      + "      owner.path,\n"
      + "      journalContent(journal),\n"
      + "      replacementMode(path),\n"
      + "    );\n"
      + "    __faultMutation(\"temporary:journal:owner:created\", owner.path);\n"
      + "    try {",
  );
  result = replaceExact(
    result,
    "    linked = true;\n    fsyncDirectory(parent);\n    return owner;",
    "    linked = true;\n"
      + "    fsyncDirectory(parent);\n"
      + "    __faultMutation(\"journal:published\", path);\n"
      + "    return owner;",
  );
  result = replaceExact(
    result,
    "        unlinkSync(temporaryPath);\n"
      + "        fsyncDirectory(dirname(temporaryPath));\n"
      + "      }\n"
      + "      return;",
    "        unlinkSync(temporaryPath);\n"
      + "        fsyncDirectory(dirname(temporaryPath));\n"
      + "        __faultMutation(`temporary:cleaned:${targetLabel(operation.target)}`);\n"
      + "      }\n"
      + "      return;",
    2,
  );
  result = replaceExact(
    result,
    "      renameSync(temporaryPath, path);\n"
      + "      fsyncDirectory(dirname(path));\n"
      + "      return;",
    "      renameSync(temporaryPath, path);\n"
      + "      fsyncDirectory(dirname(path));\n"
      + "      __faultMutation(\n"
      + "        `target:${operation.id}:${targetLabel(operation.target)}`,\n"
      + "        path,\n"
      + "      );\n"
      + "      return;",
  );
  result = replaceExact(
    result,
    "    unlinkSync(path);\n    fsyncDirectory(dirname(path));\n  }\n}\n\n"
      + "function verifyDesiredInventory",
    "    unlinkSync(path);\n"
      + "    fsyncDirectory(dirname(path));\n"
      + "    __faultMutation(`target:${operation.id}:${targetLabel(operation.target)}`);\n"
      + "  }\n"
      + "}\n\nfunction verifyDesiredInventory",
  );
  result = replaceExact(
    result,
    "  unlinkSync(journalPath(project));\n"
      + "  fsyncDirectory(dirname(journalPath(project)));\n"
      + "  removeJournalOwner(owner);",
    "  unlinkSync(journalPath(project));\n"
      + "  fsyncDirectory(dirname(journalPath(project)));\n"
      + "  __faultMutation(\"journal:removed\");\n"
      + "  removeJournalOwner(owner);",
  );
  result = replaceExact(
    result,
    "  const owner = publishInitialJournal(project, journal);",
    "  __faultBeforeJournalClaim();\n"
      + "  const owner = publishInitialJournal(project, journal);",
  );
  return `${TEST_RUNTIME}\n${result}`;
}

function instrumentCli(source) {
  const newline = source.indexOf("\n");
  const shebang = source.startsWith("#!") && newline >= 0
    ? source.slice(0, newline + 1)
    : "";
  let result = source.slice(shebang.length);
  if (result.includes("  const manifest = {\n")) {
    result = replaceExact(
      result,
      "  const manifest = {\n",
      "  __faultPauseAfterPlanning();\n  const manifest = {\n",
    );
  }
  if (result.includes("  const localExclude = desiredUninstallGitExclude(\n")) {
    result = replaceExact(
      result,
      "  const localExclude = desiredUninstallGitExclude(\n",
      "  __faultPauseAfterPlanning();\n  const localExclude = desiredUninstallGitExclude(\n",
    );
  }
  return `${shebang}${TEST_RUNTIME}\n${result}`;
}

export async function load(url, context, nextLoad) {
  const loaded = await nextLoad(url, context);
  if (!url.endsWith(MODULE_SUFFIX) && !url.endsWith(CLI_MODULE_SUFFIX)) {
    return loaded;
  }
  return {
    ...loaded,
    source: url.endsWith(MODULE_SUFFIX)
      ? instrument(Buffer.from(loaded.source).toString("utf8"))
      : instrumentCli(Buffer.from(loaded.source).toString("utf8")),
  };
}
