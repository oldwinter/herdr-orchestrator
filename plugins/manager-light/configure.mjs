import { spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";

import { reconcileStartup } from "./hook.mjs";

export const PLUGIN_ID = "herdr-manager-light";
export const CONFIG_BEGIN = "# BEGIN herdr-manager-light managed ui.sidebar.agents";
export const CONFIG_END = "# END herdr-manager-light managed ui.sidebar.agents";

export const MANAGED_CONFIG_BLOCK = `${CONFIG_BEGIN}
[ui.sidebar.agents]
rows = [
  [
    { token = "$hml_manager", fg = "#66B8FF" },
    { token = "$hml_blocked", fg = "#F7768E" },
    { token = "$hml_working", fg = "#E0AF68" },
    { token = "$hml_idle", fg = "#9ECE6A" },
    { token = "$hml_unknown", fg = "#7DCFFF" },
    "workspace",
    "tab",
  ],
  ["agent"],
]
${CONFIG_END}`;

function markerPositions(source, marker) {
  const positions = [];
  let offset = 0;
  while (offset <= source.length) {
    const position = source.indexOf(marker, offset);
    if (position === -1) {
      break;
    }
    positions.push(position);
    offset = position + marker.length;
  }
  return positions;
}

function ownsAgentRows(source) {
  let section = "";
  for (const line of source.split(/\r?\n/u)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) {
      continue;
    }
    const sectionMatch = trimmed.match(/^\[([^\]]+)\](?:\s*#.*)?$/u);
    if (sectionMatch) {
      section = sectionMatch[1].trim();
      if (
        section === "ui.sidebar.agents" ||
        section.startsWith("ui.sidebar.agents.")
      ) {
        return true;
      }
      continue;
    }
    if (/^ui\.sidebar\.agents(?:\.|\s*=)/u.test(trimmed)) {
      return true;
    }
    if (section === "ui.sidebar" && /^agents(?:\.|\s*=)/u.test(trimmed)) {
      return true;
    }
  }
  return false;
}

export function inspectConfigText(source) {
  const begins = markerPositions(source, CONFIG_BEGIN);
  const ends = markerPositions(source, CONFIG_END);
  if (begins.length !== ends.length || begins.length > 1) {
    return {
      conflict: false,
      error: "manager_light_config_markers_malformed",
      owned: false,
      state: "malformed",
    };
  }
  if (begins.length === 0) {
    const conflict = ownsAgentRows(source);
    return {
      conflict,
      error: conflict ? "manager_light_agent_rows_owned_externally" : null,
      owned: false,
      state: conflict ? "conflict" : "absent",
    };
  }

  const start = begins[0];
  const end = ends[0] + CONFIG_END.length;
  if (ends[0] < start || source.slice(start, end) !== MANAGED_CONFIG_BLOCK) {
    return {
      conflict: false,
      error: "manager_light_config_block_modified",
      owned: false,
      state: "malformed",
    };
  }
  const outside = source.slice(0, start) + source.slice(end);
  const conflict = ownsAgentRows(outside);
  return {
    block_end: end,
    block_start: start,
    conflict,
    error: conflict ? "manager_light_agent_rows_owned_externally" : null,
    owned: true,
    state: conflict ? "conflict" : "owned",
  };
}

function assertConfigIsMutable(inspection) {
  if (inspection.error) {
    throw new Error(inspection.error);
  }
}

export function installConfigText(source) {
  const inspection = inspectConfigText(source);
  assertConfigIsMutable(inspection);
  if (inspection.owned) {
    return source;
  }
  return source.length === 0
    ? `${MANAGED_CONFIG_BLOCK}\n`
    : `${source}\n${MANAGED_CONFIG_BLOCK}\n`;
}

export function uninstallConfigText(source) {
  const inspection = inspectConfigText(source);
  assertConfigIsMutable(inspection);
  if (!inspection.owned) {
    return source;
  }

  const prefix = source.slice(0, inspection.block_start);
  const suffix = source.slice(inspection.block_end);
  if (suffix === "\n") {
    return prefix.endsWith("\n") ? prefix.slice(0, -1) : prefix;
  }
  return prefix + suffix;
}

export function resolveConfigPath(env = process.env) {
  if (env.HERDR_CONFIG_PATH) {
    return resolve(env.HERDR_CONFIG_PATH);
  }
  if (process.platform === "win32" && env.APPDATA) {
    return join(env.APPDATA, "herdr", "config.toml");
  }
  const configRoot = env.XDG_CONFIG_HOME || (env.HOME && join(env.HOME, ".config"));
  if (!configRoot) {
    throw new Error("manager_light_config_home_unavailable");
  }
  return join(configRoot, "herdr", "config.toml");
}

function assertRegularConfigPath(configPath) {
  let metadata;
  try {
    metadata = lstatSync(configPath);
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw error;
    }
  }
  if (metadata?.isSymbolicLink()) {
    throw new Error("manager_light_config_path_is_symlink");
  }
}

