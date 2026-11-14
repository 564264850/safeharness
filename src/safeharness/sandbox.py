"""Trusted Docker control plane. Worker requests never choose Docker arguments."""

import hashlib
import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

from .process import CommandResult, run_bounded

ASSETS = Path(__file__).with_name("sandbox_assets")
WORKER_IMAGE = "safeharness-worker:m2"


def source_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(ASSETS.rglob("*")):
        if path.is_file() and (
            path.suffix == ".py"
            or path.name
            in {"Dockerfile", "requirements.txt", ".dockerignore", "pytest.ini"}
        ):
            digest.update(path.relative_to(ASSETS).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class SandboxCheck:
    name: str
    passed: bool
    code: str


@dataclass(frozen=True)
class Preflight:
    ready: bool
    checks: tuple[SandboxCheck, ...]
    image_id: str | None = None


@dataclass
class ContainerResult:
    code: str
    command: CommandResult | None = None
    container_name: str | None = None
    cleanup_confirmed: bool = False


@dataclass
class SmokeResult:
    status: str
    code: str
    preflight: Preflight
    probe: dict[str, bool] = field(default_factory=dict)
    cleanup_confirmed: bool = False


class DockerSandbox:
    def __init__(self):
        self._executable = shutil.which("docker")

    def _call(self, *args: str, **budgets) -> CommandResult:
        if self._executable is None:
            raise FileNotFoundError("Docker CLI is not installed")
        return run_bounded([self._executable, *args], **budgets)

    def preflight(self) -> Preflight:
        checks = []
        if self._executable is None:
            return Preflight(
                False, (SandboxCheck("docker", False, "docker_cli_missing"),)
            )
        try:
            info = self._call("info", "--format", "{{json .}}", timeout=10)
            if info.returncode or info.timed_out or info.output_limited:
                return Preflight(
                    False, (SandboxCheck("engine", False, "docker_engine_unavailable"),)
                )
            engine = json.loads(info.stdout)
            checks.append(
                SandboxCheck(
                    "engine", engine.get("OSType") == "linux", "linux_engine_required"
                )
            )
            supported = all(
                engine.get(key) is True
                for key in (
                    "MemoryLimit",
                    "SwapLimit",
                    "CpuCfsPeriod",
                    "CpuCfsQuota",
                    "PidsLimit",
                )
            )
            checks.append(
                SandboxCheck("budgets", supported, "resource_limits_required")
            )
            seccomp = any(
                option.startswith("name=seccomp")
                for option in engine.get("SecurityOptions", []) or []
            )
            checks.append(SandboxCheck("seccomp", seccomp, "seccomp_required"))
            image = self._call("image", "inspect", WORKER_IMAGE)
            if image.returncode:
                checks.append(SandboxCheck("image", False, "worker_image_missing"))
                return Preflight(False, tuple(checks))
            metadata = json.loads(image.stdout)[0]
            valid = (
                metadata.get("Os") == "linux"
                and not metadata["Config"].get("Volumes")
                and metadata["Config"]
                .get("Labels", {})
                .get("safeharness.source-sha256")
                == source_digest()
            )
            checks.append(
                SandboxCheck("image", valid, "worker_image_outdated_or_invalid")
            )
            return Preflight(
                all(check.passed for check in checks),
                tuple(checks),
                metadata["Id"] if valid else None,
            )
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return Preflight(
                False,
                (*checks, SandboxCheck("response", False, "invalid_docker_response")),
            )

    def build(self) -> CommandResult:
        return self._call(
            "build",
            "--tag",
            WORKER_IMAGE,
            "--build-arg",
            f"SOURCE_SHA256={source_digest()}",
            str(ASSETS),
            timeout=240,
            output_limit=524288,
        )

    def run_container(
        self,
        image_id: str,
        program: str,
        *,
        payload: bytes | None = None,
        timeout: float = 15,
        task_id: str = "preflight",
        output_limit: int = 131072,
        cancel_check=None,
    ) -> ContainerResult:
        if program not in {"probe.py", "worker.py"}:
            raise ValueError("Unknown trusted worker program")
        name = f"sh-m2-{uuid4().hex}"
        result = ContainerResult("container_create_failed", container_name=name)
        deadline = time.monotonic() + timeout

        def remaining():
            return max(0.01, deadline - time.monotonic())

        try:
            created = self._call(
                "create",
                "--name",
                name,
                "--label",
                "safeharness.managed=m2",
                "--label",
                f"safeharness.task={task_id}",
                "--pull=never",
                "--network=none",
                "--read-only",
                "--user=65534:65534",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges=true",
                "--cgroupns=private",
                "--cpus=1",
                "--memory=256m",
                "--memory-swap=256m",
                "--pids-limit=64",
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
                "--tmpfs=/workspace:rw,noexec,nosuid,nodev,size=16m,uid=65534,gid=65534,mode=0700",
                "--init",
                "--log-driver=none",
                "--no-healthcheck",
                "--ulimit=nofile=128:128",
                "--workdir=/workspace",
                "--entrypoint=python",
                "--interactive",
                image_id,
                "-I",
                "-B",
                f"/opt/safeharness/{program}",
                timeout=min(10, remaining()),
            )
            if created.returncode or created.timed_out or created.output_limited:
                return result
            inspected = self._call("inspect", name, timeout=min(10, remaining()))
            if inspected.returncode or not self._isolated(
                json.loads(inspected.stdout)[0]
            ):
                result.code = "container_policy_mismatch"
                return result
            if time.monotonic() >= deadline:
                result.code = "execution_timeout"
                return result
            if cancel_check and cancel_check():
                result.code = "execution_cancelled"
                return result
            result.command = self._call(
                "start",
                "--attach",
                "--interactive",
                name,
                input_bytes=payload,
                timeout=remaining(),
                output_limit=min(output_limit, 131072),
                cancel_check=cancel_check,
            )
            if result.command.cancelled:
                result.code = "execution_cancelled"
            elif result.command.timed_out:
                result.code = "execution_timeout"
            elif result.command.output_limited:
                result.code = "output_limit_exceeded"
            elif result.command.returncode:
                result.code = "worker_failed"
            else:
                result.code = "completed"
            return result
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            result.code = "docker_execution_failed"
            return result
        finally:
            # Also runs when create/start times out or the caller is interrupted.
            for _ in range(2):
                try:
                    self._call("rm", "--force", name, timeout=10)
                    remaining = self._call(
                        "ps",
                        "--all",
                        "--quiet",
                        "--filter",
                        f"name=^/{name}$",
                        timeout=10,
                    )
                    result.cleanup_confirmed = (
                        not remaining.returncode
                        and not remaining.timed_out
                        and not remaining.output_limited
                        and not remaining.stdout.strip()
                    )
                except OSError:
                    result.cleanup_confirmed = False
                if result.cleanup_confirmed:
                    break
            if not result.cleanup_confirmed:
                result.code = "container_cleanup_failed"

    @staticmethod
    def _isolated(data: dict) -> bool:
        host = data["HostConfig"]
        return (
            data["Config"]["User"] == "65534:65534"
            and host["NetworkMode"] == "none"
            and host["ReadonlyRootfs"] is True
            and not host["Privileged"]
            and not host.get("Binds")
            and not data.get("Mounts")
            and host["CapDrop"] == ["ALL"]
            and not host.get("CapAdd")
            and "no-new-privileges=true" in host["SecurityOpt"]
            and host["Memory"] == 268435456
            and host["MemorySwap"] == 268435456
            and host["NanoCpus"] == 1000000000
            and host["PidsLimit"] == 64
            and set(host["Tmpfs"]) == {"/tmp", "/workspace"}
        )

    def smoke(self) -> SmokeResult:
        preflight = self.preflight()
        if not preflight.ready:
            return SmokeResult("denied", "sandbox_unavailable", preflight)
        result = self.run_container(preflight.image_id, "probe.py")
        probe = {}
        if result.code == "completed":
            try:
                probe = json.loads(result.command.stdout)
            except (ValueError, TypeError):
                pass
        passed = len(probe) == 12 and all(value is True for value in probe.values())
        return SmokeResult(
            "passed" if result.code == "completed" and passed else "failed",
            "isolation_verified"
            if result.code == "completed" and passed
            else result.code
            if result.code != "completed"
            else "isolation_probe_failed",
            preflight,
            probe,
            result.cleanup_confirmed,
        )


def report_dict(report) -> dict:
    return asdict(report)
