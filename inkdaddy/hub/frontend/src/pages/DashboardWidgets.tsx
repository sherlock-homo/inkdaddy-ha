import { useEffect, useState } from "react";
import { apiJson } from "../lib/api";
import type { DashboardWidget, HAEntity } from "../lib/types";

export const widgetKinds = {
  clock: { name: "Clock", subtitle: "Local time of a scheduled or requested refresh", symbol: "◷" },
  location: { name: "Location", subtitle: "Name and time zone of your home", symbol: "⌖" },
  weather: { name: "Weather", subtitle: "Current conditions from Home Assistant", symbol: "☁" },
  device_battery: { name: "Display battery", subtitle: "Last battery level reported by this display", symbol: "▰" },
  home_assistant: { name: "Home Assistant sensor", subtitle: "A recently reported measurement", symbol: "≋" },
  text: { name: "Text note", subtitle: "A message that stays put", symbol: "Aa" }
} as const;

export type WidgetKind = keyof typeof widgetKinds;
export type Tile = { x: number; y: number };
type Span = readonly [number, number];
export const tiles: Tile[] = Array.from({ length: 8 }, (_, i) => ({ x: i % 4, y: Math.floor(i / 4) }));
const basicSpans: Span[] = [[1, 1], [2, 1]];
const wideSpans: Span[] = [[2, 1], [2, 2], [4, 1]];
const noteSpans: Span[] = [[1, 1], [2, 1], [4, 1], [2, 2], [4, 2]];
const spans: Record<string, Span[]> = {
  entity_state: basicSpans, big_number: basicSpans, on_off_state: [[1, 1]], status_pill: [[1, 1]],
  weather_summary: wideSpans, line_chart: wideSpans, bar_chart: wideSpans, donut_chart: [[1, 1], [2, 2]],
  agenda: [[2, 2], [4, 1], [4, 2]], text_note: noteSpans, photo_tile: noteSpans,
  vertical_status_stack: [[1, 2]], full_dashboard: [[4, 2]]
};

