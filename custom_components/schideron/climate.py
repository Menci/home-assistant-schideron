from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Awaitable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from schideron.constants import (
    ConditionerFanSpeed,
    ConditionerMode,
    ConditionerPower,
    HeatingPower,
)
from schideron.exceptions import SchideronError
from schideron.models import (
    Conditioner,
    ConditionerKey,
    ConditionerStatus,
    Heating,
    HeatingCommandResult,
    HeatingKey,
    HeatingStatus,
)

from . import SchideronConfigEntry
from .const import DOMAIN
from .coordinator import SchideronCoordinator
from .entity import SchideronEntity

MODE_TO_HA = {
    ConditionerMode.COOL: HVACMode.COOL,
    ConditionerMode.HEAT: HVACMode.HEAT,
    ConditionerMode.FAN: HVACMode.FAN_ONLY,
    ConditionerMode.DEHUMIDIFICATION: HVACMode.DRY,
}
HA_TO_MODE = {value: key for key, value in MODE_TO_HA.items()}
FAN_TO_HA = {
    ConditionerFanSpeed.HIGH: "high",
    ConditionerFanSpeed.MEDIUM: "medium",
    ConditionerFanSpeed.LOW: "low",
    ConditionerFanSpeed.AUTO: "auto",
}
HA_TO_FAN = {value: key for key, value in FAN_TO_HA.items()}

_POWER = "power"
_MODE = "mode"
_TEMPERATURE = "temperature"
_FAN = "fan"
_INTENT_DEBOUNCE_SECONDS = 0.3

type DesiredValue = (
    ConditionerPower | ConditionerMode | ConditionerFanSpeed | HeatingPower | int
)
ResultT = TypeVar("ResultT", ConditionerStatus, HeatingCommandResult)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PendingChange:
    value: DesiredValue
    revision: int
    sent: bool = False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchideronConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    known_conditioners: set[ConditionerKey] = set()
    known_heatings: set[HeatingKey] = set()

    @callback
    def discover_entities() -> None:
        new_conditioners = coordinator.data.conditioners.keys() - known_conditioners
        new_heatings = coordinator.data.heatings.keys() - known_heatings
        known_conditioners.update(new_conditioners)
        known_heatings.update(new_heatings)
        async_add_entities(
            [
                *(
                    SchideronConditionerClimate(coordinator, key)
                    for key in new_conditioners
                ),
                *(SchideronHeatingClimate(coordinator, key) for key in new_heatings),
            ]
        )

    discover_entities()
    entry.async_on_unload(coordinator.async_add_listener(discover_entities))


def _device_identifier(
    uid: str,
    kind: str,
    key: ConditionerKey | HeatingKey,
) -> str:
    serial_port = (
        "N" if key.serial_port is None else f"S{len(key.serial_port)}:{key.serial_port}"
    )
    return f"{uid}-{kind}-{key.device_name}-{key.device_id}-{key.pb}-{serial_port}"


