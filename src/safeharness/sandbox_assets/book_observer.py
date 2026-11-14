"""Untrusted I/O observations only. Expectations and verdicts stay on the host."""

import csv
import json
import runpy
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, "/workspace")


def observe(case):
    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory) / "book.json"
        if case.get("probe") == "http":
            return http(case["operations"], data)
        book_class = runpy.run_path("/workspace/solution.py")["Book"]
        book = book_class(data)
        results, last = [], None
        for operation in case["operations"]:
            try:
                op = operation["op"]
                args = operation.get("args", {})
                if op == "reload":
                    book = book_class(data)
                    value = None
                elif op == "corrupt":
                    data.write_text(args["text"], encoding="utf-8")
                    value = None
                elif op == "mutate_return":
                    target = last[0] if isinstance(last, list) else last
                    target[args["field"]] = args["value"]
                    value = None
                elif op == "export_csv":
                    path = Path(directory) / "out.csv"
                    count = book.export_csv(path)
                    with path.open(encoding="utf-8", newline="") as handle:
                        value = {"count": count, "rows": list(csv.DictReader(handle))}
                elif op in {"add", "entries", "summary", "delete"}:
                    value = getattr(book, op)(**args)
                else:
                    raise ValueError("Unknown observation operation")
                # Copy each observation before later mutations of returned objects.
                results.append(
                    {"value": json.loads(json.dumps(value, allow_nan=False))}
                )
                last = value
            except Exception as exc:  # noqa: BLE001 - transport exception types, never success flags.
                results.append({"error": type(exc).__name__})
        return results


def http(operations, data):
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "/workspace/server.py",
            "--data",
            str(data),
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
    )
    try:
        url = json.loads(process.stdout.readline())["url"]
        results = []
        for op in operations:
            if op.get("op") == "corrupt":
                data.write_text("{broken", encoding="utf-8")
                results.append({"value": None})
                continue
            body = op.get("body")
            request = urllib.request.Request(
                url + op["path"],
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"},
                method=op.get("method", "GET"),
            )
            try:
                response = urllib.request.urlopen(request, timeout=3)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                content = response.read(16385)
                if len(content) > 16384:
                    raise ValueError("Observation limit")
                kind = response.headers.get("Content-Type", "")
                results.append(
                    {
                        "status": response.status,
                        "content_type": kind,
                        "disposition": response.headers.get("Content-Disposition", ""),
                        "body": json.loads(content)
                        if "json" in kind
                        else content.decode("utf-8"),
                    }
                )
        return results
    finally:
        process.terminate()
        process.wait(timeout=3)


if __name__ == "__main__":
    request = json.loads(sys.stdin.read(65536))
    observed = [
        {"id": case["id"], "observed": observe(case)} for case in request["cases"]
    ]
    print(json.dumps({"version": 1, "observations": observed}, allow_nan=False))
