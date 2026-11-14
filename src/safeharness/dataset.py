"""Versioned benchmark contracts, kept outside all model-writable workspaces."""

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScriptAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, object]


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    category: Literal["normal", "attack", "control"]
    environment: Literal["ticket", "coding", "control"]
    instruction: str = Field(min_length=1, max_length=3000)
    fixture: Literal[
        "ticket-open-v1",
        "ticket-progress-v1",
        "clamp-v1",
        "readonly-attack-v1",
        "control-v1",
    ]
    contract: Literal[
        "read_ticket",
        "request_approval",
        "fix_clamp",
        "read_source",
        "deny_ticket_read",
        "deny_ticket_write",
        "deny_approval_forge",
        "deny_path_escape",
        "deny_indirect_write",
        "approval_reject",
        "approval_execute",
        "cancel",
        "commit_crash",
        "missing_evidence",
        "invalid_schema",
    ]
    script: list[ScriptAction] = Field(max_length=8)
    readable_tickets: list[str] = Field(default_factory=list)
    writable_tickets: list[str] = Field(default_factory=list)
    readable_files: list[str] = Field(default_factory=list)
    writable_files: list[str] = Field(default_factory=list)
    test_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_boundary(self):
        if (self.category == "control") != (self.environment == "control"):
            raise ValueError("Control-plane tests must be explicitly separated")
        if self.environment == "control" and (
            self.script or self.fixture != "control-v1"
        ):
            raise ValueError("Control tests use a trusted driver")
        if self.environment == "coding":
            from .sandbox_assets.contract import validate_path

            for path in [*self.readable_files, *self.writable_files, *self.test_paths]:
                validate_path(path)
        if any(
            ticket != "IT-001"
            for ticket in [*self.readable_tickets, *self.writable_tickets]
        ):
            raise ValueError("This dataset authorizes only the synthetic IT-001")
        return self


class EvalDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    dataset_version: Literal["m3-v1"]
    split: Literal["development"]
    description: str
    cases: list[EvalCase] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Dataset case identifiers must be unique")
        return self


DEFAULT_DATASET = Path(__file__).with_name("datasets") / "m3-v1.json"


def load_dataset(path: Path | None = None) -> tuple[EvalDataset, str]:
    data = (path or DEFAULT_DATASET).read_bytes()
    if len(data) > 131072:
        raise ValueError("Dataset exceeds its size budget")
    return EvalDataset.model_validate_json(data), hashlib.sha256(data).hexdigest()
