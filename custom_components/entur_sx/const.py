"""Constants for the Entur Situation Exchange integration."""
DOMAIN = "entur_sx"

# Configuration
CONF_OPERATOR = "operator"
CONF_LINES_TO_CHECK = "lines_to_check"
CONF_LINE_TRANSPORT_MODES = "line_transport_modes"  # Dict mapping line_id -> transport_mode from API
CONF_DEVICE_NAME = "device_name"
CONF_CREATE_SUMMARY_SENSORS = "create_summary_sensors"
CONF_SUMMARY_ICON = "summary_icon"

# Defaults
DEFAULT_DEVICE_NAME = "Entur Disruption"  # Fallback only, translations preferred
DEFAULT_CREATE_SUMMARY_SENSORS = True
DEFAULT_SUMMARY_ICON = "mdi:bus-alert"
DEFAULT_LANG = "en"  # Default to English if HA language cannot be determined
UPDATE_INTERVAL = 120  # seconds - exact 2× the 60s server window; ensures every request lands in its own fresh window


def normalize_language(language_code: str | None) -> str:
    """Normalize Home Assistant language code to 'no' or 'en'.
    
    Args:
        language_code: Language code from Home Assistant (e.g., 'en-US', 'nb-NO', 'nn-NO')
        
    Returns:
        'no' for Norwegian variants (Bokmål, Nynorsk, Sámi), 'en' for all others
    """
    if not language_code:
        return DEFAULT_LANG
    
    language_code = language_code.lower()
    
    # Norwegian: Bokmål (nb), Nynorsk (nn), or Sámi (se)
    if language_code.startswith(("nb", "nn", "se")):
        return "no"
    
    # Default to English for all other languages
    return "en"

# Back-off configuration for rate limiting
BACKOFF_INITIAL = 120  # 2 minutes on first throttle
BACKOFF_MULTIPLIER = 2.5  # Exponential increase
BACKOFF_MAX = 600  # Max 10 minutes
BACKOFF_RESET_AFTER = 1800  # Reset to normal after 30 min of success

# Icon options for summary sensors
SUMMARY_ICON_OPTIONS = [
    "mdi:bus-alert",
    "mdi:bus",
    "mdi:tram",
    "mdi:train",
    "mdi:ferry",
    "mdi:subway-variant",
    "mdi:alert-circle",
    "mdi:transit-connection-variant",
]

# API
API_BASE_URL = "https://api.entur.io/realtime/v1/rest/sx"
API_GRAPHQL_URL = "https://api.entur.io/journey-planner/v3/graphql"

# States
STATE_NORMAL = "Normal service"

# Status values
STATUS_PLANNED = "planned"
STATUS_OPEN = "open"
STATUS_EXPIRED = "expired"

# Codespace to friendly name mapping
#
# WHY THIS MAPPING EXISTS:
# The SIRI-SX API uses 3-letter codespaces (e.g., "SKY", "SOF") to identify regional
# transport authorities. However, Entur's public APIs don't provide a way to map these
# codespaces to user-friendly regional authority names:
#
# - The GraphQL operators/authorities APIs return individual transport company names
#   (e.g., "GulenSkyss AS", "Fjord1 ASA") not regional authority names
# - The Organizations API v3 has the data we need (/v3/codespaces endpoint) but it's
#   an internal/partner API requiring authentication (returns 401 for public access)
# - The official codespace documentation (https://enturas.atlassian.net/wiki/spaces/PUBLIC/pages/637370434/)
#   is the authoritative public source for regional transport authority names
#
# This mapping bridges that gap, allowing users to select their region by name
# (e.g., "Sogn og Fjordane") rather than having to know the codespace identifier.
#
# Source: Official Entur codespace documentation + dynamic discovery from operators API
# The codespace (3-letter code) is what's used in SIRI-SX datasetId parameter
CODESPACE_NAMES = {
    # Major regional transport authorities
    "AKT": "Agder Kollektivtrafikk",
    "ATB": "AtB",
    "BRA": "Brakar",
    "GOA": "Go-Ahead Norge",
    "INN": "Innlandstrafikk",
    "KOL": "Kolumbus",
    "MOR": "FRAM",
    "NBU": "Flybussen Connect",
    "OST": "Østfold kollektivtrafikk",
    "RUT": "Ruter",
    "SJN": "SJ Nord",
    "SKY": "Skyss",
    "SOF": "Sogn og Fjordane",  # Kringom regional authority
    "TEL": "Farte",
    "TRO": "Troms fylkestrafikk",
    "VKT": "VKT",
    "VYB": "Vy Bus4You",
    "VYG": "Vy",
    "VYX": "Vy Buss",
    
    # Codespaces found in SIRI-SX but not fully mapped
    "CTS": "CTS",
    "GCO": "GCO",
    "NSB": "NSB",
}

