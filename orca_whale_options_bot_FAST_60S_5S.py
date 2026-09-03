import json
import math
import os
import re
import threading
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from io import BytesIO
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

# ============================================================
# REEF ONE FILE BOT
# TradingView -> Python -> Massive -> Telegram
# + Massive option-contract tracking -> targets / stop loss
# + CONTRACT LOCK: one active contract per stock; duplicate stock signals are ignored
# ============================================================

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8000"))
BASE_URL = "https://api.massive.com"
STATE_FILE = Path(__file__).with_name("reef_trade_state.json")

# HARD LOCK: prevents two simultaneous TradingView requests from opening
# two contracts for the same stock before state is saved.
TRADE_STATE_LOCK = threading.RLock()
AUTO_MONITOR_SECONDS = int(os.getenv("AUTO_MONITOR_SECONDS", "5"))
AUTO_SCAN_ENABLED = os.getenv("AUTO_SCAN_ENABLED", "1").strip().lower() not in ("0", "false", "no")
AUTO_SCAN_SECONDS = int(os.getenv("AUTO_SCAN_SECONDS", "60"))
AUTO_SCAN_MIN_SCORE = float(os.getenv("AUTO_SCAN_MIN_SCORE", "78"))
AUTO_SCAN_MIN_EDGE = float(os.getenv("AUTO_SCAN_MIN_EDGE", "3"))
AUTO_SCAN_MIN_VOLUME = float(os.getenv("AUTO_SCAN_MIN_VOLUME", "50"))
GAMMA_FLIP_ENABLED = os.getenv("GAMMA_FLIP_ENABLED", "1").strip().lower() not in ("0", "false", "no")
GAMMA_FLIP_RANGE_PCT = float(os.getenv("GAMMA_FLIP_RANGE_PCT", "12"))
GAMMA_FLIP_STEPS = int(os.getenv("GAMMA_FLIP_STEPS", "121"))
GAMMA_RISK_FREE_RATE = float(os.getenv("GAMMA_RISK_FREE_RATE", "0.043"))
GAMMA_FLIP_BUFFER_PCT = float(os.getenv("GAMMA_FLIP_BUFFER_PCT", "0.10"))
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


COMPANY_LOGO_DOMAINS = {
    "AAPL": "apple.com",
    "MSFT": "microsoft.com",
    "NVDA": "nvidia.com",
    "META": "meta.com",
    "AMZN": "amazon.com",
    "TSLA": "tesla.com",
    "AMD": "amd.com",
    "GOOGL": "google.com",
    "PEP": "pepsico.com",
    "MSTR": "strategy.com",
    "PLTR": "palantir.com",
    "MU": "micron.com",
    "AVGO": "broadcom.com",
    "JNJ": "jnj.com",
    "PG": "pg.com",
    "PANW": "paloaltonetworks.com",
    "IBM": "ibm.com",
    "MDB": "mongodb.com",
}


