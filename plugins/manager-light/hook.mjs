#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

import {
  MANAGER_ROLE_TOKEN,
  METADATA_SOURCE,
  classifyPane,
  tokenPatchFor,
} from "./projection.mjs";

export { METADATA_SOURCE };

function runHerdr(args, env = process.env) {
  const result = spawnSync(env.HERDR_BIN_PATH || "herdr", args, {
    encoding: "utf8",
    env,
    timeout: 5_000,
  });
  if (result.error || result.status !== 0) {
    return null;
  }
  return result;
}

function readJson(args, env) {
  const result = runHerdr(args, env);
  if (!result) {
    return null;
  }
  try {
    return JSON.parse(result.stdout);
  } catch {
    return null;
  }
}

function resultValue(payload, key) {
  return payload?.result?.[key] ?? null;
}

export function reportTokenPatch(paneId, patch, env = process.env) {
  const args = [
    "pane",
    "report-metadata",
    paneId,
    "--source",
    METADATA_SOURCE,
  ];
  for (const [name, value] of Object.entries(patch)) {
    if (value === null) {
      args.push("--clear-token", name);
    } else {
      args.push("--token", `${name}=${value}`);
    }
  }
  return runHerdr(args, env) !== null;
}

export function reconcilePane(pane, env = process.env) {
  if (!pane?.pane_id) {
    return false;
  }
  const payload = readJson(
    ["pane", "process-info", "--pane", pane.pane_id],
    env,
  );
  const processInfo = resultValue(payload, "process_info");
  if (!processInfo) {
    return false;
  }
  return reportTokenPatch(
    pane.pane_id,
    tokenPatchFor(classifyPane(pane, processInfo)),
    env,
  );
}

function eventPaneId(payload) {
  return (
    payload?.pane_id ??
    payload?.pane?.pane_id ??
    payload?.data?.pane_id ??
    payload?.data?.pane?.pane_id ??
    null
  );
}

export function reconcileStartup(env = process.env) {
  const payload = readJson(["pane", "list"], env);
  const panes = resultValue(payload, "panes");
  if (!Array.isArray(panes)) {
    return false;
  }
  let succeeded = true;
  for (const pane of panes) {
    if (pane?.agent || pane?.tokens?.[MANAGER_ROLE_TOKEN] === "manager") {
      succeeded = reconcilePane(pane, env) && succeeded;
    }
  }
  return succeeded;
}

export function reconcileEvent(env = process.env) {
  let payload;
  try {
    payload = JSON.parse(env.HERDR_PLUGIN_EVENT_JSON || "{}");
  } catch {
    return false;
  }
  const paneId = eventPaneId(payload);
  if (!paneId) {
    return false;
  }
  const response = readJson(["pane", "get", paneId], env);
  const pane = resultValue(response, "pane");
  return pane ? reconcilePane(pane, env) : false;
}

export function main(env = process.env) {
  return !env.HERDR_PLUGIN_EVENT || env.HERDR_PLUGIN_EVENT === "startup"
    ? reconcileStartup(env)
    : reconcileEvent(env);
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  process.exitCode = main() ? 0 : 1;
}
