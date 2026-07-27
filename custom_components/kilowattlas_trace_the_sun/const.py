"""Constants for the Kilowattlas Solar Sharing integration."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

_LOGGER = logging.getLogger(__name__)

DOMAIN = "kilowattlas_trace_the_sun"

# Base URL of the Kilowattlas contrib API. Home Assistant talks to the API
# subdomain DIRECTLY (Traefik -> backend), not the website's /api proxy, so the
# device-flow and ingest routes registered outside the /api/v1 auth group are
# reachable without the shared website API key.
#
# End users never choose this — it always points at production. A developer can
# override it for local testing by setting the KILOWATTLAS_API_BASE environment
# variable on the Home Assistant process, mirroring how the backend uses
# CONTRIB_VERIFICATION_URI in dev vs prod.
PROD_API_BASE = "https://api.kilowattlas.com"


def _is_local_host(host: str) -> bool:
    """True for loopback / container-local hosts allowed over plaintext in dev."""
    host = (host or "").split(":")[0].lower()
    return host in ("localhost", "127.0.0.1", "::1", "backend", "host.docker.internal")


def resolve_api_base() -> str:
    """Return the API base: a validated dev env override if set, else production.

    Security: the API base is where the bearer token is sent, so an unvalidated
    override could exfiltrate the token to a plaintext or attacker-controlled
    host. We therefore ONLY accept an override that is https:// (any host), or
    http:// pointed at an explicit local dev host (localhost / 127.0.0.1 /
    the Docker service name). Anything else is rejected and we fall back to the
    hard-coded production base. A warning is logged whenever a non-prod base is
    in use so it can't happen silently.
    """
    override = os.getenv("KILOWATTLAS_API_BASE", "").strip()
    if not override:
        return PROD_API_BASE

    parsed = urlparse(override)
    if parsed.scheme == "https" and parsed.netloc:
        _LOGGER.warning("Using non-production API base (override): %s", override)
        return override.rstrip("/")
    if parsed.scheme == "http" and _is_local_host(parsed.netloc):
        _LOGGER.warning("Using INSECURE local API base (dev only): %s", override)
        return override.rstrip("/")

    _LOGGER.error(
        "Ignoring unsafe KILOWATTLAS_API_BASE %r (must be https:// or http://localhost); "
        "falling back to production.",
        override,
    )
    return PROD_API_BASE


# Public website (the map lives here, e.g. kilowattlas.com/?lat=..). This is a
# SEPARATE host from the API base: the API is api.kilowattlas.com (backend), the
# map is kilowattlas.com (frontend). The map URL only opens a browser tab — no
# token is sent to it — so it needs no security validation, just its own
# optional dev override.
PROD_MAP_BASE = "https://kilowattlas.com"


def resolve_map_base() -> str:
    """Return the public map base URL: dev override if set, else production."""
    override = os.getenv("KILOWATTLAS_MAP_BASE", "").strip()
    return override.rstrip("/") if override else PROD_MAP_BASE


# Config entry / options keys.
CONF_API_BASE = "api_base"
CONF_TOKEN = "token"
CONF_SITE_ID = "site_id"
CONF_POWER_SENSOR = "power_sensor"

# Endpoints (relative to API base).
EP_DEVICE_CODE = "/api/v1/contrib/device/code"
EP_DEVICE_TOKEN = "/api/v1/contrib/device/token"
EP_INGEST = "/api/v1/contrib/solar"
EP_REVOKE = "/api/v1/contrib/revoke"

# Sampling + push cadence. Overridable via env vars for local testing so a
# developer can watch data land in seconds instead of waiting a full slot.
# Production defaults: 15-min slots, pushed every 15 min (a completed slot goes
# out on the next cycle, so data is at most ~15 min old). The push loop still
# batches whatever completed slots are pending, so a backlog after an outage is
# flushed together rather than one slot at a time.
SAMPLE_INTERVAL_SECONDS = int(os.getenv("KILOWATTLAS_SAMPLE_SECONDS", "10"))
SLOT_SECONDS = int(os.getenv("KILOWATTLAS_SLOT_SECONDS", str(15 * 60)))
PUSH_INTERVAL_SECONDS = int(os.getenv("KILOWATTLAS_PUSH_SECONDS", str(15 * 60)))
MAX_BATCH = 500  # server cap; keep buffered points bounded

# Storage keys (HA Store) for the offline buffer.
STORAGE_VERSION = 1
STORAGE_KEY = "kilowattlas_trace_the_sun_buffer"


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