class SchideronOptimisticClimate(
    SchideronEntity,
    ClimateEntity,
    Generic[ResultT],
):
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1

    def __init__(
        self,
        coordinator: SchideronCoordinator,
        *,
        device_id: int,
        device_kind: str,
        command_order: tuple[str, ...],
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._device_kind = device_kind
        self._command_order = command_order
        self._pending: dict[str, PendingChange] = {}
        self._revision = 0
        self._intent_changed = asyncio.Event()
        self._reconcile_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def async_will_remove_from_hass(self) -> None:
        self._stopping = True
        task = self._reconcile_task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await super().async_will_remove_from_hass()

    @callback
    def _queue_intent(self, changes: Mapping[str, DesiredValue]) -> None:
        self._revision += 1
        for field, value in changes.items():
            self._pending[field] = PendingChange(value, self._revision)
        self._intent_changed.set()
        self.async_write_ha_state()
        self._ensure_reconciler()

    @callback
    def _ensure_reconciler(self) -> None:
        if self._stopping:
            return
        task = self._reconcile_task
        if task is None or task.done():
            self._reconcile_task = self.hass.async_create_task(
                self._async_reconcile(),
                f"schideron_reconcile_{self._device_kind}_{self._device_id}",
            )

    async def _async_reconcile(self) -> None:
        try:
            while self._pending:
                revision = self._revision
                self._intent_changed.clear()
                await asyncio.sleep(_INTENT_DEBOUNCE_SECONDS)
                if self._intent_changed.is_set() or revision != self._revision:
                    continue

                while self._pending and not self._intent_changed.is_set():
                    field = next(
                        (
                            field
                            for field in self._command_order
                            if field in self._pending and not self._pending[field].sent
                        ),
                        None,
                    )
                    if field is None:
                        return
                    change = self._pending[field]
                    try:
                        result = await self._command_for(field, change.value)
                    except SchideronError:
                        _LOGGER.exception(
                            "Failed to reconcile %s %s %s revision %s",
                            self._device_kind,
                            self._device_id,
                            field,
                            change.revision,
                        )
                        self._discard_failed_revision(change.revision)
                        self.async_write_ha_state()
                        break
                    confirmed = self._confirmed_values(result)
                    if confirmed is None:
                        self._mark_sent(field, change)
                        continue
                    self.coordinator.async_set_updated_data(self.coordinator.snapshot())
                    self._clear_confirmed_values(confirmed)
                    self.async_write_ha_state()
        finally:
            self._reconcile_task = None

    @abstractmethod
    def _command_for(self, field: str, value: DesiredValue) -> Awaitable[ResultT]:
        raise NotImplementedError

    @abstractmethod
    def _confirmed_values(
        self,
        result: ResultT,
    ) -> Mapping[str, DesiredValue] | None:
        raise NotImplementedError

    def _clear_confirmed_values(self, confirmed: Mapping[str, DesiredValue]) -> None:
        for field, change in tuple(self._pending.items()):
            if field in confirmed and change.value == confirmed[field]:
                del self._pending[field]

    def _mark_sent(self, field: str, change: PendingChange) -> None:
        if self._pending.get(field) == change:
            self._pending[field] = PendingChange(
                value=change.value,
                revision=change.revision,
                sent=True,
            )

    def _discard_failed_revision(self, revision: int) -> None:
        for field, change in tuple(self._pending.items()):
            if change.revision <= revision:
                del self._pending[field]


class SchideronConditionerClimate(SchideronOptimisticClimate[ConditionerStatus]):
    _attr_icon = "mdi:air-conditioner"
    _attr_min_temp = 17
    _attr_max_temp = 28
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: SchideronCoordinator, key: ConditionerKey) -> None:
        super().__init__(
            coordinator,
            device_id=key.device_id,
            device_kind="conditioner",
            command_order=(_POWER, _MODE, _TEMPERATURE, _FAN),
        )
        self._key = key
        self._conditioner = Conditioner.from_status(self.status)
        self._attr_hvac_modes = [
            HVACMode.OFF,
            HVACMode.COOL,
            HVACMode.HEAT,
            HVACMode.FAN_ONLY,
            HVACMode.DRY,
        ]
        self._attr_fan_modes = list(HA_TO_FAN)
        self._device_identifier = _device_identifier(
            coordinator.uid, "conditioner", key
        )
        self._attr_unique_id = self._device_identifier

    @property
    def status(self) -> ConditionerStatus:
        return self.coordinator.data.conditioners[self._key]

    @property
    def available(self) -> bool:
        status = self.coordinator.data.conditioners.get(self._key)
        return (
            status is not None
            and super().available
            and self.coordinator.client.is_available(status)
        )

    @property
    def device_info(self) -> DeviceInfo:
        status = self.status
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_identifier)},
            manufacturer=status.brand,
            model=status.product_model,
            name=f"Air conditioner {self._key.device_id}",
            via_device=(DOMAIN, self.coordinator.uid),
        )

    @property
    def current_temperature(self) -> float | None:
        return self.status.current_temperature

    @property
    def target_temperature(self) -> float:
        pending = self._pending.get(_TEMPERATURE)
        if pending is not None:
            return float(pending.value)
        return self.status.target_temperature

    @property
    def hvac_mode(self) -> HVACMode:
        pending_power = self._pending.get(_POWER)
        power = (
            cast(ConditionerPower, pending_power.value)
            if pending_power is not None
            else self.status.power
        )
        if power is ConditionerPower.OFF:
            return HVACMode.OFF
        pending_mode = self._pending.get(_MODE)
        mode = (
            cast(ConditionerMode, pending_mode.value)
            if pending_mode is not None
            else self.status.mode
        )
        return MODE_TO_HA[mode]

    @property
    def fan_mode(self) -> str:
        pending = self._pending.get(_FAN)
        speed = (
            cast(ConditionerFanSpeed, pending.value)
            if pending is not None
            else self.status.fan_speed
        )
        return FAN_TO_HA[speed]

    async def async_turn_on(self) -> None:
        self._queue_intent({_POWER: ConditionerPower.ON})

    async def async_turn_off(self) -> None:
        self._queue_intent({_POWER: ConditionerPower.OFF})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode is HVACMode.OFF:
            await self.async_turn_off()
            return
        mode = HA_TO_MODE.get(hvac_mode)
        if mode is None:
            raise ServiceValidationError(f"unsupported HVAC mode: {hvac_mode}")
        self._queue_intent({_POWER: ConditionerPower.ON, _MODE: mode})

    async def async_set_temperature(self, **kwargs: Any) -> None:
        value = kwargs.get(ATTR_TEMPERATURE)
        if value is None or float(value) % 1:
            raise ServiceValidationError("target temperature must be a whole number")
        self._queue_intent({_TEMPERATURE: int(value)})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        speed = HA_TO_FAN.get(fan_mode)
        if speed is None:
            raise ServiceValidationError(f"unsupported fan mode: {fan_mode}")
        self._queue_intent({_FAN: speed})

    def _command_for(
        self,
        field: str,
        value: DesiredValue,
    ) -> Awaitable[ConditionerStatus]:
        if field == _POWER:
            return (
                self.coordinator.client.conditioner_on(self._conditioner)
                if value is ConditionerPower.ON
                else self.coordinator.client.conditioner_off(self._conditioner)
            )
        if field == _MODE:
            return self.coordinator.client.set_conditioner_mode(
                self._conditioner,
                cast(ConditionerMode, value),
            )
        if field == _TEMPERATURE:
            return self.coordinator.client.set_conditioner_temperature(
                self._conditioner,
                value,
            )
        if field == _FAN:
            return self.coordinator.client.set_conditioner_fan_speed(
                self._conditioner,
                cast(ConditionerFanSpeed, value),
            )
        raise ValueError(f"unsupported conditioner command field: {field}")

    def _confirmed_values(
        self,
        status: ConditionerStatus,
    ) -> Mapping[str, DesiredValue]:
        return {
            _POWER: status.power,
            _MODE: status.mode,
            _TEMPERATURE: status.set_temperature // 10,
            _FAN: status.fan_speed,
        }


