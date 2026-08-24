"""Thin async client for the Kilowattlas contrib API."""

from __future__ import annotations

from dataclasses import dataclass
import json

import aiohttp

from .const import EP_DEVICE_CODE, EP_DEVICE_TOKEN, EP_INGEST, EP_REVOKE


async def _read_json(resp: aiohttp.ClientResponse) -> dict:
    """Parse a JSON body tolerantly.

    aiohttp's resp.json() raises ContentTypeError unless the server sends an
    application/json content-type. A misconfigured API base (e.g. a host that
    returns an HTML/text error page) would otherwise surface as an unhandled
    500 in the config flow, so we decode the raw text ourselves and raise a
    clean KilowattlasError when it isn't JSON.
    """
    text = await resp.text()
    try:
        return json.loads(text)
    except (ValueError, TypeError) as err:
        snippet = text[:120].replace("\n", " ")
        raise KilowattlasError(
            f"expected JSON from the API but got: {snippet!r}"
        ) from err


class KilowattlasError(Exception):
    """Generic API error."""


class AuthorizationPending(KilowattlasError):
    """The device code has not been approved on the web yet."""


class AuthorizationExpired(KilowattlasError):
    """The device code expired before it was approved."""


@dataclass
class DeviceCode:
    """Response of the device-code request."""

    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


@dataclass
class TokenGrant:
    """Response of a successful token poll."""

    token: str
    site_id: int


class KilowattlasClient:
    """Talks to the Kilowattlas contrib endpoints over aiohttp."""

    def __init__(self, session: aiohttp.ClientSession, api_base: str) -> None:
        self._session = session
        self._base = api_base.rstrip("/")

    async def request_device_code(
        self,
        lat: float | None,
        lng: float | None,
        capability: dict | None = None,
    ) -> DeviceCode:
        """Start a device-authorization flow.

        `capability` carries phase-2 readiness hints (sample_capability,
        sample_interval_seconds, device_brand) so the backend records, at link
        time, whether this donor could ever feed a low-latency stream. Optional
        and forward-compatible: the backend ignores unknown/absent fields.
        """
        payload: dict = {}
        if lat is not None:
            payload["lat"] = lat
        if lng is not None:
            payload["lng"] = lng
        if capability:
            payload.update(capability)
        try:
            async with self._session.post(
                f"{self._base}{EP_DEVICE_CODE}", json=payload
            ) as resp:
                if resp.status != 200:
                    raise KilowattlasError(f"device/code returned {resp.status}")
                data = await _read_json(resp)
        except aiohttp.ClientError as err:
            raise KilowattlasError(f"cannot reach {self._base}: {err}") from err
        if not isinstance(data, dict) or not all(
            k in data for k in ("device_code", "user_code", "verification_uri")
        ):
            raise KilowattlasError("device/code returned an unexpected response")
        return DeviceCode(
            device_code=str(data["device_code"]),
            user_code=str(data["user_code"]),
            verification_uri=str(data["verification_uri"]),
            interval=int(data.get("interval", 5)),
            expires_in=int(data.get("expires_in", 900)),
        )

    async def poll_token(self, device_code: str) -> TokenGrant:
        """Exchange an approved device code for the ingest token.

        Raises AuthorizationPending while the user hasn't approved yet, and
        AuthorizationExpired once the code is dead.
        """
        try:
            async with self._session.post(
                f"{self._base}{EP_DEVICE_TOKEN}", json={"device_code": device_code}
            ) as resp:
                data = await _read_json(resp)
                status = resp.status
        except aiohttp.ClientError as err:
            raise KilowattlasError(f"cannot reach {self._base}: {err}") from err
        if not isinstance(data, dict):
            raise KilowattlasError("device/token returned an unexpected response")
        if status == 200:
            if "token" not in data or "site_id" not in data:
                raise KilowattlasError("device/token missing token/site_id")
            return TokenGrant(token=str(data["token"]), site_id=int(data["site_id"]))
        err = data.get("error", "")
        if err == "authorization_pending":
            raise AuthorizationPending
        if err in ("expired_token", "invalid_grant"):
            raise AuthorizationExpired
        raise KilowattlasError(f"device/token error: {err or status}")

    async def ingest(
        self,
        token: str,
        measurements: list[dict] | None = None,
        donor: dict | None = None,
        samples: list[dict] | None = None,
    ) -> dict:
        """Push readings. Idempotent server-side.

        Two shapes, either or both:

        `samples` — raw readings at whatever resolution the sensor produces,
        each {ts, power_kw, channel_key?}. The server stages them and computes
        the slot mean itself. This is the preferred path: it keeps full
        resolution all the way to the server and needs no clock arithmetic here.

        `measurements` — pre-aggregated slot means. Kept for servers that
        predate raw ingest; a 400 "no_measurements" is how such a server
        announces itself.

        `donor` is optional self-reported telemetry (measured sample rate, the
        config version actually in effect). Servers that predate it ignore the
        key. The 200 response may carry a `config` document — the caller applies
        it; this layer just passes it through.

        Always returns a dict. A server that answers 200 with a non-object body
        is treated as an error rather than passed through, so a malicious or
        broken server can't feed an unexpected type into the caller.
        """
        body: dict = {}
        if measurements:
            body["measurements"] = measurements
        if samples:
            body["samples"] = samples
        if donor:
            body["donor"] = donor
        try:
            async with self._session.post(
                f"{self._base}{EP_INGEST}",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status == 401:
                    raise KilowattlasError("unauthorized (token revoked?)")
                if resp.status != 200:
                    # Truncate + strip newlines: the body is server-controlled
                    # and gets logged, so don't let it inject/spam the HA log.
                    text = (await resp.text())[:200].replace("\n", " ")
                    raise KilowattlasError(f"ingest returned {resp.status}: {text}")
                data = await _read_json(resp)
        except aiohttp.ClientError as err:
            raise KilowattlasError(f"cannot reach {self._base}: {err}") from err
        if not isinstance(data, dict):
            raise KilowattlasError("ingest returned an unexpected (non-object) response")
        return data

    async def revoke(self, token: str) -> None:
        """Disable this token's site server-side (called on integration removal).

        Best-effort: raises KilowattlasError on failure so the caller can log it,
        but removal should proceed regardless.
        """
        try:
            async with self._session.post(
                f"{self._base}{EP_REVOKE}",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status not in (200, 401):
                    text = (await resp.text())[:200].replace("\n", " ")
                    raise KilowattlasError(f"revoke returned {resp.status}: {text}")
                # 401 means the token was already invalid/revoked — that's fine.
        except aiohttp.ClientError as err:
            raise KilowattlasError(f"cannot reach {self._base}: {err}") from err