def fetch_company_logo(symbol, size=180):
    """Fetch and cache a company logo. Returns a transparent RGBA image or None."""
    symbol = str(symbol or "").upper().strip()
    domain = COMPANY_LOGO_DOMAINS.get(symbol)
    if not domain:
        return None

    cache_dir = Path(__file__).with_name("reef_logo_cache")
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / f"{symbol}.png"

    try:
        if cache_file.exists() and cache_file.stat().st_size > 100:
            img = Image.open(cache_file).convert("RGBA")
            img.thumbnail((size, size), Image.LANCZOS)
            return img
    except Exception:
        pass

    # Primary source: Clearbit logo endpoint.
    urls = [
        f"https://logo.clearbit.com/{domain}?size=256",
        f"https://www.google.com/s2/favicons?domain={domain}&sz=256",
    ]

    for url in urls:
        try:
            r = requests.get(
                url,
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code != 200 or not r.content:
                continue

            img = Image.open(BytesIO(r.content)).convert("RGBA")
            img.thumbnail((size, size), Image.LANCZOS)

            # Save a full-size cached copy.
            try:
                img.save(cache_file, "PNG")
            except Exception:
                pass

            return img
        except Exception:
            continue

    return None


def paste_logo_card(base_img, symbol, box):
    """Paste company logo centered in the supplied box. Falls back to ticker text."""
    x1, y1, x2, y2 = box
    draw = ImageDraw.Draw(base_img)
    logo = fetch_company_logo(symbol, size=min(x2-x1-24, y2-y1-24))

    if logo is not None:
        px = x1 + ((x2 - x1) - logo.width) // 2
        py = y1 + ((y2 - y1) - logo.height) // 2
        base_img.paste(logo, (px, py), logo)
        return

    ticker = str(symbol or "")[:5]
    font = _font(34, True)
    bbox = draw.textbbox((0, 0), ticker, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        (x1 + ((x2-x1)-tw)/2, y1 + ((y2-y1)-th)/2 - 4),
        ticker,
        font=font,
        fill=(242, 247, 250),
    )



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
    # Atomic write: write to a temporary file, then replace the state file.
    temp_file = STATE_FILE.with_suffix(".json.tmp")
    temp_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_file.replace(STATE_FILE)


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_trade_store():
    state = load_state()

    if isinstance(state, dict) and isinstance(state.get("trades"), dict):
        return state

    # Backward compatibility with older single-trade state files.
    if isinstance(state, dict) and state.get("symbol"):
        symbol = str(state.get("symbol") or "").upper().strip()
        if symbol:
            return {"trades": {symbol: state}}

    return {"trades": {}}


def _save_trade_store(store):
    if not isinstance(store, dict):
        store = {"trades": {}}
    if not isinstance(store.get("trades"), dict):
        store["trades"] = {}
    save_state(store)



def _font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _money(v):
    return f"{safe_float(v):.2f}"


def _days_to_expiry(expiration):
    try:
        exp = datetime.strptime(str(expiration), "%Y-%m-%d").date()
        return max((exp - now_new_york().date()).days, 0)
    except Exception:
        return 0


def render_trade_card(s):
    """Build the dark REEF OPTIONS image card used for both new signals and live updates."""
    W, H = 1080, 1500
    bg = (5, 17, 29)
    panel = (8, 25, 40)
    panel2 = (12, 34, 52)
    line = (38, 63, 82)
    white = (242, 247, 250)
    muted = (157, 174, 188)
    blue = (73, 171, 255)
    green = (45, 220, 90)
    red = (255, 69, 69)

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    # subtle background grid
    for x in range(0, W, 90):
        d.line((x, 0, x, H), fill=(7, 24, 38), width=1)
    for y in range(0, H, 90):
        d.line((0, y, W, y), fill=(7, 24, 38), width=1)

    # Main rounded card
    d.rounded_rectangle((42, 42, W-42, H-42), radius=36, fill=panel, outline=(25, 55, 76), width=2)

    company = COMPANY_NAMES.get(s["symbol"], s["symbol"])
    direction = str(s["direction"]).upper()
    dir_color = green if direction == "CALL" else red
    status = str(s.get("status", "active")).lower()
    achieved = int(s.get("achieved", 0))
    entry = safe_float(s["entry_price"])
    current = safe_float(s.get("current_price"), entry)
    change = current - entry
    pct = ((current / entry) - 1) * 100 if entry else 0
    t = s["targets"]
    stop = safe_float(s["stop_loss"])
    dte = _days_to_expiry(s["expiration"])

    gamma_flip = safe_float(s.get("gamma_flip"))
    gamma_stock = safe_float(s.get("gamma_stock_price"), safe_float(s.get("stock_price")))
    gamma_regime = str(s.get("gamma_regime") or "N/A").upper()
    if gamma_flip > 0 and gamma_stock > 0:
        gamma_distance = gamma_stock - gamma_flip
        gamma_distance_pct = (gamma_distance / gamma_flip) * 100.0
        gamma_side = "ABOVE" if gamma_distance > 0 else "BELOW" if gamma_distance < 0 else "AT FLIP"
        gamma_ok = (direction == "CALL" and gamma_distance > 0) or (direction == "PUT" and gamma_distance < 0)
        gamma_status = "BULLISH SIDE" if direction == "CALL" and gamma_ok else (
            "BEARISH SIDE" if direction == "PUT" and gamma_ok else "EXIT WARNING"
        )
    else:
        gamma_distance = 0.0
        gamma_distance_pct = 0.0
        gamma_side = "N/A"
        gamma_status = "N/A"

    # Header
    d.text((95, 85), "ORCA WHALE OPTIONS SIGNAL", font=_font(42, True), fill=blue)
    badge = "LIVE" if status == "active" else ("STOPPED" if status == "stopped" else "COMPLETED")
    badge_color = green if status == "active" else (red if status == "stopped" else blue)
    d.rounded_rectangle((840, 76, 980, 126), radius=15, fill=(12, 62, 37) if status=="active" else (65, 25, 29))
    d.text((875, 84), badge, font=_font(24, True), fill=badge_color)

    # Symbol badge + company
    d.rounded_rectangle((90, 165, 245, 320), radius=28, fill=(12, 41, 62), outline=(30, 85, 116), width=2)
    paste_logo_card(img, s["symbol"], (90, 165, 245, 320))
    d.text((285, 175), f"{direction} ↗" if direction=="CALL" else f"{direction} ↘", font=_font(55, True), fill=dir_color)
    d.text((285, 250), company, font=_font(30, True), fill=white)

    contract = canonical_option_ticker(s["contract_ticker"])
    d.rounded_rectangle((285, 315, 920, 382), radius=18, fill=panel2, outline=line, width=2)
    d.text((315, 330), contract, font=_font(34, True), fill=white)

    exp_display = str(s["expiration"])
    d.text((95, 410), f"{s['symbol']}  ${safe_float(s['stock_price']):.2f}  {direction}   {exp_display}  ({dte}DTE)",
           font=_font(25), fill=muted)

    # Price metrics
    y = 490
    cols = [(95, "ENTRY PRICE", entry), (405, "CURRENT PRICE", current), (730, "CHANGE", None)]
    for x, label, val in cols:
        d.text((x, y), label, font=_font(23, True), fill=blue)
        if label == "CHANGE":
            value = f"{change:+.2f} ({pct:+.1f}%)"
            d.text((x, y+48), value, font=_font(42, True), fill=green if pct >= 0 else red)
        else:
            d.text((x, y+48), f"${val:.2f}", font=_font(43, True), fill=white)
    d.line((85, 610, 995, 610), fill=line, width=2)

    # Targets: preserve actual bot requirements: +20,+45,+70,+100,+150 and stop -45
    d.text((95, 650), "PROFIT TARGETS", font=_font(31, True), fill=blue)
    pcts = [20, 45, 70, 100, 150]
    for i, (price, pp) in enumerate(zip(t, pcts)):
        row = i // 3
        col = i % 3
        x = 95 + col * 300
        yy = 705 + row * 120
        hit = i < achieved
        color = green if hit else white
        icon = "✓" if hit else "○"
        d.text((x, yy), f"{icon} T{i+1}", font=_font(31, True), fill=green if hit else muted)
        d.text((x+72, yy), f"${safe_float(price):.2f}", font=_font(31, True), fill=color)
        d.text((x+72, yy+42), f"+{pp}%", font=_font(22, True), fill=green)

    # Stop
    d.rounded_rectangle((695, 825, 960, 925), radius=20, fill=(45, 20, 25), outline=(110, 35, 42), width=2)
    d.text((720, 842), "STOP LOSS", font=_font(22, True), fill=red)
    d.text((720, 875), f"${stop:.2f}   (-45%)", font=_font(29, True), fill=white)

    # Progress
    d.line((120, 1010, 930, 1010), fill=(93, 111, 126), width=5)
    points = [120, 322, 525, 727, 930]
    for i, x in enumerate(points):
        hit = i < achieved
        fill = green if hit else panel
        outline = green if hit else (130, 151, 167)
        d.ellipse((x-25, 985, x+25, 1035), fill=fill, outline=outline, width=4)
        if hit:
            d.text((x-10, 989), "✓", font=_font(32, True), fill=white)
        d.text((x-20, 1048), f"T{i+1}", font=_font(20, True), fill=white)
    d.text((95, 950), f"TARGET PROGRESS   {achieved}/5", font=_font(24, True), fill=white)

    # Contract details table
    d.rounded_rectangle((80, 1110, 1000, 1350), radius=24, fill=panel2)
    details = [
        ("TICKER", s["symbol"]),
        ("EXPIRATION", f"{s['expiration']} ({dte}DTE)"),
        ("STRIKE", f"${safe_float(s['strike']):.2f}"),
        ("CONTRACT TYPE", direction),
        ("CONTRACT", contract),
        ("GAMMA FLIP", f"${gamma_flip:.2f}" if gamma_flip > 0 else "N/A"),
        ("GAMMA STATUS", (
            f"{gamma_side}  {gamma_distance_pct:+.2f}%  •  {gamma_regime}"
            if gamma_flip > 0 else "N/A"
        )),
    ]
    yy = 1125
    for label, value in details:
        d.text((115, yy), label, font=_font(20, True), fill=muted)
        vb = d.textbbox((0,0), str(value), font=_font(21, True))
        d.text((945-(vb[2]-vb[0]), yy), str(value), font=_font(21, True), fill=white)
        yy += 31

    # Footer / time / score
    d.text((95, 1380), f"Entry {format_entry_time_et(s.get('created_at'))}", font=_font(20), fill=muted)
    d.text((430, 1380), f"Score {s['score']}/100", font=_font(20), fill=muted)
    d.text((695, 1380), "Powered by ORCA WHALE OPTIONS BOT", font=_font(20, True), fill=blue)

    out = Path(__file__).with_name("reef_trade_card.png")
    img.save(out, "PNG", optimize=True)
    return out


def telegram_send_card(state):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")

    image_path = render_trade_card(state)
    with image_path.open("rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id},
            files={"photo": ("reef_options.png", f, "image/png")},
            timeout=20,
        )
    r.raise_for_status()
    return r.json()["result"]["message_id"]


def telegram_edit_card(message_id, state):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    image_path = render_trade_card(state)

    media = json.dumps({
        "type": "photo",
        "media": "attach://photo",
    })
    with image_path.open("rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/editMessageMedia",
            data={
                "chat_id": chat_id,
                "message_id": str(message_id),
                "media": media,
            },
            files={"photo": ("reef_options.png", f, "image/png")},
            timeout=20,
        )
    if r.status_code == 400 and "message is not modified" in r.text.lower():
        return True
    r.raise_for_status()
    return True


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



def telegram_edit_card_retry(message_id, state, attempts=3):
    """
    Target/stop updates are important. Retry editing the SAME Telegram card
    if Telegram or the network has a transient failure.
    """
    if not message_id:
        return False

    for attempt in range(1, attempts + 1):
        try:
            if telegram_edit_card(message_id, state):
                return True
        except Exception as error:
            print(f"Telegram edit attempt {attempt} failed:", error)

        if attempt < attempts:
            time.sleep(1)

    return False

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
    direction = s["direction"]
    direction_icon = "🟢" if direction == "CALL" else "🔴"

    status = str(s.get("status", "active")).lower()
    if status == "stopped":
        status_text = "STOPPED"
        status_icon = "🛑"
    elif status == "completed" or achieved >= 5:
        status_text = "COMPLETED"
        status_icon = "🏆"
    else:
        status_text = "ACTIVE"
        status_icon = "⚡"

    progress = "█" * achieved + "░" * (5 - achieved)

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        "🚨 ORCA WHALE OPTIONS ALERT",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🏢 {company}  •  {s['symbol']}",
        f"{direction_icon} {direction}  |  {status_icon} {status_text}",
        "",
        "📌 CONTRACT",
        f"• Ticker: {s['contract_ticker']}",
        f"• Expiry: {s['expiration']}",
        f"• Strike: {s['strike']}",
        "",
        "💰 TRADE",
        f"• Entry:   ${entry:.2f}",
        f"• Current: ${current:.2f}",
        f"• Return:  {current_return:+.1f}%",
        f"• High:    ${highest:.2f}  ({highest_return:+.1f}%)",
        f"• Stop:    ${s['stop_loss']:.2f}  (-45%)",
        "",
        f"🕒 Entry Time: {format_entry_time_et(s.get('created_at'))}",
        f"📈 Stock at Signal: ${s['stock_price']:.2f}",
        "",
        f"🎯 TARGETS  [{progress}]  {achieved}/5",
    ]

    pcts = [20, 45, 70, 100, 150]
    for i, price in enumerate(t):
        icon = "✅" if i < achieved else "⬜"
        lines.append(f"{icon} T{i+1}  ${price:.2f}   (+{pcts[i]}%)")

    lines.append("")

    if status == "stopped":
        lines.append("🛑 Stop Loss Hit — Trade Closed")
    elif achieved >= 5:
        lines.append("🏆 All Targets Achieved")
    else:
        lines.append(f"➡️ Next Target: ${t[achieved]:.2f}")

    lines.extend([
        "",
        f"⭐ Contract Score: {s['score']} / 100",
        f"🔎 Entry Source: {s['entry_source']}",
        "",
        "ORCA WHALE OPTIONS • Live Contract Tracking",
        "━━━━━━━━━━━━━━━━━━━━",
    ])

    return "\n".join(lines)