class SchideronHeatingClimate(SchideronOptimisticClimate[HeatingCommandResult]):
    _attr_icon = "mdi:heating-coil"
    _attr_min_temp = 5
    _attr_max_temp = 35
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: SchideronCoordinator, key: HeatingKey) -> None:
        super().__init__(
            coordinator,
            device_id=key.device_id,
            device_kind="heating",
            command_order=(_POWER, _TEMPERATURE),
        )
        self._key = key
        self._heating = Heating.from_status(self.status)
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        self._device_identifier = _device_identifier(coordinator.uid, "heating", key)
        self._attr_unique_id = self._device_identifier

    @property
    def status(self) -> HeatingStatus:
        return self.coordinator.data.heatings[self._key]

    @property
    def available(self) -> bool:
        status = self.coordinator.data.heatings.get(self._key)
        return status is not None and super().available

    @property
    def device_info(self) -> DeviceInfo:
        status = self.status
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_identifier)},
            manufacturer=status.brand,
            model=status.product_model,
            name=f"Floor heating {self._key.device_id}",
            via_device=(DOMAIN, self.coordinator.uid),
        )

    @property
    def current_temperature(self) -> float | None:
        return self.status.current_temperature if self.status.authoritative else None

    @property
    def target_temperature(self) -> float | None:
        pending = self._pending.get(_TEMPERATURE)
        if pending is not None:
            return float(pending.value)
        return self.status.target_temperature if self.status.authoritative else None

    @property
    def hvac_mode(self) -> HVACMode | None:
        pending = self._pending.get(_POWER)
        if pending is None and not self.status.authoritative:
            return None
        power = (
            cast(HeatingPower, pending.value)
            if pending is not None
            else self.status.power
        )
        return HVACMode.HEAT if power is HeatingPower.ON else HVACMode.OFF

    async def async_turn_on(self) -> None:
        self._queue_intent({_POWER: HeatingPower.ON})

    async def async_turn_off(self) -> None:
        self._queue_intent({_POWER: HeatingPower.OFF})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode is HVACMode.OFF:
            await self.async_turn_off()
            return
        if hvac_mode is not HVACMode.HEAT:
            raise ServiceValidationError(f"unsupported HVAC mode: {hvac_mode}")
        await self.async_turn_on()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        value = kwargs.get(ATTR_TEMPERATURE)
        if value is None or float(value) % 1:
            raise ServiceValidationError("target temperature must be a whole number")
        temperature = int(value)
        if not self.min_temp <= temperature <= self.max_temp:
            raise ServiceValidationError(
                f"target temperature must be between {self.min_temp} and {self.max_temp}"
            )
        self._queue_intent({_TEMPERATURE: temperature})

    def _handle_coordinator_update(self) -> None:
        if self.status.authoritative:
            self._clear_confirmed_values(self._status_values(self.status))
        super()._handle_coordinator_update()

    def _command_for(
        self,
        field: str,
        value: DesiredValue,
    ) -> Awaitable[HeatingCommandResult]:
        if field == _POWER:
            return (
                self.coordinator.client.heating_on(self._heating)
                if value is HeatingPower.ON
                else self.coordinator.client.heating_off(self._heating)
            )
        if field == _TEMPERATURE:
            return self.coordinator.client.set_heating_temperature(
                self._heating,
                value,
            )
        raise ValueError(f"unsupported heating command field: {field}")

    def _confirmed_values(
        self,
        result: HeatingCommandResult,
    ) -> Mapping[str, DesiredValue] | None:
        if not result.confirmed or result.status is None:
            return None
        return self._status_values(result.status)

    @staticmethod
    def _status_values(status: HeatingStatus) -> Mapping[str, DesiredValue]:
        confirmed: dict[str, DesiredValue] = {_POWER: status.power}
        if status.set_temperature is not None:
            confirmed[_TEMPERATURE] = status.set_temperature // 10
        return confirmed