function removeTemporaryPath(path) {
  try {
    lstatSync(path);
    unlinkSync(path);
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw error;
    }
  }
}

function herdrCommand(args, env = process.env) {
  return spawnSync(env.HERDR_BIN_PATH || "herdr", args, {
    encoding: "utf8",
    env,
    timeout: 10_000,
  });
}

function commandSucceeded(result) {
  return !result.error && result.status === 0;
}

function requireHerdr(args, env, errorCode) {
  const result = herdrCommand(args, env);
  if (!commandSucceeded(result)) {
    throw new Error(errorCode);
  }
  return result;
}

function assertSupportedVersion(env) {
  const result = requireHerdr(["--version"], env, "manager_light_herdr_unavailable");
  const match = `${result.stdout}\n${result.stderr}`.match(/\b(\d+)\.(\d+)\.(\d+)\b/u);
  if (!match) {
    throw new Error("manager_light_herdr_version_unknown");
  }
  const version = match.slice(1).map(Number);
  const supported =
    version[0] > 0 ||
    (version[0] === 0 &&
      (version[1] > 8 || (version[1] === 8 && version[2] >= 2)));
  if (!supported) {
    throw new Error("manager_light_requires_herdr_0_8_2");
  }
}

function prepareCandidate(configPath, content, env) {
  assertRegularConfigPath(configPath);
  mkdirSync(dirname(configPath), { recursive: true });
  const candidate = `${configPath}.herdr-manager-light.candidate`;
  removeTemporaryPath(candidate);
  const mode = existsSync(configPath) ? statSync(configPath).mode & 0o777 : 0o600;
  writeFileSync(candidate, content, { encoding: "utf8", flag: "wx", mode });
  chmodSync(candidate, mode);
  const result = herdrCommand(["config", "check"], {
    ...env,
    HERDR_CONFIG_PATH: candidate,
  });
  if (!commandSucceeded(result)) {
    unlinkSync(candidate);
    throw new Error("manager_light_config_candidate_invalid");
  }
  return candidate;
}

function commitCandidate(candidate, configPath) {
  renameSync(candidate, configPath);
}

function atomicWrite(configPath, content) {
  const candidate = `${configPath}.herdr-manager-light.rollback`;
  const mode = existsSync(configPath) ? statSync(configPath).mode & 0o777 : 0o600;
  removeTemporaryPath(candidate);
  writeFileSync(candidate, content, { encoding: "utf8", flag: "wx", mode });
  chmodSync(candidate, mode);
  renameSync(candidate, configPath);
}

function readConfig(configPath) {
  assertRegularConfigPath(configPath);
  return existsSync(configPath) ? readFileSync(configPath, "utf8") : "";
}

function parsePluginList(result) {
  if (!commandSucceeded(result)) {
    return { enabled: false, installed: false, reachable: false };
  }
  try {
    const payload = JSON.parse(result.stdout);
    const plugins = payload?.result?.plugins ?? payload?.plugins ?? [];
    const plugin = Array.isArray(plugins)
      ? plugins.find(
          (item) => (item?.plugin_id ?? item?.id) === PLUGIN_ID,
        )
      : null;
    return {
      enabled: plugin?.enabled === true,
      installed: Boolean(plugin),
      path: plugin?.plugin_root ?? plugin?.path ?? plugin?.source_path ?? null,
      reachable: true,
    };
  } catch {
    return { enabled: false, installed: false, reachable: false };
  }
}

