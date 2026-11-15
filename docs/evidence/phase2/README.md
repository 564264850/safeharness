# Phase 2: real DeepSeek queued approval

[Portable report](real-deepseek-approval.json) records a real paid DeepSeek run on 2026-11-15. The HTTP API and queue workers run in independent local processes. The ticket store and requester/reviewer identities are synthetic; the script acts as the reviewer. No external business system is modified.

The task waits with zero writes, the first worker stops, a reviewer approves over HTTP, and a new worker resumes the task automatically. The task succeeds after one verified write and a tool readback. Self approval returns 403; duplicate approval returns 409. No manual run/resume endpoint is called. Four real model requests report 2919 input and 278 output tokens, zero unknown usage, and estimated upper cost CNY 0.011537.

The report contains invocation metadata, the approved arguments digest, the business receipt, two queue generations, a source fingerprint, and the SHA-256 of the original local report. Credentials, raw model messages, local auth files and binary databases are excluded. The source fingerprint covers sorted `src/**/*.py` and the acceptance script (relative path followed by raw bytes); it describes the execution worktree and is not a Git commit hash.

Reproduce from the project root with `python scripts/phase2_deepseek_acceptance.py --run-paid --credentials ../pico-api.txt`. This makes paid requests and writes a new report under ignored `artifacts/phase2-acceptance/`.

Fault injection uses synthetic transports in `tests/test_queued_deepseek.py`; this real run validates the approved integration path. It does not measure large-sample reliability or certify production readiness.
