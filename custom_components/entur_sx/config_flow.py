"""Config flow for Entur Situation Exchange integration."""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EnturSXApiClient
from .const import (
    CONF_CREATE_SUMMARY_SENSORS,
    CONF_DEVICE_NAME,
    CONF_LINE_TRANSPORT_MODES,
    CONF_LINES_TO_CHECK,
    CONF_OPERATOR,
    CONF_SUMMARY_ICON,
    DEFAULT_CREATE_SUMMARY_SENSORS,
    DEFAULT_SUMMARY_ICON,
    DOMAIN,
    SUMMARY_ICON_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)


def _extract_line_number(line_display_name: str) -> tuple[int, str]:
    """Extract numeric line number for sorting.
    
    Args:
        line_display_name: Display name like "925 - Bergen-Nordheimsund (bus)"
        
    Returns:
        Tuple of (line_number, original_name) for sorting
    """
    # Try to extract leading number from the display name
    match = re.match(r'^(\d+)', line_display_name)
    if match:
        return (int(match.group(1)), line_display_name)
    # If no number, sort alphabetically at the end
    return (999999, line_display_name)


class EnturSXConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Entur Situation Exchange."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._device_name: str | None = None
        self._operator: str | None = None
        self._operator_name: str | None = None
        self._selected_lines: list[str] = []
        self._operators: dict[str, str] = {}
        self._available_lines: dict[str, str] = {}
        self._create_summary_sensors: bool = DEFAULT_CREATE_SUMMARY_SENSORS
        self._summary_icon: str = DEFAULT_SUMMARY_ICON

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - select operator."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._operator = user_input[CONF_OPERATOR]
            self._operator_name = self._operators.get(self._operator, "")
            _LOGGER.debug("Selected operator: %s (name: %s)", self._operator, self._operator_name)
            
            # Move to device name step
            return await self.async_step_device_name()

        # Fetch operators
        session = async_get_clientsession(self.hass)
        self._operators = await EnturSXApiClient.async_get_operators(session)
        
        if not self._operators:
            errors["base"] = "cannot_connect"
            return self.async_abort(reason="cannot_connect")

        # Create operator options with friendly names, sorted alphabetically by name
        # Note: name already includes codespace in format "Friendly Name (CODE)"
        operator_options = [
            selector.SelectOptionDict(
                value=code,
                label=name
            )
            for code, name in sorted(self._operators.items(), key=lambda x: x[1])
        ]

        data_schema = vol.Schema(
            {
                vol.Required(CONF_OPERATOR): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=operator_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_device_name(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle device name step - shown after operator selection."""
        errors: dict[str, str] = {}

        try:
            if user_input is not None:
                self._device_name = user_input[CONF_DEVICE_NAME]
                
                # Fetch lines for the selected operator
                session = async_get_clientsession(self.hass)
                _LOGGER.debug("Fetching lines for operator: %s", self._operator)
                self._available_lines = await EnturSXApiClient.async_get_lines_for_operator(
                    session, self._operator
                )
                _LOGGER.debug("Found %d lines for operator %s", len(self._available_lines), self._operator)
                
                if not self._available_lines:
                    errors["base"] = "no_lines_found"
                else:
                    return await self.async_step_select_lines()

            # Get translated suffix for device name
            # Note: Config flows use the system language setting from Settings → System → General
            # The user's UI language preference doesn't affect config flow defaults
            language = self.hass.config.language
            
            # Determine suffix based on language
            # Norwegian (Bokmål and Nynorsk) use "Avvik", Sámi uses "Heiveheapmi"
            if language.startswith("nb") or language.startswith("nn"):
                suffix = "Avvik"
            elif language.startswith("se"):
                suffix = "Heiveheapmi"
            else:
                suffix = "Disruption"  # Default to English
            
            if self._operator_name:
                # Strip the codespace/namespace from operator name (e.g., "Skyss (SKY)" -> "Skyss")
                operator_display = self._operator_name.split(" (")[0] if " (" in self._operator_name else self._operator_name
                default_name = f"{operator_display} {suffix}"
            else:
                default_name = f"Entur {suffix}"

            # Show the form
            data_schema = vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_NAME, default=default_name
                    ): str,
                }
            )

            return self.async_show_form(
                step_id="device_name",
                data_schema=data_schema,
                errors=errors,
                description_placeholders={
                    "operator": self._operator_name or "",
                },
            )
        except Exception as err:
            _LOGGER.error("Unexpected error in device_name step: %s", err, exc_info=True)
            errors["base"] = "unknown"
            return self.async_show_form(
                step_id="device_name",
                data_schema=vol.Schema({vol.Required(CONF_DEVICE_NAME): str}),
                errors=errors,
                description_placeholders={
                    "operator": self._operator_name or "",
                },
            )

    async def async_step_select_operator(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle operator selection step (deprecated - redirects to user step)."""
        # This step is kept for backward compatibility but redirects to user step
        return await self.async_step_user(user_input)

    async def async_step_select_lines(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle line selection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_lines = user_input.get(CONF_LINES_TO_CHECK, [])
            
            if not selected_lines:
                errors[CONF_LINES_TO_CHECK] = "no_lines"
            else:
                self._selected_lines = selected_lines
                # Move to summary sensor configuration step
                return await self.async_step_summary_sensors()

        # Create line options with friendly names, sorted numerically by line number
        line_options = [
            selector.SelectOptionDict(
                value=line_id,
                label=line_data["display_name"] if isinstance(line_data, dict) else line_data
            )
            for line_id, line_data in sorted(
                self._available_lines.items(), 
                key=lambda x: _extract_line_number(x[1]["display_name"] if isinstance(x[1], dict) else x[1])
            )
        ]

        data_schema = vol.Schema(
            {
                vol.Required(CONF_LINES_TO_CHECK): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=line_options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="select_lines",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "device_name": self._device_name or "",
                "operator": f"{self._operator} - {self._operators.get(self._operator, '')}"
            },
        )

    async def async_step_summary_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle summary sensor configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._create_summary_sensors = user_input.get(
                CONF_CREATE_SUMMARY_SENSORS, DEFAULT_CREATE_SUMMARY_SENSORS
            )
            self._summary_icon = user_input.get(CONF_SUMMARY_ICON, DEFAULT_SUMMARY_ICON)
            
            # Check if this combination already exists
            unique_id = f"{self._operator}_{'-'.join(sorted(self._selected_lines))}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            
            # Create the entry
            if self._device_name:
                title = self._device_name
            else:
                title = "Entur Disruption"
            
            # Extract transport modes and submodes for selected lines
            # Store as "mode:submode" for easy splitting later
            line_transport_modes = {}
            for line_id in self._selected_lines:
                line_data = self._available_lines.get(line_id)
                if isinstance(line_data, dict):
                    mode = line_data.get("transport_mode", "bus")
                    submode = line_data.get("transport_submode", "")
                    # Store as "mode:submode" or just "mode" if no submode
                    if submode:
                        line_transport_modes[line_id] = f"{mode}:{submode}"
                    else:
                        line_transport_modes[line_id] = mode
                    # Debug log
                    if "Line:12" in line_id or "Line:1021" in line_id:
                        _LOGGER.info("[CONFIG FLOW] Storing %s -> mode='%s' submode='%s'", line_id, mode, submode or "(none)")
                # If it's a string (old format), we'll fall back to defaults
            
            return self.async_create_entry(
                title=title,
                data={
                    CONF_DEVICE_NAME: self._device_name,
                    CONF_OPERATOR: self._operator,
                    CONF_LINES_TO_CHECK: self._selected_lines,
                    CONF_LINE_TRANSPORT_MODES: line_transport_modes,
                    CONF_CREATE_SUMMARY_SENSORS: self._create_summary_sensors,
                    CONF_SUMMARY_ICON: self._summary_icon,
                },
            )

        # Create icon selector options
        icon_options = [
            selector.SelectOptionDict(value=icon, label=icon.replace("mdi:", "").replace("-", " ").title())
            for icon in SUMMARY_ICON_OPTIONS
        ]

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_CREATE_SUMMARY_SENSORS, 
                    default=DEFAULT_CREATE_SUMMARY_SENSORS
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_SUMMARY_ICON,
                    default=DEFAULT_SUMMARY_ICON
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=icon_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="summary_sensors",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "device_name": self._device_name or "Entur Disruption",
                "line_count": str(len(self._selected_lines)),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return EnturSXOptionsFlow(config_entry)


class EnturSXOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Entur Situation Exchange."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._available_lines: dict[str, str] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Extract transport modes and submodes for selected lines
            line_transport_modes = {}
            for line_id in user_input[CONF_LINES_TO_CHECK]:
                line_data = self._available_lines.get(line_id)
                if isinstance(line_data, dict):
                    mode = line_data.get("transport_mode", "bus")
                    submode = line_data.get("transport_submode", "")
                    # Store as "mode:submode" or just "mode" if no submode
                    if submode:
                        line_transport_modes[line_id] = f"{mode}:{submode}"
                    else:
                        line_transport_modes[line_id] = mode
                    # Debug log
                    if "Line:12" in line_id or "Line:1021" in line_id:
                        _LOGGER.info("[OPTIONS FLOW] Storing %s -> mode='%s' submode='%s'", line_id, mode, submode or "(none)")
            
            # Update the config entry with new line selection and transport modes
            return self.async_create_entry(
                title="",
                data={
                    CONF_LINES_TO_CHECK: user_input[CONF_LINES_TO_CHECK],
                    CONF_LINE_TRANSPORT_MODES: line_transport_modes,
                },
            )

        # Get current operator from config entry
        operator = self.config_entry.data.get(CONF_OPERATOR)
        # Check both data and options for current lines (options takes precedence)
        current_lines = self.config_entry.options.get(
            CONF_LINES_TO_CHECK,
            self.config_entry.data.get(CONF_LINES_TO_CHECK, [])
        )

        # Fetch available lines for the operator
        session = async_get_clientsession(self.hass)
        try:
            self._available_lines = await EnturSXApiClient.async_get_lines_for_operator(
                session, operator
            )
            
            if not self._available_lines:
                errors["base"] = "no_lines"
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("Error fetching lines: %s", err)
            errors["base"] = "cannot_connect"

        if errors:
            return self.async_abort(reason=errors.get("base", "unknown"))

        # Create line options with friendly names, sorted numerically by line number
        line_options = [
            selector.SelectOptionDict(
                value=line_id,
                label=line_data["display_name"] if isinstance(line_data, dict) else line_data
            )
            for line_id, line_data in sorted(
                self._available_lines.items(), 
                key=lambda x: _extract_line_number(x[1]["display_name"] if isinstance(x[1], dict) else x[1])
            )
        ]

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_LINES_TO_CHECK,
                    default=current_lines
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=line_options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "device_name": self.config_entry.data.get(CONF_DEVICE_NAME, ""),
                "operator_name": self._get_operator_display_name(operator) if self._available_lines else operator,
            },
        )
    
    def _get_operator_display_name(self, operator: str) -> str:
        """Extract operator display name from first available line."""
        if not self._available_lines:
            return operator
        
        first_line_data = self._available_lines.get(list(self._available_lines.keys())[0], "")
        # Handle both dict format (new) and string format (shouldn't happen but defensive)
        if isinstance(first_line_data, dict):
            display_name = first_line_data.get("display_name", "")
        else:
            display_name = first_line_data
        
        # Extract operator name before parenthesis (e.g., "925 - Route (bus)" -> "925 - Route ")
        return display_name.split("(")[0].strip() if display_name else operator