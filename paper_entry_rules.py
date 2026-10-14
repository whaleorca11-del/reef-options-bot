"""Independent stock-direction rules. No option scoring or external requests."""
import math


RULES = {
    "strategy_id": "ORCA_PAPER_CROSS_V3",
    "entry_timeframe": "5m",
    "ema9": {"type": "EMA", "period": 9, "timeframe": "5m"},
    "ema200": {"type": "EMA", "period": 200, "timeframe": "5m"},
    "vwap": {"anchor": "US regular trading session"},
    "trigger_combination": "OR",
    "trigger_indicators": ["ema9_5m", "session_vwap"],
    "ema200_role": "preference_only_not_entry_trigger_or_gate",
    "confirmation": "closed five-minute candle",
    "conflicting_crosses": "WAIT",
}


def _positive(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def ema200_context(close, level, direction):
    """Descriptive preference only. Never modifies direction or eligibility."""
    price, average = _positive(close), _positive(level)
    if price is None or average is None:
        position, preference = "UNAVAILABLE", "UNAVAILABLE"
    else:
        position = "ABOVE" if price > average else "BELOW" if price < average else "AT"
        if direction not in ("CALL", "PUT"):
            preference = "NO_SIGNAL"
        elif position == "AT":
            preference = "NEUTRAL"
        else:
            aligned = (direction == "CALL" and position == "ABOVE") or (
                direction == "PUT" and position == "BELOW"
            )
            preference = "ALIGNED" if aligned else "OPPOSED"
    return {"price_position": position, "preference": preference,
            "entry_gate": False, "standalone_entry_trigger": False}


def crossing_signal(previous_close, close, previous_levels, levels, *, bar_closed):
    """Price crosses EMA9 or VWAP; EMA200 describes preference, not permission."""
    if not bar_closed:
        return {"direction": None, "status": "WAIT - CANDLE OPEN", "triggers": []}
    previous_close, close = _positive(previous_close), _positive(close)
    if previous_close is None or close is None:
        return {"direction": None, "status": "WAIT - PRICE DATA", "triggers": []}
    triggers, available = [], []
    for name in RULES["trigger_indicators"]:
        before, current = _positive(previous_levels.get(name)), _positive(levels.get(name))
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
            "available_indicators": available, "paper_only": True,
            "ema200_context": ema200_context(close, levels.get("ema200_5m"), direction)}
