"""Sensor platform for Entur Situation Exchange."""

from __future__ import annotations

from datetime import datetime
import hashlib
from html.parser import HTMLParser
import logging
import os
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_NAME,
    CONF_LINE_TRANSPORT_MODES,
    CONF_SUMMARY_ICON,
    DEFAULT_SUMMARY_ICON,
    DOMAIN,
    STATE_NORMAL,
    STATUS_EXPIRED,
    STATUS_OPEN,
    STATUS_PLANNED,
    normalize_language,
)
from .coordinator import EnturSXDataUpdateCoordinator
from .icon_constants import TRANSPORT_COLORS, TRANSPORT_ICONS

_LOGGER = logging.getLogger(__name__)

# Map Entur API transport mode values to icon keys
TRANSPORT_MODE_MAPPING = {
    "water": "ferry",      # Entur uses "water" for ferry routes
    "rail": "train",      # Entur uses "rail" for train routes
    "air": "plane",       # Entur uses "air" for flights
    "coach": "bus",       # Coach is a type of bus
}

# Map transport submodes to differentiate ferry types
# Based on actual values from Entur API (converted to lowercase)
TRANSPORT_SUBMODE_MAPPING = {
    "localcarferry": "carferry",           # Car ferries (discovered from Skyss)
    "regionalcarferry": "carferry",        # Regional car ferry service
    "localpassengerferry": "ferry",        # Passenger ferries (explicit, though falls back correctly)
    "regionalpassengerferry": "ferry",     # Regional passenger ferry
}

def _map_transport_mode(api_mode: str | None, api_submode: str | None = None) -> str:
    """Map Entur API transport mode and submode to icon key.
    
    Args:
        api_mode: Transport mode from Entur API (e.g., 'water', 'rail', 'bus')
        api_submode: Transport submode for more specific classification (e.g., 'localCarFerry')
        
    Returns:
        Mapped transport mode for icon lookup, defaults to 'bus'
    """
    if not api_mode:
        return "bus"
    
    mode_lower = api_mode.lower()
    submode_lower = api_submode.lower() if api_submode else ""
    
    # Check if submode provides more specific mapping (e.g., carferry vs ferry)
    if submode_lower and submode_lower in TRANSPORT_SUBMODE_MAPPING:
        return TRANSPORT_SUBMODE_MAPPING[submode_lower]
    
    # Fall back to mode-based mapping
    return TRANSPORT_MODE_MAPPING.get(mode_lower, mode_lower)


class HTMLSanitizer(HTMLParser):
    """Simple HTML parser to fix unclosed tags."""
    
    def __init__(self):
        super().__init__()
        self.result = []
        self.open_tags = []
        # Block-level tags that should be closed
        self.block_tags = {'ul', 'ol', 'li', 'div', 'p', 'table', 'tr', 'td', 'th', 'tbody', 'thead'}
        # Inline tags that should be auto-closed when parent block closes
        self.inline_tags = {'b', 'strong', 'i', 'em', 'u', 'a', 'span', 'code', 'small'}
        # Self-closing tags
        self.self_closing = {'br', 'img', 'hr', 'input', 'meta', 'link'}
        # All tags we track
        self.closing_tags = self.block_tags | self.inline_tags
    
    def handle_starttag(self, tag, attrs):
        """Handle opening tag."""
        attr_str = ''.join(f' {name}="{value}"' for name, value in attrs)
        self.result.append(f'<{tag}{attr_str}>')
        if tag in self.closing_tags:
            self.open_tags.append(tag)
    
    def handle_endtag(self, tag):
        """Handle closing tag."""
        if tag in self.closing_tags:
            # If this is a block tag closing, first close any open inline tags
            if tag in self.block_tags:
                # Close inline tags that are inside this block
                inline_to_close = []
                for open_tag in reversed(self.open_tags):
                    if open_tag == tag:
                        break
                    if open_tag in self.inline_tags:
                        inline_to_close.append(open_tag)
                
                # Close the inline tags
                for inline_tag in inline_to_close:
                    self.result.append(f'</{inline_tag}>')
                    self.open_tags.remove(inline_tag)
            
            # Now close the actual tag
            if tag in self.open_tags:
                # Close this tag and any improperly nested tags
                while self.open_tags and self.open_tags[-1] != tag:
                    unclosed = self.open_tags.pop()
                    self.result.append(f'</{unclosed}>')
                if self.open_tags and self.open_tags[-1] == tag:
                    self.open_tags.pop()
                    self.result.append(f'</{tag}>')
    
    def handle_data(self, data):
        """Handle text data."""
        self.result.append(data)
    
    def close_all(self):
        """Close any remaining open tags."""
        while self.open_tags:
            tag = self.open_tags.pop()
            self.result.append(f'</{tag}>')
    
    def get_result(self):
        """Get sanitized HTML."""
        self.close_all()
        return ''.join(self.result)


