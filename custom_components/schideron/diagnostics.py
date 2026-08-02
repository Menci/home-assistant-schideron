from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant

from . import SchideronConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: SchideronConfigEntry,
) -> dict[str, Any]:
    coordinator = entry.runtime_data.coordinator
    return {
        "config_entry": {
            "domain": entry.domain,
            "source": entry.source,
            "version": entry.version,
            "minor_version": entry.minor_version,
            "port": entry.data[CONF_PORT],
        },
        "connected": coordinator.client.connected,
        "relay_status_connected": coordinator.client.relay_status_connected,
        "unverified_relay_bank_count": len(coordinator.client.relay_banks),
        "conditioner_count": len(coordinator.data.conditioners),
        "heating_count": len(coordinator.data.heatings),
        "observed_relay_count": len(coordinator.data.lighting),
        "configured_relay_count": len(entry.runtime_data.relays),
    }
