import { useEffect, useState } from "react";
import { apiJson } from "../lib/api";
import { atlantaForecast, ForecastSettings } from "./ForecastSettings";
import type { DashboardWidget, HAEntity } from "../lib/types";

export const widgetKinds = {
  date: { name: "Date", subtitle: "Day and date of the frame", symbol: "▦" },
  photo_metadata: { name: "Photo details", subtitle: "Date taken, years ago, or photo location", symbol: "▧" },
  calendar: { name: "Upcoming events", subtitle: "The next 24 hours on a Home Assistant calendar", symbol: "▤" },
  location: { name: "Location", subtitle: "Home Assistant site or a selected zone", symbol: "⌖" },
  weather: { name: "Weather", subtitle: "Current conditions from Home Assistant", symbol: "☁" },
  forecast: { name: "7-day forecast", subtitle: "Daily highs and lows for a city", symbol: "◒" },
  device_battery: { name: "Display battery", subtitle: "This display or a Home Assistant battery", symbol: "▰" },
  home_assistant: { name: "Home Assistant sensor", subtitle: "A recently reported measurement", symbol: "≋" },
  text: { name: "Text note", subtitle: "A message that stays put", symbol: "Aa" }
} as const;

export type WidgetKind = keyof typeof widgetKinds;
const photoFields = { date_taken: "Date taken", years_ago: "Years ago", location: "Photo location" } as const;

function photoField(widget: DashboardWidget): keyof typeof photoFields {
  const field = widget.source?.field;
  return field === "years_ago" || field === "location" ? field : "date_taken";
}

export type Tile = { x: number; y: number };
type Span = readonly [number, number];
export const tiles: Tile[] = Array.from({ length: 8 }, (_, i) => ({ x: i % 4, y: Math.floor(i / 4) }));
const flexibleSpans: Span[] = tiles.map(({ x, y }) => [x + 1, y + 1]);
const wideSpans: Span[] = [[2, 1], [2, 2], [4, 1]];
const noteSpans: Span[] = [[1, 1], [2, 1], [4, 1], [2, 2], [4, 2]];
const spans: Record<string, Span[]> = {
  entity_state: flexibleSpans, big_number: flexibleSpans, on_off_state: [[1, 1]], status_pill: [[1, 1]],
  weather_summary: flexibleSpans, line_chart: wideSpans, bar_chart: wideSpans, donut_chart: [[1, 1], [2, 2]],
  agenda: flexibleSpans, text_note: flexibleSpans, photo_tile: noteSpans.filter(([w, h]) => w !== 4 || h === 2),
  vertical_status_stack: [[1, 2]], full_dashboard: [[4, 2]]
};

export function widgetSpans(widget: DashboardWidget): Span[] {
  return spans[widget.type] ?? [[widget.w, widget.h]];
}

