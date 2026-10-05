from __future__ import annotations

import json
import re
from pathlib import Path

from herdr_orchestrator.delivery_prompts import (
    implementation_prompt,
    plan_prompt,
    principal_proxy_prompt,
    repair_prompt,
    review_verdict_prompt,
    spec_review_prompt,
    standards_review_prompt,
    wayfinder_chart_prompt,
    wayfinder_resolve_prompt,
    wayfinder_route_prompt,
)
from herdr_orchestrator.delivery_protocol import (
    DecisionTicket,
    DeliveryPlan,
    DeliveryTicket,
    FindingSeverity,
    ReviewFinding,
    WayfinderMap,
)
from herdr_orchestrator.model import Harness
from herdr_orchestrator.planner import planner_prompt

# A rendered prompt must never contain an unfilled `{placeholder}`; literal
# braces only appear inside the documented JSON schema examples as `{"key"...`.
_UNFILLED_PLACEHOLDER = re.compile(r"\{[A-Za-z_]")


def _ticket(ticket_id: str = "01") -> DeliveryTicket:
    return DeliveryTicket(
        ticket_id=ticket_id,
        title="Build the slice",
        what_to_build="End-to-end behavior",
        blocked_by=(),
        acceptance_criteria=("observable criterion",),
    )


def _plan() -> DeliveryPlan:
    return DeliveryPlan(
        slug="demo-delivery",
        title="Demo delivery",
        problem_statement="Problem",
        solution="Solution",
        user_stories=("As a user",),
        implementation_decisions=("Decision",),
        testing_decisions=("Testing",),
        out_of_scope=(),
        further_notes=(),
        seams=("seam",),
        tickets=(_ticket(),),
    )


def _map() -> WayfinderMap:
    return WayfinderMap(
        destination="Spec destination",
        notes=("note",),
        decisions=(
            DecisionTicket(
                ticket_id="01",
                title="Decision",
                question="Which route?",
                kind="research",
                blocked_by=(),
                resolution="resolved",
            ),
        ),
        not_yet_specified=(),
        out_of_scope=(),
    )


def _finding() -> ReviewFinding:
    return ReviewFinding(
        severity=FindingSeverity.MUST_FIX,
        summary="finding summary",
        evidence="file/hunk",
        source="rule",
    )


def _all_prompts(tmp_path: Path) -> dict[str, str]:
    out = tmp_path / "out.json"
    plan = _plan()
    return {
        "wayfinder_route": wayfinder_route_prompt("Ship the feature", out),
        "wayfinder_chart": wayfinder_chart_prompt("Ship the feature", out),
        "wayfinder_resolve": wayfinder_resolve_prompt(
            "Ship the feature", "{}", _map().decisions[0], out
        ),
        "plan": plan_prompt("Ship the feature", out, wayfinder=_map()),
        "plan_without_wayfinder": plan_prompt("Ship the feature", out, wayfinder=None),
        "implementation": implementation_prompt(plan, _ticket(), tmp_path / "receipt.json"),
        "standards_review": standards_review_prompt("abc123", out),
        "spec_review": spec_review_prompt("abc123", plan, out),
        "review_verdict": review_verdict_prompt(plan, {"f-1": _finding()}, out),
        "repair": repair_prompt(plan, {"f-1": _finding()}, 2),
        "principal_proxy": principal_proxy_prompt("Ship the feature", "May I?", out),
    }


def test_prompts_have_no_unfilled_placeholders(tmp_path: Path) -> None:
    for name, prompt in _all_prompts(tmp_path).items():
        assert _UNFILLED_PLACEHOLDER.search(prompt) is None, name
        assert prompt.count("{") == prompt.count("}"), name


