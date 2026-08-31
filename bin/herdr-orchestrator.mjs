#!/usr/bin/env node

import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  lstatSync,
  readdirSync,
  readFileSync,
  rmdirSync,
} from "node:fs";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { TextDecoder } from "node:util";
import { fileURLToPath } from "node:url";

import {
  installManagerLight,
  managerLightStatus,
  uninstallManagerLight,
} from "../plugins/manager-light/configure.mjs";
import {
  METADATA_SOURCE,
  tokenPatchFor,
} from "../plugins/manager-light/projection.mjs";
import {
  installerFileState,
  inspectInstallerJournal,
  observeInstallerTarget,
  reconcileInstallerJournal,
  runInstallerTransaction,
} from "./installer-journal.mjs";

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const HARNESSES = ["droid", "grok", "codex", "pi", "claude", "hermes"];
const MANAGER_HARNESSES = ["grok", "codex", "claude"];
const DOCTOR_PROBE_TIMEOUT_OPTION = "--probe-timeout-seconds";
const DOCTOR_PROBE_TIMEOUT_SECONDS = 30;
const DOCTOR_PROBE_TIMEOUT_MAX_SECONDS = 300;
const DOCTOR_PROCESS_OVERHEAD_MS = 90_000;
const DOCTOR_PROCESS_TIMEOUT_ENV = "HERDR_ORCHESTRATOR_DOCTOR_TIMEOUT_MS";
const WORKFLOW_OPTION_PREFIXES = [
  "--w",
  "--wo",
  "--wor",
  "--work",
  "--workf",
  "--workfl",
  "--workflo",
  "--workflow",
];
const GIT_EXCLUDE_BEGIN = "# BEGIN herdr-orchestrator managed paths";
const GIT_EXCLUDE_END = "# END herdr-orchestrator managed paths";
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
  const options = {
    command,
    harnesses: [],
    installSkill: null,
    project: process.cwd(),
    projectExplicit: false,
    rest: [],
  };
  for (let index = 1; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--project") {
      if (index + 1 >= argv.length) {
        throw new Error("option_value_required: --project");
      }
      options.project = argv[++index];
      options.projectExplicit = true;
    } else if (
      ["install", "update", "upgrade", "manager"].includes(command)
      && value === "--harness"
    ) {
      if (index + 1 >= argv.length) {
        throw new Error("option_value_required: --harness");
      }
      options.harnesses.push(argv[++index]);
    } else if (
      ["install", "update", "upgrade"].includes(command)
      && ["--install-skill", "--skip-skill"].includes(value)
    ) {
      const requested = value === "--install-skill";
      if (options.installSkill !== null && options.installSkill !== requested) {
        throw new Error("skill_install_options_conflict");
      }
      options.installSkill = requested;
    } else {
      options.rest.push(value);
    }
  }
  if (
    ["install", "update", "upgrade", "uninstall"].includes(command)
    && options.rest.length > 0
  ) {
    throw new Error(`option_unsupported: ${options.rest[0]}`);
  }
  return options;
}

function sha256(content) {
  return createHash("sha256").update(content).digest("hex");
}

function stageFile(desiredFiles, relativePath, content) {
  desiredFiles.set(relativePath, Buffer.from(content));
}

function loadManifest(project) {
  const path = join(project, ".herdr-orchestrator/manifest.json");
  if (!existsSync(path)) {
    return null;
  }
  let manifest;
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(readFileSync(path));
    manifest = JSON.parse(text);
  } catch {
    throw new Error("manifest_invalid");
  }
  if (
    manifest === null
    || typeof manifest !== "object"
    || Array.isArray(manifest)
    || manifest.schema_version !== 1
    || manifest.package !== "herdr-orchestrator"
    || typeof manifest.version !== "string"
    || typeof manifest.files !== "object"
    || manifest.files === null
    || Array.isArray(manifest.files)
    || (
      manifest.unmanaged_files !== undefined
      && (
        typeof manifest.unmanaged_files !== "object"
        || manifest.unmanaged_files === null
        || Array.isArray(manifest.unmanaged_files)
      )
    )
    || !Array.isArray(manifest.harnesses)
    || manifest.harnesses.length === 0
    || new Set(manifest.harnesses).size !== manifest.harnesses.length
    || manifest.harnesses.some((harness) => !HARNESSES.includes(harness))
    || (
      manifest.install_skill !== undefined
      && typeof manifest.install_skill !== "boolean"
    )
  ) {
    throw new Error("manifest_invalid");
  }
  const unmanagedFiles = manifest.unmanaged_files ?? {};
  for (const [relativePath, hash] of Object.entries({
    ...manifest.files,
    ...unmanagedFiles,
  })) {
    if (!isManagedPath(relativePath) || !/^[a-f0-9]{64}$/.test(hash)) {
      throw new Error(`manifest_entry_invalid: ${relativePath}`);
    }
  }
  if (Object.keys(unmanagedFiles).some((path) => manifest.files[path] !== undefined)) {
    throw new Error("manifest_invalid");
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
  const parts = relativePath.split("/");
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index];
    current = join(current, part);
    try {
      const status = lstatSync(current);
      if (status.isSymbolicLink()) {
        throw new Error(`managed_path_symlink: ${relativePath}`);
      }
      if (index < parts.length - 1 && !status.isDirectory()) {
        throw new Error(`managed_path_ancestor_not_directory: ${relativePath}`);
      }
      if (index === parts.length - 1 && !status.isFile()) {
        throw new Error(`managed_path_not_regular: ${relativePath}`);
      }
    } catch (error) {
      if (error.code !== "ENOENT") {
        throw error;
      }
    }
  }
}

