"""Constants for the Kilowattlas Solar Sharing integration."""

from __future__ import annotations

import os

DOMAIN = "kilowattlas"

# Base URL of the Kilowattlas contrib API. Home Assistant talks to the API
# subdomain DIRECTLY (Traefik -> backend), not the website's /api proxy, so the
# device-flow and ingest routes registered outside the /api/v1 auth group are
# reachable without the shared website API key.
#
# End users never choose this — it always points at production. A developer can
# override it for local testing by setting the KILOWATTLAS_API_BASE environment
# variable on the Home Assistant process (e.g. KILOWATTLAS_API_BASE=http://backend:8050),
# mirroring how the backend uses CONTRIB_VERIFICATION_URI in dev vs prod.
PROD_API_BASE = "https://api.kilowattlas.com"


def resolve_api_base() -> str:
    """Return the API base: the dev env override if set, else production."""
    return os.getenv("KILOWATTLAS_API_BASE", "").strip() or PROD_API_BASE


# Config entry / options keys.
CONF_API_BASE = "api_base"
CONF_TOKEN = "token"
CONF_SITE_ID = "site_id"
CONF_POWER_SENSOR = "power_sensor"

# Endpoints (relative to API base).
EP_DEVICE_CODE = "/api/v1/contrib/device/code"
EP_DEVICE_TOKEN = "/api/v1/contrib/device/token"
EP_INGEST = "/api/v1/contrib/solar"

# Sampling + push cadence. Overridable via env vars for local testing so a
# developer can watch data land in seconds instead of waiting a full slot/hour.
# Production defaults: 15-min slots, hourly batched push.
SAMPLE_INTERVAL_SECONDS = int(os.getenv("KILOWATTLAS_SAMPLE_SECONDS", "10"))
SLOT_SECONDS = int(os.getenv("KILOWATTLAS_SLOT_SECONDS", str(15 * 60)))
PUSH_INTERVAL_SECONDS = int(os.getenv("KILOWATTLAS_PUSH_SECONDS", str(60 * 60)))
MAX_BATCH = 500  # server cap; keep buffered points bounded

# Storage keys (HA Store) for the offline buffer.
STORAGE_VERSION = 1
STORAGE_KEY = "kilowattlas_buffer"


def capability_tier(sample_interval_seconds: int) -> str:
    """Map a local sample interval to a Kilowattlas donor capability tier.

    Phase-2 (low-latency TSO stream) needs 1 s-capable donors; this classifies
    what a donor could deliver based on how fast it can be sampled locally.
    Cloud-API sensors are effectively >= a few minutes; local inverter/meter
    sensors can reach 1 s.
    """
    if sample_interval_seconds <= 5:
        return "realtime_1s"
    if sample_interval_seconds < 60:
        return "fast_subminute"
    return "batch_15min"
