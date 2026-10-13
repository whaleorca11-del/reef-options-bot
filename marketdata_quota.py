"""Account credit windows and safe retry scheduling, without market-data caching."""

import json
import math
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
RIYADH = ZoneInfo("Asia/Riyadh")


def next_daily_reset(now):
    local = datetime.fromtimestamp(now, NEW_YORK)
    reset = local.replace(hour=9, minute=30, second=0, microsecond=0)
    if reset <= local:
        reset += timedelta(days=1)
    return reset.timestamp()


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _header_reset(fields, now):
    value = _number(fields.get("x-api-ratelimit-reset"))
    # Daily and per-minute plans should not supply a years-away timestamp.
    return value if value is not None and now < value <= now + 172800 else None


class MarketDataQuota:
    def __init__(self, clock=None):
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self.reason = None
        self.reset_at = None
        self.limit = None
        self.remaining = None
        self.requests = 0
        self.credits = 0
        self.credit_header_responses = 0
        self.chain_cache_hits = 0
        self.requests_by_endpoint = {}
        self.last_retry_released_at = None
        self.storage_path = None
        self.storage_error = None

    def configure_storage(self, path):
        """Preserve only the credit pause across restarts, never credentials."""
        with self.lock:
            self.storage_path = Path(path)
            if not self.storage_path.exists():
                return
            try:
                data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                reset_at = _number(data.get("reset_at"))
                if data.get("blocked"):
                    if reset_at is None or reset_at > self.clock() + 172800:
                        raise ValueError("Invalid saved reset time")
                    if reset_at > self.clock():
                        self.reason = "MarketData account API credit limit is exhausted"
                        self.reset_at = reset_at
                        self.limit = _number(data.get("limit"))
                        self.remaining = _number(data.get("remaining"))
            except (OSError, ValueError, AttributeError):
                self.storage_error = "Cannot read saved MarketData quota pause"
                # Do not silently overwrite an unreadable file or flood the API.
                self.storage_path = None
                self.reason = self.storage_error
                self.reset_at = next_daily_reset(self.clock())
                print(self.storage_error)

    def _save(self):
        if self.storage_path is None:
            return
        temp = self.storage_path.with_suffix(".tmp")
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "blocked": bool(self.reason),
                "reset_at": self.reset_at,
                "limit": self.limit,
                "remaining": self.remaining,
            }
            temp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temp, self.storage_path)
            self.storage_error = None
        except OSError:
            self.storage_error = "Cannot persist MarketData quota pause"
            print(self.storage_error)

    def _release_due(self):
        now = self.clock()
        if self.reset_at is not None and now >= self.reset_at:
            was_blocked = bool(self.reason)
            self.reason = None
            self.reset_at = None
            self.remaining = None
            if was_blocked:
                self.last_retry_released_at = now
            self._save()
            if was_blocked:
                print("MarketData credit window elapsed; next scheduled request will verify quota")

    def blocked_reason(self):
        with self.lock:
            self._release_due()
            return self.reason

    def block(self, reason, headers=None):
        with self.lock:
            now = self.clock()
            fields = {str(k).lower(): v for k, v in (headers or {}).items()}
            reset = _header_reset(fields, now)
            self.reason = str(reason)
            self.reset_at = (
                reset if reset is not None and reset > now else next_daily_reset(now)
            )
            self._save()
            return self.reason

    def note_request(self, endpoint):
        with self.lock:
            self.requests += 1
            self.requests_by_endpoint[endpoint] = (
                self.requests_by_endpoint.get(endpoint, 0) + 1
            )

    def note_chain_cache_hit(self):
        with self.lock:
            self.chain_cache_hits += 1

    def observe(self, headers):
        with self.lock:
            fields = {str(k).lower(): v for k, v in (headers or {}).items()}
            limit = _number(fields.get("x-api-ratelimit-limit"))
            remaining = _number(fields.get("x-api-ratelimit-remaining"))
            consumed = _number(fields.get("x-api-ratelimit-consumed"))
            reset = _header_reset(fields, self.clock())
            if limit is not None:
                self.limit = limit
            if remaining is not None:
                self.remaining = remaining
            if consumed is not None and consumed >= 0:
                self.credits += consumed
                self.credit_header_responses += 1
            if reset is not None and reset > self.clock():
                self.reset_at = reset
            if remaining is not None and remaining <= 0:
                self.block("MarketData account API credit limit is exhausted", headers)

    def snapshot(self):
        with self.lock:
            self._release_due()
            return {
                "blocked": bool(self.reason),
                "block_reason": self.reason,
                "limit": self.limit,
                "remaining": self.remaining,
                "reset_at_epoch": self.reset_at,
                "next_reset_riyadh": (
                    datetime.fromtimestamp(self.reset_at, RIYADH).isoformat()
                    if self.reset_at is not None else None
                ),
                "reset_source": "provider header when available; otherwise 09:30 New York",
                "automatic_retry": True,
                "requests_since_start": self.requests,
                "credits_consumed_since_start": (
                    self.credits if self.credit_header_responses else None
                ),
                "credit_header_responses": self.credit_header_responses,
                "chain_cache_hits_since_start": self.chain_cache_hits,
                "requests_by_endpoint": dict(self.requests_by_endpoint),
                "last_retry_released_at": self.last_retry_released_at,
                "storage_error": self.storage_error,
            }
