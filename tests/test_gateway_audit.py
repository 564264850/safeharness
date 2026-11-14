import hashlib
import json
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest

from safeharness.audit import AuditError, AuditLog
from safeharness.coding import CodingReply
from safeharness.execution import ToolGateway, ToolRequest
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore


@pytest.fixture
def audited(tmp_path):
    store = TaskStore(tmp_path / "control.sqlite3")
    store.initialize()
    task = store.transition(store.create(), TaskStatus.RUNNING)
    return store, task, AuditLog(store.database, task.task_id)


@pytest.mark.parametrize(
    "tool_request,code",
    [
        (ToolRequest("run_shell", {"command": "whoami"}), "unknown_tool"),
        (ToolRequest("read_file", {"path": "../secret"}), "path_out_of_scope"),
        (ToolRequest("read_file", {"path": "C:/Users/key"}), "path_out_of_scope"),
        (ToolRequest("read_file", {"path": "private.py"}), "resource_out_of_scope"),
        (
            ToolRequest(
                "write_file", {"path": "solution.py", "content": "x", "approved": True}
            ),
            "invalid_arguments",
        ),
        (
            ToolRequest(
                "run_tests", {"paths": ["tests/test_solution.py"], "command": "bash"}
            ),
            "invalid_arguments",
        ),
        (
            ToolRequest("run_tests", {"paths": ["tests/test_solution.py;whoami"]}),
            "path_out_of_scope",
        ),
        (ToolRequest("run_tests", {"paths": ["-c"]}), "resource_out_of_scope"),
        (ToolRequest("read_file", {"path": 1}), "invalid_arguments"),
        (
            ToolRequest("write_file", {"path": "solution.py", "content": "中" * 3000}),
            "invalid_arguments",
        ),
    ],
)
def test_gateway_rejects_before_dispatch_and_records_decision(
    audited, tool_request, code
):
    _, _, audit = audited
    coding = Mock()
    gateway = ToolGateway(
        coding=coding,
        readable_files=frozenset({"solution.py"}),
        test_paths=frozenset({"tests/test_solution.py"}),
        audit=audit,
    )
    assert gateway.execute(tool_request).code == code
    coding.execute.assert_not_called()
    events = audit.read()
    assert [event["phase"] for event in events] == ["POLICY", "RESULT"]
    assert events[0]["status"] == "DENY"
    assert events[1]["code"] == code


@pytest.mark.parametrize("content", ["x" * 8193, "中" * 3000])
def test_oversized_write_returns_size_feedback_without_source_or_dispatch(
    audited, content
):
    _, _, audit = audited
    coding = Mock()
    gateway = ToolGateway(
        coding=coding, writable_files=frozenset({"solution.py"}), audit=audit
    )
    reply = gateway.execute(
        ToolRequest("write_file", {"path": "solution.py", "content": content})
    )
    assert reply.code == "invalid_arguments"
    feedback = json.loads(reply.value)
    assert feedback["actual_bytes"] == len(content.encode("utf-8"))
    assert feedback["max_bytes"] == 8192 and feedback["issue"] == "utf8_byte_limit"
    assert content not in reply.value
    coding.execute.assert_not_called()
    assert audit.read()[-1]["code"] == "invalid_arguments"


def test_hidden_test_verifier_is_scoped_recorded_and_unavailable_to_model(audited):
    store, task, audit = audited
    coding = Mock()
    coding.execute.return_value = CodingReply(True, "tests_passed", {"exit_code": 0})
    gateway = ToolGateway(
        coding=coding, test_paths=frozenset({"tests/test_private.py"}), audit=audit
    )
    denied = gateway.execute(
        ToolRequest("_verify_tests", {"paths": ["tests/test_private.py"]})
    )
    assert denied.code == "control_plane_only"
    coding.execute.assert_not_called()
    task = store.transition(task, TaskStatus.VERIFYING)
    assert (
        gateway.verify_tests(["tests/not-authorized.py"]).code
        == "resource_out_of_scope"
    )
    reply = gateway.verify_tests(["tests/test_private.py"])
    assert (
        reply.status == "failed"
        and reply.code == "trusted_contract_required"
        and reply.call_id is not None
    )
    from safeharness.invocations import InvocationLedger

    calls = InvocationLedger(store.database, task.task_id).snapshot()["calls"]
    assert calls[-1]["kind"] == "verification" and calls[-1]["status"] == "FAILED"
    assert calls[-1]["call_id"] == str(reply.call_id)
    coding.execute.assert_not_called()


