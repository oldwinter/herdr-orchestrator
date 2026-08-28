#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const cli = require.resolve("herdr-orchestrator/bin/herdr-orchestrator.mjs");
const result = spawnSync(
  process.execPath,
  [cli, "manager", ...process.argv.slice(2)],
  {
    env: process.env,
    stdio: "inherit",
  },
);

if (result.error) {
  throw result.error;
}
process.exitCode = result.status ?? 1;
