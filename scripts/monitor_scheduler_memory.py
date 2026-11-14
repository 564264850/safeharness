"""Windows companion: sample registered launcher AND interpreter descendants.

The venv python.exe can be a small launcher. Sampling only its PID misses the
interpreter. Enumerate process IDs/parents/working sets, retain only this run's tree.
Never read process command lines, environment variables or unrelated memory data.
"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def main(directory):
    if os.name != "nt":
        raise ValueError("This companion uses Windows CIM")
    directory = directory.resolve(strict=True)
    progress = directory / "progress.json"
    if not progress.is_file() or directory.parent.name != "scheduler-soak":
        raise ValueError("Provide a live scheduler-soak run directory")
    samples = []
    start = time.monotonic()
    path = directory / "process-memory.json"
    while time.monotonic() - start < 3700:
        if (directory / "report.json").exists():
            break
        roots = {
            int(pid) for pid in json.loads(progress.read_text())["working_set_bytes"]
        }
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,WorkingSetSize | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            check=True,
            timeout=20,
        )
        processes = json.loads(result.stdout.decode("utf-8-sig"))
        groups = {}
        for root in roots:
            selected = {root}
            while True:
                expanded = selected | {
                    p["ProcessId"]
                    for p in processes
                    if p["ParentProcessId"] in selected
                }
                if selected == expanded:
                    break
                selected = expanded
            group = [
                {
                    "pid": p["ProcessId"],
                    "parent_pid": p["ParentProcessId"],
                    "working_set_bytes": int(p["WorkingSetSize"]),
                }
                for p in processes
                if p["ProcessId"] in selected
            ]
            groups[str(root)] = {
                "processes": group,
                "working_set_sum_bytes": sum(p["working_set_bytes"] for p in group),
            }
        samples.append(
            {
                "unix_time": time.time(),
                "since_monitor_start_seconds": round(time.monotonic() - start, 3),
                "registered_process_trees": groups,
            }
        )
        value = {
            "measurement": "Windows working-set samples of registered launcher + interpreter process trees",
            "limits": [
                "Monitoring starts after the soak starts; not the full-hour memory history",
                "Working sets include shared pages; sum is not unique physical memory",
                "Observations only, no memory leak proof",
            ],
            "samples": samples,
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
        temporary.replace(path)
        print(
            json.dumps(
                {"memory_samples": len(samples), "registered_trees": len(groups)}
            ),
            flush=True,
        )
        for _ in range(6):
            if (directory / "report.json").exists():
                return 0
            time.sleep(10)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    raise SystemExit(main(parser.parse_args().directory))