def massive_contract_snapshot(symbol, contract_ticker):
    """Fetch one selected option contract snapshot from Massive."""
    api_key = env("MASSIVE_API_KEY")
    if not api_key:
        raise RuntimeError("MASSIVE_API_KEY is missing")

    canonical = canonical_option_ticker(contract_ticker)
    option_symbol = "O:" + canonical

    r = requests.get(
        f"{BASE_URL}/v3/snapshot/options/{symbol}/{option_symbol}",
        params={"apiKey": api_key},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("results") or data


def extract_snapshot_price(snapshot):
    """
    V11 fresh option pricing.

    Prefer the live option QUOTE midpoint because stale last_trade values can lag
    badly in thin option contracts. Fall back to last trade, then bid/ask, then
    session/day values only if necessary.
    """
    results = snapshot.get("results")

    if isinstance(results, list):
        if not results:
            return 0.0, "none"
        data = results[0] or {}
    elif isinstance(results, dict):
        data = results
    else:
        data = snapshot or {}

    quote = data.get("last_quote") or {}
    trade = data.get("last_trade") or {}
    session = data.get("session") or {}
    day = data.get("day") or {}

    bid = safe_float(quote.get("bid") or quote.get("bid_price"))
    ask = safe_float(quote.get("ask") or quote.get("ask_price"))
    midpoint = safe_float(quote.get("midpoint"))

    # Best live mark for monitoring a contract: current bid/ask midpoint.
    if midpoint > 0:
        return midpoint, "quote_midpoint"

    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0, "quote_midpoint_calc"

    # If only one side exists, use it before falling back to an old print.
    if bid > 0:
        return bid, "quote_bid"

    if ask > 0:
        return ask, "quote_ask"

    # Last trade can be stale in low-liquidity option contracts, so it is fallback.
    last_trade = safe_float(trade.get("price"))
    if last_trade > 0:
        return last_trade, "last_trade"

    # Last-resort fallbacks.
    for source, value in (
        ("session_close", session.get("close")),
        ("session_open", session.get("open")),
        ("day_close", day.get("close")),
        ("day_open", day.get("open")),
    ):
        value = safe_float(value)
        if value > 0:
            return value, source

    return 0.0, "none"


def monitor_active_trades_once():
    """
    Poll ONLY the already-selected option contracts from Massive.
    Stock prices and new TradingView signals do not change a locked contract.
    """
    store = _load_trade_store()
    trades = store.get("trades", {})

    active_symbols = [
        symbol
        for symbol, trade in trades.items()
        if isinstance(trade, dict) and trade.get("status") == "active"
    ]

    if not active_symbols:
        return {"accepted": True, "ignored": "no active trades"}

    results = []

    for symbol in active_symbols:
        trade = trades.get(symbol, {})
        contract = str(trade.get("contract_ticker", "")).strip()

        if not contract:
            results.append({
                "accepted": False,
                "symbol": symbol,
                "error": "active trade missing contract",
            })
            continue

        try:
            snapshot = massive_contract_snapshot(symbol, contract)
            price, source = extract_snapshot_price(snapshot)

            if price <= 0:
                results.append({
                    "accepted": True,
                    "symbol": symbol,
                    "contract": contract,
                    "ignored": "no usable option price",
                })
                continue

            result = update_option_price(contract, price)
            result["symbol"] = symbol
            result["source"] = source
            print(f"💹 {symbol} {contract} price={price:.2f} source={source}")
            results.append(result)

        except Exception as error:
            results.append({
                "accepted": False,
                "symbol": symbol,
                "contract": contract,
                "error": str(error),
            })

    return {
        "accepted": True,
        "active_checked": len(active_symbols),
        "results": results,
    }


# Backward-compatible name for /monitor-tick and any old references.
def monitor_active_trade_once():
    return monitor_active_trades_once()


def auto_monitor_loop():
    print(f"Auto contract monitor: every {AUTO_MONITOR_SECONDS}s")
    while True:
        try:
            monitor_active_trades_once()
        except Exception as error:
            print("Auto monitor warning:", error)
        time.sleep(max(AUTO_MONITOR_SECONDS, 5))


def start_auto_monitor():
    thread = threading.Thread(
        target=auto_monitor_loop,
        name="reef-auto-monitor",
        daemon=True,
    )
    thread.start()
    return thread


def create_trade(symbol, direction, stock_price, gamma_info=None):
    """
    HARD CONTRACT LOCK.

    Only ONE request at a time can enter this function.
    This fixes the V7 race where two simultaneous TradingView webhooks could
    both see "no active trade" and each send a different Telegram contract.

    TradingView supplies direction only.
    Massive selects ONE option contract.
    That exact contract stays locked until STOP LOSS or all targets complete.
    """
    symbol = str(symbol or "").upper().strip()
    direction = str(direction or "").upper().strip()

    with TRADE_STATE_LOCK:
        # Re-read state AFTER acquiring the lock.
        store = _load_trade_store()
        trades = store.setdefault("trades", {})

        existing = trades.get(symbol)

        # Active OR opening means absolutely no new contract for this stock.
        if isinstance(existing, dict) and existing.get("status") in ("opening", "active"):
            print(
                f"🔒 HARD LOCK: duplicate ignored {symbol} {direction} | "
                f"keeping {existing.get('contract_ticker') or 'opening contract'}"
            )
            result = dict(existing)
            result["_ignored_duplicate"] = True
            return result

        # Reserve the symbol immediately, before Massive/Telegram network calls.
        # This prevents a second webhook thread from opening another trade.
        reservation = {
            "symbol": symbol,
            "direction": direction,
            "stock_price": stock_price,
            "contract_ticker": "",
            "entry_price": 0.0,
            "status": "opening",
            "message_id": None,
            "created_at": now_new_york().isoformat(timespec="seconds"),
        }
        trades[symbol] = reservation
        _save_trade_store(store)

        try:
            # Massive selects the option contract ONCE.
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
                "created_at": reservation["created_at"],
                "gamma_flip": round(safe_float((gamma_info or {}).get("flip")), 2) if safe_float((gamma_info or {}).get("flip")) > 0 else None,
                "gamma_stock_price": round(stock_price, 2),
                "gamma_regime": str((gamma_info or {}).get("regime") or "N/A"),
                "net_gex": round(safe_float((gamma_info or {}).get("current_gex")), 2),
            }

            # Save the selected contract BEFORE sending Telegram.
            # From this point every duplicate request sees the locked contract.
            trades[symbol] = state
            _save_trade_store(store)

            # Send exactly ONE Telegram card for this locked trade.
            message_id = telegram_send_card(state)
            state["message_id"] = message_id

            trades[symbol] = state
            _save_trade_store(store)

            print(
                f"🔒 HARD CONTRACT LOCKED: {symbol} {direction} | "
                f"{contract['ticker']} | option entry {entry:.2f}"
            )

            return state

        except Exception:
            # If opening fails, remove only our unfinished reservation so a
            # later genuine signal can try again.
            latest = _load_trade_store()
            latest_trades = latest.setdefault("trades", {})
            current = latest_trades.get(symbol)

            if isinstance(current, dict) and current.get("status") == "opening":
                latest_trades.pop(symbol, None)
                _save_trade_store(latest)

            raise


