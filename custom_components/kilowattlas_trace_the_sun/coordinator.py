"""Raw-sample capture + immediate push for a linked Kilowattlas site.

Subscribes to the chosen power sensor and forwards every reading to the ingest
endpoint as soon as it arrives. The SERVER aggregates: it stages raw samples and
computes the 15-minute means itself.

This replaces the previous design, where the plugin averaged locally and pushed
one value per quarter hour. Two things were wrong with that:

  * The finest data the server could ever see was whatever the client chose to
    compute. A donor whose inverter reports every second was flattened to a
    single number per quarter before we ever saw it.
  * Every non-HA client had to reimplement the same slot arithmetic to
    contribute at all.

What is kept from the old design is durability. Readings that fail to send are
held in a persisted retry queue and go out on the next successful connection, so
a network blip or a restart still loses nothing. What is NOT kept is waiting:
there is no push timer. A reading is sent the moment it arrives, and the queue
exists only for readings that could not be.

Cadence is still SERVER-DIRECTED (slot size, retention, target sample rate arrive
in the ingest response), but the plugin no longer needs slot size to do its job —
it forwards timestamps untouched and lets the server bucket them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from statistics import median

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import KilowattlasClient, KilowattlasError
from .const import (
    DEFAULT_CONFIG,
    STORAGE_KEY,
    STORAGE_VERSION,
    capability_tier,
    env_pinned_fields,
    resolve_config,
)

_LOGGER = logging.getLogger(__name__)

# Probe tuning. The probe is a passive subscription — it never asks an
# integration to fetch, so it costs nothing and cannot burn a cloud API quota.
PROBE_WINDOW_SECONDS = 300
PROBE_STARTUP_DELAY_SECONDS = 60  # let HA's boot burst settle first
PROBE_MIN_DELTAS = 5

# Retry queue bounds.
#
# MAX_QUEUE caps memory and storage during a long outage. At one reading per
# second an hour is 3600 rows, so this holds roughly an hour of the fastest
# realistic donor. Past that the OLDEST are dropped: fresh data is worth more
# than stale, and the server can no longer use samples older than an hour
# anyway (its slot is rolled up and its raw rows pruned).
MAX_QUEUE = 5000

# Ceiling on rows per request, kept under the server's own max_batch so a drain
# after an outage is split across several calls rather than rejected wholesale.
MAX_SEND = 500

# How long to wait before retrying after a failed send. Backs off so a server
# that is down does not get hammered, but stays short enough that a brief blip
# costs one interval, not a slot.
RETRY_BASE_SECONDS = 10
RETRY_MAX_SECONDS = 300

# A send is triggered per reading. Coalesce anything arriving within this window
# into one request: a sub-second sensor would otherwise generate a request per
# reading, and batching a handful costs nothing in freshness.
COALESCE_SECONDS = 1.0


class KilowattlasStore(Store):
    """Store that upgrades older buffers in place.

    HA has no migrate_func constructor argument — migration is done by
    overriding _async_migrate_func on a Store subclass.
    """

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict
    ) -> dict:
        """Upgrade the persisted buffer to v3 (raw sample queue).

        v1/v2 held completed SLOT MEANS keyed by timestamp. Those are still
        valid measurements and the server still accepts that shape, so they are
        carried over into a separate list and flushed once on next connect
        rather than discarded — a donor upgrading mid-outage keeps their data.
        """
        if old_major_version >= STORAGE_VERSION:
            return old_data

        legacy_slot = env_pinned_fields().get(
            "slot_seconds", DEFAULT_CONFIG["slot_seconds"]
        )
        legacy: list[dict] = []
        for ts, entry in (old_data or {}).get("pending", old_data or {}).items():
            if not isinstance(entry, dict):
                continue
            legacy.append(
                {
                    "ts": ts,
                    "power_kw": entry.get("power_kw"),
                    "samples": entry.get("samples", 0),
                    "slot_seconds": entry.get("slot_seconds", legacy_slot),
                }
            )
        if legacy:
            _LOGGER.info(
                "Kilowattlas buffer migrated to v%d: %d pre-aggregated slot(s) "
                "will be flushed once, then raw samples take over",
                STORAGE_VERSION,
                len(legacy),
            )
        return {
            "config": resolve_config(DEFAULT_CONFIG, None),
            "queue": [],
            "legacy_measurements": legacy,
            "measured_sample_interval_seconds": None,
        }


def _to_kw(value: float, unit: str | None) -> float | None:
    """Normalise a power reading to kW. Returns None for unknown units."""
    if unit is None:
        return value  # assume kW if the sensor omits a unit
    u = unit.strip().lower()
    if u in ("kw",):
        return value
    if u in ("w",):
        return value / 1000.0
    if u in ("mw",):
        return value * 1000.0
    return None


class KilowattlasCoordinator:
    """Captures sensor readings and forwards them to Kilowattlas immediately."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: KilowattlasClient,
        token: str,
        site_id: int,
        sensor: str,
        entry_id: str,
    ) -> None:
        self.hass = hass
        self._client = client
        self._token = token
        self._site_id = site_id
        self._sensor = sensor

        self._store = KilowattlasStore(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry_id}"
        )

        # Readings captured but not yet acknowledged by the server.
        self._queue: list[dict] = []
        # Pre-aggregated slots inherited from an older version of this plugin.
        self._legacy: list[dict] = []

        self._config = resolve_config(DEFAULT_CONFIG, None)
        self._listeners: list = []

        self._unsub_state = None
        self._unsub_retry = None
        self._unsub_coalesce = None
        self._unsub_probe = None
        self._unsub_probe_daily = None

        self._sending = False
        self._retry_delay = RETRY_BASE_SECONDS

        self._status = "starting"
        self._last_push: datetime | None = None
        self._last_accepted = 0
        self._last_rejected = 0
        self._current_kw: float | None = None
        self._unusable = 0

        self._measured_interval: float | None = None
        self._probe_seen: list[datetime] = []
        self._probe_values: list[float] = []

        # Guards against forwarding the same reading twice: HA fires a state
        # event on attribute changes too, which repeat the same value.
        self._last_ts: datetime | None = None

    # --- properties exposed to the sensor entities ---------------------------

    @property
    def site_id(self) -> int | None:
        return self._site_id

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_push(self) -> datetime | None:
        return self._last_push

    @property
    def pending_count(self) -> int:
        return len(self._queue) + len(self._legacy)

    @property
    def last_accepted(self) -> int:
        return self._last_accepted

    @property
    def last_rejected(self) -> int:
        """Readings the server refused in the last batch.

        Surfaced because the old design dropped a batch after any HTTP 200 and
        never told anyone: a sensor reporting the wrong thing looked exactly
        like a healthy one. A non-zero value here is the visible symptom.
        """
        return self._last_rejected

    @property
    def slot_seconds(self) -> int:
        return self._config["slot_seconds"]

    @property
    def raw_sample_interval_seconds(self) -> int:
        """Server's target seconds between readings. Advisory only — the plugin
        forwards whatever the sensor actually produces."""
        return self._config.get("raw_sample_interval_seconds", 10)

    @property
    def config_version(self) -> int:
        return self._config["config_version"]

    @property
    def measured_sample_interval_seconds(self) -> float | None:
        return self._measured_interval

    @property
    def unusable_samples(self) -> int:
        return self._unusable

    @property
    def current_power_kw(self) -> float | None:
        return self._current_kw

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    @callback
    def _notify(self) -> None:
        for cb in self._listeners:
            cb()

    # --- lifecycle -----------------------------------------------------------

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self._config = resolve_config(
            DEFAULT_CONFIG, stored.get("config"), apply_env=True
        )
        self._queue = [q for q in stored.get("queue", []) if isinstance(q, dict)]
        self._legacy = [
            m for m in stored.get("legacy_measurements", []) if isinstance(m, dict)
        ]
        self._measured_interval = stored.get("measured_sample_interval_seconds")

        # THE core subscription. Event-driven, not polled: a timer can only ever
        # observe at the rate it ticks, so polling a 1 s sensor at 10 s throws
        # away nine readings out of ten. Subscribing delivers every value the
        # integration writes, at whatever rate it writes it — which is the
        # fastest the data can possibly reach us.
        self._unsub_state = async_track_state_change_event(
            self.hass, [self._sensor], self._on_state
        )

        # Rate probe, unchanged in purpose: report what the donor ACHIEVES so
        # the server can distinguish a declared capability from a real one.
        self._unsub_probe = async_call_later(
            self.hass, PROBE_STARTUP_DELAY_SECONDS, self._start_probe
        )
        self._unsub_probe_daily = async_track_time_change(
            self.hass, self._start_probe, hour=12, minute=0, second=0
        )

        self._status = "connected"
        self._notify()

        # Anything left from a previous run goes out now rather than waiting for
        # the next reading — which on a sleeping inverter could be hours.
        if self._queue or self._legacy:
            self.hass.async_create_task(self._send())

    async def async_stop(self) -> None:
        for unsub in (
            self._unsub_state,
            self._unsub_retry,
            self._unsub_coalesce,
            self._unsub_probe,
            self._unsub_probe_daily,
        ):
            if unsub:
                unsub()
        self._unsub_state = None
        self._unsub_retry = None
        self._unsub_coalesce = None
        self._unsub_probe = None
        self._unsub_probe_daily = None

        # One last attempt: a clean shutdown should not strand readings that a
        # single request would have delivered.
        if self._queue or self._legacy:
            try:
                await self._send()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                _LOGGER.debug("Kilowattlas final flush failed; queued for next start")
        await self._save()

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "config": self._config,
                "queue": self._queue,
                "legacy_measurements": self._legacy,
                "measured_sample_interval_seconds": self._measured_interval,
            }
        )

    # --- capture -------------------------------------------------------------

    @callback
    def _on_state(self, event) -> None:
        """Queue a reading and schedule an immediate send."""
        state = event.data.get("new_state")
        if state is None or state.state in ("unknown", "unavailable", None, ""):
            self._unusable += 1
            self._notify()
            return
        try:
            raw = float(state.state)
        except (ValueError, TypeError):
            self._unusable += 1
            self._notify()
            return

        kw = _to_kw(raw, state.attributes.get("unit_of_measurement"))
        if kw is None:
            # An unrecognised unit is not a transient glitch — it means this
            # sensor cannot be interpreted at all. Counted, not guessed at.
            self._unusable += 1
            self._notify()
            return

        # last_updated advances on every write by the integration, including
        # ones that did not change the value — which is what we want, since a
        # steady 4.2 kW for a minute is six real readings, not one.
        ts = state.last_updated
        if self._last_ts is not None and ts <= self._last_ts:
            return  # attribute-only event, or a repeat; not new data
        self._last_ts = ts

        self._current_kw = round(kw, 3)
        self._queue.append(
            {"ts": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "power_kw": round(kw, 3)}
        )

        # Drop from the FRONT when full: the server refuses samples older than an
        # hour, so the oldest are the ones already worthless.
        if len(self._queue) > MAX_QUEUE:
            dropped = len(self._queue) - MAX_QUEUE
            del self._queue[:dropped]
            _LOGGER.warning(
                "Kilowattlas queue full; dropped %d oldest reading(s)", dropped
            )

        self._schedule_send()
        self._notify()

    @callback
    def _schedule_send(self) -> None:
        """Send almost immediately, coalescing a burst into one request."""
        if self._unsub_coalesce or self._sending:
            return  # a send is already imminent or in flight

        @callback
        def _fire(_now) -> None:
            self._unsub_coalesce = None
            self.hass.async_create_task(self._send())

        self._unsub_coalesce = async_call_later(self.hass, COALESCE_SECONDS, _fire)

    # --- send ----------------------------------------------------------------

    async def _send(self) -> None:
        """Push queued readings. Only what the server accepts leaves the queue."""
        if self._sending:
            return
        if not self._queue and not self._legacy:
            return
        self._sending = True
        try:
            batch = self._queue[:MAX_SEND]
            legacy = self._legacy[:MAX_SEND] if self._legacy else None

            result = await self._client.ingest(
                self._token,
                measurements=legacy,
                samples=batch or None,
                donor=self._donor_report(),
            )

            # Only drop what was actually sent. Readings that arrived DURING the
            # request are still at the tail and go out next time — slicing by
            # count rather than clearing is what prevents that loss.
            del self._queue[: len(batch)]
            if legacy:
                del self._legacy[: len(legacy)]

            self._last_accepted = int(result.get("accepted", 0))
            rejected = result.get("rejected") or []
            self._last_rejected = len(rejected)
            if rejected:
                # Rejections are permanent-invalid, so resending is pointless —
                # but they must be visible. The old version discarded them
                # silently, which is how a misconfigured sensor could look
                # healthy indefinitely.
                reasons = {r.get("reason", "?") for r in rejected if isinstance(r, dict)}
                _LOGGER.warning(
                    "Kilowattlas rejected %d reading(s): %s",
                    len(rejected),
                    ", ".join(sorted(reasons)),
                )

            self._apply_config(result.get("config"))
            self._last_push = dt_util.utcnow()
            self._status = "connected"
            self._retry_delay = RETRY_BASE_SECONDS
            await self._save()

            # More waiting (a drain after an outage) — keep going immediately.
            if self._queue or self._legacy:
                self._schedule_send()

        except KilowattlasError as err:
            # Nothing is dropped: the queue is exactly what retry exists for.
            self._status = "retrying"
            _LOGGER.debug("Kilowattlas send failed (%s); %d queued", err, len(self._queue))
            await self._save()
            self._schedule_retry()
        finally:
            self._sending = False
            self._notify()

    @callback
    def _schedule_retry(self) -> None:
        if self._unsub_retry:
            return

        @callback
        def _fire(_now) -> None:
            self._unsub_retry = None
            self.hass.async_create_task(self._send())

        self._unsub_retry = async_call_later(self.hass, self._retry_delay, _fire)
        # Exponential backoff, capped: a server that is down for an hour should
        # not be probed 360 times.
        self._retry_delay = min(self._retry_delay * 2, RETRY_MAX_SECONDS)

    def _donor_report(self) -> dict:
        report: dict = {"config_version": self._config["config_version"]}
        if self._measured_interval is not None:
            report["measured_sample_interval_seconds"] = self._measured_interval
            report["sample_capability"] = capability_tier(self._measured_interval)
        return report

    # --- rate probe ----------------------------------------------------------

    @callback
    def _start_probe(self, _now=None) -> None:
        """Measure how often the sensor actually updates.

        Still worth doing even though every reading is now forwarded: the server
        uses it to tell a donor that CAN do 1 s from one that merely said so.
        """
        if self._unsub_probe:
            self._unsub_probe()
            self._unsub_probe = None
        self._probe_seen = []
        self._probe_values = []
        unsub_events = async_track_state_change_event(
            self.hass, [self._sensor], self._on_probe_event
        )

        @callback
        def _finish(_now) -> None:
            unsub_events()
            self._finish_probe()

        self._unsub_probe = async_call_later(self.hass, PROBE_WINDOW_SECONDS, _finish)

    @callback
    def _on_probe_event(self, event) -> None:
        state = event.data.get("new_state")
        if state is None or state.state in ("unknown", "unavailable", None, ""):
            return
        if not self._probe_seen or state.last_updated != self._probe_seen[-1]:
            self._probe_seen.append(state.last_updated)
        try:
            self._probe_values.append(float(state.state))
        except (ValueError, TypeError):
            pass

    @callback
    def _finish_probe(self) -> None:
        """Conclude the probe, or leave the previous rate alone.

        An inconclusive probe must NEVER be read as "this sensor is slow": one
        cloudy night would then permanently downgrade a fast donor. Silence means
        unknown, not slow.
        """
        deltas = [
            (b - a).total_seconds()
            for a, b in zip(self._probe_seen, self._probe_seen[1:])
            if (b - a).total_seconds() > 0
        ]
        if len(deltas) < PROBE_MIN_DELTAS:
            _LOGGER.debug(
                "Kilowattlas probe inconclusive (%d deltas); keeping %s s",
                len(deltas),
                self._measured_interval,
            )
            return
        # All-zero readings mean the inverter is asleep: the sensor may simply
        # have stopped being written, which says nothing about its real rate.
        if self._probe_values and not any(v != 0 for v in self._probe_values):
            _LOGGER.debug("Kilowattlas probe saw only zeros; not concluding")
            return

        # Median, not mean: one restart-induced gap or burst must not skew it.
        self._measured_interval = round(median(deltas), 3)
        _LOGGER.info(
            "Kilowattlas measured sensor rate: %.3fs", self._measured_interval
        )
        self._notify()

    # --- config --------------------------------------------------------------

    def _apply_config(self, incoming: dict | None) -> None:
        """Adopt a server config document if it is newer than what we hold.

        Slot size no longer changes anything the plugin does — the server buckets
        the timestamps — but it is kept in state so the diagnostic sensor can
        show what the server is aggregating to.
        """
        if not isinstance(incoming, dict):
            return
        try:
            incoming_version = int(incoming.get("config_version", 0))
        except (TypeError, ValueError):
            return
        if incoming_version <= self._config["config_version"]:
            return
        self._config = resolve_config(self._config, incoming, apply_env=True)
        _LOGGER.info(
            "Kilowattlas applied server config v%d (slot %ds, target sample %ds)",
            self._config["config_version"],
            self._config["slot_seconds"],
            self._config.get("raw_sample_interval_seconds", 10),
        )
        self._notify()
