"""inventory 模組的 pytest 測試：描述預期行為。"""
import pytest

from inventory import add_item, remove_item, total_value


def test_add_new_item_starts_from_zero():
    assert add_item({}, "蘋果", 3) == {"蘋果": 3}


def test_add_existing_item_accumulates_and_keeps_original():
    original = {"蘋果": 2}
    result = add_item(original, "蘋果", 5)
    assert result == {"蘋果": 7}
    assert original == {"蘋果": 2}


def test_add_zero_qty_raises():
    with pytest.raises(ValueError):
        add_item({}, "蘋果", 0)


def test_add_negative_qty_raises():
    with pytest.raises(ValueError):
        add_item({"蘋果": 1}, "蘋果", -2)


def test_remove_partial_keeps_original():
    original = {"香蕉": 5}
    result = remove_item(original, "香蕉", 2)
    assert result == {"香蕉": 3}
    assert original == {"香蕉": 5}


def test_remove_exact_qty_deletes_item():
    assert remove_item({"香蕉": 4, "蘋果": 1}, "香蕉", 4) == {"蘋果": 1}


def test_remove_insufficient_raises():
    with pytest.raises(ValueError):
        remove_item({"香蕉": 2}, "香蕉", 3)


def test_remove_missing_item_raises():
    with pytest.raises(ValueError):
        remove_item({"香蕉": 2}, "蘋果", 1)


def test_total_value_basic():
    stock = {"蘋果": 3, "香蕉": 2}
    prices = {"蘋果": 10.0, "香蕉": 5.5}
    assert total_value(stock, prices) == pytest.approx(41.0)


def test_total_value_missing_price_counts_as_zero():
    stock = {"蘋果": 3, "神秘商品": 4}
    prices = {"蘋果": 10.0}
    assert total_value(stock, prices) == pytest.approx(30.0)