export function displayText(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

export function fits(widgets: DashboardWidget[], tile: Tile, w: number, h: number, editing?: DashboardWidget) {
  return tile.x + w <= 4 && tile.y + h <= 2 && !widgets.some((item) => item !== editing &&
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
  const entity = displayText(widget.source?.entity_id).split(".").at(-1)?.replaceAll("_", " ");
  if (entity) return entity[0].toUpperCase() + entity.slice(1);
  return kind ? widgetKinds[kind].name : widget.type.replaceAll("_", " ");
}

export function createWidget(kind: WidgetKind, tile: Tile, widgets: DashboardWidget[]): DashboardWidget {
  const base = `tile-${tile.x}-${tile.y}`;
  const ids = new Set(widgets.map((item) => item.id));
  let id = base;
  for (let suffix = 2; ids.has(id); suffix++) id = `${base}-${suffix}`;
  const type = kind === "text" ? "text_note" : kind === "weather" && fits(widgets, tile, 2, 1) ? "weather_summary" : "entity_state";
  const [w, h] = spans[type][0];
  const options = kind === "home_assistant" ? {} : { label: widgetKinds[kind].name };
  return { ...tile, id, type, w, h, options,
    source: kind === "text" ? {} : { type: kind } };
}

export const dashboardPresets: Record<string, { name: string; detail: string; widgets: [WidgetKind, Tile, number?][] }> = {
  home: { name: "Daily overview", detail: "Clock, weather, location and battery", widgets: [
    ["clock", { x: 0, y: 0 }], ["weather", { x: 1, y: 0 }], ["device_battery", { x: 3, y: 0 }], ["location", { x: 0, y: 1 }, 2]
  ] },
  glance: { name: "At a glance", detail: "Clock and display battery", widgets: [
    ["clock", { x: 0, y: 0 }, 2], ["device_battery", { x: 2, y: 0 }]
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

export function WidgetGallery({ disabled, onAdd }: { disabled: boolean; onAdd: (kind: WidgetKind) => void }) {
  return <div className="dd-widget-gallery" aria-label="Available widgets">
    {(Object.keys(widgetKinds) as WidgetKind[]).map((kind) => <button key={kind} type="button" disabled={disabled} onClick={() => onAdd(kind)}>
      <WidgetSymbol kind={kind} /><span><strong>{widgetKinds[kind].name}</strong><small>{widgetKinds[kind].subtitle}</small></span><span aria-hidden="true" className="dd-add-symbol">+</span>
    </button>)}
  </div>;
}

type DashboardEntity = HAEntity & { area_name?: string | null; device_name?: string | null };
const maxReportAgeMs = 60 * 60 * 1000;

function reportExpiresAt(entity: DashboardEntity) {
  return Date.parse(entity.source_reported_at ?? "") + maxReportAgeMs;
}

function EntityPicker({ weather, selected, disabled, onChange }: {
  weather: boolean; selected: string; disabled: boolean; onChange: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [entities, setEntities] = useState<DashboardEntity[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [asOf, setAsOf] = useState(() => Date.now());
  const [cacheRefresh, setCacheRefresh] = useState(0);
  const available = entities.filter((entity) => reportExpiresAt(entity) > asOf);
  const expiresAt = Math.min(...available.map(reportExpiresAt));

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      const parameters = new URLSearchParams({
        purpose: "dashboard", domain: weather ? "weather" : "sensor", search: query.trim(), limit: "24"
      });
      try {
        const result = await apiJson<{ entities: DashboardEntity[]; error?: string | null }>(`/api/home-assistant/entities?${parameters}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setAsOf(Date.now());
        setEntities(result.entities);
        setError(result.error ?? "");
      } catch (reason) {
        if (!controller.signal.aborted) {
          setEntities([]);
          setError(reason instanceof Error ? reason.message : "Unable to load entities.");
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }, 160);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [query, weather, cacheRefresh]);

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
    if (reportExpiresAt(entity) > now) {
      onChange(entity.entity_id);
    } else {
      setAsOf(now);
      setCacheRefresh((count) => count + 1);
    }
  };
  const current = entities.find((entity) => entity.entity_id === selected);
  return <div className="dd-entity-picker">
    <label>{weather ? "Search weather providers" : "Search Home Assistant sensors"}
      <input type="search" value={query} placeholder={weather ? "Provider name" : "Name or entity ID"} disabled={disabled}
        onKeyDown={(event) => {
          if (event.key !== "Enter") return;
          event.preventDefault();
          if (!loading && !error && available.length === 1) select(available[0]);
        }}
        onChange={(event) => choose(event.target.value)} />
    </label>
    <div className="dd-picker-quick">
      <span className="dd-selected-entity">{current?.name || selected || (weather ? "Automatic provider" : "No entity selected")}</span>
      {weather ? <button type="button" className="dd-inline-button" disabled={disabled || !selected} onClick={() => onChange("")}>Use automatic</button> :
        <button type="button" className="dd-inline-button" disabled={disabled} onClick={() => choose("battery")}>Find batteries</button>}
    </div>
    {loading ? <p className="dd-hint" role="status">Searching…</p> : error ? <p className="warning-text" role="status">{error}</p> : available.length ?
      <div className="dd-entity-results" aria-label="Matching entities">
        {available.map((entity) => <button type="button" key={entity.entity_id} aria-pressed={entity.entity_id === selected} disabled={disabled} onClick={() => select(entity)}>
          <span><strong>{entity.name || entity.entity_id}</strong><small>{[entity.area_name, entity.entity_id].filter(Boolean).join(" · ")}</small></span>
          {entity.state && <span className="dd-entity-state">{entity.state}</span>}
        </button>)}
      </div> : <p className="dd-hint" role="status">{query.trim() ? "No entities match your search." : "No matching entities are available yet."}</p>}
  </div>;
}

export function WidgetSettings({ widget, widgets, disabled, onChange, onRemove }: {
  widget: DashboardWidget; widgets: DashboardWidget[]; disabled: boolean;
  onChange: (widget: DashboardWidget) => void; onRemove: () => void;
}) {
  const kind = widgetKind(widget);
  const available = (spans[widget.type] ?? [[widget.w, widget.h]])
    .filter(([w, h]) => fits(widgets, widget, w, h, widget));
  const positions = tiles.filter((tile) => fits(widgets, tile, widget.w, widget.h, widget));
  return <div className="dd-widget-fields" aria-label="Widget settings">
    {(kind === "home_assistant" || kind === "weather") && <EntityPicker weather={kind === "weather"}
      selected={displayText(widget.source?.entity_id)} disabled={disabled}
      onChange={(entityId) => onChange({ ...widget, source: { ...widget.source, entity_id: entityId || undefined } })} />}
    <label>Label<input value={displayText(widget.options?.label)} maxLength={80} disabled={disabled}
      onChange={(event) => onChange({ ...widget, options: { ...widget.options, label: event.target.value } })} /></label>
    {kind === "text" && <label>Text<textarea value={displayText(widget.options?.text)} maxLength={120} rows={2} disabled={disabled}
      onChange={(event) => onChange({ ...widget, options: { ...widget.options, text: event.target.value } })} /></label>}
    {kind === "home_assistant" && <label><span>Unit <span className="dd-optional">(optional)</span></span><input value={displayText(widget.options?.unit)} maxLength={24}
      placeholder="Use entity unit" disabled={disabled} onChange={(event) => onChange({ ...widget, options: { ...widget.options, unit: event.target.value } })} /></label>}
    <div className="dd-field-pair">
      <label>Size<select value={`${widget.w}x${widget.h}`} disabled={disabled || available.length < 2} onChange={(event) => {
        const [w, h] = event.target.value.split("x").map(Number);
        onChange({ ...widget, w, h });
      }}>
        {available.map(([w, h]) => <option key={`${w}x${h}`} value={`${w}x${h}`}>{w} × {h} {w * h === 1 ? "tile" : "tiles"}</option>)}
      </select></label>
      <label>Position<select value={`${widget.x}:${widget.y}`} disabled={disabled || positions.length < 2} onChange={(event) => {
        const [x, y] = event.target.value.split(":").map(Number);
        onChange({ ...widget, x, y });
      }}>
        {positions.map(({ x, y }) => <option key={`${x}:${y}`} value={`${x}:${y}`}>Row {y + 1}, column {x + 1}</option>)}
      </select></label>
    </div>
    <button type="button" className="danger-text-button" disabled={disabled} onClick={onRemove}>Remove widget</button>
  </div>;
}
