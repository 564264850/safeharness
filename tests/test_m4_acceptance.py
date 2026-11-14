"""120 explicit synthetic development instances; see scripts/m4_acceptance.py.

No model API calls. Case families and instance counts are reported separately.
"""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from test_postgres import pg_store  # noqa: F401 - shared isolated-schema fixture
from test_service import create_run, headers, principals

from safeharness.acceptance_cases import FAMILIES
from safeharness.approval import ApprovalActor, ApprovalIntent, ApprovalOutcome
from safeharness.audit import AuditLog
from safeharness.coding import CodingWorkspace
from safeharness.database import execute
from safeharness.execution import ToolGateway, ToolRequest
from safeharness.runtime import FinalAnswer, run_task
from safeharness.service import create_app
from safeharness.task import TaskStatus
from safeharness.task_store import ExecutionConflictError, TaskStore
from safeharness.ticket_store import EvidenceUnavailable, TicketStore
from safeharness.trace import TraceLog

pytestmark = pytest.mark.acceptance
DOCKER = pytest.mark.skipif(
    os.getenv("SAFEHARNESS_DOCKER_TESTS") != "1", reason="Enable Docker acceptance"
)


def evidence(record_property, group, family, **data):
    record_property(
        "acceptance", json.dumps({"group": group, "family": family, **data})
    )


@pytest.mark.docker
@DOCKER
@pytest.mark.parametrize(
    "family,variant",
    [(family, variant) for family in FAMILIES for variant in range(3)],
    ids=[
        f"{family.name}-bug{variant + 1}" for family in FAMILIES for variant in range(3)
    ],
)
def test_coding_matrix(tmp_path, record_property, family, variant):
    store = TaskStore(tmp_path / "coding.sqlite3")
    store.initialize()
    task = store.create()
    initial = family.source(family.bugs[variant])
    fixed = family.source(family.fixed_body)
    # Visible tests cover one happy path only; the host checks all boundary cases.
    case = family.cases[0]
    expected = family.expected[0]["value"]
    visible = f"from solution import {family.name}\ndef test_example(): assert {family.name}(*{case!r}) == {expected!r}\n"
    workspace = CodingWorkspace(
        {"solution.py": initial, "tests/test_solution.py": visible}, task.task_id
    )
    broken = workspace.verify_acceptance(family.name, family.cases)
    assert broken.ok and broken.value["observed"] != family.expected, broken
    gateway = ToolGateway(
        coding=workspace,
        readable_files=frozenset(workspace.files),
        writable_files=frozenset({"solution.py"}),
        test_paths=frozenset({"tests/test_solution.py"}),
        audit=AuditLog(store.database, task.task_id),
    )
    actions = [
        ToolRequest("read_file", {"path": "solution.py"}),
        ToolRequest("write_file", {"path": "solution.py", "content": fixed}),
        ToolRequest("run_tests", {"paths": ["tests/test_solution.py"]}),
    ]
    checked = {}

    def verify(run):
        reply = gateway.verify_acceptance(family.name, family.cases)
        observed = json.loads(reply.value) if reply.status == "ok" else {}
        checked.update(
            ok=reply.status == "ok",
            result=observed,
            cleanup=workspace.last_container.cleanup_confirmed,
        )
        TraceLog(store.database, task.task_id).emit(
            "verification",
            {
                "verifier_version": "function-contract-v1",
                "code": family.name,
                "case_count": len(family.cases),
                "passed": reply.status == "ok"
                and observed.get("observed") == family.expected,
            },
        )
        return (
            reply.status == "ok"
            and observed.get("probe_exit_code") == 0
            and observed.get("observed") == family.expected
            and run.tool_results[-1].code == "tests_passed"
        )

    result = run_task(
        lambda results: (
            actions[len(results)]
            if len(results) < len(actions)
            else FinalAnswer("ready")
        ),
        gateway.execute,
        verify,
        max_steps=4,
        store=store,
        task=task,
    )
    evidence(
        record_property,
        "coding",
        family.name,
        task_id=str(task.task_id),
        task_status=result.task.status.value,
        initial_bug_detected=True,
        verification=checked,
        image_id=workspace.image_id,
        final_sha256=hashlib.sha256(fixed.encode()).hexdigest(),
        trace=TraceLog(store.database, task.task_id).read(),
    )
    assert result.task.status is TaskStatus.SUCCEEDED and checked["cleanup"]


IT_FAMILIES = [
    "read",
    "proposal",
    "approve_execute",
    "reject",
    "cancel",
    "decision_replay",
    "wrong_digest",
    "read_only",
    "scope_revoked",
    "cross_owner",
]


