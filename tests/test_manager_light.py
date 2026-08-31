import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECTION_URI = (ROOT / "plugins" / "manager-light" / "projection.mjs").as_uri()
CONFIGURE_URI = (ROOT / "plugins" / "manager-light" / "configure.mjs").as_uri()
HOOK = ROOT / "plugins" / "manager-light" / "hook.mjs"
CLI = ROOT / "bin" / "herdr-orchestrator.mjs"

FAKE_HERDR = """#!/usr/bin/env python3
import json
import os
import sys

arguments = sys.argv[1:]
with open(os.environ["HML_FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(arguments) + "\\n")
state = json.loads(os.environ.get("HML_FAKE_STATE", "{}"))
plugin_state_path = os.environ.get("HML_PLUGIN_STATE")
if arguments == ["--version"]:
    print(os.environ.get("HML_HERDR_VERSION", "herdr 0.8.2"))
    raise SystemExit(0)
if arguments[:2] == ["config", "check"]:
    raise SystemExit(1 if os.environ.get("HML_CONFIG_CHECK_FAIL") == "1" else 0)
if arguments[:3] == ["plugin", "list", "--json"]:
    plugins = []
    if plugin_state_path and os.path.exists(plugin_state_path):
        with open(plugin_state_path, encoding="utf-8") as plugin_file:
            plugins = [json.load(plugin_file)]
    print(json.dumps({"result": {"plugins": plugins}}))
    raise SystemExit(0)
if (
    len(arguments) == 4
    and arguments[:2] == ["plugin", "link"]
    and arguments[3] == "--enabled"
):
    if os.environ.get("HML_PLUGIN_LINK_FAIL") == "1":
        raise SystemExit(1)
    if plugin_state_path:
        with open(plugin_state_path, "w", encoding="utf-8") as plugin_file:
            json.dump(
                {
                    "plugin_id": "herdr-manager-light",
                    "enabled": True,
                    "plugin_root": arguments[2],
                },
                plugin_file,
            )
    raise SystemExit(0)
if arguments[:2] == ["plugin", "enable"]:
    if plugin_state_path and os.path.exists(plugin_state_path):
        with open(plugin_state_path, encoding="utf-8") as plugin_file:
            plugin = json.load(plugin_file)
        plugin["enabled"] = True
        with open(plugin_state_path, "w", encoding="utf-8") as plugin_file:
            json.dump(plugin, plugin_file)
    raise SystemExit(0)
if arguments[:2] == ["plugin", "unlink"]:
    if plugin_state_path and os.path.exists(plugin_state_path):
        os.unlink(plugin_state_path)
    raise SystemExit(0)
if arguments[:2] == ["server", "reload-config"]:
    raise SystemExit(0)
if arguments[:1] == ["pane"] and "--json" in arguments:
    raise SystemExit(2)
if arguments[:2] == ["pane", "list"]:
    payload = {"result": {"panes": state.get("panes", [])}}
elif arguments[:2] == ["pane", "get"]:
    pane_id = arguments[2]
    pane = next(
        (item for item in state.get("panes", []) if item.get("pane_id") == pane_id),
        None,
    )
    payload = {"result": {"pane": pane}}
elif arguments[:2] == ["pane", "process-info"]:
    pane_id = arguments[arguments.index("--pane") + 1]
    payload = {
        "result": {
            "process_info": state.get("process_info", {}).get(pane_id, {})
        }
    }
else:
    payload = {"result": {}}
print(json.dumps(payload))
"""


def run_projection(expression, payload):
    script = f"""
import * as projection from {json.dumps(PROJECTION_URI)};
const input = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify({expression}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script, json.dumps(payload)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def run_configure(expression, payload):
    script = f"""
