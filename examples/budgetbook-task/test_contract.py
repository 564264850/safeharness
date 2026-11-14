import csv
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from solution import Book


def test_persistence_and_filter(tmp_path):
    path = tmp_path / "nested" / "book.json"
    book = Book(path)
    first = book.add("12.3", " food ", "lunch", "2026-11-12")
    assert first == {
        "id": 1,
        "amount": "12.30",
        "category": "food",
        "note": "lunch",
        "date": "2026-11-12",
    }
    Book(path).add("2.05", "travel", day="2026-10-01")
    assert Book(path).entries(month="2026-11", category="food") == [first]
    assert Book(path).summary() == {
        "total": "14.35",
        "count": 2,
        "by_category": {"food": "12.30", "travel": "2.05"},
    }


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity", "1.001", "abc"])
def test_invalid_amount(tmp_path, amount):
    with pytest.raises(ValueError):
        Book(tmp_path / "book.json").add(amount, "food")


def test_delete_no_id_reuse_and_csv(tmp_path):
    path = tmp_path / "book.json"
    book = Book(path)
    assert book.delete(99) is False
    book.add("0.10", "food", "comma, newline\n中文", "2024-02-29")
    second = book.add("0.20", "food")
    assert book.delete(second["id"]) is True
    assert Book(path).add("0.20", "food")["id"] > second["id"]
    assert Book(path).summary()["total"] == "0.30"
    output = tmp_path / "out.csv"
    assert Book(path).export_csv(output) == 2
    with output.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["note"] == "comma, newline\n中文"


def test_invalid_date_category_and_corruption(tmp_path):
    path = tmp_path / "book.json"
    book = Book(path)
    with pytest.raises(ValueError):
        book.add("1", " ")
    with pytest.raises(ValueError):
        book.add("1", "food", day="2026-02-29")
    with pytest.raises(ValueError):
        book.entries(month="2026-13")
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        Book(path).entries()
    assert path.read_text(encoding="utf-8") == "{broken"


def test_web_api(tmp_path):
    server = Path(__file__).resolve().parents[1] / "server.py"
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(server),
            "--data",
            str(tmp_path / "book.json"),
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        url = json.loads(process.stdout.readline())["url"]

        def request(path, body=None, method=None):
            req = urllib.request.Request(
                url + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"},
                method=method,
            )
            return urllib.request.urlopen(req, timeout=4)

        with request("/") as response:
            assert "text/html" in response.headers["Content-Type"]
            assert "<html" in response.read().decode().lower()
        with request(
            "/api/entries", {"amount": "9.9", "category": "food", "date": "2026-11-12"}
        ) as response:
            assert response.status == 201
            row = json.load(response)
            assert row["amount"] == "9.90"
        assert json.load(request("/api/summary"))["total"] == "9.90"
        assert len(json.load(request("/api/entries?month=2026-11"))) == 1
        assert json.load(request(f"/api/entries/{row['id']}", method="DELETE")) == {
            "deleted": True
        }
        with pytest.raises(urllib.error.HTTPError) as error:
            request("/api/entries", {"amount": "oops", "category": "food"})
        assert error.value.code == 400
    finally:
        process.terminate()
        process.wait(timeout=4)
