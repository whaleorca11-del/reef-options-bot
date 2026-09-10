---
name: Option entry freshness
description: The safety rule for opening option trades when market quotes are delayed or missing.
---

Trade entries require a fresh option bid/ask midpoint. Delayed session/day closes may support diagnostics or underlying-price inference, but must not become the live entry price.

**Why:** Delayed option closes produced suspicious-looking entry cards and can make an otherwise strong contract appear tradable when its current executable price is unknown.

**How to apply:** Preserve delayed data as a fallback for analysis only; block contract locking and Telegram signal creation until a fresh option quote is available.