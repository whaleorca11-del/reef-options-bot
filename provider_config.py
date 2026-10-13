"""Allowed market-data providers; retired providers cannot be reactivated."""

REMOVED_PROVIDERS = frozenset({"massive", "polygon", "polygon.io"})
PROVIDER_KEYS = {
    "marketdata": "MARKETDATA_API_KEY",
    "tradier": "TRADIER_TOKEN",
    "thetadata": "THETADATA_API_KEY",
}
_ALIASES = {
    "marketdata": "marketdata",
    "marketdata_app": "marketdata",
    "marketdata_live": "marketdata",
    "tradier": "tradier",
    "tradier_live": "tradier",
    "thetadata": "thetadata",
    "theta": "thetadata",
    "thetadata_live": "thetadata",
}


def resolve_data_provider(value=None):
    requested = str(value or "").strip().lower()
    if not requested or requested in REMOVED_PROVIDERS:
        return "marketdata"
    if requested not in _ALIASES:
        raise ValueError(f"Unsupported data provider: {requested}")
    return _ALIASES[requested]
