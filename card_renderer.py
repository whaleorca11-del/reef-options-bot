"""Reference-matched ORCA card. Presentation only; never mutates trade state."""
from datetime import date
from pathlib import Path
from PIL import Image, ImageChops


ASSETS = Path(__file__).resolve().parent / "assets" / "card"
CARD_STYLE = "white_reference"


def render_card(state, *, bot, gamma_values, is_tracking, paste_logo, signal_type,
                monitor_enabled):
    # White reference proportions: 871 × 1024, Telegram-friendly resolution.
    scale = 1080 / 871
    image = bot.Image.new("RGB", (1080, round(1024 * scale)), "#FFFFFF")
    draw = bot.ImageDraw.Draw(image)
    white, ink, muted = "#FFFFFF", "#001338", "#001338"
    cyan, green, red, yellow = "#075BCD", "#008B48", "#DD0016", "#FFCB00"
    border, panel = "#CADDEC", "#EFF7FD"

    def coords(values):
        return tuple(round(v * scale) for v in values)

    def box(bounds, fill=panel, outline=border, radius=11):
        draw.rounded_rectangle(coords(bounds), radius=round(radius * scale),
                               fill=fill, outline=outline, width=round(scale))

    def line(points, color=border):
        draw.line(coords(points), fill=color, width=round(scale))

    def text(x, y, value, size=18, color=ink, bold=False, width=None, align="left"):
        value = str(value)
        font_size = round(size * scale)
        font = bot._font(font_size, bold)
        while width is not None and font_size > round(8 * scale):
            bounds = draw.textbbox((0, 0), value, font=font)
            if bounds[2] - bounds[0] <= width * scale:
                break
            font_size -= 1
            font = bot._font(font_size, bold)
        bounds = draw.textbbox((0, 0), value, font=font)
        px = x * scale
        if align == "center":
            px -= (bounds[2] - bounds[0]) / 2
        elif align == "right":
            px -= bounds[2] - bounds[0]
        # Align by visible glyph top, rather than varying font ascender offsets.
        draw.text((round(px), round(y * scale) - bounds[1]), value, font=font, fill=color)

    def mark(name, bounds, tint=None):
        with bot.Image.open(ASSETS / name) as source:
            source = source.convert("RGBA")
            if tint:
                alpha = source.getchannel("A")
                source = bot.Image.new("RGBA", source.size, tint)
                source.putalpha(alpha)
            x1, y1, x2, y2 = coords(bounds)
            source.thumbnail((x2 - x1, y2 - y1), bot.Image.Resampling.LANCZOS)
            image.paste(source, (x1 + (x2 - x1 - source.width) // 2,
                                y1 + (y2 - y1 - source.height) // 2), source)

    def money(value, decimals=2):
        return f"${bot.safe_float(value):.{decimals}f}"

    symbol = str(state["symbol"]).upper()
    direction = str(state["direction"]).upper()
    status = str(state.get("status", "active")).lower()
    tracking = is_tracking(state)
    achieved = max(0, min(5, int(state.get("achieved", 0))))
    accent = green if direction == "CALL" else red
    contract = bot.canonical_option_ticker(state["contract_ticker"])
    expiry = date.fromisoformat(state["expiration"])
    expiration = f"{expiry.strftime('%b').upper()} {expiry.day}, {expiry.year}"
    dte = bot._days_to_expiry(state["expiration"])

    # Brand and score.
    mark("orca-mark.png", (89, 16, 180, 83), tint=ink)
    text(189, 31, "ORCA", 43, bold=True)
    line((342, 30, 342, 68), ink)
    text(358, 38, "OPTIONS SIGNAL", 25, cyan, True, 225)
    box((594, 18, 803, 70), radius=9)
    text(609, 38, "TEST" if status == "test" else "SCORE", 18, ink, True)
    text(790, 34, f"{state['score']}/100", 27, cyan, True, 129, "right")

    # Company and direction.
    box((89, 94, 226, 222), radius=12)
    if symbol == "AAPL":
        mark("apple-mark.png", (113, 106, 204, 211), tint=ink)
    elif symbol == "NVDA":
        mark("nvidia-mark.png", (94, 99, 221, 217))
    else:
        paste_logo(image, symbol, coords((95, 100, 220, 216)))
    text(240, 95, symbol, 86, bold=True, width=266)
    text(240, 177, contract, 45, accent, True, 563)
    box((526, 94, 740, 163), fill=accent,
        outline=accent, radius=13)
    text(633, 108, f"{direction} {'↗' if direction == 'CALL' else '↘'}",
         53, white, True, 191, "center")

    # Exact contract identifiers remain separate from the underlying price.
    box((89, 232, 357, 286))
    text(111, 251, "STRIKE", 20, muted, True)
    text(340, 245, money(state["strike"]), 29, bold=True, width=150, align="right")
    box((366, 232, 651, 286))
    text(387, 251, "EXP", 20, muted, True)
    text(635, 248, expiration, 27, bold=True, width=188, align="right")
    box((661, 232, 803, 286))
    text(732, 249, f"{dte} DTE", 28, bold=True, width=124, align="center")
    box((89, 297, 803, 352))
    text(111, 314, "Underlying", 24, muted, True)
    text(263, 310, money(state["stock_price"]), 36, bold=True, width=235)
    if status in ("stopped", "completed"):
        text(779, 318, status.upper(), 16, red if status == "stopped" else cyan,
             True, 200, "right")

    # Entry and stop are sourced from the existing trade, not reference values.
    midpoint = bot.safe_float(state.get("midpoint"))
    entry = midpoint if midpoint > 0 else bot.safe_float(state["entry_price"])
    box((89, 363, 480, 550), fill="#EEFBF4", outline="#30B777")
    text(111, 377, "ENTRY MID" if midpoint > 0 else "ENTRY PRICE", 34, green, True)
    text(128, 415, money(entry, 3), 83, bold=True, width=330)
    line((113, 493, 458, 493), "#99B3CE")
    text(115, 511, "BID", 22, muted, True)
    bid, ask = bot.safe_float(state.get("bid")), bot.safe_float(state.get("ask"))
    valid_quote = bid > 0 and ask >= bid
    text(181, 508, money(bid) if valid_quote else "N/A", 30, bold=True, width=92)
    text(285, 511, "/", 24, muted)
    text(307, 511, "ASK", 22, muted, True)
    text(457, 508, money(ask) if valid_quote else "N/A", 30, bold=True,
         width=92, align="right")
    box((491, 363, 803, 550), fill="#FFF0F2", outline="#E6405A")
    text(511, 377, "STOP • MID", 32, red, True)
    stop = bot.safe_float(state["stop_loss"])
    text(509, 415, money(stop), 85, bold=True, width=270)
    stop_percent = round((1 - stop / entry) * 100) if entry > 0 else None
    text(647, 494, f"−{stop_percent}%" if stop_percent is not None else "N/A",
         51, red, True, 270, "center")

    # Monitoring is never implied when disabled or no update has been received.
    box((89, 560, 803, 613))
    draw.ellipse(coords((114, 574, 139, 599)), fill=green if tracking else yellow)
    update_label = "PRICE UPDATES ON" if tracking else "PRICE UPDATES WAITING" if monitor_enabled else "PRICE UPDATES OFF"
    text(154, 578, update_label, 23, green if tracking else ink, True, 355)
    line((526, 574, 526, 602), "#99B3CE")
    text(776, 576 if tracking else 581,
         f"Targets {achieved}/5" if tracking else "Targets not tracked",
         22, muted, width=232, align="right")
    if tracking:
        current = bot.safe_float(state.get("current_price"), entry)
        change = (current / entry - 1) * 100 if entry else 0
        text(776, 598, f"Mark {money(current)} ({change:+.1f}%)", 11,
             green if change >= 0 else red, width=232, align="right")

    # Five targets in one row, using the trade's locked prices and progress.
    box((89, 623, 803, 837))
    text(111, 635, "TARGETS FROM ENTRY MID", 32, ink, True, 670)
    targets = list(state["targets"])
    percentages = (20, 45, 70, 100, 150)
    for i in range(5):
        x = 89 + i * 142.8
        if i:
            line((x - 1, 683, x - 1, 828))
        hit = i < achieved
        draw.ellipse(coords((x + 31, 687, x + 58, 714)), outline=green if hit else cyan,
                     fill="#DDF5E8" if hit else panel, width=round(2.5 * scale))
        if hit:
            text(x + 45, 692, "✓", 18, green, True, align="center")
        text(x + 78, 691, f"T{i + 1}", 22, muted, True)
        text(x + 71, 739, money(targets[i]) if i < len(targets) else "N/A",
             40, bold=True, width=126, align="center")
        text(x + 71, 790, f"+{percentages[i]}%", 35, green, True, 126, "center")

    # Keep contract metrics only; Gamma has no visible section or placeholder.
    box((89, 847, 803, 927))
    line((445, 862, 445, 916), "#99B3CE")
    text(111, 858, "DELTA", 18, muted)
    delta = state.get("delta")
    text(111, 882, "N/A" if delta in (None, "") else f"{bot.safe_float(delta):+.2f}", 44, bold=True)
    text(480, 858, "IMPLIED VOLATILITY", 18, muted)
    iv = state.get("iv")
    iv_value = bot.safe_float(iv)
    text(480, 882, "N/A" if iv in (None, "") else f"{iv_value if iv_value > 3 else iv_value * 100:.1f}%",
         44, bold=True)

    # The requested layout retains the quote time and a small strategy audit line.
    quote_time = state.get("last_successful_option_price_update") if tracking else state.get("entry_quote_timestamp") or state.get("created_at")
    text(92, 942, f"{'Quote update' if tracking else 'Quote snapshot'} {bot.format_entry_time_et(quote_time)}",
         19, muted, width=460)
    text(800, 943, "ORCA WHALE OPTIONS", 22, cyan, True, 254, "right")
    entry_type = state.get("entry_signal_type")
    if entry_type == signal_type:
        audit = f"120/180/40 | 1h | {'GREEN | ABOVE' if direction == 'CALL' else 'RED | BELOW'} EMA200"
        text(446, 982, audit, 10, muted, width=700, align="center")
    elif entry_type == "MACD_5M":
        text(446, 982, f"120/180/40 | 5m | {'GREEN' if direction == 'CALL' else 'RED'}",
             10, muted, width=700, align="center")

    # Telegram crops oversized canvases in previews. Trim the unused white
    # border while keeping a small, even white inset around every visible mark.
    white_background = Image.new("RGB", image.size, "#FFFFFF")
    content_box = ImageChops.difference(image, white_background).getbbox()
    if content_box:
        padding = round(8 * scale)
        left, top, right, bottom = content_box
        image = image.crop((
            max(0, left - padding),
            max(0, top - padding),
            min(image.width, right + padding),
            min(image.height, bottom + padding),
        ))

    output = Path(bot.__file__).with_name("reef_trade_card.png")
    image.save(output, "PNG", optimize=True)
    return output
