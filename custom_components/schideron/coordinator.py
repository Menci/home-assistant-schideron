from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from schideron.client import SchideronClient
from schideron.exceptions import SchideronError
from schideron.models import (
    ConditionerKey,
    ConditionerStatus,
    HeatingKey,
    HeatingStatus,
    LightingKey,
    LightingStatus,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SchideronData:
    conditioners: dict[ConditionerKey, ConditionerStatus]
    heatings: dict[HeatingKey, HeatingStatus]
    lighting: dict[LightingKey, LightingStatus]


class SchideronCoordinator(DataUpdateCoordinator[SchideronData]):
    def __init__(self, hass: HomeAssistant, client: SchideronClient, uid: str) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )
        self.client = client
        self.uid = uid
        self._unsubscribe = client.subscribe(self._handle_status)
        self._snapshot_scheduled = False

    def snapshot(self) -> SchideronData:
        return SchideronData(
            conditioners=dict(self.client.conditioners),
            heatings=dict(self.client.heatings),
            lighting=dict(self.client.lighting),
        )

    def _handle_status(self, _: object) -> None:
        if self._snapshot_scheduled:
            return
        self._snapshot_scheduled = True
        self.hass.loop.call_soon(self._publish_snapshot)

    @callback
    def _publish_snapshot(self) -> None:
        self._snapshot_scheduled = False
        self.async_set_updated_data(self.snapshot())

    async def _async_update_data(self) -> SchideronData:
        try:
            if not self.client.connected:
                await self.client.connect()
            await self.client.get_pi_status(self.uid)
        except (OSError, SchideronError, TimeoutError) as error:
            raise UpdateFailed(str(error)) from error
        return self.snapshot()

    async def async_shutdown(self) -> None:
        self._unsubscribe()
        await self.client.disconnect()
