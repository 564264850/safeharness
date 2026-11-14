"""Trusted control-plane contracts. Candidate code is never imported here."""

import csv
import io
import json
import random
from dataclasses import dataclass

from .invocations import digest

BOOK_CONTRACT_VERSION = "budgetbook-observations-v1"


@dataclass(frozen=True)
class ContractVerdict:
    passed: bool
    code: str
    case_count: int
    contract_sha256: str
    failures: tuple[str, ...] = ()


def book_contract():
    cases, expected = [], []

    def add(identifier, operations, values, probe="book"):
        cases.append({"id": identifier, "probe": probe, "operations": operations})
        expected.append(values)

    def op(name, **args):
        return {"op": name, "args": args}

    def value(v):
        return {"value": v}

    def row(i, amount, category="food", note="", day="2026-11-12"):
        return {
            "id": i,
            "amount": amount,
            "category": category,
            "note": note,
            "date": day,
        }

    empty = {"total": "0.00", "count": 0, "by_category": {}}
    add("empty", [op("entries"), op("summary")], [value([]), value(empty)])
    first = row(1, "12.30", note="lunch")
    second = row(2, "2.05", "travel", day="2026-10-01")
    add(
        "persist-filter",
        [
            op("add", amount="12.3", category=" food ", note="lunch", day="2026-11-12"),
            op("reload"),
            op("add", amount="2.05", category="travel", day="2026-10-01"),
            op("reload"),
            op("entries", month="2026-11", category="food"),
            op("summary"),
        ],
        [
            value(first),
            value(None),
            value(second),
            value(None),
            value([first]),
            value(
                {
                    "total": "14.35",
                    "count": 2,
                    "by_category": {"food": "12.30", "travel": "2.05"},
                }
            ),
        ],
    )
    for i, amount in enumerate(["0", "-1", "NaN", "Infinity", "1.001", "abc"]):
        add(
            f"money-{i}",
            [op("add", amount=amount, category="food", day="2026-11-12")],
            [{"error": "ValueError"}],
        )
    for i, day in enumerate(["2026-2-03", "2026-04-31", "20261112", "2026-00-10"]):
        add(
            f"date-{i}",
            [op("add", amount="1", category="food", day=day)],
            [{"error": "ValueError"}],
        )
    for i, month in enumerate(["2026-1", "2026-00", "no", "2026-11-12"]):
        add(f"month-{i}", [op("entries", month=month)], [{"error": "ValueError"}])
    add(
        "category",
        [op("add", amount="1", category=" ", day="2026-11-12")],
        [{"error": "ValueError"}],
    )
    for i, text in enumerate(["[]", "{}", '{"version":99,"entries":[]}', "{broken"]):
        add(
            f"corrupt-{i}",
            [op("corrupt", text=text), op("reload")],
            [value(None), {"error": "ValueError"}],
        )
    one = row(1, "1.00", "学习")
    add(
        "copy-isolation",
        [
            op("add", amount="1", category="学习", day="2026-11-12"),
            op("mutate_return", field="amount", value="999.00"),
            op("entries"),
            op("mutate_return", field="category", value="changed"),
            op("reload"),
            op("summary"),
        ],
        [
            value(one),
            value(None),
            value([one]),
            value(None),
            value(None),
            value({"total": "1.00", "count": 1, "by_category": {"学习": "1.00"}}),
        ],
    )
    a = row(1, "0.10", note="comma, newline\n中文", day="2024-02-29")
    b = row(2, "0.20")
    c = row(3, "0.20")
    add(
        "delete-csv",
        [
            op("add", amount="0.10", category="food", note=a["note"], day=a["date"]),
            op("add", amount="0.20", category="food", day="2026-11-12"),
            op("delete", entry_id=2),
            op("reload"),
            op("add", amount="0.20", category="food", day="2026-11-12"),
            op("export_csv"),
        ],
        [
            value(a),
            value(b),
            value(True),
            value(None),
            value(c),
            value(
                {
                    "count": 2,
                    "rows": [{k: str(v) for k, v in r.items()} for r in (a, c)],
                }
            ),
        ],
    )
    rng = random.Random(20261113)
    operations, values, sums = [], [], {"food": 0, "学习": 0, "travel": 0}
    for i in range(40):
        cents = rng.randint(1, 200000)
        category = list(sums)[i % 3]
        sums[category] += cents
        amount = f"{cents // 100}.{cents % 100:02}"
        operations.append(op("add", amount=amount, category=category, day="2026-11-12"))
        values.append(value(row(i + 1, amount, category)))
    total = sum(sums.values())
    operations.extend(
        [op("reload"), op("summary", month="2026-11"), op("summary", month="2026-10")]
    )
    values.extend(
        [
            value(None),
            value(
                {
                    "total": f"{total // 100}.{total % 100:02}",
                    "count": 40,
                    "by_category": {
                        k: f"{v // 100}.{v % 100:02}" for k, v in sums.items()
                    },
                }
            ),
            value(empty),
        ]
    )
    add("random-totals", operations, values)
    add(
        "http",
        [
            {"path": "/"},
            {
                "path": "/api/entries",
                "method": "POST",
                "body": {
                    "amount": "12.3",
                    "category": "food",
                    "note": "x,y",
                    "date": "2026-11-12",
                },
            },
            {"path": "/api/entries?month=2026-11&category=food"},
            {"path": "/api/summary?month=2026-11"},
            {"path": "/api/export"},
            {"path": "/api/entries/1", "method": "DELETE"},
            {"path": "/api/entries"},
            {
                "path": "/api/entries",
                "method": "POST",
                "body": {"amount": "NaN", "category": "food"},
            },
            {"path": "/../server.py"},
            {"op": "corrupt"},
            {"path": "/api/entries"},
        ],
        "http-v1",
        probe="http",
    )
    return cases, expected


