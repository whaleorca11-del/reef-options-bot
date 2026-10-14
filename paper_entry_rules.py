"""Independent stock-direction rules. No option scoring or external requests."""
import math


RULES = {
    "entry_timeframe": "5m",
    "ema9": {"type": "EMA", "period": 9, "timeframe": "1d"},
    "ema200": {"type": "EMA", "period": 200, "timeframe": "5m"},
    "vwap": {"anchor": "US regular trading session"},
    "trigger_combination": "OR",
    "confirmation": "closed five-minute candle",
    "conflicting_crosses": "WAIT",
}


def crossing_signal(previous_close, close, previous_levels, levels, *, bar_closed):
    """Compare each close to its own bar's levels; never infer option quality."""
    def positive(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    if not bar_closed:
        return {"direction": None, "status": "WAIT - CANDLE OPEN", "triggers": []}
    previous_close, close = positive(previous_close), positive(close)
    if previous_close is None or close is None:
        return {"direction": None, "status": "WAIT - PRICE DATA", "triggers": []}
    triggers, available = [], []
    for name in ("ema9_daily", "ema200_5m", "session_vwap"):
        before, current = positive(previous_levels.get(name)), positive(levels.get(name))
        if before is None or current is None:
            continue
        available.append(name)
        if previous_close <= before and close > current:
            triggers.append({"indicator": name, "direction": "CALL"})
        elif previous_close >= before and close < current:
            triggers.append({"indicator": name, "direction": "PUT"})
    directions = {item["direction"] for item in triggers}
    if len(directions) > 1:
        status, direction = "WAIT - CONFLICTING CROSSES", None
    elif directions:
        status, direction = "PAPER SIGNAL", directions.pop()
    else:
        status = "WAIT - NO CROSS" if available else "WAIT - INDICATOR DATA"
        direction = None
    return {"direction": direction, "status": status, "triggers": triggers,
            "available_indicators": available, "paper_only": True}
