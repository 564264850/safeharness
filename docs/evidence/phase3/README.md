# Phase 3: native tool and controlled MCP

[Portable demo report](native-mcp-demo.json) records a single Harness executing native `read_ticket` and MCP-backed `kb_read_document`. The planner is scripted, the MCP stdio subprocess is real, and both data sources are synthetic. No paid API call is involved.

The application contract independently checks the ticket status, document ID, source and exact content. Both calls share the existing invocation/audit implementation. The report includes application configuration, policy/schema/scope fingerprints, call/attempt metadata and the SHA-256 of the original ignored local report. Host commands and credentials are excluded.

Reproduce with `python scripts/application_demo.py`; run `python -m pytest -q tests/test_applications_mcp.py` for authorization, configuration recovery and protocol fault cases. The MCP process is trusted local code, not a sandboxed arbitrary third-party server.
