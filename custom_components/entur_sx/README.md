# Entur Situation Exchange Custom Integration for Home Assistant

This directory contains the Entur Situation Exchange custom integration.

## Structure

- `__init__.py` - Integration setup and entry point
- `manifest.json` - Integration metadata
- `const.py` - Constants and configuration
- `api.py` - API client for Entur Situation Exchange service
- `coordinator.py` - Data update coordinator
- `config_flow.py` - UI configuration flow
- `sensor.py` - Sensor platform implementation
- `strings.json` - UI strings
- `translations/` - Localization files
- `templates/formatted_content.j2` - English Jinja2 template for `formatted_content`
- `templates/formatted_content_no.j2` - Norwegian Jinja2 template for `formatted_content`

## Templates

The `formatted_content` attribute and the per-item `formatted_content` on each deviation in `all_deviations` are both rendered using the Jinja2 templates in the `templates/` directory.

The template receives a `per_item` boolean variable:
- `per_item=False` (default) — full bulletin: TravelTag badge + title + dates + description. Used for entity-level `formatted_content`.
- `per_item=True` — supplementary detail only: dates table, no badge, no description (the card row already shows these). Used for per-item `formatted_content` in `all_deviations`.

This flag is set internally by the integration and is transparent to end users.

## Development

To test this integration:

1. Copy the `custom_components/entur_sx` folder to your Home Assistant `custom_components` directory
2. Restart Home Assistant
3. Add the integration through the UI: Settings → Devices & Services → Add Integration → Entur Situation Exchange

## API Reference

The integration uses the Entur Situation Exchange API:
- Base URL: https://api.entur.io/realtime/v1/rest/sx
- Optional operator filter: `?datasetId={operator}`

## Features

- Async API client with aiohttp
- DataUpdateCoordinator for efficient updates (60 second polling)
- Config flow for UI-based setup
- Proper device and entity registry integration
- Deviation details as sensor attributes
- Support for multiple operators and lines
- Optional future deviation filtering
