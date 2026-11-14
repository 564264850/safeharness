"""Controller-owned functional checks, never included in the model's workspace."""

import csv
import json
import random
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from solution import Book


def test_random_totals_and_reload(tmp_path):
    path = tmp_path / "ledger.json"
    rng = random.Random(20261112)
    expected = {"food": 0, "学习": 0, "travel": 0}
    for index in range(40):
        cents = rng.randint(1, 200000)
        category = list(expected)[index % 3]
        expected[category] += cents
        Book(path).add(f"{cents // 100}.{cents % 100:02}", category, day="2026-11-12")
    report = Book(path).summary(month="2026-11")
    total = sum(expected.values())
    assert report == {
        "total": f"{total // 100}.{total % 100:02}",
        "count": 40,
        "by_category": {k: f"{v // 100}.{v % 100:02}" for k, v in expected.items()},
    }
    assert Book(path).summary(month="2026-10") == {
        "total": "0.00",
        "count": 0,
        "by_category": {},
    }


@pytest.mark.parametrize("day", ["2026-2-03", "2026-04-31", "20261112", "2026-00-10"])
def test_strict_calendar(tmp_path, day):
    with pytest.raises(ValueError):
        Book(tmp_path / "book.json").add("1", "food", day=day)


@pytest.mark.parametrize("month", ["2026-1", "2026-00", "no", "2026-11-12"])
def test_strict_month(tmp_path, month):
    with pytest.raises(ValueError):
        Book(tmp_path / "book.json").entries(month=month)


@pytest.mark.parametrize("contents", ["[]", "{}", '{"version":99,"entries":[]}'])
def test_incompatible_storage_preserved(tmp_path, contents):
    path = tmp_path / "book.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        Book(path).add("2.50", "food")
    assert path.read_text(encoding="utf-8") == contents


def test_empty_and_returned_data_isolation(tmp_path):
    path = tmp_path / "book.json"
    book = Book(path)
    assert book.entries() == []
    assert book.summary() == {"total": "0.00", "count": 0, "by_category": {}}
    row = book.add("1", "学习")
    row["amount"] = "999.00"
    rows = book.entries()
    rows[0]["category"] = "changed"
    assert Book(path).summary() == {
        "total": "1.00",
        "count": 1,
        "by_category": {"学习": "1.00"},
    }


def test_web_filter_export_and_error(tmp_path):
    server = Path(__file__).resolve().parents[1] / "server.py"
    data = tmp_path / "book.json"
    book = Book(data)
    row = book.add("10", "book", "a,b", "2026-10-01")
    book.add("2.50", "food", day="2026-11-01")
    process = subprocess.Popen(
        [sys.executable, "-u", str(server), "--data", str(data), "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        url = json.loads(process.stdout.readline())["url"]

        def get(path):
            return urllib.request.urlopen(url + path, timeout=4)

        assert json.load(get("/api/entries?month=2026-10&category=book")) == [row]
        assert json.load(get("/api/summary?month=2026-11"))["total"] == "2.50"
        with get("/api/export") as response:
            assert "attachment" in response.headers["Content-Disposition"]
            rows = list(csv.DictReader(response.read().decode().splitlines()))
            assert rows[0]["note"] == "a,b" and len(rows) == 2
        with pytest.raises(urllib.error.HTTPError) as error:
            get("/../server.py")
        assert error.value.code == 404
        data.write_text("{broken", encoding="utf-8")
        with pytest.raises(urllib.error.HTTPError) as error:
            get("/api/entries")
        assert error.value.code == 400
        assert "error" in json.load(error.value)
    finally:
        process.terminate()
        process.wait(timeout=4)
