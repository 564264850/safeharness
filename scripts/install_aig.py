"""Fetch only pinned, Git-blob-verified upstream files and install isolated deps."""

import hashlib
import json
import shutil
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = json.loads((ROOT / "deploy/aig-source-manifest.json").read_text())
    source = ROOT / ".tools/aig-source"
    source.mkdir(parents=True, exist_ok=True)

    def fetch(entry):
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or entry["type"] != "blob":
            raise ValueError("Unsafe source manifest")
        path = source.joinpath(*relative.parts)
        data = path.read_bytes() if path.exists() else None

        def valid(blob):
            return (
                blob is not None
                and hashlib.sha1(
                    b"blob " + str(len(blob)).encode() + b"\0" + blob
                ).hexdigest()
                == entry["sha"]
            )

        if not valid(data):
            url = f"https://raw.githubusercontent.com/{manifest['repository']}/{manifest['commit']}/{relative.as_posix()}"
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read(2_000_001)
            if not valid(data):
                raise ValueError("Upstream blob hash mismatch: " + relative.as_posix())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        count = sum(pool.map(fetch, manifest["files"]))
    uv = ROOT / ".tools/uv.exe"
    executable = str(uv) if uv.exists() else shutil.which("uv")
    if not executable:
        raise RuntimeError("Install uv 0.13.7 before installing the scanner")
    environment = ROOT / ".tools/aig-venv"
    python = environment / (
        "Scripts/python.exe" if __import__("os").name == "nt" else "bin/python"
    )
    if not python.exists():
        subprocess.run(
            [executable, "venv", "--python", "3.11", str(environment)], check=True
        )
    subprocess.run(
        [
            executable,
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "-r",
            str(ROOT / "deploy/aig-requirements.txt"),
            "--default-index",
            "https://pypi.org/simple",
        ],
        check=True,
    )
    subprocess.run(
        [
            executable,
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "--editable",
            str(source / "agent-scan"),
            "--default-index",
            "https://pypi.org/simple",
        ],
        check=True,
    )
    print(
        json.dumps(
            {"verified_files": count, "commit": manifest["commit"], "status": "ready"}
        )
    )


if __name__ == "__main__":
    main()
