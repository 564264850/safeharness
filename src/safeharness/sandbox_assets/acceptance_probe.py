"""Run arbitrary acceptance functions inside the worker; never receive answers."""

import contextlib
import json
import runpy
import sys


def main():
    request = json.loads(sys.stdin.read(32768))
    with contextlib.redirect_stdout(sys.stderr):
        candidate = runpy.run_path("/workspace/solution.py")[request["function"]]
        results = []
        for case in request["cases"]:
            try:
                results.append({"value": candidate(*case)})
            except Exception as exc:  # noqa: BLE001 - untrusted exception type only
                results.append({"error": type(exc).__name__})
    print(json.dumps(results, allow_nan=False))


if __name__ == "__main__":
    main()
