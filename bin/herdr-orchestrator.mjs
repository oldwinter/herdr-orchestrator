#!/usr/bin/env node

import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  rmdirSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const HARNESSES = ["droid", "grok", "codex", "pi", "claude", "hermes"];
const WORKER_NAMES = {
  droid: "operations",
  grok: "grok-build",
  codex: "implementation",
  pi: "quick-analysis",
  claude: "deep-review",
  hermes: "research",
};

function parseArguments(argv) {
  const command = argv[0] ?? "help";
  const options = { command, harnesses: [], project: process.cwd(), rest: [] };
  for (let index = 1; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--project") {
      if (index + 1 >= argv.length) {
        throw new Error("option_value_required: --project");
      }
      options.project = argv[++index];
    } else if (
      ["install", "update", "upgrade"].includes(command)
      && value === "--harness"
    ) {
      if (index + 1 >= argv.length) {
        throw new Error("option_value_required: --harness");
      }
      options.harnesses.push(argv[++index]);
    } else {
      options.rest.push(value);
    }
  }
  return options;
}

function sha256(content) {
  return createHash("sha256").update(content).digest("hex");
}

function stageFile(desiredFiles, relativePath, content) {
  desiredFiles.set(relativePath, Buffer.from(content));
}

function writeManagedFile(project, relativePath, content) {
  const target = join(project, relativePath);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, content);
}

function loadManifest(project) {
  const path = join(project, ".herdr-orchestrator/manifest.json");
  if (!existsSync(path)) {
    return null;
  }
  const manifest = JSON.parse(readFileSync(path, "utf8"));
  if (
    manifest.schema_version !== 1
    || manifest.package !== "herdr-orchestrator"
    || typeof manifest.version !== "string"
    || typeof manifest.files !== "object"
    || manifest.files === null
    || Array.isArray(manifest.files)
    || !Array.isArray(manifest.harnesses)
    || manifest.harnesses.length === 0
    || new Set(manifest.harnesses).size !== manifest.harnesses.length
    || manifest.harnesses.some((harness) => !HARNESSES.includes(harness))
  ) {
    throw new Error("manifest_invalid");
  }
  for (const [relativePath, hash] of Object.entries(manifest.files)) {
    if (!isManagedPath(relativePath) || !/^[a-f0-9]{64}$/.test(hash)) {
      throw new Error(`manifest_entry_invalid: ${relativePath}`);
    }
  }
  return manifest;
}

function isManagedPath(relativePath) {
  if (
    typeof relativePath !== "string"
    || relativePath.startsWith("/")
    || relativePath.includes("\\")
    || relativePath.split("/").includes("..")
  ) {
    return false;
  }
  return (
    relativePath.startsWith(".herdr-orchestrator/")
    || relativePath.startsWith(".agents/skills/herdr-orchestrator/")
    || relativePath === ".orchestrator/.gitignore"
  );
}

function assertNoSymlink(project, relativePath) {
  let current = project;
  for (const part of relativePath.split("/")) {
    current = join(current, part);
    try {
      if (lstatSync(current).isSymbolicLink()) {
        throw new Error(`managed_path_symlink: ${relativePath}`);
      }
    } catch (error) {
      if (error.code !== "ENOENT") {
        throw error;
      }
    }
  }
}

