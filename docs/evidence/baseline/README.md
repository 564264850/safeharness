# Curated baseline evidence

The files in this directory are sanitized, selected reports copied from the original local `artifacts/` tree. They make key demonstrations reviewable after cloning; full local artifacts remain ignored and were not rewritten. The scheduler test counts and one-hour soak summary are derived indexes, not substitutes for the original raw logs.

| File | Source run | What it supports |
| --- | --- | --- |
| [queued-approval.json](queued-approval.json) | `4ada8a49-bb3d-4c91-ab4b-7007ded949d5` | API restart, human approval, automatic queue resumption, one business write |
| [recovery-three-scenarios.json](recovery-three-scenarios.json) | `6959009d-906c-48ee-b820-1c98a53a6289` | Safe, uncertain, and stale-worker recovery outcomes |
| [consumer-fencing.json](consumer-fencing.json) | `81a8db98-4915-4bc9-ac24-bf738e464009` | PostgreSQL lease expiry and same-process recovery across two tasks |
| [scheduler-soak-summary.json](scheduler-soak-summary.json) | `665b6912-5c18-4695-864e-1c00000c6c2c` | One-hour soak summary and source raw-report digest |
| [validation-summary.json](validation-summary.json) | S5–S7 baseline, 2026-11-14 | Recorded regression, acceptance, repeated-fault and package validation counts |

Every run predates Git initialization; there is no original source commit ID to associate with them. The recorded implementation fingerprint is `15d924e6fd221316219f8fd51cc04a702a0db06c2e0ce7808c5c4ea2da553567`, computed before Git's LF normalization. The current repository archive is a newly created snapshot; the fingerprint is historical provenance, not a checksum of the normalized Git tree. These checks were not rerun merely by copying the reports.
The staged source tree was exported with `git archive`, installed into a new locked Python 3.11 environment, and ran the documented queued-approval quickstart. The fresh checkout contained 213 tracked files and did not copy the original `.venv`, `.tools`, or `artifacts` directories. Result: task `SUCCEEDED`, one verified mock business write, zero manual resume calls, and zero paid API calls. A derived report with the Git tree and source-run digest is `clean-install-quickstart.json`.
