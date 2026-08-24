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

# Sampling + push cadence.
#
# These are DEFAULTS ONLY. The authoritative values come from the server, which
# sends a `config` document in the ingest (and device/token) response; the
# coordinator applies it whenever its config_version increases. That is what lets
# storage resolution be changed centrally instead of by updating every donor's
# install. Until the plugin has heard from the server it runs on these — and
# config_version 0 guarantees any server document wins.
#
# Slot size is the one that determines stored resolution: sampling faster than
# the slot only makes the slot mean more accurate, it does not store more rows.
DEFAULT_CONFIG: dict = {
    "config_version": 0,
    "slot_seconds": 15 * 60,
    "push_interval_seconds": 15 * 60,
    # Bounds on the sample PERIOD. max_ is the slowest we may sample (a ceiling
    # on the interval is a floor on the rate); min_ is the fastest we are allowed
    # to poll. Easy to invert by accident — see resolve_config().
    "max_sample_interval_seconds": 10,
    "min_sample_interval_seconds": 1,
    "max_batch": 500,
    # Off by default. A new plugin talking to an OLD server must not emit
    # night-gap markers: that server would store them as genuine 0 kW rows,
    # turning "inverter asleep" into "produced exactly zero" — the opposite of
    # the intent. Only a server that understands markers turns this on.
    "report_empty_slots": False,
    # The rate the server would LIKE raw readings at. Advisory: the plugin
    # forwards whatever the sensor produces, whenever it produces it, so this
    # only tells a donor what would be useful — it never throttles or upsamples.
    "raw_sample_interval_seconds": 10,
}

# Accepted range for each server-sent value. The server response is untrusted
# input from the plugin's side (same posture as the defensive parsing in
# coordinator._push), so a buggy or hostile document must not be able to cause a
# ZeroDivisionError in the slot floor or a request storm from a 1 s push loop.
CONFIG_CLAMPS: dict = {
    "slot_seconds": (60, 3600),
    "push_interval_seconds": (60, 3600),
    "max_sample_interval_seconds": (1, 900),
    "min_sample_interval_seconds": (1, 900),
    "max_batch": (1, 5000),
    "raw_sample_interval_seconds": (1, 900),
}

# Env overrides for local testing. Unlike the old module constants these act as
# PINS: a pinned field keeps its value and the server's is ignored, rather than
# being silently overwritten on the first push — which would defeat the point of
# setting the override at all.
_ENV_PINS = {
    "slot_seconds": "KILOWATTLAS_SLOT_SECONDS",
    "push_interval_seconds": "KILOWATTLAS_PUSH_SECONDS",
    "max_sample_interval_seconds": "KILOWATTLAS_SAMPLE_SECONDS",
}


def env_pinned_fields() -> dict:
    """Config fields pinned by env var, as {field: int_value}.

    Invalid values are ignored rather than raising: a typo in a dev env var
    should not stop the integration from loading.
    """
    pins: dict = {}
    for field, var in _ENV_PINS.items():
        raw = os.getenv(var)
        if raw is None:
            continue
        try:
            pins[field] = int(raw)
        except (TypeError, ValueError):
            continue
    return pins


def resolve_config(base: dict, incoming: dict | None) -> dict:
    """Merge a server config document onto `base`, clamped and validated.

    Unknown keys are ignored, missing keys keep their current value, and every
    known field is clamped. Env pins are applied last so they always win.
    Returns a new dict; never mutates either argument.
    """
    cfg = dict(base)
    for key, value in (incoming or {}).items():
        if key not in cfg and key != "config_version":
            continue  # unknown field — ignore, do not carry forward
        if key == "report_empty_slots":
            cfg[key] = bool(value)
            continue
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if key in CONFIG_CLAMPS:
            lo, hi = CONFIG_CLAMPS[key]
            n = max(lo, min(hi, n))
        cfg[key] = n

    # A slot that doesn't divide the hour drifts relative to it, breaking the
    # UTC-grid alignment the server validates against and the map assumes.
    if 3600 % cfg["slot_seconds"] != 0:
        cfg["slot_seconds"] = base["slot_seconds"]

    # Guard the confusable pair: an inverted range would otherwise clamp the
    # sample interval to nonsense.
    if cfg["min_sample_interval_seconds"] > cfg["max_sample_interval_seconds"]:
        cfg["min_sample_interval_seconds"] = base["min_sample_interval_seconds"]
        cfg["max_sample_interval_seconds"] = base["max_sample_interval_seconds"]

    cfg.update(env_pinned_fields())
    return cfg


# Storage keys (HA Store) for the offline buffer.
#   v2 added the persisted config and tagged each buffered slot with its size.
#   v3 replaces the slot buffer with a raw-sample retry queue: the plugin no
#      longer aggregates, so what persists is unsent READINGS, not slot means.
#      Any v2 slot means found are carried into legacy_measurements and flushed
#      once — the server still accepts that shape.
STORAGE_VERSION = 3
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
