# Third party integrations

Based on Tencent Zhuque Lab AI-Infra-Guard.

Original repository: https://github.com/Tencent/AI-Infra-Guard

The optional external scanner is pinned to commit
`acddd07bfcab5c0c4a6eeefa487164ba34261c39` and runs in its own Python environment.
It is licensed under Apache-2.0; the downloaded source retains its LICENSE and
NOTICE. SafeHarness does not embed its source in the application wheel.

OpenJudge (`py-openjudge==0.2.4`) is an optional evaluation dependency. See
`docs/integrations.md` for upstream sources and the distinction between
deterministic contracts, model grading and external scanning.

The R3 Context and checkpoint design was researched against local Pico,
pi (earendil-works/pi, MIT, commit 926b2f8cd8a8ac966f331882557f1bdc98e200de),
DeepSeek Harness (MIT, commit e8f7b93a344964de3dca1ace9e72b6e4d6de87cb),
and Claude Code's public checkpointing documentation. The implementation is
independent; none of those frameworks' source is embedded in the wheel.
Pinned references and the adopted boundaries are in docs/harness-core.md.