export function displayText(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

export function fits(widgets: DashboardWidget[], tile: Tile, w: number, h: number, editing?: DashboardWidget) {
  return tile.x >= 0 && tile.y >= 0 && tile.x + w <= 4 && tile.y + h <= 2 && !widgets.some((item) => item !== editing &&
    item.x < tile.x + w && item.x + item.w > tile.x && item.y < tile.y + h && item.y + item.h > tile.y);
}

export function widgetAt(widgets: DashboardWidget[], { x, y }: Tile) {
  return widgets.find((item) => x >= item.x && x < item.x + item.w && y >= item.y && y < item.y + item.h);
}

export function widgetKind(widget: DashboardWidget): WidgetKind | undefined {
  const source = widget.source?.type;
  if (source && source !== "text" && Object.hasOwn(widgetKinds, source)) return source as WidgetKind;
  if (source === "entity" || widget.source?.entity_id) return "home_assistant";
  if (widget.type === "text_note" || source === "text") return "text";
  return undefined;
}

export function widgetTitle(widget: DashboardWidget) {
  const kind = widgetKind(widget);
  const label = displayText(widget.options?.label);
  if (label) return label;
  if (kind === "photo_metadata") return photoFields[photoField(widget)];
  const entity = displayText(widget.source?.entity_id).split(".").at(-1)?.replaceAll("_", " ");
  if (entity) return entity[0].toUpperCase() + entity.slice(1);
  return kind ? widgetKinds[kind].name : widget.type.replaceAll("_", " ");
}

export function createWidget(kind: WidgetKind, tile: Tile, widgets: DashboardWidget[]): DashboardWidget {
  const base = `tile-${tile.x}-${tile.y}`;
  const ids = new Set(widgets.map((item) => item.id));
  let id = base;
  for (let suffix = 2; ids.has(id); suffix++) id = `${base}-${suffix}`;
  const weatherTile = kind === "weather" || kind === "forecast";
  const type = kind === "calendar" ? "agenda" : kind === "text" ? "text_note" :
    weatherTile ? "weather_summary" : "entity_state";
  const w = (kind === "calendar" || weatherTile) && fits(widgets, tile, 2, 1) ? 2 : 1;
  const h = 1;
  if (kind === "forecast") {
    return { ...tile, id, type, w, h, source: { ...atlantaForecast }, options: {} };
  }
  if (kind === "photo_metadata") {
    return { ...tile, id, type, w, h, source: { type: kind, field: "date_taken" }, options: {} };
  }
  const options = ["home_assistant", "location", "device_battery"].includes(kind) ? {} : { label: widgetKinds[kind].name };
  return { ...tile, id, type, w, h, options,
    source: kind === "text" ? {} : { type: kind } };
}

export const dashboardPresets: Record<string, { name: string; detail: string; widgets: [WidgetKind, Tile, number?][] }> = {
  home: { name: "Daily overview", detail: "Date, weather, battery and 7-day forecast", widgets: [
    ["date", { x: 0, y: 0 }, 2], ["weather", { x: 2, y: 0 }, 2],
    ["device_battery", { x: 0, y: 1 }, 2], ["forecast", { x: 2, y: 1 }, 2]
  ] },
  glance: { name: "At a glance", detail: "Date and display battery", widgets: [
    ["date", { x: 0, y: 0 }, 2], ["device_battery", { x: 2, y: 0 }]
  ] },
  photo: { name: "Photo corner cards", detail: "Date and weather leave the photo in view", widgets: [
    ["date", { x: 0, y: 1 }], ["weather", { x: 2, y: 1 }]
  ] }
};

export function presetWidgets(preset: typeof dashboardPresets[string]) {
  const widgets: DashboardWidget[] = [];
  for (const [kind, tile, width] of preset.widgets) {
    const widget = createWidget(kind, tile, widgets);
    if (width) widget.w = width;
    widgets.push(widget);
  }
  return widgets;
}

export function WidgetSymbol({ kind }: { kind?: WidgetKind }) {
  return <span className="dd-widget-symbol" data-kind={kind} aria-hidden="true">{kind ? widgetKinds[kind].symbol : "▪"}</span>;
}

export function WidgetGallery({ disabled, onAdd }: {
  disabled: boolean; onAdd: (kind: WidgetKind) => void;
}) {
  return <div className="dd-widget-gallery" aria-label="Available widgets">
    {(Object.keys(widgetKinds) as WidgetKind[]).map((kind) => <button key={kind} type="button"
      disabled={disabled} onClick={() => onAdd(kind)}>
      <WidgetSymbol kind={kind} /><span><strong>{widgetKinds[kind].name}</strong><small>{widgetKinds[kind].subtitle}</small></span><span aria-hidden="true" className="dd-add-symbol">+</span>
    </button>)}
  </div>;
}

type DashboardEntity = HAEntity & { area_name?: string | null; device_name?: string | null; dashboard_valid_until?: string };
const maxReportAgeMs = 60 * 60 * 1000;

function reportExpiresAt(entity: DashboardEntity) {
  if (entity.dashboard_valid_until) return Date.parse(entity.dashboard_valid_until);
  return Date.parse(entity.source_reported_at ?? "") + maxReportAgeMs;
}

const entityPickers = {
  weather: { domain: "weather", purpose: "dashboard", search: "Search weather providers", fallback: "Automatic weather provider" },
  home_assistant: { domain: "sensor", purpose: "dashboard", search: "Search Home Assistant sensors", fallback: "" },
  device_battery: { domain: "sensor", purpose: "battery", search: "Search battery sensors", fallback: "This display’s battery" },
  location: { domain: "zone", purpose: "location", search: "Search Home Assistant zones", fallback: "Home Assistant site" }
} as const;

function EntityPicker({ kind, selected, disabled, onChange }: {
  kind: keyof typeof entityPickers; selected: string; disabled: boolean; onChange: (id: string) => void;
}) {
  const { domain, purpose, search, fallback } = entityPickers[kind];
  const expires = kind === "home_assistant" || kind === "weather";
  const [query, setQuery] = useState("");
  const [entities, setEntities] = useState<DashboardEntity[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [asOf, setAsOf] = useState(() => Date.now());
  const [cacheRefresh, setCacheRefresh] = useState(0);
  const available = expires ? entities.filter((entity) => reportExpiresAt(entity) > asOf) : entities;
  const expiresAt = expires ? Math.min(...available.map(reportExpiresAt)) : Infinity;

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      const parameters = new URLSearchParams({
        purpose, domain, search: query.trim(), limit: "24"
      });
      try {
        const result = await apiJson<{ entities: DashboardEntity[]; error?: string | null }>(`/api/home-assistant/entities?${parameters}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setAsOf(Date.now());
        setEntities(result.entities);
        setError(result.error ?? "");
      } catch {
        if (!controller.signal.aborted) {
          setEntities([]);
          setError("Unable to load entities.");
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }, 160);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [query, purpose, domain, cacheRefresh]);

  useEffect(() => {
    if (!Number.isFinite(expiresAt)) return;
    const timer = window.setTimeout(() => {
      setAsOf(Date.now());
      setCacheRefresh((count) => count + 1);
    }, Math.max(0, expiresAt - Date.now()));
    return () => window.clearTimeout(timer);
  }, [expiresAt]);

  const choose = (next: string) => {
    if (next !== query) { setLoading(true); setQuery(next); }
  };
  const select = (entity: DashboardEntity) => {
    const now = Date.now();
    if (!expires || reportExpiresAt(entity) > now) {
      onChange(entity.entity_id);
    } else {
      setAsOf(now);
      setCacheRefresh((count) => count + 1);
    }
  };
  const stateLabel = (entity: DashboardEntity) => {
    const state = entity.state || "";
    if (kind === "device_battery") {
      const level = Number(state);
      if (!state.trim() || !Number.isFinite(level) || level < 0 || level > 100) return "Unavailable";
      if (reportExpiresAt(entity) > asOf) return `${level}%`;
      const stamp = Date.parse(entity.source_reported_at || "");
      return Number.isFinite(stamp) ? `${level}% · Reported ${new Date(stamp).toLocaleString()}` : `${level}% · Report time unknown`;
    }
    if (kind !== "weather") return state;
    const words = state === "partlycloudy" ? "partly cloudy" : state.replaceAll("-", " ");
    const condition = words.charAt(0).toUpperCase() + words.slice(1);
    const stamp = Date.parse(entity.source_reported_at ?? "");
    return stamp + maxReportAgeMs < asOf
      ? `${condition} · Reported ${new Date(stamp).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
      : condition;
  };
  const current = entities.find((entity) => entity.entity_id === selected);
  return <div className="dd-entity-picker">
    <label>{search}
      <input type="search" value={query} placeholder="Name or entity ID" disabled={disabled}
        onKeyDown={(event) => {
          if (event.key !== "Enter") return;
          event.preventDefault();
          if (!loading && !error && available.length === 1) select(available[0]);
        }}
        onChange={(event) => choose(event.target.value)} />
    </label>
    <div className="dd-picker-quick">
      <span className="dd-selected-entity" role="status">{selected
        ? `Selected source · ${current?.name && current.name !== selected ? current.name + " · " : ""}${selected}`
        : fallback || "No entity selected"}</span>
      {fallback && selected && <button type="button" className="dd-inline-button" disabled={disabled}
        onClick={() => onChange("")}>{kind === "weather" ? "Use automatic" : `Use ${fallback}`}</button>}
      {kind === "home_assistant" && <button type="button" className="dd-inline-button" disabled={disabled} onClick={() => choose("battery")}>Find batteries</button>}
    </div>
    {loading ? <p className="dd-hint" role="status">Searching…</p> : error ? <p className="warning-text" role="status">{error}</p> : available.length ?
      <div className="dd-entity-results" aria-label="Matching entities">
        {available.map((entity) => <button type="button" key={entity.entity_id} aria-pressed={entity.entity_id === selected} disabled={disabled} onClick={() => select(entity)}>
          <span><strong>{entity.name || entity.entity_id}</strong><small>{[entity.area_name, entity.entity_id].filter(Boolean).join(" · ")}</small></span>
          {kind !== "location" && <span className="dd-entity-state">{stateLabel(entity)}</span>}
        </button>)}
      </div> : <p className="dd-hint" role="status">{query.trim() ? "No entities match your search." : "No matching entities are available yet."}</p>}
  </div>;
}

type CalendarChoice = { entity_id: string; name: string };

function CalendarPicker({ selected, disabled, onChange }: {
  selected: string; disabled: boolean; onChange: (entityId: string) => void;
}) {
  const [choices, setChoices] = useState<CalendarChoice[]>([]);
  const [message, setMessage] = useState("Loading calendars…");

  useEffect(() => {
    const controller = new AbortController();
    apiJson<{ calendars: CalendarChoice[]; error: string | null }>("/api/home-assistant/calendars", { signal: controller.signal })
      .then((response) => {
        if (controller.signal.aborted) return;
        setChoices(response.calendars);
        setMessage(response.error || (response.calendars.length ? "" : "No calendars are available."));
      })
      .catch(() => { if (!controller.signal.aborted) setMessage("Unable to load calendars."); });
    return () => controller.abort();
  }, []);

  return <>
    <label>Calendar<select aria-label="Calendar" value={selected} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
      <option value="">No calendar selected</option>
      {selected && !choices.some((choice) => choice.entity_id === selected) && <option value={selected}>{selected}</option>}
      {choices.map((choice) => <option key={choice.entity_id} value={choice.entity_id}>{choice.name}</option>)}
    </select></label>
    {selected && <p className="dd-selected-entity" role="status">Selected source · {selected}</p>}
    {message && <p className="dd-hint" role="status">{message}</p>}
  </>;
}

export function WidgetSettings({ widget, widgets, disabled, onChange, onRemove }: {
  widget: DashboardWidget; widgets: DashboardWidget[]; disabled: boolean;
  onChange: (widget: DashboardWidget) => void; onRemove: () => void;
}) {
  const kind = widgetKind(widget);
  const available = widgetSpans(widget)
    .filter(([w, h]) => fits(widgets, widget, w, h, widget));
  const positions = tiles.filter((tile) => fits(widgets, tile, widget.w, widget.h, widget));
  return <div className="dd-widget-fields" aria-label="Widget settings">
    {kind === "photo_metadata" && <>
      <label>Photo detail<select aria-label="Photo detail" value={photoField(widget)} disabled={disabled}
        onChange={(event) => onChange({ ...widget, source: { type: kind, field: event.target.value } })}>
        {Object.entries(photoFields).map(([field, title]) => <option key={field} value={field}>{title}</option>)}
      </select></label>
      <p className="dd-hint">{photoField(widget) === "location"
        ? "Shows the place name or approximate coordinates saved in each photo."
        : "Uses the capture date saved in each photo."}</p>
    </>}
    {(kind === "home_assistant" || kind === "weather" || kind === "device_battery" || kind === "location") && <EntityPicker kind={kind}
      selected={displayText(widget.source?.entity_id)} disabled={disabled}
      onChange={(entityId) => onChange({ ...widget, source: { ...widget.source, entity_id: entityId || undefined } })} />}
    {kind === "forecast" && <ForecastSettings widget={widget} disabled={disabled} onChange={onChange} />}
    {kind === "calendar" && <>
      <CalendarPicker selected={displayText(widget.source?.entity_id)} disabled={disabled}
        onChange={(entityId) => onChange({ ...widget, source: { ...widget.source, entity_id: entityId || undefined },
          options: { ...widget.options, show_event_titles: false } })} />
      <label className="dd-calendar-titles"><input type="checkbox" disabled={disabled || !widget.source?.entity_id}
        checked={widget.options?.show_event_titles === true}
        onChange={(event) => onChange({ ...widget, options: { ...widget.options, show_event_titles: event.target.checked } })} />
        Show event titles on the display
      </label>
      <p className="dd-hint">Events appear as Busy unless you share their titles.</p>
    </>}
    <label>Label<input value={displayText(widget.options?.label)} maxLength={80} disabled={disabled}
      placeholder={kind === "photo_metadata" ? photoFields[photoField(widget)] : undefined}
      onChange={(event) => onChange({ ...widget, options: { ...widget.options, label: event.target.value } })} /></label>
    {kind === "text" && <label>Text<textarea value={displayText(widget.options?.text)} maxLength={120} rows={2} disabled={disabled}
      onChange={(event) => onChange({ ...widget, options: { ...widget.options, text: event.target.value } })} /></label>}
    {kind === "home_assistant" && <label><span>Unit <span className="dd-optional">(optional)</span></span><input value={displayText(widget.options?.unit)} maxLength={24}
      placeholder="Use entity unit" disabled={disabled} onChange={(event) => onChange({ ...widget, options: { ...widget.options, unit: event.target.value } })} /></label>}
    <div className="dd-field-pair">
      <label>Size<select aria-label="Size" value={`${widget.w}x${widget.h}`} disabled={disabled || available.length < 2} onChange={(event) => {
        const [w, h] = event.target.value.split("x").map(Number);
        onChange({ ...widget, w, h });
      }}>
        {available.map(([w, h]) => <option key={`${w}x${h}`} value={`${w}x${h}`}>{w} × {h} {w * h === 1 ? "tile" : "tiles"}</option>)}
      </select></label>
      <label>Position<select aria-label="Position" value={`${widget.x}:${widget.y}`} disabled={disabled || positions.length < 2} onChange={(event) => {
        const [x, y] = event.target.value.split(":").map(Number);
        onChange({ ...widget, x, y });
      }}>
        {positions.map(({ x, y }) => <option key={`${x}:${y}`} value={`${x}:${y}`}>Row {y + 1}, column {x + 1}</option>)}
      </select></label>
    </div>
    <button type="button" className="danger-text-button" disabled={disabled} onClick={onRemove}>Remove widget</button>
  </div>;
}
