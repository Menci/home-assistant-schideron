from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import SchideronCoordinator


class SchideronEntity(CoordinatorEntity[SchideronCoordinator]):
    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.client.connected
