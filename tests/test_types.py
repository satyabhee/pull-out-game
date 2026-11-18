"""Tests for the shared interface: exact units and validated records."""

import dataclasses
import pathlib
import re

import pytest

from engine_py.types import (
    ASSETS, ConversionError, Fill, Modify, OutsideFill, Place, Side, Tif,
    TIF_FROM_WIRE, ZERO_HASH, from_units, to_units,
)

BTC, ETH = ASSETS[0], ASSETS[1]
META = dict(user="0xabc", block=1224258681, tx_index=0, item_index=0,
            hash="0x01", priority=None)


# ---- units ----------------------------------------------------------------

def test_workbench_example():
    assert to_units("101.10", "0.01") == 10110
    with pytest.raises(ConversionError):
        to_units("101.105", "0.01")


@pytest.mark.parametrize("value,unit,expected", [
    ("64500", "0.1", 645000),
    ("64500.0", "0.1", 645000),      # same price, different format
    ("0.1", "0.1", 1),
    ("1e3", "0.1", 10000),
    ("0", "0.1", 0),
])
def test_to_units_exact(value, unit, expected):
    assert to_units(value, unit) == expected


@pytest.mark.parametrize("bad", ["abc", "", "NaN", "Infinity"])
def test_to_units_rejects_non_decimals(bad):
    with pytest.raises(ConversionError):
        to_units(bad, "0.1")


def test_to_units_takes_strings_only():
    with pytest.raises(TypeError):
        to_units(0.1, "0.1")
    with pytest.raises(TypeError):
        to_units(1, "0.1")


def test_asset_units_from_meta():
    assert (BTC.tick, BTC.lot) == ("0.1", "0.00001")
    assert (ETH.tick, ETH.lot) == ("0.01", "0.0001")


def test_real_eth_order():
    # block 1224258679 window: {'a': 1, 'p': '1836.9', 's': '1.114', ...}
    assert ETH.px_to_ticks("1836.9") == 183690
    assert ETH.sz_to_lots("1.114") == 11140


@pytest.mark.parametrize("asset,px,sz", [
    (0, "63597.59", "0.00077"),      # real BTC GTC item: price has 2 decimals
    (0, "0", "0.03064524"),          # real BTC IOC item: size has 8 decimals
])
def test_real_invalid_items_are_rejected(asset, px, sz):
    spec = ASSETS[asset]
    with pytest.raises((ConversionError, ValueError)):
        Place(**META, asset=asset, side=Side.BUY, px=spec.px_to_ticks(px),
              sz=spec.sz_to_lots(sz), tif=Tif.GTC, cloid=None, reduce_only=False)


def test_round_trip():
    assert BTC.ticks_to_px(BTC.px_to_ticks("95123.4")) == "95123.4"
    assert from_units(645000, "0.1") == "64500"


# ---- records ----------------------------------------------------------------

def _place(**kw):
    args = dict(META, asset=0, side=Side.BUY, px=950000, sz=100, tif=Tif.ALO,
                cloid=None, reduce_only=False)
    args.update(kw)
    return Place(**args)


def test_place_is_frozen():
    p = _place()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.px = 1


@pytest.mark.parametrize("kw", [dict(px=0), dict(sz=0), dict(sz=-1),
                                dict(px=True), dict(px="950000"), dict(asset=7)])
def test_place_validation(kw):
    with pytest.raises((TypeError, ValueError)):
        _place(**kw)


def test_frontend_market_is_ioc():
    assert TIF_FROM_WIRE["FrontendMarket"] is Tif.IOC


def test_side_opposite():
    assert Side.BUY.opposite is Side.SELL and Side.SELL.opposite is Side.BUY


def test_modify_target_int_or_cloid():
    new = _place(tif=Tif.GTC)
    assert Modify(**META, asset=0, target=12345, new=new).target == 12345
    assert Modify(**META, asset=0, target="0xf102", new=new).target == "0xf102"


def test_outside_fill_and_fill():
    of = OutsideFill(**{**META, "hash": ZERO_HASH}, asset=0, maker_user="0xm",
                     taker_side=Side.SELL, px=950000, sz=18, tid=7)
    f = Fill(maker_order=1, taker_order=None, maker_user=of.maker_user,
             taker_user=of.user, taker_side=of.taker_side, px=of.px, sz=of.sz,
             block=of.block, seq=1, taker_hash=of.hash)
    assert f.taker_order is None


def test_no_float_in_types_module():
    src = pathlib.Path(__file__).parents[1].joinpath("engine_py/types.py").read_text()
    assert not re.search(r"\bfloat\b", src)


# ---- fast to_units must equal the original Fraction implementation, exceptions included -------------

from decimal import Decimal as _D, InvalidOperation as _IO
from fractions import Fraction as _F

from hypothesis import given, settings, strategies as st

from engine_py import types as T


def reference_to_units(value, unit):
    """The pre-Nov-18 implementation, kept verbatim as the oracle."""
    if not isinstance(value, str) or not isinstance(unit, str):
        raise TypeError("to_units takes decimal strings only")
    try:
        v, u = _D(value.strip()), _D(unit.strip())
    except _IO as e:
        raise ConversionError(f"not a decimal: {value!r} / {unit!r}") from e
    if not (v.is_finite() and u.is_finite()) or u <= 0:
        raise ConversionError(f"bad value or unit: {value!r} / {unit!r}")
    q = _F(v) / _F(u)
    if q.denominator != 1:
        raise ConversionError(f"{value} is not a whole multiple of {unit}")
    return q.numerator


def outcome(f, *args):
    try:
        return ("ok", f(*args))
    except Exception as e:
        return (type(e).__name__,)


decimal_text = st.one_of(
    st.decimals(allow_nan=True, allow_infinity=True, places=None).map(str),
    st.builds(lambda i, f, e: f"{i}.{f}" + (f"e{e}" if e is not None else ""),
              st.integers(-10 ** 9, 10 ** 9), st.integers(0, 10 ** 8), st.none() | st.integers(-12, 12)),
    st.sampled_from(["", " ", "abc", "1.2.3", " 101.10 ", "-0", "-0.0", "1e3", "1E-3", "NaN", "-inf",
                     "0.00001", "64893.0", "0.1", "0.01", "0", "-0.1", "1_000"]),
)


@settings(max_examples=3000, deadline=None)
@given(decimal_text, st.sampled_from(["0.1", "0.01", "0.00001", "0.0001", "1", "0.5", "0", "-0.1", "x", " 0.1 "])
       | decimal_text)
def test_to_units_matches_the_reference_on_any_input(value, unit):
    assert outcome(T.to_units, value, unit) == outcome(reference_to_units, value, unit)
    assert outcome(T.to_units, value, unit) == outcome(reference_to_units, value, unit)   # cached path


def test_errors_are_raised_every_time_not_cached():
    for _ in range(3):
        with pytest.raises(ConversionError):
            to_units("101.105", "0.01")


def test_non_strings_are_refused_even_if_an_equal_string_is_cached():
    assert to_units("1", "1") == 1
    for bad in (1, 1.0, True):
        with pytest.raises(TypeError):
            to_units(bad, "1")


def test_asset_units_are_unchanged():
    assert (ASSETS[0].tick, ASSETS[0].lot, ASSETS[1].tick, ASSETS[1].lot) == ("0.1", "0.00001", "0.01", "0.0001")
