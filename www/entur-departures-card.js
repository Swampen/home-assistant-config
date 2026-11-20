"use strict";
/** Standalone TypeScript namespace; compiles to a single browser script. */
var EnturCard;
(function (EnturCard) {
    EnturCard.DEFAULT_DEPARTURES = {
        max: 4,
        min_minutes: 0,
        safety_buffer: 0,
        countdown_threshold: 10,
        show_delays: true,
        show_realtime: false,
    };
    EnturCard.DEFAULT_DISRUPTIONS = {
        enabled: false,
        entities: [],
        deduplicate: true,
        include_planned: true,
        show_description: true,
        dismissible: true,
        dismissal_hours: 24,
    };
    function resolvedConfig(config) {
        return {
            ...config,
            departures: { ...EnturCard.DEFAULT_DEPARTURES, ...(config.departures || {}) },
            disruptions: { ...EnturCard.DEFAULT_DISRUPTIONS, ...(config.disruptions || {}) },
        };
    }
    EnturCard.resolvedConfig = resolvedConfig;
    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, ch => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[ch] || ch));
    }
    EnturCard.escapeHtml = escapeHtml;
    function numberInRange(value, def, min, max) {
        const n = typeof value === 'number' ? value : Number(value);
        return Number.isFinite(n) ? Math.max(min, Math.min(max, Math.round(n))) : def;
    }
    EnturCard.numberInRange = numberInRange;
})(EnturCard || (EnturCard = {}));
var EnturCard;
(function (EnturCard) {
    /**
     * Entur sensor timestamps are local HH:mm, without a date. This assumes
     * the next departures are within +/- 12 hours of the current time.
     */
    function remainingMinutes(time, now, safetyBuffer = 0) {
        const match = /^(\d{1,2}):(\d{2})$/.exec(time);
        if (!match)
            return Number.NaN;
        const h = Number(match[1]);
        const m = Number(match[2]);
        if (h > 23 || m > 59)
            return Number.NaN;
        const target = new Date(now);
        target.setHours(h, m, 0, 0);
        let diff = (target.getTime() - now.getTime()) / 60000;
        if (diff < -720)
            diff += 1440;
        if (diff > 720)
            diff -= 1440;
        return diff - safetyBuffer;
    }
    EnturCard.remainingMinutes = remainingMinutes;
    function parseDepartures(attrs, now, settings = EnturCard.DEFAULT_DEPARTURES) {
        const options = { ...EnturCard.DEFAULT_DEPARTURES, ...settings };
        const departures = [];
        const add = (route, time, source, delay, realtime, cancelled) => {
            if (typeof route !== 'string' || typeof time !== 'string')
                return;
            const match = /^(\S+)\s+(.+)$/.exec(route.trim());
            if (!match)
                return;
            const remaining = remainingMinutes(time.trim(), now, options.safety_buffer);
            if (!Number.isFinite(remaining))
                return;
            const rawDelay = delay === undefined || delay === null || delay === '' ? null : Number(delay);
            departures.push({
                line: match[1], destination: match[2], time: time.trim(), remaining,
                delay: rawDelay !== null && Number.isFinite(rawDelay) ? rawDelay : null,
                realtime: realtime === true,
                cancelled: cancelled === true,
                source,
            });
        };
        add(attrs.route, attrs.due_at, 1, attrs.delay, attrs.real_time, attrs.cancelled);
        add(attrs.next_route, attrs.next_due_at, 2, attrs.next_delay, attrs.next_real_time, attrs.next_cancelled);
        // Other Entur departures are stored as strings, e.g. "14:51 2 Østerås".
        for (const [key, value] of Object.entries(attrs)) {
            const matchIndex = /^departure_#(\d+)$/.exec(key);
            if (!matchIndex || typeof value !== 'string')
                continue;
            const matchValue = /^(\d{1,2}:\d{2})\s+(\S+)\s+(.+)$/.exec(value.trim());
            if (!matchValue)
                continue;
            add(`${matchValue[2]} ${matchValue[3]}`, matchValue[1], Number(matchIndex[1]));
        }
        const cutoff = EnturCard.numberInRange(options.min_minutes, 0, 0, 60);
        const max = EnturCard.numberInRange(options.max, 4, 1, 20);
        return departures
            .filter(d => d.remaining > cutoff && !d.cancelled)
            .sort((a, b) => a.remaining - b.remaining || a.source - b.source)
            .slice(0, max);
    }
    EnturCard.parseDepartures = parseDepartures;
    function departureTimeLabel(departure, countdownThreshold) {
        const threshold = EnturCard.numberInRange(countdownThreshold, 10, 0, 120);
        return departure.remaining <= threshold
            ? `${Math.ceil(departure.remaining)}min`
            : departure.time;
    }
    EnturCard.departureTimeLabel = departureTimeLabel;
    function delayLabel(departure) {
        if (!departure.realtime || departure.delay === null)
            return '';
        if (departure.delay === 0)
            return 'On time';
        if (departure.delay > 0)
            return `+${departure.delay} min`;
        return `${Math.abs(departure.delay)} min early`;
    }
    EnturCard.delayLabel = delayLabel;
    function normalized(value) {
        return String(value || '').replace(/\s+/g, ' ').trim().toLocaleLowerCase();
    }
    function collectDisruptions(states, settings = EnturCard.DEFAULT_DISRUPTIONS) {
        const options = { ...EnturCard.DEFAULT_DISRUPTIONS, ...settings };
        if (!options.enabled)
            return [];
        const unique = new Map();
        let counter = 0;
        for (const entity of options.entities) {
            const sensor = states[entity];
            if (!sensor || ['unavailable', 'unknown'].includes(sensor.state))
                continue;
            const a = sensor.attributes || {};
            // All deviations, not the top-level summary: a line can have many notices.
            const deviations = Array.isArray(a.all_deviations) ? a.all_deviations : [];
            const fallbackLine = /^.*line_(\w+)$/i.exec(entity)?.[1] || '';
            const line = String(a.line_ref || '').split(':').pop() || fallbackLine;
            for (const d of deviations) {
                if (!d || typeof d !== 'object')
                    continue;
                const status = String(d.status || '').toLowerCase();
                if (status === 'expired' || status === 'closed')
                    continue;
                if (!options.include_planned && status === 'planned')
                    continue;
                const summary = String(d.summary || '').trim();
                const description = String(d.description || '').trim();
                if (!summary && !description)
                    continue;
                const validFrom = String(d.valid_from || '');
                const validTo = String(d.valid_to || '');
                // Do not use SIRI status as impact start: an OPEN bulletin can describe
                // future service changes (e.g. metro closed 19–22 November).
                const key = options.deduplicate
                    ? [normalized(summary), normalized(description), validFrom, validTo].join('|')
                    : `${entity}:${counter++}`;
                const existing = unique.get(key);
                if (existing) {
                    if (!existing.lines.includes(line))
                        existing.lines.push(line);
                }
                else {
                    unique.set(key, {
                        summary, description, status, validFrom, validTo,
                        lines: [line], key,
                    });
                }
            }
        }
        return [...unique.values()].map(d => ({ ...d, lines: d.lines.sort((a, b) => a.localeCompare(b, undefined, { numeric: true })) }));
    }
    EnturCard.collectDisruptions = collectDisruptions;
    // A normalized, date-scoped key is shared by identical alerts across lines.
    // This also remains stable if the same disruption gains another affected line.
    function disruptionIdentity(alert) {
        return [normalized(alert.summary), normalized(alert.description), alert.validFrom, alert.validTo].join('|');
    }
    EnturCard.disruptionIdentity = disruptionIdentity;
    function dismissalKey(alert, deduplicate) {
        return deduplicate ? disruptionIdentity(alert) : alert.key;
    }
    EnturCard.dismissalKey = dismissalKey;
    function sensorDisplayName(state, explicit) {
        if (explicit && explicit.trim())
            return explicit.trim();
        const raw = String(state?.attributes?.friendly_name || 'Entur');
        return raw.replace(/^Entur\s+/i, '').replace(/\s+(?:Platform|Plattform)\s*\d+\s*$/i, '').trim() || 'Entur';
    }
    EnturCard.sensorDisplayName = sensorDisplayName;
    function isEnturDepartureSensor(state) {
        return !!state && typeof state.attributes?.route === 'string'
            && typeof state.attributes?.due_at === 'string';
    }
    EnturCard.isEnturDepartureSensor = isEnturDepartureSensor;
    function isDisruptionSensor(state) {
        return !!state && (Array.isArray(state.attributes?.all_deviations)
            || typeof state.attributes?.line_ref === 'string');
    }
    EnturCard.isDisruptionSensor = isDisruptionSensor;
})(EnturCard || (EnturCard = {}));
var EnturCard;
(function (EnturCard) {
    const STYLES = `
    :host { display: block; }
    ha-card {
      overflow: hidden;
      color: var(--primary-text-color);
      background: var(--ha-card-background, var(--card-background-color, #fff));
      border-radius: var(--ha-card-border-radius, 24px);
      font-family: var(--primary-font-family, var(--ha-font-family-body, Manrope, sans-serif));
    }
    .body { padding: 16px; }
    .header { display: flex; align-items: center; gap: 9px; margin-bottom: 12px; min-height: 24px; }
    .header-title { flex: 0 1 auto; font-size: 15px; font-weight: 700; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .header-spacer { flex: 1; min-width: 0; }
    .current-time { font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; font-variant-numeric: tabular-nums; }
    .alert-restore { flex: none; display: inline-flex; align-items: center; justify-content: center;
      width: 32px; height: 32px; margin: -4px -6px -4px 0; padding: 5px;
      border: 0; border-radius: 9px; cursor: pointer; background: transparent;
      color: var(--home-text-accent, var(--warning-color, var(--primary-color))); }
    .alert-restore ha-icon { --mdc-icon-size: 20px; }
    .alert-restore:hover { background: var(--home-bg-secondary, var(--secondary-background-color)); }
    .alert-restore:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
    .header-icon { width: 21px; height: 21px; display: inline-flex; align-items: center; justify-content: center; }
    .more-info { cursor: pointer; border: 0; background: transparent; padding: 0; color: inherit; font: inherit; display: contents; }
    .departure { display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; align-items: center; gap: 12px; padding: 6px 0; }
    .badge { width: 42px; height: 42px; display: flex; align-items: center; justify-content: center;
      border-radius: var(--home-radius-control, 12px);
      background: var(--home-bg-secondary, var(--home-bg-primary, var(--secondary-background-color)));
      color: var(--primary-text-color); font-size: 21px; font-weight: 800; line-height: 1;
      font-variant-numeric: tabular-nums; }
    .destination { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; font-weight: 650; line-height: 1.4; }
    .detail { font-size: 11px; color: var(--secondary-text-color); margin-top: 2px; line-height: 1.4; }
    .detail.ontime { color: var(--success-color, var(--secondary-text-color)); }
    .time { font-size: 18px; font-weight: 700; text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .empty { color: var(--secondary-text-color); font-size: 13px; padding: 12px 0; }
    .error { color: var(--error-color, #b33); font-size: 13px; padding: 10px 0; }
    .notice-area { border-top: 1px solid var(--divider-color, var(--outline-color, #ddd)); margin-top: 12px; padding-top: 7px; }
    .notice { background: var(--home-bg-primary, var(--secondary-background-color)); color: var(--primary-text-color);
      padding: 12px; border-radius: var(--home-radius-control, 12px); margin-top: 8px; }
    .notice-head { display: flex; align-items: center; gap: 7px; margin-bottom: 6px; color: var(--home-text-accent, var(--primary-color)); font-size: 12px; font-weight: 750; }
    .notice-head ha-icon { --mdc-icon-size: 18px; }
    .notice-heading { flex: 1; min-width: 0; }
    .notice-dismiss { display: flex; align-items: center; justify-content: center; flex: none;
      width: 30px; height: 30px; margin: -6px -6px -6px 0; padding: 4px;
      border: 0; border-radius: 9px; background: transparent; color: var(--primary-text-color);
      cursor: pointer; opacity: .78; }
    .notice-dismiss ha-icon { --mdc-icon-size: 19px; }
    .notice-dismiss:hover { opacity: 1; background: var(--home-bg-secondary, var(--secondary-background-color)); }
    .notice-dismiss:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
    .notice-title { font-size: 13px; font-weight: 650; line-height: 1.45; white-space: pre-wrap; }
    .notice-desc { font-size: 12px; line-height: 1.5; margin-top: 5px; white-space: pre-wrap; overflow-wrap: anywhere; }
    .scope-note { color: var(--secondary-text-color); font-size: 10px; padding: 8px 2px 0; line-height: 1.4; }
    .compact .departure { padding: 3px 0; }
    .compact .badge { width: 36px; height: 36px; font-size: 18px; border-radius: 9px; }
    .compact .departure { grid-template-columns: 36px minmax(0, 1fr) auto; }
    .compact .notice { padding: 9px 11px; }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition: none !important; } }
  `;
    class EnturDeparturesCard extends HTMLElement {
        constructor() {
            super();
            this.config = {};
            this.lastMarkup = '';
            // Disruption ID -> expiry timestamp (ms); null means until the notice disappears.
            this.dismissedKeys = new Map();
            this.storageScope = '';
            this.root = this.attachShadow({ mode: 'open' });
            this.root.addEventListener('click', (event) => {
                const restore = event.target.closest?.('button[data-restore-alerts]');
                if (restore) {
                    event.stopPropagation();
                    const c = EnturCard.resolvedConfig(this.config);
                    const alerts = this.ha ? EnturCard.collectDisruptions(this.ha.states, c.disruptions) : [];
                    let changed = false;
                    for (const alert of alerts) {
                        const key = EnturCard.dismissalKey(alert, c.disruptions.deduplicate);
                        if (this.dismissedKeys.delete(key))
                            changed = true;
                    }
                    if (changed) {
                        this.saveDismissals();
                        this.draw();
                    }
                    return;
                }
                const button = event.target.closest?.('button[data-dismiss]');
                if (!button || !this.config.disruptions?.enabled)
                    return;
                if (EnturCard.resolvedConfig(this.config).disruptions.dismissible === false)
                    return;
                event.stopPropagation();
                const token = button.dataset.dismiss;
                if (!token)
                    return;
                const hours = EnturCard.numberInRange(EnturCard.resolvedConfig(this.config).disruptions.dismissal_hours, 24, 0, 8760);
                this.dismissedKeys.set(token, hours === 0 ? null : Date.now() + hours * 3600000);
                this.saveDismissals();
                this.draw();
            });
        }
        /** Native Home Assistant card editor (2026.6+).
         * Home Assistant renders its own selectors and handles config-changed events.
         * No internal ha-* elements or custom form CSS are required.
         */
        static getConfigForm() {
            const labels = {
                entity: 'Entur departure sensor',
                name: 'Title',
                show_current_time: 'Show current time',
                compact: 'Compact rows',
                max: 'Number of departures',
                min_minutes: 'Cutoff (minutes)',
                safety_buffer: 'Safety buffer (minutes)',
                countdown_threshold: 'Countdown threshold (minutes)',
                show_delays: 'Show reported delays',
                show_realtime: 'Show real-time indicator',
                enabled: 'Show operational messages',
                entities: 'Disruption sensors',
                deduplicate: 'Merge duplicate messages',
                include_planned: 'Include planned notices',
                show_description: 'Show message descriptions',
                dismissible: 'Allow dismissing messages',
                dismissal_hours: 'Dismiss for (hours)',
            };
            const helpers = {
                entity: 'Select the Entur sensor providing departures for your stop and platform.',
                show_current_time: 'Show the current local time in the card header (24-hour format).',
                min_minutes: 'Departures are shown only if remaining minutes minus the safety buffer exceeds the cutoff.',
                safety_buffer: 'Minutes reserved for walking to the platform.',
                countdown_threshold: 'Show minutes at or below this value; otherwise show the departure time.',
                entities: 'Select the line-wide Entur Situation Exchange sensors to monitor. Messages are not platform-specific.',
                dismissible: 'Dismissals are remembered on this browser, not synced across devices.',
                dismissal_hours: 'Default 24 hours. Set to 0 to hide a dismissed message until it disappears from the sensors.',
            };
            const number = (min, max) => ({ number: { min, max, mode: 'box', step: 1 } });
            return {
                schema: [
                    { name: 'entity', required: true, selector: { entity: { filter: { domain: 'sensor' } } } },
                    { name: 'name', selector: { text: {} } },
                    { name: 'show_current_time', default: false, selector: { boolean: {} } },
                    { type: 'expandable', name: 'departures', title: 'Departures', schema: [
                            { type: 'grid', name: '', schema: [
                                    { name: 'max', default: 4, selector: number(1, 20) },
                                    { name: 'min_minutes', selector: number(0, 60) },
                                    { name: 'safety_buffer', selector: number(0, 15) },
                                    { name: 'countdown_threshold', default: 10, selector: number(0, 120) },
                                ] },
                            { name: 'show_delays', default: true, selector: { boolean: {} } },
                            { name: 'show_realtime', default: false, selector: { boolean: {} } },
                        ] },
                    { type: 'expandable', name: 'disruptions', title: 'Operational messages', schema: [
                            { name: 'enabled', default: false, selector: { boolean: {} } },
                            { name: 'entities', selector: { entity: { filter: { domain: 'sensor' }, multiple: true, reorder: true } } },
                            { name: 'include_planned', default: true, selector: { boolean: {} } },
                            { name: 'deduplicate', default: true, selector: { boolean: {} } },
                            { name: 'show_description', default: true, selector: { boolean: {} } },
                            { name: 'dismissible', default: true, selector: { boolean: {} } },
                            { name: 'dismissal_hours', default: 24, selector: number(0, 8760) },
                        ] },
                    { type: 'expandable', name: '', title: 'Appearance', schema: [
                            { name: 'compact', default: false, selector: { boolean: {} } },
                        ] },
                ],
                computeLabel: (field) => labels[field.name || ''],
                computeHelper: (field) => helpers[field.name || ''],
                assertConfig: (config) => {
                    if (config.departures && (typeof config.departures !== 'object' || Array.isArray(config.departures))) {
                        throw new Error('departures must be a mapping');
                    }
                    if (config.disruptions && (typeof config.disruptions !== 'object' || Array.isArray(config.disruptions))) {
                        throw new Error('disruptions must be a mapping');
                    }
                    if (config.disruptions?.entities && !Array.isArray(config.disruptions.entities)) {
                        throw new Error('disruptions.entities must be a list');
                    }
                },
            };
        }
        static getStubConfig(hass, entities = [], entitiesFallback = []) {
            const matches = [...entities, ...entitiesFallback];
            const id = matches.find(e => EnturCard.isEnturDepartureSensor(hass?.states[e]))
                || Object.keys(hass?.states || {}).find(e => EnturCard.isEnturDepartureSensor(hass?.states[e]));
            const { min_minutes: _cutoff, safety_buffer: _buffer, ...departureDefaults } = EnturCard.DEFAULT_DEPARTURES;
            return { entity: id || '', departures: departureDefaults, disruptions: { ...EnturCard.DEFAULT_DISRUPTIONS } };
        }
        setConfig(config) {
            if (!config || typeof config !== 'object')
                throw new Error('Invalid Entur card configuration.');
            this.config = config;
            this.loadDismissals();
            this.draw();
        }
        set hass(hass) {
            this.ha = hass;
            this.draw();
        }
        get hass() { return this.ha; }
        connectedCallback() {
            if (!this.clock)
                this.clock = window.setInterval(() => this.draw(), 15000);
            this.draw();
        }
        disconnectedCallback() {
            if (this.clock)
                window.clearInterval(this.clock);
            this.clock = undefined;
        }
        getCardSize() {
            const c = EnturCard.resolvedConfig(this.config);
            const n = c.departures.max;
            const notifications = c.disruptions.enabled ? 2 : 0;
            return Math.max(2, Math.ceil((80 + n * (c.compact ? 42 : 54) + notifications * 48) / 50));
        }
        dismissalStorageKey() {
            // Keep dismissal decisions separate for each departure entity.
            return `entur-departures-card:dismissed:v1:${this.config.entity || 'default'}`;
        }
        loadDismissals() {
            const scope = this.dismissalStorageKey();
            if (scope === this.storageScope)
                return;
            this.storageScope = scope;
            this.dismissedKeys = new Map();
            try {
                const stored = JSON.parse(window.localStorage.getItem(scope) || '[]');
                if (Array.isArray(stored)) {
                    // v0.2/v0.3 stored only IDs. Preserve those dismissals until the
                    // notices disappear; new dismissals use the configured expiration.
                    for (const item of stored) {
                        if (typeof item === 'string')
                            this.dismissedKeys.set(item, null);
                        else if (Array.isArray(item) && typeof item[0] === 'string' &&
                            (item[1] === null || (typeof item[1] === 'number' && Number.isFinite(item[1])))) {
                            this.dismissedKeys.set(item[0], item[1]);
                        }
                    }
                }
            }
            catch { /* private browsing or storage disabled: current session still works */ }
        }
        saveDismissals() {
            try {
                window.localStorage.setItem(this.dismissalStorageKey(), JSON.stringify([...this.dismissedKeys]));
            }
            catch { /* browser storage may be blocked */ }
        }
        draw() {
            if (!this.isConnected || !this.ha)
                return;
            const c = EnturCard.resolvedConfig(this.config);
            const state = c.entity ? this.ha.states[c.entity] : undefined;
            const valid = EnturCard.isEnturDepartureSensor(state);
            const now = new Date();
            const departures = valid ? EnturCard.parseDepartures(state.attributes, now, c.departures) : [];
            this.loadDismissals();
            const alerts = EnturCard.collectDisruptions(this.ha.states, c.disruptions);
            // Only clean dismissed IDs when every configured sensor is available.
            // Avoid resurrecting old notices during temporary connectivity loss.
            const sourcesReady = (c.disruptions.entities || []).every(id => !!this.ha.states[id] && !['unknown', 'unavailable'].includes(this.ha.states[id].state));
            const currentIds = new Set(alerts.map(a => EnturCard.dismissalKey(a, c.disruptions.deduplicate)));
            let changed = false;
            for (const [key, expiresAt] of this.dismissedKeys) {
                if ((expiresAt !== null && expiresAt <= now.getTime()) ||
                    (sourcesReady && c.disruptions.enabled && !currentIds.has(key))) {
                    this.dismissedKeys.delete(key);
                    changed = true;
                }
            }
            if (changed)
                this.saveDismissals();
            const hiddenCount = c.disruptions.enabled && c.disruptions.dismissible
                ? alerts.filter(d => this.dismissedKeys.has(EnturCard.dismissalKey(d, c.disruptions.deduplicate))).length : 0;
            const visibleAlerts = c.disruptions.dismissible
                ? alerts.filter(d => !this.dismissedKeys.has(EnturCard.dismissalKey(d, c.disruptions.deduplicate))) : alerts;
            const name = EnturCard.sensorDisplayName(state, c.name);
            const rows = departures.map(d => {
                const detail = c.departures.show_delays ? EnturCard.delayLabel(d) : '';
                const realtime = c.departures.show_realtime && d.realtime ? ' · Live' : '';
                const label = `${detail}${realtime}`.replace(/^ · /, '');
                return `
          <div class="departure" role="listitem" aria-label="Line ${EnturCard.escapeHtml(d.line)}, ${EnturCard.escapeHtml(d.destination)}, ${EnturCard.escapeHtml(EnturCard.departureTimeLabel(d, c.departures.countdown_threshold))}">
            <div class="badge" aria-hidden="true">${EnturCard.escapeHtml(d.line)}</div>
            <div>
              <div class="destination" title="${EnturCard.escapeHtml(d.destination)}">${EnturCard.escapeHtml(d.destination)}</div>
              ${label ? `<div class="detail${detail === 'On time' ? ' ontime' : ''}">${EnturCard.escapeHtml(label)}</div>` : ''}
            </div>
            <div class="time">${EnturCard.escapeHtml(EnturCard.departureTimeLabel(d, c.departures.countdown_threshold))}</div>
          </div>`;
            }).join('');
            const notices = visibleAlerts.map(d => {
                const lines = d.lines.filter(Boolean);
                const lineLabel = lines.length ? ' · Linje ' + EnturCard.escapeHtml(lines.join(' og ')) : '';
                const planned = d.status === 'planned';
                const heading = (planned ? 'Planlagt avvik' : 'Avvik') + lineLabel;
                const summary = d.summary || d.description;
                const showDesc = c.disruptions.show_description && d.description.trim().toLowerCase() !== summary.trim().toLowerCase();
                const key = EnturCard.dismissalKey(d, c.disruptions.deduplicate);
                return `
          <div class="notice">
            <div class="notice-head">
              <ha-icon icon="mdi:alert-outline"></ha-icon>
              <span class="notice-heading">${heading}</span>
              ${c.disruptions.dismissible ? `<button class="notice-dismiss" type="button" data-dismiss="${EnturCard.escapeHtml(key)}" aria-label="Dismiss disruption: ${EnturCard.escapeHtml(summary)}" title="Dismiss this disruption"><ha-icon icon="mdi:close"></ha-icon></button>` : ''}
            </div>
            <div class="notice-title">${EnturCard.escapeHtml(summary)}</div>
            ${showDesc ? `<div class="notice-desc">${EnturCard.escapeHtml(d.description)}</div>` : ''}
          </div>`;
            }).join('');
            const markup = `
        <style>${STYLES}</style>
        <ha-card>
          <div class="body${c.compact ? ' compact' : ''}">
            <div class="header">
              <span class="header-icon"><ha-icon icon="${EnturCard.escapeHtml(String(state?.attributes?.icon || 'mdi:subway'))}"></ha-icon></span>
              <span class="header-title">${EnturCard.escapeHtml(name)}</span>
              ${hiddenCount ? `<button class="alert-restore" type="button" data-restore-alerts aria-label="Show ${hiddenCount} dismissed disruption ${hiddenCount === 1 ? 'message' : 'messages'}" title="Show dismissed disruption ${hiddenCount === 1 ? 'message' : 'messages'}"><ha-icon icon="mdi:alert-outline"></ha-icon></button>` : ''}
              <span class="header-spacer" aria-hidden="true"></span>
              ${c.show_current_time ? `<time class="current-time" aria-label="Current time">${new Intl.DateTimeFormat('nb-NO', { hour: '2-digit', minute: '2-digit', hour12: false }).format(now)}</time>` : ''}
            </div>
            ${!c.entity ? '<div class="empty">Select an Entur departure sensor in the visual editor.</div>'
                : !valid ? '<div class="error">Entur sensor not found or missing route/due_at attributes.</div>'
                    : `<div role="list" aria-label="Upcoming departures">${rows || '<div class="empty">Ingen kommende avganger</div>'}</div>`}
            ${notices ? `<section class="notice-area" aria-label="Operational messages">${notices}<div class="scope-note">Meldingene gjelder hele linjen og er ikke bekreftet kun for denne plattformen.</div></section>` : ''}
          </div>
        </ha-card>
      `;
            if (markup !== this.lastMarkup) {
                this.lastMarkup = markup;
                this.root.innerHTML = markup;
            }
        }
    }
    EnturCard.EnturDeparturesCard = EnturDeparturesCard;
})(EnturCard || (EnturCard = {}));
var EnturCard;
(function (EnturCard) {
    const cardName = 'entur-departures-card';
    if (!customElements.get(cardName))
        customElements.define(cardName, EnturCard.EnturDeparturesCard);
    const win = window;
    win.customCards = win.customCards || [];
    if (!win.customCards.some(c => c.type === cardName)) {
        win.customCards.push({
            type: cardName,
            name: 'Entur Departures',
            description: 'Next departures and optional, deduplicated line disruptions',
            preview: false,
            getEntitySuggestion: (hass, entityId) => EnturCard.isEnturDepartureSensor(hass?.states[entityId])
                ? { config: { type: 'custom:entur-departures-card', entity: entityId } }
                : null,
        });
    }
})(EnturCard || (EnturCard = {}));
