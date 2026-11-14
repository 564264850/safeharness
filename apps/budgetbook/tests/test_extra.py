import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from solution import Book

ROOT = Path(__file__).resolve().parents[1]


def test_empty_book_and_defaults(tmp_path):
    book = Book(tmp_path / "book.json")
    assert book.summary() == {"total": "0.00", "count": 0, "by_category": {}}
    assert book.entries() == []
    row = book.add("1", " 交通 ")
    assert row["category"] == "交通"
    assert row["note"] == ""
    assert len(row["date"]) == 10
    assert book.delete("abc") is False


def test_decimal_precision_and_filters(tmp_path):
    book = Book(tmp_path / "book.json")
    book.add("0.10", "a", day="2026-01-01")
    book.add("0.20", "a", day="2026-01-31")
    book.add("1.00", "b", day="2026-02-01")
    assert book.summary(month="2026-01")["total"] == "0.30"
    assert book.summary(month="2026-02")["by_category"] == {"b": "1.00"}
    assert [r["id"] for r in book.entries(category="a")] == [1, 2]
    with pytest.raises(ValueError):
        book.entries(month="2026-1")
    with pytest.raises(ValueError):
        book.add("1.5", "a", day="2026-1-01")


def test_incompatible_structure(tmp_path):
    path = tmp_path / "book.json"
    path.write_text(
        json.dumps({"version": 99, "next_id": 1, "entries": []}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        Book(path).entries()
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(ValueError):
        Book(path).entries()


def test_csv_header_and_quoting(tmp_path):
    book = Book(tmp_path / "book.json")
    book.add("3.00", "food", 'say "hi", ok', "2026-03-03")
    out = tmp_path / "x.csv"
    assert book.export_csv(out) == 1
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "id,amount,category,note,date"
    assert '"say ""hi"", ok"' in text


@pytest.fixture()
def server(tmp_path):
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(ROOT / "server.py"),
            "--data",
            str(tmp_path / "b.json"),
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    url = json.loads(process.stdout.readline())["url"]
    yield url
    process.terminate()
    process.wait(timeout=4)


def call(url, path, body=None, method=None):
    req = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    return urllib.request.urlopen(req, timeout=4)


def test_http_errors_and_export(server):
    with call(
        server, "/api/entries", {"amount": "2.5", "category": "food", "note": "x"}
    ) as res:
        assert res.status == 201
    with call(server, "/api/export") as res:
        assert "text/csv" in res.headers["Content-Type"]
        assert "attachment" in res.headers["Content-Disposition"]
        assert "food" in res.read().decode("utf-8")
    for path, body in [
        ("/api/entries", {"amount": "1", "category": " "}),
        ("/api/entries", {"amount": "1", "category": "a", "date": "2026-02-30"}),
    ]:
        with pytest.raises(urllib.error.HTTPError) as err:
            call(server, path, body)
        assert err.value.code == 400
        assert "error" in json.load(err.value)
    with pytest.raises(urllib.error.HTTPError) as err:
        call(server, "/api/nope")
    assert err.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as err:
        call(server, "/api/entries?month=2026-13")
    assert err.value.code == 400
    with call(server, "/api/entries/999", method="DELETE") as res:
        assert json.load(res) == {"deleted": False}


def test_corrupt_storage_returns_400(server, tmp_path):
    data = tmp_path / "b.json"
    data.write_text("{oops", encoding="utf-8")
    with pytest.raises(urllib.error.HTTPError) as err:
        call(server, "/api/summary")
    assert err.value.code == 400
    assert data.read_text(encoding="utf-8") == "{oops"
