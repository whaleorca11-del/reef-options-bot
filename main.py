"""Canonical production entry point for ORCA Whale Options Bot.

This module keeps the approved production implementation in
orca_whale_options_bot_FAST_60S_5S.py intact and replaces only the Phase 1
scanner/selection behaviors.
"""

import json
import io
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests

import orca_whale_options_bot_FAST_60S_5S as bot


SCAN_IN_PROGRESS_LOCK = threading.Lock()
CARD_RENDER_LOCK = threading.Lock()
THREAD_START_LOCK = threading.Lock()
_STARTED_THREADS = {"scanner": None, "monitor": None}
_DUPLICATE_THREAD_START_PREVENTED = False
_LAST_SUCCESSFUL_STATE_SAVE = None
_STATE_LOAD_STATUS = "not loaded"
_RESTORED_ACTIVE_TRADE_COUNT = 0
_RECOVERY_REQUIRED_COUNT = 0
_LAST_SCAN_DECISIONS = {}


class TradeStateCorruptionError(RuntimeError):
    """Raised rather than silently replacing a malformed durable trade file."""


def trade_state_path():
    """Return the configured durable path, retaining the historical default."""
    configured = os.getenv("TRADE_STATE_PATH", "").strip()
    return Path(configured).expanduser() if configured else Path(bot.__file__).with_name(
        "reef_trade_state.json"
    )


