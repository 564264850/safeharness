# External review triage

Review input: `SafeHarness-项目评审汇报.md`, dated 2026-11-14. The source review lives beside the repository and is not part of this repository. This note separates statements that match current source/evidence from recommendations, stale observations, and claims that need a dedicated reproduction. It is not a security certification.

## Confirmed and reflected in the project plan

| Finding | Current check | Decision |
| --- | --- | --- |
| The project lacked Git history and a recruiter-friendly entry point. | Git was initialized as a new archive; README, documentation index, and the earlier command walkthrough were separated. No past milestones were fabricated. | Corrected in this Stage 1 archive. |
| Local test artifacts and credentials must not be published. | The source archive excludes `artifacts/`, local environments, provider credentials, databases, and private service config. A new publication guard scans Git index blobs and rejects private paths; tests prove it sees staged content even if the working file has since been cleaned. | Add the index scan to CI and keep the original local artifact tree. |
| CI needs a hosted result before the README can claim it passed. | GitHub Actions run [43618882633](https://github.com/564264850/safeharness/actions/runs/43618882633) completed successfully on commit `724fceb`; it ran the regression suite, fixed acceptance matrix, Docker worker checks, demos, and both secret scans. | The hosted gate now has a real passing result. Preserve the run link and do not generalize its synthetic acceptance data into production claims. |
| The user-facing evidence should make its scope easy to inspect. | Selected raw demo reports and derived validation summaries are checked in under `docs/evidence/baseline/`; each derived summary identifies source provenance and its limitations. | Keep raw local runs ignored; do not rewrite old reports to match the new Git snapshot. |
| There is no operator UI for all `NEEDS_REVIEW` cases and no arbitrary business adapter guarantee. | Current guides explicitly limit the queue to scripted ticket workflows and local mock receipts. | Real business adapter and safe operator resolution remain future work; not a reason to claim production readiness. |
| An authenticated publication identity is needed for the specified GitHub remote. | The target repository was an existing public empty repository; the user's GitHub account is authenticated and the initial archive is pushed. | The repository now has a real initial snapshot commit; the original source had no earlier Git history to import. |

## Current design limits, not newly discovered regressions

- `TaskStore.resume` deliberately holds the task lock over receipt lookup and the business operation so cancellation cannot race an in-flight write. This is visible in [`task_store.py`](../src/safeharness/task_store.py). It is a real scalability/latency trade-off for slow remote adapters. The existing durable HTTP demo uses a local mock and does not validate arbitrary provider latency. A redesign must retain intent, cancellation, fencing, and receipt invariants; moving I/O outside the lock by itself would be unsafe.
- The queue handles scripted ticket scenarios. Paid model calls and coding workspaces use explicit run/resume paths; queueing their recovery is not implemented.
- SQLite config sets a five-second connection/busy timeout in [`database.py`](../src/safeharness/database.py). WAL is not enabled as a universal default. The review's general SQLite-locking concern should be measured under a named concurrent workload before treating WAL as a correctness fix.
- The scanner uses explicit `hardened` or `vulnerable-lab` profiles, requires provider enablement, caps each in-process session at 16 target and 12 proxy requests, bounds input/output budgets, and preserves cumulative counts for that app instance. These are session limits, not counters intended to decrement after each request. They are not a substitute for durable cross-process quotas.
- DeepSeek request timeouts and output-token ceilings exist in [`provider.py`](../src/safeharness/provider.py). A timeout bounds the caller's wait, but cannot prove a provider stopped processing a request already sent; unknown usage and late completion require separate accounting/recovery work.
- The checked-in 120-instance acceptance run is a fixed development dataset, not a held-out generalization benchmark. The 3,600-second soak used a fixed scripted ticket workload without injected outages; it is not an SLA, capacity benchmark, or proof of leak-free operation.

## Claims to verify before turning them into defects

1. **Immediate permission revocation:** the new queued-worker regression removes the principal's writable ticket scope after approval and confirms the worker stops in `NEEDS_REVIEW` without a business write. This verifies a fresh worker configuration at resume time. It does not prove live config reload or millisecond revocation in an already-dispatched call; that is a separate requirement if live revocation is promised.
2. **Task lock duration:** code inspection confirms the lock spans the local business adapter call. It does not quantify production impact. Add a slow-adapter concurrency test and lock/queue latency measurements before selecting a transactional-outbox or lease-based redesign.
3. **Provider timeout and cost:** current tests can verify local caller timeouts and budget reservations; they cannot infer vendor-side cancellation or a final invoice when usage is missing. Do not state that a timed-out request is free or was cancelled remotely.
4. **Status-code handling:** investigate each exception path with a concrete request and expected recovery contract. Do not globally map all failures to one status if that would hide an unknown side effect.
5. **Database connection/locking behavior:** use a repeatable SQLite and PostgreSQL workload, capture wait duration and failure mode, and include database configuration before proposing WAL or a schema rewrite.

## Priority decisions for the internship portfolio

1. Keep the portable publication path green as changes land; the initial archive and hosted CI gate are now established.
2. Keep unsupported product promises out of the README; make one short demo easy to run without a paid API.
3. Add a second adapter behind a stable contract (MCP read-only is a suitable first step) and demonstrate that it still passes the same gateway policy and audit checks.
4. Only then add a minimal task/approval/evidence view if it helps show an end-to-end Agent workflow. A polished console is optional for a Runtime-focused resume and does not replace protocol tests.
5. Defer broad memory, multi-agent orchestration, a generic plugin marketplace, and speculative infrastructure until a target JD or measured failure justifies them.

The external review's numeric ratings and calendar plan are opinions. The current project's accepted scope is based on source, test reports, and the explicit limitations in this repository rather than those scores.