def _http_matches(rows):
    if not isinstance(rows, list) or len(rows) != 11:
        return False
    if not all(isinstance(r, dict) for r in rows):
        return False
    for i, row in enumerate(rows):
        if i == 9:
            continue
        if (
            type(row.get("status")) is not int
            or not isinstance(row.get("content_type"), str)
            or not isinstance(row.get("disposition"), str)
        ):
            return False
    if (
        rows[0].get("status") != 200
        or "text/html" not in rows[0].get("content_type", "")
        or "<html" not in str(rows[0].get("body", "")).lower()
    ):
        return False
    entry = {
        "id": 1,
        "amount": "12.30",
        "category": "food",
        "note": "x,y",
        "date": "2026-11-12",
    }
    expected = [
        (201, entry),
        (200, [entry]),
        (200, {"total": "12.30", "count": 1, "by_category": {"food": "12.30"}}),
    ]
    for actual, (status, body) in zip(rows[1:4], expected):
        if (
            actual.get("status") != status
            or "application/json" not in actual.get("content_type", "")
            or digest(actual.get("body")) != digest(body)
        ):
            return False
    export = rows[4]
    if export.get("status") != 200 or "attachment" not in export.get("disposition", ""):
        return False
    if list(csv.DictReader(io.StringIO(export.get("body", "")))) != [
        {k: str(v) for k, v in entry.items()}
    ]:
        return False
    if (
        rows[5].get("status") != 200
        or digest(rows[5].get("body")) != digest({"deleted": True})
        or rows[6].get("status") != 200
        or rows[6].get("body") != []
    ):
        return False
    if rows[9] != {"value": None}:
        return False
    return all(
        rows[i].get("status") == status
        and isinstance(rows[i].get("body"), dict)
        and isinstance(rows[i]["body"].get("error"), str)
        and bool(rows[i]["body"]["error"])
        for i, status in ((7, 400), (8, 404), (10, 400))
    )


def evaluate_book(reply, cases, expected):
    sha = digest(
        {"version": BOOK_CONTRACT_VERSION, "cases": cases, "expected": expected}
    )

    def failed(code, failures=()):
        return ContractVerdict(False, code, len(cases), sha, tuple(failures))

    if (
        not cases
        or len(expected) != len(cases)
        or len({c["id"] for c in cases}) != len(cases)
    ):
        return failed("invalid_host_contract")
    if reply.status != "ok":
        return failed(reply.code)
    try:
        payload = json.loads(reply.value)
        envelope = payload["observations"]
        observations = envelope["observations"]
        if (
            type(payload["probe_exit_code"]) is not int
            or payload["probe_exit_code"] != 0
            or type(envelope["version"]) is not int
            or envelope["version"] != 1
            or not isinstance(observations, list)
            or len(observations) != len(cases)
        ):
            return failed("observation_incomplete")
        failures = []
        for case, want, actual in zip(cases, expected, observations):
            if (
                not isinstance(actual, dict)
                or set(actual) != {"id", "observed"}
                or actual["id"] != case["id"]
            ):
                return failed("observation_schema_error")
            matches = (
                _http_matches(actual["observed"])
                if want == "http-v1"
                else digest(actual["observed"]) == digest(want)
            )
            if not matches:
                failures.append(case["id"])
        return (
            failed("observation_contract_failed", failures)
            if failures
            else ContractVerdict(True, "observation_contract_passed", len(cases), sha)
        )
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, csv.Error):
        return failed("observation_schema_error")
