from __future__ import annotations

from typing import Any

from homeassistant.components.light import LightEntity
from homeassistant.components.light.const import ColorMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from schideron.exceptions import SchideronError
from schideron.models import LightingChannel, LightingKey

from . import SchideronConfigEntry
from .const import DOMAIN, RelayConfig
from .coordinator import SchideronCoordinator
from .entity import SchideronEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchideronConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SchideronRelayLight(coordinator, relay) for relay in entry.runtime_data.relays
    )


class SchideronRelayLight(SchideronEntity, LightEntity):
    _attr_icon = "mdi:lightbulb"
    _attr_color_mode = ColorMode.ONOFF

    def __init__(self, coordinator: SchideronCoordinator, relay: RelayConfig) -> None:
        super().__init__(coordinator)
        self._relay = relay
        self._channel = LightingChannel(relay.device_id, relay.channel)
        self._key = LightingKey(relay.device_id, relay.channel)
        self._attr_name = relay.name
        self._attr_unique_id = (
            f"{coordinator.uid}-relay-{relay.device_id}-{relay.channel}"
        )
        self._attr_is_on: bool | None = None
        self._attr_supported_color_modes = {ColorMode.ONOFF}
        self._state_known = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={
                (DOMAIN, f"{self.coordinator.uid}-relay-{self._relay.device_id}")
            },
            manufacturer="Schideron",
            name=f"Relay controller {self._relay.device_id}",
            via_device=(DOMAIN, self.coordinator.uid),
        )

    @property
    def assumed_state(self) -> bool:
        return not self._state_known

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        observed = self.coordinator.data.lighting.get(self._key)
        if observed is not None:
            self._attr_is_on = observed.is_on
            self._state_known = True

    def _handle_coordinator_update(self) -> None:
        observed = self.coordinator.data.lighting.get(self._key)
        if observed is not None:
            self._attr_is_on = observed.is_on
            self._state_known = True
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_state(False)

    async def _async_set_state(self, is_on: bool) -> None:
        previous_is_on = self._attr_is_on
        previous_state_known = self._state_known
        self._attr_is_on = is_on
        self._state_known = True
        self.async_write_ha_state()

        try:
            command = (
                self.coordinator.client.lighting_on(self._channel)
                if is_on
                else self.coordinator.client.lighting_off(self._channel)
            )
            result = await command
        except SchideronError as error:
            self._attr_is_on = previous_is_on
            self._state_known = previous_state_known
            self.async_write_ha_state()
            raise HomeAssistantError(str(error)) from error
        except Exception:
            self._attr_is_on = previous_is_on
            self._state_known = previous_state_known
            self.async_write_ha_state()
            raise

        self._attr_is_on = result.is_on
        self.async_write_ha_state()
