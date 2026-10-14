"""Isolated, asynchronous option paper evaluations. Never opens real trades."""
import json
import math
import os
import threading
import uuid
from collections import Counter
from pathlib import Path

EVALUATION_ID = "ORCA_PAPER_OPTIONS_ASK_BID_60M"
HORIZONS = (30, 60)
WINDOW_SECONDS = 120


class PaperOptionUnavailable(RuntimeError):
    """A safe, descriptive rejection message, without credentials."""


def executable_quote(quote, now, max_age, *, allow_zero_bid=False):
    bid, ask, stamp = (quote.get(k) for k in ("bid", "ask", "quote_timestamp"))
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) for v in (bid, ask, stamp)):
        raise PaperOptionUnavailable("MISSING_OR_INVALID_BID_ASK_TIMESTAMP")
    if (bid < 0 or (bid == 0 and not allow_zero_bid) or ask <= 0 or ask < bid
            or not 0 <= now.timestamp() - stamp <= max_age):
        raise PaperOptionUnavailable("FRESH_TWO_SIDED_QUOTE_REQUIRED")
    return {"bid": bid, "ask": ask, "quote_timestamp": stamp}


class PaperOptionEngine:
    def __init__(self, path, *, select, quote, clock, session_bounds, max_quote_age=30):
        self.path = Path(path)
        self.select, self.quote = select, quote
        self.clock, self.session_bounds = clock, session_bounds
        self.max_quote_age = max_quote_age
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.worker_error = None

    def _read(self):
        if not self.path.exists():
            return {"version": 1, "records": {}}
        try:
            state = json.loads(self.path.read_text())
            if (state.get("version") != 1 or not isinstance(state.get("records"), dict)
                    or any(not isinstance(r, dict) or not isinstance(r.get("outcomes"), dict)
                           or not isinstance(r.get("signal_close_ms"), int)
                           or r.get("direction") not in ("CALL", "PUT")
                           or r.get("status") not in ("QUEUED", "SELECTING", "NO_ENTRY", "OPEN", "CLOSED", "EXIT_UNAVAILABLE")
                           or (r.get("status") in ("OPEN", "CLOSED", "EXIT_UNAVAILABLE")
                               and (not isinstance(r.get("contract"), dict)
                                    or not r["contract"].get("ticker")
                                    or not isinstance(r.get("entry_at_ms"), int)
                                    or not isinstance(r.get("entry_ask"), (float, int))
                                    or r["entry_ask"] <= 0))
                           for r in state["records"].values())):
                raise ValueError("invalid paper option state")
            return state
        except (ValueError, OSError, AttributeError) as error:
            raise RuntimeError("Paper option storage unreadable; not reset") from error

    def _write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("w") as handle:
                json.dump(state, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def submit(self, candle, now):
        if candle.get("direction") not in ("CALL", "PUT"):
            return {"queued": False, "reason": "NO_DIRECTION_SIGNAL"}
        key = f'{candle["signal_type"]}:{candle["symbol"]}:{candle["bar_close_time_ms"]}'
        with self.lock:
            state = self._read()
            if key in state["records"]:
                return {"queued": False, "duplicate": True, "id": key}
            state["records"][key] = {
                "id": key, "evaluation_id": EVALUATION_ID,
                "signal_type": candle["signal_type"], "symbol": candle["symbol"],
                "direction": candle["direction"],
                "signal_close_ms": candle["bar_close_time_ms"],
                "stock_close": candle["close"], "queued_at": now.isoformat(),
                "status": "QUEUED", "contract": None, "outcomes": {},
                "fees_included": False, "net_profit_usd": None,
            }
            self._write(state)
        return {"queued": True, "id": key}

    def _save_record(self, row):
        with self.lock:
            state = self._read()
            state["records"][row["id"]] = row
            self._write(state)

    def _error(self, error):
        # Never persist an arbitrary HTTP exception, URL, or authorization header.
        return str(error) if isinstance(error, PaperOptionUnavailable) else type(error).__name__

    def _enter(self, row, records, now):
        bounds = self.session_bounds(now)
        age = now.timestamp() - row["signal_close_ms"] / 1000
        if not 0 <= age <= WINDOW_SECONDS:
            row.update(status="NO_ENTRY", reason="MISSED_ENTRY_WINDOW")
        elif not bounds or not bounds[0] <= now < bounds[1]:
            row.update(status="NO_ENTRY", reason="OUTSIDE_REGULAR_SESSION")
        elif now.timestamp() + 3600 > bounds[1].timestamp():
            row.update(status="NO_ENTRY", reason="INSUFFICIENT_SESSION_TIME_FOR_60M_EXIT")
        else:
            used = {r["contract"]["ticker"] for r in records
                    if isinstance(r.get("contract"), dict)}
            row["status"] = "SELECTING"
            self._save_record(row)
            try:
                selected = self.select(row, used)
                if not selected.get("contract"):
                    row.update(status="NO_ENTRY", reason=selected.get("reason", "NO_ELIGIBLE_CONTRACT"),
                               diagnostics=selected.get("diagnostics", {}))
                else:
                    contract = selected["contract"]
                    current = self.clock()
                    if contract["ticker"] in used:
                        raise PaperOptionUnavailable("EXACT_CONTRACT_ALREADY_RECORDED")
                    quote = executable_quote(contract, current, self.max_quote_age)
                    if current.timestamp() - row["signal_close_ms"] / 1000 > WINDOW_SECONDS:
                        raise PaperOptionUnavailable("MISSED_ENTRY_WINDOW_AFTER_PROVIDER_REQUEST")
                    if current.timestamp() + 3600 > bounds[1].timestamp():
                        raise PaperOptionUnavailable("INSUFFICIENT_SESSION_TIME_FOR_60M_EXIT")
                    row.update(status="OPEN", contract=contract,
                               entry_at_ms=int(current.timestamp() * 1000),
                               entry_ask=quote["ask"], entry_cost_usd=round(quote["ask"] * 100, 2),
                               entry_quote_timestamp=quote["quote_timestamp"],
                               reason=None, diagnostics=selected.get("diagnostics", {}))
            except Exception as error:
                row.update(status="NO_ENTRY", reason=self._error(error))
        self._save_record(row)

    def _observe(self, row, minutes, now):
        due = row["entry_at_ms"] / 1000 + minutes * 60
        outcome = {"scheduled_at_ms": int(due * 1000)}
        bounds = self.session_bounds(now)
        if now.timestamp() - due > WINDOW_SECONDS:
            outcome.update(status="missing_observation", reason="MISSED_EXIT_WINDOW")
        elif not bounds or not bounds[0] <= now or now.timestamp() > bounds[1].timestamp() + WINDOW_SECONDS:
            outcome.update(status="missing_observation", reason="OUTSIDE_REGULAR_SESSION")
        else:
            row["outcomes"][str(minutes)] = {**outcome, "status": "OBSERVING"}
            self._save_record(row)
            try:
                contract = row["contract"]
                refreshed = self.quote(row["symbol"], contract, row["stock_close"])
                if refreshed.get("ticker") != contract["ticker"]:
                    raise PaperOptionUnavailable("QUOTE_CONTRACT_MISMATCH")
                if (refreshed.get("expiration") != contract.get("expiration")
                        or refreshed.get("strike") != contract.get("strike")):
                    raise PaperOptionUnavailable("QUOTE_CONTRACT_DETAILS_MISMATCH")
                current = self.clock()
                if current.timestamp() - due > WINDOW_SECONDS:
                    raise PaperOptionUnavailable("MISSED_EXIT_WINDOW_AFTER_PROVIDER_REQUEST")
                quote = executable_quote(refreshed, current, self.max_quote_age, allow_zero_bid=True)
                pnl = (quote["bid"] - row["entry_ask"]) * 100
                outcome.update(
                    status="observed", observed_at_ms=int(current.timestamp() * 1000),
                    quote_timestamp=quote["quote_timestamp"], exit_bid=quote["bid"],
                    gross_pnl_usd=round(pnl, 2),
                    gross_return_pct=round(pnl / row["entry_cost_usd"] * 100, 6),
                    fees_included=False, net_profit_usd=None,
                    paper_exit=minutes == 60,
                    liquidation_basis=("bid_mark" if quote["bid"] > 0
                                       else "zero_bid_zero_proceeds_not_a_fill"),
                )
            except Exception as error:
                outcome.update(status="missing_observation", reason=self._error(error))
        row["outcomes"][str(minutes)] = outcome
        if minutes == 60:
            row["status"] = "CLOSED" if outcome["status"] == "observed" else "EXIT_UNAVAILABLE"
        self._save_record(row)

    def tick(self):
        if not self.run_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                records = list(self._read()["records"].values())
            # Exit observations always precede new chains; at most one provider
            # action per tick. Already missed windows require no provider call.
            now = self.clock()
            for row in records:
                if row["status"] == "SELECTING":
                    row.update(status="NO_ENTRY", reason="INTERRUPTED_ENTRY_SELECTION")
                    self._save_record(row)
                    return
                for key, outcome in row["outcomes"].items():
                    if outcome.get("status") == "OBSERVING":
                        outcome.update(status="missing_observation",
                                       reason="INTERRUPTED_EXIT_OBSERVATION")
                        if key == "60":
                            row["status"] = "EXIT_UNAVAILABLE"
                        self._save_record(row)
                        return
            due = []
            for row in records:
                if row["status"] != "OPEN":
                    continue
                for minutes in HORIZONS:
                    target = row["entry_at_ms"] / 1000 + minutes * 60
                    if str(minutes) not in row["outcomes"] and now.timestamp() >= target:
                        due.append((target, minutes, row))
            if due:
                _, minutes, row = min(due, key=lambda item: item[0])
                self._observe(row, minutes, now)
                return
            queued = sorted((r for r in records if r["status"] == "QUEUED"),
                            key=lambda r: r["signal_close_ms"])
            if queued:
                self._enter(queued[0], records, now)
        finally:
            self.run_lock.release()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.thread
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._loop, name="orca-paper-options", daemon=True)
            self.thread.start()
            return self.thread

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.worker_error = None
            except Exception as error:
                self.worker_error = self._error(error)
            self.stop_event.wait(1)

    def snapshot(self, now):
        with self.lock:
            try:
                records = list(self._read()["records"].values())
            except RuntimeError:
                return {"status": "storage_error", "error": "Paper option storage unreadable"}
        closed = [r for r in records if r["status"] == "CLOSED"]
        entered = [r for r in records if r.get("entry_at_ms")]
        missing_exits = sum(r["status"] == "EXIT_UNAVAILABLE" for r in records)
        results = [r["outcomes"]["60"] for r in closed]
        pnl = [r["gross_pnl_usd"] for r in results]
        return {
            "evaluation_id": EVALUATION_ID, "paper_only": True,
            "fills_simulated": True,
            "contract_selection_enabled": True, "telegram_enabled": False,
            "worker_running": bool(self.thread and self.thread.is_alive()),
            "worker_error": self.worker_error,
            "entry_price_basis": "fresh_ask", "exit_price_basis": "fresh_bid",
            "fixed_exit_minutes": 60, "diagnostic_horizon_minutes": 30,
            "fees_included": False, "net_profit_usd": None,
            "status_counts": dict(Counter(r["status"] for r in records)),
            "total_signals": len(records), "completed_trades": len(closed),
            "entered_trades": len(entered),
            "missing_exit_count": missing_exits,
            "awaiting_exit_count": sum(r["status"] == "OPEN" for r in records),
            "exit_observation_coverage_pct": (round(len(closed) / (len(closed) + missing_exits) * 100, 2)
                                               if closed or missing_exits else None),
            "wins": sum(v > 0 for v in pnl), "losses": sum(v < 0 for v in pnl),
            "breakeven": sum(v == 0 for v in pnl),
            "gross_pnl_usd": round(sum(pnl), 2),
            "mean_gross_return_pct": (round(sum(r["gross_return_pct"] for r in results) / len(results), 6)
                                      if results else None),
            "worst_observed_trade_usd": min(pnl) if pnl else None,
            "assessment": "PRELIMINARY_NOT_PROOF_OF_PROFITABILITY",
            "recent_records": sorted(records, key=lambda r: r["signal_close_ms"])[-50:],
        }
