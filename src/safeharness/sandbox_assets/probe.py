"""Fixed diagnostic program, executed only inside the worker container."""

import json
import os
import socket
import tempfile
from pathlib import Path


def probe():
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
    )
    try:
        Path("/opt/safeharness/should-not-write").write_text("blocked")
        readonly = False
    except OSError:
        readonly = True
    with tempfile.TemporaryFile(dir="/workspace") as handle:
        handle.write(b"workspace")
        writable = True
    with socket.socket() as connection:
        connection.settimeout(0.3)
        try:
            connection.connect(("198.51.100.1", 80))
            external_network_blocked = False
        except OSError:
            external_network_blocked = True
    cgroup = Path("/sys/fs/cgroup")
    quota, period = (cgroup / "cpu.max").read_text().split()
    return {
        "non_root": os.getuid() == 65534 and os.getgid() == 65534,
        "no_new_privileges": status["NoNewPrivs"].strip() == "1",
        "capabilities_dropped": int(status["CapEff"].strip(), 16) == 0,
        "seccomp_enabled": status["Seccomp"].strip() == "2",
        "root_readonly": readonly,
        "workspace_writable": writable,
        "network_disabled": set(os.listdir("/sys/class/net")) == {"lo"}
        and external_network_blocked,
        "memory_limited": (cgroup / "memory.max").read_text().strip() == "268435456",
        "swap_disabled": (cgroup / "memory.swap.max").read_text().strip() == "0",
        "pids_limited": (cgroup / "pids.max").read_text().strip() == "64",
        "cpu_limited": quota != "max" and int(quota) == int(period),
        "docker_socket_absent": not Path("/var/run/docker.sock").exists(),
    }


if __name__ == "__main__":
    print(json.dumps(probe()))
