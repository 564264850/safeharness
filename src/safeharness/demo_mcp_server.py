"""Synthetic MCP stdio fixture, not an external business integration."""

import json
import sys

from .document_app import DocumentArgs
from .mcp_readonly import PROTOCOL_VERSION


def main():
    initialized = False
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "notifications/initialized":
            initialized = True
            continue
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-kb", "version": "1"},
            }
        elif initialized and method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "read_document",
                        "description": "Synthetic document.",
                        "inputSchema": DocumentArgs.model_json_schema(),
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
        elif (
            initialized
            and method == "tools/call"
            and request["params"]["name"] == "read_document"
        ):
            args = DocumentArgs.model_validate(request["params"]["arguments"])
            found = args.document_id == "KB-001"
            document = {
                "document_id": args.document_id,
                "source": "synthetic://kb/KB-001",
                "text": "Inspect the deployment log before resolving the incident.",
            }
            result = {
                "isError": not found,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(document) if found else "Document not found",
                    }
                ],
            }
        else:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "error": {"code": -32601, "message": "Unsupported method"},
                    }
                ),
                flush=True,
            )
            continue
        print(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}),
            flush=True,
        )


if __name__ == "__main__":
    main()