@pytest.mark.parametrize(
    "family,initial",
    [
        (family, status)
        for family in IT_FAMILIES
        for status in ["open", "in_progress", "resolved"]
    ],
    ids=[
        f"{family}-{status}"
        for family in IT_FAMILIES
        for status in ["open", "in_progress", "resolved"]
    ],
)
def test_it_matrix(tmp_path, record_property, family, initial):
    store = TaskStore(tmp_path / "it.sqlite3")
    business = TicketStore(store.database)
    business.initialize(initial_status=initial)
    actors = principals()
    if family == "read_only":
        from dataclasses import replace

        actors = [
            replace(actor, writable_tickets=frozenset())
            if actor.subject == "alice"
            else actor
            for actor in actors
        ]
    target = {"open": "in_progress", "in_progress": "resolved", "resolved": "open"}[
        initial
    ]
    expected_writes = 0
    with TestClient(create_app(store, business, actors)) as client:
        task_id, result = create_run(
            client,
            "ticket_read" if family == "read" else "ticket_update",
            status=target,
        )
        if family == "read":
            assert result["task_status"] == "SUCCEEDED"
        elif family == "read_only":
            assert result["task_status"] == "FAILED"
        else:
            assert result["task_status"] == "WAITING_APPROVAL"
            approval = client.get(
                f"/v1/tasks/{task_id}/approval", headers=headers("reviewer")
            ).json()
            url = f"/v1/approvals/{approval['approval_id']}/decision"
            body = {
                "decision": "approve",
                "arguments_sha256": approval["arguments_sha256"],
            }
            if family == "cancel":
                assert (
                    client.post(
                        f"/v1/tasks/{task_id}/cancel", headers=headers("alice")
                    ).json()["task_status"]
                    == "CANCELLED"
                )
            elif family != "proposal":
                if family == "cross_owner":
                    assert (
                        client.get(
                            f"/v1/tasks/{task_id}", headers=headers("bob")
                        ).status_code
                        == 404
                    )
                    assert (
                        client.post(
                            f"/v1/tasks/{task_id}/resume", headers=headers("bob")
                        ).status_code
                        == 404
                    )
                if family == "wrong_digest":
                    assert (
                        client.post(
                            url,
                            json={**body, "arguments_sha256": "0" * 64},
                            headers=headers("reviewer"),
                        ).status_code
                        == 409
                    )
                if family == "reject":
                    body["decision"] = "reject"
                assert (
                    client.post(url, json=body, headers=headers("reviewer")).status_code
                    == 200
                )
                if family == "decision_replay":
                    assert (
                        client.post(
                            url, json=body, headers=headers("reviewer")
                        ).status_code
                        == 409
                    )
                if family == "scope_revoked":
                    from dataclasses import replace

                    revoked = [
                        replace(actor, writable_tickets=frozenset())
                        if actor.subject == "alice"
                        else actor
                        for actor in actors
                    ]
                    with TestClient(
                        create_app(TaskStore(store.database), business, revoked)
                    ) as restarted:
                        assert (
                            restarted.post(
                                f"/v1/tasks/{task_id}/resume", headers=headers("alice")
                            ).status_code
                            == 403
                        )
                elif family == "reject":
                    assert (
                        client.post(
                            f"/v1/tasks/{task_id}/resume", headers=headers("alice")
                        ).status_code
                        == 409
                    )
                else:
                    for _ in range(2):
                        assert (
                            client.post(
                                f"/v1/tasks/{task_id}/resume", headers=headers("alice")
                            ).json()["task_status"]
                            == "SUCCEEDED"
                        )
                    expected_writes = 1
        status, writes = business.ticket()
        evidence(
            record_property,
            "it",
            family,
            initial=initial,
            target=target,
            task_id=task_id,
            status=status,
            writes=writes,
            expected_writes=expected_writes,
            trace=TraceLog(
                store.database, store.get(__import__("uuid").UUID(task_id)).task_id
            ).read(),
        )
        assert (status, writes) == (
            target if expected_writes else initial,
            expected_writes,
        )


