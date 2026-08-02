from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.const import Platform

DOMAIN = "schideron"
CONF_UID = "uid"
CONF_RELAYS = "relays"
DEFAULT_PORT = 3001
PLATFORMS = (Platform.CLIMATE, Platform.LIGHT)


@dataclass(frozen=True, slots=True)
class RelayConfig:
    device_id: int
    channel: int
    name: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RelayConfig:
        return cls(
            device_id=int(value["device_id"]),
            channel=int(value["channel"]),
            name=str(value["name"]),
        )

    def as_dict(self) -> dict[str, int | str]:
        return {
            "device_id": self.device_id,
            "channel": self.channel,
            "name": self.name,
        }


def parse_relay_mapping(value: str) -> tuple[RelayConfig, ...]:
    relays: list[RelayConfig] = []
    addresses: set[tuple[int, int]] = set()
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"line {line_number} must be device_id:channel:name")
        device_id = int(parts[0])
        channel = int(parts[1])
        name = parts[2].strip()
        if device_id < 1 or channel < 0 or not name:
            raise ValueError(f"line {line_number} contains an invalid relay")
        address = (device_id, channel)
        if address in addresses:
            raise ValueError(
                f"line {line_number} duplicates relay {device_id}:{channel}"
            )
        addresses.add(address)
        relays.append(RelayConfig(device_id, channel, name))
    return tuple(relays)


def format_relay_mapping(relays: tuple[RelayConfig, ...]) -> str:
    return "\n".join(
        f"{relay.device_id}:{relay.channel}:{relay.name}" for relay in relays
    )
