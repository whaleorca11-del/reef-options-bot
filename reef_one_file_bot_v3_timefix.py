import json
import os
import re
import threading
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

# ============================================================
# REEF ONE FILE BOT
# TradingView -> Python -> Massive -> Telegram
# + Option live price updates -> targets / stop loss
# ============================================================

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8000"))
BASE_URL = "https://api.massive.com"
STATE_FILE = Path(__file__).with_name("reef_trade_state.json")
NEW_YORK_TZ = ZoneInfo("America/New_York")

WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "META", "AMZN", "TSLA", "AMD", "GOOGL",
    "PEP", "MSTR", "PLTR", "MU", "AVGO", "JNJ", "PG", "PANW", "IBM", "MDB",
]

COMPANY_NAMES = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "META": "Meta Platforms",
    "AMZN": "Amazon",
    "TSLA": "Tesla",
    "AMD": "Advanced Micro Devices",
    "GOOGL": "Alphabet",
    "PEP": "PepsiCo",
    "MSTR": "Strategy",
    "PLTR": "Palantir",
    "MU": "Micron Technology",
    "AVGO": "Broadcom",
    "JNJ": "Johnson & Johnson",
    "PG": "Procter & Gamble",
    "PANW": "Palo Alto Networks",
    "IBM": "IBM",
    "MDB": "MongoDB",
}


def env(name):
    return os.getenv(name, "").strip()


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def now_new_york():
    """Timezone-aware current time in New York (handles DST automatically)."""
    return datetime.now(NEW_YORK_TZ)


def format_entry_time_et(value):
    """Return a friendly New York market-time string for Telegram."""
    try:
        if not value:
            dt = now_new_york()
        else:
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=NEW_YORK_TZ)
            else:
                dt = dt.astimezone(NEW_YORK_TZ)
        return dt.strftime("%I:%M:%S %p ET").lstrip("0")
    except Exception:
        return now_new_york().strftime("%I:%M:%S %p ET").lstrip("0")


