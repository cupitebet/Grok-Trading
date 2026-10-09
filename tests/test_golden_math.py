import math

import pytest

from gse.golden import spearman
from gse.market import parse_fear_greed, parse_kline


def test_spearman_perfect_and_inverse():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_ties_use_average_rank():
    # rank x = [1, 2.5, 2.5, 4], rank y = [1, 3, 2, 4] -> 4.5 / sqrt(4.5 * 5)
    assert spearman([1, 2, 2, 3], [1, 3, 2, 4]) == pytest.approx(4.5 / math.sqrt(22.5))


def test_spearman_constant_or_short_is_nan():
    assert math.isnan(spearman([1, 1, 1], [1, 2, 3]))
    assert math.isnan(spearman([1], [1]))


def test_parse_kline():
    k = [1700000000000, "1.5", "2", "1", "1.8", "100", 1700086399999, "0", 1, "0", "0", "0"]
    b = parse_kline(k)
    assert str(b["close"]) == "1.8" and b["open_time"].year == 2023 and b["close_time"] > b["open_time"]


def test_parse_fear_greed():
    rows = parse_fear_greed({"data": [{"value": "40", "value_classification": "Fear", "timestamp": "1551157200"}]})
    assert rows == [(rows[0][0], 40, "Fear")] and str(rows[0][0]) == "2019-02-26"
