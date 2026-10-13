"""
tests/test_models.py
Unit tests for ArbForge models, validation guards, and spread normalization mathematics.
Covers IEEE 754 precision, numerical traps, mathematical invariants, and Pydantic V2 models.
"""

import math
import pytest
from pydantic import ValidationError

from arbforge.models import (
    ConversionLeg,
    CrossedBookError,
    EmptyBookError,
    InvalidPayloadError,
    InvalidPriceError,
    InvalidQuantityError,
    Level1Ticker,
    LockedBookError,
    OrderBookMatrixResponse,
    TriangularCycleOpportunity,
    TriangularCyclesResponse,
    calculate_spreads,
    parse_safe_float,
    parse_safe_price,
    parse_safe_quantity,
    parse_symbol_pair,
)


class TestSpreadMathematics:
    """Verifies quantitative precision and scale-invariance of spread calculations."""

    @pytest.mark.parametrize(
        "bid,ask,expected_abs,expected_mid_bps,expected_bid_bps",
        [
            # BTCUSDT: large price, 1-cent spread
            (79624.00, 79624.01, 0.01, 0.00126, 0.00126),
            # ETHUSDT: medium price, 1-cent spread
            (2453.16, 2453.17, 0.01, 0.04076, 0.04076),
            # SOLUSDT: small price, 1-cent spread (~1 bps)
            (102.30, 102.31, 0.01, 0.97747, 0.97752),
            # ETHBTC: fractional BTC price, 5-decimal spread
            (0.030810, 0.030820, 0.000010, 3.24517, 3.24570),
            # SOLBTC: micro-tick price (7 decimals)
            (0.00128470, 0.00128480, 0.00000010, 0.77836, 0.77842),
            # Locked book: bid == ask
            (100.0, 100.0, 0.0, 0.0, 0.0),
        ],
    )
    def test_spread_calculations_precision(
        self, bid, ask, expected_abs, expected_mid_bps, expected_bid_bps
    ):
        spread_abs, bps_mid, bps_bid = calculate_spreads(bid, ask)
        assert spread_abs == pytest.approx(expected_abs, abs=1e-8)
        assert bps_mid == pytest.approx(expected_mid_bps, abs=1e-4)
        assert bps_bid == pytest.approx(expected_bid_bps, abs=1e-4)

    def test_monotone_invariant_bid_gt_mid(self):
        """spread_bps_bid must be strictly greater than spread_bps_mid for any positive spread."""
        test_cases = [
            (100.0, 105.0),
            (0.001, 0.0015),
            (80000.0, 80050.0),
            (2400.0, 2400.20),
        ]
        for bid, ask in test_cases:
            _, bps_mid, bps_bid = calculate_spreads(bid, ask, round_output=False)
            assert bps_bid > bps_mid, f"Failed for bid={bid}, ask={ask}: {bps_bid} <= {bps_mid}"

    def test_algebraic_identity_invariant(self):
        """Verifies the algebraic identity: bps_bid == bps_mid / (1.0 - bps_mid / 20000.0)."""
        test_cases = [
            (10.0, 10.5),
            (100.0, 102.0),
            (79624.0, 79625.0),
            (0.03081, 0.03082),
        ]
        for bid, ask in test_cases:
            _, bps_mid, bps_bid = calculate_spreads(bid, ask, round_output=False)
            reconstructed_bid_bps = bps_mid / (1.0 - (bps_mid / 20000.0))
            assert bps_bid == pytest.approx(reconstructed_bid_bps, rel=1e-9)