def _load_durable_trade_store():
    """Load the old JSON shape without ever treating corrupt data as empty."""
    global _STATE_LOAD_STATUS
    path = trade_state_path()
    if not path.exists():
        _STATE_LOAD_STATUS = "missing (initialized empty)"
        return {"trades": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        _STATE_LOAD_STATUS = f"corrupt: {error}"
        raise TradeStateCorruptionError(
            f"trade state file is corrupt and was not discarded: {path}"
        ) from error
    if not isinstance(state, dict):
        _STATE_LOAD_STATUS = "corrupt: root is not a JSON object"
        raise TradeStateCorruptionError(
            f"trade state file is corrupt and was not discarded: {path}"
        )
    if isinstance(state.get("trades"), dict):
        _STATE_LOAD_STATUS = "loaded"
        return state
    # Compatibility with the pre-multi-symbol single trade state format.
    if state.get("symbol"):
        symbol = str(state["symbol"]).upper().strip()
        if symbol:
            _STATE_LOAD_STATUS = "loaded (legacy single trade)"
            return {"trades": {symbol: state}}
    if state:
        _STATE_LOAD_STATUS = "corrupt: missing trades object"
        raise TradeStateCorruptionError(
            f"trade state file has an invalid structure and was not discarded: {path}"
        )
    _STATE_LOAD_STATUS = "loaded"
    return {"trades": {}}


def _save_durable_trade_store(store):
    """Durably replace state using a same-directory temporary file and rename."""
    global _LAST_SUCCESSFUL_STATE_SAVE
    if not isinstance(store, dict):
        raise ValueError("trade state must be an object")
    if not isinstance(store.get("trades"), dict):
        raise ValueError("trade state must contain a trades object")
    path = trade_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(store, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _LAST_SUCCESSFUL_STATE_SAVE = bot.now_new_york().isoformat(timespec="seconds")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def restore_durable_trade_state():
    """Classify crash leftovers before threads are allowed to create new trades."""
    global _RESTORED_ACTIVE_TRADE_COUNT, _RECOVERY_REQUIRED_COUNT
    with bot.TRADE_STATE_LOCK:
        store = _load_durable_trade_store()
        changed = False
        active = 0
        required = 0
        for symbol, trade in list(store["trades"].items()):
            if not isinstance(trade, dict):
                raise TradeStateCorruptionError(f"invalid trade record for {symbol}")
            status = trade.get("status")
            if status == "active" and trade.get("contract_ticker") and trade.get("message_id"):
                active += 1
            elif status == "opening":
                # A reservation made before Telegram is definitely unsent; old
                # opening records and post-send failures are intentionally not
                # retried, because retrying could duplicate a Telegram alert.
                if trade.get("opening_phase") == "reserved":
                    trade.update(status="cancelled", recovery_note="restart: reservation was never sent")
                else:
                    trade.update(status="recovery_required",
                                 recovery_note="restart: opening state may have sent Telegram")
                    required += 1
                store["trades"][symbol] = trade
                changed = True
            elif status == "active" and not trade.get("message_id"):
                trade.update(status="recovery_required",
                             recovery_note="restart: Telegram send outcome is ambiguous")
                store["trades"][symbol] = trade
                changed = True
                required += 1
            elif status == "recovery_required":
                required += 1
        if changed:
            _save_durable_trade_store(store)
        _RESTORED_ACTIVE_TRADE_COUNT = active
        _RECOVERY_REQUIRED_COUNT = required
        return {"restored_active": active, "recovery_required": required}

# These deliberately describe data freshness, not the monitor cadence.  Quotes
# are short lived; an option print is allowed slightly longer because thin
# contracts do not necessarily trade on every quote update.
QUOTE_FRESH_SECONDS = float(os.getenv("MASSIVE_QUOTE_FRESH_SECONDS", "30"))
TRADE_FRESH_SECONDS = float(os.getenv("MASSIVE_TRADE_FRESH_SECONDS", "90"))
TELEGRAM_EDIT_ATTEMPTS = max(1, int(os.getenv("TELEGRAM_EDIT_ATTEMPTS", "3")))
TELEGRAM_EDIT_RETRY_SECONDS = float(os.getenv("TELEGRAM_EDIT_RETRY_SECONDS", "1"))
_ORIGINAL_RENDER_TRADE_CARD = bot.render_trade_card

# Phase 4 contract-quality defaults. Component maxima total 100 points:
# delta 20, volume 12, OI 12, spread 14, strike distance 12, DTE 10,
# theta/premium 8, IV 7, and completeness/freshness 5.
SCORE_DELTA_PREFERRED_MIN = float(os.getenv("SCORE_DELTA_PREFERRED_MIN", "0.35"))
SCORE_DELTA_PREFERRED_MAX = float(os.getenv("SCORE_DELTA_PREFERRED_MAX", "0.65"))
SCORE_DELTA_EXTREME_LOW = float(os.getenv("SCORE_DELTA_EXTREME_LOW", "0.10"))
SCORE_DELTA_EXTREME_HIGH = float(os.getenv("SCORE_DELTA_EXTREME_HIGH", "0.90"))
SCORE_MIN_OPEN_INTEREST = max(0.0, float(os.getenv("SCORE_MIN_OPEN_INTEREST", "100")))
SCORE_MIN_OPTION_VOLUME = max(0.0, float(os.getenv("SCORE_MIN_OPTION_VOLUME", "50")))
SCORE_MAX_DTE = max(0, int(os.getenv("SCORE_MAX_DTE", "14")))
SCORE_DTE_0_POINTS = float(os.getenv("SCORE_DTE_0_POINTS", "7"))
SCORE_DTE_1_3_POINTS = float(os.getenv("SCORE_DTE_1_3_POINTS", "10"))
SCORE_DTE_4_7_POINTS = float(os.getenv("SCORE_DTE_4_7_POINTS", "7"))
SCORE_DTE_LONGER_POINTS = float(os.getenv("SCORE_DTE_LONGER_POINTS", "3"))
SCORE_0DTE_MIN_REMAINING_MINUTES = float(
    os.getenv("SCORE_0DTE_MIN_REMAINING_MINUTES", "90")
)
SCORE_0DTE_MIN_DELTA = float(os.getenv("SCORE_0DTE_MIN_DELTA", "0.40"))
SCORE_0DTE_MIN_OPEN_INTEREST = max(
    SCORE_MIN_OPEN_INTEREST, float(os.getenv("SCORE_0DTE_MIN_OPEN_INTEREST", "500"))
)
SCORE_0DTE_MIN_OPTION_VOLUME = max(
    SCORE_MIN_OPTION_VOLUME, float(os.getenv("SCORE_0DTE_MIN_OPTION_VOLUME", "200"))
)
SCORE_0DTE_MAX_SPREAD_PERCENT = float(
    os.getenv("SCORE_0DTE_MAX_SPREAD_PERCENT", "15")
)
GAMMA_MAX_DTE = max(0, int(os.getenv("GAMMA_MAX_DTE", "14")))
GAMMA_FLIP_MAX_RANGE_PCT = max(
    float(getattr(bot, "GAMMA_FLIP_RANGE_PCT", 12.0)),
    float(os.getenv("GAMMA_FLIP_MAX_RANGE_PCT", "50")),
)
GAMMA_MIN_COMPLETENESS_PERCENT = max(
    0.0, min(100.0, float(os.getenv("GAMMA_MIN_COMPLETENESS_PERCENT", "40")))
)
GAMMA_NEAR_ZERO_ABS = max(0.0, float(os.getenv("GAMMA_NEAR_ZERO_ABS", "1")))
ORCA_CONSERVATIVE_MODE = os.getenv(
    "ORCA_CONSERVATIVE_MODE", "1"
).strip().lower() not in ("0", "false", "no")
ORCA_CONSERVATIVE_MIN_SCORE = float(
    os.getenv("ORCA_CONSERVATIVE_MIN_SCORE", "82")
)
ORCA_MIN_DIRECTION_EDGE = max(
    0.0, float(os.getenv("ORCA_MIN_DIRECTION_EDGE", "8"))
)
CONSERVATIVE_MIN_COMPLETENESS = 83.0
CONSERVATIVE_HIGH_CONFIDENCE = 82.0
CONSERVATIVE_MEDIUM_CONFIDENCE = 65.0


def _present_number(value):
    """Preserve missing-versus-zero semantics while accepting numeric JSON."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_contract(raw, stock_price):
    """Normalize a Massive candidate without inventing missing quality data."""
    details = raw.get("details", {}) or {}
    day = raw.get("day", {}) or {}
    greeks = raw.get("greeks", {}) or {}
    quote = raw.get("last_quote", {}) or {}
    trade = raw.get("last_trade", {}) or {}
    strike = bot.safe_float(details.get("strike_price"))
    bid = _present_number(quote.get("bid") if quote.get("bid") is not None else quote.get("bid_price"))
    ask = _present_number(quote.get("ask") if quote.get("ask") is not None else quote.get("ask_price"))
    quote_stamp = _newest_timestamp([
        quote.get("last_updated"), quote.get("sip_timestamp"),
        quote.get("participant_timestamp"),
    ])
    trade_stamp = _newest_timestamp([
        trade.get("sip_timestamp"), trade.get("participant_timestamp"),
        trade.get("timestamp"),
    ])
    quote_age = max(0.0, time.time() - quote_stamp) if quote_stamp else None
    trade_age = max(0.0, time.time() - trade_stamp) if trade_stamp else None
    quote_fresh = quote_age is not None and quote_age <= QUOTE_FRESH_SECONDS
    spread = None
    midpoint = None
    if quote_fresh and bid is not None and ask is not None and bid > 0 and ask >= bid:
        midpoint = (bid + ask) / 2.0
        if midpoint > 0:
            spread = ((ask - bid) / midpoint) * 100.0
    return {
        "ticker": str(details.get("ticker", "")).replace("O:", ""),
        "expiration": details.get("expiration_date", ""),
        "strike": strike,
        "distance": abs(strike - stock_price),
        "open_interest": _present_number(raw.get("open_interest")),
        "volume": _present_number(day.get("volume")),
        "bid": bid,
        "ask": ask,
        "midpoint": midpoint,
        "last": _present_number(trade.get("price")),
        "day_close": _present_number(day.get("close")),
        "day_open": _present_number(day.get("open")),
        "spread_percent": spread,
        "quote_age_seconds": quote_age,
        "trade_age_seconds": trade_age,
        "quote_fresh": quote_fresh,
        "trade_fresh": trade_age is not None and trade_age <= TRADE_FRESH_SECONDS,
        "delta": _present_number(greeks.get("delta")),
        "theta": _present_number(greeks.get("theta")),
        "iv": _present_number(raw.get("implied_volatility")),
    }


def _contract_dte(expiration, now=None):
    now = now or bot.now_new_york()
    try:
        return (bot.datetime.strptime(str(expiration), "%Y-%m-%d").date() - now.date()).days
    except (TypeError, ValueError):
        return None


def _remaining_regular_session_minutes(now=None):
    now = now or bot.now_new_york()
    close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return max(0.0, (close - now).total_seconds() / 60.0)


def _progressive_points(value, minimum, full_at, maximum):
    if value is None or value <= 0:
        return 0.0
    if value < minimum:
        return maximum * 0.15 * (value / minimum) if minimum > 0 else 0.0
    if full_at <= minimum:
        return maximum
    ratio = min(1.0, (value - minimum) / (full_at - minimum))
    return maximum * (0.35 + 0.65 * ratio)


def calculate_score(contract, stock_price, now=None):
    """Attach a transparent, bounded quality score and eligibility decision."""
    now = now or bot.now_new_york()
    delta = contract.get("delta")
    ad = abs(delta) if delta is not None else None
    oi = contract.get("open_interest")
    volume = contract.get("volume")
    spread = contract.get("spread_percent")
    iv = contract.get("iv")
    theta = contract.get("theta")
    dte = _contract_dte(contract.get("expiration"), now)
    price = bot.safe_float(contract.get("midpoint") or contract.get("last"))

    if ad is None:
        delta_points = 0.0
    elif SCORE_DELTA_PREFERRED_MIN <= ad <= SCORE_DELTA_PREFERRED_MAX:
        center = (SCORE_DELTA_PREFERRED_MIN + SCORE_DELTA_PREFERRED_MAX) / 2.0
        half = max((SCORE_DELTA_PREFERRED_MAX - SCORE_DELTA_PREFERRED_MIN) / 2.0, 0.01)
        delta_points = 18.0 + 2.0 * max(0.0, 1.0 - abs(ad - center) / half)
    elif ad < SCORE_DELTA_PREFERRED_MIN:
        delta_points = 18.0 * max(0.0, (ad - SCORE_DELTA_EXTREME_LOW) /
                                  max(SCORE_DELTA_PREFERRED_MIN - SCORE_DELTA_EXTREME_LOW, 0.01))
    else:
        delta_points = 18.0 * max(0.0, (SCORE_DELTA_EXTREME_HIGH - ad) /
                                  max(SCORE_DELTA_EXTREME_HIGH - SCORE_DELTA_PREFERRED_MAX, 0.01))

    volume_points = _progressive_points(volume, SCORE_MIN_OPTION_VOLUME, 2500.0, 12.0)
    oi_points = _progressive_points(oi, SCORE_MIN_OPEN_INTEREST, 5000.0, 12.0)
    if spread is None:
        spread_points = 0.0
    elif spread <= 5:
        spread_points = 14.0
    elif spread <= 10:
        spread_points = 11.0
    elif spread <= 20:
        spread_points = 7.0
    elif spread <= 30:
        spread_points = 3.0
    else:
        spread_points = 0.0

    distance_pct = (bot.safe_float(contract.get("distance")) / stock_price * 100.0
                    if stock_price > 0 else 100.0)
    distance_points = 12.0 * max(0.0, 1.0 - distance_pct / 5.0)
    if dte == 0:
        dte_points = SCORE_DTE_0_POINTS
    elif dte is not None and 1 <= dte <= 3:
        dte_points = SCORE_DTE_1_3_POINTS
    elif dte is not None and 4 <= dte <= 7:
        dte_points = SCORE_DTE_4_7_POINTS
    elif dte is not None and 8 <= dte <= SCORE_MAX_DTE:
        dte_points = SCORE_DTE_LONGER_POINTS
    else:
        dte_points = 0.0

    theta_ratio = abs(theta) / price if theta is not None and price > 0 else None
    if theta_ratio is None:
        theta_points = 0.0
    elif theta_ratio <= 0.03:
        theta_points = 8.0
    elif theta_ratio <= 0.07:
        theta_points = 6.0
    elif theta_ratio <= 0.12:
        theta_points = 3.0
    else:
        theta_points = 0.0

    # Soft, scale-aware IV treatment: normal/high-volatility names remain viable,
    # while increasingly inflated premium receives progressively fewer points.
    if iv is None or iv <= 0:
        iv_points = 0.0
    elif iv <= 0.80:
        iv_points = 7.0
    elif iv <= 1.50:
        iv_points = 5.5
    elif iv <= 2.50:
        iv_points = 3.0
    else:
        iv_points = 1.0

    critical = {
        "delta": delta is not None,
        "open_interest": oi is not None,
        "volume": volume is not None,
    }
    optional = {
        "fresh_bid_ask": spread is not None,
        "iv": iv is not None,
        "theta": theta is not None,
    }
    completeness_points = 3.0 * (sum(critical.values()) / len(critical))
    completeness_points += 2.0 * (sum(optional.values()) / len(optional))
    breakdown = {
        "delta": round(delta_points, 2),
        "volume": round(volume_points, 2),
        "open_interest": round(oi_points, 2),
        "spread": round(spread_points, 2),
        "strike_distance": round(distance_points, 2),
        "dte": round(max(0.0, min(dte_points, 10.0)), 2),
        "theta": round(theta_points, 2),
        "iv": round(iv_points, 2),
        "freshness_completeness": round(completeness_points, 2),
    }
    rejection_reasons = []
    if dte is None or dte < 0 or dte > SCORE_MAX_DTE:
        rejection_reasons.append(f"DTE {dte} outside 0-{SCORE_MAX_DTE}")
    if delta is None:
        rejection_reasons.append("Delta unavailable")
    if oi is None:
        rejection_reasons.append("open interest unavailable")
    elif oi < SCORE_MIN_OPEN_INTEREST:
        rejection_reasons.append(f"OI {oi:.0f} < {SCORE_MIN_OPEN_INTEREST:.0f}")
    if volume is None:
        rejection_reasons.append("volume unavailable")
    elif volume < SCORE_MIN_OPTION_VOLUME:
        rejection_reasons.append(f"volume {volume:.0f} < {SCORE_MIN_OPTION_VOLUME:.0f}")
    if dte == 0:
        remaining = _remaining_regular_session_minutes(now)
        if remaining < SCORE_0DTE_MIN_REMAINING_MINUTES:
            rejection_reasons.append(
                f"0DTE cutoff: {remaining:.0f} < {SCORE_0DTE_MIN_REMAINING_MINUTES:.0f} minutes"
            )
        if ad is None or ad < SCORE_0DTE_MIN_DELTA:
            rejection_reasons.append("0DTE Delta below stricter minimum")
        if oi is None or oi < SCORE_0DTE_MIN_OPEN_INTEREST:
            rejection_reasons.append("0DTE OI below stricter minimum")
        if volume is None or volume < SCORE_0DTE_MIN_OPTION_VOLUME:
            rejection_reasons.append("0DTE volume below stricter minimum")
        if spread is None:
            rejection_reasons.append("0DTE fresh spread unavailable")
        elif spread > SCORE_0DTE_MAX_SPREAD_PERCENT:
            rejection_reasons.append("0DTE spread above stricter maximum")
    score = round(max(0.0, min(100.0, sum(breakdown.values()))), 1)
    contract.update(
        score=score,
        score_breakdown=breakdown,
        dte=dte,
        critical_inputs=critical,
        optional_inputs=optional,
        completeness_percent=round(
            100.0 * (sum(critical.values()) + sum(optional.values())) /
            (len(critical) + len(optional)), 1
        ),
        selection_eligible=not rejection_reasons,
        rejection_reasons=rejection_reasons,
    )
    return score


class ScoredCandidateList(list):
    """Eligible candidates plus diagnostics for every scored alternative."""

    def __init__(self, eligible, all_candidates):
        super().__init__(eligible)
        self.all_candidates = all_candidates


def _candidate_diagnostic(contract, winner_score):
    return {
        "ticker": contract.get("ticker"),
        "expiration": contract.get("expiration"),
        "dte": contract.get("dte"),
        "score": contract.get("score"),
        "points_behind_winner": round(
            max(0.0, winner_score - bot.safe_float(contract.get("score"))), 1
        ),
        "eligible": bool(contract.get("selection_eligible")),
        "rejection_reasons": contract.get("rejection_reasons") or [],
        "critical_inputs": contract.get("critical_inputs") or {},
        "optional_inputs": contract.get("optional_inputs") or {},
        "completeness_percent": contract.get("completeness_percent"),
        "score_breakdown": contract.get("score_breakdown") or {},
    }


def _prepare_side(raw_chain, stock_price):
    """Score all near-term expirations so safer 1-3 DTE can outrank 0DTE."""
    all_candidates = []
    for raw in raw_chain or []:
        contract = normalize_contract(raw, stock_price)
        if not contract["ticker"] or not contract["expiration"]:
            continue
        calculate_score(contract, stock_price)
        all_candidates.append(contract)
    all_candidates.sort(key=lambda item: (
        -item["score"], item["distance"],
        -(item.get("volume") or 0), -(item.get("open_interest") or 0),
    ))
    eligible = [item for item in all_candidates if item["selection_eligible"]]
    eligible.sort(key=lambda item: (
        -item["score"], item["distance"],
        -(item.get("volume") or 0), -(item.get("open_interest") or 0),
    ))
    candidates = ScoredCandidateList(eligible, all_candidates)
    return candidates, eligible[0]["expiration"] if eligible else None


def _side_flow_stats(contracts):
    """Preserve flow qualification while exposing every candidate's score."""
    eligible = list(contracts or [])
    near = sorted(
        eligible,
        key=lambda item: (
            item["distance"], -(item.get("volume") or 0),
            -(item.get("open_interest") or 0),
        ),
    )[:12]
    best = eligible[0] if eligible else None
    all_candidates = getattr(contracts, "all_candidates", eligible)
    winner_score = bot.safe_float((best or {}).get("score"))
    return {
        "volume": sum(bot.safe_float(item.get("volume")) for item in near),
        "open_interest": sum(bot.safe_float(item.get("open_interest")) for item in near),
        "best": best,
        "candidate_scores": [
            _candidate_diagnostic(item, winner_score) for item in all_candidates
        ],
        "candidate_count": len(all_candidates),
        "eligible_candidate_count": len(eligible),
    }


def choose_initial_price(contract):
    """Use only a fresh midpoint or recent trade as an executable entry mark."""
    midpoint = bot.safe_float(contract.get("midpoint"))
    if contract.get("quote_fresh") and midpoint > 0:
        return midpoint, "Fresh Bid/Ask Midpoint"
    last = bot.safe_float(contract.get("last"))
    if contract.get("trade_fresh") and last > 0:
        return last, "Recent Trade (Lower Confidence)"
    return 0.0, "Unavailable"


def _confidence_class(value):
    if value >= CONSERVATIVE_HIGH_CONFIDENCE:
        return "HIGH"
    if value >= CONSERVATIVE_MEDIUM_CONFIDENCE:
        return "MEDIUM"
    return "LOW"


def calculate_trade_confidence(contract, direction_edge, gamma_state, entry_source):
    """Combine independent evidence without replacing the Phase 4 score."""
    contract_score = bot.safe_float(contract.get("score"))
    completeness = bot.safe_float(contract.get("completeness_percent"))
    edge_denominator = max(ORCA_MIN_DIRECTION_EDGE * 2.0, 1.0)
    edge_quality = min(
        100.0, max(0.0, direction_edge) / edge_denominator * 100.0
    )
    gamma_quality = {"confirmed": 100.0, "unavailable": 55.0, "opposed": 0.0}.get(
        gamma_state, 0.0
    )
    entry_quality = {
        "Fresh Bid/Ask Midpoint": 100.0,
        "Recent Trade (Lower Confidence)": 65.0,
    }.get(entry_source, 0.0)
    spread_quality = 100.0 if contract.get("spread_percent") is not None else 0.0
    components = {
        "contract_score": round(contract_score * 0.45, 2),
        "data_completeness": round(completeness * 0.20, 2),
        "direction_edge": round(edge_quality * 0.15, 2),
        "gamma_state": round(gamma_quality * 0.10, 2),
        "entry_price_quality": round(entry_quality * 0.07, 2),
        "spread_availability": round(spread_quality * 0.03, 2),
    }
    value = round(max(0.0, min(100.0, sum(components.values()))), 1)
    return {
        "score": value,
        "level": _confidence_class(value),
        "components": components,
    }


def _conservative_assessment(
    direction, contract, direction_edge, gamma_info, stock_price
):
    """Return explicit quality/price WAIT decisions for one exact candidate."""
    if not contract:
        return {
            "can_open": False, "wait_status": "WAIT - DATA INCOMPLETE",
            "wait_reasons": ["WAIT - DATA INCOMPLETE"],
            "trade_confidence": {"score": 0.0, "level": "LOW", "components": {}},
            "entry_price": 0.0, "entry_source": "Unavailable",
        }
    gamma_state = gamma_direction_state(direction, stock_price, gamma_info)
    entry, entry_source = choose_initial_price(contract)
    reasons = []
    score = bot.safe_float(contract.get("score"))
    dte = contract.get("dte")
    theta_points = bot.safe_float((contract.get("score_breakdown") or {}).get("theta"))
    if score < ORCA_CONSERVATIVE_MIN_SCORE:
        reasons.append("WAIT - SCORE TOO LOW")
    if contract.get("delta") is None:
        reasons.append("WAIT - DATA INCOMPLETE")
    if contract.get("open_interest") is None or bot.safe_float(
        contract.get("open_interest")
    ) < SCORE_MIN_OPEN_INTEREST:
        reasons.append("WAIT - LOW OI")
    if contract.get("volume") is None or bot.safe_float(
        contract.get("volume")
    ) < SCORE_MIN_OPTION_VOLUME:
        reasons.append("WAIT - LOW VOLUME")
    if dte is None or dte < 0 or dte > SCORE_MAX_DTE:
        reasons.append("WAIT - DATA INCOMPLETE")
    if theta_points <= 0 or contract.get("iv") is None:
        reasons.append("WAIT - DATA INCOMPLETE")
    if bot.safe_float(contract.get("completeness_percent")) < CONSERVATIVE_MIN_COMPLETENESS:
        reasons.append("WAIT - DATA INCOMPLETE")
    if direction_edge < ORCA_MIN_DIRECTION_EDGE:
        reasons.append("WAIT - DIRECTION UNCLEAR")
    if gamma_state == "opposed":
        reasons.append("WAIT - GAMMA OPPOSED")
    elif gamma_state == "unavailable" and (
        score < ORCA_CONSERVATIVE_MIN_SCORE + 4.0
        or direction_edge < ORCA_MIN_DIRECTION_EDGE * 1.25
        or bot.safe_float(contract.get("completeness_percent"))
        < CONSERVATIVE_MIN_COMPLETENESS
    ):
        reasons.append("WAIT - GAMMA UNAVAILABLE / EVIDENCE NOT STRONG ENOUGH")
    if dte == 0:
        if contract.get("spread_percent") is None:
            reasons.append("WAIT - SPREAD UNAVAILABLE")
        if any("0DTE cutoff" in reason for reason in contract.get("rejection_reasons") or []):
            reasons.append("WAIT - 0DTE TOO LATE")
    elif contract.get("spread_percent") is None and score < ORCA_CONSERVATIVE_MIN_SCORE + 5:
        reasons.append("WAIT - SPREAD UNAVAILABLE")
    if entry <= 0:
        reasons.append("WAITING FOR PRICE CONFIRMATION")
    confidence = calculate_trade_confidence(
        contract, direction_edge, gamma_state, entry_source
    )
    if confidence["level"] != "HIGH":
        reasons.append("WAIT - CONFIDENCE NOT HIGH")
    reasons = list(dict.fromkeys(reasons))
    return {
        "can_open": not reasons,
        "wait_status": reasons[0] if reasons else "TRADE",
        "wait_reasons": reasons,
        "trade_confidence": confidence,
        "entry_price": entry,
        "entry_source": entry_source,
        "gamma_direction_state": gamma_state,
    }


def _save_pending_candidate(symbol, direction, contract, assessment):
    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        pending = store.setdefault("pending_candidates", {})
        pending[symbol] = {
            "symbol": symbol,
            "direction": direction,
            "contract_ticker": contract.get("ticker"),
            "contract": contract,
            "wait_status": assessment.get("wait_status"),
            "wait_reasons": assessment.get("wait_reasons") or [],
            "trade_confidence": assessment.get("trade_confidence"),
            "updated_at": bot.now_new_york().isoformat(timespec="seconds"),
        }
        bot._save_trade_store(store)


def _clear_pending_candidate(symbol):
    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        pending = store.get("pending_candidates")
        if isinstance(pending, dict) and symbol in pending:
            pending.pop(symbol, None)
            bot._save_trade_store(store)


def _promote_pending_candidate(candidates, pending):
    ticker = str((pending or {}).get("contract_ticker") or "")
    direction = str((pending or {}).get("direction") or "")
    if not ticker:
        return candidates
    for index, candidate in enumerate(candidates):
        if candidate.get("ticker") == ticker:
            if index:
                candidates.insert(0, candidates.pop(index))
            break
    return candidates


def _paper_trade_record(state):
    return {
        "id": state.get("paper_trade_id"),
        "symbol": state.get("symbol"),
        "exact_contract": state.get("contract_ticker"),
        "direction": state.get("direction"),
        "entry_timestamp": state.get("created_at"),
        "entry_price": state.get("entry_price"),
        "score": state.get("score"),
        "confidence": state.get("trade_confidence"),
        "confidence_score": state.get("trade_confidence_score"),
        "gamma_state": state.get("gamma_direction_state"),
        "dte": state.get("dte"),
        "highest_price": state.get("entry_price"),
        "lowest_price": state.get("entry_price"),
        "targets_reached": [False, False, False, False, False],
        "stop_reached": False,
        "time_to_first_target_seconds": None,
        "final_status": "active",
    }


def _update_paper_trade(store, state, previous_achieved):
    records = store.setdefault("paper_trades", [])
    record = next(
        (item for item in records if item.get("id") == state.get("paper_trade_id")),
        None,
    )
    if record is None:
        return
    achieved = int(state.get("achieved", 0))
    record["highest_price"] = state.get("highest_price")
    record["lowest_price"] = state.get("lowest_price")
    record["targets_reached"] = [index < achieved for index in range(5)]
    if previous_achieved == 0 and achieved > 0 and record.get(
        "time_to_first_target_seconds"
    ) is None:
        try:
            start = bot.datetime.fromisoformat(str(record.get("entry_timestamp")))
            record["time_to_first_target_seconds"] = max(
                0.0, (bot.now_new_york() - start).total_seconds()
            )
        except (TypeError, ValueError):
            record["time_to_first_target_seconds"] = None
    record["stop_reached"] = state.get("close_reason") == "STOP_LOSS"
    record["final_status"] = state.get("status")


def paper_performance_summary(store=None):
    if store is None:
        with bot.TRADE_STATE_LOCK:
            store = bot._load_trade_store()
    records = [
        item for item in (store.get("paper_trades") or [])
        if isinstance(item, dict)
    ]
    finalized = [
        item for item in records
        if item.get("final_status") in ("completed", "stopped")
    ]
    total = len(finalized)
    winners = sum(any(item.get("targets_reached") or []) for item in finalized)
    stopped = sum(item.get("stop_reached") is True for item in finalized)
    target_rates = []
    for index in range(5):
        hits = sum(
            bool((item.get("targets_reached") or [False] * 5)[index])
            for item in finalized
        )
        target_rates.append(round(100.0 * hits / total, 1) if total else 0.0)
    favorable, adverse = [], []
    for item in finalized:
        entry = bot.safe_float(item.get("entry_price"))
        if entry > 0:
            favorable.append(
                (bot.safe_float(item.get("highest_price"), entry) / entry - 1.0) * 100.0
            )
            adverse.append(
                (bot.safe_float(item.get("lowest_price"), entry) / entry - 1.0) * 100.0
            )
    return {
        "total_sent_paper_trades": len(records),
        "total_completed_paper_trades": total,
        "winners": winners,
        "stopped_trades": stopped,
        "target_hit_percentages": {
            f"+{percent}%": target_rates[index]
            for index, percent in enumerate((20, 45, 70, 100, 150))
        },
        "average_maximum_favorable_excursion_percent": round(
            sum(favorable) / len(favorable), 2
        ) if favorable else 0.0,
        "average_maximum_adverse_excursion_percent": round(
            sum(adverse) / len(adverse), 2
        ) if adverse else 0.0,
        "profitability_claim": (
            "INSUFFICIENT SAMPLE" if total < 30
            else "No profitability claim; paper outcomes only"
        ),
    }


def _unix_seconds(value):
    """Normalize Massive seconds/ms/us/ns timestamps to Unix seconds."""
    if value in (None, ""):
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    # Epoch values currently have 10, 13, 16, or 19 digits.  Magnitude avoids
    # string-format assumptions and also accepts numeric JSON values.
    if stamp >= 1e17:
        stamp /= 1e9
    elif stamp >= 1e14:
        stamp /= 1e6
    elif stamp >= 1e11:
        stamp /= 1e3
    return stamp


def _newest_timestamp(values):
    values = [_unix_seconds(value) for value in values]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _snapshot_data(snapshot):
    results = (snapshot or {}).get("results") if isinstance(snapshot, dict) else None
    if isinstance(results, list):
        return results[0] if results else {}
    return results if isinstance(results, dict) else (snapshot if isinstance(snapshot, dict) else {})


def classify_snapshot_price(snapshot, now=None):
    """Return only a genuinely current quote/trade; never promote day data."""
    data = _snapshot_data(snapshot)
    quote, trade = data.get("last_quote") or {}, data.get("last_trade") or {}
    session, day = data.get("session") or {}, data.get("day") or {}
    now = time.time() if now is None else float(now)
    quote_stamp = _newest_timestamp([
        quote.get("last_updated"), quote.get("sip_timestamp"),
        quote.get("participant_timestamp"),
    ])
    trade_stamp = _newest_timestamp([
        trade.get("sip_timestamp"), trade.get("participant_timestamp"),
        trade.get("timestamp"),
    ])
    quote_age = max(0.0, now - quote_stamp) if quote_stamp else None
    trade_age = max(0.0, now - trade_stamp) if trade_stamp else None
    bid = bot.safe_float(quote.get("bid") or quote.get("bid_price"))
    ask = bot.safe_float(quote.get("ask") or quote.get("ask_price"))
    midpoint = bot.safe_float(quote.get("midpoint"))
    trade_price = bot.safe_float(trade.get("price"))
    quote_fresh = quote_age is not None and quote_age <= QUOTE_FRESH_SECONDS
    trade_fresh = trade_age is not None and trade_age <= TRADE_FRESH_SECONDS

    if quote_fresh and midpoint > 0:
        return midpoint, "fresh quote midpoint", quote_age, quote_age, trade_age
    if quote_fresh and bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2, "fresh calculated bid/ask midpoint", quote_age, quote_age, trade_age
    if quote_fresh and bid > 0:
        return bid, "fresh one-sided quote", quote_age, quote_age, trade_age
    if quote_fresh and ask > 0:
        return ask, "fresh one-sided quote", quote_age, quote_age, trade_age
    if trade_fresh and trade_price > 0:
        return trade_price, "recent trade", trade_age, quote_age, trade_age
    if (bid > 0 or ask > 0 or midpoint > 0) and quote_age is not None:
        return 0.0, "stale quote", quote_age, quote_age, trade_age
    if trade_price > 0 and trade_age is not None:
        return 0.0, "stale trade", trade_age, quote_age, trade_age
    if any(bot.safe_float(source.get(key)) > 0 for source in (session, day)
           for key in ("open", "close")):
        return 0.0, "session/day fallback", None, quote_age, trade_age
    return 0.0, "unavailable", None, quote_age, trade_age


def _card_fitted_font(draw, text, maximum_size, maximum_width, minimum_size=16, bold=True):
    """Return the largest existing card font that keeps one line inside its area."""
    value = str(text)
    for size in range(maximum_size, minimum_size - 1, -1):
        font = bot._font(size, bold)
        bounds = draw.textbbox((0, 0), value, font=font)
        if bounds[2] - bounds[0] <= maximum_width:
            return font
    return bot._font(minimum_size, bold)


def _card_gamma_values(state):
    """Format only canonical Phase 5 Gamma results for the card."""
    raw_status = str(
        state.get("gamma_status") or state.get("gamma_regime") or "INSUFFICIENT DATA"
    ).strip().upper()
    regime = str(state.get("gamma_regime") or "").strip().upper()
    flip = _present_number(state.get("gamma_flip"))
    stock_price = _present_number(
        state.get("gamma_stock_price")
        if state.get("gamma_stock_price") is not None
        else state.get("stock_price")
    )

    if raw_status == "FOUND" and flip is not None and flip > 0:
        position = ""
        if stock_price is not None and stock_price > 0:
            position = (
                "ABOVE FLIP" if stock_price > flip
                else "BELOW FLIP" if stock_price < flip
                else "AT FLIP"
            )
        status_parts = [
            value for value in (regime if regime not in ("", "FOUND") else "", position)
            if value
        ]
        return f"${flip:.2f}", " • ".join(status_parts) or "FOUND"

    if raw_status == "NO FLIP IN RANGE":
        gamma_status = regime if regime not in ("", "FOUND") else "NO FLIP IN RANGE"
        return "NO FLIP IN RANGE", gamma_status

    if raw_status in ("INSUFFICIENT DATA", "INSUFFICIENT"):
        return "INSUFFICIENT DATA", "INSUFFICIENT DATA"

    if raw_status == "CALCULATION UNAVAILABLE":
        return "INSUFFICIENT DATA", "INSUFFICIENT DATA"

    return "INSUFFICIENT DATA", "INSUFFICIENT DATA"


def _paste_card_logo(image, symbol, box):
    """Fill the existing logo square while preserving the downloaded logo ratio."""
    x1, y1, x2, y2 = box
    inner_width = max(1, x2 - x1 - 8)
    inner_height = max(1, y2 - y1 - 8)
    logo = bot.fetch_company_logo(symbol, size=max(inner_width, inner_height))
    if logo is None:
        draw = bot.ImageDraw.Draw(image)
        label = str(symbol).upper()
        font = _card_fitted_font(draw, label, 34, inner_width, 18, True)
        bounds = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (
                x1 + ((x2 - x1) - (bounds[2] - bounds[0])) // 2,
                y1 + ((y2 - y1) - (bounds[3] - bounds[1])) // 2 - bounds[1],
            ),
            label,
            font=font,
            fill=(242, 247, 250),
        )
        return
    logo.thumbnail((inner_width, inner_height), bot.Image.Resampling.LANCZOS)
    x = x1 + ((x2 - x1) - logo.width) // 2
    y = y1 + ((y2 - y1) - logo.height) // 2
    image.paste(logo, (x, y), logo)


def render_trade_card(state):
    """Render the approved 1080×1500 ORCA Telegram card from existing trade state."""
    width, height = 1080, 1500
    background = (5, 17, 29)
    panel = (8, 25, 40)
    detail_panel = (12, 34, 52)
    line = (38, 63, 82)
    white = (242, 247, 250)
    muted = (157, 174, 188)
    blue = (73, 171, 255)
    green = (45, 220, 90)
    red = (255, 69, 69)

    image = bot.Image.new("RGB", (width, height), background)
    draw = bot.ImageDraw.Draw(image)
    for x in range(0, width, 90):
        draw.line((x, 0, x, height), fill=(7, 24, 38), width=1)
    for y in range(0, height, 90):
        draw.line((0, y, width, y), fill=(7, 24, 38), width=1)
    draw.rounded_rectangle(
        (42, 42, width - 42, height - 42),
        radius=36,
        fill=panel,
        outline=(25, 55, 76),
        width=2,
    )

    symbol = str(state["symbol"]).upper()
    company = bot.COMPANY_NAMES.get(symbol, symbol)
    direction = str(state["direction"]).upper()
    direction_color = green if direction == "CALL" else red
    status = str(state.get("status", "active")).lower()
    achieved = max(0, min(5, int(state.get("achieved", 0))))
    entry = bot.safe_float(state["entry_price"])
    current = bot.safe_float(state.get("current_price"), entry)
    change = current - entry
    change_percent = ((current / entry) - 1) * 100 if entry else 0.0
    targets = list(state["targets"])
    stop = bot.safe_float(state["stop_loss"])
    dte = bot._days_to_expiry(state["expiration"])
    contract = bot.canonical_option_ticker(state["contract_ticker"])
    gamma_flip, gamma_status = _card_gamma_values(state)

    draw.text((65, 78), "ORCA WHALE OPTIONS SIGNAL", font=bot._font(39, True), fill=blue)
    badge = "LIVE" if status == "active" else ("STOPPED" if status == "stopped" else "COMPLETED")
    badge_color = green if status == "active" else (red if status == "stopped" else blue)
    draw.rounded_rectangle(
        (840, 76, 980, 126),
        radius=15,
        fill=(12, 62, 37) if status == "active" else (65, 25, 29),
    )
    badge_font = _card_fitted_font(draw, badge, 24, 112, 17, True)
    badge_bounds = draw.textbbox((0, 0), badge, font=badge_font)
    draw.text(
        (910 - (badge_bounds[2] - badge_bounds[0]) / 2, 84),
        badge,
        font=badge_font,
        fill=badge_color,
    )

    logo_box = (75, 158, 250, 333)
    draw.rounded_rectangle(
        logo_box, radius=28, fill=(12, 41, 62), outline=(30, 85, 116), width=2
    )
    _paste_card_logo(image, symbol, logo_box)
    direction_text = f"{direction} ↗" if direction == "CALL" else f"{direction} ↘"
    draw.text((285, 166), direction_text, font=bot._font(55, True), fill=direction_color)
    company_font = _card_fitted_font(draw, company, 30, 690, 19, True)
    draw.text((285, 242), company, font=company_font, fill=white)

    contract_box = (285, 302, 970, 370)
    draw.rounded_rectangle(contract_box, radius=18, fill=detail_panel, outline=line, width=2)
    contract_font = _card_fitted_font(draw, contract, 34, 625, 20, True)
    draw.text((315, 317), contract, font=contract_font, fill=white)
    summary = (
        f"{symbol}  ${bot.safe_float(state['stock_price']):.2f}  {direction}   "
        f"{state['expiration']}  ({dte}DTE)"
    )
    summary_font = _card_fitted_font(draw, summary, 25, 890, 18, False)
    draw.text((75, 402), summary, font=summary_font, fill=muted)

    metric_y = 480
    metrics = [(75, "ENTRY PRICE", entry), (390, "CURRENT PRICE", current), (705, "CHANGE", None)]
    for x, label, value in metrics:
        draw.text((x, metric_y), label, font=bot._font(22, True), fill=blue)
        if label == "CHANGE":
            text = f"{change:+.2f} ({change_percent:+.1f}%)"
            font = _card_fitted_font(draw, text, 40, 300, 25, True)
            draw.text((x, metric_y + 47), text, font=font, fill=green if change_percent >= 0 else red)
        else:
            draw.text((x, metric_y + 47), f"${value:.2f}", font=bot._font(42, True), fill=white)
    draw.line((70, 602, 1010, 602), fill=line, width=2)

    draw.text((75, 632), "PROFIT TARGETS", font=bot._font(31, True), fill=blue)
    percentages = [20, 45, 70, 100, 150]
    target_cells = [
        (75, 690), (390, 690), (705, 690),
        (75, 812), (390, 812),
    ]
    for index, (price, percentage, (x, y)) in enumerate(
        zip(targets, percentages, target_cells)
    ):
        hit = index < achieved
        marker_color = green if hit else blue
        draw.ellipse((x, y + 3, x + 34, y + 37), fill=panel, outline=marker_color, width=3)
        if hit:
            draw.text((x + 7, y + 1), "✓", font=bot._font(25, True), fill=green)
        label_x = x + 54
        draw.text((label_x, y), f"T{index + 1}", font=bot._font(27, True), fill=white)
        price_text = f"${bot.safe_float(price):.2f}"
        draw.text((label_x + 48, y), price_text, font=bot._font(27, True), fill=white)
        draw.text(
            (label_x + 48, y + 40),
            f"+{percentage}%",
            font=bot._font(22, True),
            fill=green,
        )

    draw.rounded_rectangle(
        (705, 807, 970, 905), radius=20, fill=(45, 20, 25), outline=(145, 38, 48), width=2
    )
    draw.text((728, 824), "STOP LOSS", font=bot._font(21, True), fill=red)
    stop_text = f"${stop:.2f}  (-45%)"
    stop_font = _card_fitted_font(draw, stop_text, 29, 220, 22, True)
    draw.text((728, 857), stop_text, font=stop_font, fill=white)

    draw.text((75, 930), f"TARGET PROGRESS   {achieved}/5", font=bot._font(24, True), fill=white)
    progress_points = [105, 315, 525, 735, 945]
    draw.line((105, 990, 945, 990), fill=(93, 111, 126), width=5)
    for index, x in enumerate(progress_points):
        hit = index < achieved
        draw.ellipse(
            (x - 25, 965, x + 25, 1015),
            fill=green if hit else panel,
            outline=green if hit else (130, 151, 167),
            width=4,
        )
        if hit:
            draw.text((x - 10, 969), "✓", font=bot._font(32, True), fill=white)
        draw.text((x - 20, 1028), f"T{index + 1}", font=bot._font(20, True), fill=white)

    draw.rounded_rectangle((70, 1085, 1010, 1355), radius=24, fill=detail_panel)
    details = [
        ("TICKER", symbol),
        ("EXPIRATION", f"{state['expiration']} ({dte}DTE)"),
        ("STRIKE", f"${bot.safe_float(state['strike']):.2f}"),
        ("CONTRACT TYPE", direction),
        ("CONTRACT", contract),
        ("GAMMA FLIP", gamma_flip),
        ("GAMMA STATUS", gamma_status),
    ]
    detail_y = 1100
    for label, value in details:
        draw.text((105, detail_y), label, font=bot._font(19, True), fill=muted)
        value_text = str(value)
        value_font = _card_fitted_font(draw, value_text, 20, 610, 14, True)
        bounds = draw.textbbox((0, 0), value_text, font=value_font)
        draw.text(
            (975 - (bounds[2] - bounds[0]), detail_y),
            value_text,
            font=value_font,
            fill=white,
        )
        detail_y += 34

    draw.text(
        (75, 1392),
        f"Entry {bot.format_entry_time_et(state.get('created_at'))}",
        font=bot._font(19),
        fill=muted,
    )
    draw.text((410, 1392), f"Score {state['score']}/100", font=bot._font(19), fill=muted)
    footer = "Powered by ORCA WHALE OPTIONS"
    footer_font = _card_fitted_font(draw, footer, 19, 390, 15, True)
    footer_bounds = draw.textbbox((0, 0), footer, font=footer_font)
    draw.text((985 - (footer_bounds[2] - footer_bounds[0]), 1392), footer, font=footer_font, fill=blue)

    output = Path(bot.__file__).with_name("reef_trade_card.png")
    image.save(output, "PNG", optimize=True)
    return output


def _render_unique_card(state):
    """Render the canonical card into a safely unique temporary image."""
    unique = Path(bot.__file__).with_name(
        f"reef_trade_card_{state.get('symbol', 'trade')}_{state.get('message_id', 'new')}_{uuid.uuid4().hex}.png"
    )
    with CARD_RENDER_LOCK:
        shared = render_trade_card(state)
        shutil.copy2(shared, unique)
        try:
            Path(shared).unlink()
        except OSError:
            pass
    return unique


def telegram_edit_card_retry(message_id, state, attempts=TELEGRAM_EDIT_ATTEMPTS):
    """Retry the same-message edit and return (success, last_error)."""
    if not message_id:
        return False, "missing Telegram message id"
    error_text = None
    for attempt in range(1, max(1, attempts) + 1):
        image_path = None
        try:
            image_path = _render_unique_card(state)
            token, chat_id = bot.env("TELEGRAM_BOT_TOKEN"), bot.env("TELEGRAM_CHAT_ID")
            if not token or not chat_id:
                raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
            with image_path.open("rb") as image:
                response = requests.post(
                    f"https://api.telegram.org/bot{token}/editMessageMedia",
                    data={"chat_id": chat_id, "message_id": str(message_id),
                          "media": json.dumps({"type": "photo", "media": "attach://photo"})},
                    files={"photo": ("reef_options.png", image, "image/png")}, timeout=20)
            if response.status_code == 400 and "message is not modified" in response.text.lower():
                return True, None
            response.raise_for_status()
            return True, None
        except Exception as error:
            error_text = str(error)
            print(f"Telegram edit attempt {attempt} failed:", error_text)
            if attempt < attempts:
                time.sleep(TELEGRAM_EDIT_RETRY_SECONDS)
        finally:
            if image_path:
                try:
                    image_path.unlink()
                except OSError:
                    pass
    return False, error_text or "Telegram edit failed"


def telegram_send_card(state):
    """Initial card send also owns a unique temporary image."""
    token, chat_id = bot.env("TELEGRAM_BOT_TOKEN"), bot.env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
    image_path = _render_unique_card(state)
    try:
        with image_path.open("rb") as image:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id},
                files={"photo": ("reef_options.png", image, "image/png")}, timeout=20)
        response.raise_for_status()
        return response.json()["result"]["message_id"]
    finally:
        try:
            image_path.unlink()
        except OSError:
            pass