def _sanitize_html(html: str) -> str:
    """Sanitize HTML by closing any unclosed tags."""
    if not html or '<' not in html:
        return html
    
    try:
        parser = HTMLSanitizer()
        parser.feed(html)
        return parser.get_result()
    except Exception as err:
        _LOGGER.debug("Failed to sanitize HTML, returning original: %s", err)
        return html


async def _async_load_template(hass: HomeAssistant, lang: str = "no") -> str | None:
    """Load Jinja2 template asynchronously based on language."""
    try:
        # Choose template based on language
        template_name = "formatted_content_no.j2" if lang == "no" else "formatted_content.j2"
        
        # Get the directory where this module is located
        module_dir = os.path.dirname(__file__)
        template_path = os.path.join(module_dir, "templates", template_name)

        # Read template file asynchronously
        def _read_template():
            with open(template_path, encoding="utf-8") as f:
                return f.read()

        template_content = await hass.async_add_executor_job(_read_template)
        _LOGGER.debug("Successfully loaded formatted_content template")
        return template_content
    except FileNotFoundError:
        _LOGGER.warning(
            "Template file not found: %s. Formatted content will not be available.",
            template_path,
        )
        return None
    except Exception as err:
        _LOGGER.error(
            "Failed to load formatted_content template: %s. Formatted content will not be available.",
            err,
            exc_info=True,
        )
        return None


def _format_datetime_norwegian(dt: datetime) -> str:
    """Format datetime in Norwegian.
    
    Args:
        dt: datetime object to format
        
    Returns:
        Formatted string like "Mandag, 19. januar kl. 12:00"
    """
    # Norwegian day names
    norwegian_days = {
        0: "mandag",
        1: "tirsdag",
        2: "onsdag",
        3: "torsdag",
        4: "fredag",
        5: "lørdag",
        6: "søndag"
    }
    
    # Norwegian month names
    norwegian_months = {
        1: "januar",
        2: "februar",
        3: "mars",
        4: "april",
        5: "mai",
        6: "juni",
        7: "juli",
        8: "august",
        9: "september",
        10: "oktober",
        11: "november",
        12: "desember"
    }
    
    day_name = norwegian_days[dt.weekday()].capitalize()
    month_name = norwegian_months[dt.month]
    
    return f"{day_name}, {dt.day:02d}. {month_name} kl. {dt.strftime('%H:%M')}"



def _create_icon_svg(transport_mode: str) -> str:
    """Create a square transport mode icon with padding as a data URL.
    
    Creates a simple square icon suitable for entity_picture:
    - Transport mode icon scaled to 512x512 (high resolution)
    - Colored background matching transport mode
    - 25% margin (128px on all sides)
    - Total size: 768x768
    - Centered in viewBox for proper display
    
    Args:
        transport_mode: The transport mode (bus, train, ferry, etc.)
        
    Returns:
        Data URL containing the SVG icon
    """
    import base64
    import re
    
    # Get icon and color from constants
    icon_data_url = TRANSPORT_ICONS.get(transport_mode, TRANSPORT_ICONS["bus"])
    bg_color = TRANSPORT_COLORS.get(transport_mode, TRANSPORT_COLORS["bus"])
    
    # Icon dimensions and padding (high resolution for entity picture)
    ICON_SIZE = 512  # Original is 16x16, scale by 32x
    SCALE_FACTOR = 32  # Scale from 16x16 to 512x512
    PADDING = 128  # 25% margin (512 * 0.25)
    TOTAL_SIZE = ICON_SIZE + (PADDING * 2)  # 768x768
    
    # Extract the base64 content from icon data URL
    prefix = "data:image/svg+xml;base64,"
    if not icon_data_url.startswith(prefix):
        return ""
    
    icon_base64 = icon_data_url[len(prefix):].strip()
    # Normalize base64 padding (must be multiple of 4)
    icon_base64 += '=' * (4 - len(icon_base64) % 4) if len(icon_base64) % 4 else ''
    icon_svg = base64.b64decode(icon_base64).decode('utf-8')
    
    # Extract all content between <svg> and </svg> tags
    svg_content_match = re.search(r'<svg[^>]*>(.*?)</svg>', icon_svg, re.DOTALL)
    if not svg_content_match:
        return ""
    
    icon_content = svg_content_match.group(1)
    # Ensure all fill colors are white
    icon_content = re.sub(r'fill="[^"]*"', 'fill="#FFFFFF"', icon_content)
    # Remove any comments or XML declarations that might be in the content
    icon_content = re.sub(r'<!--.*?-->', '', icon_content, flags=re.DOTALL)
    icon_content = icon_content.strip()
    
    # Create square icon with padding
    # Use viewBox to add padding around the icon (centered)
    # Scale the icon content from 16x16 to 512x512
    icon_svg_output = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{TOTAL_SIZE}" height="{TOTAL_SIZE}" viewBox="-{PADDING} -{PADDING} {TOTAL_SIZE} {TOTAL_SIZE}">
  <rect x="-{PADDING}" y="-{PADDING}" width="{TOTAL_SIZE}" height="{TOTAL_SIZE}" fill="{bg_color}"/>
  <g transform="scale({SCALE_FACTOR})">
    {icon_content}
  </g>