class TestSanityGuardsAndParsing:
    """Verifies protections against corrupted or adversarial inputs (NaN, Inf, strings)."""

    @pytest.mark.parametrize(
        "bad_val",
        [
            "nan",
            "NaN",
            "-NaN",
            "inf",
            "Infinity",
            "-inf",
            "-Infinity",
            "",
            "   ",
            "null",
            "undefined",
            "N/A",
            "foo",
            None,
        ],
    )
    def test_parse_safe_float_rejections(self, bad_val):
        with pytest.raises((InvalidPayloadError, InvalidPriceError)):
            parse_safe_price(bad_val, "test_field")

    @pytest.mark.parametrize(
        "zero_or_neg",
        [
            "0.0",
            "0.00000000",
            0.0,
            0,
            "-1.0",
            "-0.00000001",
            -50.0,
        ],
    )
    def test_parse_safe_price_non_positive_rejected(self, zero_or_neg):
        with pytest.raises(InvalidPriceError):
            parse_safe_price(zero_or_neg, "price")

    def test_valid_exponential_notation(self):
        """Binance or Python sometimes formats micro-satoshi as 1e-07."""
        val = parse_safe_price("1e-07", "price")
        assert val == 1e-07
        assert val > 0

    def test_quantity_guards(self):
        """Verifies quantity bounds and zero-handling."""
        # Positive quantity
        assert parse_safe_quantity("10.5", "qty") == 10.5
        # Negative quantity always rejected
        with pytest.raises(InvalidQuantityError):
            parse_safe_quantity("-0.1", "qty", allow_zero=True)
        # Zero quantity rejected if allow_zero=False
        with pytest.raises(EmptyBookError):
            parse_safe_quantity("0.0", "qty", allow_zero=False)
        # Zero quantity permitted if allow_zero=True
        assert parse_safe_quantity("0.0", "qty", allow_zero=True) == 0.0


class TestLevel1TickerModel:
    """Verifies Pydantic Level1Ticker construction, validation, and factory methods."""

    def test_valid_from_raw_factory(self):
        ticker = Level1Ticker.from_raw(
            symbol="BTCUSDT",
            bid_price=79624.00,
            bid_qty=4.08102,
            ask_price=79624.01,
            ask_qty=2.86563,
            timestamp_ms=1788607566123,
            iso_time="2026-09-05T11:26:06.123456Z",
        )
        assert ticker.symbol == "BTCUSDT"
        assert ticker.bid_price == 79624.00
        assert ticker.ask_price == 79624.01
        assert ticker.bid_qty == 4.08102
        assert ticker.ask_qty == 2.86563
        assert ticker.spread_abs == 0.01
        assert ticker.spread_bps_mid == 0.00126
        assert ticker.spread_bps_bid == 0.00126
        assert ticker.base_asset == "BTC"
        assert ticker.quote_asset == "USDT"
        assert ticker.status == "ACTIVE"
        assert ticker.timestamp_ms == 1788607566123
        assert ticker.iso_time == "2026-09-05T11:26:06.123456Z"

    def test_from_binance_raw(self):
        raw = {
            "symbol": "ETHUSDT",
            "bidPrice": "2453.16000000",
            "bidQty": "20.69370000",
            "askPrice": "2453.17000000",
            "askQty": "2.85550000",
        }
        ticker = Level1Ticker.from_binance_raw(raw)
        assert ticker.symbol == "ETHUSDT"
        assert ticker.bid_price == 2453.16
        assert ticker.ask_price == 2453.17
        assert ticker.base_asset == "ETH"
        assert ticker.quote_asset == "USDT"
        assert ticker.status == "ACTIVE"

    def test_crossed_book_strict_rejection(self):
        raw = {
            "symbol": "BTCUSDT",
            "bidPrice": "80000.00",
            "bidQty": "1.0",
            "askPrice": "79990.00",  # ask < bid!
            "askQty": "1.0",
        }
        with pytest.raises(CrossedBookError):
            Level1Ticker.from_binance_raw(raw, strict=True)

    def test_crossed_book_permissive_flagging(self):
        raw = {
            "symbol": "BTCUSDT",
            "bidPrice": "80000.00",
            "bidQty": "1.0",
            "askPrice": "79990.00",
            "askQty": "1.0",
        }
        ticker = Level1Ticker.from_binance_raw(raw, strict=False)
        assert ticker.status == "CROSSED"
        assert ticker.spread_abs == -10.0
        assert ticker.spread_bps_mid < 0.0

    def test_locked_book_detection(self):
        raw = {
            "symbol": "ETHUSDT",
            "bidPrice": "2500.00",
            "bidQty": "10.0",
            "askPrice": "2500.00",
            "askQty": "10.0",
        }
        ticker = Level1Ticker.from_binance_raw(raw)
        assert ticker.status == "LOCKED"
        assert ticker.spread_abs == 0.0
        assert ticker.spread_bps_mid == 0.0
        assert ticker.spread_bps_bid == 0.0

    def test_empty_book_detection(self):
        raw = {
            "symbol": "SOLUSDT",
            "bidPrice": "150.00",
            "bidQty": "0.0",
            "askPrice": "150.05",
            "askQty": "10.0",
        }
        ticker = Level1Ticker.from_binance_raw(raw, strict=False)
        assert ticker.status == "EMPTY_BOOK"

    def test_json_serialization_roundtrip(self):
        raw = {
            "symbol": "SOLUSDT",
            "bidPrice": "102.30",
            "bidQty": "394.522",
            "askPrice": "102.31",
            "askQty": "388.897",
        }
        ticker = Level1Ticker.from_binance_raw(raw)
        dumped = ticker.model_dump()
        reconstructed = Level1Ticker(**dumped)
        assert reconstructed.symbol == "SOLUSDT"
        assert reconstructed.spread_abs == ticker.spread_abs
        assert reconstructed.model_dump() == dumped

    def test_invalid_pydantic_direct_construction(self):
        """Direct construction with negative or non-finite price must fail."""
        with pytest.raises(ValidationError):
            Level1Ticker(
                symbol="BTCUSDT",
                bid_price=-100.0,
                bid_qty=1.0,
                ask_price=80000.0,
                ask_qty=1.0,
                spread_abs=80100.0,
                spread_bps_mid=100.0,
                spread_bps_bid=100.0,
                timestamp_ms=1000,
                iso_time="2026-09-05T00:00:00Z",
            )


