"""Synthetic development fixtures, ten function families and three bugs each.

Expected values are authored independently and never sent to candidate workers.
These are development instances, not a hidden generalization benchmark.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CodingFamily:
    name: str
    signature: str
    fixed_body: str
    bugs: tuple[str, str, str]
    cases: list[list]
    expected: list[dict]

    def source(self, body):
        return (
            f"def {self.name}({self.signature}):\n"
            + "\n".join("    " + line for line in body.splitlines())
            + "\n"
        )


def values(*items):
    return [{"value": item} for item in items]


FAMILIES = [
    CodingFamily(
        "clamp",
        "value, lower, upper",
        "if lower > upper: raise ValueError()\nreturn min(upper, max(lower, value))",
        (
            "return min(lower, max(value, upper))",
            "return value",
            "return min(upper, max(lower, value))",
        ),
        [[5, 0, 10], [-2, 0, 10], [12, 0, 10], [0, 2, 1]],
        values(5, 0, 10) + [{"error": "ValueError"}],
    ),
    CodingFamily(
        "safe_divide",
        "a, b",
        "return None if b == 0 else a / b",
        ("return a * b", "return None if b == 0 else a // b", "return a / b"),
        [[5, 2], [1, 0], [-3, 2]],
        values(2.5, None, -1.5),
    ),
    CodingFamily(
        "parse_bool",
        "value",
        "value = value.strip().lower()\nif value in ('true', 'yes', '1'): return True\nif value in ('false', 'no', '0'): return False\nraise ValueError()",
        ("return bool(value)", "return value == 'true'", "return False"),
        [[" YES "], ["false"], ["0"], ["invalid"]],
        values(True, False, False) + [{"error": "ValueError"}],
    ),
    CodingFamily(
        "unique_ordered",
        "items",
        "return list(dict.fromkeys(items))",
        ("return sorted(set(items))", "return items", "return list(reversed(items))"),
        [[[3, 1, 3, 2]], [[]], [[1, 1]]],
        values([3, 1, 2], [], [1]),
    ),
    CodingFamily(
        "chunks",
        "items, size",
        "if size <= 0: raise ValueError()\nreturn [items[i:i+size] for i in range(0, len(items), size)]",
        (
            "return [items]",
            "return [items[i:i+size] for i in range(1, len(items), size)]",
            "return [items[i:i+size] for i in range(0, len(items)-size+1, size)]",
        ),
        [[[1, 2, 3], 2], [[], 2], [[1], 0]],
        values([[1, 2], [3]], []) + [{"error": "ValueError"}],
    ),
    CodingFamily(
        "median",
        "items",
        "items = sorted(items)\nif not items: raise ValueError()\nn = len(items)\nreturn items[n//2] if n%2 else (items[n//2-1]+items[n//2])/2",
        (
            "return sorted(items)[len(items)//2]",
            "return items[len(items)//2]",
            "return 0",
        ),
        [[[9, 1, 2]], [[2, 4]], [[]]],
        values(2, 3) + [{"error": "ValueError"}],
    ),
    CodingFamily(
        "normalize_tags",
        "tags",
        "return sorted({tag.strip().lower() for tag in tags if tag.strip()})",
        ("return tags", "return sorted(set(tags))", "return []"),
        [[[" B ", "a", "A", ""]], [[]]],
        values(["a", "b"], []),
    ),
    CodingFamily(
        "merge_counts",
        "left, right",
        "result = dict(left)\nfor key, value in right.items(): result[key] = result.get(key, 0) + value\nreturn result",
        ("return {**left, **right}", "return left", "return {}"),
        [[{"a": 2}, {"a": 3, "b": 1}], [{}, {"x": 1}], [{}, {}]],
        values({"a": 5, "b": 1}, {"x": 1}, {}),
    ),
    CodingFamily(
        "retry_delay",
        "base, attempt, cap",
        "if min(base, attempt, cap) < 0: raise ValueError()\nreturn min(cap, base * 2**attempt)",
        ("return base * 2**attempt", "return min(cap, base * attempt)", "return 0"),
        [[2, 0, 10], [2, 4, 10], [-1, 2, 10]],
        values(2, 10) + [{"error": "ValueError"}],
    ),
    CodingFamily(
        "redact_tokens",
        "text",
        "import re\nreturn re.sub(r'sk-[A-Za-z0-9]{4,}', '[REDACTED]', text)",
        (
            "return text",
            "return text.lower()",
            "return text.replace('token', '[REDACTED]')",
        ),
        [["key=sk-FAKE1234 end"], ["hello WORLD"], ["sk-abcd sk-1234"]],
        values("key=[REDACTED] end", "hello WORLD", "[REDACTED] [REDACTED]"),
    ),
]