function previousSkillPreference(manifest) {
  if (typeof manifest?.install_skill === "boolean") {
    return manifest.install_skill;
  }
  return Object.keys(manifest?.files ?? {}).some((relativePath) =>
    relativePath.startsWith(".agents/skills/herdr-orchestrator/")
  );
}

function gitExcludePath(project) {
  const result = spawnSync(
    "git",
    ["-C", project, "rev-parse", "--git-path", "info/exclude"],
    { encoding: "utf8", env: gitEnvironment(), timeout: 5_000 },
  );
  if (result.status !== 0) {
    return null;
  }
  const rendered = result.stdout.trim();
  return rendered.length > 0 ? resolve(project, rendered) : null;
}

function gitEnvironment() {
  const environment = { ...process.env };
  for (const name of [
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
  ]) {
    delete environment[name];
  }
  return environment;
}

function removeManagedExcludeBlock(content) {
  const start = content.indexOf(GIT_EXCLUDE_BEGIN);
  if (start < 0) {
    return content;
  }
  const endMarker = content.indexOf(GIT_EXCLUDE_END, start);
  if (endMarker < 0) {
    throw new Error("git_exclude_marker_invalid");
  }
  let end = endMarker + GIT_EXCLUDE_END.length;
  if (content[end] === "\n") {
    end += 1;
  }
  return `${content.slice(0, start)}${content.slice(end)}`;
}

function assertGitExcludeSafe(project, path) {
  const relativePath = relative(project, path);
  if (
    relativePath !== ".."
    && !relativePath.startsWith(`..${sep}`)
    && !isAbsolute(relativePath)
  ) {
    let current = project;
    for (const component of relativePath.split(sep)) {
      if (component.length === 0) {
        continue;
      }
      current = join(current, component);
      try {
        if (lstatSync(current).isSymbolicLink()) {
          throw new Error(`git_exclude_symlink: ${path}`);
        }
      } catch (error) {
        if (error.code !== "ENOENT") {
          throw error;
        }
      }
    }
  }
  try {
    const parent = lstatSync(dirname(path));
    if (parent.isSymbolicLink()) {
      throw new Error(`git_exclude_symlink: ${path}`);
    }
    if (!parent.isDirectory()) {
      throw new Error(`git_exclude_parent_invalid: ${path}`);
    }
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw error;
    }
  }
  try {
    const exclude = lstatSync(path);
    if (exclude.isSymbolicLink()) {
      throw new Error(`git_exclude_symlink: ${path}`);
    }
    if (!exclude.isFile()) {
      throw new Error(`git_exclude_not_regular: ${path}`);
    }
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw error;
    }
  }
}

function renderGitExcludeBlock(includeSkill) {
  const paths = ["/.herdr-orchestrator/", "/.orchestrator/"];
  if (includeSkill) {
    paths.push("/.agents/skills/herdr-orchestrator/");
  }
  return `${GIT_EXCLUDE_BEGIN}\n${paths.join("\n")}\n${GIT_EXCLUDE_END}\n`;
}

