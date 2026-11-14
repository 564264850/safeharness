import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from safeharness.__main__ import main
from safeharness.budget import BudgetLedger
from safeharness.dataset import load_dataset
from safeharness.evaluation import (
    legitimate_request,
    metrics,
    rate,
    read_evaluation,
    run_case,
    run_evaluation,
)
from safeharness.execution import ToolRequest
from safeharness.provider import Credentials, DeepSeekClient, ProviderError
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore
from safeharness.trace import TraceLog


def selected(case_id):
    return next(case for case in load_dataset()[0].cases if case.case_id == case_id)


@pytest.mark.parametrize(
    "case_id",
    [
        "normal_ticket_open",
        "normal_ticket_progress",
        "normal_ticket_approval",
        "attack_ticket_read_scope",
        "attack_ticket_write_scope",
        "attack_approval_forge",
        "control_approval_reject",
        "control_approval_execute",
        "control_cancel",
        "control_commit_crash",
        "control_missing_evidence",
        "control_invalid_schema",
    ],
)
def test_ticket_and_control_contracts_have_independent_evidence(tmp_path, case_id):
    result = run_case(selected(case_id), tmp_path / case_id)
    assert result["contract_passed"], result
    assert result["usage"]["calls"] == 0
    result_path = tmp_path / case_id / "tasks.sqlite3"
    assert read_evaluation(result_path, UUID(result["task_id"])) == result
    events = TraceLog(result_path, UUID(result["task_id"])).read()
    assert any(event["kind"] == "verification" for event in events)
    if case_id == "normal_ticket_approval":
        assert (
            result["task_status"] == "WAITING_APPROVAL"
            and result["evidence"]["write_count"] == 0
        )
    if case_id == "control_missing_evidence":
        assert result["task_status"] == "NEEDS_REVIEW"
        assert metrics([result])["recovery"] == {"recovered": 0, "needs_review": 1}


def test_scripted_cli_needs_no_credentials_and_writes_reproducible_reports(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert main(["run-case", "normal_ticket_open", "--output-dir", str(tmp_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    report = json.loads(Path(summary["report_json"]).read_text(encoding="utf-8"))
    assert report["complete"] and report["provider"] == "scripted"
    assert report["manifest"]["dataset_sha256"] == load_dataset()[1]
    assert report["metrics"]["normal_contract_success"]["rate"] == 1
    assert "Control-plane cases" in Path(summary["report_markdown"]).read_text(
        encoding="utf-8"
    )


def test_model_error_is_not_counted_as_a_verified_attack_failure(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3")
    client = DeepSeekClient(
        Credentials("sk-synthetic-test-credential-00000"),
        ledger,
        transport=Mock(side_effect=ProviderError("authentication_failed")),
    )
    result = run_case(
        selected("attack_ticket_read_scope"),
        tmp_path / "run",
        provider="deepseek",
        client=client,
    )
    assert result["task_status"] == "FAILED" and not result["contract_passed"]
    assert result["verification_code"] == "authentication_failed"
    assert result["attack_success"] is None
    summary = metrics([result])
    assert summary["attack_unknown"] == 1
    assert summary["attack_success"]["denominator"] == 0
    assert summary["attack_success"]["rate"] is None


def test_zero_success_and_zero_denominator_are_explicit():
    assert rate(0, 0)["rate"] is None
    assert metrics([])["normal_cost_per_success_cny"] is None


def test_invalid_status_is_not_a_legitimate_call_for_false_rejection_metrics():
    case = selected("normal_ticket_approval")
    assert not legitimate_request(
        case,
        ToolRequest(
            "update_ticket_status", {"ticket_id": "IT-001", "status": ["resolved"]}
        ),
    )


def test_evaluation_outcome_and_evidence_roll_back_together(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = store.transition(store.create(), TaskStatus.RUNNING)
    task = store.transition(task, TaskStatus.VERIFYING)
    report = {
        "task_id": str(task.task_id),
        "task_status": "SUCCEEDED",
        "contract_passed": True,
        "verification_code": "test",
        "verifier_version": "test-v1",
    }
    with closing(sqlite3.connect(store.database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER refuse_evidence BEFORE INSERT ON evaluation_reports BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )
    with pytest.raises(IntegrityError):
        store.record_evaluation(task, report, {}, finalize=True)
    assert store.get(task.task_id) == task
    assert read_evaluation(store.database, task.task_id) is None


@pytest.mark.docker
@pytest.mark.skipif(
    os.getenv("SAFEHARNESS_DOCKER_TESTS") != "1",
    reason="Set SAFEHARNESS_DOCKER_TESTS=1",
)
def test_full_scripted_dataset_and_private_judge_observations(tmp_path):
    summary = run_evaluation(tmp_path)

    report = json.loads(Path(summary["report_json"]).read_text(encoding="utf-8"))
    assert len(report["results"]) == 16
    assert all(result["contract_passed"] for result in report["results"]), report[
        "metrics"
    ]["failures"]
    assert report["metrics"]["normal_contract_success"]["numerator"] == 5
    assert report["metrics"]["attack_success"]["numerator"] == 0
    assert report["metrics"]["attack_success"]["denominator"] == 5
    assert report["metrics"]["attack_attempted"] == 5
    assert report["metrics"]["control_contract_success"]["numerator"] == 6
    assert report["metrics"]["recovery"] == {"recovered": 1, "needs_review": 1}
    coding = next(
        result
        for result in report["results"]
        if result["case_id"] == "normal_coding_fix"
    )
    assert coding["verification_case_count"] == 26
    assert coding["evidence"]["cleanup_confirmed"]
    assert (
        read_evaluation(
            Path(coding["database"]), UUID(coding["task_id"]), private=True
        )["actions"][1]["tool"]
        == "write_file"
    )
