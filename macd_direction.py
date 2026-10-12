"""CM_Ult_MacD_MTF-compatible direction from completed 5-minute bars.

EMA(close, 120) - EMA(close, 180), with SMA(MACD, 40) as signal.
Extended-session candles match the chart supplied by the user. The histogram
sign, not its changing shade or a price moving-average crossing, selects side.
"""

import math

FAST = 120
SLOW = 180
SIGNAL = 40
BAR_SECONDS = 300
MIN_BARS = SLOW * 6


class MacdDataError(ValueError):
    pass


def histogram(closes):
    if len(closes) < MIN_BARS:
        raise MacdDataError(f"MACD needs at least {MIN_BARS} completed candles")
    fast = slow = float(closes[0])
    macd_values = []
    for close in closes:
        close = float(close)
        if not math.isfinite(close) or close <= 0:
            raise MacdDataError("MACD candle has an invalid close")
        fast += (close - fast) * (2.0 / (FAST + 1))
        slow += (close - slow) * (2.0 / (SLOW + 1))
        macd_values.append(fast - slow)
    macd = macd_values[-1]
    signal = sum(macd_values[-SIGNAL:]) / SIGNAL
    return macd, signal, macd - signal


def from_bars(symbol, bars, now):
    """Reject stale/partial data; never infer direction from an unfinished bar."""
    expected_start = int(now.timestamp()) // BAR_SECONDS * BAR_SECONDS - BAR_SECONDS
    completed = {}
    for bar in bars or []:
        if not isinstance(bar, dict):
            raise MacdDataError("Invalid MACD candle response")
        try:
            start = float(bar["t"]) / 1000.0  # Massive aggregate timestamps: ms
            close = float(bar["c"])
        except (KeyError, TypeError, ValueError):
            raise MacdDataError("MACD candle is missing timestamp/close") from None
        if not math.isfinite(start) or not math.isfinite(close) or close <= 0:
            raise MacdDataError("Invalid MACD candle")
        if int(start) % BAR_SECONDS:
            raise MacdDataError("MACD candle is not aligned to five minutes")
        if start > expected_start:
            continue
        local = now.fromtimestamp(start, now.tzinfo)
        if local.weekday() >= 5 or not 4 <= local.hour < 20:
            continue
        if start in completed and completed[start] != close:
            raise MacdDataError("Conflicting duplicate MACD candles")
        completed[start] = close
    if not completed or max(completed) != expected_start:
        raise MacdDataError("Latest closed five-minute candle is unavailable or delayed")
    times = sorted(completed)
    macd, signal, hist = histogram([completed[t] for t in times])
    direction = "CALL" if hist > 0 else "PUT" if hist < 0 else None
    return {
        "symbol": symbol, "direction": direction,
        "signal_type": "MACD_5M", "timeframe": "5",
        "fast": FAST, "slow": SLOW, "signal_length": SIGNAL,
        "signal_average": "SMA", "source": "massive_5m_extended",
        "macd": macd, "signal": signal, "histogram": hist,
        "bar_time_ms": expected_start * 1000,
        "bar_close_time_ms": (expected_start + BAR_SECONDS) * 1000,
        "price": completed[times[-1]], "completed_candles": len(times),
    }


def validate_direction(signal, symbol, direction, now):
    expected = (int(now.timestamp()) // BAR_SECONDS * BAR_SECONDS - BAR_SECONDS) * 1000
    if (
        not isinstance(signal, dict)
        or signal.get("symbol") != symbol
        or signal.get("signal_type") != "MACD_5M"
        or signal.get("bar_time_ms") != expected
        or signal.get("direction") != direction
        or direction not in ("CALL", "PUT")
    ):
        raise MacdDataError("No fresh MACD confirmation for the requested contract direction")
    hist = signal.get("histogram")
    if not isinstance(hist, (int, float)) or not math.isfinite(hist):
        raise MacdDataError("Invalid MACD histogram")
    if (direction == "CALL" and hist <= 0) or (direction == "PUT" and hist >= 0):
        raise MacdDataError("MACD histogram contradicts the requested direction")


def from_webhook(payload, now, watchlist, excluded):
    """Validate authenticated TradingView values, without trusting a side label."""
    symbol = str(payload.get("ticker", "")).upper().split(":")[-1].strip()
    if not symbol or symbol not in watchlist or symbol in excluded:
        raise MacdDataError("MACD ticker is not permitted")
    if (
        payload.get("signal_type") != "MACD_5M"
        or str(payload.get("timeframe")) != "5"
        or payload.get("fast") != FAST or payload.get("slow") != SLOW
        or payload.get("signal_length") != SIGNAL
        or payload.get("signal_average") != "SMA"
        or payload.get("bar_closed") is not True
    ):
        raise MacdDataError("MACD webhook must use closed 5-minute bars and EMA 120/180, SMA 40")
    expected_start = int(now.timestamp()) // BAR_SECONDS * BAR_SECONDS - BAR_SECONDS
    if (
        payload.get("bar_time_ms") != expected_start * 1000
        or payload.get("bar_close_time_ms") != (expected_start + BAR_SECONDS) * 1000
    ):
        raise MacdDataError("MACD webhook candle is stale, future, or not yet closed")
    try:
        line = float(payload["macd"])
        average = float(payload["signal"])
        hist = float(payload["histogram"])
        price = float(payload["price"])
    except (KeyError, TypeError, ValueError):
        raise MacdDataError("MACD webhook requires numeric indicator values and price") from None
    if not all(math.isfinite(v) for v in (line, average, hist, price)) or price <= 0:
        raise MacdDataError("Invalid MACD webhook values")
    if not math.isclose(hist, line - average, rel_tol=1e-8, abs_tol=1e-8):
        raise MacdDataError("MACD webhook histogram does not match MACD minus signal")
    return {
        "symbol": symbol, "direction": "CALL" if hist > 0 else "PUT" if hist < 0 else None,
        "signal_type": "MACD_5M", "timeframe": "5",
        "fast": FAST, "slow": SLOW, "signal_length": SIGNAL,
        "signal_average": "SMA", "source": "tradingview_closed_bar",
        "macd": line, "signal": average, "histogram": hist, "price": price,
        "bar_time_ms": expected_start * 1000,
        "bar_close_time_ms": (expected_start + BAR_SECONDS) * 1000,
    }
