import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest

pytest.importorskip("openjudge")
from openjudge.graders.agent import ActionLoopDetectionGrader

from safeharness.budget import BudgetLedger
from safeharness.dataset import load_dataset
from safeharness.evaluation import run_case
from safeharness.judging import action_messages, grade_report, judge_version
from safeharness.provider import Credentials, DeepSeekClient
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore

pytestmark = pytest.mark.judge


def make_source(tmp_path, *, failed=False):
    case = next(
        case for case in load_dataset()[0].cases if case.case_id == "normal_ticket_open"
    )
    if failed:
        action = case.script[0].model_copy(
            update={"arguments": {"ticket_id": "IT-002"}}
        )
        case = case.model_copy(update={"script": [action]})
    result = run_case(case, tmp_path / "case")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"results": [result]}), encoding="utf-8")
    return report, result


def test_code_based_loop_grader_contract():
    grader = ActionLoopDetectionGrader()
    repeated = [
        {
            "tool": "read_ticket",
            "arguments": {"ticket_id": "IT-001"},
            "status": "ok",
            "code": "ticket_read",
        }
    ] * 3
    distinct = [
        {**action, "arguments": {"ticket_id": f"IT-00{index}"}}
        for index, action in enumerate(repeated, 1)
    ]
    loop = asyncio.run(grader.aevaluate(messages=action_messages(repeated)))
    no_loop = asyncio.run(grader.aevaluate(messages=action_messages(distinct)))
    assert no_loop.score == 1 and loop.score < no_loop.score
    assert judge_version() == "0.2.4"


def test_real_grader_bridge_uses_bounded_client_and_ledger(tmp_path):
    report, _ = make_source(tmp_path)
    ledger = BudgetLedger(tmp_path / "budget.sqlite3")
    transport = Mock(
        return_value={
            "model": "deepseek-flash",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {"score": 5, "reason": "contract response"}
                        ),
                    },
                }
            ],
        }
    )
    client = DeepSeekClient(
        Credentials("sk-synthetic-judge-credential-000"), ledger, transport=transport
    )
    result = grade_report(report, client=client)
    assert transport.call_count == 2 and ledger.usage()["calls"] == 2
    assert all(
        grade["score"] == 5 for grade in result["results"][0]["graders"].values()
    )
    assert transport.call_args.args[0]["response_format"] == {"type": "json_object"}
    assert "synthetic-judge-credential" not in Path(result["judge_report"]).read_text()


def test_semantic_score_never_changes_independent_task_outcome(tmp_path):
    report, original = make_source(tmp_path, failed=True)
    assert not original["contract_passed"]
    judged = grade_report(report)
    assert judged["judge_provider"] == "contract-fixture"
    assert judged["results"][0]["selection_and_task_outcome_differ"]
    task = TaskStore(Path(original["database"])).get(UUID(original["task_id"]))
    assert task.status is TaskStatus.FAILED


def test_judge_budget_failure_is_unknown_not_a_zero_quality_score(tmp_path):
    report, _ = make_source(tmp_path)
    transport = Mock()
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_cny=Decimal("0.000001"))
    client = DeepSeekClient(
        Credentials("sk-synthetic-judge-credential-000"), ledger, transport=transport
    )
    judged = grade_report(report, client=client)
    selection = judged["results"][0]["graders"]["tool_selection"]
    assert selection["status"] == "failed" and selection["score"] is None
    assert selection["error"] == "provider_budget_exhausted"
    transport.assert_not_called()