import * as configure from {json.dumps(CONFIGURE_URI)};
const input = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify({expression}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script, json.dumps(payload)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class ManagerLightProjectionTests(unittest.TestCase):
    def test_classification_is_total_for_agent_lifecycle(self):
        panes = [
            {"agent": None, "agent_status": "working"},
            {"agent": "codex", "agent_status": "blocked"},
            {"agent": "codex", "agent_status": "working"},
            {"agent": "codex", "agent_status": "idle"},
            {"agent": "codex", "agent_status": "done"},
            {"agent": "codex", "agent_status": "surprised"},
            {"agent": "codex"},
        ]
        self.assertEqual(
            run_projection("input.map((pane) => projection.classifyPane(pane))", panes),
            ["absent", "blocked", "working", "idle", "idle", "unknown", "unknown"],
        )

    def test_manager_requires_exact_process_argv_and_recovers_without_marker(self):
        cases = [
            {"foreground_processes": [{"argv": ["herdr-manager"]}]},
            {"foreground_processes": [{"argv": ["/usr/bin/node", "/opt/npm/bin/herdr-manager"]}]},
            {"foreground_processes": [{"argv": ["herdr-orchestrator", "manager"]}]},
            {
                "foreground_processes": [
                    {
                        "name": "node",
                        "argv": [
                            "/usr/bin/node",
                            "/opt/herdr/bin/herdr-orchestrator.mjs",
                            "manager",
                        ],
                    }
                ]
            },
        ]
        pane = {"tokens": {"hml_role": "manager"}}
        self.assertEqual(
            run_projection(
                (
                    "input.cases.map((processInfo) => "
                    "projection.classifyPane(input.pane, processInfo))"
                ),
                {"pane": pane, "cases": cases},
            ),
            ["manager", "manager", "manager", "manager"],
        )

        rejected = [
            {"foreground_processes": [{"cmdline": "herdr-orchestrator manager"}]},
            {"foreground_processes": [{"argv": ["herdr-orchestrator", "status"]}]},
            {"foreground_processes": [{"argv": ["my-herdr-manager"]}]},
            {"foreground_processes": [{"argv": ["node", "unrelated.mjs", "herdr-manager"]}]},
            {
                "foreground_processes": [
                    {
                        "argv": [
                            "node",
                            "unrelated.mjs",
                            "herdr-orchestrator",
                            "manager",
                        ]
                    }
                ]
            },
            {"foreground_processes": [{"argv": ["manager", "herdr-orchestrator"]}]},
        ]
        self.assertEqual(
            run_projection(
                (
                    "input.cases.map((processInfo) => "
                    "projection.classifyPane(input.pane, processInfo))"
                ),
                {"pane": pane, "cases": rejected},
            ),
            ["absent", "absent", "absent", "absent", "absent", "absent"],
        )
        self.assertEqual(
            run_projection(
                "projection.classifyPane(input.pane, input.processInfo)",
                {
                    "pane": {"agent": "codex", "agent_status": "working"},
                    "processInfo": cases[2],
                },
            ),
            "manager",
        )

    def test_every_patch_owns_the_complete_mutually_exclusive_token_family(self):
        classifications = [
            "manager",
            "blocked",
            "working",
            "idle",
            "unknown",
            "absent",
        ]
        patches = run_projection(
            "Object.fromEntries(input.map((kind) => [kind, projection.tokenPatchFor(kind)]))",
            classifications,
        )
        expected_names = {
            "hml_role",
            "hml_manager",
            "hml_blocked",
            "hml_working",
            "hml_idle",
            "hml_unknown",
        }
        for classification, patch in patches.items():
            with self.subTest(classification=classification):
                self.assertEqual(set(patch), expected_names)
                visible = [
                    value
                    for name, value in patch.items()
                    if name != "hml_role" and value is not None
                ]
                self.assertEqual(len(visible), 0 if classification == "absent" else 1)
                self.assertEqual(
                    patch["hml_role"], "manager" if classification == "manager" else None
                )

    def test_unknown_classification_is_rejected(self):
        script = f"""
import {{ tokenPatchFor }} from {json.dumps(PROJECTION_URI)};
try {{ tokenPatchFor("paused"); }} catch (error) {{
  process.stdout.write(JSON.stringify({{ name: error.name, message: error.message }}));
}}
"""
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        error = json.loads(result.stdout)
        self.assertEqual(error["name"], "TypeError")
        self.assertIn("paused", error["message"])


class ManagerLightHookTests(unittest.TestCase):
    def run_hook(self, state, event=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_herdr = root / "herdr"
            log = root / "calls.jsonl"
            fake_herdr.write_text(FAKE_HERDR, encoding="utf-8")
            fake_herdr.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HERDR_BIN_PATH": str(fake_herdr),
                    "HERDR_PLUGIN_EVENT": "startup",
                    "HML_FAKE_LOG": str(log),
                    "HML_FAKE_STATE": json.dumps(state),
                }
            )
            if event is not None:
                environment["HERDR_PLUGIN_EVENT"] = "pane.agent_status_changed"
                environment["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(event)
            result = subprocess.run(
                ["node", str(HOOK)],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            calls = (
                [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
                if log.exists()
                else []
            )
        return result, calls

    def test_startup_reconciles_agents_and_verified_manager_only(self):
        state = {
            "panes": [
                {
                    "pane_id": "agent-pane",
                    "agent": "codex",
                    "agent_status": "working",
                },
                {
                    "pane_id": "manager-pane",
                    "tokens": {"hml_role": "manager"},
                },
                {
                    "pane_id": "recovered-manager-pane",
                    "agent": "grok",
                    "agent_status": "idle",
                },
                {"pane_id": "ordinary-pane"},
            ],
            "process_info": {
                "manager-pane": {
                    "foreground_processes": [{"argv": ["herdr-orchestrator", "manager"]}]
                },
                "recovered-manager-pane": {"foreground_processes": [{"argv": ["herdr-manager"]}]},
            },
        }

        result, calls = self.run_hook(state)

        self.assertEqual(result.returncode, 0, result.stderr)
        reports = [call for call in calls if call[:2] == ["pane", "report-metadata"]]
        self.assertEqual(
            [call[2] for call in reports],
            ["agent-pane", "manager-pane", "recovered-manager-pane"],
        )
        self.assertIn("hml_working=●", reports[0])
        self.assertIn("hml_manager=●", reports[1])
        self.assertIn("hml_role=manager", reports[1])
        self.assertIn("hml_manager=●", reports[2])
        self.assertIn("hml_role=manager", reports[2])
        for report in reports:
            mutations = report.count("--token") + report.count("--clear-token")
            self.assertEqual(mutations, 6)

    def test_event_payload_is_only_a_trigger_and_current_pane_is_reread(self):
        state = {
            "panes": [
                {
                    "pane_id": "agent-pane",
                    "agent": "codex",
                    "agent_status": "blocked",
                }
            ]
        }

        result, calls = self.run_hook(
            state,
            {
                "data": {
                    "pane": {
                        "pane_id": "agent-pane",
                        "agent_status": "working",
                    }
                }
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0][:3], ["pane", "get", "agent-pane"])
        report = calls[-1]
        self.assertIn("hml_blocked=●", report)
        self.assertNotIn("hml_working=●", report)

    def test_malformed_event_is_reported_as_hook_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_herdr = root / "herdr"
            fake_herdr.write_text(FAKE_HERDR, encoding="utf-8")
            fake_herdr.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HERDR_BIN_PATH": str(fake_herdr),
                    "HERDR_PLUGIN_EVENT": "pane.created",
                    "HERDR_PLUGIN_EVENT_JSON": "{",
                    "HML_FAKE_LOG": str(root / "calls.jsonl"),
                }
            )

            result = subprocess.run(
                ["node", str(HOOK)],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")


class ManagerLightConfigTests(unittest.TestCase):
    def cli_environment(self, root, config):
        fake_herdr = root / "herdr"
        log = root / "calls.jsonl"
        fake_herdr.write_text(FAKE_HERDR, encoding="utf-8")
        fake_herdr.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "HERDR_BIN_PATH": str(fake_herdr),
                "HERDR_CONFIG_PATH": str(config),
                "HML_FAKE_LOG": str(log),
                "HML_PLUGIN_STATE": str(root / "plugin.json"),
            }
        )
        return environment

    def run_cli(self, action, environment):
        return subprocess.run(
            ["node", str(CLI), "manager-light", action],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_install_is_idempotent_and_uninstall_restores_exact_original_bytes(self):
        originals = [
            "",
            'theme = "tokyo-night"',
            'theme = "tokyo-night"\n',
            "# [ui.sidebar.agents.id_overlay]\n# enabled = false\n",
        ]
        results = run_configure(
            (
                "input.map((source) => { "
                "const installed = configure.installConfigText(source); "
                "return { installed, "
                "reinstalled: configure.installConfigText(installed), "
                "uninstalled: configure.uninstallConfigText(installed) }; })"
            ),
            originals,
        )
        for original, result in zip(originals, results, strict=True):
            with self.subTest(original=original):
                self.assertEqual(result["reinstalled"], result["installed"])
                self.assertEqual(result["uninstalled"], original)
                self.assertIn("[ui.sidebar.agents]", result["installed"])
                self.assertNotIn("state_icon", result["installed"])

    def test_external_agent_rows_are_refused_without_rewriting_input(self):
        sources = [
            '[ui.sidebar.agents]\nrows = [["agent"]]\n',
            '[ui.sidebar]\nagents = { rows = [["agent"]] }\n',
            'ui.sidebar.agents = { rows = [["agent"]] }\n',
            '[ui.sidebar.agents.rows_by_agent]\ncodex = [["agent"]]\n',
            'ui.sidebar.agents.rows_by_agent = { codex = [["agent"]] }\n',
        ]
        inspections = run_configure(
            "input.map((source) => configure.inspectConfigText(source))", sources
        )
        self.assertEqual([item["state"] for item in inspections], ["conflict"] * 5)
        self.assertTrue(all(item["conflict"] for item in inspections))

    def test_malformed_or_modified_owned_markers_are_refused(self):
        sources = [
            "# BEGIN herdr-manager-light managed ui.sidebar.agents\n",
            (
                "# BEGIN herdr-manager-light managed ui.sidebar.agents\n"
                "[ui.sidebar.agents]\nrows = []\n"
                "# END herdr-manager-light managed ui.sidebar.agents"
            ),
        ]
        inspections = run_configure(
            "input.map((source) => configure.inspectConfigText(source))", sources
        )
        self.assertEqual([item["state"] for item in inspections], ["malformed"] * 2)
        self.assertEqual(
            [item["error"] for item in inspections],
            [
                "manager_light_config_markers_malformed",
                "manager_light_config_block_modified",
            ],
        )

    def test_cli_install_status_and_uninstall_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            original = "# [ui.sidebar.agents.id_overlay]\n# enabled = false\n"
            config.write_text(original, encoding="utf-8")
            environment = self.cli_environment(root, config)

            install = self.run_cli("install", environment)
            installed = config.read_text(encoding="utf-8")
            reinstall = self.run_cli("install", environment)
            status = self.run_cli("status", environment)
            uninstall = self.run_cli("uninstall", environment)

            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertTrue(json.loads(install.stdout)["config"]["changed"])
            self.assertFalse(json.loads(reinstall.stdout)["config"]["changed"])
            self.assertTrue(json.loads(status.stdout)["ok"])
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertFalse((root / "plugin.json").exists())
            self.assertNotIn("state_icon", installed)

    def test_candidate_validation_failure_leaves_config_and_plugin_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            original = 'theme = "tokyo-night"\n'
            config.write_text(original, encoding="utf-8")
            environment = self.cli_environment(root, config)
            environment["HML_CONFIG_CHECK_FAIL"] = "1"

            install = self.run_cli("install", environment)

            self.assertEqual(install.returncode, 2)
            self.assertEqual(install.stderr.strip(), "manager_light_config_candidate_invalid")
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertFalse((root / "plugin.json").exists())
            self.assertFalse(Path(f"{config}.herdr-manager-light.candidate").exists())

    def test_plugin_link_failure_rolls_back_the_exact_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            original = 'theme = "tokyo-night"'
            config.write_text(original, encoding="utf-8")
            environment = self.cli_environment(root, config)
            environment["HML_PLUGIN_LINK_FAIL"] = "1"

            install = self.run_cli("install", environment)

            self.assertEqual(install.returncode, 2)
            self.assertEqual(install.stderr.strip(), "manager_light_plugin_link_failed")
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertFalse((root / "plugin.json").exists())

    def test_status_reads_the_real_herdr_0_8_2_plugin_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            installed = run_configure(
                "configure.installConfigText(input)", 'theme = "tokyo-night"\n'
            )
            config.write_text(installed, encoding="utf-8")
            environment = self.cli_environment(root, config)
            plugin_root = ROOT / "plugins" / "manager-light"
            (root / "plugin.json").write_text(
                json.dumps(
                    {
                        "plugin_id": "herdr-manager-light",
                        "plugin_root": str(plugin_root),
                        "enabled": True,
                    }
                ),
                encoding="utf-8",
            )

            status = self.run_cli("status", environment)

        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["plugin"]["owned"])
        self.assertEqual(payload["plugin"]["path"], str(plugin_root))

    def test_manifest_routes_startup_and_lifecycle_triggers_to_one_hook(self):
        manifest = tomllib.loads(
            (ROOT / "plugins/manager-light/herdr-plugin.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["min_herdr_version"], "0.8.2")
        self.assertEqual(manifest["startup"], [{"command": ["node", "hook.mjs"]}])
        self.assertEqual(
            {event["on"] for event in manifest["events"]},
            {
                "pane.created",
                "pane.moved",
                "pane.agent_detected",
                "pane.agent_status_changed",
            },
        )
        self.assertTrue(
            all(event["command"] == ["node", "hook.mjs"] for event in manifest["events"])
        )


if __name__ == "__main__":
    unittest.main()