def test_prompts_embed_output_path(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "out.json"
    plan = _plan()
    prompts = {
        "wayfinder_route": wayfinder_route_prompt("Ship the feature", out),
        "wayfinder_chart": wayfinder_chart_prompt("Ship the feature", out),
        "wayfinder_resolve": wayfinder_resolve_prompt(
            "Ship the feature", "{}", _map().decisions[0], out
        ),
        "plan": plan_prompt("Ship the feature", out, wayfinder=None),
        "standards_review": standards_review_prompt("abc123", out),
        "spec_review": spec_review_prompt("abc123", plan, out),
        "review_verdict": review_verdict_prompt(plan, {"f-1": _finding()}, out),
        "principal_proxy": principal_proxy_prompt("Ship the feature", "May I?", out),
    }

    for name, prompt in prompts.items():
        assert str(out) in prompt, name

    receipt = tmp_path / "receipt.json"
    assert str(receipt) in implementation_prompt(plan, _ticket(), receipt)


def test_wayfinder_route_prompt_schema(tmp_path: Path) -> None:
    prompt = wayfinder_route_prompt("Ship the feature", tmp_path / "route.json")

    assert '"use_wayfinder":true' in prompt
    assert '"reason":"..."' in prompt
    assert "Ship the feature" in prompt


def test_wayfinder_chart_prompt_schema(tmp_path: Path) -> None:
    prompt = wayfinder_chart_prompt("Ship the feature", tmp_path / "map.json")

    for field in (
        '"destination"',
        '"decisions"',
        '"id"',
        '"question"',
        '"kind"',
        '"blocked_by"',
        '"resolution"',
        '"not_yet_specified"',
        '"out_of_scope"',
    ):
        assert field in prompt


def test_wayfinder_resolve_prompt_schema_and_ticket(tmp_path: Path) -> None:
    selected = _map().decisions[0]

    prompt = wayfinder_resolve_prompt(
        "Ship the feature", "{}", selected, tmp_path / "resolution.json"
    )

    for field in ('"ticket_id"', '"resolution"', '"new_decisions"'):
        assert field in prompt
    assert json.dumps(selected.ticket_id) in prompt
    assert "Which route?" in prompt


def test_plan_prompt_schema(tmp_path: Path) -> None:
    prompt = plan_prompt("Ship the feature", tmp_path / "plan.json", wayfinder=None)

    for field in (
        '"slug"',
        '"title"',
        '"problem_statement"',
        '"solution"',
        '"user_stories"',
        '"implementation_decisions"',
        '"testing_decisions"',
        '"seams"',
        '"tickets"',
        '"what_to_build"',
        '"acceptance_criteria"',
    ):
        assert field in prompt
    assert "No Wayfinder map was needed." in prompt


def test_plan_prompt_embeds_resolved_map(tmp_path: Path) -> None:
    prompt = plan_prompt("Ship the feature", tmp_path / "plan.json", wayfinder=_map())

    assert "Spec destination" in prompt
    assert "No Wayfinder map was needed." not in prompt


def test_implementation_prompt_receipt_schema(tmp_path: Path) -> None:
    ticket = _ticket("07")
    receipt = tmp_path / "receipt.json"

    prompt = implementation_prompt(_plan(), ticket, receipt)

    for field in ('"ticket_id"', '"commit"', '"acceptance"', '"checks"', '"summary"'):
        assert field in prompt
    assert json.dumps(ticket.ticket_id) in prompt
    assert str(receipt) in prompt
    assert "Demo delivery" in prompt


def test_standards_review_prompt_schema(tmp_path: Path) -> None:
    prompt = standards_review_prompt("abc123", tmp_path / "standards.json")

    assert "abc123...HEAD" in prompt
    assert '"standards"' in prompt
    assert '"severity"' in prompt
    assert "must-fix|advisory" in prompt


def test_spec_review_prompt_schema(tmp_path: Path) -> None:
    prompt = spec_review_prompt("abc123", _plan(), tmp_path / "spec.json")

    assert '"spec"' in prompt
    assert '"source"' in prompt
    assert "Demo delivery" in prompt


def test_review_verdict_prompt_schema_and_findings(tmp_path: Path) -> None:
    prompt = review_verdict_prompt(_plan(), {"f-1": _finding()}, tmp_path / "verdict.json")

    for field in ('"accepted"', '"dismissed"', '"rationale"'):
        assert field in prompt
    assert "f-1" in prompt
    assert "finding summary" in prompt


def test_repair_prompt_embeds_round_and_findings(tmp_path: Path) -> None:
    prompt = repair_prompt(_plan(), {"f-1": _finding()}, 2)

    assert "round 2" in prompt
    assert "finding summary" in prompt


def test_principal_proxy_prompt_schema(tmp_path: Path) -> None:
    prompt = principal_proxy_prompt(
        "Ship the feature", "May I delete tmp?", tmp_path / "decision.json"
    )

    for field in ('"action"', '"category"', '"response"', '"rationale"'):
        assert field in prompt
    assert "answer|approve|deny|escalate" in prompt
    assert "May I delete tmp?" in prompt


def test_data_blocks_escape_embedded_json(tmp_path: Path) -> None:
    goal = 'goal with "quotes" and {braces}'

    prompt = wayfinder_route_prompt(goal, tmp_path / "route.json")

    assert json.dumps(goal, ensure_ascii=False) in prompt


def test_planner_prompt_preserves_dedupe_key_contract(tmp_path: Path) -> None:
    prompt = planner_prompt(
        "Plan work.",
        tmp_path / "tasks.json",
        3,
        '{"harnesses":[{"harness":"codex","summary":"coding"}]}',
        (Harness.CODEX, Harness.DROID),
    )

    assert '"dedupe_key"' in prompt
    assert '"title"' in prompt
    assert '"harness"' in prompt
    assert '"prompt"' in prompt
    assert "codex|droid" in prompt
    assert _UNFILLED_PLACEHOLDER.search(prompt) is None
