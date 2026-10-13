"""CM_Ult_MacD_MTF-compatible hourly stock direction with EMA200.

EMA(close, 120) - EMA(close, 180), with SMA(MACD, 40) as signal.
Extended-session candles match the chart supplied by the user. The histogram
sign and price position relative to EMA200 must agree; no new cross is needed.
"""

import math

FAST = 120
SLOW = 180
SIGNAL = 40
BAR_SECONDS = 3600
SIGNAL_TYPE = "MACD_1H_EMA200"
EMA_LENGTH = 200
MIN_BARS = SLOW * 6


class MacdDataError(ValueError):
    pass


def ema(closes, length=EMA_LENGTH):
    if len(closes) < length:
        raise MacdDataError("EMA200 needs at least 200 completed candles")
    value = float(closes[0])
    for close in closes:
        close = float(close)
        if not math.isfinite(close) or close <= 0:
            raise MacdDataError("Invalid EMA200 candle")
        value += (close - value) * (2.0 / (length + 1))
    return value


def stock_direction(hist, price, ema200):
    """Select a side only when both stock conditions agree."""
    if not all(math.isfinite(v) for v in (hist, price, ema200)) or min(price, ema200) <= 0:
        raise MacdDataError("Invalid hourly MACD/EMA200 values")
    if hist > 0 and price > ema200:
        return "CALL"
    if hist < 0 and price < ema200:
        return "PUT"
    return None


def validate_candle(signal, now):
    """Validate actual TradingView close times, including :30 US session bars."""
    start = signal.get("bar_time_ms")
    end = signal.get("bar_close_time_ms")
    if (
        type(start) is not int or type(end) is not int
        or start % 60000 or end % 60000
        or end - start not in (1800000, 3600000)
        or not 0 <= now.timestamp() * 1000 - end < BAR_SECONDS * 1000
    ):
        raise MacdDataError("Hourly candle is stale, future, or not yet closed")
    local_start = now.fromtimestamp(start / 1000, now.tzinfo)
    local_end = now.fromtimestamp(end / 1000, now.tzinfo)
    if local_start.minute not in (0, 30) or local_start.weekday() >= 5:
        raise MacdDataError("Hourly candle is not aligned to a trading session")
    if end - start == 1800000 and (local_end.hour, local_end.minute) not in ((9, 30), (16, 0)):
        raise MacdDataError("Short hourly candle must end at a session boundary")


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
        if start + BAR_SECONDS > now.timestamp():
            continue
        local = now.fromtimestamp(start, now.tzinfo)
        if local.weekday() >= 5 or not 4 <= local.hour < 20:
            continue
        if start in completed and completed[start] != close:
            raise MacdDataError("Conflicting duplicate MACD candles")
        completed[start] = close
    if not completed:
        raise MacdDataError("Latest closed hourly candle is unavailable")
    times = sorted(completed)
    latest = times[-1]
    candle = {"bar_time_ms": int(latest * 1000),
              "bar_close_time_ms": int((latest + BAR_SECONDS) * 1000)}
    validate_candle(candle, now)
    macd, signal, hist = histogram([completed[t] for t in times])
    ema200 = ema([completed[t] for t in times])
    direction = stock_direction(hist, completed[latest], ema200)
    return {
        "symbol": symbol, "direction": direction,
        "signal_type": SIGNAL_TYPE, "timeframe": "60",
        "fast": FAST, "slow": SLOW, "signal_length": SIGNAL,
        "signal_average": "SMA", "source": "massive_1h_extended",
        "macd": macd, "signal": signal, "histogram": hist,
        **candle, "price": completed[latest], "completed_candles": len(times),
        "ema200": ema200, "ema_length": EMA_LENGTH, "ema_average": "EMA",
    }


def validate_direction(signal, symbol, direction, now):
    if (
        not isinstance(signal, dict)
        or signal.get("symbol") != symbol
        or signal.get("signal_type") != SIGNAL_TYPE
        or signal.get("timeframe") != "60"
        or signal.get("ema_length") != EMA_LENGTH
        or signal.get("ema_average") != "EMA"
        or signal.get("direction") != direction
        or direction not in ("CALL", "PUT")
    ):
        raise MacdDataError("No hourly MACD/EMA200 confirmation for the requested side")
    validate_candle(signal, now)
    hist = signal.get("histogram")
    if not isinstance(hist, (int, float)) or not math.isfinite(hist):
        raise MacdDataError("Invalid MACD histogram")
    try:
        actual = stock_direction(hist, float(signal["price"]), float(signal["ema200"]))
    except (KeyError, TypeError, ValueError):
        raise MacdDataError("Missing hourly stock price/EMA200 confirmation") from None
    if actual != direction:
        raise MacdDataError("Hourly MACD and EMA200 price position do not agree")


def from_webhook(payload, now, watchlist, excluded):
    """Validate authenticated TradingView values, without trusting a side label."""
    symbol = str(payload.get("ticker", "")).upper().split(":")[-1].strip()
    if not symbol or symbol not in watchlist or symbol in excluded:
        raise MacdDataError("MACD ticker is not permitted")
    if (
        payload.get("signal_type") != SIGNAL_TYPE
        or str(payload.get("timeframe")) != "60"
        or payload.get("fast") != FAST or payload.get("slow") != SLOW
        or payload.get("signal_length") != SIGNAL
        or payload.get("signal_average") != "SMA"
        or payload.get("bar_closed") is not True
        or payload.get("ema_length") != EMA_LENGTH
        or payload.get("ema_average") != "EMA"
    ):
        raise MacdDataError("Use closed 60-minute bars, MACD EMA120/180 SMA40, and EMA200")
    validate_candle(payload, now)
    try:
        line = float(payload["macd"])
        average = float(payload["signal"])
        hist = float(payload["histogram"])
        price = float(payload["price"])
        ema200 = float(payload["ema200"])
    except (KeyError, TypeError, ValueError):
        raise MacdDataError("Hourly webhook requires numeric MACD, price, and EMA200") from None
    if not all(math.isfinite(v) for v in (line, average, hist, price, ema200)) or min(price, ema200) <= 0:
        raise MacdDataError("Invalid MACD webhook values")
    if not math.isclose(hist, line - average, rel_tol=1e-8, abs_tol=1e-8):
        raise MacdDataError("MACD webhook histogram does not match MACD minus signal")
    return {
        "symbol": symbol, "direction": stock_direction(hist, price, ema200),
        "signal_type": SIGNAL_TYPE, "timeframe": "60",
        "fast": FAST, "slow": SLOW, "signal_length": SIGNAL,
        "signal_average": "SMA", "source": "tradingview_closed_bar",
        "macd": line, "signal": average, "histogram": hist, "price": price,
        "bar_time_ms": payload["bar_time_ms"],
        "bar_close_time_ms": payload["bar_close_time_ms"],
        "ema200": ema200, "ema_length": EMA_LENGTH, "ema_average": "EMA",
    }
