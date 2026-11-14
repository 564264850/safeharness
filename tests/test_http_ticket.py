import json
import socket
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from safeharness.approval import ApprovalActor, ApprovalOutcome
from safeharness.http_ticket import HttpTicketStore
from safeharness.service import Principal
from safeharness.task_store import TaskStore
from safeharness.ticket_store import EvidenceUnavailable, TicketStore
from safeharness.worker import QueueWorker


@pytest.mark.parametrize("receipt_mode", ["confirmed", "missing", "unavailable"])
def test_lost_http_response_is_reconciled_without_second_write(tmp_path, receipt_mode):
    local = TicketStore(tmp_path / "remote.sqlite3")
    workspace = local.initialize()
    counts = {"apply": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.headers.get("Authorization") != "Bearer synthetic-business":
                self.send_error(401)
                return
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/apply":
                counts["apply"] += 1
                local.apply(**data)
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if self.path == "/ticket":
                status, revision = local.ticket(**data)
                result = {"status": status, "revision": revision}
            else:
                if receipt_mode == "unavailable":
                    self.send_error(503)
                    return
                receipt = local.receipt(**data)
                result = (
                    asdict(receipt) if receipt and receipt_mode == "confirmed" else None
                )
            self.reply(result)

        def do_GET(self):
            self.reply({"workspace_id": workspace})

        def reply(self, result):
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        adapter = HttpTicketStore(
            f"http://127.0.0.1:{server.server_port}", "synthetic-business"
        )
        if receipt_mode != "confirmed":
            with pytest.raises(EvidenceUnavailable):
                adapter.apply(workspace, "operation", "a" * 64, "IT-001", "resolved")
            assert counts["apply"] == 1 and local.ticket() == ("resolved", 1)
            return
        receipt = adapter.apply(workspace, "operation", "a" * 64, "IT-001", "resolved")
        assert (
            receipt.revision == 1
            and counts["apply"] == 1
            and local.ticket() == ("resolved", 1)
        )
        control = TaskStore(tmp_path / "control.sqlite3")
        control.initialize()
        w = QueueWorker(
            control,
            adapter,
            [
                Principal(
                    "alice",
                    "unused",
                    frozenset({"requester"}),
                    writable_tickets=frozenset({"IT-001"}),
                )
            ],
        )
        task, _ = control.submit(
            "alice",
            {
                "scenario": "ticket_update_read",
                "execution_mode": "queued",
                "status": "in_progress",
            },
        )
        assert w.run_once()["status"] == "waiting_approval"
        request = control.get_approval(task.task_id).request
        control.decide_approval(
            request.approval_id,
            ApprovalOutcome.APPROVED,
            request.intent.arguments_sha256,
            actor=ApprovalActor("reviewer", can_approve=True),
        )
        assert w.run_once()["task_status"] == "SUCCEEDED"
        assert local.ticket() == ("in_progress", 2) and counts["apply"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://127.0.0.1",
        "http://user:password@localhost",
        "http://localhost/path",
    ],
)
def test_business_endpoint_is_not_model_controlled(url):
    with pytest.raises(ValueError):
        HttpTicketStore(url, "unused")
