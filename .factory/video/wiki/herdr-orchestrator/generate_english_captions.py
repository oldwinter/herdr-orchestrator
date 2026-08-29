from __future__ import annotations

import json
import re
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WIKI_VIDEO = ROOT.parents[3] / "droid-wiki" / "video"

ENGLISH = {
    "01": "If an agent process disappears, task state should not disappear with it. Herdr Orchestrator places multiple coding harnesses behind one local control plane. Models may propose tasks and select workers. But Python and SQLite always own the queue, leases, retries, and success decisions. Remember it this way: Herdr hosts terminals, while the Coordinator owns the facts.",
    "02": "The first step is loading a declarative workflow, not starting a model. TOML fixes concurrency, leases, timeouts, placement, and the allowed workers. The router sees only a compact harness catalog, not six long contexts. After Droid, Grok, Codex, Pi, Claude, or Hermes is selected, its full profile is injected at dispatch. This keeps context small and locks routing to configured candidates.",
    "03": "After a task enters SQLite, its dedupe key prevents duplicate work. Claim runs inside an immediate write transaction. It increments the attempt, sets the lease, chooses a replica slot, and creates a correlation ID. Each wave claims only the configured capacity before dispatching through Herdr. Store deterministically folds outcomes into success, blocked, retry, or failure. A stale attempt can never overwrite a newly claimed execution.",
    "04": "Herdr runtime does not treat a running process as proof that a task started. It first proves the pane shell is ready, then proves the harness is interactive. Before submitting the prompt, it records the state change sequence. Only a strict sequence advance proves a new turn. The turn must then stabilize in idle or done. Blocked, working, unknown, and timeout are not success. Every wait shares one bounded deadline.",
    "05": "After choosing a worker, the system chooses where the task runs. A tab provides a dedicated visible location. Pane placement shares a tab within one wave while preserving separate terminals. Worktree placement creates a task branch, checkout, and Herdr workspace for write work. Explicit overrides and worker defaults come first. Deterministic read and write signals come next. Only ambiguous cases reach a bounded topology controller. A worktree isolates a checkout, not security.",
    "06": "Each of the six harnesses receives fixed maximum automation flags that planners cannot override. These flags reduce local confirmations, but they do not authorize pushes, merges, publishing, or production actions. Claude workspace trust has another guard. Three stable markers must appear, and the resolved execution root must match one complete output line. Login prompts, authentication, another directory, or missing markers never trigger automatic input.",
    "07": "Settled only means the agent stopped. It does not prove the task is correct. An output receipt must appear in new lines from the current turn. Prompt echoes and old output cannot count. A file receipt must stay inside the execution root, reject absolute paths, parent traversal, and symlinks, and change relative to its baseline. Failed tasks may receive more attempt budget. Blocked tasks continue on the same agent, pane, and attempt after a reviewed response.",
    "08": "The Dashboard projects SQLite durable state and Herdr runtime into one read-only snapshot. It shows queue state, attention items, receipt history, and a compound project, worktree, tab, and pane topology. Structural changes trigger layout, while status changes update content only, so live refreshes remain stable. The server binds only to loopback, validates Host and port, applies CSP and field allowlists, and never reads prompts, environment variables, or complete terminal output.",
    "09": "Alongside ordinary dispatch, standardized delivery is available only through explicit opt-in. It resolves specification-blocking decisions, then creates an accepted spec and dependency-ordered ticket graph. Each ticket is implemented in an isolated worktree and closed with commit and acceptance evidence. After integration, fresh Standards and Spec reviewers run in parallel, and the controller adjudicates every finding. Repair rounds are bounded. Success stops on an isolated integration branch.",
    "10": "This project is not an operating system sandbox. It protects state transitions, evidence, and authorization boundaries. Model artifacts pass exact-key, enum, length, and allowlist checks. Telemetry stays local by default, exporters are off by default, and fields are sanitized before persistence. The installer manages only manifest-owned project paths and stops conservatively on symlinks or user changes. Garbage collection starts as a dry run and requires creation, pane identity, and current ownership evidence.",
    "11": "Unattended work enters the durable queue, where the Coordinator owns leases, retries, and receipts. Live operations use Manual Manager instead. It starts one interactive harness from a fixed policy directory inside the current Herdr session, and never pretends to provide queue semantics. The npx herdr-manager entry point probes Grok, Codex, then Claude by default. Optional Manager Light only projects current pane and agent facts into the sidebar through an owned config block and mutually exclusive metadata tokens. Its colors are not task receipts and do not own lifecycle.",
    "12": "The version zero point one point six baseline has twenty-six reachable commits, twenty-six package Python modules, and twenty-two Python test files. Nine thousand five hundred forty-three Python test lines compared with nine thousand nine hundred sixteen package source lines gives a size ratio of about zero point nine six to one, not code coverage. Contributors run focused tests first, then just check, followed by a real doctor or read-only smoke when needed. Remember three invariants: model output is constrained, the Coordinator owns state, and current execution must produce new evidence before success is accepted.",
}


def timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def chunks(text: str) -> list[str]:
    words = text.split()
    result: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and (len(current) >= 7 or len(candidate) > 42):
            result.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        result.append(" ".join(current))
    return result


def main() -> None:
    scenes = json.loads((ROOT / "scenes-timed.json").read_text(encoding="utf-8"))
    source_chinese = ROOT / "assets" / "captions.en.vtt"
    chinese_target = WIKI_VIDEO / "captions.zh-CN.vtt"
    shutil.copyfile(source_chinese, chinese_target)

    output = ["WEBVTT", ""]
    previous_end = 0.0
    for scene in scenes:
        text = ENGLISH[scene["id"]]
        cue_text = chunks(text)
        weights = [len(re.findall(r"\w+", cue)) for cue in cue_text]
        total = sum(weights)
        cursor = max(previous_end, float(scene["start"]))
        spoken_end = float(scene["start"]) + float(scene["spokenDuration"])
        for cue, weight in zip(cue_text, weights):
            cue_end = min(spoken_end, cursor + (spoken_end - float(scene["start"])) * weight / total)
            cue_end = max(cursor + 0.08, cue_end)
            output.extend([f"{timestamp(cursor)} --> {timestamp(cue_end)}", cue, ""])
            cursor = cue_end
            previous_end = cue_end

    (WIKI_VIDEO / "captions.en.vtt").write_text("\n".join(output), encoding="utf-8")
    print(
        json.dumps(
            {
                "englishBytes": (WIKI_VIDEO / "captions.en.vtt").stat().st_size,
                "chineseBytes": chinese_target.stat().st_size,
            }
        )
    )


if __name__ == "__main__":
    main()