function readPluginStatus(env) {
  return parsePluginList(herdrCommand(["plugin", "list", "--json"], env));
}

function withPluginOwnership(plugin, pluginRoot) {
  const canonicalPath = (path) => {
    try {
      return realpathSync(path);
    } catch {
      return resolve(path);
    }
  };
  return {
    ...plugin,
    owned:
      plugin.installed &&
      typeof plugin.path === "string" &&
      typeof pluginRoot === "string" &&
      canonicalPath(plugin.path) === canonicalPath(pluginRoot),
  };
}

function reloadAndRefresh(env) {
  const reloaded = commandSucceeded(herdrCommand(["server", "reload-config"], env));
  const refreshed = reconcileStartup({ ...env, HERDR_PLUGIN_EVENT: "startup" });
  return { refreshed, reloaded };
}

export function managerLightStatus({ env = process.env, pluginRoot } = {}) {
  const configPath = resolveConfigPath(env);
  const config = inspectConfigText(readConfig(configPath));
  const plugin = withPluginOwnership(readPluginStatus(env), pluginRoot);
  return {
    action: "status",
    config: { ...config, path: configPath },
    ok: config.state === "owned" && plugin.enabled && plugin.owned,
    plugin,
  };
}

export function installManagerLight({ env = process.env, pluginRoot } = {}) {
  assertSupportedVersion(env);
  const before = withPluginOwnership(readPluginStatus(env), pluginRoot);
  if (!before.reachable) {
    throw new Error("manager_light_plugin_status_failed");
  }
  if (before.installed && !before.owned) {
    throw new Error("manager_light_plugin_owned_externally");
  }
  const configPath = resolveConfigPath(env);
  const existed = existsSync(configPath);
  const original = readConfig(configPath);
  const desired = installConfigText(original);
  const changed = desired !== original;
  if (changed) {
    commitCandidate(prepareCandidate(configPath, desired, env), configPath);
  }

  try {
    if (before.installed) {
      requireHerdr(
        ["plugin", "enable", PLUGIN_ID],
        env,
        "manager_light_plugin_enable_failed",
      );
    } else {
      requireHerdr(
        ["plugin", "link", pluginRoot, "--enabled"],
        env,
        "manager_light_plugin_link_failed",
      );
    }
  } catch (error) {
    if (changed) {
      if (existed) {
        atomicWrite(configPath, original);
      } else if (existsSync(configPath)) {
        unlinkSync(configPath);
      }
    }
    throw error;
  }

  return {
    action: "install",
    config: { changed, owned: true, path: configPath },
    ok: true,
    plugin: { enabled: true, id: PLUGIN_ID, path: pluginRoot },
    runtime: reloadAndRefresh(env),
  };
}

export function uninstallManagerLight({ env = process.env, pluginRoot } = {}) {
  assertSupportedVersion(env);
  const plugin = withPluginOwnership(readPluginStatus(env), pluginRoot);
  if (!plugin.reachable) {
    throw new Error("manager_light_plugin_status_failed");
  }
  if (plugin.installed && !plugin.owned) {
    throw new Error("manager_light_plugin_owned_externally");
  }
  const configPath = resolveConfigPath(env);
  const original = readConfig(configPath);
  const desired = uninstallConfigText(original);
  const changed = desired !== original;
  const candidate = changed ? prepareCandidate(configPath, desired, env) : null;
  try {
    if (plugin.installed) {
      requireHerdr(
        ["plugin", "unlink", PLUGIN_ID],
        env,
        "manager_light_plugin_unlink_failed",
      );
    }
  } catch (error) {
    if (candidate && existsSync(candidate)) {
      unlinkSync(candidate);
    }
    throw error;
  }
  if (candidate) {
    commitCandidate(candidate, configPath);
  }

  return {
    action: "uninstall",
    config: { changed, owned: false, path: configPath },
    ok: true,
    plugin: { enabled: false, id: PLUGIN_ID, installed: false },
    runtime: {
      reloaded: commandSucceeded(herdrCommand(["server", "reload-config"], env)),
    },
  };
}