def _update_option_price_locked(option_ticker, option_price):
    """
    V10 LIVE CARD:
    The SAME Telegram card is edited on EVERY valid Massive option-price update.
    Therefore CURRENT PRICE / CHANGE / TARGET PROGRESS stay live, not only when
    a target or stop event occurs.
    """
    store = _load_trade_store()
    trades = store.get("trades", {})
    incoming = canonical_option_ticker(option_ticker)

    matched_symbol = None
    state = None

    for symbol, trade in trades.items():
        if not isinstance(trade, dict) or trade.get("status") != "active":
            continue
        expected = canonical_option_ticker(trade.get("contract_ticker", ""))
        if incoming == expected:
            matched_symbol = symbol
            state = trade
            break

    if state is None:
        return {
            "accepted": True,
            "ignored": "option contract is not an active locked trade",
            "received": incoming,
        }

    price = safe_float(option_price)
    if price <= 0:
        return {"accepted": False, "error": "invalid option price"}

    previous_achieved = int(state.get("achieved", 0))
    previous_high = safe_float(state.get("highest_price"))

    state["current_price"] = round(price, 2)
    state["highest_price"] = round(max(previous_high, price), 2)

    # STOP LOSS is based on the locked OPTION contract only.
    if price <= safe_float(state["stop_loss"]):
        state["status"] = "stopped"
        state["closed_at"] = now_new_york().isoformat(timespec="seconds")
        state["close_reason"] = "STOP_LOSS"

    else:
        achieved = previous_achieved
        highest = safe_float(state["highest_price"])

        while achieved < 5 and highest >= safe_float(state["targets"][achieved]):
            achieved += 1

        state["achieved"] = achieved

        if achieved >= 5:
            state["status"] = "completed"
            state["closed_at"] = now_new_york().isoformat(timespec="seconds")
            state["close_reason"] = "ALL_TARGETS"

    trades[matched_symbol] = state
    _save_trade_store(store)

    # IMPORTANT V10 FIX:
    # edit the SAME Telegram card on every valid option quote update.
    # This keeps current price, percentage change and target progress current.
    if state.get("message_id"):
        telegram_edit_card_retry(state["message_id"], state)

    if state["status"] == "stopped":
        print(f"🛑 STOP LOSS | {incoming} = {price:.2f} | {matched_symbol}")
    elif int(state.get("achieved", 0)) > previous_achieved:
        print(
            f"🎯 TARGET {state['achieved']} HIT | {incoming} | "
            f"option {price:.2f} | {matched_symbol}"
        )
    else:
        print(
            f"📍 LIVE OPTION UPDATE | {incoming} = {price:.2f} | "
            f"{matched_symbol} | targets {state.get('achieved', 0)}/5"
        )

    return {
        "accepted": True,
        "symbol": matched_symbol,
        "ticker": incoming,
        "price": price,
        "achieved": state.get("achieved", 0),
        "status": state["status"],
    }


