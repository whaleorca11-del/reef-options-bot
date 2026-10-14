"""Canonical production entry point for ORCA Whale Options Bot.

This module keeps the approved production implementation in
orca_whale_options_bot_FAST_60S_5S.py intact and replaces only the Phase 1
scanner/selection behaviors.
"""

import json
import io
import hmac
import os
import shutil
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
import macd_direction
from card_renderer import CARD_STYLE
from marketdata_quota import MarketDataQuota
from provider_config import PROVIDER_KEYS, REMOVED_PROVIDERS, resolve_data_provider

import orca_whale_options_bot_FAST_60S_5S as bot

# Canonical production scanner threshold for this entry point.
bot.AUTO_SCAN_MIN_SCORE = 75.0
# Gamma Flip is explicitly disabled as an entry gate and confidence vote.
GAMMA_DIRECTION_GATE_ENABLED = False
bot.ENTRY_SIGNAL_DESCRIPTION = "Autonomous ORCA contract score and relative option activity -> fresh option quote -> Telegram; Gamma Flip disabled; no MACD/EMA200 entry gate"


def _autonomous_entry_signal_mode():
    return "ORCA_SCORE_GAMMA" if GAMMA_DIRECTION_GATE_ENABLED else "ORCA_SCORE_FLOW"


def _gamma_entry_policy():
    return "directional_quality" if GAMMA_DIRECTION_GATE_ENABLED else "disabled"


REQUESTED_DATA_PROVIDER = os.getenv("DATA_PROVIDER", "").strip().lower()
DATA_PROVIDER = resolve_data_provider(REQUESTED_DATA_PROVIDER)
AUTO_SCAN_OPENING_WINDOW_MINUTES = max(
    0, int(os.getenv("AUTO_SCAN_OPENING_WINDOW_MINUTES", "390"))
)
AUTO_SCAN_OPENING_INTERVAL_SECONDS = max(
    60.0, float(os.getenv("AUTO_SCAN_OPENING_INTERVAL_SECONDS", "180"))
)
AUTO_MONITOR_ENABLED = os.getenv(
    "AUTO_MONITOR_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
THETADATA_BASE_URL = os.getenv(
    "THETADATA_BASE_URL", "http://127.0.0.1:25503"
).rstrip("/")
THETADATA_REQUEST_TIMEOUT = max(
    5.0, float(os.getenv("THETADATA_REQUEST_TIMEOUT", "20"))
)
THETADATA_MAX_EXPIRATIONS = max(
    1, int(os.getenv("THETADATA_MAX_EXPIRATIONS", "8"))
)
TRADIER_BASE_URL = os.getenv(
    "TRADIER_BASE_URL", "https://api.tradier.com"
).rstrip("/")
TRADIER_MAX_EXPIRATIONS = max(
    1, int(os.getenv("TRADIER_MAX_EXPIRATIONS", "8"))
)
TRADIER_REQUEST_TIMEOUT = max(
    5.0, float(os.getenv("TRADIER_REQUEST_TIMEOUT", "20"))
)
MARKETDATA_BASE_URL = os.getenv(
    "MARKETDATA_BASE_URL", "https://api.marketdata.app"
).rstrip("/")
MARKETDATA_REQUEST_TIMEOUT = max(
    5.0, float(os.getenv("MARKETDATA_REQUEST_TIMEOUT", "20"))
)
MARKETDATA_CHAIN_CACHE_TTL_SECONDS = float(
    os.getenv("MARKETDATA_CHAIN_CACHE_TTL_SECONDS", "90")
)
_MARKETDATA_QUOTA = MarketDataQuota()
_MARKETDATA_CHAIN_FETCH_LOCK = threading.Lock()


SCAN_IN_PROGRESS_LOCK = threading.Lock()
CARD_RENDER_LOCK = threading.Lock()
THREAD_START_LOCK = threading.Lock()
_STARTED_THREADS = {"scanner": None, "monitor": None, "ichimoku": None}
_DUPLICATE_THREAD_START_PREVENTED = False
_LAST_SUCCESSFUL_STATE_SAVE = None
_STATE_LOAD_STATUS = "not loaded"
_RESTORED_ACTIVE_TRADE_COUNT = 0
_RECOVERY_REQUIRED_COUNT = 0
_LAST_SCAN_DECISIONS = {}
_MACD_CACHE = {}
_MACD_CACHE_LOCK = threading.Lock()
_SCANNER_STATUS_LOCK = threading.Lock()
_SCANNER_STATUS = {
    "last_cycle_started": None,
    "last_cycle_finished": None,
    "last_cycle_status": "not started",
    "last_cycle_checked": 0,
    "last_cycle_trades": 0,
    "last_cycle_errors": 0,
    "last_cycle_ignored": 0,
    "last_cycle_skipped": 0,
    "last_cycle_error": None,
}
_ICHIMOKU_STATUS_LOCK = threading.Lock()
_ICHIMOKU_STATUS = {
    "last_poll_started": None,
    "last_poll_finished": None,
    "last_poll_status": "not started",
    "last_poll_checked": 0,
    "last_poll_signals": 0,
    "last_poll_errors": 0,
    "last_poll_error": None,
}
UNDERLYING_PRICE_CACHE_TTL_SECONDS = 120.0
UNDERLYING_PRICE_BACKOFF_SECONDS = 15.0
_UNDERLYING_PRICE_CACHE = {}
_UNDERLYING_PRICE_CACHE_LOCK = threading.Lock()
_UNDERLYING_PRICE_BACKOFF_UNTIL = 0.0
MASSIVE_CHAIN_CACHE_TTL_SECONDS = float(
    os.getenv("MASSIVE_CHAIN_CACHE_TTL_SECONDS", "90")
)
MASSIVE_RATE_LIMIT_BACKOFF_SECONDS = float(
    os.getenv("MASSIVE_RATE_LIMIT_BACKOFF_SECONDS", "30")
)
_MASSIVE_CHAIN_CACHE = {}
_MASSIVE_CHAIN_CACHE_LOCK = threading.Lock()
_MASSIVE_BACKOFF_UNTIL = 0.0
_TRADIER_EXPIRATION_CACHE = {}
_TRADIER_EXPIRATION_CACHE_LOCK = threading.Lock()
_THETADATA_EXPIRATION_CACHE = {}
_THETADATA_EXPIRATION_CACHE_LOCK = threading.Lock()


class TradeStateCorruptionError(RuntimeError):
    """Raised rather than silently replacing a malformed durable trade file."""


class MarketDataAccountCreditLimitError(RuntimeError):
    """Raised after MarketData explicitly reports an exhausted account limit."""


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


def _trade_expiration_date(trade):
    """Return a trade's contract expiration date when it is safely parseable."""
    expiration = str((trade or {}).get("expiration") or "").strip()
    if not expiration:
        return None
    try:
        return bot.datetime.strptime(expiration, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _trade_symbol(trade_key, trade):
    """Resolve an entry's underlying symbol for legacy and composite keys."""
    raw = (trade or {}).get("symbol")
    if not raw:
        raw = str(trade_key or "").split("::", 1)[0]
    return str(raw or "").upper().strip()


def _iter_trade_entries(store, symbol=None):
    """Yield (storage key, underlying symbol, state) for every saved contract."""
    expected_symbol = str(symbol or "").upper().strip() if symbol else None
    for trade_key, trade in (store.get("trades") or {}).items():
        if not isinstance(trade, dict):
            continue
        trade_symbol = _trade_symbol(trade_key, trade)
        if expected_symbol is None or trade_symbol == expected_symbol:
            yield trade_key, trade_symbol, trade


def _trade_storage_key(symbol, contract_ticker):
    normalized_symbol = str(symbol or "").upper().strip()
    normalized_ticker = bot.canonical_option_ticker(contract_ticker)
    if not normalized_symbol or not normalized_ticker:
        raise RuntimeError("Trade storage key requires symbol and option ticker")
    return f"{normalized_symbol}::{normalized_ticker}"


def _find_trade_entry(store, symbol, contract_ticker):
    target = bot.canonical_option_ticker(contract_ticker)
    if not target:
        return None, None
    for trade_key, _, trade in _iter_trade_entries(store, symbol):
        if bot.canonical_option_ticker(trade.get("contract_ticker")) == target:
            return trade_key, trade
    return None, None


def _existing_contract_tickers(store, symbol):
    return {
        bot.canonical_option_ticker(trade.get("contract_ticker"))
        for _, _, trade in _iter_trade_entries(store, symbol)
        if trade.get("contract_ticker")
    }


def _expire_trade_if_needed(store, trade_key, trade, now=None):
    """Release an old contract without deleting its historical trade record."""
    if not isinstance(trade, dict) or trade.get("status") not in (
        "active", "recovery_required"
    ):
        return False
    now = now or bot.now_new_york()
    expiration = _trade_expiration_date(trade)
    if expiration is None or expiration >= now.date():
        return False
    trade.update(
        status="expired",
        closed_at=now.isoformat(timespec="seconds"),
        close_reason="EXPIRED_CONTRACT",
        recovery_note="Contract expiration passed; released for a new signal",
    )
    store.setdefault("trades", {})[trade_key] = trade
    _update_paper_trade(store, trade, int(trade.get("achieved", 0)))
    return True


def restore_durable_trade_state():
    """Classify crash leftovers before threads are allowed to create new trades."""
    global _RESTORED_ACTIVE_TRADE_COUNT, _RECOVERY_REQUIRED_COUNT
    with bot.TRADE_STATE_LOCK:
        store = _load_durable_trade_store()
        changed = False
        active = 0
        required = 0
        now = bot.now_new_york()
        for trade_key, trade in list(store["trades"].items()):
            if not isinstance(trade, dict):
                raise TradeStateCorruptionError(
                    f"invalid trade record for {trade_key}"
                )
            symbol = _trade_symbol(trade_key, trade)
            status = trade.get("status")
            if _expire_trade_if_needed(store, trade_key, trade, now):
                changed = True
                continue
            if (
                symbol in bot.EXCLUDED_SYMBOLS
                and status == "active"
            ):
                trade.update(
                    status="untracked",
                    untracked_at=now.isoformat(timespec="seconds"),
                    untracked_reason="USER_EXCLUDED_SYMBOL",
                )
                store["trades"][trade_key] = trade
                print(
                    "Excluded active contract from monitoring:",
                    symbol,
                    trade.get("contract_ticker"),
                )
                changed = True
                continue
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
                store["trades"][trade_key] = trade
                changed = True
            elif status == "active" and not trade.get("message_id"):
                trade.update(status="recovery_required",
                             recovery_note="restart: Telegram send outcome is ambiguous")
                store["trades"][trade_key] = trade
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
TELEGRAM_SEND_ATTEMPTS = max(1, int(os.getenv("TELEGRAM_SEND_ATTEMPTS", "3")))
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
SCORE_MIN_DTE = max(0, int(os.getenv("SCORE_MIN_DTE", "10")))
SCORE_MAX_DTE = max(
    SCORE_MIN_DTE, int(os.getenv("SCORE_MAX_DTE", "30"))
)
MAX_OPTION_IMPLIED_VOLATILITY = max(
    0.0, float(os.getenv("MAX_OPTION_IMPLIED_VOLATILITY", "0.50"))
)
MAX_THETA_TO_DELTA_RATIO = 0.50
ICHIMOKU_30M_SIGNAL_MAX_AGE_SECONDS = 30 * 60
ICHIMOKU_30M_BAR_MAX_AGE_SECONDS = max(
    60, int(os.getenv("BOT_ICHIMOKU_MAX_BAR_AGE_SECONDS", "600"))
)
ICHIMOKU_30M_POLL_SECONDS = max(
    60.0, float(os.getenv("BOT_ICHIMOKU_POLL_SECONDS", "300"))
)
ICHIMOKU_30M_CLOUD_DISPLACEMENT = 26
SCORE_DTE_LONGER_POINTS = float(os.getenv("SCORE_DTE_LONGER_POINTS", "3"))
# Temporary test budget for a standard option contract (100 shares). Keep this
# filter separate from quality scoring so a cheaper contract cannot compensate
# for weak data; override both bounds with environment variables as needed.
OPTION_ENTRY_MIN_PREMIUM = max(
    0.01, float(os.getenv("OPTION_ENTRY_MIN_PREMIUM", "3.75"))
)
OPTION_ENTRY_MAX_PREMIUM = max(
    OPTION_ENTRY_MIN_PREMIUM,
    float(os.getenv("OPTION_ENTRY_MAX_PREMIUM", "10.00")),
)
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
    os.getenv("ORCA_CONSERVATIVE_MIN_SCORE", "75")
)
ORCA_MIN_DIRECTION_EDGE = max(
    0.0, float(os.getenv("ORCA_MIN_DIRECTION_EDGE", "8"))
)
CONSERVATIVE_MIN_COMPLETENESS = 83.0
CONSERVATIVE_HIGH_CONFIDENCE = 82.0
CONSERVATIVE_MEDIUM_CONFIDENCE = 65.0
REQUIRE_FRESH_OPTION_ENTRY = os.getenv(
    "REQUIRE_FRESH_OPTION_ENTRY", "1"
).strip().lower() not in ("0", "false", "no")


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
        "quote_timestamp": quote_stamp,
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


def _contract_in_dte_window(expiration, now=None):
    dte = _contract_dte(expiration, now)
    return dte is not None and SCORE_MIN_DTE <= dte <= SCORE_MAX_DTE


def _filter_contracts_by_dte(records, now=None):
    now = now or bot.now_new_york()
    filtered = []
    for record in records or []:
        details = record.get("details", {}) or {}
        expiration = details.get("expiration_date")
        if _contract_in_dte_window(expiration, now):
            filtered.append(record)
    return filtered


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
    price = bot.safe_float(
        contract.get("midpoint")
        or contract.get("last")
        or contract.get("day_close")
    )
    ask = bot.safe_float(contract.get("ask"))
    execution_price = ask if ask > 0 else price
    contract["reference_premium_per_share"] = round(price, 4) if price > 0 else None
    contract["execution_premium_per_share"] = (
        round(execution_price, 4) if execution_price > 0 else None
    )
    contract["estimated_contract_cost_usd"] = (
        round(execution_price * 100.0, 2) if execution_price > 0 else None
    )

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
    if dte is not None and SCORE_MIN_DTE <= dte <= SCORE_MAX_DTE:
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
    if dte is None or dte < SCORE_MIN_DTE or dte > SCORE_MAX_DTE:
        rejection_reasons.append(
            f"DTE {dte} outside {SCORE_MIN_DTE}-{SCORE_MAX_DTE}"
        )
    if price <= 0:
        rejection_reasons.append("option premium unavailable")
    elif not OPTION_ENTRY_MIN_PREMIUM <= execution_price <= OPTION_ENTRY_MAX_PREMIUM:
        premium_label = "ask premium" if ask > 0 else "premium"
        rejection_reasons.append(
            f"{premium_label} ${execution_price:.2f} outside "
            f"${OPTION_ENTRY_MIN_PREMIUM:.2f}-${OPTION_ENTRY_MAX_PREMIUM:.2f}"
        )
    if iv is not None and iv > MAX_OPTION_IMPLIED_VOLATILITY:
        rejection_reasons.append(
            f"IV {iv:.1%} exceeds maximum "
            f"{MAX_OPTION_IMPLIED_VOLATILITY:.1%}"
        )
    if delta is None:
        rejection_reasons.append("Delta unavailable")
    if theta is None:
        rejection_reasons.append("Theta unavailable")
    elif delta is not None and abs(theta) > ad * MAX_THETA_TO_DELTA_RATIO:
        rejection_reasons.append(
            f"|Theta| {abs(theta):.4f} exceeds "
            f"{MAX_THETA_TO_DELTA_RATIO:.0%} of |Delta| {ad:.4f}"
        )
    if oi is None:
        rejection_reasons.append("open interest unavailable")
    elif oi < SCORE_MIN_OPEN_INTEREST:
        rejection_reasons.append(f"OI {oi:.0f} < {SCORE_MIN_OPEN_INTEREST:.0f}")
    if volume is None:
        rejection_reasons.append("volume unavailable")
    elif volume < SCORE_MIN_OPTION_VOLUME:
        rejection_reasons.append(f"volume {volume:.0f} < {SCORE_MIN_OPTION_VOLUME:.0f}")
    score = round(max(0.0, min(100.0, sum(breakdown.values()))), 1)
    contract.update(
        score=score,
        score_breakdown=breakdown,
        dte=dte,
        critical_inputs=critical,
        optional_inputs=optional,
        theta_delta_ratio=(
            round(abs(theta) / ad, 4)
            if theta is not None and ad is not None and ad > 0
            else None
        ),
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

    def __init__(self, eligible, all_candidates, raw_count=0, skipped_identity=0):
        super().__init__(eligible)
        self.all_candidates = all_candidates
        self.raw_count = raw_count
        self.skipped_identity = skipped_identity


def _exclude_recorded_contracts(candidates, recorded_tickers):
    """Keep new eligible contracts while marking exact prior alerts as duplicates."""
    recorded = {
        bot.canonical_option_ticker(ticker)
        for ticker in (recorded_tickers or set())
        if ticker
    }
    all_candidates = list(getattr(candidates, "all_candidates", candidates))
    annotated = []
    for candidate in all_candidates:
        ticker = bot.canonical_option_ticker(candidate.get("ticker"))
        if ticker in recorded:
            item = dict(candidate)
            item["selection_eligible"] = False
            reasons = list(item.get("rejection_reasons") or [])
            if "exact contract already recorded" not in reasons:
                reasons.append("exact contract already recorded")
            item["rejection_reasons"] = reasons
            annotated.append(item)
        else:
            annotated.append(candidate)
    eligible = [
        candidate for candidate in candidates
        if bot.canonical_option_ticker(candidate.get("ticker")) not in recorded
    ]
    return ScoredCandidateList(
        eligible,
        annotated,
        raw_count=getattr(candidates, "raw_count", len(all_candidates)),
        skipped_identity=getattr(candidates, "skipped_identity", 0),
    )


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
        "theta_delta_ratio": contract.get("theta_delta_ratio"),
        "critical_inputs": contract.get("critical_inputs") or {},
        "optional_inputs": contract.get("optional_inputs") or {},
        "completeness_percent": contract.get("completeness_percent"),
        "score_breakdown": contract.get("score_breakdown") or {},
    }


def _prepare_side(raw_chain, stock_price):
    """Score only contracts within the configured 10-30 DTE window."""
    all_candidates = []
    raw_count = len(raw_chain or [])
    skipped_identity = 0
    for raw in raw_chain or []:
        contract = normalize_contract(raw, stock_price)
        if not contract["ticker"] or not contract["expiration"]:
            skipped_identity += 1
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
    candidates = ScoredCandidateList(
        eligible, all_candidates, raw_count=raw_count,
        skipped_identity=skipped_identity,
    )
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
        "raw_count": getattr(contracts, "raw_count", len(all_candidates)),
        "skipped_identity": getattr(contracts, "skipped_identity", 0),
    }