</svg>'''
    
    # Encode as data URL
    icon_base64_out = base64.b64encode(icon_svg_output.encode('utf-8')).decode('utf-8')
    return f"data:image/svg+xml;base64,{icon_base64_out}"


def _create_badge_svg(transport_mode: str, line_name: str) -> str:
    """Create a complete SVG badge as a data URL.
    
    Creates an Entur TravelTag-style badge with:
    - Colored rounded rectangle background
    - Transport mode icon (embedded from data URL)
    - Line number text
    
    Uses proportional scaling system where all dimensions scale from text font size.
    """
    import base64
    import re
    
    # Get icon and color from constants
    icon_data_url = TRANSPORT_ICONS.get(transport_mode, TRANSPORT_ICONS["bus"])
    bg_color = TRANSPORT_COLORS.get(transport_mode, TRANSPORT_COLORS["bus"])
    
    # Proportional scaling parameters - all dimensions scale from font size
    TEXT_FONT_SIZE = 14
    BADGE_HEIGHT_SCALE = 2.25
    ICON_SIZE_SCALE = 1.875
    PADDING_LEFT_SCALE = 0.625
    PADDING_RIGHT_SCALE = 0.75
    GAP_SCALE = 0.5
    BORDER_RADIUS_SCALE = 0.375
    TEXT_VERTICAL_OFFSET_SCALE = 0.125
    ICON_VERTICAL_OFFSET_SCALE = 0
    TEXT_FONT_WEIGHT = "500"
    
    # Calculate actual dimensions
    badge_height = TEXT_FONT_SIZE * BADGE_HEIGHT_SCALE
    icon_size = TEXT_FONT_SIZE * ICON_SIZE_SCALE
    padding_left = TEXT_FONT_SIZE * PADDING_LEFT_SCALE
    padding_right = TEXT_FONT_SIZE * PADDING_RIGHT_SCALE
    gap = TEXT_FONT_SIZE * GAP_SCALE
    border_radius = TEXT_FONT_SIZE * BORDER_RADIUS_SCALE
    icon_vertical_offset = TEXT_FONT_SIZE * ICON_VERTICAL_OFFSET_SCALE
    text_vertical_offset = TEXT_FONT_SIZE * TEXT_VERTICAL_OFFSET_SCALE
    
    # Extract the base64 content from icon data URL
    icon_match = re.search(r'data:image/svg\+xml;base64,(.+)', icon_data_url)
    if not icon_match:
        return ""
    
    icon_base64 = icon_match.group(1)
    icon_svg = base64.b64decode(icon_base64).decode('utf-8')
    
    # Extract just the SVG content (remove XML declaration and svg wrapper)
    # We'll embed the paths directly
    icon_content = ""
    path_matches = re.findall(r'<(path|g|rect|circle|polygon)[^>]*>.*?</\1>|<(path|rect|circle|polygon)[^>]*/>', icon_svg, re.DOTALL)
    if path_matches:
        for match in path_matches:
            # Find the full match in original string
            for pattern in [r'<path[^>]*>.*?</path>', r'<path[^>]*/>',
                           r'<g[^>]*>.*?</g>', r'<rect[^>]*/>',
                           r'<circle[^>]*/>',  r'<polygon[^>]*/>', r'<polygon[^>]*>.*?</polygon>']:
                found = re.search(pattern, icon_svg, re.DOTALL)
                if found:
                    content = found.group(0)
                    # Replace any fill colors with white
                    content = re.sub(r'fill="[^"]*"', 'fill="#FFFFFF"', content)
                    icon_content += content
    
    # Calculate text width (approximate - proportional to font size)
    char_width_ratio = 0.5625  # Characters are ~56.25% of font size width
    text_width = len(line_name) * TEXT_FONT_SIZE * char_width_ratio
    
    # Calculate badge dimensions
    badge_width = padding_left + icon_size + gap + text_width + padding_right
    
    # Calculate vertical positions with offsets for alignment
    icon_y = (badge_height - icon_size) / 2 + icon_vertical_offset
    text_y = badge_height / 2 + text_vertical_offset
    
    # Create the complete badge SVG
    badge_svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{badge_width}" height="{badge_height}" viewBox="0 0 {badge_width} {badge_height}">
  <rect width="{badge_width}" height="{badge_height}" rx="{border_radius}" fill="{bg_color}"/>
  <g transform="translate({padding_left}, {icon_y}) scale({icon_size / 16})">
    {icon_content}
  </g>
  <text x="{padding_left + icon_size + gap}" y="{text_y}" font-family="system-ui, -apple-system, sans-serif" font-size="{TEXT_FONT_SIZE}" font-weight="{TEXT_FONT_WEIGHT}" fill="#FFFFFF" dominant-baseline="middle">{line_name}</text>
</svg>'''
    
    # Encode as data URL
    badge_base64 = base64.b64encode(badge_svg.encode('utf-8')).decode('utf-8')
    return f"data:image/svg+xml;base64,{badge_base64}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Entur SX sensors from a config entry."""
    coordinator: EnturSXDataUpdateCoordinator = hass.data[DOMAIN][
        entry.entry_id
    ]

    # Get the list of lines to monitor - merge data and options
    # (options takes precedence)
    config_data = {**entry.data, **entry.options}
    lines = config_data.get("lines_to_check", [])
    line_transport_modes = config_data.get(CONF_LINE_TRANSPORT_MODES, {})

    # Get language from HA's setting
    lang = normalize_language(hass.config.language)

    # Load Jinja2 template asynchronously for formatted_content
    template_content = await _async_load_template(hass, lang)

    # Clean up entities for lines that are no longer configured
    entity_registry = er.async_get(hass)

    # Get all entities for this config entry
    current_entities = er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    )

    # Build set of expected unique IDs
    expected_unique_ids = {
        f"{entry.entry_id}_{line_ref.replace(':', '_')}" for line_ref in lines
    }

    # Add summary sensor unique ID if enabled
    if config_data.get("create_summary_sensors", False):
        expected_unique_ids.add(f"{entry.entry_id}_summary")

    # Remove entities that are no longer configured
    for entity_entry in current_entities:
        if entity_entry.unique_id not in expected_unique_ids:
            _LOGGER.info(
                "Removing entity %s (unique_id: %s) - line no longer "
                "configured",
                entity_entry.entity_id,
                entity_entry.unique_id,
            )
            entity_registry.async_remove(entity_entry.entity_id)

    # Create a sensor for each line
    entities = []
    for line_ref in lines:
        # Clean the line name for entity ID (replace : with _)
        line_name = line_ref.replace(":", "_")
        transport_mode = line_transport_modes.get(line_ref)  # May be None for old configs
        entities.append(EnturSXSensor(coordinator, entry, line_ref, line_name, template_content, lang, transport_mode))

    # Create summary sensor if configured
    if config_data.get("create_summary_sensors", False):
        entities.append(EnturSXSummarySensor(coordinator, entry, lines, template_content, lang, line_transport_modes))

    _LOGGER.info("Setting up %d Entur SX sensors", len(entities))
    # Don't force immediate update (False) - coordinator already fetched in async_setup_entry
    # This prevents double API call during setup
    async_add_entities(entities, False)