def test_ticket_and_coding_share_journal_and_persist_intent_before_dispatch(audited):
    _, _, audit = audited
    coding = Mock()

    def write(action, args, **budgets):
        assert budgets == {"timeout": 15, "output_limit": 131072}
        events = audit.read()
        assert events[-1]["phase"] == "INTENT"
        assert events[-1]["tool_name"] == "write_file"
        return CodingReply(True, "completed", "file_written")

    coding.execute.side_effect = write
    gateway = ToolGateway(
        {"IT-001": "open"},
        frozenset({"IT-001"}),
        coding=coding,
        writable_files=frozenset({"solution.py"}),
        audit=audit,
    )
    assert (
        gateway.execute(ToolRequest("read_ticket", {"ticket_id": "IT-001"})).value
        == "open"
    )
    secret = "private source body"
    assert (
        gateway.execute(
            ToolRequest("write_file", {"path": "solution.py", "content": secret})
        ).status
        == "ok"
    )
    events = audit.read()
    assert [event["phase"] for event in events] == ["POLICY", "INTENT", "RESULT"] * 2
    assert len({event["call_id"] for event in events}) == 2
    assert events[0]["policy_version"] == "mock-ticket-v1"
    assert events[3]["policy_version"] == "coding-workspace-v1"
    canonical = json.dumps(
        {"content": secret, "path": "solution.py"},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert (
        events[3]["arguments_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    )
    assert secret not in json.dumps(events)


def test_journal_failure_and_non_running_task_block_side_effects(audited):
    store, task, audit = audited
    coding = Mock()
    gateway = ToolGateway(
        coding=coding, writable_files=frozenset({"solution.py"}), audit=audit
    )
    request = ToolRequest("write_file", {"path": "solution.py", "content": "changed"})
    with closing(sqlite3.connect(store.database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER refuse_event BEFORE INSERT ON gateway_events BEGIN SELECT RAISE(ABORT, 'no space'); END"
        )
    with pytest.raises(AuditError):
        gateway.execute(request)
    coding.execute.assert_not_called()
    assert audit.read() == []
    with closing(sqlite3.connect(store.database)) as connection, connection:
        connection.execute("DROP TRIGGER refuse_event")
    store.transition(task, TaskStatus.FAILED)
    with pytest.raises(AuditError, match="not running"):
        gateway.execute(request)
    coding.execute.assert_not_called()


def test_approval_pause_is_journaled_without_dispatch_intent(audited):
    _, _, audit = audited
    gateway = ToolGateway({}, writable_tickets=frozenset({"IT-001"}), audit=audit)
    assert (
        gateway.execute(
            ToolRequest(
                "update_ticket_status", {"ticket_id": "IT-001", "status": "resolved"}
            )
        ).status
        == "approval_required"
    )
    events = audit.read()
    assert [event["phase"] for event in events] == ["POLICY", "RESULT"]
    assert events[0]["status"] == "REQUIRE_APPROVAL"


def test_acceptance_verifier_uses_gateway_and_is_control_plane_only(audited):
    from safeharness.coding import CodingReply
    from safeharness.execution import public_tool_schemas

    store, task, audit = audited
    coding = Mock()
    coding.verify_acceptance.return_value = CodingReply(
        True, "completed", {"probe_exit_code": 0, "observed": [{"value": 3}]}
    )
    gateway = ToolGateway(coding=coding, audit=audit)
    arguments = {"function": "add", "cases": [[1, 2]]}
    assert (
        gateway.execute(ToolRequest("_verify_acceptance", arguments)).code
        == "control_plane_only"
    )
    coding.verify_acceptance.assert_not_called()
    assert "_verify_acceptance" not in {
        item["function"]["name"] for item in public_tool_schemas("coding")
    }
    store.transition(task, TaskStatus.VERIFYING)
    result = gateway.verify_acceptance("add", [[1, 2]])
    assert result.status == "ok" and json.loads(result.value)["observed"] == [
        {"value": 3}
    ]
    coding.verify_acceptance.assert_called_once_with("add", [[1, 2]])
    assert [row["phase"] for row in audit.read()][-3:] == ["POLICY", "INTENT", "RESULT"]
