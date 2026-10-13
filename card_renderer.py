"""Reference-matched ORCA card. Presentation only; never mutates trade state."""
from datetime import date
from pathlib import Path


ASSETS = Path(__file__).resolve().parent / "assets" / "card"


def render_card(state, *, bot, gamma_values, is_tracking, paste_logo, signal_type,
                monitor_enabled):
    # Reference proportions: 683 × 1024, rendered at Telegram-friendly resolution.
    scale = 1080 / 683
    image = bot.Image.new("RGB", (1080, 1620), "#00121C")
    draw = bot.ImageDraw.Draw(image)
    white, muted = "#FAFCFF", "#B1C9E3"
    cyan, green, red, yellow = "#4BD4F5", "#36ED94", "#FF596D", "#FFD15B"
    border, panel = "#284C60", "#001621"

    def coords(values):
        return tuple(round(v * scale) for v in values)

    def box(bounds, fill=panel, outline=border, radius=11):
        draw.rounded_rectangle(coords(bounds), radius=round(radius * scale),
                               fill=fill, outline=outline, width=round(scale))

    def line(points, color=border):
        draw.line(coords(points), fill=color, width=round(scale))

    def text(x, y, value, size=18, color=white, bold=False, width=None, align="left"):
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

    def mark(name, bounds):
        with bot.Image.open(ASSETS / name) as source:
            source = source.convert("RGBA")
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
    mark("orca-mark.png", (20, 18, 93, 72))
    text(102, 30, "ORCA", 35, bold=True)
    line((236, 30, 236, 62), muted)
    text(252, 38, "OPTIONS SIGNAL", 20, cyan, True, 225)
    box((488, 25, 661, 68), outline="#28627D", radius=9)
    text(501, 41, "TEST" if status == "test" else "SCORE", 15,
         yellow if status == "test" else muted, True)
    text(649, 38, f"{state['score']}/100", 22, cyan, True, 92, "right")

    # Company and direction.
    box((25, 91, 135, 198), outline="#3B657B", radius=16)
    if symbol == "AAPL":
        mark("apple-mark.png", (46, 106, 115, 182))
    else:
        paste_logo(image, symbol, coords((32, 98, 128, 191)))
    text(160, 87, symbol, 63, bold=True, width=197)
    text(160, 156, contract, 43, bold=True, width=499)
    box((365, 86, 547, 142), fill="#062D27" if direction == "CALL" else "#2C1420",
        outline=accent, radius=13)
    text(456, 98, f"{direction} {'↗' if direction == 'CALL' else '↘'}",
         44, accent, True, 169, "center")

    # Exact contract identifiers remain separate from the underlying price.
    box((25, 212, 252, 264))
    text(44, 232, "STRIKE", 17, muted, True)
    text(234, 228, money(state["strike"]), 24, bold=True, width=128, align="right")
    box((266, 212, 524, 264))
    text(284, 232, "EXP", 17, muted, True)
    text(510, 229, expiration, 21, bold=True, width=181, align="right")
    box((537, 212, 659, 264))
    text(598, 229, f"{dte} DTE", 23, bold=True, width=106, align="center")
    text(25, 288, "Underlying", 18, muted, True)
    text(144, 283, money(state["stock_price"]), 27, bold=True, width=132)
    if status in ("stopped", "completed"):
        text(657, 291, status.upper(), 14, red if status == "stopped" else cyan,
             True, 200, "right")

    # Entry and stop are sourced from the existing trade, not reference values.
    midpoint = bot.safe_float(state.get("midpoint"))
    entry = midpoint if midpoint > 0 else bot.safe_float(state["entry_price"])
    box((24, 332, 382, 511), outline=cyan)
    text(45, 352, "ENTRY MID" if midpoint > 0 else "ENTRY PRICE", 26, cyan, True)
    text(45, 391, money(entry, 3), 65, bold=True, width=316)
    line((45, 457, 361, 457), "#376073")
    text(45, 475, "BID", 20, muted, True)
    bid, ask = bot.safe_float(state.get("bid")), bot.safe_float(state.get("ask"))
    valid_quote = bid > 0 and ask >= bid
    text(100, 473, money(bid) if valid_quote else "N/A", 24, bold=True, width=82)
    text(194, 473, "/", 23, muted)
    text(226, 475, "ASK", 20, muted, True)
    text(359, 473, money(ask) if valid_quote else "N/A", 24, bold=True,
         width=82, align="right")
    box((395, 332, 659, 511), fill="#150F17", outline="#DE405A")
    text(416, 352, "STOP • MID", 26, red, True)
    stop = bot.safe_float(state["stop_loss"])
    text(416, 391, money(stop), 63, bold=True, width=223)
    stop_percent = round((1 - stop / entry) * 100) if entry > 0 else None
    text(416, 463, f"−{stop_percent}%" if stop_percent is not None else "N/A", 40, red, True)

    # Monitoring is never implied when disabled or no update has been received.
    box((25, 529, 659, 583))
    draw.ellipse(coords((44, 547, 63, 566)), fill=green if tracking else yellow)
    update_label = "PRICE UPDATES ON" if tracking else "PRICE UPDATES WAITING" if monitor_enabled else "PRICE UPDATES OFF"
    text(84, 548, update_label, 21, green if tracking else yellow, True, 335)
    line((440, 546, 440, 568))
    text(642, 550, f"Targets {achieved}/5" if tracking else "Targets not tracked",
         17, muted, width=192, align="right")
    if tracking:
        current = bot.safe_float(state.get("current_price"), entry)
        change = (current / entry - 1) * 100 if entry else 0
        text(641, 567, f"Mark {money(current)} ({change:+.1f}%)", 11,
             green if change >= 0 else red, width=197, align="right")

    # Five targets in one row, using the trade's locked prices and progress.
    box((24, 600, 659, 767))
    text(44, 615, "TARGETS FROM ENTRY MID", 26, cyan, True, 588)
    targets = list(state["targets"])
    percentages = (20, 45, 70, 100, 150)
    for i in range(5):
        x = 24 + i * 127
        if i:
            line((x - 3, 656, x - 3, 751))
        hit = i < achieved
        draw.ellipse(coords((x + 23, 657, x + 49, 683)), outline=green if hit else cyan,
                     fill="#084533" if hit else panel, width=round(1.7 * scale))
        if hit:
            text(x + 36, 662, "✓", 17, green, True, align="center")
        text(x + 65, 663, f"T{i + 1}", 17, muted)
        text(x + 62, 695, money(targets[i]) if i < len(targets) else "N/A",
             27, bold=True, width=113, align="center")
        text(x + 62, 731, f"+{percentages[i]}%", 23, green, True, 110, "center")

    # Gamma is informational; absent values remain explicitly unavailable.
    box((24, 783, 659, 940))
    line((342, 798, 342, 923))
    text(46, 807, "DELTA", 14, muted)
    delta = state.get("delta")
    text(46, 827, "N/A" if delta in (None, "") else f"{bot.safe_float(delta):+.2f}", 24, bold=True)
    text(46, 879, "IMPLIED VOLATILITY", 14, muted)
    iv = state.get("iv")
    iv_value = bot.safe_float(iv)
    text(46, 900, "N/A" if iv in (None, "") else f"{iv_value if iv_value > 3 else iv_value * 100:.1f}%",
         24, bold=True)
    flip, gamma_status = gamma_values(state)
    text(380, 807, "MODELED GAMMA FLIP", 14, muted)
    text(380, 827, flip if flip is not None else "N/A", 24, bold=True)
    text(380, 879, "GAMMA STATUS", 14, muted)
    text(380, 900, gamma_status if gamma_status else "UNAVAILABLE", 20,
         bold=True, width=263)

    # The requested layout retains the quote time and a small strategy audit line.
    line((24, 959, 659, 959))
    quote_time = state.get("last_successful_option_price_update") if tracking else state.get("entry_quote_timestamp") or state.get("created_at")
    text(25, 979, f"{'Quote update' if tracking else 'Quote snapshot'} {bot.format_entry_time_et(quote_time)}",
         15, muted, width=391)
    text(657, 979, "ORCA WHALE OPTIONS", 17, cyan, True, 234, "right")
    entry_type = state.get("entry_signal_type")
    if entry_type == signal_type:
        audit = f"120/180/40 | 1h | {'GREEN | ABOVE' if direction == 'CALL' else 'RED | BELOW'} EMA200"
        text(341, 1007, audit, 10, muted, width=630, align="center")
    elif entry_type == "MACD_5M":
        text(341, 1007, f"120/180/40 | 5m | {'GREEN' if direction == 'CALL' else 'RED'}",
             10, muted, width=630, align="center")

    output = Path(bot.__file__).with_name("reef_trade_card.png")
    image.save(output, "PNG", optimize=True)
    return output