class TestForwardModelsContract:
    """Verifies all models defined in PROJECT.md interface contracts."""

    def test_orderbook_matrix_response_structure(self):
        ticker = Level1Ticker.from_raw("BTCUSDT", 80000.0, 1.0, 80001.0, 1.0)
        resp = OrderBookMatrixResponse(
            status="HEALTHY",
            timestamp_ms=1788607566000,
            pairs=[ticker],
            matrix={"BTCUSDT": ticker},
        )
        assert resp.status == "HEALTHY"
        assert resp.pairs_count == 1
        assert "BTCUSDT" in resp.matrix
        assert resp.iso_time is not None

    def test_conversion_leg_and_triangular_models(self):
        leg = ConversionLeg(
            step=1,
            from_asset="USDT",
            to_asset="BTC",
            symbol="BTCUSDT",
            action="BUY",
            price=80001.0,
            available_qty_base=2.5,
            taker_fee_rate=0.001,
            rate_multiplier=0.000012499,
        )
        assert leg.step == 1
        assert leg.action == "BUY"

        opp = TriangularCycleOpportunity(
            cycle_id="USDT->BTC->ETH->USDT",
            path=["USDT", "BTC", "ETH", "USDT"],
            legs=[leg],
            gross_yield_pct=1.5,
            net_yield_pct=1.2,
            is_arbitrage=True,
            synthetic_rate=2400.0,
            direct_rate=2380.0,
            discrepancy_pct=0.84,
            bottleneck_leg_index=0,
            bottleneck_symbol="BTCUSDT",
            max_executable_volume_quote=10000.0,
            expected_net_profit_quote=120.0,
            starting_asset="USDT",
            timestamp_ms=1788607566000,
        )
        assert opp.is_arbitrage is True
        assert opp.expected_net_profit_quote == 120.0

        cycles_resp = TriangularCyclesResponse(
            status="HEALTHY",
            timestamp_ms=1788607566000,
            opportunities=[opp],
            active_count=1,
            scanned_count=6,
        )
        assert cycles_resp.active_count == 1
        assert len(cycles_resp.opportunities) == 1