PATHS = [
    "../secret",
    "/etc/passwd",
    "C:/Users/key",
    "a\\b",
    "a/../b",
    "a//b",
    "a:stream",
    "./a",
    "",
    "a\x00b",
]
UNKNOWN = [
    "run_shell",
    "approve",
    "decide_approval",
    "delete_ticket",
    "eval",
    "curl",
    "_verify_coding_contract",
    "_verify_acceptance",
]
BAD_ARGS = [
    ToolRequest("read_ticket", {"ticket_id": 1}),
    ToolRequest("read_ticket", {"ticket_id": "IT-001", "approved": True}),
    ToolRequest("read_ticket", {"ticket_id": "IT-002"}),
    ToolRequest("update_ticket_status", {"ticket_id": "IT-002", "status": "resolved"}),
    ToolRequest("update_ticket_status", {"ticket_id": "IT-001", "status": "deleted"}),
    ToolRequest(
        "update_ticket_status",
        {"ticket_id": "IT-001", "status": "resolved", "approved": True},
    ),
    ToolRequest("write_file", {"path": "solution.py", "content": "x" * 8193}),
    ToolRequest("run_tests", {"paths": ["tests/test_solution.py"], "command": "sh"}),
]
HTTP_ATTACKS = [
    "foreign_read",
    "foreign_cancel",
    "self_approval",
    "forged_actor",
    "no_auth",
    "scanner_create",
    "wrong_digest",
    "approver_create",
]
INDIRECT = [
    "symlink",
    "hardlink",
    "fifo",
    "protected_write",
    "network",
    "host_environment",
]
ATTACKS = (
    [("path", str(index), path) for index, path in enumerate(PATHS)]
    + [("unknown_tool", name, name) for name in UNKNOWN]
    + [("schema_scope", str(index), request) for index, request in enumerate(BAD_ARGS)]
    + [("http_authority", name, name) for name in HTTP_ATTACKS]
    + [("indirect", name, name) for name in INDIRECT]
)


@pytest.mark.parametrize(
    "family,name,payload",
    ATTACKS,
    ids=[f"{family}-{name}" for family, name, _ in ATTACKS],
)
def test_attack_matrix(tmp_path, record_property, family, name, payload, monkeypatch):
    store = TaskStore(tmp_path / "attack.sqlite3")
    store.initialize()
    task = store.transition(store.create(), TaskStatus.RUNNING)
    workspace = CodingWorkspace(
        {"solution.py": "original", "tests/test_solution.py": ""}, task.task_id
    )
    gateway = ToolGateway(
        {"IT-001": "open", "IT-002": "synthetic-secret"},
        frozenset({"IT-001"}),
        writable_tickets=frozenset({"IT-001"}),
        coding=workspace,
        readable_files=frozenset(workspace.files),
        writable_files=frozenset({"solution.py"}),
        test_paths=frozenset({"tests/test_solution.py"}),
        audit=AuditLog(store.database, task.task_id),
    )
    detail = {}
    if family in {"path", "unknown_tool", "schema_scope"}:
        request = (
            ToolRequest("read_file", {"path": payload})
            if family == "path"
            else ToolRequest(payload, {})
            if family == "unknown_tool"
            else payload
        )
        result = gateway.execute(request)
        detail = {"status": result.status, "code": result.code}
        assert result.status in {"denied", "invalid"}
        assert workspace.files["solution.py"] == "original"
    elif family == "http_authority":
        business = TicketStore(store.database)
        with TestClient(create_app(store, business, principals())) as client:
            target, _ = create_run(client)
            approval = client.get(
                f"/v1/tasks/{target}/approval", headers=headers("reviewer")
            ).json()
            url = f"/v1/approvals/{approval['approval_id']}/decision"
            body = {
                "decision": "approve",
                "arguments_sha256": approval["arguments_sha256"],
            }
            if name == "foreign_read":
                response = client.get(f"/v1/tasks/{target}", headers=headers("bob"))
            elif name == "foreign_cancel":
                response = client.post(
                    f"/v1/tasks/{target}/cancel", headers=headers("bob")
                )
            elif name in {"self_approval", "forged_actor", "wrong_digest"}:
                if name == "forged_actor":
                    body["decided_by"] = "reviewer"
                if name == "wrong_digest":
                    body["arguments_sha256"] = "0" * 64
                response = client.post(
                    url,
                    json=body,
                    headers=headers("alice" if name == "self_approval" else "reviewer"),
                )
            else:
                response = client.post(
                    "/v1/tasks",
                    json={"scenario": "ticket_read"},
                    headers={}
                    if name == "no_auth"
                    else headers("scanner" if name == "scanner_create" else "reviewer"),
                )
            assert response.status_code in {401, 403, 404, 409, 422}
            assert business.ticket() == ("open", 0)
            detail = {"http_status": response.status_code, "target_task_id": target}
    else:
        if os.getenv("SAFEHARNESS_DOCKER_TESTS") != "1":
            pytest.skip("Enable Docker acceptance")
        statements = {
            "symlink": "os.symlink('/etc/passwd', '/workspace/stolen')",
            "hardlink": "os.link('/workspace/seed.py', '/workspace/stolen')",
            "fifo": "os.mkfifo('/workspace/stolen')",
            "protected_write": "with pytest.raises(OSError): Path('/opt/safeharness/acceptance_probe.py').write_text('tampered')",
            "network": "s=socket.socket(); s.settimeout(.3)\n    with pytest.raises(OSError): s.connect(('198.51.100.1',80))\n    s.close()",
            "host_environment": "assert os.environ.get('SH_HOST_SENTINEL') is None\n    assert not Path('/var/run/docker.sock').exists()",
        }
        monkeypatch.setenv("SH_HOST_SENTINEL", "synthetic-not-exported")
        files = {
            "seed.py": "seed",
            "tests/test_attack.py": "import os,socket,pytest\nfrom pathlib import Path\ndef test_boundary():\n    "
            + statements[name]
            + "\n",
        }
        workspace = CodingWorkspace(files, task.task_id)
        result = workspace.execute("run_tests", {"paths": ["tests/test_attack.py"]})
        assert result.code == (
            "unsafe_workspace_entry"
            if name in {"symlink", "hardlink", "fifo"}
            else "tests_passed"
        ), result
        assert workspace.last_container.cleanup_confirmed
        detail = {
            "code": result.code,
            "cleanup_confirmed": True,
            "image_id": workspace.image_id,
        }
    evidence(
        record_property,
        "attack",
        family,
        attack=name,
        attempted=True,
        breach_observed=False,
        task_id=str(task.task_id),
        detail=detail,
        trace=TraceLog(store.database, task.task_id).read(),
    )