def update_option_price(option_ticker, option_price):
    # Serialize option-state updates with new signal creation.
    with TRADE_STATE_LOCK:
        return _update_option_price_locked(option_ticker, option_price)


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
                "service": "ORCA WHALE OPTIONS BOT",
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

        if path == "/scan-now":
            try:
                result = scan_watchlist_once(force=bool(payload.get("force", False)))
                self.json_response(200, result)
            except Exception as error:
                self.json_response(500, {"error": str(error)})
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

                duplicate = bool(state.get("_ignored_duplicate"))

                self.json_response(202, {
                    "accepted": True,
                    "ignored_duplicate": duplicate,
                    "contract_locked": state.get("contract_ticker", ""),
                    "entry": state.get("entry_price", 0.0),
                    "status": state.get("status", "active"),
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

            if path == "/monitor-tick":
                result = monitor_active_trade_once()
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



# ============================================================
# ORCA AUTONOMOUS OPTIONS SCANNER
# Massive only: Delta / Volume / OI / Strike / Spread / Theta / IV / DTE
# ============================================================

def _median(values):
    vals = sorted(safe_float(v) for v in values if safe_float(v) > 0)
    if not vals:
        return 0.0
    n = len(vals)
    m = n // 2
    return vals[m] if n % 2 else (vals[m - 1] + vals[m]) / 2.0


def _raw_option_mark(raw):
    q = raw.get("last_quote", {}) or {}
    t = raw.get("last_trade", {}) or {}
    d = raw.get("day", {}) or {}
    midpoint = safe_float(q.get("midpoint"))
    if midpoint > 0:
        return midpoint
    bid = safe_float(q.get("bid") or q.get("bid_price"))
    ask = safe_float(q.get("ask") or q.get("ask_price"))
    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0
    if ask > 0:
        return ask
    if bid > 0:
        return bid
    last = safe_float(t.get("price"))
    if last > 0:
        return last
    close = safe_float(d.get("close"))
    return close if close > 0 else safe_float(d.get("open"))


def infer_underlying_price(call_raw, put_raw):
    direct = []
    for raw in list(call_raw or []) + list(put_raw or []):
        p = safe_float((raw.get("underlying_asset", {}) or {}).get("price"))
        if p > 0:
            direct.append(p)
    p = _median(direct)
    if p > 0:
        return p, "underlying_asset"

    inferred = []
    for raw in call_raw or []:
        be, premium = safe_float(raw.get("break_even_price")), _raw_option_mark(raw)
        if be > 0 and premium > 0:
            inferred.append(be - premium)
    for raw in put_raw or []:
        be, premium = safe_float(raw.get("break_even_price")), _raw_option_mark(raw)
        if be > 0 and premium > 0:
            inferred.append(be + premium)

    p = _median(inferred)
    return p, "break_even_inference" if p > 0 else "unavailable"


def _prepare_side(raw_chain, stock_price):
    contracts = []
    for item in raw_chain or []:
        c = normalize_contract(item, stock_price)
        if c["ticker"] and c["expiration"]:
            contracts.append(c)

    exp = nearest_expiration(contracts)
    if not exp:
        return [], None

    exp_text = exp.strftime("%Y-%m-%d")
    contracts = [c for c in contracts if c["expiration"] == exp_text]
    for c in contracts:
        c["score"] = calculate_score(c, stock_price)

    contracts.sort(key=lambda x: (-x["score"], x["distance"], -x["volume"], -x["open_interest"]))
    return contracts, exp_text


def _side_flow_stats(contracts):
    near = sorted(contracts, key=lambda x: (x["distance"], -x["volume"], -x["open_interest"]))[:12]
    return {
        "volume": sum(safe_float(c.get("volume")) for c in near),
        "open_interest": sum(safe_float(c.get("open_interest")) for c in near),
        "best": contracts[0] if contracts else None,
    }


def _direction_strength(side, other):
    best = side.get("best") or {}
    strength = safe_float(best.get("score"))
    sv, ov = safe_float(side.get("volume")), safe_float(other.get("volume"))
    soi, ooi = safe_float(side.get("open_interest")), safe_float(other.get("open_interest"))
    if sv >= ov * 1.15 + 25:
        strength += 3
    if sv >= ov * 1.50 + 50:
        strength += 3
    if soi >= ooi * 1.10 + 100:
        strength += 2
    if soi >= ooi * 1.35 + 250:
        strength += 2
    return round(strength, 2)


def market_is_open_now():
    now = now_new_york()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 570 <= mins < 960



def _normal_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_gamma(spot, strike, t_years, iv, rate):
    """Black-Scholes gamma at a hypothetical underlying price."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or iv <= 0:
        return 0.0
    try:
        sqrt_t = math.sqrt(t_years)
        d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t_years) / (iv * sqrt_t)
        return _normal_pdf(d1) / (spot * iv * sqrt_t)
    except (ValueError, OverflowError, ZeroDivisionError):
        return 0.0


def _contract_t_years(raw):
    details = raw.get("details", {}) or {}
    exp_text = str(details.get("expiration_date") or "")
    try:
        exp = datetime.strptime(exp_text, "%Y-%m-%d").date()
    except Exception:
        return 0.0

    now = now_new_york()
    days = (exp - now.date()).days

    # On 0DTE use remaining regular-session time instead of zero time.
    if days == 0:
        close_minutes = 16 * 60
        now_minutes = now.hour * 60 + now.minute + now.second / 60.0
        remaining_minutes = max(close_minutes - now_minutes, 5.0)
        return remaining_minutes / (365.0 * 24.0 * 60.0)

    return max(days, 0.25) / 365.0


def _gamma_position(raw, spot, side_sign):
    details = raw.get("details", {}) or {}
    strike = safe_float(details.get("strike_price"))
    oi = safe_float(raw.get("open_interest"))
    iv = safe_float(raw.get("implied_volatility"))
    t = _contract_t_years(raw)

    if strike <= 0 or oi <= 0 or iv <= 0 or t <= 0:
        return 0.0

    gamma = _bs_gamma(spot, strike, t, iv, GAMMA_RISK_FREE_RATE)

    # Common GEX convention:
    # calls positive, puts negative. This is an exposure model because public OI
    # does not reveal the dealer's true long/short inventory.
    return side_sign * gamma * oi * 100.0 * spot * spot * 0.01


def net_gamma_exposure_at_price(call_raw, put_raw, spot):
    call_gex = sum(_gamma_position(raw, spot, 1.0) for raw in call_raw or [])
    put_gex = sum(_gamma_position(raw, spot, -1.0) for raw in put_raw or [])
    return call_gex + put_gex


def calculate_gamma_flip(call_raw, put_raw, stock_price):
    """
    Reprice gamma across a spot grid and locate the nearest Net GEX zero crossing.
    Returns the interpolated Gamma Flip plus current Net GEX.
    """
    if stock_price <= 0:
        return None

    current_gex = net_gamma_exposure_at_price(call_raw, put_raw, stock_price)
    if not GAMMA_FLIP_ENABLED:
        return {
            "flip": None,
            "current_gex": current_gex,
            "regime": "POSITIVE" if current_gex > 0 else "NEGATIVE" if current_gex < 0 else "ZERO",
        }

    range_pct = max(2.0, min(GAMMA_FLIP_RANGE_PCT, 30.0)) / 100.0
    steps = max(41, min(GAMMA_FLIP_STEPS, 301))
    low = stock_price * (1.0 - range_pct)
    high = stock_price * (1.0 + range_pct)
    step = (high - low) / (steps - 1)

    points = []
    for i in range(steps):
        s = low + i * step
        points.append((s, net_gamma_exposure_at_price(call_raw, put_raw, s)))

    crossings = []
    for (s1, g1), (s2, g2) in zip(points, points[1:]):
        if g1 == 0:
            crossings.append(s1)
            continue
        if g1 * g2 < 0:
            # Linear interpolation between the two surrounding GEX points.
            flip = s1 + (0.0 - g1) * (s2 - s1) / (g2 - g1)
            crossings.append(flip)

    flip = min(crossings, key=lambda x: abs(x - stock_price)) if crossings else None
    regime = "POSITIVE" if current_gex > 0 else "NEGATIVE" if current_gex < 0 else "ZERO"

    return {
        "flip": flip,
        "current_gex": current_gex,
        "regime": regime,
        "crossings": crossings,
    }


def gamma_flip_allows(direction, stock_price, gamma_info):
    """
    Direction filter:
      CALL requires price clearly above Gamma Flip.
      PUT requires price clearly below Gamma Flip.
    If no reliable flip exists in the scan range, do not reject solely on Gamma Flip.
    """
    if not GAMMA_FLIP_ENABLED or not gamma_info:
        return True

    flip = gamma_info.get("flip")
    if not flip or flip <= 0:
        return True

    buffer_value = stock_price * max(GAMMA_FLIP_BUFFER_PCT, 0.0) / 100.0
    if direction == "CALL":
        return stock_price > flip + buffer_value
    if direction == "PUT":
        return stock_price < flip - buffer_value
    return False


def scan_symbol_options(symbol):
    symbol = str(symbol or "").upper().strip()

    with TRADE_STATE_LOCK:
        store = _load_trade_store()
        existing = (store.get("trades") or {}).get(symbol)
        has_active_trade = isinstance(existing, dict) and existing.get("status") in ("opening", "active")

    call_raw = massive_chain(symbol, "CALL")
    put_raw = massive_chain(symbol, "PUT")

    stock_price, stock_source = infer_underlying_price(call_raw, put_raw)
    if stock_price <= 0:
        return {"symbol": symbol, "accepted": False, "ignored": "underlying price unavailable"}

    if has_active_trade:
        gamma_info = calculate_gamma_flip(call_raw, put_raw, stock_price)

        with TRADE_STATE_LOCK:
            store = _load_trade_store()
            live = (store.get("trades") or {}).get(symbol)
            if isinstance(live, dict) and live.get("status") == "active":
                live["gamma_flip"] = round(safe_float(gamma_info.get("flip")), 2) if gamma_info and safe_float(gamma_info.get("flip")) > 0 else None
                live["gamma_stock_price"] = round(stock_price, 2)
                live["stock_price"] = round(stock_price, 2)
                live["gamma_regime"] = str((gamma_info or {}).get("regime") or "N/A")
                live["net_gex"] = round(safe_float((gamma_info or {}).get("current_gex")), 2)
                (store.get("trades") or {})[symbol] = live
                _save_trade_store(store)

                if live.get("message_id"):
                    try:
                        telegram_edit_card(live["message_id"], live)
                    except Exception as error:
                        print("Gamma card refresh warning:", symbol, error)

                flip = safe_float(live.get("gamma_flip"))
                side = "ABOVE" if flip > 0 and stock_price > flip else "BELOW" if flip > 0 and stock_price < flip else "NO-FLIP"
                warning = (
                    (live.get("direction") == "CALL" and side == "BELOW") or
                    (live.get("direction") == "PUT" and side == "ABOVE")
                )
                return {
                    "symbol": symbol,
                    "accepted": True,
                    "ignored": "active contract already locked",
                    "gamma_flip": live.get("gamma_flip"),
                    "stock_price": round(stock_price, 2),
                    "price_vs_gamma_flip": side,
                    "exit_warning": warning,
                }

        return {"symbol": symbol, "accepted": True, "ignored": "contract opening/locked"}

    calls, call_exp = _prepare_side(call_raw, stock_price)
    puts, put_exp = _prepare_side(put_raw, stock_price)
    if not calls or not puts:
        return {"symbol": symbol, "accepted": False, "ignored": "no usable nearest-expiry contracts"}

    cs, ps = _side_flow_stats(calls), _side_flow_stats(puts)
    call_strength, put_strength = _direction_strength(cs, ps), _direction_strength(ps, cs)
    cb, pb = cs["best"], ps["best"]

    gamma_info = calculate_gamma_flip(call_raw, put_raw, stock_price)

    direction = None
    if (
        safe_float(cb.get("score")) >= AUTO_SCAN_MIN_SCORE
        and call_strength >= put_strength + AUTO_SCAN_MIN_EDGE
        and safe_float(cs.get("volume")) >= AUTO_SCAN_MIN_VOLUME
    ):
        direction = "CALL"
        if not gamma_flip_allows(direction, stock_price, gamma_info):
            direction = None
    elif (
        safe_float(pb.get("score")) >= AUTO_SCAN_MIN_SCORE
        and put_strength >= call_strength + AUTO_SCAN_MIN_EDGE
        and safe_float(ps.get("volume")) >= AUTO_SCAN_MIN_VOLUME
    ):
        direction = "PUT"
        if not gamma_flip_allows(direction, stock_price, gamma_info):
            direction = None

    result = {
        "symbol": symbol,
        "accepted": True,
        "stock_price": round(stock_price, 2),
        "stock_price_source": stock_source,
        "call_score": safe_float(cb.get("score")),
        "put_score": safe_float(pb.get("score")),
        "call_strength": call_strength,
        "put_strength": put_strength,
        "call_volume": round(safe_float(cs.get("volume")), 0),
        "put_volume": round(safe_float(ps.get("volume")), 0),
        "call_oi": round(safe_float(cs.get("open_interest")), 0),
        "put_oi": round(safe_float(ps.get("open_interest")), 0),
        "call_expiration": call_exp,
        "put_expiration": put_exp,
        "gamma_flip": round(gamma_info["flip"], 2) if gamma_info and gamma_info.get("flip") else None,
        "net_gex": round(safe_float(gamma_info.get("current_gex")), 2) if gamma_info else None,
        "gamma_regime": gamma_info.get("regime") if gamma_info else None,
        "price_vs_gamma_flip": (
            "ABOVE" if gamma_info and gamma_info.get("flip") and stock_price > gamma_info["flip"]
            else "BELOW" if gamma_info and gamma_info.get("flip") and stock_price < gamma_info["flip"]
            else "AT/NO-FLIP"
        ),
    }

    if not direction:
        call_raw_ok = (
            safe_float(cb.get("score")) >= AUTO_SCAN_MIN_SCORE
            and call_strength >= put_strength + AUTO_SCAN_MIN_EDGE
            and safe_float(cs.get("volume")) >= AUTO_SCAN_MIN_VOLUME
        )
        put_raw_ok = (
            safe_float(pb.get("score")) >= AUTO_SCAN_MIN_SCORE
            and put_strength >= call_strength + AUTO_SCAN_MIN_EDGE
            and safe_float(ps.get("volume")) >= AUTO_SCAN_MIN_VOLUME
        )

        if call_raw_ok and not gamma_flip_allows("CALL", stock_price, gamma_info):
            result["ignored"] = "CALL rejected by Gamma Flip"
        elif put_raw_ok and not gamma_flip_allows("PUT", stock_price, gamma_info):
            result["ignored"] = "PUT rejected by Gamma Flip"
        else:
            result["ignored"] = "no strong options-only directional edge"
        return result

    state = create_trade(symbol, direction, stock_price, gamma_info=gamma_info)
    result["direction"] = direction
    result["contract"] = state.get("contract_ticker")
    result["message_id"] = state.get("message_id")
    return result


def scan_watchlist_once(force=False):
    if not AUTO_SCAN_ENABLED:
        return {"accepted": True, "ignored": "auto scanner disabled"}
    if not force and not market_is_open_now():
        return {"accepted": True, "ignored": "US regular market is closed"}

    results = []
    for symbol in WATCHLIST:
        try:
            item = scan_symbol_options(symbol)
        except Exception as error:
            item = {"symbol": symbol, "accepted": False, "error": str(error)}
        results.append(item)
        print("🐋 ORCA SCAN:", item)
        time.sleep(0.35)

    return {"accepted": True, "checked": len(WATCHLIST), "results": results}


def auto_scan_loop():
    print(f"ORCA autonomous scanner every {AUTO_SCAN_SECONDS}s")
    time.sleep(8)
    while True:
        try:
            if market_is_open_now():
                scan_watchlist_once(False)
        except Exception as error:
            print("ORCA auto scanner warning:", error)
        time.sleep(max(AUTO_SCAN_SECONDS, 60))


def start_auto_scanner():
    if not AUTO_SCAN_ENABLED:
        return None
    t = threading.Thread(target=auto_scan_loop, name="orca-options-scanner", daemon=True)
    t.start()
    return t


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
    print("ORCA WHALE OPTIONS BOT - AUTONOMOUS SCANNER")
    print("=" * 60)
    print(f"Local URL: http://127.0.0.1:{PORT}")
    print(f"Health:    http://127.0.0.1:{PORT}/health")
    print(f"Signal:    POST /signal")
    print(f"Option:    POST /option-price")
    print(f"Monitor:   POST /monitor-tick")
    print()
    print("✅ ORCA جاهز: ماسح عقود مستقل + Massive لاختيار ومتابعة العقد")
    print("لإيقافه: Ctrl + C")
    print()

    start_auto_monitor()
    start_auto_scanner()

    server = ThreadingHTTPServer((HOST, PORT), Handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⛔ تم الإيقاف.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
