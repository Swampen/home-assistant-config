"""The Entur Situation Exchange integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import EnturSXApiClient
from .const import DOMAIN, normalize_language
from .coordinator import EnturSXDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Sub-keys within hass.data[DOMAIN] for shared coordinator management
_SHARED_COORDINATORS = "coordinators"  # {operator: EnturSXDataUpdateCoordinator}
_COORDINATOR_REFS = "coordinator_refs"  # {operator: int}  reference count


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Entur Situation Exchange from a config entry.

    Coordinators are shared per operator so that multiple config entries for the
    same transport authority (e.g. two different line groups under SKY) result in
    only one API request per update interval instead of one per entry.
    """
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(_SHARED_COORDINATORS, {})
    hass.data[DOMAIN].setdefault(_COORDINATOR_REFS, {})

    config_data = {**entry.data, **entry.options}
    operator = config_data.get("operator")
    lang = normalize_language(hass.config.language)

    active_operators = list(hass.data[DOMAIN][_SHARED_COORDINATORS].keys())
    _LOGGER.info(
        "Setting up entry '%s' (id=%s) for operator=%s. "
        "Currently active shared coordinators: %s",
        entry.title,
        entry.entry_id,
        operator,
        active_operators or "none",
    )

    if operator not in hass.data[DOMAIN][_SHARED_COORDINATORS]:
        # First entry for this operator – create the shared coordinator
        api = EnturSXApiClient(hass=hass, operator=operator, lang=lang)
        coordinator = EnturSXDataUpdateCoordinator(hass, api)

        # Raises ConfigEntryNotReady automatically on failure
        await coordinator.async_config_entry_first_refresh()

        hass.data[DOMAIN][_SHARED_COORDINATORS][operator] = coordinator
        hass.data[DOMAIN][_COORDINATOR_REFS][operator] = 0
        _LOGGER.info(
            "Created NEW shared coordinator #%d for operator=%s (entry='%s', id=%s)",
            coordinator._coordinator_id,
            operator,
            entry.title,
            entry.entry_id,
        )
    else:
        existing = hass.data[DOMAIN][_SHARED_COORDINATORS][operator]
        ref_count = hass.data[DOMAIN][_COORDINATOR_REFS][operator]
        _LOGGER.info(
            "Reusing shared coordinator #%d for operator=%s (entry='%s', id=%s, ref_count=%d)",
            existing._coordinator_id,
            operator,
            entry.title,
            entry.entry_id,
            ref_count,
        )

    # Increment reference count and expose coordinator under entry_id
    hass.data[DOMAIN][_COORDINATOR_REFS][operator] += 1
    hass.data[DOMAIN][entry.entry_id] = hass.data[DOMAIN][_SHARED_COORDINATORS][operator]

    # Sanity check: warn if somehow multiple entries are all pointing to the same operator
    # (harmless with shared coordinator, but worth knowing about)
    ref_count_now = hass.data[DOMAIN][_COORDINATOR_REFS][operator]
    if ref_count_now > 1:
        _LOGGER.warning(
            "Operator %s now has %d config entries sharing coordinator #%d. "
            "This is expected only if you have multiple SKY line groups configured.",
            operator,
            ref_count_now,
            hass.data[DOMAIN][_SHARED_COORDINATORS][operator]._coordinator_id,
        )

    # Forward setup to sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener for options flow
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)

        config_data = {**entry.data, **entry.options}
        operator = config_data.get("operator")

        refs = hass.data[DOMAIN].get(_COORDINATOR_REFS, {})
        if operator in refs:
            refs[operator] -= 1
            _LOGGER.info(
                "Unloaded entry '%s' (id=%s) for operator=%s. Remaining refs: %d",
                entry.title,
                entry.entry_id,
                operator,
                refs[operator],
            )
            if refs[operator] <= 0:
                # Last entry for this operator – remove the shared coordinator
                coordinator = hass.data[DOMAIN][_SHARED_COORDINATORS].pop(operator, None)
                refs.pop(operator, None)
                _LOGGER.info(
                    "Removed shared coordinator #%d for operator=%s (no more entries)",
                    coordinator._coordinator_id if coordinator else -1,
                    operator,
                )

    return unload_ok
