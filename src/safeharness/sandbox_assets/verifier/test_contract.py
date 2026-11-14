"""Fixed smoke contract outside the writable workspace."""

import pytest
from solution import clamp


@pytest.mark.parametrize(
    "value,lower,upper,expected",
    [(5, 0, 10, 5), (-3, 0, 10, 0), (20, 0, 10, 10), (-9, -10, -1, -9), (2, 2, 2, 2)],
)
def test_clamp(value, lower, upper, expected):
    assert clamp(value, lower, upper) == expected


def test_reversed_bounds():
    with pytest.raises(ValueError):
        clamp(1, 10, 0)