class EnturSXSensor(
    CoordinatorEntity[EnturSXDataUpdateCoordinator], SensorEntity
):
    """Sensor for a single Entur transit line deviation status."""

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"formatted_content", "all_deviations", "entity_picture", "travel_tag"})

    def __init__(
        self,
        coordinator: EnturSXDataUpdateCoordinator,
        entry: ConfigEntry,
        line_ref: str,
        line_name: str,
        template_content: str | None,
        lang: str = "no",
        transport_mode: str | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.line_ref = line_ref
        self.line_name = line_name
        self.transport_mode = transport_mode  # Store API-provided transport mode (may be None for old configs)
        
        # Debug log for line 12
        if "Line:12" in line_ref:
            _LOGGER.info("[SENSOR INIT] Line 12 sensor created: %s -> transport_mode='%s'", line_ref, transport_mode)

        device_name = entry.data.get(CONF_DEVICE_NAME, "Entur Avvik")

        # Unique ID
        self._attr_unique_id = f"{entry.entry_id}_{line_name}"

        # Entity name is the line reference
        self._attr_name = line_ref

        # Device info - all lines belong to the same device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=device_name,
            manufacturer="Entur AS",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://entur.no",
        )

        # Icon
        self._attr_icon = "mdi:bus-alert"

        # Store pre-loaded template content
        self._template_content = template_content
        self._formatted_content_template = None
        self._lang = lang
        if template_content:
            self._compile_template(template_content)

        # Cache for icon and badge SVGs (generated once per line)
        self._icon_svg_cache = None
        self._badge_svg_cache = None

    @property
    def entity_picture(self) -> str | None:
        """Return the entity picture as transport mode icon only (square with padding)."""
        if self._icon_svg_cache is None:
            # Generate icon once and cache it
            # Use stored transport mode from API, default to "bus" if not available (old configs)
            raw_mode = self.transport_mode or "bus"
            # Split mode:submode format
            if ":" in raw_mode:
                mode, submode = raw_mode.split(":", 1)
            else:
                mode, submode = raw_mode, None
            transport_mode = _map_transport_mode(mode, submode)
            self._icon_svg_cache = _create_icon_svg(transport_mode)
        
        return self._icon_svg_cache
    
    @property
    def travel_tag(self) -> str | None:
        """Return the travel tag badge with line number (for use in templates)."""
        if self._badge_svg_cache is None:
            # Generate badge once and cache it
            # Use stored transport mode from API, default to "bus" if not available (old configs)
            raw_mode = self.transport_mode or "bus"
            # Split mode:submode format
            if ":" in raw_mode:
                mode, submode = raw_mode.split(":", 1)
            else:
                mode, submode = raw_mode, None
            transport_mode = _map_transport_mode(mode, submode)
            line_name = self.line_ref.split(":")[-1]
            self._badge_svg_cache = _create_badge_svg(transport_mode, line_name)
        
        return self._badge_svg_cache

    def _compile_template(self, template_string: str) -> None:
        """Compile Jinja2 template from string."""
        try:
            from jinja2 import Template

            self._formatted_content_template = Template(template_string)
            _LOGGER.debug("Successfully compiled formatted_content template for %s", self.line_ref)
        except Exception as err:
            _LOGGER.error(
                "Failed to compile formatted_content template: %s. Formatted content will not be available.",
                err,
                exc_info=True,
            )
            self._formatted_content_template = None

    def _generate_formatted_content(self, disruptions: list[dict], per_item: bool = False) -> str:
        """Generate formatted content from template."""
        if self._formatted_content_template is None:
            return "Template not available"
        
        if not disruptions:
            disruptions = []
        
        # Use the travel_tag (badge with line number) for templates
        travel_tag = self.travel_tag
        line_name = self.line_ref.split(":")[-1]  # Extract just the line number
        raw_mode = self.transport_mode or "bus"
        # Split mode:submode format
        if ":" in raw_mode:
            mode, submode = raw_mode.split(":", 1)
        else:
            mode, submode = raw_mode, None
        transport_mode = _map_transport_mode(mode, submode)
        
        # Prepare disruption data with badge SVG and formatted dates
        enriched_disruptions = []
        for disruption in disruptions:
            enriched = disruption.copy()
            enriched["transport_mode"] = transport_mode
            enriched["line_name"] = line_name
            enriched["travel_tag"] = travel_tag
            
            # Sanitize description HTML to fix unclosed tags from API
            if "description" in enriched and enriched["description"]:
                enriched["description"] = _sanitize_html(enriched["description"])
            
            # Format dates
            valid_from = disruption.get("valid_from", "")
            valid_to = disruption.get("valid_to", "")
            
            if valid_from:
                try:
                    from_dt = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
                    if self._lang == "no":
                        enriched["valid_from_formatted"] = _format_datetime_norwegian(from_dt)
                    else:
                        enriched["valid_from_formatted"] = from_dt.strftime("%A, %d %B at %H:%M")
                except (ValueError, AttributeError):
                    enriched["valid_from_formatted"] = valid_from
            
            if valid_to:
                try:
                    to_dt = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
                    if self._lang == "no":
                        enriched["valid_to_formatted"] = _format_datetime_norwegian(to_dt)
                    else:
                        enriched["valid_to_formatted"] = to_dt.strftime("%A, %d %B at %H:%M")
                except (ValueError, AttributeError):
                    enriched["valid_to_formatted"] = valid_to
            
            enriched_disruptions.append(enriched)
        
        try:
            return self._formatted_content_template.render(
                disruptions=enriched_disruptions,
                per_item=per_item,
            )
        except Exception as err:
            _LOGGER.error("Failed to render formatted_content template: %s", err)
            return f"Error rendering template: {err}"

    @property
    def native_value(self) -> str | None:
        """Return the state of the sensor.
        
        Returns the summary of the most recent deviation.
        """
        if not self.coordinator.data:
            return None

        line_data = self.coordinator.data.get(self.line_ref, [])
        # Empty line_data means no disruptions - will return STATE_NORMAL below

        # Filter to only active (open) disruptions that are within
        # their time window
        now_timestamp = datetime.now().timestamp()
        active_disruptions = []

        for item in line_data:
            status = item.get("status")

            # Only consider open status disruptions
            if status != STATUS_OPEN:
                continue

            # Verify the disruption is within its time window
            valid_from = item.get("valid_from")
            valid_to = item.get("valid_to")

            if not valid_from:
                continue

            try:
                start_timestamp = datetime.fromisoformat(
                    valid_from
                ).timestamp()

                # Check if disruption has started
                if now_timestamp < start_timestamp:
                    continue

                # Check if disruption has ended (if end time is specified)
                if valid_to:
                    end_timestamp = datetime.fromisoformat(
                        valid_to
                    ).timestamp()
                    if now_timestamp > end_timestamp:
                        continue

                # This disruption is currently active
                active_disruptions.append(item)
            except (ValueError, AttributeError):
                # Skip items with invalid timestamps
                continue

        # If no active disruptions, return normal state
        if not active_disruptions:
            return STATE_NORMAL

        # If there's only one active disruption, return its summary
        if len(active_disruptions) == 1:
            summary = active_disruptions[0].get("summary", "Disruption")
            # Truncate if too long
            if len(summary) > 255:
                return summary[:252] + "..."
            return summary

        # Multiple active disruptions - combine their summaries
        summaries = [
            item.get("summary", "Unknown disruption")
            for item in active_disruptions
        ]

        # Join with separator for readability
        combined = " | ".join(summaries)

        # If the combined summary is too long, truncate appropriately
        if len(combined) > 255:
            # Use count format with truncated first summary
            count_prefix = f"{len(active_disruptions)} active disruptions: "
            max_summary_len = 255 - len(count_prefix) - 3  # -3 for "..."
            first_summary = summaries[0][:max_summary_len] + "..."
            return count_prefix + first_summary

        return combined

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional attributes."""
        if not self.coordinator.data:
            return None

        line_data = self.coordinator.data.get(self.line_ref, [])
        if not line_data:
            return None

        # Get the most recent deviation
        current = line_data[0]

        attrs = {
            "valid_from": current.get("valid_from"),
            "valid_to": current.get("valid_to"),
            "summary": current.get("summary"),
            "description": current.get("description"),
            "status": current.get("status"),
            "progress": current.get("progress"),
            "line_ref": self.line_ref,
            "travel_tag": self.travel_tag,  # Full badge with line number for template use
        }

        # Always include all deviations list, each with its own formatted_content
        enriched_deviations = []
        for deviation in line_data:
            item = dict(deviation)
            item["formatted_content"] = self._generate_formatted_content([deviation], per_item=True)
            enriched_deviations.append(item)
        attrs["all_deviations"] = enriched_deviations
        attrs["total_deviations"] = len(line_data)

        if len(line_data) > 1:
            # Count by status
            status_counts = {}
            for item in line_data:
                status = item.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            attrs["deviations_by_status"] = status_counts

        # Add formatted_content for markdown card display
        attrs["formatted_content"] = self._generate_formatted_content(line_data)

        return attrs


class EnturSXSummarySensor(
    CoordinatorEntity[EnturSXDataUpdateCoordinator], RestoreEntity, SensorEntity
):
    """Summary sensor with markdown-ready content for all monitored lines."""

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({
        "markdown_active", 
        "markdown_planned",
        "_tracked_disruption_ids"  # Internal tracking, don't record in DB
    })

    def __init__(
        self,
        coordinator: EnturSXDataUpdateCoordinator,
        entry: ConfigEntry,
        lines: list[str],
        template_content: str | None,
        lang: str = "no",
        line_transport_modes: dict[str, str] | None = None,
    ) -> None:
        """Initialize the summary sensor."""
        super().__init__(coordinator)
        self.lines = lines
        self._lang = lang
        self.line_transport_modes = line_transport_modes or {}
        self._entry_id = entry.entry_id
        
        # Store pre-loaded template content
        self._template_content = template_content
        self._formatted_content_template = None
        if template_content:
            self._compile_template(template_content)
        
        # Track disruptions to detect new ones
        self._previous_disruption_ids: set[str] = set()
        self._new_disruptions_count: int = 0
        self._new_disruptions: list[dict[str, Any]] = []
        self._last_disruptions_changed: str | None = None

        device_name = entry.data.get(CONF_DEVICE_NAME, "Entur Disruption")
        config_data = {**entry.data, **entry.options}
        icon = config_data.get(CONF_SUMMARY_ICON, DEFAULT_SUMMARY_ICON)

        # Unique ID
        self._attr_unique_id = f"{entry.entry_id}_summary"

        # Entity name
        self._attr_name = "Summary"

        # Icon
        self._attr_icon = icon

        # Device info - belongs to the same device as line sensors
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=device_name,
            manufacturer="Entur AS",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://entur.no",
        )

    def _compile_template(self, template_string: str) -> None:
        """Compile Jinja2 template from string."""
        try:
            from jinja2 import Template

            self._formatted_content_template = Template(template_string)
            _LOGGER.debug("Successfully compiled formatted_content template for summary sensor")
        except Exception as err:
            _LOGGER.error(
                "Failed to compile formatted_content template: %s. Formatted content will not be available.",
                err,
                exc_info=True,
            )
            self._formatted_content_template = None

    async def async_added_to_hass(self) -> None:
        """Restore previous state to maintain disruption tracking across restarts."""
        await super().async_added_to_hass()
        
        # Attempt to restore previous state
        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes:
            # Restore previous disruption IDs to avoid marking everything as new
            stored_ids = last_state.attributes.get("_tracked_disruption_ids", [])
            if stored_ids:
                self._previous_disruption_ids = set(stored_ids)
                _LOGGER.debug(
                    "Restored %d disruption IDs from previous state for %s",
                    len(stored_ids),
                    self._entry_id
                )
            
            # Restore other tracking variables
            self._last_disruptions_changed = last_state.attributes.get(
                "last_disruptions_changed"
            )

    @property
    def native_value(self) -> int:
        """Return simple state based on total disruption count (open + planned)."""
        if not self.coordinator.data:
            return 0

        disrupted_lines = set()
        for line_ref in self.lines:
            line_data = self.coordinator.data.get(line_ref, [])
            # Empty line_data means no disruptions for this line
            if not line_data:
                continue
                
            # Check if line has any non-expired deviations
            for deviation in line_data:
                status = deviation.get("status")
                if status in (STATUS_OPEN, STATUS_PLANNED):
                    disrupted_lines.add(line_ref)
                    break  # Count this line once

        # Return numeric state for easy filtering/automation
        return len(disrupted_lines)

    def _generate_disruption_id(self, disruption: dict[str, Any], line_ref: str) -> str:
        """Generate a unique ID for a disruption.
        
        Args:
            disruption: The disruption dictionary
            line_ref: The line reference
            
        Returns:
            A unique hash string for the disruption
        """
        # Create a stable identifier from key fields
        summary = disruption.get("summary", "")
        valid_from = disruption.get("valid_from", "")
        valid_to = disruption.get("valid_to", "")
        status = disruption.get("status", "")
        
        # Combine fields and hash
        identifier = f"{line_ref}|{summary}|{valid_from}|{valid_to}|{status}"
        return hashlib.md5(identifier.encode()).hexdigest()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes.
        
        Includes markdown_active and markdown_planned with TravelTag badges for each disrupted line.
        Also tracks new disruptions since last update.
        """
        if not self.coordinator.data:
            no_planned = "Ingen planlagte avvik" if self._lang == "no" else "No planned disruptions"
            return {
                "total_lines": len(self.lines),
                "active_disruptions": 0,
                "planned_disruptions": 0,
                "normal_lines": len(self.lines),
                "markdown_active": STATE_NORMAL,
                "markdown_planned": no_planned,
                "new_disruptions_count": 0,
                "new_disruptions": [],
                "last_disruptions_changed": self._last_disruptions_changed,
            }

        active_lines = set()
        planned_lines = set()
        normal = []
        active_disruptions_list = []
        planned_disruptions_list = []
        
        # Track current disruption IDs to detect new ones
        current_disruption_ids: set[str] = set()
        current_disruptions_map: dict[str, dict[str, Any]] = {}

        for line_ref in self.lines:
            line_data = self.coordinator.data.get(line_ref, [])
            if not line_data:
                normal.append(line_ref)
                continue

            # Reuse the already-generated travel_tag from the line sensor's live state
            line_unique_id = f"{self._entry_id}_{line_ref.replace(':', '_')}"
            entity_registry = er.async_get(self.hass)
            line_entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, line_unique_id)
            line_state = self.hass.states.get(line_entity_id) if line_entity_id else None
            badge_svg = line_state.attributes.get("travel_tag") if line_state else None

            # Fallback: generate badge if line sensor state is not yet available
            if not badge_svg:
                raw_mode = self.line_transport_modes.get(line_ref) or "bus"
                if ":" in raw_mode:
                    mode, submode = raw_mode.split(":", 1)
                else:
                    mode, submode = raw_mode, None
                transport_mode = _map_transport_mode(mode, submode)
                line_display_name = line_ref.split(":")[-1]
                badge_svg = _create_badge_svg(transport_mode, line_display_name)

            transport_mode = self.line_transport_modes.get(line_ref) or "bus"
            line_display_name = line_ref.split(":")[-1]

            # Track if this line has any non-expired deviations
            has_active = False
            has_planned = False

            # Process all deviations for this line
            for deviation in line_data:
                status = deviation.get("status")

                # Skip expired deviations
                if status == STATUS_EXPIRED:
                    continue

                # Extract deviation details
                summary = deviation.get("summary", "Unknown disruption")
                description = deviation.get("description", "")
                valid_from = deviation.get("valid_from", "")
                valid_to = deviation.get("valid_to", "")
                
                # Sanitize description HTML to fix unclosed tags from API
                if description:
                    description = _sanitize_html(description)
                
                # Format dates for display
                valid_from_formatted = valid_from
                valid_to_formatted = valid_to
                
                if valid_from:
                    try:
                        from_dt = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
                        if self._lang == "no":
                            valid_from_formatted = _format_datetime_norwegian(from_dt)
                        else:
                            valid_from_formatted = from_dt.strftime("%A, %d %B at %H:%M")
                    except (ValueError, AttributeError):
                        pass
                
                if valid_to:
                    try:
                        to_dt = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
                        if self._lang == "no":
                            valid_to_formatted = _format_datetime_norwegian(to_dt)
                        else:
                            valid_to_formatted = to_dt.strftime("%A, %d %B at %H:%M")
                    except (ValueError, AttributeError):
                        pass
                
                # Generate unique ID for this disruption
                disruption_id = self._generate_disruption_id(deviation, line_ref)
                current_disruption_ids.add(disruption_id)
                
                # Build enriched disruption dict for template
                disruption_dict = {
                    'line_name': line_display_name,
                    'transport_mode': transport_mode,
                    'travel_tag': badge_svg,
                    'summary': summary,
                    'description': description,
                    'valid_from_formatted': valid_from_formatted,
                    'valid_to_formatted': valid_to_formatted,
                    'status': status,
                    'disruption_id': disruption_id,
                }
                
                # Store in map for later comparison
                current_disruptions_map[disruption_id] = disruption_dict

                # Categorize by status
                if status == STATUS_OPEN:
                    has_active = True
                    active_lines.add(line_ref)
                    active_disruptions_list.append(disruption_dict)
                elif status == STATUS_PLANNED:
                    has_planned = True
                    planned_lines.add(line_ref)
                    planned_disruptions_list.append(disruption_dict)
                else:
                    # Unknown status - include in active for safety
                    has_active = True
                    active_lines.add(line_ref)
                    active_disruptions_list.append(disruption_dict)

            # If line has no non-expired deviations, mark as normal
            if not has_active and not has_planned:
                normal.append(line_ref)

        # Detect new disruptions by comparing with previous set
        new_disruption_ids = current_disruption_ids - self._previous_disruption_ids
        
        # Update tracking variables
        if new_disruption_ids:
            # New disruptions detected
            self._new_disruptions = [
                current_disruptions_map[disruption_id]
                for disruption_id in new_disruption_ids
                if disruption_id in current_disruptions_map
            ]
            self._new_disruptions_count = len(self._new_disruptions)
            self._last_disruptions_changed = datetime.now().isoformat()
            
            _LOGGER.info(
                "Detected %d new disruption(s) for %s",
                self._new_disruptions_count,
                self._entry_id
            )
        elif current_disruption_ids != self._previous_disruption_ids:
            # Disruptions changed (some removed), but no new ones added
            # Reset new disruptions counter
            self._new_disruptions = []
            self._new_disruptions_count = 0
            self._last_disruptions_changed = datetime.now().isoformat()
        
        # Update previous disruption IDs for next comparison
        self._previous_disruption_ids = current_disruption_ids

        # Render markdown using template
        if not active_disruptions_list:
            markdown_active = STATE_NORMAL
        else:
            if self._formatted_content_template:
                try:
                    markdown_active = self._formatted_content_template.render(
                        disruptions=active_disruptions_list
                    )
                    # Add normal service footer
                    if normal or planned_lines:
                        normal_count = len(normal) + len(planned_lines)
                        if self._lang == "no":
                            markdown_active += (
                                f"\n*{normal_count} linje(r) med normal drift*\n"
                            )
                        else:
                            markdown_active += (
                                f"\n*{normal_count} line(s) with normal service*\n"
                            )
                except Exception as err:
                    _LOGGER.error("Failed to render active disruptions template: %s", err)
                    markdown_active = "Error rendering template"
            else:
                markdown_active = "Template not available"

        # Render planned disruptions using template
        if not planned_disruptions_list:
            markdown_planned = "Ingen planlagte avvik" if self._lang == "no" else "No planned disruptions"
        else:
            if self._formatted_content_template:
                try:
                    markdown_planned = self._formatted_content_template.render(
                        disruptions=planned_disruptions_list
                    )
                    # Add normal service footer
                    if normal or active_lines:
                        normal_count = len(normal) + len(active_lines)
                        if self._lang == "no":
                            markdown_planned += (
                                f"\n*{normal_count} linje(r) med normal drift*\n"
                            )
                        else:
                            markdown_planned += (
                                f"\n*{normal_count} line(s) with normal service*\n"
                            )
                except Exception as err:
                    _LOGGER.error("Failed to render planned disruptions template: %s", err)
                    markdown_planned = "Error rendering template"
            else:
                markdown_planned = "Template not available"

        return {
            "total_lines": len(self.lines),
            "active_disruptions": len(active_lines),
            "planned_disruptions": len(planned_lines),
            "normal_lines": len(normal),
            "active_line_refs": list(active_lines),
            "planned_line_refs": list(planned_lines),
            "normal_line_refs": normal,
            "markdown_active": markdown_active,
            "markdown_planned": markdown_planned,
            "new_disruptions_count": self._new_disruptions_count,
            "new_disruptions": self._new_disruptions,
            "last_disruptions_changed": self._last_disruptions_changed,
            "_tracked_disruption_ids": list(current_disruption_ids),
        }