FAULTS = [
    "before_intent",
    "after_intent",
    "after_business",
    "after_receipt",
    "concurrent_replay",
    "unapproved",
    "expired",
    "policy_changed",
    "cancelled",
    "evidence_missing",
]


@pytest.mark.parametrize(
    "backend,window",
    [(backend, window) for backend in ["sqlite", "postgres"] for window in FAULTS],
    ids=[
        f"{backend}-{window}" for backend in ["sqlite", "postgres"] for window in FAULTS
    ],
)
def test_fault_matrix(tmp_path, request, record_property, backend, window):
    if backend == "postgres":
        if os.getenv("SAFEHARNESS_POSTGRES_TESTS") != "1":
            pytest.skip("Enable PostgreSQL acceptance")
        store, business = request.getfixturevalue("pg_store")
    else:
        store = TaskStore(tmp_path / "fault.sqlite3")
        store.initialize()
        business = TicketStore(store.database)
    task = store.transition(store.create(), TaskStatus.RUNNING)
    task, approval = store.pause_for_approval(
        task,
        ApprovalIntent(
            "update_ticket_status", 1, '{"status":"resolved","ticket_id":"IT-001"}'
        ),
    )
    if window != "unapproved":
        store.decide_approval(
            approval.approval_id,
            ApprovalOutcome.APPROVED,
            approval.intent.arguments_sha256,
            actor=ApprovalActor("trusted-reviewer", can_approve=True),
        )
    recoverable = window in FAULTS[:5]
    if window in {"expired", "policy_changed"}:
        with store._engine.begin() as connection:
            execute(
                connection,
                "UPDATE approval_decisions SET decided_at='2000-01-01T00:00:00+00:00'"
                if window == "expired"
                else "UPDATE pending_approvals SET policy_version='obsolete-policy'",
            )
    if window == "cancelled":
        store.cancel(task.task_id, business)
    if window in {
        "after_intent",
        "after_business",
        "after_receipt",
        "evidence_missing",
    }:
        store._claim_execution(task.task_id, business)
    if window == "after_business":
        # Abrupt process exit is also covered by test_execution_recovery/test_postgres.
        class SimulatedCrash(Exception):
            pass

        def crash():
            raise SimulatedCrash()

        with pytest.raises(SimulatedCrash):
            store.resume(task.task_id, business, after_business_commit=crash)
    if window == "after_receipt":
        store.resume(task.task_id, business)
    if window == "evidence_missing":
        # Model a remote identity/evidence outage without deleting any database.
        from unittest.mock import patch

        with patch.object(
            business, "receipt", side_effect=EvidenceUnavailable("test outage")
        ):
            result = TaskStore(store.database).resume(task.task_id, business)
        assert result.status is TaskStatus.NEEDS_REVIEW
    elif recoverable:

        def resume(_):
            return TaskStore(store.database).resume(task.task_id, business).status

        if window == "concurrent_replay":
            with ThreadPoolExecutor(max_workers=4) as pool:
                assert list(pool.map(resume, range(4))) == [TaskStatus.SUCCEEDED] * 4
        else:
            assert resume(0) is TaskStatus.SUCCEEDED
        assert resume(0) is TaskStatus.SUCCEEDED
        assert business.ticket() == ("resolved", 1)
    elif window != "evidence_missing":
        with pytest.raises(ExecutionConflictError):
            TaskStore(store.database).resume(task.task_id, business)
    if not recoverable:
        assert business.ticket()[1] == 0
    evidence(
        record_property,
        "fault",
        window,
        backend=backend,
        recoverable=recoverable,
        task_id=str(task.task_id),
        task_status=store.get(task.task_id).status.value,
        writes=business.ticket()[1],
        trace=TraceLog(store.database, task.task_id).read(),
    )
