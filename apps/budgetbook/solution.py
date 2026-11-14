"""BudgetBook 记账核心逻辑。"""

import csv
import datetime
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import storage

AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FIELDS = ["id", "amount", "category", "note", "date"]


def _amount(value):
    if not isinstance(value, str):
        raise ValueError("金额必须是十进制字符串")  # noqa: TRY004 - Public validation contract.
    text = value.strip()
    if not AMOUNT_RE.match(text):
        raise ValueError("金额必须为正数且最多两位小数")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("金额格式无效") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("金额必须为正数")
    return number.quantize(Decimal("0.01"))


def _day(value):
    if value is None:
        return datetime.date.today().isoformat()
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValueError("日期格式必须为 YYYY-MM-DD")
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("日期不存在") from exc


def _month(value):
    if value is None:
        return None
    if not isinstance(value, str) or not MONTH_RE.match(value):
        raise ValueError("月份格式必须为 YYYY-MM")
    return value


class Book:
    def __init__(self, path):
        self.path = Path(path)
        self._entries, self._next_id = storage.load(self.path)

    def _save(self):
        storage.save(self.path, self._entries, self._next_id)

    def add(self, amount, category, note="", day=None):
        value = _amount(amount)
        if not isinstance(category, str) or not category.strip():
            raise ValueError("分类不能为空")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise ValueError("备注必须是字符串")  # noqa: TRY004 - Public validation contract.
        entry = {
            "id": self._next_id,
            "amount": f"{value:.2f}",
            "category": category.strip(),
            "note": note,
            "date": _day(day),
        }
        self._entries.append(entry)
        self._next_id += 1
        self._save()
        return dict(entry)

    def entries(self, *, month=None, category=None):
        month = _month(month)
        result = []
        for item in self._entries:
            if month and not item["date"].startswith(month + "-"):
                continue
            if category is not None and item["category"] != category:
                continue
            result.append(dict(item))
        return sorted(result, key=lambda item: item["id"])

    def delete(self, entry_id):
        try:
            target = int(entry_id)
        except (TypeError, ValueError):
            return False
        for index, item in enumerate(self._entries):
            if item["id"] == target:
                del self._entries[index]
                self._save()
                return True
        return False

    def summary(self, *, month=None):
        rows = self.entries(month=month)
        total = Decimal("0.00")
        groups = {}
        for item in rows:
            value = Decimal(item["amount"])
            total += value
            groups[item["category"]] = (
                groups.get(item["category"], Decimal("0.00")) + value
            )
        return {
            "total": f"{total:.2f}",
            "count": len(rows),
            "by_category": {key: f"{value:.2f}" for key, value in groups.items()},
        }

    def export_csv(self, target):
        rows = self.entries()
        with open(target, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for item in rows:
                writer.writerow(item)
        return len(rows)
