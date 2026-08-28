import { basename } from "node:path";

export const MANAGER_ROLE_TOKEN = "hml_role";
export const METADATA_SOURCE = "herdr-manager-light";

export const VISIBLE_TOKENS = Object.freeze({
  manager: Object.freeze({ name: "hml_manager", value: "●" }),
  blocked: Object.freeze({ name: "hml_blocked", value: "●" }),
  working: Object.freeze({ name: "hml_working", value: "●" }),
  idle: Object.freeze({ name: "hml_idle", value: "○" }),
  unknown: Object.freeze({ name: "hml_unknown", value: "○" }),
});

export const OWNED_TOKENS = Object.freeze([
  MANAGER_ROLE_TOKEN,
  ...Object.values(VISIBLE_TOKENS).map(({ name }) => name),
]);

const CLASSIFICATIONS = new Set([
  ...Object.keys(VISIBLE_TOKENS),
  "absent",
]);

function executableName(value) {
  return typeof value === "string" && value.length > 0
    ? basename(value).toLowerCase()
    : "";
}

function isDirectManagerProcess(process) {
  const names = [process?.name, process?.argv0, process?.argv?.[0]];
  if (names.some((value) => executableName(value) === "herdr-manager")) {
    return true;
  }

  const argv = Array.isArray(process?.argv) ? process.argv : [];
  return (
    ["node", "node.exe"].includes(executableName(argv[0])) &&
    executableName(argv[1]) === "herdr-manager"
  );
}

function isOrchestratorManagerProcess(process) {
  const argv = Array.isArray(process?.argv)
    ? process.argv.filter((value) => typeof value === "string")
    : [];
  const names = new Set(["herdr-orchestrator", "herdr-orchestrator.mjs"]);

  for (let index = 0; index < argv.length - 1; index += 1) {
    if (names.has(executableName(argv[index])) && argv[index + 1] === "manager") {
      return true;
    }
  }

  const processName = executableName(process?.argv0 || process?.name);
  return names.has(processName) && argv[0] === "manager";
}

function foregroundProcesses(processInfo) {
  if (Array.isArray(processInfo?.foreground_processes)) {
    return processInfo.foreground_processes;
  }
  if (Array.isArray(processInfo?.processes)) {
    return processInfo.processes;
  }
  return [];
}

export function hasManagerProcess(processInfo) {
  return foregroundProcesses(processInfo).some(
    (process) =>
      isDirectManagerProcess(process) || isOrchestratorManagerProcess(process),
  );
}

export function classifyPane(pane, processInfo = {}) {
  if (hasManagerProcess(processInfo)) {
    return "manager";
  }

  if (!pane?.agent) {
    return "absent";
  }

  switch (pane.agent_status) {
    case "blocked":
      return "blocked";
    case "working":
      return "working";
    case "idle":
    case "done":
      return "idle";
    default:
      return "unknown";
  }
}

export function tokenPatchFor(classification) {
  if (!CLASSIFICATIONS.has(classification)) {
    throw new TypeError(`Unknown manager-light classification: ${classification}`);
  }

  const patch = Object.fromEntries(OWNED_TOKENS.map((name) => [name, null]));
  if (classification === "absent") {
    return patch;
  }

  if (classification === "manager") {
    patch[MANAGER_ROLE_TOKEN] = "manager";
  }
  const token = VISIBLE_TOKENS[classification];
  patch[token.name] = token.value;
  return patch;
}
