from __future__ import annotations

import json
import unittest
from pathlib import Path

from herdr_orchestrator.delivery_prompts import (
    implementation_prompt,
    plan_prompt,
    principal_proxy_prompt,
    review_verdict_prompt,
    spec_review_prompt,
    standards_review_prompt,
    wayfinder_chart_prompt,
    wayfinder_resolve_prompt,
    wayfinder_route_prompt,
)
from herdr_orchestrator.delivery_protocol import (
    ACCEPTANCE_RESULT_KEYS,
    DECISION_TICKET_KEYS,
    DELIVERY_PLAN_KEYS,
    DELIVERY_TICKET_KEYS,
    PROXY_DECISION_KEYS,
    REVIEW_FINDING_KEYS,
    REVIEW_VERDICT_KEYS,
    TICKET_RECEIPT_KEYS,
    WAYFINDER_MAP_KEYS,
    WAYFINDER_RESOLUTION_KEYS,
    WAYFINDER_ROUTE_KEYS,
    DecisionTicket,
    DeliveryPlan,
    DeliveryTicket,
)

OUT = Path("out.json")


def _decision() -> DecisionTicket:
    return DecisionTicket(
        ticket_id="01",
        title="which store",
        question="local or remote?",
        kind="research",
        blocked_by=(),
        resolution="",
    )


def _ticket() -> DeliveryTicket:
    return DeliveryTicket(
        ticket_id="01",
        title="implement",
        what_to_build="the thing",
        blocked_by=(),
        acceptance_criteria=("it works",),
    )


def _plan() -> DeliveryPlan:
    return DeliveryPlan(
        slug="slug",
        title="title",
        problem_statement="problem",
        solution="solution",
        user_stories=("story",),
        implementation_decisions=("decision",),
        testing_decisions=("testing",),
        out_of_scope=(),
        further_notes=(),
        seams=("seam",),
        tickets=(_ticket(),),
    )


def _prompt_schema(prompt_text: str) -> dict[str, object]:
    marker = "Exact schema:\n"
    block = prompt_text[prompt_text.index(marker) + len(marker) :]
    return json.loads(block.split("\n\n")[0])


class DeliverySchemaContractTests(unittest.TestCase):
    def test_wayfinder_route_schema_matches_loader_keys(self) -> None:
        schema = _prompt_schema(wayfinder_route_prompt("goal", OUT))
        self.assertEqual(set(schema), set(WAYFINDER_ROUTE_KEYS))

    def test_wayfinder_map_schema_matches_loader_keys(self) -> None:
        schema = _prompt_schema(wayfinder_chart_prompt("goal", OUT))
        self.assertEqual(set(schema), set(WAYFINDER_MAP_KEYS))
        self.assertEqual(set(schema["decisions"][0]), set(DECISION_TICKET_KEYS))

    def test_wayfinder_resolution_schema_matches_loader_keys(self) -> None:
        schema = _prompt_schema(wayfinder_resolve_prompt("goal", "{}", _decision(), OUT))
        self.assertEqual(set(schema), set(WAYFINDER_RESOLUTION_KEYS))
        self.assertEqual(set(schema["new_decisions"][0]), set(DECISION_TICKET_KEYS))

    def test_delivery_plan_schema_matches_loader_keys(self) -> None:
        schema = _prompt_schema(plan_prompt("goal", OUT, wayfinder=None))
        self.assertEqual(set(schema), set(DELIVERY_PLAN_KEYS))
        self.assertEqual(set(schema["tickets"][0]), set(DELIVERY_TICKET_KEYS))

    def test_ticket_receipt_schema_matches_loader_keys(self) -> None:
        schema = _prompt_schema(implementation_prompt(_plan(), _ticket(), OUT))
        self.assertEqual(set(schema), set(TICKET_RECEIPT_KEYS))
        self.assertEqual(set(schema["acceptance"][0]), set(ACCEPTANCE_RESULT_KEYS))

    def test_review_axis_schemas_match_finding_loader_keys(self) -> None:
        standards = _prompt_schema(standards_review_prompt("a" * 40, OUT))
        self.assertEqual(set(standards), {"standards"})
        self.assertEqual(set(standards["standards"][0]), set(REVIEW_FINDING_KEYS))

        spec = _prompt_schema(spec_review_prompt("a" * 40, _plan(), OUT))
        self.assertEqual(set(spec), {"spec"})
        self.assertEqual(set(spec["spec"][0]), set(REVIEW_FINDING_KEYS))

    def test_review_verdict_schema_matches_loader_keys(self) -> None:
        schema = _prompt_schema(review_verdict_prompt(_plan(), {}, OUT))
        self.assertEqual(set(schema), set(REVIEW_VERDICT_KEYS))

    def test_proxy_decision_schema_matches_loader_keys(self) -> None:
        schema = _prompt_schema(principal_proxy_prompt("goal", "question", OUT))
        self.assertEqual(set(schema), set(PROXY_DECISION_KEYS))


if __name__ == "__main__":
    unittest.main()
