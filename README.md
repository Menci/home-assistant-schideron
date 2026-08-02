# Schideron for Home Assistant

This unofficial custom integration connects Home Assistant to a local
Schideron UControl gateway. It exposes supported air conditioners, floor
heating, and explicitly configured relay channels.

## Usage

Install this repository through HACS as a custom integration, restart Home
Assistant, then add **Schideron** from **Settings > Devices & services**.

Enter the gateway address and optionally its UID. Relay entities use one line
per channel:

```text
device_id:channel:display name
```

The integration depends on the `schideron` Python package and communicates
with the gateway entirely over the local network.
