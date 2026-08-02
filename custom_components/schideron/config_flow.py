from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers import selector

from schideron.client import SchideronClient
from schideron.discovery import discover_gateways
from schideron.exceptions import SchideronError

from .const import (
    CONF_RELAYS,
    CONF_UID,
    DEFAULT_PORT,
    DOMAIN,
    RelayConfig,
    format_relay_mapping,
    parse_relay_mapping,
)


class CannotConnect(Exception):
    pass


async def validate_input(user_input: dict[str, Any]) -> tuple[dict[str, Any], str]:
    host = str(user_input[CONF_HOST]).strip()
    port = int(user_input[CONF_PORT])
    uid = str(user_input[CONF_UID]).strip()
    try:
        if not uid:
            gateways = await discover_gateways(timeout=2, broadcast_address=host)
            matching_uid = next(
                (
                    gateway.uid
                    for gateway in gateways
                    if gateway.host == host
                    and gateway.port == port
                    and gateway.uid is not None
                ),
                None,
            )
            if matching_uid is None:
                raise CannotConnect("gateway UID discovery failed")
            uid = matching_uid

        client = SchideronClient(host, port=port)
        await client.connect()
        try:
            statuses = await client.get_pi_status(uid)
            if not statuses:
                raise CannotConnect("gateway returned no status")
        finally:
            await client.disconnect()
    except (OSError, SchideronError, TimeoutError) as error:
        raise CannotConnect from error

    return (
        {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_UID: uid,
            CONF_RELAYS: [
                relay.as_dict()
                for relay in parse_relay_mapping(user_input[CONF_RELAYS])
            ],
        },
        f"Schideron {uid.removeprefix('schideron_')[:8]}",
    )


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Optional(CONF_UID, default=defaults.get(CONF_UID, "")): str,
            vol.Optional(
                CONF_RELAYS,
                default=defaults.get(CONF_RELAYS, ""),
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        }
    )


class SchideronConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data, title = await validate_input(user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except (TypeError, ValueError):
                errors["base"] = "invalid_relay_mapping"
            else:
                await self.async_set_unique_id(data[CONF_UID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=title, data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        relays = tuple(RelayConfig.from_dict(item) for item in entry.data[CONF_RELAYS])
        defaults = {
            **entry.data,
            CONF_RELAYS: format_relay_mapping(relays),
        }
        if user_input is not None:
            try:
                data, _ = await validate_input(user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except (TypeError, ValueError):
                errors["base"] = "invalid_relay_mapping"
            else:
                if data[CONF_UID] != entry.unique_id:
                    return self.async_abort(reason="wrong_gateway")
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(user_input or defaults),
            errors=errors,
        )