def choose_initial_price(contract):
    """Prefer live prices and retain delayed close only for diagnostics."""
    midpoint = bot.safe_float(contract.get("midpoint"))
    if contract.get("quote_fresh") and midpoint > 0:
        return midpoint, "Fresh Bid/Ask Midpoint"
    last = bot.safe_float(contract.get("last"))
    if contract.get("trade_fresh") and last > 0:
        return last, "Recent Trade (Lower Confidence)"
    delayed_close = bot.safe_float(contract.get("day_close"))
    if delayed_close > 0:
        return delayed_close, "Delayed Session Close (Reference Only)"
    return 0.0, "Unavailable"


def refresh_contract_quote_for_entry(symbol, contract, stock_price):
    """Refresh the selected option's exact quote immediately before alerting."""
    ticker = bot.canonical_option_ticker(contract.get("ticker"))
    snapshot = provider_contract_snapshot(symbol, ticker)
    raw = _snapshot_data(snapshot)
    refreshed = normalize_contract(raw, stock_price)
    if refreshed.get("ticker") != ticker:
        raise RuntimeError("WAIT - FRESH QUOTE CONTRACT DID NOT MATCH")
    if (
        refreshed.get("expiration") != contract.get("expiration")
        or abs(
            bot.safe_float(refreshed.get("strike"))
            - bot.safe_float(contract.get("strike"))
        ) > 0.0001
    ):
        raise RuntimeError("WAIT - FRESH QUOTE CONTRACT DETAILS DID NOT MATCH")
    bid = bot.safe_float(refreshed.get("bid"))
    ask = bot.safe_float(refreshed.get("ask"))
    midpoint = bot.safe_float(refreshed.get("midpoint"))
    if (
        not refreshed.get("quote_fresh")
        or bid <= 0
        or ask < bid
        or midpoint <= 0
    ):
        raise RuntimeError("WAIT - FRESH TWO-SIDED OPTION QUOTE REQUIRED")

    selected = dict(contract)
    quote_fields = (
        "bid", "ask", "midpoint", "spread_percent", "quote_timestamp",
        "quote_age_seconds", "trade_age_seconds", "quote_fresh",
        "trade_fresh", "last", "day_close",
    )
    for key in quote_fields:
        selected[key] = refreshed.get(key)
    for key in ("delta", "theta", "iv", "volume", "open_interest"):
        if refreshed.get(key) is not None:
            selected[key] = refreshed[key]

    calculate_score(selected, stock_price)
    if not selected.get("selection_eligible"):
        reasons = "; ".join(selected.get("rejection_reasons") or [])
        raise RuntimeError(
            f"WAIT - FRESH QUOTE NO LONGER QUALIFIES: {reasons or 'contract filters'}"
        )
    return selected


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
        "Delayed Session Close (Reference Only)": 45.0,
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
    if not GAMMA_DIRECTION_GATE_ENABLED:
        # Remove the Gamma vote entirely, rather than inventing confirmation
        # or penalizing a deliberately disabled indicator as missing evidence.
        components.pop("gamma_state")
        components = {
            name: round(points / 0.90, 2)
            for name, points in components.items()
        }
    value = round(max(0.0, min(100.0, sum(components.values()))), 1)
    return {
        "score": value,
        "level": _confidence_class(value),
        "components": components,
    }


def _conservative_assessment(
    direction, contract, direction_edge, gamma_info, stock_price,
    minimum_score=None, minimum_edge=None, allow_strong_score_exception=False,
):
    """Return explicit quality/price WAIT decisions for one exact candidate."""
    direction_edge = bot.safe_float(direction_edge)
    if not contract:
        return {
            "can_open": False, "wait_status": "WAIT - DATA INCOMPLETE",
            "wait_reasons": ["WAIT - DATA INCOMPLETE"],
            "trade_confidence": {"score": 0.0, "level": "LOW", "components": {}},
            "entry_price": 0.0, "entry_source": "Unavailable",
            "gamma_direction_state": gamma_direction_state(
                direction, stock_price, gamma_info
            ),
        }
    gamma_state = gamma_direction_state(direction, stock_price, gamma_info)
    entry, entry_source = choose_initial_price(contract)
    reasons = []
    score = bot.safe_float(contract.get("score"))
    score_requirement = (
        ORCA_CONSERVATIVE_MIN_SCORE
        if minimum_score is None else float(minimum_score)
    )
    edge_requirement = (
        ORCA_MIN_DIRECTION_EDGE
        if minimum_edge is None else float(minimum_edge)
    )
    strong_score_exception = (
        allow_strong_score_exception
        and score >= 70.0
        and direction_edge >= 12.0
    )
    dte = contract.get("dte")
    if score < score_requirement and not strong_score_exception:
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
    if dte is None or dte < SCORE_MIN_DTE or dte > SCORE_MAX_DTE:
        reasons.append("WAIT - DATA INCOMPLETE")
    if contract.get("theta") is None or contract.get("iv") is None:
        reasons.append("WAIT - DATA INCOMPLETE")
    if bot.safe_float(contract.get("completeness_percent")) < CONSERVATIVE_MIN_COMPLETENESS:
        reasons.append("WAIT - DATA INCOMPLETE")
    if direction_edge < edge_requirement:
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
    if (
        contract.get("spread_percent") is None
        and entry_source != "Delayed Session Close (Reference Only)"
        and score < ORCA_CONSERVATIVE_MIN_SCORE + 5
    ):
        reasons.append("WAIT - SPREAD UNAVAILABLE")
    if entry <= 0:
        reasons.append("WAITING FOR PRICE CONFIRMATION")
    if REQUIRE_FRESH_OPTION_ENTRY and entry_source != "Fresh Bid/Ask Midpoint":
        reasons.append("WAIT - LIVE OPTION PRICE REQUIRED")
    confidence = calculate_trade_confidence(
        contract, direction_edge, gamma_state, entry_source
    )
    delayed_reference_allowed = (
        entry_source == "Delayed Session Close (Reference Only)"
        and confidence["score"] >= CONSERVATIVE_MEDIUM_CONFIDENCE
    )
    if confidence["level"] != "HIGH" and not delayed_reference_allowed:
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


def _macd_contract_assessment(direction, contract, gamma_info, stock_price, minimum_score):
    """Keep contract safeguards without letting Gamma/flow vote on MACD side."""
    if not contract:
        return {
            "can_open": False, "wait_status": "WAIT - NO QUALIFIED CONTRACT",
            "wait_reasons": ["WAIT - NO QUALIFIED CONTRACT"],
            "trade_confidence": {"score": 0, "level": "LOW", "components": {}},
            "gamma_direction_state": gamma_direction_state(direction, stock_price, gamma_info),
        }
    entry, source = choose_initial_price(contract)
    score = bot.safe_float(contract.get("score"))
    completeness = bot.safe_float(contract.get("completeness_percent"))
    reasons = list(contract.get("rejection_reasons") or [])
    if not contract.get("selection_eligible"):
        reasons.append("WAIT - CONTRACT QUALITY FILTERS")
    if score < minimum_score:
        reasons.append("WAIT - SCORE TOO LOW")
    if completeness < CONSERVATIVE_MIN_COMPLETENESS:
        reasons.append("WAIT - DATA INCOMPLETE")
    if entry <= 0 or source != "Fresh Bid/Ask Midpoint":
        reasons.append("WAIT - LIVE OPTION PRICE REQUIRED")
    # Normalize only quality components; do not invent a MACD success probability
    # or carry old Gamma/relative-flow votes into the HIGH-confidence gate.
    components = {
        "contract_score": score * 0.45,
        "data_completeness": completeness * 0.20,
        "entry_price_quality": 7.0 if source == "Fresh Bid/Ask Midpoint" else 0,
        "spread_availability": 3.0 if contract.get("spread_percent") is not None else 0,
    }
    value = round(min(100.0, sum(components.values()) / 0.75), 1)
    confidence = {
        "score": value, "level": _confidence_class(value),
        "components": components, "method": "contract_quality_not_success_probability",
    }
    if confidence["level"] != "HIGH":
        reasons.append("WAIT - CONFIDENCE NOT HIGH")
    reasons = list(dict.fromkeys(reasons))
    return {
        "can_open": not reasons, "wait_status": reasons[0] if reasons else "TRADE",
        "wait_reasons": reasons, "trade_confidence": confidence,
        "entry_price": entry, "entry_source": source,
        "gamma_direction_state": gamma_direction_state(direction, stock_price, gamma_info),
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
        try:
            parsed = bot.datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=bot.NEW_YORK_TZ)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
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


def _ichimoku_30m_signal_from_payload(payload, now=None):
    """Validate a closed 30-minute Ichimoku event payload."""
    payload = payload if isinstance(payload, dict) else {}
    direction = _tradingview_direction(payload)
    signal_type = str(payload.get("signal_type") or "").upper().strip()
    if direction not in ("CALL", "PUT") or signal_type != f"ICHIMOKU_{direction}":
        raise ValueError("signal must be ICHIMOKU_CALL or ICHIMOKU_PUT")

    interval = str(payload.get("interval") or payload.get("timeframe") or "")
    interval = interval.strip().lower().replace(" ", "")
    if interval not in {"30", "30m", "30min", "30minute", "30minutes"}:
        raise ValueError("Ichimoku trigger must use the 30-minute timeframe")

    symbol = _tradingview_symbol(
        payload.get("ticker") or payload.get("symbol")
    )
    if not symbol or symbol not in bot.WATCHLIST:
        raise ValueError("ticker is not in the ORCA watchlist")
    if symbol in bot.EXCLUDED_SYMBOLS:
        raise ValueError("ticker is excluded from contract selection")

    bar_time_seconds = _unix_seconds(payload.get("bar_time"))
    if bar_time_seconds is None:
        raise ValueError("30-minute Ichimoku signal is missing bar_time")
    source = str(payload.get("source") or "external_webhook")[:80]
    max_age_seconds = (
        ICHIMOKU_30M_BAR_MAX_AGE_SECONDS
        if source == "orca_internal_massive"
        else ICHIMOKU_30M_SIGNAL_MAX_AGE_SECONDS
    )
    now = now or bot.now_new_york()
    age_seconds = now.timestamp() - bar_time_seconds
    if age_seconds < -30:
        raise ValueError("30-minute Ichimoku bar_time is in the future")
    if age_seconds > max_age_seconds:
        raise ValueError("30-minute Ichimoku signal is stale")

    price = bot.safe_float(payload.get("price"))
    if price <= 0:
        raise ValueError("30-minute Ichimoku signal has no valid stock price")

    bar_time_ms = int(round(bar_time_seconds * 1000))
    return {
        "symbol": symbol,
        "direction": direction,
        "signal_type": signal_type,
        "timeframe": "30",
        "bar_time_ms": bar_time_ms,
        "bar_time_seconds": bar_time_seconds,
        "event_id": f"{symbol}:{direction}:{bar_time_ms}",
        "price": price,
        "source": source,
    }


def _completed_regular_30m_bars(bars, now=None):
    """Normalize only completed, regular-session 30-minute stock bars."""
    now = now or bot.now_new_york()
    if now.tzinfo is None:
        now = now.replace(tzinfo=bot.NEW_YORK_TZ)
    else:
        now = now.astimezone(bot.NEW_YORK_TZ)
    now_seconds = now.timestamp()
    normalized = {}

    if not isinstance(bars, list):
        raise ValueError("30-minute aggregate response has no bars")

    for raw in bars:
        if not isinstance(raw, dict):
            continue
        start_seconds = _unix_seconds(raw.get("t"))
        high = _present_number(raw.get("h"))
        low = _present_number(raw.get("l"))
        close = _present_number(raw.get("c"))
        if (
            start_seconds is None
            or high is None
            or low is None
            or close is None
            or min(high, low, close) <= 0
            or high < low
            or close < low
            or close > high
        ):
            continue

        start = bot.datetime.fromtimestamp(
            start_seconds, bot.NEW_YORK_TZ
        )
        start_minute = start.hour * 60 + start.minute
        market_open_minute = 9 * 60 + 30
        if (
            start_minute < market_open_minute
            or start_minute >= 16 * 60
            or (start_minute - market_open_minute) % 30 != 0
            or start.second != 0
            or start.microsecond != 0
        ):
            continue

        close_seconds = start_seconds + 30 * 60
        if close_seconds > now_seconds:
            continue
        normalized[int(round(start_seconds * 1000))] = {
            "start_seconds": start_seconds,
            "close_seconds": close_seconds,
            "high": high,
            "low": low,
            "close": close,
        }

    return [normalized[key] for key in sorted(normalized)]


