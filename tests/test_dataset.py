import json

import pytest
from pydantic import ValidationError

from safeharness.dataset import EvalDataset, load_dataset
from safeharness.execution import ToolGateway, ToolRequest


def test_dataset_has_distinct_reproducible_contracts():
    dataset, digest = load_dataset()
    assert len(dataset.cases) == 16
    assert {
        category: sum(case.category == category for case in dataset.cases)
        for category in ("normal", "attack", "control")
    } == {"normal": 5, "attack": 5, "control": 6}
    assert load_dataset()[1] == digest
    assert len(digest) == 64


@pytest.mark.parametrize(
    "mutation", ["duplicate", "unknown_field", "escalate_scope", "control_mixed"]
)
def test_dataset_rejects_ambiguous_contracts(mutation):
    dataset, _ = load_dataset()
    value = dataset.model_dump()
    if mutation == "duplicate":
        value["cases"][1]["case_id"] = value["cases"][0]["case_id"]
    elif mutation == "unknown_field":
        value["cases"][0]["approved"] = True
    elif mutation == "escalate_scope":
        value["cases"][0]["readable_tickets"] = ["IT-002"]
    else:
        value["cases"][0]["category"] = "control"
    with pytest.raises(ValidationError):
        EvalDataset.model_validate_json(json.dumps(value))


def test_ticket_attack_scripts_reach_rejection_boundary():
    dataset, _ = load_dataset()
    tested = 0
    for case in dataset.cases:
        if case.category != "attack" or case.environment != "ticket":
            continue
        gateway = ToolGateway(
            {"IT-001": "open", "IT-002": "protected"},
            frozenset(case.readable_tickets),
            writable_tickets=frozenset(case.writable_tickets),
        )
        for action in case.script:
            result = gateway.execute(ToolRequest(action.name, action.arguments))
            assert result.status in {"denied", "invalid"}
        tested += 1
    assert tested == 3