def massive_chain(symbol, direction):
    """Fetch every page of Massive option snapshots for one contract side."""
    api_key = bot.env("MASSIVE_API_KEY")
    if not api_key:
        raise RuntimeError("MASSIVE_API_KEY is missing")

    contract_type = "call" if direction == "CALL" else "put"
    url = f"{bot.BASE_URL}/v3/snapshot/options/{symbol}"
    params = {
        "contract_type": contract_type,
        "limit": 250,
        "apiKey": api_key,
    }
    results = []
    seen_urls = set()

    while url:
        if url in seen_urls:
            raise RuntimeError(f"Massive pagination loop detected for {symbol} {direction}")
        seen_urls.add(url)

        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        page = payload.get("results") or []
        if not isinstance(page, list):
            raise RuntimeError(f"Unexpected Massive chain response for {symbol} {direction}")
        results.extend(page)

        next_url = payload.get("next_url")
        if not next_url:
            break
        url = str(next_url)
        params = {"apiKey": api_key}

    return results


def _gamma_t_years(expiration, now):
    """Return near-term expiry time using New York dates and 0DTE session time."""
    try:
        expiry = bot.datetime.strptime(str(expiration), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None, None
    dte = (expiry - now.date()).days
    if dte < 0 or dte > GAMMA_MAX_DTE:
        return dte, None
    if dte == 0:
        remaining = max(
            5.0,
            (now.replace(hour=16, minute=0, second=0, microsecond=0) - now).total_seconds() / 60.0,
        )
        return dte, remaining / (365.0 * 24.0 * 60.0)
    return dte, max(dte, 0.25) / 365.0


def _prepare_gamma_universe(call_raw, put_raw, now=None):
    """Validate the configured near-term Gamma universe and count every skip."""
    now = now or bot.now_new_york()
    stats = {
        "contracts_considered": 0,
        "contracts_in_universe": 0,
        "contracts_usable": 0,
        "skipped_missing_iv": 0,
        "skipped_missing_oi": 0,
        "skipped_invalid_expiration_or_strike": 0,
        "skipped_outside_dte_universe": 0,
    }
    usable = []
    for chain, sign in ((call_raw or [], 1.0), (put_raw or [], -1.0)):
        for raw in chain:
            stats["contracts_considered"] += 1
            details = raw.get("details", {}) or {}
            strike = _present_number(details.get("strike_price"))
            expiration = details.get("expiration_date")
            dte, t_years = _gamma_t_years(expiration, now)
            if strike is None or strike <= 0 or dte is None:
                stats["skipped_invalid_expiration_or_strike"] += 1
                continue
            if t_years is None:
                stats["skipped_outside_dte_universe"] += 1
                continue
            stats["contracts_in_universe"] += 1
            iv = _present_number(raw.get("implied_volatility"))
            oi = _present_number(raw.get("open_interest"))
            missing_iv = iv is None or iv <= 0
            missing_oi = oi is None or oi <= 0
            if missing_iv:
                stats["skipped_missing_iv"] += 1
            if missing_oi:
                stats["skipped_missing_oi"] += 1
            if missing_iv or missing_oi:
                continue
            usable.append((strike, iv, oi, t_years, sign))
            stats["contracts_usable"] += 1
    denominator = (
        stats["contracts_in_universe"]
        + stats["skipped_invalid_expiration_or_strike"]
    )
    stats["gamma_completeness_percent"] = round(
        100.0 * stats["contracts_usable"] / denominator, 1
    ) if denominator else 0.0
    stats["gamma_confidence"] = (
        "SUFFICIENT"
        if stats["contracts_usable"] > 0
        and stats["gamma_completeness_percent"] >= GAMMA_MIN_COMPLETENESS_PERCENT
        else "INSUFFICIENT"
    )
    stats["gamma_max_dte"] = GAMMA_MAX_DTE
    return usable, stats


def _modeled_gex_at_price(usable, spot):
    """Apply the unchanged historical Black–Scholes Gamma exposure convention."""
    if spot <= 0:
        return None
    total = 0.0
    for strike, iv, oi, t_years, sign in usable:
        gamma = bot._bs_gamma(spot, strike, t_years, iv, bot.GAMMA_RISK_FREE_RATE)
        total += sign * gamma * oi * 100.0 * spot * spot * 0.01
    return total


def net_gamma_exposure_at_price(call_raw, put_raw, spot, now=None):
    """Return modeled, not observed, Net GEX for the configured DTE universe."""
    usable, _ = _prepare_gamma_universe(call_raw, put_raw, now)
    return _modeled_gex_at_price(usable, spot) if usable else None


def _gex_sign(value):
    if value is None:
        return "unavailable"
    if abs(value) <= GAMMA_NEAR_ZERO_ABS:
        return "near-zero"
    return "positive" if value > 0 else "negative"


def _find_gamma_crossings(usable, stock_price, range_pct):
    steps = max(41, min(int(bot.GAMMA_FLIP_STEPS), 301))
    fraction = range_pct / 100.0
    low, high = stock_price * (1.0 - fraction), stock_price * (1.0 + fraction)
    step = (high - low) / (steps - 1)
    points = [
        (low + index * step, _modeled_gex_at_price(usable, low + index * step))
        for index in range(steps)
    ]
    crossings = []
    for (spot1, gex1), (spot2, gex2) in zip(points, points[1:]):
        if gex1 == 0:
            crossings.append(spot1)
        elif gex2 == 0:
            crossings.append(spot2)
        elif gex1 is not None and gex2 is not None and gex1 * gex2 < 0:
            crossings.append(
                spot1 + (0.0 - gex1) * (spot2 - spot1) / (gex2 - gex1)
            )
    return crossings


def calculate_gamma_flip(call_raw, put_raw, stock_price, now=None):
    """Calculate transparent modeled Net GEX and adaptively search for a flip."""
    if stock_price <= 0:
        return {
            "flip": None, "current_gex": None, "modeled_net_gex": None,
            "gex_sign": "unavailable", "regime": "CALCULATION UNAVAILABLE",
            "status": "CALCULATION UNAVAILABLE",
            "gamma_distance_percent": None,
            "gamma_confidence": "UNAVAILABLE",
            "gamma_completeness_percent": 0.0,
        }
    usable, stats = _prepare_gamma_universe(call_raw, put_raw, now)
    current_gex = _modeled_gex_at_price(usable, stock_price) if usable else None
    sign = _gex_sign(current_gex)
    result = {
        **stats,
        "flip": None,
        "current_gex": current_gex,
        "modeled_net_gex": current_gex,
        "gex_sign": sign,
        "regime": sign.upper() if sign != "unavailable" else "INSUFFICIENT DATA",
        "gamma_distance_percent": None,
        "crossings": [],
        "search_ranges_percent": [],
        "modeled_not_observed": True,
    }
    if not usable or stats["gamma_confidence"] != "SUFFICIENT":
        result["status"] = "INSUFFICIENT DATA"
        result["regime"] = "INSUFFICIENT DATA"
        return result
    if not bot.GAMMA_FLIP_ENABLED:
        result["status"] = "CALCULATION UNAVAILABLE"
        result["regime"] = "CALCULATION UNAVAILABLE"
        return result

    initial = max(2.0, min(float(bot.GAMMA_FLIP_RANGE_PCT), 30.0))
    maximum = max(initial, min(GAMMA_FLIP_MAX_RANGE_PCT, 100.0))
    search_range = initial
    crossings = []
    while True:
        result["search_ranges_percent"].append(round(search_range, 4))
        crossings = _find_gamma_crossings(usable, stock_price, search_range)
        if crossings or search_range >= maximum:
            break
        search_range = min(maximum, search_range * 2.0)
    result["search_range_percent"] = search_range
    result["crossings"] = crossings
    if crossings:
        flip = min(crossings, key=lambda value: abs(value - stock_price))
        result["flip"] = flip
        result["status"] = "FOUND"
        result["gamma_distance_percent"] = (
            (stock_price - flip) / flip * 100.0 if flip > 0 else None
        )
    else:
        result["status"] = "NO FLIP IN RANGE"
    return result


def gamma_direction_state(direction, stock_price, gamma_info):
    """Return confirmed/opposed/unavailable without rejecting unavailable data."""
    if not bot.GAMMA_FLIP_ENABLED or not gamma_info:
        return "unavailable"
    if gamma_info.get("status") != "FOUND":
        return "unavailable"
    flip = bot.safe_float(gamma_info.get("flip"))
    if flip <= 0:
        return "unavailable"
    buffer_value = stock_price * max(bot.GAMMA_FLIP_BUFFER_PCT, 0.0) / 100.0
    confirmed = (
        direction == "CALL" and stock_price > flip + buffer_value
    ) or (
        direction == "PUT" and stock_price < flip - buffer_value
    )
    return "confirmed" if confirmed else "opposed"


def gamma_flip_allows(direction, stock_price, gamma_info):
    return gamma_direction_state(direction, stock_price, gamma_info) != "opposed"


def _gamma_state_fields(gamma_info, stock_price):
    info = gamma_info or {}
    flip = _present_number(info.get("flip"))
    current_gex = _present_number(
        info.get("modeled_net_gex")
        if info.get("modeled_net_gex") is not None
        else info.get("current_gex")
    )
    status = str(info.get("status") or "INSUFFICIENT DATA").upper()
    return {
        "gamma_flip": round(flip, 2) if flip is not None and flip > 0 else None,
        "gamma_stock_price": round(stock_price, 2),
        "gamma_status": status,
        "gamma_regime": str(info.get("regime") or status).upper(),
        "net_gex": round(current_gex, 2) if current_gex is not None else None,
        "modeled_net_gex": round(current_gex, 2) if current_gex is not None else None,
        "modeled_net_gex_sign": str(info.get("gex_sign") or "unavailable"),
        "gamma_distance_percent": info.get("gamma_distance_percent"),
        "gamma_completeness_percent": info.get("gamma_completeness_percent", 0.0),
        "gamma_confidence": str(info.get("gamma_confidence") or "UNAVAILABLE"),
        "gamma_data_quality": {
            key: info.get(key)
            for key in (
                "contracts_considered", "contracts_in_universe", "contracts_usable",
                "skipped_missing_iv", "skipped_missing_oi",
                "skipped_invalid_expiration_or_strike",
                "skipped_outside_dte_universe",
            )
        },
        "modeled_not_observed": True,
    }


def _populate_gamma_card_fields(image_path, state):
    """Populate the existing Gamma detail area without changing card layout."""
    try:
        image = bot.Image.open(image_path).convert("RGB")
        draw = bot.ImageDraw.Draw(image)
        panel2, muted, white = (9, 29, 44), (139, 163, 181), (238, 245, 249)
        draw.rectangle((105, 1274, 960, 1344), fill=panel2)
        flip = _present_number(state.get("gamma_flip"))
        status = str(state.get("gamma_status") or state.get("gamma_regime") or "INSUFFICIENT DATA").upper()
        flip_value = f"${flip:.2f}" if flip is not None and flip > 0 else status
        gex = _present_number(
            state.get("modeled_net_gex")
            if state.get("modeled_net_gex") is not None
            else state.get("net_gex")
        )
        regime = str(state.get("gamma_regime") or "UNAVAILABLE").upper()
        gex_value = (
            f"{gex:+,.2f} • {regime} • MODELED"
            if gex is not None else f"UNAVAILABLE • {regime} • MODELED"
        )
        rows = [
            (1280, "GAMMA FLIP", flip_value),
            (1311, "GAMMA / MODELED NET GEX", gex_value),
        ]
        for y, label, value in rows:
            draw.text((115, y), label, font=bot._font(16, True), fill=muted)
            font = bot._font(17, True)
            bounds = draw.textbbox((0, 0), value, font=font)
            draw.text((945 - (bounds[2] - bounds[0]), y), value, font=font, fill=white)
        image.save(image_path)
    except Exception as error:
        print("Gamma card detail warning:", error)


def nearest_expiration(contracts):
    """Return the nearest expiration using the New York market date."""
    today = bot.now_new_york().date()
    valid = []

    for contract in contracts:
        try:
            expiration = bot.datetime.strptime(
                contract["expiration"], "%Y-%m-%d"
            ).date()
            if expiration >= today:
                valid.append(expiration)
        except (KeyError, TypeError, ValueError):
            continue

    return min(valid) if valid else None


def create_trade(
    symbol, direction, stock_price, contract=None, gamma_info=None,
    trade_confidence=None, gamma_direction=None,
):
    """Lock and open the exact contract that qualified during the scan."""
    symbol = str(symbol or "").upper().strip()
    direction = str(direction or "").upper().strip()
    if contract is None:
        # Manual /signal compatibility: select once through the canonical,
        # fully-paginated path and calculate Gamma from that same full universe.
        call_raw = massive_chain(symbol, "CALL")
        put_raw = massive_chain(symbol, "PUT")
        if gamma_info is None:
            gamma_info = calculate_gamma_flip(call_raw, put_raw, stock_price)
        calls, _ = _prepare_side(call_raw, stock_price)
        puts, _ = _prepare_side(put_raw, stock_price)
        call_stats, put_stats = _side_flow_stats(calls), _side_flow_stats(puts)
        call_strength = bot._direction_strength(call_stats, put_stats)
        put_strength = bot._direction_strength(put_stats, call_stats)
        requested = calls if direction == "CALL" else puts
        contract = requested[0] if requested else None
        direction_edge = (
            call_strength - put_strength
            if direction == "CALL" else put_strength - call_strength
        )
        assessment = _conservative_assessment(
            direction, contract, direction_edge, gamma_info, stock_price
        )
        if ORCA_CONSERVATIVE_MODE and not assessment["can_open"]:
            raise RuntimeError(assessment["wait_status"])
        trade_confidence = assessment["trade_confidence"]
        gamma_direction = assessment["gamma_direction_state"]
    if not isinstance(contract, dict) or not contract.get("ticker"):
        raise RuntimeError("Qualified contract is missing")

    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        trades = store.setdefault("trades", {})
        existing = trades.get(symbol)
        if isinstance(existing, dict) and existing.get("status") in (
            "opening", "active", "recovery_required"
        ):
            result = dict(existing)
            result["_ignored_duplicate"] = True
            return result
        created_at = bot.now_new_york().isoformat(timespec="seconds")
        reservation = {"symbol": symbol, "direction": direction, "stock_price": stock_price,
                       "contract_ticker": contract["ticker"], "entry_price": 0.0,
                       "status": "opening", "message_id": None, "created_at": created_at,
                        "monitor_version": 0, "opening_phase": "reserved"}
        trades[symbol] = reservation
        bot._save_trade_store(store)
    try:
        entry, source = bot.choose_initial_price(contract)
        if entry <= 0:
            raise RuntimeError("Qualified contract has no usable option price")
        state = {
            "symbol": symbol, "direction": direction, "stock_price": stock_price,
            "contract_ticker": contract["ticker"], "expiration": contract["expiration"],
            "strike": contract["strike"], "score": contract["score"], "entry_price": round(entry, 2),
            "entry_source": source, "stop_loss": bot.stop_loss(entry), "targets": bot.targets(entry),
            "current_price": round(entry, 2), "highest_price": round(entry, 2), "achieved": 0,
            "lowest_price": round(entry, 2),
            "status": "active", "message_id": None, "created_at": created_at, "monitor_version": 1,
            "telegram_send_state": "not_started",
            "dte": contract.get("dte"),
            "trade_confidence": (trade_confidence or {}).get("level", "N/A"),
            "trade_confidence_score": (trade_confidence or {}).get("score"),
            "trade_confidence_components": (trade_confidence or {}).get("components", {}),
            "gamma_direction_state": gamma_direction or gamma_direction_state(
                direction, stock_price, gamma_info
            ),
            "paper_trade_id": uuid.uuid4().hex,
            **_gamma_state_fields(gamma_info, stock_price),
        }
        with bot.TRADE_STATE_LOCK:
            store = bot._load_trade_store()
            current = (store.get("trades") or {}).get(symbol)
            if not isinstance(current, dict) or current.get("status") != "opening" or current.get("contract_ticker") != contract["ticker"]:
                raise RuntimeError("opening trade changed before activation")
            # This durable checkpoint makes a subsequent send failure
            # unambiguous: it may have reached Telegram, so never resend it.
            state["telegram_send_state"] = "sending"
            store["trades"][symbol] = state
            bot._save_trade_store(store)
        # Rendering and Telegram are intentionally outside TRADE_STATE_LOCK.
        message_id = bot.telegram_send_card(dict(state))
        with bot.TRADE_STATE_LOCK:
            store = bot._load_trade_store()
            current = (store.get("trades") or {}).get(symbol)
            if not isinstance(current, dict) or current.get("contract_ticker") != contract["ticker"] or current.get("message_id"):
                raise RuntimeError("active trade changed before Telegram synchronization")
            current["message_id"] = message_id
            current["telegram_send_state"] = "sent"
            current["monitor_version"] = int(current.get("monitor_version", 0)) + 1
            store["trades"][symbol] = current
            store.setdefault("paper_trades", []).append(_paper_trade_record(current))
            (store.get("pending_candidates") or {}).pop(symbol, None)
            bot._save_trade_store(store)
            state = dict(current)
        print(f"🔒 HARD CONTRACT LOCKED: {symbol} {direction} | {contract['ticker']} | option entry {entry:.2f}")
        return state
    except Exception:
        with bot.TRADE_STATE_LOCK:
            latest = bot._load_trade_store()
            current = (latest.get("trades") or {}).get(symbol)
            if isinstance(current, dict) and current.get("contract_ticker") == contract["ticker"] and not current.get("message_id"):
                if current.get("opening_phase") == "reserved":
                    latest["trades"].pop(symbol, None)
                else:
                    current.update(status="recovery_required",
                                   recovery_note="Telegram send may have succeeded; manual recovery required")
                    latest["trades"][symbol] = current
                bot._save_trade_store(latest)
        raise


def _apply_monitored_price(symbol, identity, price, source, age, quote_age, trade_age):
    """Atomically apply an already-fetched exact-contract update."""
    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        state = (store.get("trades") or {}).get(symbol)
        if not isinstance(state, dict) or state.get("status") != "active":
            return None
        current_identity = (
            state.get("contract_ticker"),
            state.get("message_id"),
            int(state.get("monitor_version", 0)),
        )
        if current_identity != identity:
            return None
        previous_achieved = int(state.get("achieved", 0))
        state["current_price"] = round(price, 2)
        state["highest_price"] = round(max(bot.safe_float(state.get("highest_price")), price), 2)
        prior_low = bot.safe_float(state.get("lowest_price"), bot.safe_float(state.get("entry_price")))
        state["lowest_price"] = round(min(prior_low, price), 2)
        if price <= bot.safe_float(state["stop_loss"]):
            state.update(status="stopped", closed_at=bot.now_new_york().isoformat(timespec="seconds"),
                         close_reason="STOP_LOSS")
        else:
            achieved = previous_achieved
            while achieved < 5 and bot.safe_float(state["highest_price"]) >= bot.safe_float(state["targets"][achieved]):
                achieved += 1
            state["achieved"] = achieved
            if achieved >= 5:
                state.update(status="completed", closed_at=bot.now_new_york().isoformat(timespec="seconds"),
                             close_reason="ALL_TARGETS")
        state.update(
            last_successful_option_price_update=bot.now_new_york().isoformat(timespec="seconds"),
            last_massive_price_source=source, last_data_age_seconds=round(age, 3),
            last_quote_age_seconds=round(quote_age, 3) if quote_age is not None else None,
            last_trade_age_seconds=round(trade_age, 3) if trade_age is not None else None,
            monitor_version=int(state.get("monitor_version", 0)) + 1,
        )
        store["trades"][symbol] = state
        _update_paper_trade(store, state, previous_achieved)
        bot._save_trade_store(store)
        return dict(state)


def _record_telegram_sync(symbol, identity, success, error=None):
    """Persist edit outcome only if this remains the exact original message."""
    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        state = (store.get("trades") or {}).get(symbol)
        current_identity = (
            state.get("contract_ticker"),
            state.get("message_id"),
            int(state.get("monitor_version", 0)),
        ) if isinstance(state, dict) else None
        if current_identity != identity:
            return
        if success:
            state["telegram_sync_pending"] = False
            state["telegram_sync_pending_count"] = 0
            state["last_successful_telegram_sync"] = bot.now_new_york().isoformat(timespec="seconds")
            state["last_telegram_error"] = None
        else:
            state["telegram_sync_pending"] = True
            state["telegram_sync_pending_count"] = int(state.get("telegram_sync_pending_count", 0)) + 1
            state["last_telegram_error"] = error or "Telegram edit retry exhausted"
        store["trades"][symbol] = state
        bot._save_trade_store(store)


def monitor_active_trades_once():
    """Poll exact locked tickers without holding the state lock during I/O."""
    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        work = [(symbol, (
                    trade.get("contract_ticker"),
                    trade.get("message_id"),
                    int(trade.get("monitor_version", 0)),
                ))
                for symbol, trade in (store.get("trades") or {}).items()
                if isinstance(trade, dict) and trade.get("status") == "active"]
    if not work:
        return {"accepted": True, "ignored": "no active trades"}
    results = []
    for symbol, identity in work:
        contract, message_id, _ = identity
        if not contract:
            results.append({"accepted": False, "symbol": symbol, "error": "active trade missing contract"})
            continue
        try:
            snapshot = bot.massive_contract_snapshot(symbol, contract)
            price, source, age, quote_age, trade_age = classify_snapshot_price(snapshot)
            if price > 0:
                state = _apply_monitored_price(symbol, identity, price, source, age, quote_age, trade_age)
                if state is None:
                    results.append({"accepted": True, "symbol": symbol, "contract": contract,
                                    "ignored": "locked trade changed during poll"})
                    continue
                sync_identity = (
                    state.get("contract_ticker"),
                    state.get("message_id"),
                    int(state.get("monitor_version", 0)),
                )
            else:
                # Do not overwrite a known price with old session/day values.
                with bot.TRADE_STATE_LOCK:
                    latest = bot._load_trade_store()
                    state = (latest.get("trades") or {}).get(symbol)
                    if isinstance(state, dict) and (
                        state.get("contract_ticker"), state.get("message_id"),
                        int(state.get("monitor_version", 0)),
                    ) == identity:
                        state.update(last_massive_price_source=source,
                                     last_data_age_seconds=round(age, 3) if age is not None else None,
                                     last_quote_age_seconds=round(quote_age, 3) if quote_age is not None else None,
                                     last_trade_age_seconds=round(trade_age, 3) if trade_age is not None else None)
                        latest["trades"][symbol] = state
                        bot._save_trade_store(latest)
                        state = dict(state)
                    else:
                        # Do not edit a replacement trade/message using a
                        # snapshot obtained for the prior identity.
                        state = None
                results.append({"accepted": True, "symbol": symbol, "contract": contract, "ignored": source,
                                "source": source, "source_age_seconds": age})
                print(f"💹 {symbol} {contract} price=unchanged source={source} age={age}")
                if isinstance(state, dict) and state.get("telegram_sync_pending") and message_id:
                    success, error = telegram_edit_card_retry(message_id, state)
                    _record_telegram_sync(symbol, identity, success, error)
                continue
            success, error = telegram_edit_card_retry(message_id, state)
            _record_telegram_sync(symbol, sync_identity, success, error)
            item = {"accepted": True, "symbol": symbol, "contract": contract, "price": price,
                    "source": source, "source_age_seconds": round(age, 3),
                    "quote_age_seconds": round(quote_age, 3) if quote_age is not None else None,
                    "trade_age_seconds": round(trade_age, 3) if trade_age is not None else None,
                    "telegram_synchronized": success, "status": state.get("status")}
            results.append(item)
            print(f"💹 {symbol} {contract} price={price:.2f} source={source} age={age:.1f}s telegram={success}")
        except Exception as error:
            results.append({"accepted": False, "symbol": symbol, "contract": contract, "error": str(error)})
    return {"accepted": True, "active_checked": len(work), "results": results}


def auto_monitor_loop():
    interval = max(float(getattr(bot, "AUTO_MONITOR_SECONDS", 5)), 0.1)
    print(f"Auto contract monitor: every {interval:g}s")
    while True:
        try:
            monitor_active_trades_once()
        except Exception as error:
            print("Auto monitor warning:", error)
        bot.time.sleep(interval)


def _side_diagnostic(direction, side_stats, strength, other_strength, gamma_info, stock_price):
    best = side_stats.get("best") or {}
    score = bot.safe_float(best.get("score"))
    volume = bot.safe_float(side_stats.get("volume"))
    has_eligible_contract = bool(best)
    score_ok = has_eligible_contract and score >= bot.AUTO_SCAN_MIN_SCORE
    edge = strength - other_strength
    edge_ok = edge >= bot.AUTO_SCAN_MIN_EDGE
    volume_ok = volume >= bot.AUTO_SCAN_MIN_VOLUME
    gamma_state = gamma_direction_state(direction, stock_price, gamma_info)
    gamma_ok = gamma_state != "opposed"

    reasons = []
    if not has_eligible_contract:
        reasons.append("no eligible contract after quality safeguards")
    if not score_ok:
        reasons.append(f"score {score:.1f} < {bot.AUTO_SCAN_MIN_SCORE:.1f}")
    if not edge_ok:
        reasons.append(f"edge {edge:.1f} < {bot.AUTO_SCAN_MIN_EDGE:.1f}")
    if not volume_ok:
        reasons.append(f"volume {volume:.0f} < {bot.AUTO_SCAN_MIN_VOLUME:.0f}")
    if not gamma_ok:
        reasons.append("Gamma opposed")

    accepted = score_ok and edge_ok and volume_ok and gamma_ok
    return {
        "direction": direction,
        "accepted": accepted,
        "contract": best.get("ticker"),
        "score": score,
        "strength": strength,
        "edge": round(edge, 2),
        "volume": round(volume, 0),
        "open_interest": round(bot.safe_float(side_stats.get("open_interest")), 0),
        "score_breakdown": best.get("score_breakdown") or {},
        "critical_inputs": best.get("critical_inputs") or {},
        "optional_inputs": best.get("optional_inputs") or {},
        "completeness_percent": best.get("completeness_percent"),
        "selection_eligible": best.get("selection_eligible", False),
        "rejection_reasons": best.get("rejection_reasons") or [],
        "gamma_direction_state": gamma_state,
        "gamma_status": (gamma_info or {}).get("status", "INSUFFICIENT DATA"),
        "gamma_completeness_percent": (gamma_info or {}).get(
            "gamma_completeness_percent", 0.0
        ),
        "candidate_count": side_stats.get("candidate_count", 0),
        "eligible_candidate_count": side_stats.get("eligible_candidate_count", 0),
        "candidate_scores": side_stats.get("candidate_scores") or [],
        "checks": {
            "eligible_contract": has_eligible_contract,
            "score": score_ok,
            "directional_edge": edge_ok,
            "volume": volume_ok,
            "gamma_flip": gamma_ok,
        },
        "reason": "qualified" if accepted else "; ".join(reasons),
    }


def scan_symbol_options(symbol):
    """Evaluate CALL and PUT independently and report every qualification check."""
    symbol = str(symbol or "").upper().strip()

    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        existing = (store.get("trades") or {}).get(symbol)
        pending_candidate = (store.get("pending_candidates") or {}).get(symbol)
        has_active_trade = isinstance(existing, dict) and existing.get("status") in (
            "opening",
            "active",
            "recovery_required",
        )

    call_raw = massive_chain(symbol, "CALL")
    put_raw = massive_chain(symbol, "PUT")
    stock_price, stock_source = bot.infer_underlying_price(call_raw, put_raw)
    if stock_price <= 0:
        return {
            "symbol": symbol,
            "accepted": False,
            "call": {"accepted": False, "reason": "underlying price unavailable"},
            "put": {"accepted": False, "reason": "underlying price unavailable"},
            "ignored": "underlying price unavailable",
        }

    gamma_info = bot.calculate_gamma_flip(call_raw, put_raw, stock_price)

    if has_active_trade:
        gamma_sync = None
        with bot.TRADE_STATE_LOCK:
            store = bot._load_trade_store()
            live = (store.get("trades") or {}).get(symbol)
            if isinstance(live, dict) and live.get("status") == "active":
                live.update(_gamma_state_fields(gamma_info, stock_price))
                live["stock_price"] = round(stock_price, 2)
                live["monitor_version"] = int(live.get("monitor_version", 0)) + 1
                (store.get("trades") or {})[symbol] = live
                bot._save_trade_store(store)

                if live.get("message_id"):
                    gamma_sync = (
                        live["message_id"],
                        dict(live),
                        (
                            live.get("contract_ticker"),
                            live.get("message_id"),
                            int(live.get("monitor_version", 0)),
                        ),
                    )

                flip = bot.safe_float(live.get("gamma_flip"))
                side = (
                    "ABOVE"
                    if flip > 0 and stock_price > flip
                    else "BELOW"
                    if flip > 0 and stock_price < flip
                    else "NO-FLIP"
                )
                warning = (
                    live.get("direction") == "CALL" and side == "BELOW"
                ) or (
                    live.get("direction") == "PUT" and side == "ABOVE"
                )
                locked_reason = "not evaluated: active contract already locked"
                result = {
                    "symbol": symbol,
                    "accepted": True,
                    "ignored": "active contract already locked",
                    "call": {"accepted": False, "reason": locked_reason},
                    "put": {"accepted": False, "reason": locked_reason},
                    "gamma_flip": live.get("gamma_flip"),
                    "stock_price": round(stock_price, 2),
                    "price_vs_gamma_flip": side,
                    "exit_warning": warning,
                }
        if gamma_sync:
            message_id, sync_state, sync_identity = gamma_sync
            synced, error = telegram_edit_card_retry(message_id, sync_state)
            _record_telegram_sync(symbol, sync_identity, synced, error)
            if not synced:
                print("Gamma card refresh warning:", symbol, error)
            result["telegram_synchronized"] = synced
        if "result" in locals():
            return result

        locked_reason = "not evaluated: contract opening/locked"
        return {
            "symbol": symbol,
            "accepted": True,
            "ignored": "contract opening/locked",
            "call": {"accepted": False, "reason": locked_reason},
            "put": {"accepted": False, "reason": locked_reason},
        }

    calls, call_exp = bot._prepare_side(call_raw, stock_price)
    puts, put_exp = bot._prepare_side(put_raw, stock_price)
    if isinstance(pending_candidate, dict):
        if pending_candidate.get("direction") == "CALL":
            _promote_pending_candidate(calls, pending_candidate)
        elif pending_candidate.get("direction") == "PUT":
            _promote_pending_candidate(puts, pending_candidate)
    call_stats = bot._side_flow_stats(calls)
    put_stats = bot._side_flow_stats(puts)
    call_strength = bot._direction_strength(call_stats, put_stats)
    put_strength = bot._direction_strength(put_stats, call_stats)
    call_diag = _side_diagnostic(
        "CALL", call_stats, call_strength, put_strength, gamma_info, stock_price
    )
    put_diag = _side_diagnostic(
        "PUT", put_stats, put_strength, call_strength, gamma_info, stock_price
    )
    assessments = {}
    if ORCA_CONSERVATIVE_MODE:
        assessments["CALL"] = _conservative_assessment(
            "CALL", call_stats.get("best"), call_strength - put_strength,
            gamma_info, stock_price,
        )
        assessments["PUT"] = _conservative_assessment(
            "PUT", put_stats.get("best"), put_strength - call_strength,
            gamma_info, stock_price,
        )
        ambiguous = bool(
            call_stats.get("best") and put_stats.get("best")
            and abs(call_strength - put_strength) < ORCA_MIN_DIRECTION_EDGE
        )
        for direction, diagnostic in (("CALL", call_diag), ("PUT", put_diag)):
            assessment = assessments[direction]
            reasons = list(assessment["wait_reasons"])
            if ambiguous:
                reasons.insert(0, "WAIT - DIRECTION UNCLEAR")
            reasons = list(dict.fromkeys(reasons))
            assessment["wait_reasons"] = reasons
            assessment["wait_status"] = reasons[0] if reasons else "TRADE"
            assessment["can_open"] = not reasons
            diagnostic.update(
                accepted=assessment["can_open"],
                trade_confidence=assessment["trade_confidence"],
                wait_status=assessment["wait_status"],
                wait_reasons=reasons,
                entry_price_quality=assessment["entry_source"],
                gamma_direction_state=assessment["gamma_direction_state"],
                reason="qualified" if not reasons else "; ".join(reasons),
            )

    result = {
        "symbol": symbol,
        "accepted": True,
        "stock_price": round(stock_price, 2),
        "stock_price_source": stock_source,
        "call": call_diag,
        "put": put_diag,
        "call_expiration": call_exp,
        "put_expiration": put_exp,
        "gamma_flip": (
            round(gamma_info["flip"], 2)
            if gamma_info and gamma_info.get("flip")
            else None
        ),
        "modeled_net_gex": (
            round(gamma_info["modeled_net_gex"], 2)
            if gamma_info and gamma_info.get("modeled_net_gex") is not None
            else None
        ),
        "net_gex": (
            round(gamma_info["modeled_net_gex"], 2)
            if gamma_info and gamma_info.get("modeled_net_gex") is not None
            else None
        ),
        "modeled_net_gex_sign": (
            gamma_info.get("gex_sign") if gamma_info else "unavailable"
        ),
        "gamma_regime": gamma_info.get("regime") if gamma_info else None,
        "gamma_status": gamma_info.get("status") if gamma_info else "INSUFFICIENT DATA",
        "gamma_distance_percent": (
            gamma_info.get("gamma_distance_percent") if gamma_info else None
        ),
        "gamma_completeness_percent": (
            gamma_info.get("gamma_completeness_percent", 0.0) if gamma_info else 0.0
        ),
        "gamma_confidence": (
            gamma_info.get("gamma_confidence", "UNAVAILABLE")
            if gamma_info else "UNAVAILABLE"
        ),
        "gamma_data_quality": (
            _gamma_state_fields(gamma_info, stock_price)["gamma_data_quality"]
            if gamma_info else {}
        ),
        "modeled_not_observed": True,
    }

    qualified = []
    if call_diag["accepted"]:
        qualified.append(("CALL", call_stats["best"], call_strength))
    if put_diag["accepted"]:
        qualified.append(("PUT", put_stats["best"], put_strength))

    if not qualified:
        preferred = max(
            (
                ("CALL", call_stats.get("best"), call_strength),
                ("PUT", put_stats.get("best"), put_strength),
            ),
            key=lambda item: item[2],
        )
        preferred_direction, preferred_contract, _ = preferred
        assessment = assessments.get(preferred_direction)
        if (
            ORCA_CONSERVATIVE_MODE and preferred_contract and assessment
            and "WAITING FOR PRICE CONFIRMATION" in assessment["wait_reasons"]
            and not any(
                reason not in (
                    "WAITING FOR PRICE CONFIRMATION",
                    "WAIT - CONFIDENCE NOT HIGH",
                )
                for reason in assessment["wait_reasons"]
            )
        ):
            _save_pending_candidate(
                symbol, preferred_direction, preferred_contract, assessment
            )
            result["status"] = "WAITING FOR PRICE CONFIRMATION"
            result["preferred_contract"] = preferred_contract.get("ticker")
        else:
            _clear_pending_candidate(symbol)
            result["status"] = (
                assessment.get("wait_status")
                if assessment else "WAIT - NO QUALIFIED DIRECTION"
            )
        result["ignored"] = "neither CALL nor PUT qualified"
        _LAST_SCAN_DECISIONS[symbol] = {
            "status": result["status"],
            "preferred_contract": result.get("preferred_contract"),
            "call_reasons": call_diag.get("wait_reasons") or [call_diag.get("reason")],
            "put_reasons": put_diag.get("wait_reasons") or [put_diag.get("reason")],
            "updated_at": bot.now_new_york().isoformat(timespec="seconds"),
        }
        return result

    direction, contract, _ = max(qualified, key=lambda item: item[2])
    assessment = assessments.get(direction)
    _clear_pending_candidate(symbol)
    state = create_trade(
        symbol, direction, stock_price, contract=contract, gamma_info=gamma_info,
        trade_confidence=(assessment or {}).get("trade_confidence"),
        gamma_direction=(assessment or {}).get("gamma_direction_state"),
    )
    result["direction"] = direction
    result["contract"] = state.get("contract_ticker")
    result["message_id"] = state.get("message_id")
    result["status"] = "TRADE"
    result["trade_confidence"] = state.get("trade_confidence")
    _LAST_SCAN_DECISIONS[symbol] = {
        "status": "TRADE",
        "contract": state.get("contract_ticker"),
        "confidence": state.get("trade_confidence"),
        "updated_at": bot.now_new_york().isoformat(timespec="seconds"),
    }
    return result


def scan_watchlist_once(force=False):
    """Run at most one scanner pass process-wide."""
    if not SCAN_IN_PROGRESS_LOCK.acquire(blocking=False):
        return {"accepted": True, "ignored": "scan already in progress"}

    try:
        if not bot.AUTO_SCAN_ENABLED:
            return {"accepted": True, "ignored": "auto scanner disabled"}
        if not force and not bot.market_is_open_now():
            return {"accepted": True, "ignored": "US regular market is closed"}

        results = []
        for symbol in bot.WATCHLIST:
            try:
                item = scan_symbol_options(symbol)
            except Exception as error:
                item = {
                    "symbol": symbol,
                    "accepted": False,
                    "call": {"accepted": False, "reason": f"scan error: {error}"},
                    "put": {"accepted": False, "reason": f"scan error: {error}"},
                    "error": str(error),
                }
            results.append(item)
            print("🐋 ORCA SCAN:", item)
            bot.time.sleep(0.35)

        return {"accepted": True, "checked": len(bot.WATCHLIST), "results": results}
    finally:
        SCAN_IN_PROGRESS_LOCK.release()


def parse_force(value):
    """Accept only a JSON boolean for force."""
    if value is None:
        return False
    if type(value) is not bool:
        raise ValueError("force must be a JSON boolean")
    return value


def _start_thread_once(kind, target, name):
    """Prevent duplicate in-process loops when startup is invoked twice."""
    global _DUPLICATE_THREAD_START_PREVENTED
    with THREAD_START_LOCK:
        existing = _STARTED_THREADS.get(kind)
        if existing and existing.is_alive():
            _DUPLICATE_THREAD_START_PREVENTED = True
            print(f"Duplicate {kind} thread startup prevented")
            return existing
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        _STARTED_THREADS[kind] = thread
        return thread


def start_auto_monitor():
    return _start_thread_once("monitor", auto_monitor_loop, "reef-auto-monitor")


def start_auto_scanner():
    if not bot.AUTO_SCAN_ENABLED:
        return None
    return _start_thread_once("scanner", bot.auto_scan_loop, "orca-options-scanner")


def _thread_status(kind):
    thread = _STARTED_THREADS.get(kind)
    return bool(thread and thread.is_alive())


class CanonicalHandler(bot.Handler):
    """Secure the canonical /scan-now route before invoking the scanner."""

    def do_GET(self):
        path = urlparse(self.path).path
        if path not in ("/health", "/performance"):
            return super().do_GET()
        monitored = []
        pending = []
        performance = paper_performance_summary({"paper_trades": []})
        try:
            with bot.TRADE_STATE_LOCK:
                store = bot._load_trade_store()
                for symbol, trade in (store.get("trades") or {}).items():
                    if isinstance(trade, dict) and trade.get("status") == "active":
                        monitored.append({
                            "symbol": symbol,
                            "contract_ticker": trade.get("contract_ticker"),
                            "last_successful_option_price_update": trade.get("last_successful_option_price_update"),
                            "last_massive_price_source": trade.get("last_massive_price_source"),
                            "last_data_age_seconds": trade.get("last_data_age_seconds"),
                            "last_successful_telegram_sync": trade.get("last_successful_telegram_sync"),
                            "telegram_sync_pending": bool(trade.get("telegram_sync_pending")),
                            "telegram_sync_pending_count": int(trade.get("telegram_sync_pending_count", 0)),
                        })
                pending = [
                    {
                        "symbol": symbol,
                        "direction": item.get("direction"),
                        "contract_ticker": item.get("contract_ticker"),
                        "wait_status": item.get("wait_status"),
                        "updated_at": item.get("updated_at"),
                    }
                    for symbol, item in (store.get("pending_candidates") or {}).items()
                    if isinstance(item, dict)
                ]
                performance = paper_performance_summary(store)
        except TradeStateCorruptionError:
            # Health remains operational specifically so corrupted state is
            # observable without risking a replacement empty state.
            pass
        if path == "/performance":
            self.json_response(200, {
                "ok": True,
                "mode": "conservative validation",
                "delayed_data_disclaimer": (
                    "Paper outcomes only; market data may be delayed. "
                    "No profitability or live-price claim."
                ),
                "performance": performance,
            })
            return
        self.json_response(200, {"ok": True, "service": "ORCA WHALE OPTIONS BOT",
                                  "conservative_mode": ORCA_CONSERVATIVE_MODE,
                                  "conservative_min_score": ORCA_CONSERVATIVE_MIN_SCORE,
                                  "minimum_direction_edge": ORCA_MIN_DIRECTION_EDGE,
                                  "data_feed_mode": "delayed/non-paid compatible; not claimed real-time",
                                  "last_scan_decisions": dict(_LAST_SCAN_DECISIONS),
                                  "pending_price_confirmation": pending,
                                  "paper_performance": performance,
                                 "monitor_interval_seconds": getattr(bot, "AUTO_MONITOR_SECONDS", 5),
                                 "monitored_contracts": monitored,
                                 "trade_state_path": str(trade_state_path()),
                                 "trade_state_path_configured": bool(os.getenv("TRADE_STATE_PATH", "").strip()),
                                 "state_load_status": _STATE_LOAD_STATUS,
                                 "last_successful_state_save": _LAST_SUCCESSFUL_STATE_SAVE,
                                 "restored_active_trade_count": _RESTORED_ACTIVE_TRADE_COUNT,
                                 "recovery_required_count": _RECOVERY_REQUIRED_COUNT,
                                 "scanner_thread_running": _thread_status("scanner"),
                                 "monitor_thread_running": _thread_status("monitor"),
                                 "duplicate_thread_start_prevented": _DUPLICATE_THREAD_START_PREVENTED,
                                 "single_process_expected": True})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/scan-now":
            if path in ("/signal", "/monitor-tick", "/option-price"):
                secret = bot.env("REEF_WEBHOOK_SECRET")
                if not secret:
                    self.json_response(
                        503, {"error": "webhook secret is not configured"}
                    )
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw_bytes = self.rfile.read(length)
                try:
                    payload = json.loads(
                        raw_bytes.decode("utf-8", errors="replace")
                    )
                except json.JSONDecodeError:
                    self.json_response(400, {"error": "JSON required"})
                    return
                if not isinstance(payload, dict):
                    self.json_response(400, {"error": "JSON object required"})
                    return
                if str(payload.get("secret", "")) != secret:
                    self.json_response(401, {"error": "invalid secret"})
                    return
                # The historical handler owns the route implementation. Restore
                # the authenticated body so it can parse it normally.
                self.rfile = io.BytesIO(raw_bytes)
            return super().do_POST()

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.json_response(400, {"error": "JSON required"})
            return
        if not isinstance(payload, dict):
            self.json_response(400, {"error": "JSON object required"})
            return

        secret = bot.env("REEF_WEBHOOK_SECRET")
        if not secret:
            self.json_response(503, {"error": "webhook secret is not configured"})
            return
        if str(payload.get("secret", "")) != secret:
            self.json_response(401, {"error": "invalid secret"})
            return

        try:
            force = parse_force(payload.get("force"))
            result = scan_watchlist_once(force=force)
            self.json_response(200, result)
        except ValueError as error:
            self.json_response(400, {"error": str(error)})
        except Exception as error:
            self.json_response(500, {"error": str(error)})


# Install the Phase 1 implementations into the historical production module's
# global namespace so its existing main loop and HTTP server use them.
bot.massive_chain = massive_chain
bot.nearest_expiration = nearest_expiration
bot.normalize_contract = normalize_contract
bot.calculate_score = calculate_score
bot._prepare_side = _prepare_side
bot._side_flow_stats = _side_flow_stats
bot.choose_initial_price = choose_initial_price
bot.calculate_gamma_flip = calculate_gamma_flip
bot.gamma_flip_allows = gamma_flip_allows
bot.net_gamma_exposure_at_price = net_gamma_exposure_at_price
bot.create_trade = create_trade
bot.scan_symbol_options = scan_symbol_options
bot.scan_watchlist_once = scan_watchlist_once
bot.monitor_active_trades_once = monitor_active_trades_once
bot.monitor_active_trade_once = monitor_active_trades_once
bot.auto_monitor_loop = auto_monitor_loop
bot.telegram_edit_card_retry = telegram_edit_card_retry
bot.telegram_send_card = telegram_send_card
bot._load_trade_store = _load_durable_trade_store
bot._save_trade_store = _save_durable_trade_store
bot.load_state = _load_durable_trade_store
bot.save_state = _save_durable_trade_store
bot.start_auto_monitor = start_auto_monitor
bot.start_auto_scanner = start_auto_scanner
bot.Handler = CanonicalHandler


def main():
    """Restore durable state before handing off to the canonical HTTP server."""
    workers = os.getenv("WEB_CONCURRENCY") or os.getenv("RENDER_WORKERS")
    if workers and workers != "1":
        print(f"WARNING: ORCA requires one application process/worker; configured workers={workers}")
    else:
        print("ORCA process ownership: one application process/worker is required")
    try:
        recovery = restore_durable_trade_state()
        print("Durable trade-state recovery:", recovery)
    except TradeStateCorruptionError as error:
        print("FATAL durable trade-state recovery error:", error)
        return
    bot.main()


if __name__ == "__main__":
    main()