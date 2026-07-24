"""Sampling + aggregation + push for a linked Kilowattlas site.

Reads the chosen power sensor every SAMPLE_INTERVAL_SECONDS, averages each raw
sample into its 15-min UTC slot, and flushes completed slots to the ingest
endpoint on a batched (hourly) cadence. Unsent slots persist via HA Store so a
network outage or restart never loses data — the server upsert makes resends
idempotent.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import KilowattlasClient, KilowattlasError
from .const import (
    MAX_BATCH,
    PUSH_INTERVAL_SECONDS,
    SAMPLE_INTERVAL_SECONDS,
    SLOT_SECONDS,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)


def _slot_start(ts: datetime) -> datetime:
    """Floor a UTC datetime to its 15-min slot boundary."""
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % SLOT_SECONDS), tz=timezone.utc)


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
    ) -> None:
        self.hass = hass
        self._client = client
        self._token = token
        self._sensor = power_sensor
        self._site_id = site_id
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

        # Accumulator for the slot currently being filled.
        self._cur_slot: datetime | None = None
        self._cur_sum = 0.0
        self._cur_count = 0

        # Completed-but-unsent slots: {iso_ts: {"power_kw": float, "samples": int}}.
        self._pending: dict[str, dict] = {}

        self._unsub_sample = None
        self._unsub_push = None

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

    async def async_start(self) -> None:
        """Load any buffered points and start the sampling + push timers."""
        stored = await self._store.async_load()
        if stored:
            self._pending = stored
        self._unsub_sample = async_track_time_interval(
            self.hass, self._sample, timedelta(seconds=SAMPLE_INTERVAL_SECONDS)
        )
        self._unsub_push = async_track_time_interval(
            self.hass, self._push, timedelta(seconds=PUSH_INTERVAL_SECONDS)
        )
        _LOGGER.info(
            "Kilowattlas coordinator started: sensor=%s sample=%ds slot=%ds push=%ds",
            self._sensor,
            SAMPLE_INTERVAL_SECONDS,
            SLOT_SECONDS,
            PUSH_INTERVAL_SECONDS,
        )

    async def async_stop(self) -> None:
        """Stop timers and flush the current slot + pending buffer to disk."""
        if self._unsub_sample:
            self._unsub_sample()
        if self._unsub_push:
            self._unsub_push()
        self._roll_slot(force=True)
        await self._store.async_save(self._pending)

    @callback
    def _sample(self, _now) -> None:
        """Read the sensor once and fold it into the current 15-min slot."""
        state = self.hass.states.get(self._sensor)
        if state is None or state.state in ("unknown", "unavailable", None, ""):
            return
        try:
            raw = float(state.state)
        except (ValueError, TypeError):
            return
        unit = state.attributes.get("unit_of_measurement")
        kw = _to_kw(raw, unit)
        if kw is None:
            return

        now = dt_util.utcnow()
        slot = _slot_start(now)
        if self._cur_slot is None:
            self._cur_slot = slot
        elif slot != self._cur_slot:
            # A slot boundary was crossed — finalise the old slot first.
            self._roll_slot()
            self._cur_slot = slot

        self._cur_sum += kw
        self._cur_count += 1
        # Let the production sensor reflect the latest reading.
        self._notify()

    def _roll_slot(self, force: bool = False) -> None:
        """Finalise the current slot into the pending buffer."""
        if self._cur_slot is None or self._cur_count == 0:
            self._cur_slot = None
            self._cur_sum = 0.0
            self._cur_count = 0
            return
        mean_kw = round(self._cur_sum / self._cur_count, 3)
        self._pending[self._cur_slot.isoformat().replace("+00:00", "Z")] = {
            "power_kw": mean_kw,
            "samples": self._cur_count,
        }
        self._cur_slot = None
        self._cur_sum = 0.0
        self._cur_count = 0

    async def _push(self, _now) -> None:
        """Flush completed slots to the ingest endpoint, batched."""
        # Roll the current slot only if it's already in the past.
        if self._cur_slot is not None and self._cur_slot < _slot_start(dt_util.utcnow()):
            self._roll_slot()

        if not self._pending:
            return

        items = sorted(self._pending.items())[:MAX_BATCH]
        measurements = [
            {"ts": ts, "power_kw": v["power_kw"], "samples": v["samples"]}
            for ts, v in items
        ]
        try:
            result = await self._client.ingest(self._token, measurements)
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
        await self._store.async_save(self._pending)

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
