"""DataUpdateCoordinator for Entur Situation Exchange."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EnturSXApiClient
from .const import (
    BACKOFF_INITIAL,
    BACKOFF_MAX,
    BACKOFF_MULTIPLIER,
    BACKOFF_RESET_AFTER,
    DOMAIN,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
_DISRUPTION_LOGGER = logging.getLogger(f"{__name__}.disruptions")


class EnturSXDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching Entur SX data."""

    # Class-level counter so each coordinator gets a unique ID for log tracing
    _instance_counter: int = 0

    def __init__(self, hass: HomeAssistant, api: EnturSXApiClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.api = api
        # Set the session for the API client
        session = async_get_clientsession(hass)
        self.api.set_session(session)

        # Unique ID for log tracing (helps detect accidental duplicate coordinators)
        EnturSXDataUpdateCoordinator._instance_counter += 1
        self._coordinator_id = EnturSXDataUpdateCoordinator._instance_counter
        _LOGGER.info(
            "[Coordinator #%d] Created for operator=%s (total ever created: %d)",
            self._coordinator_id,
            api._operator or "ALL",
            self._coordinator_id,
        )
        
        # Track active disruptions to detect changes
        self._previous_disruptions: dict[str, set[str]] = {}
        self._first_disruption_check: bool = True
        
        # Throttle/back-off management
        self._throttle_count = 0
        self._last_success_time: datetime | None = None
        self._in_backoff = False
        self._cached_data: dict[str, Any] | None = None
        
        # Request history tracking (for diagnostics when throttled)
        self._request_history: deque = deque(maxlen=10)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Entur API with smart throttle handling."""
        request_start = datetime.now()
        try:
            data = await self.api.async_get_deviations()
            request_end = datetime.now()
            duration_ms = (request_end - request_start).total_seconds() * 1000

            # Log successful request in history
            self._request_history.append({
                "timestamp": request_start.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "duration_ms": round(duration_ms, 1),
                "status": "success",
                "lines_count": len(data),
                "provider": self.api._operator or "ALL",
                "coordinator_id": self._coordinator_id,
            })
            
            _LOGGER.debug("Fetched data for %d lines", len(data))
            
            # Success - reset throttle tracking
            if self._in_backoff:
                _LOGGER.info(
                    "API access recovered after throttling (back-off ended)"
                )
                self._in_backoff = False
                # Reset update interval to normal
                self.update_interval = timedelta(seconds=UPDATE_INTERVAL)
                _LOGGER.debug("Update interval reset to %d seconds", UPDATE_INTERVAL)
            
            # Reset throttle count if enough time has passed
            if self._last_success_time:
                time_since_success = (
                    datetime.now() - self._last_success_time
                ).total_seconds()
                if time_since_success > BACKOFF_RESET_AFTER:
                    if self._throttle_count > 0:
                        _LOGGER.debug(
                            "Resetting throttle count after %d seconds of success",
                            time_since_success,
                        )
                    self._throttle_count = 0
            
            self._last_success_time = datetime.now()
            self._cached_data = data
            
            # Track disruption changes
            self._track_disruption_changes(data)
            
            return data
        except aiohttp.ClientResponseError as err:
            request_end = datetime.now()
            duration_ms = (request_end - request_start).total_seconds() * 1000
            
            # Log failed request in history
            self._request_history.append({
                "timestamp": request_start.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "duration_ms": round(duration_ms, 1),
                "status": f"error_{err.status}",
                "error": str(err.message) if hasattr(err, 'message') else str(err),
                "provider": self.api._operator or "ALL",
                "coordinator_id": self._coordinator_id,
            })
            
            if err.status == 429:
                # Rate limit hit - apply back-off and dump history
                return await self._handle_throttle(err)
            _LOGGER.error("Error updating Entur SX data: %s", err)
            raise UpdateFailed(f"Error communicating with Entur API: {err}") from err
        except Exception as err:
            _LOGGER.error("Error updating Entur SX data: %s", err)
            raise UpdateFailed(f"Error communicating with Entur API: {err}") from err
    
    async def _handle_throttle(self, err: aiohttp.ClientResponseError) -> dict[str, Any]:
        """Handle 429 rate limit with exponential back-off and state preservation.
        
        Returns cached data if available to keep sensors alive during cooldown.
        """
        self._throttle_count += 1
        self._in_backoff = True
        
        # Calculate back-off time with exponential increase
        backoff_time = min(
            BACKOFF_INITIAL * (BACKOFF_MULTIPLIER ** (self._throttle_count - 1)),
            BACKOFF_MAX,
        )
        
        # Log the throttle event with request history
        _LOGGER.warning(
            "Rate limit hit (429 Too Many Requests) - throttle event #%d. "
            "Applying %d second back-off. Will retry after cooldown. "
            "Preserving last known state to keep sensors available.",
            self._throttle_count,
            backoff_time,
        )
        
        # Dump request history to help diagnose what led to throttling
        if self._request_history:
            _LOGGER.warning(
                "[Coordinator #%d] Request history (last %d requests leading to throttle):",
                self._coordinator_id,
                len(self._request_history),
            )
            for i, req in enumerate(self._request_history, 1):
                _LOGGER.warning(
                    "  #%d: %s | coordinator=#%s | provider=%s | status=%s | duration=%sms%s",
                    i,
                    req.get("timestamp", "unknown"),
                    req.get("coordinator_id", "?"),
                    req.get("provider", "?"),
                    req.get("status", "unknown"),
                    req.get("duration_ms", "?"),
                    f" | lines={req['lines_count']}" if "lines_count" in req else f" | error={req.get('error', 'unknown')}",
                )
        else:
            _LOGGER.warning("[Coordinator #%d] No request history available (first request?)", self._coordinator_id)
        
        # Adjust update interval for back-off period
        self.update_interval = timedelta(seconds=backoff_time)
        
        # Return cached data to preserve sensor state
        if self._cached_data is not None:
            _LOGGER.debug(
                "Returning cached data with %d lines during back-off",
                len(self._cached_data),
            )
            return self._cached_data
        
        # No cache available - this should only happen on first fetch
        _LOGGER.error(
            "No cached data available during throttle. Sensors may become unavailable."
        )
        raise UpdateFailed(f"Rate limit exceeded and no cached data: {err}") from err
    
    def _track_disruption_changes(self, data: dict[str, Any]) -> None:
        """Track when disruptions appear and disappear."""
        current_disruptions: dict[str, set[str]] = {}
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Build current state
        for line_ref, deviations in data.items():
            if not deviations:
                current_disruptions[line_ref] = set()
                continue
            
            # Track unique disruption IDs (summary + status is unique enough)
            disruption_ids = set()
            for dev in deviations:
                summary = dev.get("summary", "")
                status = dev.get("status", "")
                valid_from = dev.get("valid_from", "")
                # Create unique ID
                disruption_id = f"{summary[:50]}|{status}|{valid_from}"
                disruption_ids.add(disruption_id)
            
            current_disruptions[line_ref] = disruption_ids
        
        # On the first check _previous_disruptions is empty, so every existing
        # disruption would appear as "new". Log at DEBUG on startup to avoid
        # flooding the log with disruptions that were already known.
        log = _DISRUPTION_LOGGER.debug if self._first_disruption_check else _DISRUPTION_LOGGER.info

        # Compare with previous state
        for line_ref in current_disruptions:
            current = current_disruptions.get(line_ref, set())
            previous = self._previous_disruptions.get(line_ref, set())
            
            # New disruptions appeared
            new_disruptions = current - previous
            for disruption_id in new_disruptions:
                parts = disruption_id.split("|")
                summary = parts[0] if len(parts) > 0 else "Unknown"
                status = parts[1] if len(parts) > 1 else "unknown"
                valid_from = parts[2] if len(parts) > 2 else ""
                
                log(
                    "[%s] NEW disruption on %s (status: %s) - %s - "
                    "valid from: %s",
                    timestamp,
                    line_ref,
                    status,
                    summary,
                    valid_from,
                )
            
            # Disruptions disappeared
            removed_disruptions = previous - current
            for disruption_id in removed_disruptions:
                parts = disruption_id.split("|")
                summary = parts[0] if len(parts) > 0 else "Unknown"
                status = parts[1] if len(parts) > 1 else "unknown"
                
                log(
                    "[%s] REMOVED disruption from %s (was: %s) - %s",
                    timestamp,
                    line_ref,
                    status,
                    summary,
                )
        
        # Update previous state
        self._previous_disruptions = current_disruptions
        self._first_disruption_check = False
