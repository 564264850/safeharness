"""Host-owned application registration; models cannot register tools or grants."""

from collections.abc import Callable
from dataclasses import dataclass

from .execution import _TOOL_SPECS, ToolSpec, public_tool_schemas
from .invocations import digest


@dataclass(frozen=True)
class ReadOnlyBinding:
    spec: ToolSpec
    description: str
    resources: Callable
    dispatch: Callable
    allowed_resources: frozenset[str]
    policy_version: str
    adapter_configuration: str

    def __post_init__(self):
        if self.spec.access != "read" or self.spec.target != "extension":
            raise ValueError("Extensions currently support read-only tools")
        if self.spec.name.startswith("_") or self.spec.name in _TOOL_SPECS:
            raise ValueError("Extension cannot replace a native/control tool")
        if not self.policy_version or not self.adapter_configuration:
            raise ValueError("Versioned adapter configuration is required")


@dataclass(frozen=True)
class Application:
    app_id: str
    version: str
    native_tools: tuple[str, ...]
    bindings: tuple[ReadOnlyBinding, ...]
    verifier_version: str
    verify: Callable

    def __post_init__(self):
        names = [*self.native_tools, *(b.spec.name for b in self.bindings)]
        if not self.app_id or not self.version or not self.verifier_version:
            raise ValueError("Application identity and verifier version are required")
        if len(names) != len(set(names)) or any(
            n not in _TOOL_SPECS or n.startswith("_") for n in self.native_tools
        ):
            raise ValueError("Duplicate or unknown native tool")

    @property
    def configuration(self):
        return {
            "app_id": self.app_id,
            "version": self.version,
            "verifier": self.verifier_version,
            "native_tools": list(self.native_tools),
            "native_contracts": [
                {
                    "name": name,
                    "version": _TOOL_SPECS[name].version,
                    "schema": _TOOL_SPECS[name].input_schema.model_json_schema(),
                    "access": _TOOL_SPECS[name].access,
                    "target": _TOOL_SPECS[name].target,
                }
                for name in self.native_tools
            ],
            "extensions": [
                {
                    "name": b.spec.name,
                    "version": b.spec.version,
                    "schema": b.spec.input_schema.model_json_schema(),
                    "policy": b.policy_version,
                    "scope": sorted(b.allowed_resources),
                    "adapter": b.adapter_configuration,
                    "timeout": b.spec.timeout_seconds,
                    "output_limit": b.spec.max_output_bytes,
                }
                for b in self.bindings
            ],
        }

    @property
    def fingerprint(self):
        return digest(self.configuration)

    def tool_schemas(self):
        native = public_tool_schemas("ticket") + public_tool_schemas("coding")
        return [s for s in native if s["function"]["name"] in self.native_tools] + [
            {
                "type": "function",
                "function": {
                    "name": b.spec.name,
                    "description": b.description,
                    "parameters": b.spec.input_schema.model_json_schema(),
                },
            }
            for b in self.bindings
        ]


class ApplicationRegistry:
    def __init__(self):
        self._applications = {}

    def register(self, application: Application):
        if application.app_id in self._applications:
            raise ValueError("Application is already registered")
        self._applications[application.app_id] = application

    def get(self, app_id: str) -> Application:
        try:
            return self._applications[app_id]
        except KeyError:
            raise ValueError("Unknown application") from None
