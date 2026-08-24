#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { appendFileSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

const PACKAGE_NAME = /^(?:@[a-z0-9][a-z0-9._~-]*\/)?[a-z0-9][a-z0-9._~-]*$/;
const PACKAGE_VERSION = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;

function packageJsonPath(argv) {
  if (argv.length !== 2 || argv[0] !== "--package-json" || !argv[1]) {
    throw new Error("usage: npm-release-plan --package-json <path>");
  }
  return resolve(argv[1]);
}

function packageIdentity(path) {
  const payload = JSON.parse(readFileSync(path, "utf8"));
  if (
    typeof payload.name !== "string"
    || payload.name.length > 214
    || !PACKAGE_NAME.test(payload.name)
    || typeof payload.version !== "string"
    || !PACKAGE_VERSION.test(payload.version)
  ) {
    throw new Error("package_identity_invalid");
  }
  return {
    name: payload.name,
    version: payload.version,
  };
}

function registryVersions(name) {
  const result = spawnSync("npm", ["view", name, "versions", "--json"], {
    encoding: "utf8",
    timeout: 30_000,
  });
  if (result.error || result.status !== 0) {
    throw new Error("npm_registry_query_failed");
  }
  const payload = JSON.parse(result.stdout);
  if (!Array.isArray(payload) || payload.some((version) => typeof version !== "string")) {
    throw new Error("npm_registry_response_invalid");
  }
  return payload;
}

function main() {
  try {
    const identity = packageIdentity(packageJsonPath(process.argv.slice(2)));
    const exists = registryVersions(identity.name).includes(identity.version);
    const plan = {
      name: identity.name,
      publish: !exists,
      reason: exists ? "version_exists" : "version_missing",
      version: identity.version,
    };
    if (process.env.GITHUB_OUTPUT) {
      appendFileSync(
        process.env.GITHUB_OUTPUT,
        [
          `name=${plan.name}`,
          `version=${plan.version}`,
          `publish=${String(plan.publish)}`,
          `reason=${plan.reason}`,
          "",
        ].join("\n"),
        "utf8",
      );
    }
    process.stdout.write(`${JSON.stringify(plan)}\n`);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 2;
  }
}

main();