function install(options) {
  const project = resolve(options.project);
  if (!existsSync(project)) {
    throw new Error(`project_not_found: ${project}`);
  }
  if (!lstatSync(project).isDirectory()) {
    throw new Error(`project_not_directory: ${project}`);
  }
  assertNoSymlink(project, ".herdr-orchestrator/manifest.json");
  const previous = loadManifest(project);
  const harnesses = options.harnesses.length > 0
    ? [...new Set(options.harnesses)]
    : previous?.harnesses ?? HARNESSES.filter((harness) => commandExists(harness));
  if (harnesses.length === 0) {
    throw new Error("no_harness_detected: pass --harness <name>");
  }
  for (const harness of harnesses) {
    if (!HARNESSES.includes(harness)) {
      throw new Error(`unsupported_harness: ${harness}`);
    }
  }

  const desiredFiles = new Map();
  const workflow = renderWorkflow(harnesses);
  stageFile(
    desiredFiles,
    ".herdr-orchestrator/workflows/multi-harness.toml",
    workflow,
  );
  stageFile(
    desiredFiles,
    ".herdr-orchestrator/workflows/prompts/planner.md",
    readFileSync(join(PACKAGE_ROOT, "workflows/prompts/planner.md")),
  );
  for (const harness of harnesses) {
    for (const extension of ["toml", "md"]) {
      stageFile(
        desiredFiles,
        `.herdr-orchestrator/profiles/harnesses/${harness}.${extension}`,
        readFileSync(join(PACKAGE_ROOT, `profiles/harnesses/${harness}.${extension}`)),
      );
    }
  }
  const skillRoot = join(PACKAGE_ROOT, "skills/herdr-orchestrator");
  for (const relativePath of listSkillFiles()) {
    stageFile(
      desiredFiles,
      `.agents/skills/herdr-orchestrator/${relativePath}`,
      readFileSync(join(skillRoot, relativePath)),
    );
  }
  stageFile(desiredFiles, ".orchestrator/.gitignore", "*\n!.gitignore\n");

  const previousFiles = previous?.files ?? {};
  const conflicts = [];
  const preserved = [];
  const removals = [];
  const unmanaged = [];
  const manifestFiles = {};
  for (const [relativePath, content] of desiredFiles) {
    assertNoSymlink(project, relativePath);
    const target = join(project, relativePath);
    const desiredHash = sha256(content);
    if (!existsSync(target)) {
      manifestFiles[relativePath] = desiredHash;
      continue;
    }
    const currentHash = sha256(readFileSync(target));
    const previousHash = previousFiles[relativePath];
    if (previousHash === undefined) {
      if (currentHash !== desiredHash) {
        conflicts.push(relativePath);
      } else {
        unmanaged.push(relativePath);
      }
    } else if (previousHash !== undefined && currentHash !== previousHash) {
      preserved.push(relativePath);
      manifestFiles[relativePath] = previousHash;
    } else {
      manifestFiles[relativePath] = desiredHash;
    }
  }
  for (const [relativePath, previousHash] of Object.entries(previousFiles)) {
    if (desiredFiles.has(relativePath)) {
      continue;
    }
    assertNoSymlink(project, relativePath);
    const target = join(project, relativePath);
    if (!existsSync(target)) {
      continue;
    }
    if (sha256(readFileSync(target)) === previousHash) {
      removals.push(relativePath);
    } else {
      preserved.push(relativePath);
      manifestFiles[relativePath] = previousHash;
    }
  }
  if (conflicts.length > 0) {
    throw new Error(`unmanaged_file_conflict: ${conflicts.sort().join(",")}`);
  }
  for (const relativePath of removals) {
    unlinkSync(join(project, relativePath));
  }
  for (const [relativePath, content] of desiredFiles) {
    if (!preserved.includes(relativePath) && !unmanaged.includes(relativePath)) {
      writeManagedFile(project, relativePath, content);
    }
  }

  const manifest = {
    schema_version: 1,
    package: "herdr-orchestrator",
    version: packageVersion(),
    harnesses,
    files: manifestFiles,
  };
  mkdirSync(join(project, ".herdr-orchestrator"), { recursive: true });
  writeFileSync(
    join(project, ".herdr-orchestrator/manifest.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
  process.stdout.write(`${JSON.stringify({
    harnesses,
    manifest: ".herdr-orchestrator/manifest.json",
    ok: preserved.length === 0,
    preserved: preserved.sort(),
    project,
    unmanaged: unmanaged.sort(),
    workflow: ".herdr-orchestrator/workflows/multi-harness.toml",
  })}\n`);
  if (preserved.length > 0) {
    process.exitCode = 1;
  }
}

function listSkillFiles() {
  return ["SKILL.md"];
}

function packageVersion() {
  return JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8")).version;
}

function commandExists(command) {
  const result = spawnSync(command, ["--version"], {
    encoding: "utf8",
    timeout: 5_000,
  });
  return result.status === 0;
}

function inspectInstallation(project) {
  const manifestPath = join(project, ".herdr-orchestrator/manifest.json");
  assertNoSymlink(project, ".herdr-orchestrator/manifest.json");
  if (!existsSync(manifestPath)) {
    return {
      manifest: false,
      missing: [".herdr-orchestrator/manifest.json"],
      modified: [],
      ok: false,
    };
  }
  const manifest = loadManifest(project);
  const missing = [];
  const modified = [];
  for (const [relativePath, expectedHash] of Object.entries(manifest.files ?? {})) {
    assertNoSymlink(project, relativePath);
    const target = join(project, relativePath);
    if (!existsSync(target)) {
      missing.push(relativePath);
    } else if (sha256(readFileSync(target)) !== expectedHash) {
      modified.push(relativePath);
    }
  }
  return {
    manifest: true,
    missing: missing.sort(),
    modified: modified.sort(),
    ok: missing.length === 0 && modified.length === 0,
    version: manifest.version,
  };
}

function doctor(options) {
  const project = resolve(options.project);
  const installation = inspectInstallation(project);
  const workflow = join(project, ".herdr-orchestrator/workflows/multi-harness.toml");
  let runtime = {
    checks: [],
    ok: false,
  };
  if (existsSync(workflow)) {
    const python = process.env.PYTHON ?? "python3";
    const result = spawnSync(
      python,
      ["-m", "herdr_orchestrator", "doctor", "--workflow", workflow],
      {
        cwd: project,
        encoding: "utf8",
        env: {
          ...process.env,
          PYTHONPATH: join(PACKAGE_ROOT, "src"),
        },
      },
    );
    if (result.error) {
      runtime = { error: result.error.message, ok: false };
    } else {
      try {
        runtime = JSON.parse(result.stdout);
      } catch {
        runtime = {
          error: result.stderr.trim() || "runtime_doctor_invalid_output",
          ok: false,
        };
      }
    }
  }
  const ok = installation.ok && runtime.ok;
  process.stdout.write(`${JSON.stringify({ installation, ok, project, runtime })}\n`);
  if (!ok) {
    process.exitCode = 1;
  }
}

function uninstall(options) {
  const project = resolve(options.project);
  const manifestPath = join(project, ".herdr-orchestrator/manifest.json");
  assertNoSymlink(project, ".herdr-orchestrator/manifest.json");
  if (!existsSync(manifestPath)) {
    throw new Error(`installation_not_found: ${project}`);
  }
  const manifest = loadManifest(project);
  const preserved = [];
  for (const [relativePath, expectedHash] of Object.entries(manifest.files)) {
    assertNoSymlink(project, relativePath);
    const target = join(project, relativePath);
    if (!existsSync(target)) {
      continue;
    }
    if (sha256(readFileSync(target)) === expectedHash) {
      unlinkSync(target);
    } else {
      preserved.push(relativePath);
    }
  }
  unlinkSync(manifestPath);
  for (const relativePath of [
    ".herdr-orchestrator/profiles/harnesses",
    ".herdr-orchestrator/profiles",
    ".herdr-orchestrator/workflows/prompts",
    ".herdr-orchestrator/workflows",
    ".herdr-orchestrator",
    ".agents/skills/herdr-orchestrator",
  ]) {
    try {
      rmdirSync(join(project, relativePath));
    } catch (error) {
      if (!["ENOENT", "ENOTEMPTY"].includes(error.code)) {
        throw error;
      }
    }
  }
  const ok = preserved.length === 0;
  process.stdout.write(`${JSON.stringify({ ok, preserved: preserved.sort(), project })}\n`);
  if (!ok) {
    process.exitCode = 1;
  }
}

function renderWorkflow(harnesses) {
  const workerHarnesses = harnesses.map((item) => `"${item}"`).join(", ");
  const workers = harnesses.map((harness) => `
[[workers]]
name = "${WORKER_NAMES[harness]}"
harness = "${harness}"
`).join("");
  return `schema_version = 1
name = "multi-harness"
workspace = "../.."
state_db = "../../.orchestrator/state.db"
profiles_dir = "../profiles/harnesses"

[coordinator]
poll_seconds = 5
max_parallel = ${Math.min(harnesses.length, 6)}
lease_seconds = 900
max_attempts = 2
agent_timeout_seconds = 300

[placement]
mode = "hybrid"
worktree_root = ".orchestrator/worktrees"

[standardized_delivery]
tracker_backend = "local-markdown"
tracker_root = ".scratch/standardized-delivery"
artifact_root = ".orchestrator/deliveries"
wayfinder = "auto"
max_parallel = 3
review_repair_rounds = 2

[planner]
enabled = false
harness = "auto"
worker_harnesses = [${workerHarnesses}]
interval_seconds = 1800
prompt_file = "prompts/planner.md"
output_file = "../../.orchestrator/plans/multi-harness.json"
max_tasks = 20
${workers}`;
}

function runRuntime(options) {
  const workflow = join(
    resolve(options.project),
    ".herdr-orchestrator/workflows/multi-harness.toml",
  );
  if (!existsSync(workflow)) {
    throw new Error(`installation_not_found: run herdr-orchestrator install --project ${resolve(options.project)}`);
  }
  const python = process.env.PYTHON ?? "python3";
  const result = spawnSync(
    python,
    ["-m", "herdr_orchestrator", options.command, "--workflow", workflow, ...options.rest],
    {
      cwd: resolve(options.project),
      env: {
        ...process.env,
        PYTHONPATH: join(PACKAGE_ROOT, "src"),
      },
      stdio: "inherit",
    },
  );
  if (result.error) {
    throw result.error;
  }
  process.exitCode = result.status ?? 1;
}

function printHelp() {
  process.stdout.write(`Usage: herdr-orchestrator <command> [options]

Setup:
  install --project <path> [--harness <name> ...]
  upgrade --project <path> [--harness <name> ...]
  doctor --project <path>
  uninstall --project <path>

Runtime:
  doctor | catalog | profile | seed | status | enqueue | run | dashboard | smoke

Options:
  --project <path>   Target repository (default: current directory)
  --harness <name>   Harness to enable during install; repeat for more
  --version          Print the package version
`);
}

function main() {
  try {
    const options = parseArguments(process.argv.slice(2));
    if (["install", "update", "upgrade"].includes(options.command)) {
      install(options);
    } else if (options.command === "doctor") {
      doctor(options);
    } else if (options.command === "uninstall") {
      uninstall(options);
    } else if (options.command === "--version" || options.command === "-v") {
      process.stdout.write(`${packageVersion()}\n`);
    } else if (options.command === "help" || options.command === "--help") {
      printHelp();
    } else {
      runRuntime(options);
    }
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 2;
  }
}

main();
