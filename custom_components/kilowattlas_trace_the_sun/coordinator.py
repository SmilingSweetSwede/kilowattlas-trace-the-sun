"""Sampling + aggregation + push for a linked Kilowattlas site.

Reads the chosen power sensor on a timer, averages each raw sample into its UTC
slot, and flushes completed slots to the ingest endpoint. Unsent slots persist
via HA Store so a network outage or restart never loses data — the server upsert
makes resends idempotent.

Cadence is SERVER-DIRECTED: slot size, push interval and the allowed sampling
range arrive in the ingest response and are applied when their config_version
increases (see const.resolve_config). The plugin only picks its own sample rate,
and only within the bounds the server allows — it measures what the sensor
actually delivers rather than assuming a fixed rate, since a cloud-polled
inverter and a local Modbus one differ by two orders of magnitude.
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


class KilowattlasStore(Store):
    """Store that upgrades the v1 buffer in place.

    HA has no migrate_func constructor argument — migration is done by
    overriding _async_migrate_func on a Store subclass.
    """

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict
    ) -> dict:
        """Upgrade the persisted buffer from v1 to v2.

        v1 was a bare {iso_ts: {power_kw, samples}} map with no record of the
        slot size that produced each entry. Everything in a v1 buffer was
        produced at the then-current slot size, so tag it with that — reading the
        env pin if one is set, since a developer's override was the only way v1
        could have been anything other than 900 s.
        """
        if old_major_version >= STORAGE_VERSION:
            return old_data
        legacy_slot = env_pinned_fields().get(
            "slot_seconds", DEFAULT_CONFIG["slot_seconds"]
        )
        pending = {}
        for ts, entry in (old_data or {}).items():
            if not isinstance(entry, dict):
                continue
            pending[ts] = {**entry, "slot_seconds": legacy_slot}
        _LOGGER.info(
            "Kilowattlas buffer migrated v%d -> v%d: %d slot(s) tagged %ds",
            old_major_version,
            STORAGE_VERSION,
            len(pending),
            legacy_slot,
        )
        return {
            "config": resolve_config(DEFAULT_CONFIG, None),
            "pending": pending,
            "measured_sample_interval_seconds": None,
        }


def _floor(ts: datetime, slot_seconds: int) -> datetime:
    """Floor a UTC datetime to a slot boundary of the given size.

    Takes the size explicitly because a slot-size change has to floor against
    both the old and the new value.
    """
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % slot_seconds), tz=timezone.utc)


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
    """Owns the sampling loop and the push loop for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: KilowattlasClient,
        token: str,
        power_sensor: str,
        site_id: int | None = None,
        entry_id: str | None = None,
    ) -> None:
        self.hass = hass
        self._client = client
        self._token = token
        self._sensor = power_sensor
        self._site_id = site_id
        # Per-entry storage key. A shared key would let two linked sites clobber
        # each other's buffer — harmless when every site had identical cadence,
        # actively corrupting now that slot size is per-site.
        self._storage_key = f"{STORAGE_KEY}_{entry_id}" if entry_id else STORAGE_KEY
        self._store: Store = KilowattlasStore(
            hass, STORAGE_VERSION, self._storage_key
        )

        # Effective (clamped) cadence policy. Starts at the baked-in defaults so
        # the plugin works before it has ever heard from the server; any real
        # server document outranks it (config_version 0).
        self._cfg: dict = resolve_config(DEFAULT_CONFIG, None)
        self._sample_interval: int = self._cfg["max_sample_interval_seconds"]
        # The config version whose cadence is actually running. Distinct from
        # _cfg["config_version"] only for the instant between receiving a
        # document and finishing its application; reported to the server so a
        # donor that failed to apply a change is distinguishable from one that
        # never received it.
        self._applied_version: int = self._cfg["config_version"]

        # Accumulator for the slot currently being filled.
        self._cur_slot: datetime | None = None
        self._cur_sum = 0.0
        self._cur_count = 0
        self._cur_unusable = 0  # reads that happened but were unusable

        # Completed-but-unsent slots:
        # {iso_ts: {"power_kw": float, "samples": int, "slot_seconds": int}}.
        self._pending: dict[str, dict] = {}

        self._unsub_sample = None
        self._unsub_push = None
        self._unsub_probe = None
        self._unsub_probe_daily = None

        # Measured sensor refresh rate (seconds between distinct updates).
        self._measured_interval: float | None = None
        self._probe_seen: list[datetime] = []
        self._probe_values: list[float] = []

        # Observable state for the status sensor.
        self._status: str = "starting"  # starting|ok|revoked|error
        self._last_push: datetime | None = None
        self._last_accepted: int = 0
        self._listeners: list = []

    # --- status observability (read by the sensor entity) --------------------

    @property
    def site_id(self) -> int | None:
        return getattr(self, "_site_id", None)

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_push(self) -> datetime | None:
        return self._last_push

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def last_accepted(self) -> int:
        return self._last_accepted

    @property
    def slot_seconds(self) -> int:
        return self._cfg["slot_seconds"]

    @property
    def push_interval_seconds(self) -> int:
        return self._cfg["push_interval_seconds"]

    @property
    def sample_interval_seconds(self) -> int:
        return self._sample_interval

    @property
    def config_version(self) -> int:
        """The config version whose cadence is actually running."""
        return self._applied_version

    @property
    def measured_sample_interval_seconds(self) -> float | None:
        return self._measured_interval

    @property
    def unusable_samples_this_slot(self) -> int:
        return self._cur_unusable

    @property
    def current_power_kw(self) -> float | None:
        """Running mean (kW) of the slot currently being filled, or None if the
        sensor hasn't produced a usable sample yet this slot."""
        if self._cur_count == 0:
            return None
        return round(self._cur_sum / self._cur_count, 3)

    def add_listener(self, cb) -> None:
        """Register a callback fired whenever the status state changes."""
        self._listeners.append(cb)

    def _notify(self) -> None:
        for cb in self._listeners:
            cb()

    # --- lifecycle -----------------------------------------------------------

    async def async_start(self) -> None:
        """Load the persisted buffer + config and start the timers."""
        stored = await self._store.async_load()
        if stored:
            # v2 shape; the Store migrator upgrades v1 before we see it.
            self._pending = stored.get("pending", {}) or {}
            self._cfg = resolve_config(DEFAULT_CONFIG, stored.get("config"))
            # The persisted config IS what we start running, so it is applied by
            # definition — without this a restart would report version 0 and
            # look like a donor that had regressed.
            self._applied_version = self._cfg["config_version"]
            measured = stored.get("measured_sample_interval_seconds")
            if isinstance(measured, (int, float)):
                self._measured_interval = float(measured)

        self._sample_interval = self._clamp_sample_interval(
            self._measured_interval or self._cfg["max_sample_interval_seconds"]
        )
        self._reschedule_timers()

        # Probe after HA settles: during startup every integration writes state,
        # which would measure HA's boot rather than the sensor.
        self._unsub_probe = async_call_later(
            self.hass, PROBE_STARTUP_DELAY_SECONDS, self._start_probe
        )
        # Re-probe daily around local noon — a solar sensor is guaranteed to be
        # varying then. A plain 24 h interval would eventually land at night and
        # measure a sleeping inverter.
        self._unsub_probe_daily = async_track_time_change(
            self.hass, self._start_probe, hour=12, minute=0, second=0
        )

        _LOGGER.info(
            "Kilowattlas coordinator started: sensor=%s sample=%ds slot=%ds "
            "push=%ds config_version=%d",
            self._sensor,
            self._sample_interval,
            self._cfg["slot_seconds"],
            self._cfg["push_interval_seconds"],
            self._cfg["config_version"],
        )

    async def async_stop(self) -> None:
        """Stop timers and flush the current slot + pending buffer to disk."""
        for unsub in (
            self._unsub_sample,
            self._unsub_push,
            self._unsub_probe,
            self._unsub_probe_daily,
        ):
            if unsub:
                unsub()
        self._unsub_sample = self._unsub_push = None
        self._unsub_probe = self._unsub_probe_daily = None
        self._roll_slot(force=True)
        await self._save()

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "config": self._cfg,
                "pending": self._pending,
                "measured_sample_interval_seconds": self._measured_interval,
            }
        )

    def _reschedule_timers(self) -> None:
        """(Re-)register the sample + push timers at the current cadence.

        async_track_time_interval cannot change an interval in place, so a
        cadence change means unsubscribe + re-subscribe. Safe to call repeatedly;
        callers should only do so when a value actually changed, since
        re-registering resets the timer phase.
        """
        if self._unsub_sample:
            self._unsub_sample()
        if self._unsub_push:
            self._unsub_push()
        self._unsub_sample = async_track_time_interval(
            self.hass, self._sample, timedelta(seconds=self._sample_interval)
        )
        self._unsub_push = async_track_time_interval(
            self.hass,
            self._push,
            timedelta(seconds=self._cfg["push_interval_seconds"]),
        )

    def _slot_start(self, ts: datetime) -> datetime:
        """Floor a UTC datetime to the current (server-directed) slot boundary."""
        return _floor(ts, self._cfg["slot_seconds"])

    # --- sampling ------------------------------------------------------------

    @callback
    def _sample(self, _now) -> None:
        """Read the sensor once and fold it into the current slot."""
        # Advance the slot clock BEFORE reading. If the sensor is unavailable the
        # read below returns early, so doing this after would mean a boundary is
        # never noticed while the sensor is down — and the slot that was open
        # when it went down would sit unfinalised instead of being emitted.
        slot = self._slot_start(dt_util.utcnow())
        if self._cur_slot is not None and slot != self._cur_slot:
            self._roll_slot()
        if self._cur_slot is None:
            self._cur_slot = slot

        state = self.hass.states.get(self._sensor)
        if state is None or state.state in ("unknown", "unavailable", None, ""):
            self._cur_unusable += 1
            return
        try:
            raw = float(state.state)
        except (ValueError, TypeError):
            self._cur_unusable += 1
            return
        unit = state.attributes.get("unit_of_measurement")
        kw = _to_kw(raw, unit)
        if kw is None:
            self._cur_unusable += 1
            return

        self._cur_sum += kw
        self._cur_count += 1
        # Let the production sensor reflect the latest reading.
        self._notify()

    def _roll_slot(self, force: bool = False) -> None:
        """Finalise the current slot into the pending buffer.

        A slot with no usable samples is emitted as an explicit zero marker when
        the server asked for it: a sleeping inverter and an offline donor both
        produce silence, and only the marker tells them apart downstream. The
        zero is an inference, not a measurement, which is why the server records
        it as is_estimated.
        """
        if self._cur_slot is None:
            self._reset_accumulator()
            return

        if self._cur_count == 0:
            if self._cfg["report_empty_slots"] and self._cur_unusable > 0:
                self._pending[self._iso(self._cur_slot)] = {
                    "power_kw": 0.0,
                    "samples": 0,
                    "slot_seconds": self._cfg["slot_seconds"],
                    "empty": True,
                }
            self._reset_accumulator()
            return

        mean_kw = round(self._cur_sum / self._cur_count, 3)
        self._pending[self._iso(self._cur_slot)] = {
            "power_kw": mean_kw,
            "samples": self._cur_count,
            "slot_seconds": self._cfg["slot_seconds"],
        }
        self._reset_accumulator()

    def _reset_accumulator(self) -> None:
        self._cur_slot = None
        self._cur_sum = 0.0
        self._cur_count = 0
        self._cur_unusable = 0

    @staticmethod
    def _iso(ts: datetime) -> str:
        return ts.isoformat().replace("+00:00", "Z")

    # --- rate probe ----------------------------------------------------------

    def _clamp_sample_interval(self, seconds: float) -> int:
        lo = self._cfg["min_sample_interval_seconds"]
        hi = self._cfg["max_sample_interval_seconds"]
        return int(max(lo, min(hi, round(seconds))))

    @callback
    def _start_probe(self, _now=None) -> None:
        """Measure how often the sensor actually updates.

        Subscribes instead of polling: polling can only observe the rate at which
        we poll. Uses last_updated (advances on every write by the integration)
        rather than last_changed (which a steady value never advances, so a
        healthy sensor reporting a constant would look dead).
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

        self._unsub_probe = async_call_later(
            self.hass, PROBE_WINDOW_SECONDS, _finish
        )

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
        new_interval = self._clamp_sample_interval(self._measured_interval)
        if new_interval != self._sample_interval:
            self._sample_interval = new_interval
            self._reschedule_timers()
        _LOGGER.info(
            "Kilowattlas measured sensor rate: %.3fs -> sampling every %ds",
            self._measured_interval,
            self._sample_interval,
        )
        self._notify()

    # --- config --------------------------------------------------------------

    def _apply_config(self, incoming: dict | None) -> None:
        """Apply a server config document, handling a slot-size change safely.

        The buffer is keyed by slot start, so entries produced under different
        slot sizes are not interchangeable: a 900 s mean relabelled as a 300 s
        slot is silently wrong data, and a 300 s key is simply off-grid for a
        900 s server (rejected, then dropped without retry). So finalise and tag
        the old-size work before switching, and let _push send one granularity
        per request.
        """
        if not isinstance(incoming, dict):
            return
        try:
            version = int(incoming.get("config_version", 0))
        except (TypeError, ValueError):
            return
        if version <= self._cfg["config_version"]:
            return  # idempotent: a repeated document must not reset timer phase

        new_cfg = resolve_config(self._cfg, incoming)
        old_slot = self._cfg["slot_seconds"]
        new_slot = new_cfg["slot_seconds"]
        cadence_changed = (
            new_cfg["push_interval_seconds"] != self._cfg["push_interval_seconds"]
            or new_cfg["max_sample_interval_seconds"]
            != self._cfg["max_sample_interval_seconds"]
            or new_cfg["min_sample_interval_seconds"]
            != self._cfg["min_sample_interval_seconds"]
        )

        if new_slot == old_slot:
            self._cfg = new_cfg
            new_interval = self._clamp_sample_interval(
                self._measured_interval or new_cfg["max_sample_interval_seconds"]
            )
            if new_interval != self._sample_interval:
                self._sample_interval = new_interval
                cadence_changed = True
            if cadence_changed:
                self._reschedule_timers()
            self._applied_version = version  # cadence is now live
            return

        # Slot size is changing.
        self._roll_slot(force=True)  # finalise in-flight work at the OLD size
        for entry in self._pending.values():
            entry.setdefault("slot_seconds", old_slot)
        self._cfg = new_cfg
        self._cur_slot = None
        self._sample_interval = self._clamp_sample_interval(
            self._measured_interval or new_cfg["max_sample_interval_seconds"]
        )
        self._reschedule_timers()
        self._applied_version = version  # cadence is now live
        _LOGGER.info(
            "Kilowattlas slot size changed %ds -> %ds (config_version %d); "
            "%d buffered slot(s) will be flushed at the old size",
            old_slot,
            new_slot,
            version,
            len(self._pending),
        )
        # Drain the old-granularity backlog promptly rather than waiting a full
        # push interval — it can only be sent while the server still accepts it.
        if self._pending:
            self.hass.async_create_task(self._push(None))

    def _donor_report(self) -> dict:
        """Self-report: what this donor achieves, and which config it is running.

        config_version is the version whose cadence is ACTUALLY in effect — set
        once the timers have been rescheduled, not merely once a document has
        been received. That makes the server's config_version_ack answer "is this
        donor caught up?", which is the whole point of the column: an ack that
        equals config_version proves the change landed, and one that trails
        identifies a stuck donor.
        """
        report: dict = {"config_version": self._applied_version}
        if self._measured_interval is not None:
            report["measured_sample_interval_seconds"] = self._measured_interval
            report["sample_capability"] = capability_tier(
                int(round(self._measured_interval))
            )
        return report

    # --- push ----------------------------------------------------------------

    async def _push(self, _now) -> None:
        """Flush completed slots to the ingest endpoint, batched."""
        # Roll the current slot only if it's already in the past.
        if self._cur_slot is not None and self._cur_slot < self._slot_start(
            dt_util.utcnow()
        ):
            self._roll_slot()

        if not self._pending:
            return

        # Never mix granularities in one request: the server validates the whole
        # batch against ONE slot size, so a mixed batch has part of it rejected —
        # and rejected rows are dropped, not retried. Oldest granularity first;
        # the next cycle picks up the rest.
        items = sorted(self._pending.items())
        oldest_slot = items[0][1].get("slot_seconds", self._cfg["slot_seconds"])
        items = [
            (ts, v)
            for ts, v in items
            if v.get("slot_seconds", self._cfg["slot_seconds"]) == oldest_slot
        ][: self._cfg["max_batch"]]

        measurements = []
        for ts, v in items:
            m = {"ts": ts, "power_kw": v["power_kw"], "samples": v["samples"]}
            if v.get("empty"):
                m["empty"] = True
            measurements.append(m)

        try:
            result = await self._client.ingest(
                self._token, measurements, donor=self._donor_report()
            )
        except KilowattlasError as err:
            _LOGGER.warning("Kilowattlas push failed (will retry): %s", err)
            # A revoked/unauthorized token won't recover on retry, so surface it
            # distinctly; other failures are transient.
            self._status = "revoked" if "unauthorized" in str(err) else "error"
            self._notify()
            return  # keep buffer; retry next cycle

        # The request as a whole succeeded (HTTP 200). Every slot in this batch
        # was either accepted (upserted, idempotent) or rejected as permanently
        # invalid (off-grid / over-capacity / etc.) — neither case benefits from
        # a resend, so drop the whole batch from the buffer. Defensive parsing:
        # the server response is untrusted, so tolerate a malformed `rejected`.
        rejected = result.get("rejected", [])
        rejected_ts = {
            r.get("ts") for r in rejected if isinstance(r, dict)
        } if isinstance(rejected, list) else set()
        for ts, _ in items:
            self._pending.pop(ts, None)
        await self._save()

        accepted = result.get("accepted", 0)
        self._status = "ok"
        self._last_push = dt_util.utcnow()
        self._last_accepted = accepted if isinstance(accepted, int) else 0
        self._notify()

        _LOGGER.info(
            "Kilowattlas push: %d sent, %d accepted, %d rejected",
            len(measurements),
            accepted,
            len(rejected_ts),
        )

        # Apply any new cadence policy last, so it takes effect from the next
        # cycle rather than mid-flush.
        self._apply_config(result.get("config"))
