"""Authenticated stock observations, isolated from option trades and Telegram."""
import json
import math
import os
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_entry_rules import crossing_signal

SIGNAL_TYPE = "ORCA_PAPER_CROSS_V1"
NY = ZoneInfo("America/New_York")
LEVELS = ("ema9_daily", "ema200_5m", "session_vwap")
HORIZONS = (15, 30, 60)


class PaperDataError(ValueError):
    """A rejected observation, never a fabricated signal."""


class PaperConflictError(PaperDataError):
    """Duplicate candle changed, or an older candle arrived."""


def _number(value, label, *, nullable=False):
    if nullable and value is None:
        return None
    if isinstance(value, bool):
        raise PaperDataError(f"{label} must be a finite positive number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PaperDataError(f"{label} must be a finite positive number") from None
    if not math.isfinite(number) or number <= 0:
        raise PaperDataError(f"{label} must be a finite positive number")
    return number


def _stamp(payload, key):
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PaperDataError(f"{key} must be integer milliseconds")
    return value


def from_webhook(payload, now, watchlist, excluded, session_bounds):
    """Recompute direction; ignore any caller-supplied CALL/PUT label."""
    symbol = str(payload.get("ticker", "")).strip().upper().split(":")[-1]
    if symbol not in watchlist or symbol in excluded:
        raise PaperDataError("ticker is not permitted")
    if (
        payload.get("signal_type") != SIGNAL_TYPE
        or payload.get("timeframe") != "5"
        or payload.get("bar_closed") is not True
        or payload.get("ema9_period") != 9
        or payload.get("ema9_timeframe") != "1D"
        or payload.get("ema9_basis") != "previous_closed_daily"
        or payload.get("ema200_period") != 200
        or payload.get("ema200_timeframe") != "5"
        or payload.get("vwap_anchor") != "US_REGULAR_SESSION"
        or payload.get("regular_session") is not True
    ):
        raise PaperDataError("use the ORCA five-minute paper script and closed regular-session bars")
    start = _stamp(payload, "bar_time_ms")
    end = _stamp(payload, "bar_close_time_ms")
    previous_end = _stamp(payload, "previous_bar_close_time_ms")
    if end - start != 300000 or previous_end > start:
        raise PaperDataError("invalid five-minute candle duration or previous candle time")
    age = now.timestamp() - end / 1000
    if age < 0 or age > 120:
        raise PaperDataError("candle must be closed and received within 120 seconds")
    try:
        local_start = datetime.fromtimestamp(start / 1000, NY)
        local_end = datetime.fromtimestamp(end / 1000, NY)
        previous_local = datetime.fromtimestamp(previous_end / 1000, NY)
    except (OverflowError, OSError, ValueError):
        raise PaperDataError("invalid candle timestamp") from None
    bounds = session_bounds(local_start)
    if (
        not bounds
        or local_start < bounds[0] or local_end > bounds[1]
        or local_start.second or local_start.microsecond
        or local_start.minute % 5
    ):
        raise PaperDataError("candle is outside a valid regular US session")
    if previous_local.date() == local_start.date():
        if previous_end != start:
            raise PaperDataError("previous candle must be consecutive within the session")
    else:
        if local_start != bounds[0]:
            raise PaperDataError("prior-session comparison is only permitted at the opening bar")
        prior_date = local_start - timedelta(days=1)
        for _ in range(10):
            prior_bounds = session_bounds(prior_date)
            if prior_bounds:
                if previous_local != prior_bounds[1]:
                    raise PaperDataError("previous candle must end at the prior session close")
                break
            prior_date -= timedelta(days=1)
        else:
            raise PaperDataError("prior trading session is unavailable")
    previous = payload.get("previous_levels")
    levels = payload.get("levels")
    if not isinstance(previous, dict) or not isinstance(levels, dict):
        raise PaperDataError("previous_levels and levels must be objects")
    before = {k: _number(previous.get(k), k, nullable=True) for k in LEVELS}
    current = {k: _number(levels.get(k), k, nullable=True) for k in LEVELS}
    # A reset VWAP is not a crossing of yesterday's VWAP.
    if previous_local.date() != local_start.date():
        before["session_vwap"] = None
    close = _number(payload.get("close"), "close")
    previous_close = _number(payload.get("previous_close"), "previous_close")
    result = crossing_signal(previous_close, close, before, current, bar_closed=True)
    return {
        "symbol": symbol, "source": "tradingview", "signal_type": SIGNAL_TYPE,
        "bar_time_ms": start, "bar_close_time_ms": end,
        "previous_bar_close_time_ms": previous_end,
        "close": close, "previous_close": previous_close,
        "levels": current, "previous_levels": before, **result,
    }


class PaperRecorder:
    """One-process, atomic JSON storage. No secrets or live trade store access."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def _read(self):
        if not self.path.exists():
            return {"version": 1, "latest": {}, "signals": []}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise RuntimeError("paper state is unreadable; it was not replaced") from None
        if (
            not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("latest"), dict)
            or not isinstance(state.get("signals"), list)
            or any(not isinstance(row, dict)
                   or not isinstance(row.get("bar_close_time_ms"), int)
                   or not isinstance(row.get("close"), (float, int))
                   for row in state["latest"].values())
            or any(not isinstance(row, dict) or not isinstance(row.get("outcomes"), dict)
                   or not isinstance(row.get("bar_close_time_ms"), int)
                   or not isinstance(row.get("close"), (float, int))
                   or row.get("direction") not in ("CALL", "PUT")
                   or not isinstance(row.get("symbol"), str)
                   for row in state["signals"])
        ):
            raise RuntimeError("paper state is invalid; it was not replaced")
        return state

    def _write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def record(self, candle, received_at):
        with self.lock:
            state = self._read()
            symbol, end = candle["symbol"], candle["bar_close_time_ms"]
            prior = state["latest"].get(symbol)
            if prior and end <= prior["bar_close_time_ms"]:
                if end < prior["bar_close_time_ms"]:
                    raise PaperConflictError("out-of-order five-minute candle")
                old = {k: v for k, v in prior.items() if k != "received_at"}
                if old != candle:
                    raise PaperConflictError("conflicting values for an already recorded candle")
                return {"accepted": True, "duplicate": True, "candle": candle}
            # Heartbeats measure stock movement, not option P/L. Missing horizons
            # remain explicitly missing; never forward-fill an absent observation.
            for row in state["signals"]:
                if row["symbol"] != symbol:
                    continue
                elapsed = (end - row["bar_close_time_ms"]) / 60000
                for minutes in HORIZONS:
                    key = str(minutes)
                    if key in row["outcomes"] or elapsed < minutes:
                        continue
                    if elapsed == minutes:
                        move = (candle["close"] / row["close"] - 1) * 100
                        row["outcomes"][key] = {
                            "status": "observed", "stock_move_pct": round(move, 6),
                            "directional_move_pct": round(move if row["direction"] == "CALL" else -move, 6),
                            "observed_at_ms": end, "stock_close": candle["close"],
                        }
                    else:
                        row["outcomes"][key] = {"status": "missing_observation"}
            saved = {**candle, "received_at": received_at.isoformat()}
            state["latest"][symbol] = saved
            if candle["direction"]:
                state["signals"].append({
                    **saved, "id": f"{symbol}:{end}", "outcomes": {},
                    "contract_status": "NOT_EVALUATED",
                    "option_profitability": None,
                })
            self._write(state)
        return {"accepted": True, "duplicate": False, "candle": candle,
                "paper_signal_recorded": bool(candle["direction"]),
                "live_entry_created": False, "telegram_sent": False}

    def snapshot(self, now):
        with self.lock:
            try:
                state = self._read()
            except RuntimeError as error:
                return {"source": "tradingview", "status": "storage_error", "error": str(error)}
        latest = state["latest"]
        fresh = {
            symbol: 0 <= now.timestamp() - row["bar_close_time_ms"] / 1000 <= 420
            for symbol, row in latest.items()
        }
        return {
            "source": "tradingview", "endpoint": "/paper-signal",
            "status": ("receiving" if any(fresh.values()) else
                       "waiting_for_fresh_candles" if latest else "awaiting_tradingview_alert"),
            "fresh_symbols": [symbol for symbol, is_fresh in fresh.items() if is_fresh],
            "latest_candles": latest, "signal_count": len(state["signals"]),
            "recent_signals": state["signals"][-10:],
            "outcome_horizons_minutes": list(HORIZONS),
            "outcomes_measure": "stock_direction_only_not_option_profit",
            "contract_selection_enabled": False, "telegram_enabled": False,
            "ema9_basis": "last completed regular-session daily candle",
        }