function buildDesiredFiles(harnesses, installSkill) {
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
  for (const filename of ["AGENTS.md", "CLAUDE.md"]) {
    stageFile(
      desiredFiles,
      `.herdr-orchestrator/manager/${filename}`,
      readFileSync(join(PACKAGE_ROOT, `manager/${filename}`)),
    );
  }
  for (const harness of harnesses) {
    for (const extension of ["toml", "md"]) {
      stageFile(
        desiredFiles,
        `.herdr-orchestrator/profiles/harnesses/${harness}.${extension}`,
        readFileSync(join(PACKAGE_ROOT, `profiles/harnesses/${harness}.${extension}`)),
      );
    }
  }
  if (installSkill) {
    const skillRoot = join(PACKAGE_ROOT, "skills/herdr-orchestrator");
    for (const relativePath of listSkillFiles()) {
      stageFile(
        desiredFiles,
        `.agents/skills/herdr-orchestrator/${relativePath}`,
        readFileSync(join(skillRoot, relativePath)),
      );
    }
  }
  stageFile(desiredFiles, ".orchestrator/.gitignore", "*\n!.gitignore\n");
  return desiredFiles;
}

function desiredLocalGitExclude(project, path, includeSkill) {
  if (path === null) {
    return {
      content: null,
      status: "unavailable",
    };
  }
  assertGitExcludeSafe(project, path);
  const current = existsSync(path) ? readFileSync(path, "utf8") : "";
  const withoutManaged = removeManagedExcludeBlock(current);
  const separator = withoutManaged.length > 0 && !withoutManaged.endsWith("\n") ? "\n" : "";
  return {
    content: Buffer.from(
      `${withoutManaged}${separator}${renderGitExcludeBlock(includeSkill)}`,
    ),
    status: "managed",
  };
}

function inspectLocalGitExclude(project, path, includeSkill) {
  if (path === null) {
    return {
      available: false,
      ok: true,
      status: "unavailable",
    };
  }
  assertGitExcludeSafe(project, path);
  if (!existsSync(path)) {
    return {
      available: true,
      ok: false,
      status: "missing",
    };
  }
  try {
    const current = readFileSync(path);
    const desired = desiredLocalGitExclude(project, path, includeSkill).content;
    const ok = current.equals(desired);
    return {
      available: true,
      ok,
      status: ok ? "managed" : "modified",
    };
  } catch (error) {
    if (error.message === "git_exclude_marker_invalid") {
      return {
        available: true,
        ok: false,
        status: "invalid",
      };
    }
    throw error;
  }
}

function managedRootsHaveRemainingEntries(project, roots, ignoredPaths) {
  function containsEntry(relativeDirectory) {
    const directory = join(project, relativeDirectory);
    let entries;
    try {
      const status = lstatSync(directory);
      if (status.isSymbolicLink() || !status.isDirectory()) {
        return true;
      }
      entries = readdirSync(directory, { withFileTypes: true });
    } catch (error) {
      if (error.code === "ENOENT") {
        return false;
      }
      throw error;
    }
    for (const entry of entries) {
      const relativePath = `${relativeDirectory}/${entry.name}`;
      if (entry.isDirectory() && !entry.isSymbolicLink()) {
        if (containsEntry(relativePath)) {
          return true;
        }
      } else if (!ignoredPaths.has(relativePath)) {
        return true;
      }
    }
    return false;
  }

  return roots.some((root) => containsEntry(root));
}

function listManagedRootEntries(project) {
  const paths = [];
  function visit(relativeDirectory) {
    const directory = join(project, relativeDirectory);
    let entries;
    try {
      const status = lstatSync(directory);
      if (status.isSymbolicLink() || !status.isDirectory()) {
        paths.push(relativeDirectory);
        return;
      }
      entries = readdirSync(directory, { withFileTypes: true });
    } catch (error) {
      if (error.code === "ENOENT") {
        return;
      }
      throw error;
    }
    for (const entry of entries) {
      const relativePath = `${relativeDirectory}/${entry.name}`;
      if (entry.isDirectory() && !entry.isSymbolicLink()) {
        visit(relativePath);
      } else {
        paths.push(relativePath);
      }
    }
  }
  for (const root of [
    ".herdr-orchestrator",
    ".orchestrator",
    ".agents/skills/herdr-orchestrator",
  ]) {
    visit(root);
  }
  return paths.sort();
}

function unownedLocalExcludeStatus(path) {
  if (path === null || !existsSync(path)) {
    return "unavailable";
  }
  const content = readFileSync(path, "utf8");
  return (
    content.includes(GIT_EXCLUDE_BEGIN)
    || content.includes(GIT_EXCLUDE_END)
  ) ? "retained" : "removed";
}