def _ichimoku_30m_signal_from_bars(symbol, bars, now=None):
    """Calculate one closed-bar Ichimoku crossing from Massive aggregates."""
    symbol = str(symbol or "").upper().strip()
    if not symbol or symbol not in bot.WATCHLIST:
        raise ValueError("ticker is not in the ORCA watchlist")
    if symbol in bot.EXCLUDED_SYMBOLS:
        raise ValueError("ticker is excluded from contract selection")

    completed = _completed_regular_30m_bars(bars, now=now)
    minimum_bars = 3 * ICHIMOKU_30M_CLOUD_DISPLACEMENT + 1
    if len(completed) < minimum_bars:
        raise ValueError(
            f"insufficient completed 30-minute bars ({len(completed)}/{minimum_bars})"
        )

    now = now or bot.now_new_york()
    if now.tzinfo is None:
        now = now.replace(tzinfo=bot.NEW_YORK_TZ)
    else:
        now = now.astimezone(bot.NEW_YORK_TZ)
    latest = completed[-1]
    age_seconds = now.timestamp() - latest["close_seconds"]
    if age_seconds < -30:
        raise ValueError("latest completed 30-minute bar is in the future")
    if age_seconds > ICHIMOKU_30M_BAR_MAX_AGE_SECONDS:
        raise ValueError(
            f"latest completed 30-minute bar is stale ({int(age_seconds)}s old)"
        )

    highs = [bar["high"] for bar in completed]
    lows = [bar["low"] for bar in completed]
    closes = [bar["close"] for bar in completed]

    def midpoint(index, length):
        start = index - length + 1
        if start < 0:
            return None
        return (
            max(highs[start:index + 1]) + min(lows[start:index + 1])
        ) / 2.0

    def levels(index):
        tenkan = midpoint(index, 9)
        kijun = midpoint(index, 26)
        cloud_source_index = index - ICHIMOKU_30M_CLOUD_DISPLACEMENT
        span_a = (
            midpoint(cloud_source_index, 9)
            + midpoint(cloud_source_index, 26)
        ) / 2.0
        span_b = midpoint(cloud_source_index, 52)
        return {
            "tenkan": tenkan,
            "kijun": kijun,
            "cloud_top": max(span_a, span_b),
            "cloud_bottom": min(span_a, span_b),
        }

    current_index = len(completed) - 1
    previous_levels = levels(current_index - 1)
    current_levels = levels(current_index)
    previous_close = closes[current_index - 1]
    current_close = closes[current_index]

    crossed_up = any(
        previous_close <= previous_levels[name]
        and current_close > current_levels[name]
        for name in ("tenkan", "kijun", "cloud_top")
    )
    crossed_down = any(
        previous_close >= previous_levels[name]
        and current_close < current_levels[name]
        for name in ("tenkan", "kijun", "cloud_bottom")
    )
    if crossed_up == crossed_down:
        return None

    direction = "CALL" if crossed_up else "PUT"
    close_seconds = latest["close_seconds"]
    return {
        "ticker": symbol,
        "direction": direction,
        "signal_type": f"ICHIMOKU_{direction}",
        "interval": "30",
        "price": current_close,
        "bar_time": int(round(close_seconds * 1000)),
        "source": "orca_internal_massive",
        "bar_age_seconds": round(age_seconds, 2),
    }


def _register_ichimoku_30m_signal(payload, now=None):
    """Persist the newest valid 30-minute crossing and reject duplicate bars."""
    signal = _ichimoku_30m_signal_from_payload(payload, now=now)
    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        signals = store.setdefault("ichimoku_30m_signals", {})
        previous = signals.get(signal["symbol"])
        if isinstance(previous, dict):
            if previous.get("event_id") == signal["event_id"]:
                if previous.get("consumed"):
                    return None, "duplicate 30-minute Ichimoku event"
                return dict(previous), None
            previous_time = bot.safe_float(previous.get("bar_time_ms"))
            if previous_time >= signal["bar_time_ms"]:
                return None, "older 30-minute Ichimoku event"

        last_contract_directions = (
            store.get("ichimoku_30m_last_contract_directions") or {}
        )
        if last_contract_directions.get(signal["symbol"]) == signal["direction"]:
            return None, "same-direction Ichimoku contract already sent"

        signal.update(
            consumed=False,
            received_at=(now or bot.now_new_york()).isoformat(timespec="seconds"),
        )
        signals[signal["symbol"]] = signal
        bot._save_trade_store(store)
    return signal, None


def _active_ichimoku_30m_signal(symbol, now=None):
    """Return an unconsumed, fresh 30-minute crossover for one symbol."""
    symbol = str(symbol or "").upper().strip()
    if not symbol:
        return None
    now = now or bot.now_new_york()
    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        signal = (store.get("ichimoku_30m_signals") or {}).get(symbol)
        if not isinstance(signal, dict) or signal.get("consumed"):
            return None
        if signal.get("source") != "orca_internal_massive":
            return None
        event_time = _unix_seconds(signal.get("bar_time_ms"))
        if event_time is None:
            return None
        age_seconds = now.timestamp() - event_time
        if (
            age_seconds < -30
            or age_seconds > ICHIMOKU_30M_BAR_MAX_AGE_SECONDS
        ):
            return None
        return dict(signal)


def _consume_ichimoku_30m_signal(symbol, event_id):
    """Mark a 30-minute crossover used after a trade is opened or duplicated."""
    symbol = str(symbol or "").upper().strip()
    if not symbol or not event_id:
        return False
    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        signals = store.get("ichimoku_30m_signals") or {}
        signal = signals.get(symbol)
        if not isinstance(signal, dict) or signal.get("event_id") != event_id:
            return False
        last_contract_directions = (
            store.get("ichimoku_30m_last_contract_directions") or {}
        )
        if signal.get("consumed"):
            if last_contract_directions.get(symbol) != signal["direction"]:
                last_contract_directions[symbol] = signal["direction"]
                store["ichimoku_30m_last_contract_directions"] = (
                    last_contract_directions
                )
                bot._save_trade_store(store)
            return True
        signal["consumed"] = True
        signal["consumed_at"] = bot.now_new_york().isoformat(timespec="seconds")
        signals[symbol] = signal
        store["ichimoku_30m_signals"] = signals
        last_contract_directions[symbol] = signal["direction"]
        store["ichimoku_30m_last_contract_directions"] = (
            last_contract_directions
        )
        bot._save_trade_store(store)
        return True


def _newest_timestamp(values):
    values = [_unix_seconds(value) for value in values]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _option_reference_mark(raw):
    """Return the best available mark for conservative parity inference."""
    quote = raw.get("last_quote", {}) or {}
    trade = raw.get("last_trade", {}) or {}
    day = raw.get("day", {}) or {}
    midpoint = _present_number(quote.get("midpoint"))
    if midpoint is not None and midpoint > 0:
        return midpoint
    bid = _present_number(
        quote.get("bid") if quote.get("bid") is not None else quote.get("bid_price")
    )
    ask = _present_number(
        quote.get("ask") if quote.get("ask") is not None else quote.get("ask_price")
    )
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        return (bid + ask) / 2.0
    for value in (
        ask,
        bid,
        _present_number(trade.get("price")),
        _present_number(day.get("close")),
        _present_number(day.get("open")),
    ):
        if value is not None and value > 0:
            return value
    return None


def _infer_underlying_from_put_call_parity(call_raw, put_raw):
    """Infer spot from paired same-strike options when the stock endpoint is limited."""
    puts_by_key = {}
    for raw in put_raw or []:
        details = raw.get("details", {}) or {}
        expiration = details.get("expiration_date")
        strike = _present_number(details.get("strike_price"))
        mark = _option_reference_mark(raw)
        if expiration and strike is not None and mark is not None:
            puts_by_key[(str(expiration), round(strike, 4))] = mark

    estimates = []
    for raw in call_raw or []:
        details = raw.get("details", {}) or {}
        expiration = details.get("expiration_date")
        strike = _present_number(details.get("strike_price"))
        call_mark = _option_reference_mark(raw)
        put_mark = puts_by_key.get(
            (str(expiration), round(strike, 4))
        ) if expiration and strike is not None else None
        if (
            call_mark is None
            or put_mark is None
            or strike is None
            or call_mark <= 0
            or put_mark <= 0
            or strike <= 0
        ):
            continue
        estimate = call_mark - put_mark + strike
        # Reject obviously stale/mismatched pairs before taking the median.
        if strike * 0.50 <= estimate <= strike * 1.50:
            estimates.append(estimate)

    if len(estimates) < 5:
        return 0.0
    estimates.sort()
    middle = len(estimates) // 2
    if len(estimates) % 2:
        return estimates[middle]
    return (estimates[middle - 1] + estimates[middle]) / 2.0


def _infer_underlying_with_parity(call_raw, put_raw):
    """Use the original inference first, then paired option parity as a fallback."""
    price, source = bot.infer_underlying_price(call_raw, put_raw)
    if price > 0:
        return price, source
    parity_price = _infer_underlying_from_put_call_parity(call_raw, put_raw)
    if parity_price > 0:
        return parity_price, "options_parity_inference"
    return 0.0, "options_inference"


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
    """Format a usable modeled Gamma Flip; omit unavailable results."""
    if state.get("gamma_policy") == "disabled":
        return "OFF", "DISABLED"
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

    return None, None


def _paste_card_logo(image, symbol, box):
    """Fill the logo square, removing transparent margins without distortion."""
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
            fill=(0, 19, 56),
        )
        return

    # Clearbit assets often include transparent padding around the visible mark.
    # Crop that padding first, then use a cover fit so the card square is filled.
    alpha = logo.getchannel("A") if logo.mode == "RGBA" else None
    visible_bounds = alpha.getbbox() if alpha is not None else logo.getbbox()
    if visible_bounds:
        logo = logo.crop(visible_bounds)

    scale = max(inner_width / logo.width, inner_height / logo.height)
    resized = logo.resize(
        (
            max(inner_width, round(logo.width * scale)),
            max(inner_height, round(logo.height * scale)),
        ),
        bot.Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - inner_width) // 2)
    top = max(0, (resized.height - inner_height) // 2)
    fitted = resized.crop((left, top, left + inner_width, top + inner_height))
    image.paste(fitted, (x1 + 4, y1 + 4), fitted if fitted.mode == "RGBA" else None)


def _card_is_tracking(state, monitor_enabled=None):
    monitor_enabled = (
        AUTO_MONITOR_ENABLED if monitor_enabled is None else bool(monitor_enabled)
    )
    return bool(
        monitor_enabled and state.get("last_successful_option_price_update")
    )


def _card_badge(state, monitor_enabled=None):
    status = str(state.get("status", "active")).lower()
    if status == "test":
        return "TEST"
    if status == "active":
        return "TRACKING" if _card_is_tracking(state, monitor_enabled) else "SIGNAL"
    if status == "stopped":
        return "STOPPED"
    return "COMPLETED"


def _card_price_metrics(state, monitor_enabled=None):
    """Choose labels that distinguish a quote snapshot from tracked prices."""
    monitor_enabled = (
        AUTO_MONITOR_ENABLED if monitor_enabled is None else bool(monitor_enabled)
    )
    monitor_enabled = (
        AUTO_MONITOR_ENABLED if monitor_enabled is None else bool(monitor_enabled)
    )
    entry = bot.safe_float(state.get("entry_price"))
    midpoint = bot.safe_float(state.get("midpoint"))
    entry_mark = midpoint if midpoint > 0 else entry
    is_tracking = _card_is_tracking(state, monitor_enabled)
    entry_label = "ENTRY MID" if midpoint > 0 else "ENTRY PRICE"

    if is_tracking:
        current = bot.safe_float(state.get("current_price"), entry)
        change = current - entry
        change_percent = ((current / entry) - 1) * 100 if entry else 0.0
        return [
            (75, entry_label, f"${entry_mark:.3f}", "value"),
            (390, "CURRENT MARK", f"${current:.2f}", "value"),
            (705, "CHANGE", f"{change:+.2f} ({change_percent:+.1f}%)", "change"),
        ]

    bid = bot.safe_float(state.get("bid"))
    ask = bot.safe_float(state.get("ask"))
    bid_ask = f"${bid:.2f} / ${ask:.2f}" if bid > 0 and ask >= bid else "N/A"
    update_state = "WAITING" if monitor_enabled else "OFF"
    return [
        (75, entry_label, f"${entry_mark:.3f}", "value"),
        (390, "ENTRY BID / ASK", bid_ask, "value"),
        (705, "PRICE UPDATES", update_state, "status"),
    ]


def render_trade_card(state):
    """Render the user-approved card layout without changing trading behavior."""
    from card_renderer import render_card
    return render_card(
        state, bot=bot, gamma_values=_card_gamma_values,
        is_tracking=_card_is_tracking, paste_logo=_paste_card_logo,
        signal_type=macd_direction.SIGNAL_TYPE, monitor_enabled=AUTO_MONITOR_ENABLED,
    )


def _telegram_value(value, suffix="", decimals=2):
    """Format an observed contract value without inventing unavailable data."""
    if value in (None, ""):
        return "N/A"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{value}{suffix}"
    return f"{number:.{decimals}f}{suffix}"


def _telegram_contract_caption(state):
    """Keep internal contract attributes out of the visible Telegram caption."""
    return ""


def _telegram_response_error(response):
    """Return Telegram's useful API description without exposing credentials."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        description = payload.get("description")
        error_code = payload.get("error_code")
        if description:
            return f"Telegram API {error_code or response.status_code}: {description}"
    text = (response.text or "").strip().replace("\n", " ")
    return f"Telegram HTTP {response.status_code}: {text[:300] or 'empty response'}"


def _telegram_message_id(response):
    """Validate both HTTP success and Telegram's JSON success flag."""
    if not response.ok:
        raise RuntimeError(_telegram_response_error(response))
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("Telegram returned invalid JSON") from error
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError(_telegram_response_error(response))
    message_id = (payload.get("result") or {}).get("message_id")
    if message_id is None:
        raise RuntimeError("Telegram response did not contain message_id")
    return message_id


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
                          "media": json.dumps({
                              "type": "photo",
                              "media": "attach://photo",
                              "caption": _telegram_contract_caption(state),
                          })},
                    files={"photo": ("reef_options.png", image, "image/png")}, timeout=20)
            if response.status_code == 400 and "message is not modified" in response.text.lower():
                return True, None
            if not response.ok:
                raise RuntimeError(_telegram_response_error(response))
            try:
                payload = response.json()
            except ValueError as error:
                raise RuntimeError("Telegram returned invalid JSON") from error
            if payload.get("ok") is not True:
                raise RuntimeError(_telegram_response_error(response))
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
    """Send the exact contract card and retry transient Telegram failures."""
    token, chat_id = bot.env("TELEGRAM_BOT_TOKEN"), bot.env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
    error_text = None
    for attempt in range(1, TELEGRAM_SEND_ATTEMPTS + 1):
        image_path = None
        try:
            image_path = _render_unique_card(state)
            send_data = {"chat_id": chat_id}
            if str(state.get("status", "")).lower() == "test":
                send_data["caption"] = (
                    "اختبار إرسال بطاقة فقط — ليست توصية تداول.\n"
                    "بيانات العقد والأسعار افتراضية؛ لم تُفتح أو تُسجّل صفقة."
                )
            with image_path.open("rb") as image:
                response = requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data=send_data,
                    files={"photo": ("reef_options.png", image, "image/png")},
                    timeout=20,
                )
            return _telegram_message_id(response)
        except Exception as error:
            error_text = str(error)
            print(f"Telegram send attempt {attempt} failed:", error_text)
            retryable = (
                isinstance(error, requests.RequestException)
                or "Telegram HTTP 429" in error_text
                or "Telegram HTTP 5" in error_text
            )
            if attempt >= TELEGRAM_SEND_ATTEMPTS or not retryable:
                raise RuntimeError(error_text) from error
            time.sleep(TELEGRAM_EDIT_RETRY_SECONDS)
        finally:
            if image_path:
                try:
                    image_path.unlink()
                except OSError:
                    pass
    raise RuntimeError(error_text or "Telegram send failed")


