"""Config flow for the Kilowattlas Solar Sharing integration.

Device-linking wizard:
  1. Request a device code and show the user the user_code + verification URL.
  2. Poll in the background until they approve it on the web.
  3. Ask which power sensor to share.
"""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    AuthorizationExpired,
    AuthorizationPending,
    DeviceCode,
    KilowattlasClient,
    KilowattlasError,
)
from .const import (
    CONF_API_BASE,
    CONF_POWER_SENSOR,
    CONF_SITE_ID,
    CONF_TOKEN,
    DEFAULT_CONFIG,
    DOMAIN,
    capability_tier,
    resolve_api_base,
    resolve_config,
)


class KilowattlasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the linking wizard."""

    VERSION = 1

    def __init__(self) -> None:
        self._client: KilowattlasClient | None = None
        self._device: DeviceCode | None = None
        self._token: str | None = None
        self._site_id: int | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Request a device code and send the user off to approve it.

        The API base always points at production (a developer can override it
        with the KILOWATTLAS_API_BASE env var); end users never see or choose an
        address. This step just shows a "start linking" confirmation, then
        requests the code when the user continues — so a transient connection
        error surfaces as a friendly form error, not a 500.
        """
        api_base = resolve_api_base()

        if user_input is None:
            # A confirm-only form (no fields) so the network request happens on
            # submit rather than on first render.
            return self.async_show_form(step_id="user")

        session = async_get_clientsession(self.hass)
        self._client = KilowattlasClient(session, api_base)
        self._api_base = api_base

        # Report a provisional stream capability up front (phase-2 readiness).
        # This is only the rate we START at, since the sensor hasn't been chosen
        # yet — the coordinator measures what the sensor actually delivers and
        # reports the real figure via the donor block on each push, which is what
        # the backend should trust.
        initial_interval = resolve_config(DEFAULT_CONFIG, None)[
            "max_sample_interval_seconds"
        ]
        capability = {
            "sample_capability": capability_tier(initial_interval),
            "sample_interval_seconds": initial_interval,
        }

        try:
            # Volunteer HA's own position so the web form can prefill coordinates.
            self._device = await self._client.request_device_code(
                self.hass.config.latitude,
                self.hass.config.longitude,
                capability=capability,
            )
        except KilowattlasError:
            return self.async_show_form(
                step_id="user", errors={"base": "cannot_connect"}
            )

        return await self.async_step_link()

    async def async_step_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the code + link, and poll for approval when the user continues."""
        assert self._client is not None and self._device is not None

        if user_input is None:
            return self.async_show_form(
                step_id="link",
                description_placeholders={
                    "user_code": self._device.user_code,
                    "verification_uri": (
                        f"{self._device.verification_uri}"
                        f"?user_code={self._device.user_code}"
                    ),
                },
            )

        # User pressed Submit — poll a few times for approval. Both timings are
        # server-supplied, so clamp them: a floor on the interval prevents a
        # tight poll loop, and a ceiling on the deadline prevents a hostile
        # server from making this step block for an unbounded time.
        deadline = min(max(self._device.expires_in, 30), 900)
        interval = min(max(self._device.interval, 3), 30)
        waited = 0
        while waited < deadline:
            try:
                grant = await self._client.poll_token(self._device.device_code)
                self._token = grant.token
                self._site_id = grant.site_id
                return await self.async_step_sensor()
            except AuthorizationPending:
                await asyncio.sleep(interval)
                waited += interval
            except AuthorizationExpired:
                return self.async_abort(reason="expired")
            except KilowattlasError:
                return self.async_abort(reason="cannot_connect")

        # Not approved within the poll window — let them retry from this step.
        return self.async_show_form(
            step_id="link",
            errors={"base": "not_approved_yet"},
            description_placeholders={
                "user_code": self._device.user_code,
                "verification_uri": (
                    f"{self._device.verification_uri}"
                    f"?user_code={self._device.user_code}"
                ),
            },
        )

    async def async_step_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the power sensor to share."""
        if user_input is not None:
            await self.async_set_unique_id(str(self._site_id))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Kilowattlas Solar",
                data={
                    CONF_API_BASE: self._api_base,
                    CONF_TOKEN: self._token,
                    CONF_SITE_ID: self._site_id,
                    CONF_POWER_SENSOR: user_input[CONF_POWER_SENSOR],
                },
            )

        # Offer power sensors (device_class: power). Most homes have exactly one.
        return self.async_show_form(
            step_id="sensor",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POWER_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor", device_class="power"
                        )
                    )
                }
            ),
        )
