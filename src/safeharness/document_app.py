"""Example host registration: native ticket read plus controlled MCP document read."""

import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .applications import Application, ReadOnlyBinding
from .execution import ToolSpec
from .mcp_readonly import ReadOnlyMCP, StdioServer


class DocumentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    document_id: str = Field(pattern=r"^KB-[0-9]{3}$")


def document_application(*, server=None, allowed=frozenset({"KB-001"})):
    server = server or StdioServer(
        (sys.executable, "-m", "safeharness.demo_mcp_server"),
        str(Path.cwd().resolve()),
        "synthetic-kb-v1",
    )
    client = ReadOnlyMCP(server)
    spec = ToolSpec(
        "kb_read_document",
        1,
        DocumentArgs,
        "read",
        "extension",
        timeout_seconds=server.timeout_seconds,
        max_output_bytes=server.max_output_bytes,
    )

    def dispatch(args):
        return client.call(
            spec.name,
            "read_document",
            DocumentArgs.model_json_schema(),
            args.model_dump(),
        )

    def verify(run):
        read_ticket = any(
            r.name == "read_ticket" and r.status == "ok" and r.value == "open"
            for r in run.tool_results
        )
        docs = [r for r in run.tool_results if r.name == spec.name and r.status == "ok"]
        if not read_ticket or not docs:
            return False
        try:
            document = json.loads(docs[-1].value)
            return document == {
                "document_id": "KB-001",
                "source": "synthetic://kb/KB-001",
                "text": "Inspect the deployment log before resolving the incident.",
            }
        except (TypeError, ValueError):
            return False

    binding = ReadOnlyBinding(
        spec,
        "Read an authorized synthetic troubleshooting document.",
        lambda args: (args.document_id,),
        dispatch,
        allowed,
        "kb-read-v1",
        server.fingerprint,
    )
    return Application(
        "ticket-docs",
        "1",
        ("read_ticket",),
        (binding,),
        "ticket-docs-contract-v1",
        verify,
    )
