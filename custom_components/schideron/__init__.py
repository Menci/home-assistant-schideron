from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from schideron.client import SchideronClient

from .const import CONF_RELAYS, CONF_UID, DOMAIN, PLATFORMS, RelayConfig
from .coordinator import SchideronCoordinator


@dataclass(slots=True)
class SchideronRuntimeData:
    coordinator: SchideronCoordinator
    relays: tuple[RelayConfig, ...]


type SchideronConfigEntry = ConfigEntry[SchideronRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: SchideronConfigEntry) -> bool:
    client = SchideronClient(entry.data[CONF_HOST], port=entry.data[CONF_PORT])
    coordinator = SchideronCoordinator(hass, client, entry.data[CONF_UID])
    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = SchideronRuntimeData(
            coordinator=coordinator,
            relays=tuple(
                RelayConfig.from_dict(item) for item in entry.data[CONF_RELAYS]
            ),
        )
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.data[CONF_UID])},
            manufacturer="Schideron",
            model="SMART-CORE",
            name="Schideron Gateway",
        )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_shutdown()
        raise

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SchideronConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.coordinator.async_shutdown()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: SchideronConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