def _massive_error(response, endpoint):
    """Keep provider failures actionable without logging credentials."""
    text = (getattr(response, "text", "") or "").strip().replace("\n", " ")
    return (
        f"MASSIVE_HTTP_{getattr(response, 'status_code', 'UNKNOWN')} "
        f"{endpoint}: {text[:240] or 'empty response'}"
    )


def _massive_rate_limit_delay(response):
    retry_after = None
    try:
        retry_after = float(response.headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        pass
    return max(
        1.0,
        min(
            120.0,
            retry_after if retry_after is not None
            else MASSIVE_RATE_LIMIT_BACKOFF_SECONDS,
        ),
    )


def _provider_is_tradier():
    return DATA_PROVIDER in ("tradier", "tradier_live")


def _provider_is_thetadata():
    return DATA_PROVIDER in ("thetadata", "theta", "thetadata_live")


def _provider_is_marketdata():
    return DATA_PROVIDER in ("marketdata", "marketdata_app", "marketdata_live")


def _provider_token():
    if _provider_is_tradier():
        return bot.env("TRADIER_TOKEN")
    if _provider_is_thetadata():
        return bot.env("THETADATA_API_KEY")
    if _provider_is_marketdata():
        return bot.env("MARKETDATA_API_KEY")
    raise RuntimeError(f"Unsupported or removed data provider: {DATA_PROVIDER}")


def _marketdata_account_credit_limit_reason():
    return _MARKETDATA_QUOTA.blocked_reason()


def _latch_marketdata_account_credit_limit(reason, headers=None):
    return _MARKETDATA_QUOTA.block(reason, headers)


def _marketdata_error(response, endpoint):
    """Keep MarketData failures actionable without logging credentials."""
    text = (getattr(response, "text", "") or "").strip().replace("\n", " ")
    return (
        f"MARKETDATA_HTTP_{getattr(response, 'status_code', 'UNKNOWN')} "
        f"{endpoint}: {text[:240] or 'empty response'}"
    )


def _marketdata_get(endpoint, params=None):
    """Fetch MarketData JSON and accept 203 cached responses as successful."""
    blocked_reason = _marketdata_account_credit_limit_reason()
    if blocked_reason:
        raise MarketDataAccountCreditLimitError(
            f"MarketData account credit limit remains blocked: {blocked_reason}"
        )

    token = bot.env("MARKETDATA_API_KEY")
    if not token:
        raise RuntimeError("MARKETDATA_API_KEY is missing")
    _MARKETDATA_QUOTA.note_request(endpoint)
    response = requests.get(
        f"{MARKETDATA_BASE_URL}{endpoint}",
        params=dict(params or {}),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=MARKETDATA_REQUEST_TIMEOUT,
    )
    response_headers = getattr(response, "headers", {}) or {}
    _MARKETDATA_QUOTA.observe(response_headers)
    if response.status_code not in (200, 203):
        error_message = _marketdata_error(response, endpoint)
        response_text = (
            getattr(response, "text", "") or ""
        ).casefold()
        if (
            response.status_code == 429
            and "credit limit" in response_text
        ):
            blocked_reason = _latch_marketdata_account_credit_limit(
                error_message, response_headers
            )
            raise MarketDataAccountCreditLimitError(blocked_reason)
        raise RuntimeError(error_message)
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(f"MARKETDATA_INVALID_JSON {endpoint}") from error
    if not isinstance(payload, dict) or payload.get("s") not in (None, "ok"):
        status = payload.get("s") if isinstance(payload, dict) else "unknown"
        raise RuntimeError(f"MARKETDATA_RESPONSE_{status} {endpoint}")
    return payload


def _marketdata_column(payload, key, index, default=None):
    values = (payload or {}).get(key)
    if isinstance(values, list) and index < len(values):
        return values[index]
    return default


def _marketdata_expiration(value):
    if isinstance(value, str) and len(value) == 10 and value[4] == "-":
        return value
    timestamp = _unix_seconds(value)
    if timestamp is None:
        return ""
    return bot.datetime.fromtimestamp(timestamp, bot.NEW_YORK_TZ).date().isoformat()


def _marketdata_records(payload):
    """Translate MarketData columnar option responses to the bot's shape."""
    symbols = (payload or {}).get("optionSymbol")
    if not isinstance(symbols, list):
        return []

    records = []
    for index, option_symbol in enumerate(symbols):
        ticker = bot.canonical_option_ticker(option_symbol)
        if not ticker:
            continue
        side = str(_marketdata_column(payload, "side", index, "") or "").lower()
        if side not in ("call", "put"):
            side = "call" if "C" in ticker[-9:] else "put"
        expiration = _marketdata_expiration(
            _marketdata_column(payload, "expiration", index)
        )
        strike = _present_number(_marketdata_column(payload, "strike", index))
        updated = _marketdata_column(payload, "updated", index)
        last = _marketdata_column(payload, "last", index)
        records.append({
            "details": {
                "ticker": ticker,
                "expiration_date": expiration,
                "strike_price": strike,
                "contract_type": side,
            },
            "last_quote": {
                "bid": _marketdata_column(payload, "bid", index),
                "ask": _marketdata_column(payload, "ask", index),
                "last_updated": updated,
            },
            "last_trade": {
                "price": last,
                "timestamp": updated,
            },
            "day": {
                "volume": _marketdata_column(payload, "volume", index),
                "close": last,
                "open": None,
            },
            "open_interest": _marketdata_column(payload, "openInterest", index),
            "implied_volatility": _marketdata_column(payload, "iv", index),
            "greeks": {
                "delta": _marketdata_column(payload, "delta", index),
                "gamma": _marketdata_column(payload, "gamma", index),
                "theta": _marketdata_column(payload, "theta", index),
                "vega": _marketdata_column(payload, "vega", index),
            },
            "underlying_price": _marketdata_column(
                payload, "underlyingPrice", index
            ),
        })
    return records


def _marketdata_raw_chain(symbol):
    # A concurrent cache miss must not purchase the same complete chain twice.
    # Do not extend the cache TTL or filter Gamma's universe to save credits.
    with _MARKETDATA_CHAIN_FETCH_LOCK:
        return _marketdata_raw_chain_once(symbol)


def _marketdata_raw_chain_once(symbol):
    symbol = str(symbol or "").upper().strip()
    today = bot.now_new_york().date()
    expiration_from = (today + timedelta(days=SCORE_MIN_DTE)).isoformat()
    expiration_to = (today + timedelta(days=SCORE_MAX_DTE)).isoformat()
    cache_key = (
        "marketdata", symbol, expiration_from, expiration_to
    )
    now = time.monotonic()
    with _MASSIVE_CHAIN_CACHE_LOCK:
        cached = _MASSIVE_CHAIN_CACHE.get(cache_key)
        if (
            isinstance(cached, dict)
            and now - cached["timestamp"] <= MARKETDATA_CHAIN_CACHE_TTL_SECONDS
        ):
            _MARKETDATA_QUOTA.note_chain_cache_hit()
            return list(cached["results"])

    payload = _marketdata_get(
        f"/v1/options/chain/{symbol}/",
        params={"from": expiration_from, "to": expiration_to},
    )
    records = _marketdata_records(payload)
    with _MASSIVE_CHAIN_CACHE_LOCK:
        _MASSIVE_CHAIN_CACHE[cache_key] = {
            "timestamp": time.monotonic(),
            "results": list(records),
        }
    return records


def _marketdata_chain(symbol, direction):
    wanted = "call" if str(direction).upper() == "CALL" else "put"
    return [
        record for record in _marketdata_raw_chain(symbol)
        if (record.get("details") or {}).get("contract_type") == wanted
        and _contract_in_dte_window(
            (record.get("details") or {}).get("expiration_date")
        )
    ]


def _marketdata_contract_snapshot(symbol, contract_ticker):
    canonical = bot.canonical_option_ticker(contract_ticker)
    payload = _marketdata_get(f"/v1/options/quotes/{canonical}/")
    records = _marketdata_records(payload)
    if not records:
        raise RuntimeError(
            f"MARKETDATA option quote unavailable for {symbol} {contract_ticker}"
        )
    return {"results": [records[0]]}


def _marketdata_underlying_price(symbol, call_raw, put_raw):
    for record in list(call_raw or []) + list(put_raw or []):
        price = _present_number(record.get("underlying_price"))
        if price is not None and price > 0:
            return price, "marketdata_underlying_price"
    price, source = _infer_underlying_with_parity(call_raw, put_raw)
    return price, source


def _thetadata_error(response, endpoint):
    text = (getattr(response, "text", "") or "").strip().replace("\n", " ")
    return (
        f"THETADATA_HTTP_{getattr(response, 'status_code', 'UNKNOWN')} "
        f"{endpoint}: {text[:240] or 'empty response'}"
    )


def _thetadata_get(endpoint, params=None):
    """Call the local Theta Terminal; its API key is consumed by the terminal."""
    if not bot.env("THETADATA_API_KEY"):
        raise RuntimeError("THETADATA_API_KEY is missing")
    query = dict(params or {})
    query.setdefault("format", "json")
    response = requests.get(
        f"{THETADATA_BASE_URL}{endpoint}",
        params=query,
        headers={"Accept": "application/json"},
        timeout=THETADATA_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise RuntimeError(_thetadata_error(response, endpoint))
    try:
        return response.json()
    except ValueError as error:
        raise RuntimeError(f"THETADATA_INVALID_JSON {endpoint}") from error


def _theta_expiration_date(value):
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _theta_record_key(record):
    record = record if isinstance(record, dict) else {}
    expiration = _theta_expiration_date(record.get("expiration"))
    strike = _present_number(record.get("strike"))
    right = str(record.get("right") or "").lower()
    if not expiration or strike is None or right not in ("call", "put"):
        return None
    return expiration, round(strike, 6), right


def _theta_occ_ticker(symbol, expiration, right, strike):
    expiry = bot.datetime.strptime(
        _theta_expiration_date(expiration), "%Y-%m-%d"
    )
    right_code = "C" if str(right).lower() == "call" else "P"
    strike_field = str(int(round(float(strike) * 1000))).zfill(8)
    return (
        f"{str(symbol).upper().replace('/', '.')}{expiry:%y%m%d}"
        f"{right_code}{strike_field}"
    )


def _thetadata_expirations(symbol):
    symbol = str(symbol or "").upper().strip()
    now = time.monotonic()
    with _THETADATA_EXPIRATION_CACHE_LOCK:
        cached = _THETADATA_EXPIRATION_CACHE.get(symbol)
        if cached and now - cached["timestamp"] <= 300:
            return list(cached["dates"])
    payload = _thetadata_get(
        "/v3/option/list/expirations", {"symbol": symbol}
    )
    dates = sorted({
        _theta_expiration_date(item.get("expiration"))
        for item in _as_list(payload)
        if isinstance(item, dict) and item.get("expiration")
    })
    with _THETADATA_EXPIRATION_CACHE_LOCK:
        _THETADATA_EXPIRATION_CACHE[symbol] = {
            "timestamp": time.monotonic(),
            "dates": list(dates),
        }
    return dates


def _thetadata_raw_chain(symbol):
    """Merge Theta quote, OHLC, OI, and Greeks snapshots by contract identity."""
    cache_key = ("thetadata", str(symbol).upper().strip())
    now = time.monotonic()
    with _MASSIVE_CHAIN_CACHE_LOCK:
        cached = _MASSIVE_CHAIN_CACHE.get(cache_key)
        if (
            isinstance(cached, dict)
            and now - cached["timestamp"] <= MASSIVE_CHAIN_CACHE_TTL_SECONDS
        ):
            return list(cached["results"])

    symbol = str(symbol or "").upper().strip()
    allowed = set()
    today = bot.now_new_york().date()
    for expiration in _thetadata_expirations(symbol):
        try:
            expiry = bot.datetime.strptime(
                expiration, "%Y-%m-%d"
            ).date()
        except (TypeError, ValueError):
            continue
        if SCORE_MIN_DTE <= (expiry - today).days <= SCORE_MAX_DTE:
            allowed.add(expiration)
        if len(allowed) >= THETADATA_MAX_EXPIRATIONS:
            break

    common = {
        "symbol": symbol,
        "expiration": "*",
        "right": "both",
        "max_dte": SCORE_MAX_DTE,
    }
    quote_rows = _as_list(
        _thetadata_get("/v3/option/snapshot/quote", common)
    )
    ohlc_rows = _as_list(
        _thetadata_get("/v3/option/snapshot/ohlc", common)
    )
    oi_rows = _as_list(
        _thetadata_get("/v3/option/snapshot/open_interest", common)
    )
    greek_rows = _as_list(
        _thetadata_get("/v3/option/snapshot/greeks/first_order", common)
    )

    by_key = {}
    for group_name, rows in (
        ("quote", quote_rows),
        ("ohlc", ohlc_rows),
        ("open_interest", oi_rows),
        ("greeks", greek_rows),
    ):
        for row in rows:
            key = _theta_record_key(row)
            if key is None:
                continue
            expiration = key[0]
            if expiration not in allowed:
                continue
            by_key.setdefault(key, {})[group_name] = row

    records = []
    for (expiration, strike, right), groups in by_key.items():
        quote = groups.get("quote") or {}
        ohlc = groups.get("ohlc") or {}
        oi = groups.get("open_interest") or {}
        greeks = groups.get("greeks") or {}
        ticker = _theta_occ_ticker(symbol, expiration, right, strike)
        quote_time = quote.get("timestamp")
        trade_time = ohlc.get("timestamp") or quote_time
        records.append({
            "details": {
                "ticker": ticker,
                "expiration_date": expiration,
                "strike_price": strike,
                "contract_type": right,
            },
            "last_quote": {
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
                "last_updated": quote_time,
            },
            "last_trade": {
                "price": ohlc.get("close"),
                "timestamp": trade_time,
            },
            "day": {
                "volume": ohlc.get("volume"),
                "close": ohlc.get("close"),
                "open": ohlc.get("open"),
            },
            "open_interest": oi.get("open_interest"),
            "implied_volatility": (
                greeks.get("implied_vol")
                if greeks.get("implied_vol") is not None
                else greeks.get("iv")
            ),
            "greeks": {
                "delta": greeks.get("delta"),
                "theta": greeks.get("theta"),
            },
        })
    with _MASSIVE_CHAIN_CACHE_LOCK:
        _MASSIVE_CHAIN_CACHE[cache_key] = {
            "timestamp": time.monotonic(),
            "results": list(records),
        }
    return records


def _thetadata_chain(symbol, direction):
    wanted = "call" if str(direction).upper() == "CALL" else "put"
    return [
        record for record in _thetadata_raw_chain(symbol)
        if (record.get("details") or {}).get("contract_type") == wanted
    ]


def _theta_contract_parts(contract_ticker):
    canonical = bot.canonical_option_ticker(contract_ticker)
    match = bot.re.fullmatch(
        r"([A-Z.\-]+)(\d{6})([CP])(\d{8})", canonical
    )
    if not match:
        raise ValueError(f"invalid canonical option ticker: {contract_ticker}")
    root, yymmdd, right, strike_field = match.groups()
    expiration = f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    return (
        root.replace(".", "/"),
        expiration,
        "call" if right == "C" else "put",
        int(strike_field) / 1000.0,
    )


def _thetadata_contract_snapshot(symbol, contract_ticker):
    root, expiration, right, strike = _theta_contract_parts(contract_ticker)
    params = {
        "symbol": root,
        "expiration": expiration,
        "right": right,
        "strike": f"{strike:.3f}",
    }
    quote_rows = _as_list(
        _thetadata_get("/v3/option/snapshot/quote", params)
    )
    ohlc_rows = _as_list(
        _thetadata_get("/v3/option/snapshot/ohlc", params)
    )
    quote = quote_rows[0] if quote_rows else {}
    ohlc = ohlc_rows[0] if ohlc_rows else {}
    if not quote and not ohlc:
        raise RuntimeError(
            f"THETADATA option quote unavailable for {symbol} {contract_ticker}"
        )
    return {
        "results": [{
            "details": {
                "ticker": bot.canonical_option_ticker(contract_ticker),
                "expiration_date": expiration,
                "strike_price": strike,
            },
            "last_quote": {
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
                "last_updated": quote.get("timestamp"),
            },
            "last_trade": {
                "price": ohlc.get("close"),
                "timestamp": ohlc.get("timestamp"),
            },
            "day": {
                "open": ohlc.get("open"),
                "close": ohlc.get("close"),
                "volume": ohlc.get("volume"),
            },
        }],
    }


def _thetadata_underlying_price(symbol):
    rows = _as_list(_thetadata_get(
        "/v3/stock/snapshot/quote", {"symbol": symbol}
    ))
    for row in rows:
        bid = _present_number(row.get("bid"))
        ask = _present_number(row.get("ask"))
        if bid is not None and ask is not None and bid > 0 and ask >= bid:
            return (bid + ask) / 2.0, "thetadata_quote_midpoint"
        for value in (bid, ask):
            if value is not None and value > 0:
                return value, "thetadata_quote_side"
    raise RuntimeError(
        f"THETADATA underlying quote unavailable for {symbol}"
    )


def _tradier_error(response, endpoint):
    text = (getattr(response, "text", "") or "").strip().replace("\n", " ")
    return (
        f"TRADIER_HTTP_{getattr(response, 'status_code', 'UNKNOWN')} "
        f"{endpoint}: {text[:240] or 'empty response'}"
    )


def _tradier_get(endpoint, params=None):
    token = bot.env("TRADIER_TOKEN")
    if not token:
        raise RuntimeError("TRADIER_TOKEN is missing")
    response = requests.get(
        f"{TRADIER_BASE_URL}{endpoint}",
        params=dict(params or {}),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=TRADIER_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise RuntimeError(_tradier_error(response, endpoint))
    try:
        return response.json()
    except ValueError as error:
        raise RuntimeError(f"TRADIER_INVALID_JSON {endpoint}") from error


def _as_list(value):
    """Normalize Tradier's one-item object versus array response behavior."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _tradier_expirations(symbol):
    symbol = str(symbol or "").upper().strip()
    now = time.monotonic()
    with _TRADIER_EXPIRATION_CACHE_LOCK:
        cached = _TRADIER_EXPIRATION_CACHE.get(symbol)
        if cached and now - cached["timestamp"] <= 300:
            return list(cached["dates"])

    payload = _tradier_get(
        "/v1/markets/options/expirations",
        {"symbol": symbol, "includeAllRoots": "true"},
    )
    dates = ((payload or {}).get("expirations") or {}).get("date") or []
    dates = [str(value) for value in _as_list(dates) if value]
    dates.sort()
    with _TRADIER_EXPIRATION_CACHE_LOCK:
        _TRADIER_EXPIRATION_CACHE[symbol] = {
            "timestamp": time.monotonic(),
            "dates": list(dates),
        }
    return dates


def _tradier_record(option):
    """Translate one Tradier option record to the existing internal shape."""
    option = option if isinstance(option, dict) else {}
    greeks = option.get("greeks") or {}
    option_type = str(
        option.get("option_type") or option.get("type") or ""
    ).lower()
    quote_stamp = max(
        (
            _unix_seconds(option.get("bid_date")) or 0,
            _unix_seconds(option.get("ask_date")) or 0,
        ),
        default=0,
    )
    trade_stamp = _unix_seconds(option.get("trade_date"))
    return {
        "details": {
            "ticker": option.get("symbol") or "",
            "expiration_date": option.get("expiration_date") or "",
            "strike_price": option.get("strike"),
            "contract_type": option_type,
        },
        "last_quote": {
            "bid": option.get("bid"),
            "ask": option.get("ask"),
            "last_updated": quote_stamp or None,
        },
        "last_trade": {
            "price": option.get("last"),
            "timestamp": trade_stamp,
        },
        "day": {
            "volume": option.get("volume"),
            "close": option.get("close"),
            "open": option.get("open"),
        },
        "open_interest": option.get("open_interest"),
        "implied_volatility": (
            greeks.get("mid_iv")
            if greeks.get("mid_iv") is not None
            else greeks.get("iv")
        ),
        "greeks": {
            "delta": greeks.get("delta"),
            "theta": greeks.get("theta"),
        },
    }


def _tradier_chain(symbol, direction):
    """Fetch near-term Tradier chains once and expose CALL/PUT separately."""
    symbol = str(symbol or "").upper().strip()
    direction = str(direction or "").upper().strip()
    cache_key = ("tradier", symbol, "ALL")
    now = time.monotonic()
    with _MASSIVE_CHAIN_CACHE_LOCK:
        cached = _MASSIVE_CHAIN_CACHE.get(cache_key)
        if (
            isinstance(cached, dict)
            and now - cached["timestamp"] <= MASSIVE_CHAIN_CACHE_TTL_SECONDS
        ):
            records = list(cached["results"])
        else:
            records = None

    if records is None:
        today = bot.now_new_york().date()
        dates = []
        for expiration in _tradier_expirations(symbol):
            try:
                expiry = bot.datetime.strptime(
                    expiration, "%Y-%m-%d"
                ).date()
            except (TypeError, ValueError):
                continue
            dte = (expiry - today).days
            if SCORE_MIN_DTE <= dte <= SCORE_MAX_DTE:
                dates.append(expiration)
            if len(dates) >= TRADIER_MAX_EXPIRATIONS:
                break
        records = []
        for expiration in dates:
            payload = _tradier_get(
                "/v1/markets/options/chains",
                {
                    "symbol": symbol,
                    "expiration": expiration,
                    "greeks": "true",
                },
            )
            options = ((payload or {}).get("options") or {}).get("option") or []
            records.extend(
                _tradier_record(option) for option in _as_list(options)
            )
        with _MASSIVE_CHAIN_CACHE_LOCK:
            _MASSIVE_CHAIN_CACHE[cache_key] = {
                "timestamp": time.monotonic(),
                "results": list(records),
            }

    wanted = "call" if direction == "CALL" else "put"
    return [
        record for record in records
        if str(
            (record.get("details") or {}).get("contract_type") or ""
        ).lower() == wanted
    ]


def _tradier_quote_record(symbol):
    payload = _tradier_get(
        "/v1/markets/quotes",
        {"symbols": str(symbol or "").strip(), "greeks": "true"},
    )
    quote = ((payload or {}).get("quotes") or {}).get("quote") or {}
    return quote if isinstance(quote, dict) else {}


def _tradier_underlying_price(symbol):
    quote = _tradier_quote_record(symbol)
    price = _present_number(
        quote.get("last")
        if quote.get("last") is not None
        else quote.get("close")
    )
    if price is None or price <= 0:
        raise RuntimeError(f"TRADIER underlying quote unavailable for {symbol}")
    return price, "tradier_quote"


def _tradier_contract_snapshot(symbol, contract_ticker):
    quote = _tradier_quote_record(contract_ticker)
    if not quote:
        raise RuntimeError(
            f"TRADIER option quote unavailable for {symbol} {contract_ticker}"
        )
    return {"results": [_tradier_record(quote)]}


def provider_contract_snapshot(symbol, contract_ticker):
    """Return the selected contract snapshot through the configured provider."""
    if _provider_is_thetadata():
        return _thetadata_contract_snapshot(symbol, contract_ticker)
    if _provider_is_tradier():
        return _tradier_contract_snapshot(symbol, contract_ticker)
    if _provider_is_marketdata():
        return _marketdata_contract_snapshot(symbol, contract_ticker)
    raise RuntimeError(f"Unsupported or removed data provider: {DATA_PROVIDER}")


def massive_chain(symbol, direction):
    """Historical entry-point name; dispatch only to permitted providers."""
    if _provider_is_thetadata():
        return _filter_contracts_by_dte(_thetadata_chain(symbol, direction))
    if _provider_is_tradier():
        return _filter_contracts_by_dte(_tradier_chain(symbol, direction))
    if _provider_is_marketdata():
        return _filter_contracts_by_dte(_marketdata_chain(symbol, direction))
    raise RuntimeError(f"Unsupported or removed data provider: {DATA_PROVIDER}")


def _first_valid_stock_price(payload):
    """Return the first positive price field from a Massive stock response."""
    preferred_paths = (
        ("lastTrade", "p"),
        ("last_trade", "p"),
        ("min", "c"),
        ("minute", "c"),
        ("results", "lastTrade", "p"),
        ("results", "last_trade", "p"),
        ("results", "p"),
        ("results", "c"),
        ("p",),
        ("c",),
        ("price",),
        ("close",),
        ("last",),
    )

    def at_path(value, path):
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value

    for path in preferred_paths:
        price = _present_number(at_path(payload, path))
        if price is not None and price > 0:
            return price

    price_keys = {"p", "c", "price", "close", "last"}

    def walk(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in price_keys:
                    price = _present_number(nested)
                    if price is not None and price > 0:
                        return price
                found = walk(nested)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = walk(nested)
                if found is not None:
                    return found
        return None

    return walk(payload)


def _massive_stock_get(path, api_key, params=None):
    """Historical compatibility only: the retired provider cannot make requests."""
    raise RuntimeError("Massive has been permanently removed")


def _fetch_macd_1h_direction(symbol, now=None):
    """TradingView alone supplies direction; never fall back to delayed candles."""
    now = now or bot.now_new_york()
    symbol = str(symbol).upper().strip()
    with _MACD_CACHE_LOCK:
        cached = _MACD_CACHE.get(symbol)
        if cached and cached.get("source") == "tradingview_closed_bar":
            macd_direction.validate_candle(cached, now)
            if cached.get("direction") is not None:
                macd_direction.validate_direction(cached, symbol, cached["direction"], now)
            return dict(cached)
    raise macd_direction.MacdDataError("Waiting for a fresh closed hourly MACD/EMA200 candle from TradingView")


def _macd_health_snapshot():
    """Expose accepted stock signals, never credentials or raw webhook bodies."""
    with _MACD_CACHE_LOCK:
        signals = {symbol: dict(signal) for symbol, signal in _MACD_CACHE.items()}
    now = bot.now_new_york()
    for signal in signals.values():
        try:
            macd_direction.validate_candle(signal, now)
            signal["fresh"] = True
        except macd_direction.MacdDataError:
            signal["fresh"] = False
    return signals


def _fetch_bot_ichimoku_30m_signal(symbol, now=None):
    """Keep historical calculations, but never fetch from the retired provider."""
    return None, "Massive has been permanently removed; automatic Ichimoku polling is disabled"


def _poll_and_register_bot_ichimoku_signal(symbol, now=None):
    """Register a fresh bot-calculated crossing in the durable signal store."""
    payload, error = _fetch_bot_ichimoku_30m_signal(symbol, now=now)
    if error or not payload:
        return None, error or "no fresh bot-calculated signal"
    try:
        return _register_ichimoku_30m_signal(payload, now=now)
    except ValueError as error:
        return None, str(error)


def fetch_underlying_price(symbol, call_raw, put_raw):
    """Fetch an underlying price only through the configured permitted provider."""
    if _provider_is_thetadata():
        return _thetadata_underlying_price(symbol)
    if _provider_is_tradier():
        return _tradier_underlying_price(symbol)
    if _provider_is_marketdata():
        return _marketdata_underlying_price(symbol, call_raw, put_raw)
    raise RuntimeError(f"Unsupported or removed data provider: {DATA_PROVIDER}")


def _gamma_t_years(expiration, now):
    """Return expiry time only for contracts inside the 10-30 DTE window."""
    try:
        expiry = bot.datetime.strptime(str(expiration), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None, None
    dte = (expiry - now.date()).days
    if dte < SCORE_MIN_DTE or dte > SCORE_MAX_DTE:
        return dte, None
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
    stats["gamma_min_dte"] = SCORE_MIN_DTE
    stats["gamma_max_dte"] = SCORE_MAX_DTE
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
    if not GAMMA_DIRECTION_GATE_ENABLED:
        return "disabled"
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
    """Return the nearest eligible expiration within the configured DTE window."""
    now = bot.now_new_york()
    valid = []

    for contract in contracts:
        try:
            expiration = contract["expiration"]
            if _contract_in_dte_window(expiration, now):
                valid.append(bot.datetime.strptime(
                    expiration, "%Y-%m-%d"
                ).date())
        except (KeyError, TypeError, ValueError):
            continue

    return min(valid) if valid else None


def create_trade(
    symbol, direction, stock_price, contract=None, gamma_info=None,
    trade_confidence=None, gamma_direction=None, directional_edge=None,
    scan_mode=None, required_score=None, required_edge=None,
    allow_strong_score_exception=False,
):
    """Lock and open the exact contract that qualified during the scan."""
    symbol = str(symbol or "").upper().strip()
    direction = str(direction or "").upper().strip()
    if symbol in bot.EXCLUDED_SYMBOLS:
        raise RuntimeError(f"{symbol} is excluded from contract selection")
    if bot.safe_float(stock_price) <= 0:
        raise RuntimeError("Underlying price unavailable; refusing to lock a contract")
    if contract is None:
        # Manual contract selection: use the canonical, fully-paginated path
        # and calculate Gamma from that same full contract universe.
        call_raw = massive_chain(symbol, "CALL")
        put_raw = massive_chain(symbol, "PUT")
        if gamma_info is None:
            gamma_info = (
                calculate_gamma_flip(call_raw, put_raw, stock_price)
                if GAMMA_DIRECTION_GATE_ENABLED
                else {"status": "DISABLED", "regime": "DISABLED"}
            )
        calls, _ = _prepare_side(call_raw, stock_price)
        puts, _ = _prepare_side(put_raw, stock_price)
        with bot.TRADE_STATE_LOCK:
            used_tickers = _existing_contract_tickers(
                bot._load_trade_store(), symbol
            )
        calls = _exclude_recorded_contracts(calls, used_tickers)
        puts = _exclude_recorded_contracts(puts, used_tickers)
        call_stats, put_stats = _side_flow_stats(calls), _side_flow_stats(puts)
        requested = calls if direction == "CALL" else puts
        contract = requested[0] if requested else None
        call_strength = bot._direction_strength(call_stats, put_stats)
        put_strength = bot._direction_strength(put_stats, call_stats)
        directional_edge = (
            call_strength - put_strength if direction == "CALL"
            else put_strength - call_strength
        )
        assessment = _conservative_assessment(
            direction, contract, directional_edge, gamma_info, stock_price,
            minimum_score=required_score, minimum_edge=required_edge,
            allow_strong_score_exception=allow_strong_score_exception,
        )
        if not assessment["can_open"]:
            raise RuntimeError(assessment["wait_status"])
        trade_confidence = assessment["trade_confidence"]
        gamma_direction = assessment["gamma_direction_state"]
    if not isinstance(contract, dict) or not contract.get("ticker"):
        raise RuntimeError("Qualified contract is missing")
    trade_key = _trade_storage_key(symbol, contract["ticker"])

    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        trades = store.setdefault("trades", {})
        _, existing = _find_trade_entry(store, symbol, contract["ticker"])
        if isinstance(existing, dict):
            result = dict(existing)
            result["_ignored_duplicate"] = True
            return result
        created_at = bot.now_new_york().isoformat(timespec="seconds")
        reservation = {"symbol": symbol, "trade_record_key": trade_key,
                       "direction": direction, "stock_price": stock_price,
                       "contract_ticker": contract["ticker"], "entry_price": 0.0,
                       "status": "opening", "message_id": None, "created_at": created_at,
                        "monitor_version": 0, "opening_phase": "reserved"}
        trades[trade_key] = reservation
        bot._save_trade_store(store)
    try:
        contract = refresh_contract_quote_for_entry(
            symbol, contract, stock_price
        )
        entry, source = bot.choose_initial_price(contract)
        assessment = _conservative_assessment(
            direction, contract, directional_edge, gamma_info, stock_price,
            minimum_score=required_score, minimum_edge=required_edge,
            allow_strong_score_exception=allow_strong_score_exception,
        )
        if not assessment["can_open"]:
            raise RuntimeError(assessment["wait_status"])
        if entry <= 0 or source != "Fresh Bid/Ask Midpoint":
            raise RuntimeError("WAIT - FRESH OPTION MIDPOINT REQUIRED")
        trade_confidence = assessment["trade_confidence"]
        gamma_direction = assessment["gamma_direction_state"]
        quote_timestamp = contract.get("quote_timestamp")
        quote_timestamp_iso = (
            bot.datetime.fromtimestamp(
                quote_timestamp, bot.NEW_YORK_TZ
            ).isoformat(timespec="seconds")
            if quote_timestamp is not None
            else bot.now_new_york().isoformat(timespec="seconds")
        )
        state = {
            "symbol": symbol, "direction": direction, "stock_price": stock_price,
            "trade_record_key": trade_key,
            "contract_ticker": contract["ticker"], "expiration": contract["expiration"],
            "strike": contract["strike"], "score": contract["score"], "entry_price": round(entry, 3),
            "entry_source": source, "stop_loss": bot.stop_loss(entry), "targets": bot.targets(entry),
            "current_price": round(entry, 3), "highest_price": round(entry, 3), "achieved": 0,
            "lowest_price": round(entry, 3),
            "status": "active", "message_id": None, "created_at": created_at, "monitor_version": 1,
            "telegram_send_state": "not_started",
            "entry_quote_timestamp": quote_timestamp_iso,
            "entry_quote_age_seconds": contract.get("quote_age_seconds"),
            "dte": contract.get("dte"),
            "trade_confidence": (trade_confidence or {}).get("level", "N/A"),
            "trade_confidence_score": (trade_confidence or {}).get("score"),
            "trade_confidence_components": (trade_confidence or {}).get("components", {}),
            "delta": contract.get("delta"),
            "volume": contract.get("volume"),
            "open_interest": contract.get("open_interest"),
            "bid": contract.get("bid"),
            "ask": contract.get("ask"),
            "midpoint": contract.get("midpoint"),
            "last": contract.get("last"),
            "day_close": contract.get("day_close"),
            "spread_percent": contract.get("spread_percent"),
            "iv": contract.get("iv"),
            "theta": contract.get("theta"),
            "theta_delta_ratio": contract.get("theta_delta_ratio"),
            "directional_edge": directional_edge,
            "scan_mode": scan_mode,
            "required_score": required_score,
            "required_edge": required_edge,
            "entry_signal_type": _autonomous_entry_signal_mode(),
            "entry_signal_bar_time_ms": None,
            "gamma_policy": _gamma_entry_policy(),
            "gamma_direction_state": gamma_direction or gamma_direction_state(
                direction, stock_price, gamma_info
            ),
            "paper_trade_id": uuid.uuid4().hex,
            **_gamma_state_fields(gamma_info, stock_price),
        }
        with bot.TRADE_STATE_LOCK:
            store = bot._load_trade_store()
            current = (store.get("trades") or {}).get(trade_key)
            if not isinstance(current, dict) or current.get("status") != "opening" or current.get("contract_ticker") != contract["ticker"]:
                raise RuntimeError("opening trade changed before activation")
            # This durable checkpoint makes a subsequent send failure
            # unambiguous: it may have reached Telegram, so never resend it.
            state["telegram_send_state"] = "sending"
            store["trades"][trade_key] = state
            bot._save_trade_store(store)
        # Rendering and Telegram are intentionally outside TRADE_STATE_LOCK.
        message_id = bot.telegram_send_card(dict(state))
        with bot.TRADE_STATE_LOCK:
            store = bot._load_trade_store()
            current = (store.get("trades") or {}).get(trade_key)
            if not isinstance(current, dict) or current.get("contract_ticker") != contract["ticker"] or current.get("message_id"):
                raise RuntimeError("active trade changed before Telegram synchronization")
            current["message_id"] = message_id
            current["telegram_send_state"] = "sent"
            current["monitor_version"] = int(current.get("monitor_version", 0)) + 1
            store["trades"][trade_key] = current
            store.setdefault("paper_trades", []).append(_paper_trade_record(current))
            (store.get("pending_candidates") or {}).pop(symbol, None)
            bot._save_trade_store(store)
            state = dict(current)
        print(f"🔒 HARD CONTRACT LOCKED: {symbol} {direction} | {contract['ticker']} | option entry {entry:.2f}")
        return state
    except Exception:
        with bot.TRADE_STATE_LOCK:
            latest = bot._load_trade_store()
            current = (latest.get("trades") or {}).get(trade_key)
            if isinstance(current, dict) and current.get("contract_ticker") == contract["ticker"] and not current.get("message_id"):
                if current.get("opening_phase") == "reserved":
                    latest["trades"].pop(trade_key, None)
                else:
                    current.update(status="recovery_required",
                                   recovery_note="Telegram send may have succeeded; manual recovery required")
                    latest["trades"][trade_key] = current
                bot._save_trade_store(latest)
        raise


def _apply_monitored_price(
    trade_key, symbol, identity, price, source, age, quote_age, trade_age
):
    """Atomically apply an already-fetched exact-contract update."""
    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        state = (store.get("trades") or {}).get(trade_key)
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
        store["trades"][trade_key] = state
        _update_paper_trade(store, state, previous_achieved)
        bot._save_trade_store(store)
        return dict(state)


def _record_telegram_sync(trade_key, identity, success, error=None):
    """Persist edit outcome only if this remains the exact original message."""
    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        state = (store.get("trades") or {}).get(trade_key)
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
        store["trades"][trade_key] = state
        bot._save_trade_store(store)


def monitor_active_trades_once():
    """Poll exact locked tickers without holding the state lock during I/O."""
    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        work = [(trade_key, symbol, (
                    trade.get("contract_ticker"),
                    trade.get("message_id"),
                    int(trade.get("monitor_version", 0)),
                ))
                for trade_key, symbol, trade in _iter_trade_entries(store)
                if isinstance(trade, dict)
                and trade.get("status") == "active"
                and symbol not in bot.EXCLUDED_SYMBOLS]
    if not work:
        return {"accepted": True, "ignored": "no active trades"}
    results = []
    for trade_key, symbol, identity in work:
        contract, message_id, _ = identity
        if not contract:
            results.append({"accepted": False, "symbol": symbol, "error": "active trade missing contract"})
            continue
        try:
            snapshot = bot.massive_contract_snapshot(symbol, contract)
            price, source, age, quote_age, trade_age = classify_snapshot_price(snapshot)
            if price > 0:
                state = _apply_monitored_price(
                    trade_key, symbol, identity, price, source, age,
                    quote_age, trade_age,
                )
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
                    state = (latest.get("trades") or {}).get(trade_key)
                    if isinstance(state, dict) and (
                        state.get("contract_ticker"), state.get("message_id"),
                        int(state.get("monitor_version", 0)),
                    ) == identity:
                        state.update(last_massive_price_source=source,
                                     last_data_age_seconds=round(age, 3) if age is not None else None,
                                     last_quote_age_seconds=round(quote_age, 3) if quote_age is not None else None,
                                     last_trade_age_seconds=round(trade_age, 3) if trade_age is not None else None)
                        latest["trades"][trade_key] = state
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
                    _record_telegram_sync(trade_key, identity, success, error)
                continue
            # This monitor runs hourly. Keep the existing Telegram card's price
            # fields current once per scheduled pass, rather than on every tick.
            success, error = telegram_edit_card_retry(message_id, state)
            _record_telegram_sync(trade_key, sync_identity, success, error)
            item = {"accepted": True, "symbol": symbol, "contract": contract, "price": price,
                    "source": source, "source_age_seconds": round(age, 3),
                    "quote_age_seconds": round(quote_age, 3) if quote_age is not None else None,
                    "trade_age_seconds": round(trade_age, 3) if trade_age is not None else None,
                    "telegram_synchronized": success, "telegram_updated": True,
                    "status": state.get("status")}
            results.append(item)
            print(f"💹 {symbol} {contract} price={price:.2f} source={source} age={age:.1f}s telegram={success}")
        except Exception as error:
            results.append({"accepted": False, "symbol": symbol, "contract": contract, "error": str(error)})
    return {"accepted": True, "active_checked": len(work), "results": results}


def _seconds_until_next_hour(now=None, minimum_wait_seconds=0):
    """Return delay to a wall-clock hour boundary, optionally after a minimum wait."""
    now = now or bot.now_new_york()
    earliest = now + timedelta(seconds=max(0.0, float(minimum_wait_seconds)))
    boundary = earliest.replace(minute=0, second=0, microsecond=0)
    if boundary < earliest or (minimum_wait_seconds <= 0 and boundary <= now):
        boundary += timedelta(hours=1)
    return max(0.0, (boundary - now).total_seconds())


def auto_monitor_loop():
    if not AUTO_MONITOR_ENABLED:
        print("Auto contract monitor disabled")
        return
    interval = max(float(getattr(bot, "AUTO_MONITOR_SECONDS", 3600)), 0.1)
    hourly = interval >= 3600
    if hourly:
        first_delay = _seconds_until_next_hour()
        print(
            "Auto contract monitor: hourly on the hour; "
            f"first update in {first_delay:.0f}s"
        )
        time.sleep(first_delay)
    else:
        print(f"Auto contract monitor: every {interval:g}s")
    while True:
        try:
            if not hourly or bot.market_is_open_now():
                monitor_active_trades_once()
            else:
                print("Auto contract monitor skipped: market closed")
        except Exception as error:
            print("Auto monitor warning:", error)
        delay = _seconds_until_next_hour() if hourly else interval
        time.sleep(max(1.0, delay))


def _scan_mode_context(now=None):
    """Return the opening-session requirements for the current New York time."""
    now = now or bot.now_new_york()
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= minutes < 10 * 60:
        return {
            "scan_mode": "OPENING_FAST",
            "required_score": 72.0,
            "required_edge": 8.0,
            "strong_score": 70.0,
            "strong_edge": 12.0,
            "allow_strong_score_exception": True,
        }
    return {
        "scan_mode": "NORMAL",
        "required_score": float(bot.AUTO_SCAN_MIN_SCORE),
        "required_edge": float(bot.AUTO_SCAN_MIN_EDGE),
        "strong_score": None,
        "strong_edge": None,
        "allow_strong_score_exception": False,
    }


def _side_diagnostic(
    direction, side_stats, strength, other_strength, gamma_info, stock_price,
    scan_context=None,
):
    scan_context = scan_context or _scan_mode_context()
    best = side_stats.get("best") or {}
    score = bot.safe_float(best.get("score"))
    volume = bot.safe_float(side_stats.get("volume"))
    has_eligible_contract = bool(best) and bool(best.get("selection_eligible"))
    edge = strength - other_strength
    required_score = scan_context["required_score"]
    required_edge = scan_context["required_edge"]
    strong_exception = (
        scan_context["allow_strong_score_exception"]
        and score >= scan_context["strong_score"]
        and edge >= scan_context["strong_edge"]
    )
    score_ok = has_eligible_contract and (
        score >= required_score or strong_exception
    )
    edge_ok = edge >= required_edge
    volume_ok = volume >= bot.AUTO_SCAN_MIN_VOLUME
    gamma_state = gamma_direction_state(direction, stock_price, gamma_info)
    gamma_ok = gamma_state != "opposed"

    reasons = []
    if not has_eligible_contract:
        reasons.append("no eligible contract after quality safeguards")
    if not score_ok:
        reasons.append(f"score {score:.1f} < {required_score:.1f}")
    if not edge_ok:
        reasons.append(f"edge {edge:.1f} < {required_edge:.1f}")
    if not volume_ok:
        reasons.append(f"volume {volume:.0f} < {bot.AUTO_SCAN_MIN_VOLUME:.0f}")
    if not gamma_ok:
        reasons.append("Gamma opposed")
    if not side_stats.get("raw_count"):
        reasons.insert(0, f"{direction} chain returned no contracts")
    elif side_stats.get("skipped_identity"):
        reasons.append(
            f"{side_stats['skipped_identity']} contracts missing ticker/expiration"
        )

    accepted = score_ok and edge_ok and volume_ok and gamma_ok
    return {
        "direction": direction,
        "accepted": accepted,
        "contract": best.get("ticker"),
        "score": score,
        "strength": strength,
        "edge": round(edge, 2),
        "scan_mode": scan_context["scan_mode"],
        "required_score": required_score,
        "required_edge": required_edge,
        "strong_score_exception": strong_exception,
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
        "raw_count": side_stats.get("raw_count", 0),
        "skipped_identity": side_stats.get("skipped_identity", 0),
        "candidate_scores": side_stats.get("candidate_scores") or [],
        "checks": {
            "eligible_contract": has_eligible_contract,
            "score": score_ok,
            "directional_edge": edge_ok,
            "volume": volume_ok,
            "gamma_flip": gamma_ok if GAMMA_DIRECTION_GATE_ENABLED else None,
        },
        "reason": "qualified" if accepted else "; ".join(reasons),
    }


def _marketdata_blocked_symbol_result(symbol, reason):
    side_reason = "not evaluated: MarketData account credit limit is latched"
    return {
        "symbol": symbol,
        "accepted": False,
        "status": "DATA PROVIDER BLOCKED",
        "direction": None,
        "contract": None,
        "error": str(reason),
        "call": {"accepted": False, "reason": side_reason},
        "put": {"accepted": False, "reason": side_reason},
        "ignored": side_reason,
    }


def scan_symbol_options(symbol):
    """Evaluate both sides without a Gamma Flip veto or MACD entry gate."""
    symbol = str(symbol or "").upper().strip()
    if symbol in bot.EXCLUDED_SYMBOLS:
        return {
            "symbol": symbol,
            "accepted": True,
            "status": "EXCLUDED",
            "ignored": "symbol excluded from contract selection",
        }
    blocked_reason = (
        _marketdata_account_credit_limit_reason()
        if _provider_is_marketdata()
        else None
    )
    if blocked_reason:
        return _marketdata_blocked_symbol_result(symbol, blocked_reason)
    scan_context = _scan_mode_context()

    with bot.TRADE_STATE_LOCK:
        store = bot._load_trade_store()
        expired_any = False
        for trade_key, _, existing in list(_iter_trade_entries(store, symbol)):
            expired_any = (
                _expire_trade_if_needed(store, trade_key, existing)
                or expired_any
            )
        if expired_any:
            bot._save_trade_store(store)
        recorded_contract_tickers = _existing_contract_tickers(store, symbol)
        pending_candidate = (store.get("pending_candidates") or {}).get(symbol)
    if (
        isinstance(pending_candidate, dict)
        and bot.canonical_option_ticker(
            pending_candidate.get("contract_ticker")
        ) in recorded_contract_tickers
    ):
        _clear_pending_candidate(symbol)
        pending_candidate = None

    call_raw = massive_chain(symbol, "CALL")
    put_raw = massive_chain(symbol, "PUT")
    stock_price, stock_source = fetch_underlying_price(symbol, call_raw, put_raw)
    if stock_price <= 0:
        return {
            "symbol": symbol,
            "accepted": False,
            "scan_mode": scan_context["scan_mode"],
            "required_score": scan_context["required_score"],
            "required_edge": scan_context["required_edge"],
            "call": {"accepted": False, "reason": "underlying price unavailable"},
            "put": {"accepted": False, "reason": "underlying price unavailable"},
            "ignored": "underlying price unavailable",
        }

    gamma_info = (
        bot.calculate_gamma_flip(call_raw, put_raw, stock_price)
        if GAMMA_DIRECTION_GATE_ENABLED
        else {"status": "DISABLED", "regime": "DISABLED"}
    )

    calls, call_exp = bot._prepare_side(call_raw, stock_price)
    puts, put_exp = bot._prepare_side(put_raw, stock_price)
    calls = _exclude_recorded_contracts(calls, recorded_contract_tickers)
    puts = _exclude_recorded_contracts(puts, recorded_contract_tickers)
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
        "CALL", call_stats, call_strength, put_strength, gamma_info, stock_price,
        scan_context,
    )
    put_diag = _side_diagnostic(
        "PUT", put_stats, put_strength, call_strength, gamma_info, stock_price,
        scan_context,
    )
    assessments = {}
    if ORCA_CONSERVATIVE_MODE:
        assessment_requirements = {}
        if scan_context["scan_mode"] == "OPENING_FAST":
            assessment_requirements = {
                "minimum_score": scan_context["required_score"],
                "minimum_edge": scan_context["required_edge"],
                "allow_strong_score_exception": scan_context["allow_strong_score_exception"],
            }
        assessments["CALL"] = _conservative_assessment(
            "CALL", call_stats.get("best"), call_strength - put_strength,
            gamma_info, stock_price, **assessment_requirements,
        )
        assessments["PUT"] = _conservative_assessment(
            "PUT", put_stats.get("best"), put_strength - call_strength,
            gamma_info, stock_price, **assessment_requirements,
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
            assessment.update(
                wait_reasons=reasons, wait_status=reasons[0] if reasons else "TRADE",
                can_open=not reasons,
            )
            diagnostic.update(
                accepted=assessment["can_open"],
                trade_confidence=assessment["trade_confidence"],
                wait_status=assessment["wait_status"], wait_reasons=reasons,
                entry_price_quality=assessment["entry_source"],
                gamma_direction_state=assessment["gamma_direction_state"],
                reason="qualified" if not reasons else "; ".join(reasons),
            )

    result = {
        "symbol": symbol,
        "accepted": True,
        "entry_signal_mode": _autonomous_entry_signal_mode(),
        "gamma_policy": _gamma_entry_policy(),
        "scan_mode": scan_context["scan_mode"],
        "required_score": scan_context["required_score"],
        "required_edge": scan_context["required_edge"],
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
        available = [
            (direction, stats["best"], strength)
            for direction, stats, strength in (
                ("CALL", call_stats, call_strength), ("PUT", put_stats, put_strength)
            ) if stats.get("best")
        ]
        if available:
            preferred_direction, preferred_contract, _ = max(available, key=lambda item: item[2])
        else:
            preferred_direction = "CALL" if call_strength >= put_strength else "PUT"
            preferred_contract = None
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
            "call": _scan_decision_summary(call_diag),
            "put": _scan_decision_summary(put_diag),
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
        directional_edge=call_diag["edge"] if direction == "CALL" else put_diag["edge"],
        scan_mode=scan_context["scan_mode"],
        required_score=scan_context["required_score"],
        required_edge=scan_context["required_edge"],
        allow_strong_score_exception=scan_context["allow_strong_score_exception"],
    )
    if state.get("_ignored_duplicate"):
        result["status"] = "DUPLICATE CONTRACT"
        result["ignored"] = "exact option contract already recorded"
        result["contract"] = state.get("contract_ticker")
        return result
    result["direction"] = direction
    result["contract"] = state.get("contract_ticker")
    result["message_id"] = state.get("message_id")
    result["status"] = "TRADE"
    result["trade_confidence"] = state.get("trade_confidence")
    _LAST_SCAN_DECISIONS[symbol] = {
        "status": "TRADE",
        "contract": state.get("contract_ticker"),
        "confidence": state.get("trade_confidence"),
        "direction": direction,
        "scan_mode": scan_context["scan_mode"],
        "updated_at": bot.now_new_york().isoformat(timespec="seconds"),
    }
    return result


def _scan_decision_summary(diagnostic):
    """Keep health/log output complete without dumping every candidate row."""
    return {
        key: diagnostic.get(key)
        for key in (
            "accepted", "contract", "score", "edge", "volume",
            "required_score", "required_edge", "scan_mode",
            "raw_count", "candidate_count", "eligible_candidate_count",
            "reason", "wait_status", "wait_reasons",
        )
        if key in diagnostic
    }


def _scan_log_summary(item):
    """Print a bounded scan summary while retaining full API diagnostics."""
    result = {
        "symbol": item.get("symbol"),
        "accepted": item.get("accepted"),
        "status": item.get("status"),
        "direction": item.get("direction"),
        "contract": item.get("contract"),
        "error": item.get("error"),
    }
    for side in ("call", "put"):
        diagnostic = item.get(side) or {}
        result[side] = _scan_decision_summary(diagnostic)
    return result


def scan_watchlist_once(force=False, opening_window_only=False):
    """Run at most one scanner pass process-wide."""
    if not SCAN_IN_PROGRESS_LOCK.acquire(blocking=False):
        return {"accepted": True, "ignored": "scan already in progress"}

    try:
        # A forced /scan-now is an explicit manual multi-symbol test. It may
        # run while the background scanner remains disabled, but all normal
        # freshness, score, gamma, and budget gates still apply.
        if not bot.AUTO_SCAN_ENABLED and not force:
            return {"accepted": True, "ignored": "auto scanner disabled"}
        if not force and not bot.market_is_open_now():
            return {"accepted": True, "ignored": "US regular market is closed"}
        if opening_window_only and not _is_within_auto_scan_window():
            return {
                "accepted": True,
                "ignored": "outside opening scan window",
                "results": [],
            }

        if _provider_is_marketdata() and not _provider_token():
            return {
                "accepted": False,
                "checked": 0,
                "results": [],
                "skipped_symbols": list(bot.WATCHLIST),
                "incomplete": True,
                "provider_blocked": True,
                "provider_block_kind": "missing_key",
                "error": "MARKETDATA_API_KEY is missing; Massive is permanently disabled",
            }

        blocked_reason = (
            _marketdata_account_credit_limit_reason()
            if _provider_is_marketdata()
            else None
        )
        if blocked_reason:
            skipped_symbols = list(bot.WATCHLIST)
            print(
                "ORCA scan blocked by MarketData account credit limit; "
                f"symbols_not_scanned={len(skipped_symbols)}"
            )
            return {
                "accepted": True,
                "checked": 0,
                "results": [],
                "skipped_symbols": skipped_symbols,
                "incomplete": True,
                "provider_blocked": True,
                "error": blocked_reason,
            }

        results = []
        symbols = list(bot.WATCHLIST)
        skipped_symbols = []
        for index, symbol in enumerate(symbols):
            # A successful response can itself spend the last credits. Stop
            # here rather than logging an error for every remaining company.
            exhausted = (
                _marketdata_account_credit_limit_reason()
                if _provider_is_marketdata() else None
            )
            if exhausted:
                return {
                    "accepted": True,
                    "checked": len(results),
                    "results": results,
                    "skipped_symbols": symbols[index:],
                    "incomplete": True,
                    "provider_blocked": True,
                    "error": exhausted,
                }
            if opening_window_only and not _is_within_auto_scan_window():
                print("ORCA opening scan window ended; remaining symbols skipped")
                skipped_symbols.extend(symbols[index:])
                break
            try:
                item = scan_symbol_options(symbol)
            except MarketDataAccountCreditLimitError as error:
                item = _marketdata_blocked_symbol_result(symbol, error)
                results.append(item)
                print("🐋 ORCA SCAN:", _scan_log_summary(item))
                skipped_symbols.extend(symbols[index + 1:])
                print(
                    "ORCA scan halted after MarketData account credit limit; "
                    f"symbols_not_scanned={len(skipped_symbols)}"
                )
                return {
                    "accepted": True,
                    "checked": len(results),
                    "results": results,
                    "skipped_symbols": skipped_symbols,
                    "incomplete": True,
                    "provider_blocked": True,
                    "error": str(error),
                }
            except Exception as error:
                item = {
                    "symbol": symbol,
                    "accepted": False,
                    "call": {"accepted": False, "reason": f"scan error: {error}"},
                    "put": {"accepted": False, "reason": f"scan error: {error}"},
                    "error": str(error),
                }
            results.append(item)
            print("🐋 ORCA SCAN:", _scan_log_summary(item))
            bot.time.sleep(0.35)

        return {
            "accepted": True,
            "checked": len(results),
            "results": results,
            "skipped_symbols": skipped_symbols,
            "incomplete": bool(skipped_symbols),
            "provider_blocked": False,
        }
    finally:
        SCAN_IN_PROGRESS_LOCK.release()


def _ichimoku_status_update(**changes):
    """Publish safe telemetry for the bot-native Ichimoku signal worker."""
    with _ICHIMOKU_STATUS_LOCK:
        _ICHIMOKU_STATUS.update(changes)
        return dict(_ICHIMOKU_STATUS)


def bot_ichimoku_signal_loop():
    """Poll age-gated Massive bars and scan a symbol when a crossing appears."""
    symbols = [
        symbol for symbol in bot.WATCHLIST
        if symbol not in bot.EXCLUDED_SYMBOLS
    ]
    if not symbols:
        _ichimoku_status_update(last_poll_status="no eligible symbols")
        return

    slot_seconds = max(
        5.0, ICHIMOKU_30M_POLL_SECONDS / max(1, len(symbols))
    )
    print(
        "ORCA bot-native Ichimoku worker: "
        f"polling {len(symbols)} symbols on a {ICHIMOKU_30M_POLL_SECONDS:g}s "
        f"cycle; rejecting 30-minute bars older than "
        f"{ICHIMOKU_30M_BAR_MAX_AGE_SECONDS}s"
    )
    time.sleep(8)

    while True:
        now = bot.now_new_york()
        if not bot.AUTO_SCAN_ENABLED:
            _ichimoku_status_update(
                last_poll_finished=now.isoformat(timespec="seconds"),
                last_poll_status="auto scanner disabled",
                last_poll_checked=0,
                last_poll_signals=0,
                last_poll_errors=0,
                last_poll_error=None,
            )
            time.sleep(60)
            continue
        if not bot.market_is_open_now(now) or not _is_within_auto_scan_window(now):
            _ichimoku_status_update(
                last_poll_finished=now.isoformat(timespec="seconds"),
                last_poll_status="market closed or outside scan window",
                last_poll_checked=0,
                last_poll_signals=0,
                last_poll_errors=0,
                last_poll_error=None,
            )
            time.sleep(60)
            continue

        _ichimoku_status_update(
            last_poll_started=now.isoformat(timespec="seconds"),
            last_poll_status="running",
            last_poll_error=None,
        )
        checked = signals = errors = 0
        first_error = None
        next_slot = time.monotonic()

        for symbol in symbols:
            now = bot.now_new_york()
            if (
                not bot.market_is_open_now(now)
                or not _is_within_auto_scan_window(now)
            ):
                break
            checked += 1

            try:
                with bot.TRADE_STATE_LOCK:
                    store = bot._load_trade_store()
                    existing_trade = (store.get("trades") or {}).get(symbol)
                    has_active_trade = (
                        isinstance(existing_trade, dict)
                        and existing_trade.get("status") in (
                            "opening", "active", "recovery_required"
                        )
                    )

                if not has_active_trade:
                    signal = _active_ichimoku_30m_signal(symbol, now=now)
                    if signal is None:
                        signal, ignored = _poll_and_register_bot_ichimoku_signal(
                            symbol, now=now
                        )
                        if ignored and ignored != "no crossing on the latest completed bar":
                            print(f"{symbol} bot Ichimoku check: {ignored}")

                    if signal:
                        signals += 1
                        if SCAN_IN_PROGRESS_LOCK.acquire(blocking=False):
                            try:
                                result = scan_symbol_options(symbol)
                                if result.get("error"):
                                    raise RuntimeError(result["error"])
                            finally:
                                SCAN_IN_PROGRESS_LOCK.release()
                        else:
                            print(
                                f"{symbol} bot Ichimoku signal retained; "
                                "another scanner pass is in progress"
                            )
            except Exception as error:
                errors += 1
                if first_error is None:
                    first_error = str(error)[:300]
                print(f"{symbol} bot Ichimoku poll warning: {error}")

            next_slot += slot_seconds
            wait_seconds = next_slot - time.monotonic()
            if wait_seconds > 0:
                time.sleep(wait_seconds)

        finished = bot.now_new_york().isoformat(timespec="seconds")
        _ichimoku_status_update(
            last_poll_finished=finished,
            last_poll_status=(
                "completed" if errors == 0 else "completed with errors"
            ),
            last_poll_checked=checked,
            last_poll_signals=signals,
            last_poll_errors=errors,
            last_poll_error=first_error,
        )


def _scanner_status_update(**changes):
    """Persist safe scanner telemetry for health checks without exposing secrets."""
    with _SCANNER_STATUS_LOCK:
        _SCANNER_STATUS.update(changes)
        return dict(_SCANNER_STATUS)


def _scanner_result_counts(result):
    """Summarize one completed pass for operators and health checks."""
    result = result or {}
    items = result.get("results") or []
    trades = 0
    errors = 0
    ignored = 0
    for item in items:
        if not isinstance(item, dict):
            errors += 1
            continue
        if item.get("status") == "TRADE" and item.get("message_id"):
            trades += 1
        if item.get("error"):
            errors += 1
        if item.get("ignored") or item.get("status") not in (None, "TRADE"):
            ignored += 1
    if result.get("provider_blocked") and errors == 0:
        errors = 1
    return {
        "checked": len(items),
        "trades": trades,
        "errors": errors,
        "ignored": ignored,
        "skipped": len(result.get("skipped_symbols") or []),
    }


def _is_within_auto_scan_window(now=None):
    """Return whether now is within the configured opening-session scan window."""
    if AUTO_SCAN_OPENING_WINDOW_MINUTES <= 0:
        return False
    now = now or bot.now_new_york()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    market_open_minute = 9 * 60 + 30
    return (
        market_open_minute
        <= minutes
        < market_open_minute + AUTO_SCAN_OPENING_WINDOW_MINUTES
    )


def _seconds_until_next_auto_scan(
    now=None,
    regular_interval_seconds=None,
    opening_interval_seconds=None,
):
    """Align scans to market open: every 3m for 15m, then the regular cadence."""
    now = now or bot.now_new_york()
    if now.tzinfo is None:
        now = now.replace(tzinfo=bot.NEW_YORK_TZ)
    else:
        now = now.astimezone(bot.NEW_YORK_TZ)

    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed_seconds = (now - market_open).total_seconds()
    if elapsed_seconds < 0:
        return max(1.0, -elapsed_seconds)

    fast_window_seconds = 15 * 60
    if elapsed_seconds < fast_window_seconds:
        opening_interval_seconds = (
            opening_interval_seconds
            if opening_interval_seconds is not None
            else AUTO_SCAN_OPENING_INTERVAL_SECONDS
        )
        cadence_seconds = max(60.0, float(opening_interval_seconds))
    else:
        regular_interval_seconds = (
            regular_interval_seconds
            if regular_interval_seconds is not None
            else getattr(bot, "AUTO_SCAN_SECONDS", 900)
        )
        cadence_seconds = max(60.0, float(regular_interval_seconds))

    next_slot = (int(elapsed_seconds // cadence_seconds) + 1) * cadence_seconds
    return max(1.0, next_slot - elapsed_seconds)


def _seconds_until_next_market_open(now=None):
    """Wait until the next valid 09:30 New York opening, skipping holidays."""
    now = now or bot.now_new_york()
    if now.tzinfo is None:
        now = now.replace(tzinfo=bot.NEW_YORK_TZ)
    else:
        now = now.astimezone(bot.NEW_YORK_TZ)

    for day_offset in range(15):
        trading_day = now.date() + timedelta(days=day_offset)
        opening = now.replace(
            year=trading_day.year,
            month=trading_day.month,
            day=trading_day.day,
            hour=9,
            minute=30,
            second=0,
            microsecond=0,
        )
        if opening > now and bot.market_is_open_now(opening):
            return max(1.0, (opening - now).total_seconds())
    return 60.0


def auto_scan_loop():
    """Run the canonical scanner forever, even when a pass raises unexpectedly."""
    interval = max(float(getattr(bot, "AUTO_SCAN_SECONDS", 3600)), 60.0)
    opening_interval = AUTO_SCAN_OPENING_INTERVAL_SECONDS
    print(
        f"ORCA canonical scanner: every {opening_interval:g}s for the first "
        f"15 minutes after 09:30 New York, then every {interval:g}s; "
        "scanning only during the "
        f"first {AUTO_SCAN_OPENING_WINDOW_MINUTES} minutes of the session"
    )
    time.sleep(8)
    while True:
        now = bot.now_new_york()
        started = now.isoformat(timespec="seconds")
        _scanner_status_update(
            last_cycle_started=started,
            last_cycle_status="running",
            last_cycle_error=None,
        )
        delay = interval
        try:
            if not bot.market_is_open_now(now):
                _scanner_status_update(
                    last_cycle_finished=bot.now_new_york().isoformat(timespec="seconds"),
                    last_cycle_status="market closed",
                    last_cycle_checked=0,
                    last_cycle_trades=0,
                    last_cycle_errors=0,
                    last_cycle_ignored=0,
                    last_cycle_skipped=0,
                    last_cycle_error=None,
                )
                delay = _seconds_until_next_market_open(now)
            elif not _is_within_auto_scan_window(now):
                _scanner_status_update(
                    last_cycle_finished=bot.now_new_york().isoformat(timespec="seconds"),
                    last_cycle_status="outside opening scan window",
                    last_cycle_checked=0,
                    last_cycle_trades=0,
                    last_cycle_errors=0,
                    last_cycle_ignored=0,
                    last_cycle_skipped=0,
                    last_cycle_error=None,
                )
                delay = _seconds_until_next_market_open(now)
            else:
                result = scan_watchlist_once(False, opening_window_only=True)
                counts = _scanner_result_counts(result)
                cycle_status = (
                    "blocked: MARKETDATA_API_KEY is missing"
                    if result.get("provider_block_kind") == "missing_key"
                    else "blocked by MarketData account credit limit"
                    if result.get("provider_blocked")
                    else "incomplete"
                    if result.get("incomplete")
                    else "completed with errors"
                    if counts["errors"]
                    else "completed"
                )
                _scanner_status_update(
                    last_cycle_finished=bot.now_new_york().isoformat(timespec="seconds"),
                    last_cycle_status=cycle_status,
                    last_cycle_checked=counts["checked"],
                    last_cycle_trades=counts["trades"],
                    last_cycle_errors=counts["errors"],
                    last_cycle_ignored=counts["ignored"],
                    last_cycle_skipped=counts["skipped"],
                    last_cycle_error=(
                        str(result.get("error"))[:500]
                        if result.get("error")
                        else None
                    ),
                )
                print(
                    "ORCA scanner cycle:",
                    f"checked={counts['checked']}",
                    f"trades={counts['trades']}",
                    f"errors={counts['errors']}",
                    f"ignored={counts['ignored']}",
                    f"skipped={counts['skipped']}",
                )
                delay = _seconds_until_next_auto_scan(
                    bot.now_new_york(),
                    regular_interval_seconds=interval,
                    opening_interval_seconds=opening_interval,
                )
        except Exception as error:
            _scanner_status_update(
                last_cycle_finished=bot.now_new_york().isoformat(timespec="seconds"),
                last_cycle_status="failed",
                last_cycle_error=str(error)[:500],
            )
            print("ORCA canonical scanner warning:", error)
            recovery_time = bot.now_new_york()
            if bot.market_is_open_now(recovery_time) and _is_within_auto_scan_window(recovery_time):
                delay = _seconds_until_next_auto_scan(
                    recovery_time,
                    regular_interval_seconds=interval,
                    opening_interval_seconds=opening_interval,
                )
            else:
                delay = _seconds_until_next_market_open(recovery_time)
        time.sleep(max(1.0, delay))


def parse_force(value):
    """Accept only a JSON boolean for force."""
    if value is None:
        return False
    if type(value) is not bool:
        raise ValueError("force must be a JSON boolean")
    return value


def _tradingview_symbol(value):
    """Normalize TradingView's optional exchange prefix for stock signals."""
    raw = str(value or "").upper().strip()
    if ":" in raw:
        raw = raw.rsplit(":", 1)[-1]
    return raw.replace(" ", "")


def _tradingview_signal_type(payload):
    """Return the explicit TradingView event name, if one was supplied."""
    for key in ("signal_type", "event", "signal", "type", "direction"):
        value = str(payload.get(key) or "").upper().strip()
        if value:
            return value.replace("-", "_").replace(" ", "_")
    return ""


def _tradingview_direction(payload):
    """Extract CALL/PUT from either the direct field or an event name."""
    direct = str(payload.get("direction") or "").upper().strip()
    if direct in ("CALL", "PUT"):
        return direct
    event = _tradingview_signal_type(payload)
    if "CALL" in event:
        return "CALL"
    if "PUT" in event:
        return "PUT"
    return ""


def _normalize_tradingview_payload(path, payload):
    """
    Normalize TradingView JSON without weakening the webhook contract.

    TradingView commonly sends NASDAQ:AAPL for stock charts and OPRA:... for
    option charts. The historical handler already owns the actual trade and
    quote logic, so this adapter only maps those aliases to its stable fields.
    """
    normalized = dict(payload)
    normalized["source"] = str(payload.get("source") or "tradingview").strip()

    if path == "/signal":
        symbol = _tradingview_symbol(
            payload.get("ticker") or payload.get("symbol")
        )
        direction = _tradingview_direction(payload)
        signal_type = _tradingview_signal_type(payload)
        if symbol:
            normalized["ticker"] = symbol
        if direction:
            normalized["direction"] = direction
        if signal_type:
            normalized["signal_type"] = signal_type
        if payload.get("price") is None and payload.get("close") is not None:
            normalized["price"] = payload.get("close")
        if payload.get("interval") is None and payload.get("timeframe") is not None:
            normalized["interval"] = payload.get("timeframe")

    elif path == "/option-price":
        ticker = str(
            payload.get("ticker")
            or payload.get("option_ticker")
            or payload.get("symbol")
            or ""
        ).strip()
        if ticker:
            normalized["ticker"] = ticker
        if payload.get("price") is None and payload.get("close") is not None:
            normalized["price"] = payload.get("close")

    return normalized


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
    if not AUTO_MONITOR_ENABLED:
        print("Auto contract monitor disabled by configuration")
        return None
    return _start_thread_once("monitor", auto_monitor_loop, "reef-auto-monitor")


def start_auto_scanner():
    if not bot.AUTO_SCAN_ENABLED:
        return None
    return _start_thread_once(
        "scanner", bot.auto_scan_loop, "orca-options-scanner"
    )


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
                for _, symbol, trade in _iter_trade_entries(store):
                    if (
                        isinstance(trade, dict)
                        and trade.get("status") == "active"
                        and symbol not in bot.EXCLUDED_SYMBOLS
                    ):
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
                                  "gamma_direction_gate_enabled": GAMMA_DIRECTION_GATE_ENABLED,
                                  "macd_entry_gate_enabled": False,
                                  "data_feed_mode": "delayed/non-paid compatible; not claimed real-time",
                                  "last_scan_decisions": dict(_LAST_SCAN_DECISIONS),
                                  "tradingview_signals": _macd_health_snapshot(),
                                  "pending_price_confirmation": pending,
                                  "paper_performance": performance,
                                  "scanner_interval_seconds": getattr(bot, "AUTO_SCAN_SECONDS", 3600),
                                  "monitor_interval_seconds": getattr(bot, "AUTO_MONITOR_SECONDS", 3600),
                                 "monitored_contracts": monitored,
                                 "trade_state_path": str(trade_state_path()),
                                 "trade_state_path_configured": bool(os.getenv("TRADE_STATE_PATH", "").strip()),
                                 "state_load_status": _STATE_LOAD_STATUS,
                                 "last_successful_state_save": _LAST_SUCCESSFUL_STATE_SAVE,
                                 "restored_active_trade_count": _RESTORED_ACTIVE_TRADE_COUNT,
                                 "recovery_required_count": _RECOVERY_REQUIRED_COUNT,
                                 "scanner_thread_running": _thread_status("scanner"),
                                  "scanner_status": dict(_SCANNER_STATUS),
                                   "ichimoku_worker_running": False,
                                   "ichimoku_entry_gate_enabled": False,
                                 "monitor_thread_running": _thread_status("monitor"),
                                 "duplicate_thread_start_prevented": _DUPLICATE_THREAD_START_PREVENTED,
                                  "configuration": {
                                       "data_provider": DATA_PROVIDER,
                                      "marketdata_quota": _MARKETDATA_QUOTA.snapshot(),
                                        "marketdata_api_key_present": bool(
                                            bot.env("MARKETDATA_API_KEY")
                                        ),
                                       "tradier_token_present": bool(
                                           bot.env("TRADIER_TOKEN")
                                       ),
                                      "telegram_bot_token_present": bool(
                                          bot.env("TELEGRAM_BOT_TOKEN")
                                      ),
                                      "telegram_chat_id_present": bool(
                                          bot.env("TELEGRAM_CHAT_ID")
                                      ),
                                      "massive_enabled": False,
                                      "provider_ready": bool(_provider_token()),
                                      "provider_configuration_migrated": (
                                          REQUESTED_DATA_PROVIDER in REMOVED_PROVIDERS
                                      ),
                                      "webhook_secret_present": bool(
                                          bot.env("REEF_WEBHOOK_SECRET")
                                      ),
                                        "contract_dte_window": (
                                            f"{SCORE_MIN_DTE}-{SCORE_MAX_DTE}"
                                        ),
                                        "auto_scan_opening_window_minutes": (
                                            AUTO_SCAN_OPENING_WINDOW_MINUTES
                                        ),
                                        "auto_scan_opening_interval_seconds": (
                                            AUTO_SCAN_OPENING_INTERVAL_SECONDS
                                        ),
                                        "entry_signal_mode": _autonomous_entry_signal_mode(),
                                        "card_gamma_section_visible": False,
                                        "card_style": CARD_STYLE,
                                        "macd_settings": {
                                            "entry_gate_enabled": False,
                                            "fast": 120, "slow": 180, "signal": 40,
                                             "signal_average": "SMA", "timeframe_minutes": 60,
                                             "ema_length": 200, "ema_average": "EMA",
                                             "price_rule": "above_for_call_below_for_put",
                                             "new_cross_required": False,
                                            "closed_candles_only": True,
                                            "gamma_policy": _gamma_entry_policy(),
                                            "source": "TradingView /macd-signal",
                                        },
                                        "ichimoku_entry_gate_enabled": False,
                                        "auto_monitor_enabled": AUTO_MONITOR_ENABLED,
                                       "excluded_symbols": sorted(bot.EXCLUDED_SYMBOLS),
                                  "option_budget_per_contract_usd": {
                                      "min": round(OPTION_ENTRY_MIN_PREMIUM * 100.0, 2),
                                      "max": round(OPTION_ENTRY_MAX_PREMIUM * 100.0, 2),
                                  },
                                      "massive_chain_cache_ttl_seconds": (
                                          MASSIVE_CHAIN_CACHE_TTL_SECONDS
                                      ),
                                  },
                                 "single_process_expected": True})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/macd-signal":
            secret = bot.env("REEF_WEBHOOK_SECRET")
            if not secret:
                self.json_response(503, {"error": "webhook authentication is not configured"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError("MACD request must be at most 16 KiB")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON object required")
            except (ValueError, UnicodeDecodeError):
                self.json_response(400, {"error": "valid bounded JSON object required"})
                return
            if not hmac.compare_digest(
                str(payload.get("secret", "")).encode("utf-8"), secret.encode("utf-8")
            ):
                self.json_response(401, {"error": "invalid secret"})
                return
            try:
                signal = macd_direction.from_webhook(
                    payload, bot.now_new_york(), bot.WATCHLIST, bot.EXCLUDED_SYMBOLS,
                )
            except macd_direction.MacdDataError as error:
                self.json_response(400, {"error": str(error)})
                return
            with _MACD_CACHE_LOCK:
                previous = _MACD_CACHE.get(signal["symbol"])
                if previous and previous["bar_close_time_ms"] > signal["bar_close_time_ms"]:
                    self.json_response(409, {"error": "out-of-order hourly candle"})
                    return
                if (
                    previous and previous.get("source") == "tradingview_closed_bar"
                    and previous.get("bar_time_ms") == signal["bar_time_ms"]
                    and any(previous.get(field) != signal[field]
                            for field in ("histogram", "macd", "signal", "price", "ema200"))
                ):
                    self.json_response(409, {"error": "conflicting values for an already closed candle"})
                    return
                _MACD_CACHE[signal["symbol"]] = {**signal, "received_at": bot.now_new_york().isoformat()}
            # Cache a heartbeat, not a second scanner. Preserve the approved cadence.
            self.json_response(200, {
                "accepted": True, "cached": True, "signal": signal,
                "used_for_entry": False,
                "note": "diagnostic cache only; MACD/EMA200 entry gates are disabled; no alert sent by this request",
            })
            return
        if not AUTO_MONITOR_ENABLED and path in ("/monitor-tick", "/option-price"):
            self.json_response(
                503, {"error": "automatic contract updates are disabled"}
            )
            return
        if path == "/signal":
            self.json_response(410, {
                "accepted": False,
                "error": (
                    "Entry webhooks are disabled; ORCA qualifies contracts "
                    "using contract quality and relative option activity; Gamma Flip is disabled."
                ),
            })
            return
        if path != "/scan-now":
            if path in ("/monitor-tick", "/option-price"):
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
                payload = _normalize_tradingview_payload(path, payload)
                if path == "/signal":
                    signal_type = str(payload.get("signal_type") or "").upper()
                    if signal_type.startswith("EARLY_") or signal_type.startswith(
                        "FAST_EARLY_"
                    ):
                        self.json_response(202, {
                            "accepted": True,
                            "ignored": "TradingView preparation signal",
                            "signal_type": signal_type,
                            "ticker": payload.get("ticker"),
                        })
                        return
                    if signal_type.startswith("EXIT_"):
                        self.json_response(202, {
                            "accepted": True,
                            "ignored": "TradingView exit signal is informational",
                            "signal_type": signal_type,
                            "ticker": payload.get("ticker"),
                        })
                        return
                    self.json_response(410, {
                        "error": (
                            "entry webhooks are disabled; ORCA selects contracts "
                            "using contract quality and relative option activity; Gamma Flip is disabled"
                        ),
                        "signal_type": signal_type or None,
                        "ticker": payload.get("ticker"),
                    })
                    return
                # The historical handler owns the route implementation. Restore
                # the authenticated body so it can parse it normally.
                normalized_bytes = json.dumps(
                    payload, ensure_ascii=False
                ).encode("utf-8")
                self.headers.replace_header(
                    "Content-Length", str(len(normalized_bytes))
                )
                self.rfile = io.BytesIO(normalized_bytes)
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
bot.massive_contract_snapshot = provider_contract_snapshot
bot.monitor_active_trades_once = monitor_active_trades_once
bot.monitor_active_trade_once = monitor_active_trades_once
bot.auto_monitor_loop = auto_monitor_loop
bot.auto_scan_loop = auto_scan_loop
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
    if _provider_is_marketdata():
        _MARKETDATA_QUOTA.configure_storage(
            trade_state_path().with_name("marketdata_quota_state.json")
        )
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