"""Untrusted candidate runs here. Expected answers stay in the control plane."""

import json
import runpy
import sys


def main():
    cases = json.loads(sys.stdin.read())
    candidate = runpy.run_path("/workspace/solution.py")["clamp"]
    results = []
    for case in cases:
        try:
            value = candidate(*case)
            results.append({"value": value})
        except Exception as exc:  # noqa: BLE001 - report any candidate exception across this boundary
            results.append({"error": type(exc).__name__})
    print(json.dumps(results, allow_nan=False))


if __name__ == "__main__":
    main()
