"""Bounded byte capture for trusted CLI clients. Never invokes a shell."""

import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_limited: bool = False
    cancelled: bool = False


def run_bounded(
    argv: list[str],
    *,
    timeout: float = 15,
    output_limit: int = 131072,
    input_bytes: bytes | None = None,
    cancel_check=None,
) -> CommandResult:
    if timeout <= 0 or output_limit <= 0:
        raise ValueError("Process budgets must be positive")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "CREATE_NO_WINDOW")
        else 0,
    )
    buffers = [bytearray(), bytearray()]
    lock = threading.Lock()
    overflow = threading.Event()
    used = 0

    def drain(stream, index):
        nonlocal used
        with stream:
            while chunk := stream.read1(4096):
                with lock:
                    remaining = max(0, output_limit - used)
                    buffers[index].extend(chunk[:remaining])
                    used += min(remaining, len(chunk))
                    if len(chunk) > remaining:
                        overflow.set()

    def send():
        try:
            with process.stdin:
                process.stdin.write(input_bytes)
        except (BrokenPipeError, OSError):
            pass

    readers = [
        threading.Thread(target=drain, args=(stream, index), daemon=True)
        for index, stream in enumerate((process.stdout, process.stderr))
    ]
    for thread in readers:
        thread.start()
    if input_bytes is not None:
        threading.Thread(target=send, daemon=True).start()
    deadline = time.monotonic() + timeout
    timed_out = False
    cancelled = False
    try:
        while process.poll() is None:
            timed_out = time.monotonic() >= deadline
            cancelled = bool(cancel_check and cancel_check())
            if timed_out or overflow.is_set() or cancelled:
                process.kill()
                break
            time.sleep(0.01)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process.poll() is None:
            process.kill()
        for thread in readers:
            thread.join(timeout=1)
    return CommandResult(
        process.returncode if process.returncode is not None else -9,
        buffers[0].decode("utf-8", errors="replace"),
        buffers[1].decode("utf-8", errors="replace"),
        timed_out,
        overflow.is_set(),
        cancelled,
    )
