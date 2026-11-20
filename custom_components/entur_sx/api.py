"""API client for Entur Situation Exchange."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING
from email.utils import parsedate_to_datetime

import aiohttp
import async_timeout

from .const import API_BASE_URL, API_GRAPHQL_URL, CODESPACE_NAMES, STATE_NORMAL, STATUS_EXPIRED, STATUS_PLANNED, STATUS_OPEN

from homeassistant.helpers import instance_id

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Global quota manager key for hass.data
QUOTA_MANAGER_KEY = "entur_sx_quota_manager"


class GlobalQuotaManager:
    """Centralized quota manager shared across all API clients.

    Tracks the server-reported rate-limit headers and predicts remaining quota
    locally (by decrementing after each call) so we never need a separate
    rolling-window counter.  The server's authoritative values are refreshed
    on every successful response via update_from_headers().
    """
    
    def __init__(self):
        """Initialize global quota manager."""
        # Server-reported quota (updated on every response, decremented locally after each call)
        self.allowed: int | None = None
        self.available: int | None = None
        self.used: int | None = None
        self.expiry_time: str | None = None
        self.expiry_datetime: datetime | None = None
        self.range: str | None = None  # e.g. "per-minute"
        self.window_seconds: float = 60.0  # Calibrated from rate-limit-range header

        # Spike-arrest: track time of last request (100 ms minimum between calls)
        self._last_request_time: float = 0.0
        self.min_request_interval: float = 0.1

        # Lock to prevent race conditions across async requests
        self._lock = asyncio.Lock()

        _LOGGER.info("Global Entur API quota manager initialized")
    
    def update_from_headers(self, headers: dict) -> None:
        """Update rate limit info from response headers.
        
        Args:
            headers: Response headers containing rate-limit-* fields
        """
        if "rate-limit-allowed" in headers:
            self.allowed = int(headers["rate-limit-allowed"])
        if "rate-limit-available" in headers:
            server_available = int(headers["rate-limit-available"])
            if self.available is not None and server_available != self.available:
                _LOGGER.warning(
                    "[GLOBAL QUOTA] Server corrected quota: %s → %s/%s",
                    self.available,
                    server_available,
                    self.allowed,
                )
            self.available = server_available
        if "rate-limit-used" in headers:
            self.used = int(headers["rate-limit-used"])
        if "rate-limit-expiry-time" in headers:
            self.expiry_time = headers["rate-limit-expiry-time"]
            # Parse the expiry time to datetime for calculations
            try:
                parsed_dt = parsedate_to_datetime(self.expiry_time)
                # Ensure the datetime is timezone-aware and normalized to UTC
                if parsed_dt.tzinfo is None:
                    # If naive, assume UTC
                    self.expiry_datetime = parsed_dt.replace(tzinfo=timezone.utc)
                else:
                    # Convert to UTC to ensure consistent timezone handling
                    self.expiry_datetime = parsed_dt.astimezone(timezone.utc)
            except Exception as err:
                _LOGGER.debug("Could not parse expiry time '%s': %s", self.expiry_time, err)
                self.expiry_datetime = None
        if "rate-limit-range" in headers:
            raw_range = headers["rate-limit-range"].strip('"')
            if raw_range != self.range:
                _LOGGER.info("API rate limit window reported by server: %s", raw_range)
            self.range = raw_range
            # Calibrate our internal rolling window to match the server's window.
            # "per-minute" → 60s, "per-second" → 1s, "per-hour" → 3600s, etc.
            range_map = {
                "per-minute": 60.0,
                "per-second": 1.0,
                "per-hour": 3600.0,
            }
            if raw_range in range_map:
                self.window_seconds = range_map[raw_range]

        # Log rate limit info for monitoring
        if self.available is not None and self.allowed is not None:
            range_label = f" [{self.range}]" if self.range else ""
            if self.available <= 1:
                _LOGGER.warning(
                    "API rate limit headers show low quota: %d/%d requests remaining until %s%s",
                    self.available,
                    self.allowed,
                    self.expiry_time or "unknown",
                    range_label,
                )
            elif self.available <= 2:
                _LOGGER.info(
                    "API rate limit headers: %d/%d requests remaining until %s%s",
                    self.available,
                    self.allowed,
                    self.expiry_time or "unknown",
                    range_label,
                )
            _LOGGER.debug(
                "API rate limit headers: %d/%d requests remaining until %s%s",
                self.available,
                self.allowed,
                self.expiry_time or "unknown",
                range_label,
            )
    

    def get_seconds_until_quota_available(self) -> float:
        """Calculate seconds to wait before the next request can be made.

        Two rules:
        - Spike-arrest: minimum 100 ms between requests.
        - Quota exhausted: back off until server expiry time (+1 s margin).

        Returns:
            Seconds to wait, or 0.0 if a request can go immediately.
        """
        # Spike-arrest
        spike_wait = 0.0
        if self._last_request_time > 0:
            elapsed = time.time() - self._last_request_time
            if elapsed < self.min_request_interval:
                spike_wait = self.min_request_interval - elapsed

        # Quota exhausted: wait until server window expires
        quota_wait = 0.0
        if self.available is not None and self.available <= 0:
            if self.expiry_datetime:
                seconds = (self.expiry_datetime - datetime.now(timezone.utc)).total_seconds()
                quota_wait = max(0.0, seconds + 1.0)
            else:
                quota_wait = self.window_seconds + 1.0

        return max(spike_wait, quota_wait)
    
    def can_make_request(self) -> tuple[bool, str]:
        """Check if a request can be made right now.

        Two rules:
        - Spike-arrest: reject if last request was less than 100 ms ago.
        - Quota: block if server-reported (or predicted) available is 0,
          unless the expiry time has already passed.

        Returns:
            Tuple of (can_proceed, reason_if_not)
        """
        # Rule 1: spike-arrest
        time_since_last = self.get_time_since_last_request()
        if time_since_last < self.min_request_interval:
            wait_ms = (self.min_request_interval - time_since_last) * 1000
            return False, f"spike arrest: last request {time_since_last*1000:.0f}ms ago, wait {wait_ms:.0f}ms"

        # Rule 2: quota exhausted — only block while the server window is still active
        if self.available is not None and self.available <= 0:
            if self.expiry_datetime is None or datetime.now(timezone.utc) < self.expiry_datetime:
                wait_time = self.get_seconds_until_quota_available()
                return False, f"quota exhausted ({self.available}/{self.allowed}), wait {wait_time:.1f}s"

        return True, ""
    
    async def wait_for_quota(self) -> None:
        """Wait until request quota is available."""
        wait_time = self.get_seconds_until_quota_available()

        if wait_time > 0:
            _LOGGER.debug(
                "[GLOBAL QUOTA] Quota: %s/%s remaining. Waiting %.1f seconds for restoration.",
                self.available,
                self.allowed,
                wait_time,
            )
            await asyncio.sleep(wait_time)
    
    def record_request(self, operator: str | None = None) -> None:
        """Record that a request was made.

        Updates spike-arrest timer and decrements our local copy of
        server-reported available quota by 1.  The server will confirm the
        exact value on the next successful response via update_from_headers().

        Args:
            operator: Optional operator/provider for logging
        """
        self._last_request_time = time.time()

        # If the server window has expired since our last request, the server will
        # have reset its counter to `allowed`. Reset our local copy to match before
        # decrementing, so our prediction stays accurate.
        if (self.expiry_datetime is not None and self.allowed is not None
                and datetime.now(timezone.utc) > self.expiry_datetime):
            self.available = self.allowed

        before = self.available
        if self.available is not None:
            self.available = max(0, self.available - 1)

        _LOGGER.debug(
            "[GLOBAL QUOTA] Request recorded for %s. Quota: %s → %s/%s (predicted)",
            operator or "unknown",
            before if before is not None else "?",
            self.available if self.available is not None else "?",
            self.allowed if self.allowed is not None else "?",
        )
    
    def get_time_since_last_request(self) -> float:
        """Get seconds since last request.

        Returns:
            Seconds elapsed since last request, or infinity if no previous request
        """
        if self._last_request_time == 0.0:
            return float('inf')
        return time.time() - self._last_request_time


def get_quota_manager(hass: HomeAssistant) -> GlobalQuotaManager:
    """Get or create the global quota manager singleton.
    
    Args:
        hass: Home Assistant instance
        
    Returns:
        The global quota manager instance
    """
    if QUOTA_MANAGER_KEY not in hass.data:
        hass.data[QUOTA_MANAGER_KEY] = GlobalQuotaManager()
    return hass.data[QUOTA_MANAGER_KEY]


class EnturSXApiClient:
    """API client for Entur Situation Exchange."""

    def __init__(
        self,
        hass: HomeAssistant,
        operator: str | None = None,
        lang: str = "no",
    ) -> None:
        """Initialize the API client.
        
        Args:
            hass: Home Assistant instance
            operator: Codespace (e.g., "SKY", "SOF")
            lang: Preferred language code ("no" or "en")
        """
        self._hass = hass
        self._operator = operator
        self._lang = lang
        self._session: aiohttp.ClientSession | None = None
        self._client_name: str | None = None

        # Get the global quota manager singleton
        self._quota_manager = get_quota_manager(hass)

        # The operator is now the codespace directly (e.g., "SKY", "SOF")
        # This is what we use for the SIRI-SX datasetId parameter
        self._operator_code = operator if operator else None
        
        if operator:
            self._service_url = f"{API_BASE_URL}?datasetId={operator}"
        else:
            self._service_url = API_BASE_URL

    def set_session(self, session: aiohttp.ClientSession) -> None:
        """Set the aiohttp session."""
        self._session = session

    def _select_text_by_language(self, text_objects: list[dict[str, Any]]) -> str:
        """Select best text from array based on language preference.
        
        The Entur API returns Summary and Description as arrays of objects with:
        - 'value': the text content
        - Language codes in various formats:
          * 'xml:lang' attribute (proper XML syntax, e.g., xml:lang="EN")
          * 'lang' field (lowercase)
          * 'Language' field (mixed case, sometimes inconsistent)
        
        Args:
            text_objects: List of text objects from API (e.g., Summary or Description array)
            
        Returns:
            Selected text string, or empty string if no text available
        """
        if not text_objects:
            return ""
        
        # If only one text available, use it regardless of language
        if len(text_objects) == 1:
            return text_objects[0].get("value", "")
        
        # Try to find text in preferred language
        preferred_lang = self._lang.lower()
        for text_obj in text_objects:
            # Check multiple possible language field names (API is inconsistent):
            # - 'xml:lang' (proper XML attribute syntax)
            # - 'lang' (alternative field name)
            # - 'Language' (another variant)
            lang_code = (
                text_obj.get("xml:lang", "") or 
                text_obj.get("lang", "") or 
                text_obj.get("Language", "")
            ).lower()
            
            if lang_code and lang_code.startswith(preferred_lang):
                return text_obj.get("value", "")
        
        # If preferred language not found:
        # - For Norwegian users: Just use first text (likely Norwegian anyway)
        # - For English users: Try to find any English text before falling back
        if preferred_lang != "no":
            for text_obj in text_objects:
                lang_code = (
                    text_obj.get("xml:lang", "") or 
                    text_obj.get("lang", "") or 
                    text_obj.get("Language", "")
                ).lower()
                if lang_code and lang_code.startswith("en"):
                    return text_obj.get("value", "")
        
        # Fall back to first available text
        return text_objects[0].get("value", "")

    async def async_get_deviations(self) -> dict[str, Any]:
        """Fetch deviation data for configured lines.
        
        Handles pagination when MoreData=true using requestorId to retrieve
        all situations in extreme weather scenarios (flooding, heavy snow).
        Uses smart quota management: allows rapid requests (burst) during pagination
        as long as staying within 4 requests per 60-second rolling window.
        
        Returns:
            Dict mapping line reference to list of deviations with status, e.g.
            {"SKY:Line:1": [{"valid_from": "...", "valid_to": "...", "summary": "...", 
                             "description": "...", "status": "open"}]}
        """
        if not self._session:
            _LOGGER.error("Session not set")
            return {}

        if self._client_name is None:
            uid = await instance_id.async_get(self._hass)
            self._client_name = f"homeassistant-entur-sx-{uid[:8]}"
            _LOGGER.info("Entur API client name: %s", self._client_name)

        headers = {
            "Content-Type": "application/json",
            "ET-Client-Name": self._client_name,
        }

        # Generate requestorId for pagination tracking
        requestor_id = str(uuid.uuid4())
        all_situations = []
        page_count = 0
        max_pages = 20  # Safety limit to prevent infinite loops
        data = None  # Initialize to handle early breaks

        try:
            # Timeout: one quota-wait cycle (≤60s) + page requests. 90s is sufficient.
            async with async_timeout.timeout(90):
                while page_count < max_pages:
                    # CRITICAL: Wait for quota outside lock, then record atomically
                    # Lock must be released during sleep to prevent request queue buildup
                    
                    # Wait loop: Keep checking and sleeping until quota available
                    while True:
                        async with self._quota_manager._lock:
                            can_proceed, reason = self._quota_manager.can_make_request()

                            if can_proceed:
                                # Capture time-since-last BEFORE recording this request
                                # so the value is meaningful (gap from previous, not self)
                                time_since_prev = self._quota_manager.get_time_since_last_request()
                                # Capture pre-decrement available for error reporting
                                available_before = self._quota_manager.available
                                # Quota available! Record this request atomically
                                page_count += 1
                                self._quota_manager.record_request(self._operator_code)
                                break  # Exit wait loop, proceed to make request

                            # No quota, calculate wait time
                            wait_time = self._quota_manager.get_seconds_until_quota_available()

                            if page_count == 0:
                                # First page - no data yet. Don't sleep here: a long
                                # asyncio.sleep during async_config_entry_first_refresh can
                                # be cancelled by HA, producing CancelledError and marking
                                # the entry as cancelled. Fail fast instead so the
                                # coordinator returns cached data and HA retries on schedule.
                                _LOGGER.debug(
                                    "[%s] Quota exhausted before first request (%s, %.1fs wait). "
                                    "Failing fast - coordinator will retry on next interval.",
                                    self._operator_code or "ALL",
                                    reason,
                                    wait_time,
                                )
                                raise RuntimeError(
                                    f"API quota exhausted, retry in {wait_time:.0f}s"
                                )

                            _LOGGER.debug(
                                "[%s] Rate limit before page %d: %s. API quota: %s/%s remaining. Waiting %.1f seconds.",
                                self._operator_code or "ALL",
                                page_count + 1,
                                reason,
                                self._quota_manager.available,
                                self._quota_manager.allowed,
                                wait_time,
                            )
                        
                        # Sleep OUTSIDE the lock so other tasks can check/use quota
                        await asyncio.sleep(wait_time)
                        _LOGGER.debug(
                            "[%s] Quota wait complete, re-checking availability for page %d",
                            self._operator_code or "ALL",
                            page_count + 1
                        )
                    
                    # Add requestorId parameter for pagination
                    url = f"{self._service_url}&requestorId={requestor_id}" if "?" in self._service_url else f"{self._service_url}?requestorId={requestor_id}"
                    
                    async with self._session.get(url, headers=headers) as response:
                        # Check for 429 rate limit errors
                        if response.status == 429:
                            # Update quota manager FIRST so expiry_datetime is recorded.
                            # Per Entur policy: do NOT retry until Rate-Limit-Expiry-Time.
                            # Recording the headers here ensures can_make_request() will
                            # block the next coordinator cycle until the window resets.
                            self._quota_manager.update_from_headers(response.headers)
                            _LOGGER.error(
                                "⚠️  RATE LIMIT ERROR (429) - API rejected request for %s. "
                                "Page %d of pagination. Time since previous request: %.1fs. "
                                "API quota before call: %s/%s remaining. "
                                "Will not retry until expiry: %s. "
                                "Global quota manager should have prevented this - please report!",
                                self._operator_code or "ALL",
                                page_count,
                                time_since_prev,
                                available_before,
                                self._quota_manager.allowed,
                                self._quota_manager.expiry_time or "unknown",
                            )
                            if "retry-after" in response.headers:
                                _LOGGER.error("  Retry-After header: %s", response.headers["retry-after"])
                            response.raise_for_status()
                        
                        response.raise_for_status()
                        
                        # Update quota manager from response headers
                        self._quota_manager.update_from_headers(response.headers)
                        
                        # API returns JSON but with incorrect content-type header sometimes
                        # Use text() and json.loads() to handle this
                        text = await response.text()
                        import json
                        data = json.loads(text)

                        # Extract situations from this page
                        service_delivery = data.get("Siri", {}).get("ServiceDelivery", {})
                        sx_delivery = service_delivery.get("SituationExchangeDelivery", [])
                        
                        if sx_delivery:
                            situations_obj = sx_delivery[0].get("Situations", {})
                            situations = situations_obj.get("PtSituationElement", [])
                            
                            # Ensure it's a list
                            if not isinstance(situations, list):
                                situations = [situations]
                            
                            all_situations.extend(situations)
                            
                            rate_info = ""
                            if self._quota_manager.available is not None:
                                rate_info = f" API headers: {self._quota_manager.available}/{self._quota_manager.allowed} remaining"
                            
                            _LOGGER.debug(
                                "[%s] Retrieved page %d: %d situations (total so far: %d).%s",
                                self._operator_code or "ALL",
                                page_count,
                                len(situations),
                                len(all_situations),
                                rate_info
                            )

                        # Check for MoreData flag
                        more_data = service_delivery.get("MoreData", False)
                        
                        if more_data:
                            quota_str = f" API quota: {self._quota_manager.available}/{self._quota_manager.allowed}" if self._quota_manager.available is not None else ""

                            _LOGGER.info(
                                "[%s] MoreData=true, continuing pagination. Page %d: %d situations (total: %d).%s",
                                self._operator_code or "ALL",
                                page_count,
                                len(situations),
                                len(all_situations),
                                quota_str,
                            )
                            # Continue loop to fetch next page with same requestorId
                        else:
                            # No more data, we're done
                            if page_count > 1:
                                _LOGGER.info(
                                    "Pagination complete: retrieved %d situations across %d pages. Operator: %s",
                                    len(all_situations),
                                    page_count,
                                    self._operator_code or "all"
                                )
                            break
                
                if page_count >= max_pages:
                    _LOGGER.warning(
                        "Reached maximum page limit (%d pages) - some disruptions may be missing. "
                        "Retrieved %d situations. Operator: %s",
                        max_pages,
                        len(all_situations),
                        self._operator_code or "all"
                    )

                # Handle case where no data was retrieved (rate limit exhausted, etc.)
                if data is None:
                    _LOGGER.warning(
                        "No data retrieved from API (page_count=%d). Returning empty result. Operator: %s",
                        page_count,
                        self._operator_code or "all"
                    )
                    return {}

                # Reconstruct response with all situations
                if page_count > 1:
                    # Multiple pages - merge all situations
                    data["Siri"]["ServiceDelivery"]["SituationExchangeDelivery"][0]["Situations"]["PtSituationElement"] = all_situations
                    # Set MoreData to false since we've fetched everything
                    data["Siri"]["ServiceDelivery"]["MoreData"] = False
                
                return self._parse_response(data)

        except asyncio.TimeoutError as err:
            _LOGGER.error("Timeout fetching data from Entur API: %s", err)
            raise
        except aiohttp.ClientError as err:
            _LOGGER.error("Error fetching data from Entur API: %s", err)
            raise
        except Exception as err:
            _LOGGER.error("Unexpected error fetching Entur data: %s", err, exc_info=True)
            raise

    def _parse_response(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse the Entur API response.

        Processes ALL situations for the operator in a single pass and returns
        every affected line found. Sensors each filter by their own line_ref,
        so no per-line API requests are needed regardless of how many config
        entries share this operator.

        Args:
            data: JSON response from Entur API

        Returns:
            Dict mapping ALL found line references to their list of situations
        """
        allitems_dict: dict[str, list[dict[str, Any]]] = {}
        now_timestamp = datetime.now(timezone.utc).timestamp()

        try:
            siri = data.get("Siri", {})
            service_delivery = siri.get("ServiceDelivery", {})
            sx_delivery = service_delivery.get("SituationExchangeDelivery", [])

            for sed in sx_delivery:
                situations = sed.get("Situations", {})
                elements = situations.get("PtSituationElement", [])

                for element in elements:
                    progress = element.get("Progress", "")
                    progress_lower = progress.lower()

                    affects = element.get("Affects", {})
                    networks = affects.get("Networks")

                    if not networks:
                        continue

                    # Get validity period
                    validity_periods = element.get("ValidityPeriod", [])
                    if not validity_periods:
                        continue

                    validity_period = validity_periods[0]
                    start_time = validity_period.get("StartTime")
                    end_time = validity_period.get("EndTime")

                    if not start_time:
                        continue

                    # Determine status based on time and Progress field
                    start_timestamp = datetime.fromisoformat(start_time).timestamp()

                    if now_timestamp < start_timestamp:
                        status = STATUS_PLANNED
                    elif end_time:
                        end_timestamp = datetime.fromisoformat(end_time).timestamp()
                        if now_timestamp > end_timestamp:
                            status = STATUS_EXPIRED
                        else:
                            status = STATUS_EXPIRED if progress_lower == "closed" else STATUS_OPEN
                    else:
                        status = STATUS_EXPIRED if progress_lower == "closed" else STATUS_OPEN

                    # Extract text once per situation (not per line)
                    summaries = element.get("Summary", [])
                    descriptions = element.get("Description", [])
                    if not isinstance(summaries, list):
                        summaries = [summaries] if summaries else []
                    if not isinstance(descriptions, list):
                        descriptions = [descriptions] if descriptions else []

                    summary = self._select_text_by_language(summaries) or STATE_NORMAL
                    description = self._select_text_by_language(descriptions) or STATE_NORMAL

                    situation_entry = {
                        "valid_from": start_time,
                        "valid_to": end_time,
                        "summary": summary,
                        "description": description,
                        "status": status,
                        "progress": progress_lower,
                    }

                    # Add this situation to every line it affects
                    affected_networks = networks.get("AffectedNetwork", [])
                    for an in affected_networks:
                        affected_lines = an.get("AffectedLine", [])
                        if not affected_lines:
                            continue

                        for affected_line in affected_lines:
                            line_ref_obj = affected_line.get("LineRef", {})
                            line_ref = line_ref_obj.get("value")

                            if not line_ref:
                                continue

                            if line_ref not in allitems_dict:
                                allitems_dict[line_ref] = []
                            allitems_dict[line_ref].append(situation_entry)

        except Exception as err:
            _LOGGER.error("Error parsing API response: %s", err, exc_info=True)

        # Sort each line: OPEN first, then PLANNED, then EXPIRED; newest first within group
        status_priority = {STATUS_OPEN: 0, STATUS_PLANNED: 1, STATUS_EXPIRED: 2}
        for line_ref, items in allitems_dict.items():
            if items:
                items.sort(
                    key=lambda x: (
                        status_priority.get(x["status"], 3),
                        -datetime.fromisoformat(x["valid_from"]).timestamp(),
                    )
                )

        _LOGGER.debug("Parsed deviations for %d lines", len(allitems_dict))
        return allitems_dict

    @staticmethod
    async def async_get_operators(session: aiohttp.ClientSession) -> dict[str, str]:
        """Fetch list of operators (codespaces) from Entur GraphQL API.
        
        Extracts all unique 3-letter codespaces from the operators API and maps them
        to friendly names. Falls back to CODESPACE_NAMES constant for better naming.
        
        Returns:
            Dict mapping codespace to display name, e.g. {"SKY": "Skyss (SKY)", "SOF": "Sogn og Fjordane (SOF)"}
        """
        query = """
        query {
          operators {
            id
            name
          }
        }
        """

        headers = {
            "Content-Type": "application/json",
            "ET-Client-Name": "homeassistant-entur-sx",
        }

        try:
            async with async_timeout.timeout(10):
                async with session.post(
                    API_GRAPHQL_URL,
                    json={"query": query},
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    data = await response.json()

                    all_operators = data.get("data", {}).get("operators", [])
                    
                    # Extract unique codespaces and find best names
                    codespace_names = {}
                    
                    for operator in all_operators:
                        op_id = operator.get("id", "")
                        op_name = operator.get("name", "")
                        
                        if not op_id:
                            continue
                        
                        # Extract codespace (first part before colon)
                        if ":" in op_id:
                            parts = op_id.split(":")
                            codespace = parts[0]
                            
                            # Only include 3-letter uppercase codespaces
                            if len(codespace) == 3 and codespace.isupper():
                                # Prefer canonical operator names (XXX:Operator:XXX)
                                is_canonical = (len(parts) == 3 and 
                                              parts[0] == parts[2] and 
                                              parts[1] == "Operator")
                                
                                if is_canonical or codespace not in codespace_names:
                                    # Use CODESPACE_NAMES if available, otherwise API name
                                    friendly_name = CODESPACE_NAMES.get(codespace, op_name)
                                    codespace_names[codespace] = friendly_name
                    
                    # Build final operator dict with display names
                    operators = {}
                    for codespace in sorted(codespace_names.keys()):
                        friendly_name = codespace_names[codespace]
                        display_name = f"{friendly_name} ({codespace})"
                        operators[codespace] = display_name
                    
                    _LOGGER.debug("Found %d operators from GraphQL API", len(operators))
                    return operators

        except Exception as err:
            _LOGGER.error("Error fetching operators from GraphQL: %s", err, exc_info=True)
            # Fallback to CODESPACE_NAMES constant
            _LOGGER.info("Falling back to CODESPACE_NAMES constant")
            operators = {}
            for codespace, friendly_name in sorted(CODESPACE_NAMES.items()):
                display_name = f"{friendly_name} ({codespace})"
                operators[codespace] = display_name
            return operators

    @staticmethod
    async def async_get_lines_for_operator(
        session: aiohttp.ClientSession, operator: str
    ) -> dict[str, str]:
        """Fetch list of lines for a specific operator (codespace) from Entur GraphQL API.
        
        Args:
            session: aiohttp session
            operator: Codespace (e.g., "SKY", "SOF")
            
        Returns:
            Dict mapping line ref to line name, e.g. {"SKY:Line:1": "Line 1 - Bergen sentrum"}
        """
        # Query all lines and filter by codespace
        # We can't use authority query since we only have the codespace now
        query = """
        query {
          lines {
            id
            name
            publicCode
            transportMode
            transportSubmode
            authority {
              id
            }
          }
        }
        """

        headers = {
            "Content-Type": "application/json",
            "ET-Client-Name": "homeassistant-entur-sx",
        }

        try:
            async with async_timeout.timeout(30):
                async with session.post(
                    API_GRAPHQL_URL,
                    json={"query": query},
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    data = await response.json()

                    lines = {}
                    all_lines = data.get("data", {}).get("lines", [])
                    
                    # Filter lines by codespace
                    for line in all_lines:
                        line_id = line.get("id", "")
                        
                        # Check if line belongs to this codespace
                        if not line_id.startswith(f"{operator}:"):
                            continue
                        
                        line_name = line.get("name", "")
                        public_code = line.get("publicCode", "")
                        transport_mode = line.get("transportMode", "")
                        transport_submode = line.get("transportSubmode", "")
                        
                        # Create a friendly display name
                        display_name = f"{public_code}"
                        if line_name:
                            display_name += f" - {line_name}"
                        if transport_mode:
                            display_name += f" ({transport_mode})"
                        
                        # Store as dict with display name, transport mode, and submode
                        lines[line_id] = {
                            "display_name": display_name,
                            "transport_mode": transport_mode.lower() if transport_mode else "bus",
                            "transport_submode": transport_submode.lower() if transport_submode else ""
                        }
                        
                        # Debug log for specific lines
                        if "Line:12" in line_id or "Line:1021" in line_id:
                            _LOGGER.debug("[API] %s found: mode='%s' submode='%s'", line_id, transport_mode, transport_submode or "(none)")

                    _LOGGER.debug("Found %d lines for codespace %s", len(lines), operator)
                    return lines

        except Exception as err:
            _LOGGER.error("Error fetching lines for codespace %s: %s", operator, err, exc_info=True)
            return {}