function desiredUninstallGitExclude(
  project,
  path,
  managedSkill,
  removedPaths,
) {
  if (path === null || !existsSync(path)) {
    return {
      content: null,
      status: "unavailable",
    };
  }
  assertGitExcludeSafe(project, path);
  const roots = [".herdr-orchestrator", ".orchestrator"];
  if (managedSkill) {
    roots.push(".agents/skills/herdr-orchestrator");
  }
  const ignoredPaths = new Set([
    ...removedPaths,
    ".herdr-orchestrator/manifest.json",
    ".herdr-orchestrator/install-journal.json",
  ]);
  const current = readFileSync(path, "utf8");
  if (managedRootsHaveRemainingEntries(project, roots, ignoredPaths)) {
    return {
      content: Buffer.from(current),
      status: "retained",
    };
  }
  return {
    content: Buffer.from(removeManagedExcludeBlock(current)),
    status: "removed",
  };
}

function projectFileTarget(relativePath) {
  return {
    path: relativePath,
    scope: "project",
  };
}

function gitExcludeTarget(path) {
  return {
    path,
    scope: "git-exclude",
  };
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
  const localExcludePath = gitExcludePath(project);
  if (localExcludePath !== null) {
    assertGitExcludeSafe(project, localExcludePath);
  }
  const journalContext = {
    assertGitExcludeSafe,
    gitExcludePath: localExcludePath,
    project,
  };
  reconcileInstallerJournal(journalContext);
  const previous = loadManifest(project);
  const skillRouterExists = existsSync(join(project, ".agents/skills"));
  const installSkill = options.installSkill ?? (
    previous === null ? !skillRouterExists : previousSkillPreference(previous)
  );
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

  const desiredFiles = buildDesiredFiles(harnesses, installSkill);

  const previousFiles = previous?.files ?? {};
  const conflicts = [];
  const preserved = [];
  const removals = [];
  const unmanaged = [];
  const skillPath = ".agents/skills/herdr-orchestrator/SKILL.md";
  if (
    !installSkill
    && previousFiles[skillPath] === undefined
    && existsSync(join(project, skillPath))
  ) {
    unmanaged.push(skillPath);
  }
  const manifestFiles = {};
  const unmanagedFiles = {};
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
        unmanagedFiles[relativePath] = desiredHash;
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

  const manifest = {
    schema_version: 1,
    package: "herdr-orchestrator",
    version: packageVersion(),
    harnesses,
    install_skill: installSkill,
    files: manifestFiles,
    unmanaged_files: unmanagedFiles,
  };
  const manifestContent = Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`);
  const managesSkill = Object.keys(manifestFiles).some((relativePath) =>
    relativePath.startsWith(".agents/skills/herdr-orchestrator/")
  );
  const localExclude = desiredLocalGitExclude(
    project,
    localExcludePath,
    managesSkill,
  );
  const preservedSet = new Set(preserved);
  const unmanagedSet = new Set(unmanaged);
  const participants = [];
  const priorOnlyPaths = Object.keys(previousFiles)
    .filter((path) => !desiredFiles.has(path))
    .sort();
  const priorOnlySet = new Set(priorOnlyPaths);
  const managedPaths = [
    ...priorOnlyPaths,
    ...[...desiredFiles.keys()].filter((path) => !priorOnlySet.has(path)).sort(),
  ];
  for (const relativePath of managedPaths) {
    const target = projectFileTarget(relativePath);
    const original = observeInstallerTarget(project, target, journalContext);
    let desired = installerFileState(null);
    let desiredContent = null;
    if (desiredFiles.has(relativePath)) {
      const content = desiredFiles.get(relativePath);
      if (preservedSet.has(relativePath) || unmanagedSet.has(relativePath)) {
        desired = original;
      } else {
        desired = installerFileState(content);
        desiredContent = content;
      }
    } else if (preservedSet.has(relativePath)) {
      desired = original;
    }
    participants.push({
      desired,
      desiredContent,
      original,
      target,
    });
  }
  if (localExcludePath !== null) {
    const target = gitExcludeTarget(localExcludePath);
    participants.push({
      desired: installerFileState(localExclude.content),
      desiredContent: localExclude.content,
      original: observeInstallerTarget(project, target, journalContext),
      target,
    });
  }
  const manifestTarget = projectFileTarget(
    ".herdr-orchestrator/manifest.json",
  );
  participants.push({
    desired: installerFileState(manifestContent),
    desiredContent: manifestContent,
    original: observeInstallerTarget(project, manifestTarget, journalContext),
    target: manifestTarget,
  });
  runInstallerTransaction({
    ...journalContext,
    command: options.command === "install" ? "install" : "upgrade",
    harnesses,
    installSkill,
    packageVersion: packageVersion(),
    participants,
  });
  let skill = "skipped";
  if (installSkill) {
    skill = unmanaged.includes(skillPath) ? "existing_unmanaged" : "managed";
  } else if (options.installSkill === null && previous === null && skillRouterExists) {
    skill = "skipped_existing_router";
  }
  process.stdout.write(`${JSON.stringify({
    harnesses,
    local_exclude: localExclude.status,
    manager: ".herdr-orchestrator/manager",
    manifest: ".herdr-orchestrator/manifest.json",
    ok: preserved.length === 0,
    preserved: preserved.sort(),
    project,
    skill,
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

function doctorProcessTimeoutMs(rest) {
  let probeTimeoutSeconds = DOCTOR_PROBE_TIMEOUT_SECONDS;
  for (let index = 0; index < rest.length; index += 1) {
    const value = rest[index];
    if (
      value.length > 2
      && DOCTOR_PROBE_TIMEOUT_OPTION.startsWith(value)
      && index + 1 < rest.length
    ) {
      probeTimeoutSeconds = Number(rest[index + 1]);
      index += 1;
      continue;
    }
    const [option, inlineValue] = value.split("=", 2);
    if (
      option.length > 2
      && DOCTOR_PROBE_TIMEOUT_OPTION.startsWith(option)
      && inlineValue !== undefined
    ) {
      probeTimeoutSeconds = Number(inlineValue);
    }
  }
  if (!Number.isInteger(probeTimeoutSeconds) || probeTimeoutSeconds <= 0) {
    probeTimeoutSeconds = DOCTOR_PROBE_TIMEOUT_SECONDS;
  }
  probeTimeoutSeconds = Math.min(probeTimeoutSeconds, DOCTOR_PROBE_TIMEOUT_MAX_SECONDS);
  const maximum = (
    HARNESSES.length * probeTimeoutSeconds * 1_000
    + DOCTOR_PROCESS_OVERHEAD_MS
  );
  const requested = Number(process.env[DOCTOR_PROCESS_TIMEOUT_ENV]);
  if (Number.isFinite(requested) && requested > 0) {
    return Math.min(Math.floor(requested), maximum);
  }
  return maximum;
}

function rejectWorkflowOverride(rest) {
  for (const value of rest) {
    const option = value.split("=", 1)[0];
    if (WORKFLOW_OPTION_PREFIXES.includes(option)) {
      throw new Error("workflow_option_reserved");
    }
  }
}

function reportManagerLight(classification) {
  const paneId = process.env.HERDR_PANE_ID;
  if (!paneId) {
    return;
  }
  const args = [
    "pane",
    "report-metadata",
    paneId,
    "--source",
    METADATA_SOURCE,
  ];
  for (const [name, value] of Object.entries(tokenPatchFor(classification))) {
    if (value === null) {
      args.push("--clear-token", name);
    } else {
      args.push("--token", `${name}=${value}`);
    }
  }
  try {
    spawnSync(process.env.HERDR_BIN_PATH || "herdr", args, {
      env: process.env,
      stdio: "ignore",
      timeout: 2_000,
    });
  } catch {
    // Manager metadata is optional and must never prevent the harness launch.
  }
}

function manager(options) {
  if (process.env.HERDR_ENV !== "1") {
    throw new Error("manager_requires_herdr: HERDR_ENV=1");
  }
  if (options.harnesses.length > 1 || options.rest.length > 1) {
    throw new Error("manager_harness_ambiguous: pass one harness");
  }
  if (options.harnesses.length === 1 && options.rest.length === 1) {
    throw new Error("manager_harness_conflict: use a positional harness or --harness, not both");
  }

  const requestedHarness = options.harnesses[0] ?? options.rest[0];
  if (requestedHarness !== undefined && !HARNESSES.includes(requestedHarness)) {
    throw new Error(`unsupported_harness: ${requestedHarness}`);
  }
  let directory = join(PACKAGE_ROOT, "manager");
  let enabledHarnesses = HARNESSES;
  if (options.projectExplicit) {
    const project = resolve(options.project);
    const manifest = loadManifest(project);
    if (project === PACKAGE_ROOT) {
      directory = join(PACKAGE_ROOT, "manager");
    } else if (manifest === null) {
      throw new Error(`installation_not_found: run herdr-orchestrator install --project ${project}`);
    } else {
      enabledHarnesses = manifest.harnesses;
      directory = join(project, ".herdr-orchestrator/manager");
    }
  }

  const harness = requestedHarness ?? MANAGER_HARNESSES.find(
    (candidate) => enabledHarnesses.includes(candidate) && commandExists(candidate),
  );
  if (harness === undefined) {
    throw new Error("manager_default_harness_not_found: install grok, codex, or claude");
  }
  if (!enabledHarnesses.includes(harness)) {
    throw new Error(`manager_harness_not_enabled: ${harness}`);
  }
  if (!existsSync(join(directory, "AGENTS.md"))) {
    throw new Error(`manager_workspace_not_found: ${directory}`);
  }
  let result;
  reportManagerLight("manager");
  try {
    result = spawnSync(harness, [], {
      cwd: directory,
      env: process.env,
      stdio: "inherit",
    });
  } finally {
    reportManagerLight("absent");
  }
  if (result.error?.code === "ENOENT") {
    throw new Error(`manager_harness_not_found: ${harness}`);
  }
  if (result.error) {
    throw result.error;
  }
  process.exitCode = result.status ?? 1;
}

function managerLight(options) {
  if (options.projectExplicit || options.harnesses.length > 0 || options.rest.length !== 1) {
    throw new Error("manager_light_usage: expected install, status, or uninstall");
  }
  const action = options.rest[0];
  const pluginRoot = join(PACKAGE_ROOT, "plugins/manager-light");
  let payload;
  if (action === "install") {
    payload = installManagerLight({
      pluginRoot,
    });
  } else if (action === "status") {
    payload = managerLightStatus({ pluginRoot });
  } else if (action === "uninstall") {
    payload = uninstallManagerLight({ pluginRoot });
  } else {
    throw new Error(`manager_light_action_unsupported: ${action}`);
  }
  process.stdout.write(`${JSON.stringify(payload)}\n`);
  if (!payload.ok) {
    process.exitCode = 1;
  }
}

function inspectInstallation(project) {
  const manifestPath = join(project, ".herdr-orchestrator/manifest.json");
  assertNoSymlink(project, ".herdr-orchestrator/manifest.json");
  const localExcludePath = gitExcludePath(project);
  if (localExcludePath !== null) {
    assertGitExcludeSafe(project, localExcludePath);
  }
  const journal = inspectInstallerJournal({
    assertGitExcludeSafe,
    gitExcludePath: localExcludePath,
    project,
  });
  if (!existsSync(manifestPath)) {
    return {
      git_exclude: {
        available: localExcludePath !== null,
        ok: false,
        status: "not_checked",
      },
      journal,
      manifest: false,
      manifest_extra: [],
      manifest_mismatched: [],
      manifest_missing: [],
      missing: [".herdr-orchestrator/manifest.json"],
      modified: [],
      ok: false,
      package_missing: [],
      package_modified: [],
    };
  }
  const manifest = loadManifest(project);
  const missing = [];
  const modified = [];
  for (const [relativePath, expectedHash] of Object.entries(manifest.files)) {
    assertNoSymlink(project, relativePath);
    const target = join(project, relativePath);
    if (!existsSync(target)) {
      missing.push(relativePath);
    } else if (sha256(readFileSync(target)) !== expectedHash) {
      modified.push(relativePath);
    }
  }
  const desiredFiles = buildDesiredFiles(
    manifest.harnesses,
    previousSkillPreference(manifest),
  );
  const unmanagedFiles = manifest.unmanaged_files ?? {};
  const declaredFiles = {
    ...manifest.files,
    ...unmanagedFiles,
  };
  const packageMissing = [];
  const packageModified = [];
  const manifestMissing = [];
  const manifestMismatched = [];
  for (const [relativePath, content] of desiredFiles) {
    const expectedHash = sha256(content);
    const target = join(project, relativePath);
    assertNoSymlink(project, relativePath);
    if (!existsSync(target)) {
      packageMissing.push(relativePath);
    } else if (sha256(readFileSync(target)) !== expectedHash) {
      packageModified.push(relativePath);
    }
    if (declaredFiles[relativePath] === undefined) {
      manifestMissing.push(relativePath);
    } else if (declaredFiles[relativePath] !== expectedHash) {
      manifestMismatched.push(relativePath);
    }
  }
  const manifestExtra = Object.keys(declaredFiles).filter(
    (relativePath) => !desiredFiles.has(relativePath),
  );
  const managesSkill = Object.keys(manifest.files).some((relativePath) =>
    relativePath.startsWith(".agents/skills/herdr-orchestrator/")
  );
  const gitExclude = inspectLocalGitExclude(
    project,
    localExcludePath,
    managesSkill,
  );
  const runtimeVersion = packageVersion();
  const versionSkew = manifest.version !== runtimeVersion;
  return {
    git_exclude: gitExclude,
    journal,
    manifest: true,
    manifest_extra: manifestExtra.sort(),
    manifest_mismatched: manifestMismatched.sort(),
    manifest_missing: manifestMissing.sort(),
    missing: missing.sort(),
    modified: modified.sort(),
    ok: (
      missing.length === 0
      && modified.length === 0
      && packageMissing.length === 0
      && packageModified.length === 0
      && manifestMissing.length === 0
      && manifestExtra.length === 0
      && manifestMismatched.length === 0
      && gitExclude.ok
      && !versionSkew
      && !journal.active
    ),
    package_missing: packageMissing.sort(),
    package_modified: packageModified.sort(),
    unmanaged: Object.keys(unmanagedFiles).sort(),
    installed_version: manifest.version,
    runtime_version: runtimeVersion,
    version: manifest.version,
    version_skew: versionSkew,
  };
}

function doctor(options) {
  rejectWorkflowOverride(options.rest);
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
      [
        "-m",
        "herdr_orchestrator",
        "doctor",
        "--workflow",
        workflow,
        ...options.rest,
      ],
      {
        cwd: project,
        encoding: "utf8",
        timeout: doctorProcessTimeoutMs(options.rest),
        env: {
          ...process.env,
          PYTHONPATH: join(PACKAGE_ROOT, "src"),
        },
      },
    );
    if (result.error) {
      runtime = {
        error: result.error.code === "ETIMEDOUT"
          ? "runtime_doctor_timeout"
          : result.error.message,
        ok: false,
      };
    } else {
      try {
        const parsed = JSON.parse(result.stdout);
        if (
          parsed === null
          || typeof parsed !== "object"
          || Array.isArray(parsed)
          || typeof parsed.ok !== "boolean"
        ) {
          throw new Error("runtime_doctor_invalid_output");
        }
        runtime = parsed;
        if (result.status !== 0) {
          const status = result.status === null ? "signal" : result.status;
          runtime = {
            ...runtime,
            error: runtime.error ?? `runtime_doctor_exit: ${status}`,
            ok: false,
          };
        }
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

function pruneManagedDirectories(project) {
  for (const relativePath of [
    ".herdr-orchestrator/profiles/harnesses",
    ".herdr-orchestrator/profiles",
    ".herdr-orchestrator/manager",
    ".herdr-orchestrator/workflows/prompts",
    ".herdr-orchestrator/workflows",
    ".herdr-orchestrator",
    ".agents/skills/herdr-orchestrator",
    ".orchestrator",
  ]) {
    try {
      rmdirSync(join(project, relativePath));
    } catch (error) {
      if (!["ENOENT", "ENOTDIR", "ENOTEMPTY"].includes(error.code)) {
        throw error;
      }
    }
  }
}

function writeUninstallResult(project, result) {
  process.stdout.write(`${JSON.stringify({
    ...result,
    project,
  })}\n`);
  if (!result.ok) {
    process.exitCode = 1;
  }
}

function uninstall(options) {
  const project = resolve(options.project);
  const manifestPath = join(project, ".herdr-orchestrator/manifest.json");
  assertNoSymlink(project, ".herdr-orchestrator/manifest.json");
  const localExcludePath = gitExcludePath(project);
  if (localExcludePath !== null) {
    assertGitExcludeSafe(project, localExcludePath);
  }
  const recovery = reconcileInstallerJournal({
    assertGitExcludeSafe,
    gitExcludePath: localExcludePath,
    project,
  });
  if (!existsSync(manifestPath)) {
    if (recovery.active && recovery.command === "uninstall") {
      pruneManagedDirectories(project);
      writeUninstallResult(project, recovery.command_result);
      return;
    }
    pruneManagedDirectories(project);
    const preserved = listManagedRootEntries(project);
    writeUninstallResult(project, {
      local_exclude: unownedLocalExcludeStatus(localExcludePath),
      ok: preserved.length === 0,
      preserved,
    });
    return;
  }
  const manifest = loadManifest(project);
  const managedSkill = Object.keys(manifest.files).some((relativePath) =>
    relativePath.startsWith(".agents/skills/herdr-orchestrator/")
  );
  const preserved = [];
  const removals = [];
  const originalStates = new Map();
  const journalContext = {
    assertGitExcludeSafe,
    gitExcludePath: localExcludePath,
    project,
  };
  for (const [relativePath, expectedHash] of Object.entries(manifest.files).sort()) {
    assertNoSymlink(project, relativePath);
    const target = projectFileTarget(relativePath);
    const original = observeInstallerTarget(project, target, journalContext);
    originalStates.set(relativePath, original);
    if (original.kind === "absent") {
      continue;
    }
    if (original.digest === expectedHash) {
      removals.push(relativePath);
    } else {
      preserved.push(relativePath);
    }
  }
  const localExclude = desiredUninstallGitExclude(
    project,
    localExcludePath,
    managedSkill,
    removals,
  );
  const removalSet = new Set(removals);
  const participants = [];
  for (const relativePath of Object.keys(manifest.files).sort()) {
    const original = originalStates.get(relativePath);
    participants.push({
      desired: removalSet.has(relativePath) ? installerFileState(null) : original,
      desiredContent: null,
      original,
      target: projectFileTarget(relativePath),
    });
  }
  if (localExcludePath !== null) {
    const target = gitExcludeTarget(localExcludePath);
    participants.push({
      desired: installerFileState(localExclude.content),
      desiredContent: localExclude.content,
      original: observeInstallerTarget(project, target, journalContext),
      target,
    });
  }
  const manifestTarget = projectFileTarget(
    ".herdr-orchestrator/manifest.json",
  );
  participants.push({
    desired: installerFileState(null),
    desiredContent: null,
    original: observeInstallerTarget(project, manifestTarget, journalContext),
    target: manifestTarget,
  });
  runInstallerTransaction({
    ...journalContext,
    command: "uninstall",
    commandResult: {
      local_exclude: localExclude.status,
      ok: preserved.length === 0,
      preserved: preserved.sort(),
    },
    harnesses: manifest.harnesses,
    installSkill: previousSkillPreference(manifest),
    packageVersion: packageVersion(),
    participants,
  });
  pruneManagedDirectories(project);
  writeUninstallResult(project, {
    local_exclude: localExclude.status,
    ok: preserved.length === 0,
    preserved: preserved.sort(),
  });
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
  rejectWorkflowOverride(options.rest);
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

function printHelp(command = null) {
  if (["install", "update", "upgrade"].includes(command)) {
    process.stdout.write(`Usage: herdr-orchestrator ${command} --project <path> [--harness <name> ...] [--install-skill | --skip-skill]

Install or reconcile the managed workflow, selected harness profiles, runtime ignore file, and optional operating Skill. Existing project Skill routers are not modified unless --install-skill is explicit.
`);
    return;
  }
  if (command === "uninstall") {
    process.stdout.write(`Usage: herdr-orchestrator uninstall --project <path>

Remove only unchanged files owned by the installation manifest.
`);
    return;
  }
  if (command === "manager") {
    process.stdout.write(`Usage:
  herdr-manager [harness]
  herdr-orchestrator manager [harness] [--project <path>]

Start one harness in the dedicated manual Herdr manager workspace. Without an explicit harness, try grok, codex, then claude. This command must run inside a Herdr session.
`);
    return;
  }
  if (command === "manager-light") {
    process.stdout.write(`Usage: herdr-orchestrator manager-light <install|status|uninstall>

Install, inspect, or remove the package-owned Herdr manager-light plugin and sidebar row projection.
`);
    return;
  }
  if (command && command !== "help") {
    process.stdout.write(`Usage: herdr-orchestrator ${command} --project <path> [command options]

Run the ${command} command against the installed project workflow.
`);
    return;
  }
  process.stdout.write(`Usage: herdr-orchestrator <command> [options]

Setup:
  install --project <path> [--harness <name> ...] [--install-skill]
  upgrade --project <path> [--harness <name> ...] [--install-skill | --skip-skill]
  doctor --project <path>
  uninstall --project <path>

Runtime:
  doctor | catalog | profile | seed | status | enqueue | run | retry | resume | gc | dashboard | smoke

Interactive:
  manager [harness] [--project <path>]
  manager-light <install|status|uninstall>

Options:
  --project <path>   Target repository (default: current directory)
  --harness <name>   Harness to enable during install, or select for manager
  --install-skill    Install the project Skill even when a Skill router exists
  --skip-skill       Do not install, or remove an unchanged managed project Skill
  --version          Print the package version
`);
}

function main() {
  try {
    const arguments_ = process.argv.slice(2);
    const argv = basename(process.argv[1] ?? "") === "herdr-manager"
      ? ["manager", ...arguments_]
      : arguments_;
    if (argv.includes("--help") || argv.includes("-h")) {
      const command = argv[0]?.startsWith("-") ? null : argv[0];
      printHelp(command);
      return;
    }
    const options = parseArguments(argv);
    if (["install", "update", "upgrade"].includes(options.command)) {
      install(options);
    } else if (options.command === "manager") {
      manager(options);
    } else if (options.command === "manager-light") {
      managerLight(options);
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