def canonical_option_ticker(value):
    """
    Converts TradingView/OPRA and Massive/OCC option tickers to one format.

    Examples:
      OPRA:NVDA260831C217.5     -> NVDA260831C00217500
      NVDA260831C217.5          -> NVDA260831C00217500
      O:NVDA260831C00217500     -> NVDA260831C00217500
      NVDA260831C00217500       -> NVDA260831C00217500
    """
    raw = str(value or "").upper().strip()
    raw = raw.replace("OPRA:", "").replace("O:", "")
    raw = raw.replace(" ", "")

    # Massive/OCC format already uses an 8-digit strike field.
    occ_match = re.fullmatch(
        r"([A-Z.\-]+)(\d{6})([CP])(\d{8})",
        raw,
    )
    if occ_match:
        return raw

    # TradingView compact format, e.g. NVDA260831C217.5
    tv_match = re.fullmatch(
        r"([A-Z.\-]+)(\d{6})([CP])(\d+(?:\.\d+)?)",
        raw,
    )
    if not tv_match:
        return raw

    root, expiry, right, strike_text = tv_match.groups()

    try:
        strike = Decimal(strike_text)
        strike_millis = int(
            (strike * Decimal("1000")).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
    except (InvalidOperation, ValueError):
        return raw

    return f"{root}{expiry}{right}{strike_millis:08d}"


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def telegram_send(text):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")

    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["result"]["message_id"]


def telegram_edit(message_id, text):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")

    r = requests.post(
        f"https://api.telegram.org/bot{token}/editMessageText",
        json={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        },
        timeout=10,
    )

    if r.status_code == 400 and "message is not modified" in r.text.lower():
        return True

    r.raise_for_status()
    return True


def massive_chain(symbol, direction):
    api_key = env("MASSIVE_API_KEY")
    if not api_key:
        raise RuntimeError("MASSIVE_API_KEY is missing")

    contract_type = "call" if direction == "CALL" else "put"

    r = requests.get(
        f"{BASE_URL}/v3/snapshot/options/{symbol}",
        params={
            "contract_type": contract_type,
            "limit": 250,
            "apiKey": api_key,
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("results", [])


def normalize_contract(raw, stock_price):
    details = raw.get("details", {}) or {}
    day = raw.get("day", {}) or {}
    greeks = raw.get("greeks", {}) or {}
    quote = raw.get("last_quote", {}) or {}
    trade = raw.get("last_trade", {}) or {}

    strike = safe_float(details.get("strike_price"))
    bid = safe_float(quote.get("bid") or quote.get("bid_price"))
    ask = safe_float(quote.get("ask") or quote.get("ask_price"))
    last_price = safe_float(trade.get("price"))

    spread_percent = None
    if bid > 0 and ask > 0 and ask >= bid:
        midpoint = (bid + ask) / 2
        if midpoint > 0:
            spread_percent = ((ask - bid) / midpoint) * 100

    return {
        "ticker": str(details.get("ticker", "")).replace("O:", ""),
        "expiration": details.get("expiration_date", ""),
        "strike": strike,
        "distance": abs(strike - stock_price),
        "open_interest": safe_float(raw.get("open_interest")),
        "volume": safe_float(day.get("volume")),
        "bid": bid,
        "ask": ask,
        "last": last_price,
        "day_close": safe_float(day.get("close")),
        "day_open": safe_float(day.get("open")),
        "spread_percent": spread_percent,
        "delta": greeks.get("delta"),
        "theta": greeks.get("theta"),
        "iv": raw.get("implied_volatility"),
    }


def nearest_expiration(contracts):
    today = date.today()
    valid = []

    for c in contracts:
        try:
            d = datetime.strptime(c["expiration"], "%Y-%m-%d").date()
            if d >= today:
                valid.append(d)
        except Exception:
            pass

    return min(valid) if valid else None


def calculate_score(c, stock_price):
    score = 70.0

    if stock_price > 0:
        distance_pct = (c["distance"] / stock_price) * 100
        score -= min(distance_pct * 8, 35)

    oi = c["open_interest"]
    if oi >= 5000:
        score += 10
    elif oi >= 2000:
        score += 8
    elif oi >= 1000:
        score += 6
    elif oi >= 500:
        score += 4
    elif oi > 0:
        score += 1
    else:
        score -= 5

    volume = c["volume"]
    if volume >= 5000:
        score += 10
    elif volume >= 2000:
        score += 8
    elif volume >= 1000:
        score += 6
    elif volume >= 500:
        score += 4
    elif volume > 0:
        score += 1
    else:
        score -= 5

    spread = c["spread_percent"]
    if spread is not None:
        if spread <= 5:
            score += 10
        elif spread <= 10:
            score += 7
        elif spread <= 20:
            score += 2
        elif spread > 30:
            score -= 10

    delta = c["delta"]
    if delta is not None:
        ad = abs(safe_float(delta))
        if 0.45 <= ad <= 0.60:
            score += 10
        elif 0.35 <= ad < 0.45 or 0.60 < ad <= 0.70:
            score += 5
        elif ad < 0.20:
            score -= 8

    theta = c["theta"]
    if theta is not None:
        at = abs(safe_float(theta))
        if at > 0.50:
            score -= 8
        elif at > 0.25:
            score -= 4

    return round(max(0, min(score, 100)), 1)


def select_contract(symbol, direction, stock_price):
    raw = massive_chain(symbol, direction)
    contracts = []

    for item in raw:
        c = normalize_contract(item, stock_price)
        if c["ticker"] and c["expiration"]:
            contracts.append(c)

    exp = nearest_expiration(contracts)
    if not exp:
        return None

    exp_text = exp.strftime("%Y-%m-%d")
    contracts = [c for c in contracts if c["expiration"] == exp_text]

    for c in contracts:
        c["score"] = calculate_score(c, stock_price)

    contracts.sort(
        key=lambda x: (
            -x["score"],
            x["distance"],
            -x["volume"],
            -x["open_interest"],
        )
    )

    return contracts[0] if contracts else None


def choose_initial_price(c):
    for source, value in [
        ("Ask", c.get("ask")),
        ("Last", c.get("last")),
        ("Bid", c.get("bid")),
        ("Day Close", c.get("day_close")),
        ("Day Open", c.get("day_open")),
    ]:
        p = safe_float(value)
        if p > 0:
            return p, source
    return 0.0, "Unavailable"


def targets(entry):
    return [
        round(entry * 1.20, 2),
        round(entry * 1.45, 2),
        round(entry * 1.70, 2),
        round(entry * 2.00, 2),
        round(entry * 2.50, 2),
    ]


def stop_loss(entry):
    return round(entry * 0.55, 2)


def message_from_state(s):
    t = s["targets"]
    achieved = int(s.get("achieved", 0))
    current = safe_float(s.get("current_price"), s["entry_price"])
    highest = safe_float(s.get("highest_price"), s["entry_price"])
    entry = safe_float(s["entry_price"])

    current_return = ((current / entry) - 1) * 100 if entry else 0
    highest_return = ((highest / entry) - 1) * 100 if entry else 0

    company = COMPANY_NAMES.get(s["symbol"], s["symbol"])
    direction_ar = "كول" if s["direction"] == "CALL" else "بوت"
    direction_icon = "🟩" if s["direction"] == "CALL" else "🟥"

    lines = [
        "🚨 REEF OPTIONS",
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"🏢 الشركة: {company} ({s['symbol']})",
        f"{direction_icon} الاتجاه: {s['direction']} — {direction_ar}",
        f"🎟 العقد: {s['contract_ticker']}",
        f"📅 الانتهاء: {s['expiration']}",
        f"🎯 Strike: {s['strike']}",
        "",
        f"💵 الدخول: {entry:.2f} ({s['entry_source']})",
        f"🕒 وقت الإشارة (نيويورك): {format_entry_time_et(s.get('created_at'))}",
        f"🛑 وقف الخسارة: {s['stop_loss']:.2f} — -45%",
        f"💰 السعر الحالي للعقد: {current:.2f}",
        f"📈 العائد الحالي: {current_return:+.1f}%",
        f"🚀 أعلى سعر: {highest:.2f}",
        f"📊 أعلى عائد: {highest_return:+.1f}%",
        f"📈 سعر السهم وقت الإشارة: {s['stock_price']:.2f}",
        "",
        "🎯 الأهداف",
    ]

    icons = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    pcts = [20, 45, 70, 100, 150]

    for i, price in enumerate(t):
        status = "✅" if i < achieved else "⬜"
        lines.append(f"{status} {icons[i]} {price:.2f} — +{pcts[i]}%")

    lines.append("")

    if s.get("status") == "stopped":
        lines.append("🛑 تم تفعيل وقف الخسارة -45%")
    elif achieved >= 5:
        lines.append("🏆 تم تحقيق جميع الأهداف")
    else:
        lines.append(f"➡️ الهدف القادم: {t[achieved]:.2f}")

    lines.extend([
        "",
        f"⭐ التقييم: {s['score']} / 100",
        "━━━━━━━━━━━━━━━━━━",
    ])

    if s.get("status") == "stopped":
        lines.append("⛔ انتهت المتابعة عند وقف الخسارة")
    elif achieved >= 5:
        lines.append("🎉 اكتملت أهداف الصفقة")
    elif achieved == 0:
        lines.append("⌛ بانتظار تحقق الهدف الأول")
    else:
        lines.append(f"✅ تم تحقيق {achieved} من 5 أهداف")

    return "\n".join(lines)


def create_trade(symbol, direction, stock_price):
    contract = select_contract(symbol, direction, stock_price)
    if not contract:
        raise RuntimeError("No suitable contract found")

    entry, source = choose_initial_price(contract)
    if entry <= 0:
        raise RuntimeError("No usable option price found")

    state = {
        "symbol": symbol,
        "direction": direction,
        "stock_price": stock_price,
        "contract_ticker": contract["ticker"],
        "expiration": contract["expiration"],
        "strike": contract["strike"],
        "score": contract["score"],
        "entry_price": round(entry, 2),
        "entry_source": source,
        "stop_loss": stop_loss(entry),
        "targets": targets(entry),
        "current_price": round(entry, 2),
        "highest_price": round(entry, 2),
        "achieved": 0,
        "status": "active",
        "message_id": None,
        "created_at": now_new_york().isoformat(timespec="seconds"),
    }

    message_id = telegram_send(message_from_state(state))
    state["message_id"] = message_id
    save_state(state)

    print(
        f"✅ New trade: {symbol} {direction} | "
        f"{contract['ticker']} | entry {entry:.2f}"
    )

    return state


def update_option_price(option_ticker, option_price):
    state = load_state()

    if not state or state.get("status") != "active":
        return {"accepted": True, "ignored": "no active trade"}

    incoming = canonical_option_ticker(option_ticker)
    expected = canonical_option_ticker(state.get("contract_ticker", ""))

    if incoming != expected:
        return {
            "accepted": True,
            "ignored": "different option contract",
            "expected": expected,
            "received": incoming,
        }

    price = safe_float(option_price)
    if price <= 0:
        return {"accepted": False, "error": "invalid option price"}

    state["current_price"] = round(price, 2)
    state["highest_price"] = round(
        max(safe_float(state.get("highest_price")), price),
        2,
    )

    # Stop-loss is based on CURRENT TradingView option price.
    if price <= safe_float(state["stop_loss"]):
        state["status"] = "stopped"
    else:
        achieved = int(state.get("achieved", 0))
        while (
            achieved < 5
            and safe_float(state["highest_price"]) >= safe_float(state["targets"][achieved])
        ):
            achieved += 1
        state["achieved"] = achieved

        if achieved >= 5:
            state["status"] = "completed"

    save_state(state)

    if state.get("message_id"):
        telegram_edit(state["message_id"], message_from_state(state))

    print(
        f"📍 {incoming} = {price:.2f} | "
        f"targets {state['achieved']}/5 | status {state['status']}"
    )

    return {
        "accepted": True,
        "ticker": incoming,
        "price": price,
        "achieved": state["achieved"],
        "status": state["status"],
    }


class Handler(BaseHTTPRequestHandler):
    def json_response(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            self.json_response(200, {
                "ok": True,
                "service": "REEF ONE FILE BOT",
            })
            return

        self.json_response(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.json_response(400, {"error": "JSON required"})
            return

        secret = env("REEF_WEBHOOK_SECRET")
        if secret and str(payload.get("secret", "")) != secret:
            self.json_response(401, {"error": "invalid secret"})
            return

        try:
            if path == "/signal":
                symbol = str(payload.get("ticker", "")).upper().strip()
                direction = str(payload.get("direction", "")).upper().strip()
                stock_price = safe_float(payload.get("price"))

                if symbol not in WATCHLIST:
                    self.json_response(400, {"error": "symbol not in watchlist"})
                    return

                if direction not in ("CALL", "PUT"):
                    self.json_response(400, {"error": "direction must be CALL or PUT"})
                    return

                if stock_price <= 0:
                    self.json_response(400, {"error": "invalid stock price"})
                    return

                state = create_trade(symbol, direction, stock_price)

                self.json_response(202, {
                    "accepted": True,
                    "contract": state["contract_ticker"],
                    "entry": state["entry_price"],
                })
                return

            if path == "/option-price":
                ticker = str(payload.get("ticker", "")).strip()
                price = safe_float(payload.get("price"))

                result = update_option_price(ticker, price)
                self.json_response(
                    202 if result.get("accepted") else 400,
                    result,
                )
                return

            self.json_response(404, {"error": "unknown webhook path"})

        except Exception as error:
            print("❌", error)
            self.json_response(500, {"error": str(error)})

    def log_message(self, format, *args):
        return


def main():
    missing = [
        name for name in
        ("MASSIVE_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        if not env(name)
    ]

    if missing:
        print("❌ Missing environment variables:", ", ".join(missing))
        return

    print("=" * 60)
    print("REEF ONE FILE BOT")
    print("=" * 60)
    print(f"Local URL: http://127.0.0.1:{PORT}")
    print(f"Health:    http://127.0.0.1:{PORT}/health")
    print(f"Signal:    POST /signal")
    print(f"Option:    POST /option-price")
    print()
    print("✅ جاهز لاستقبال TradingView")
    print("لإيقافه: Ctrl + C")
    print()

    server = ThreadingHTTPServer((HOST, PORT), Handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⛔ تم الإيقاف.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
