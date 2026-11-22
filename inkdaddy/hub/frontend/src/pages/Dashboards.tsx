import { useEffect, useRef, useState } from "react";
import { EpaperPreview } from "../components/EpaperPreview";
import { apiFetch, apiJson, apiPath } from "../lib/api";
import type { DashboardConfig, DashboardRecord, DashboardWidget, DeviceRecord, HAEntity } from "../lib/types";

const widgetKinds = [
  ["home_assistant", "Home Assistant entity"],
  ["clock", "Time"],
  ["location", "Home location"],
  ["weather", "Local weather"],
  ["device_battery", "Display battery"],
  ["text", "Text note"]
] as const;

type WidgetKind = typeof widgetKinds[number][0];
type Tile = { x: number; y: number };
const tiles: Tile[] = Array.from({ length: 8 }, (_, index) => ({ x: index % 4, y: Math.floor(index / 4) }));

const emptyConfig: DashboardConfig = { name: "", grid: { cols: 4, rows: 2 }, widgets: [] };

export function DashboardsPage() {
  const [items, setItems] = useState<DashboardRecord[]>([]);
  const [devices, setDevices] = useState<DeviceRecord[]>([]);
  const [active, setActive] = useState<DashboardRecord | null>(null);
  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [importing, setImporting] = useState(false);
  const importInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };
    apiJson<{ dashboards: DashboardRecord[] }>("/api/dashboards", options).then(({ dashboards }) => {
      if (controller.signal.aborted) return;
      setItems(dashboards);
      setReady(true);
    }).catch((reason: Error) => {
      if (!controller.signal.aborted) setError(reason.message);
    });
    apiJson<{ devices: DeviceRecord[] }>("/api/devices", options).then(({ devices: next }) => {
      if (!controller.signal.aborted) setDevices(next);
    }).catch(() => {});
    return () => controller.abort();
  }, []);

  const open = (dashboard: DashboardRecord | null) => {
    if (dirty && !window.confirm("Discard unsaved changes and open another dashboard?")) return;
    setMessage("");
    setDirty(false);
    setActive(dashboard);
    setEditing(true);
  };

  const saved = (dashboard: DashboardRecord) => {
    setItems((previous) => previous.some((item) => item.id === dashboard.id)
      ? previous.map((item) => item.id === dashboard.id ? dashboard : item) : [...previous, dashboard]);
    setActive(dashboard);
    setMessage(`${dashboard.name} saved.`);
  };

  const importDashboard = async (file: File) => {
    setImporting(true);
    setMessage("");
    setError("");
    try {
      const yaml = await file.text();
      if (!yaml.trim()) throw new Error("The dashboard file is empty.");
      const response = await apiFetch("/api/dashboards/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yaml })
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(typeof payload?.detail === "string" ? payload.detail : "Unable to import dashboard.");
      }
      const dashboard: DashboardRecord = await response.json();
      setItems((previous) => [...previous, dashboard]);
      setMessage(`${dashboard.name} imported.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to import dashboard.");
    } finally {
      setImporting(false);
    }
  };

  const remove = async (dashboard: DashboardRecord) => {
    await apiJson(`/api/dashboards/${encodeURIComponent(dashboard.id)}`, { method: "DELETE" });
    setItems((previous) => previous.filter((item) => item.id !== dashboard.id));
    setActive(null);
    setEditing(false);
    setDirty(false);
    setMessage(`${dashboard.name} deleted.`);
  };

  return (
    <div className="page-grid">
      <header className="page-header">
        <div><h1>Dashboards</h1><p>Build panels with live Home Assistant entities and add them to your display playlist.</p></div>
        <div className="form-row">
          <button className="secondary-button" disabled={!ready || importing} onClick={() => importInput.current?.click()}>
            {importing ? "Importing…" : "Import YAML"}
          </button>
          <input ref={importInput} type="file" accept=".yaml,.yml,application/yaml,application/x-yaml" hidden onChange={(event) => {
            const file = event.currentTarget.files?.[0];
            event.currentTarget.value = "";
            if (file) void importDashboard(file);
          }} />
          <button className="primary-button" onClick={() => open(null)} disabled={editing && !active}>New dashboard</button>
        </div>
      </header>
      {error && <p className="warning-text" role="alert">{error}</p>}
      {message && <p className="preview-message" role="status">{message}</p>}
      <div className="dashboard-layout">
        <section className="workflow-panel dashboard-library" aria-label="Saved dashboards">
          <h2>Saved dashboards</h2>
          {items.map((item) => (
            <button key={item.id} className="dashboard-library-item" aria-pressed={editing && active?.id === item.id}
              onClick={() => open(item)} disabled={editing && active?.id === item.id}>
              <strong>{item.name}</strong>
              <span>{item.id === "sample" ? "Included dashboard" : `${item.config.widgets.length} widgets`}</span>
            </button>
          ))}
          {!items.length && !error && <p className="muted-text">{ready ? "No dashboards yet." : "Loading dashboards…"}</p>}
        </section>
        {editing ? <DashboardEditor key={active?.id ?? "new"} dashboard={active} devices={devices} onSave={saved}
          onDelete={remove} onDirty={setDirty} onClose={() => { setEditing(false); setDirty(false); }} /> : (
          <section className="empty-state compact"><div><h2>Choose a dashboard</h2><p>Open a saved dashboard or create one for your display.</p></div></section>
        )}
      </div>
    </div>
  );
}

function DashboardEditor({ dashboard, devices, onSave, onDelete, onDirty, onClose }: {
  dashboard: DashboardRecord | null;
  devices: DeviceRecord[];
  onSave: (record: DashboardRecord) => void;
  onDelete: (record: DashboardRecord) => Promise<void>;
  onDirty: (dirty: boolean) => void;
  onClose: () => void;
}) {
  const savedConfig = dashboard ? { ...dashboard.config, name: dashboard.name } : emptyConfig;
  const [draft, setDraft] = useState<DashboardConfig>(savedConfig);
  const [tile, setTile] = useState<Tile>(tiles[0]);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [deviceUid, setDeviceUid] = useState("");
  const [busy, setBusy] = useState<"save" | "preview" | "delete" | null>(null);
  const [error, setError] = useState("");
  const previewPanel = useRef<HTMLDivElement>(null);
  const previewRequest = useRef<AbortController | null>(null);
  const widget = widgetAt(draft.widgets, tile);
  const dirty = JSON.stringify(draft) !== JSON.stringify(savedConfig);
  const firstBlockedColumn = widget ? tiles.find((position) => position.y === widget.y && position.x > widget.x &&
    draft.widgets.some((item) => item !== widget && widgetAt([item], position)))?.x : undefined;
  const maxWidth = widget ? (firstBlockedColumn ?? 4) - widget.x : 0;

  useEffect(() => onDirty(dirty), [dirty, onDirty]);

  useEffect(() => {
    if (!previewUrl) return;
    previewPanel.current?.focus({ preventScroll: true });
    previewPanel.current?.scrollIntoView({ block: "nearest" });
    return () => URL.revokeObjectURL(previewUrl);
  }, [previewUrl]);

  useEffect(() => () => previewRequest.current?.abort(), []);

  const change = (config: DashboardConfig) => {
    setDraft(config);
    setPreviewUrl(null);
  };

  const setWidget = (next: DashboardWidget | null) => {
    const widgets = draft.widgets.filter((item) => item !== widget);
    change({ ...draft, widgets: next ? [...widgets, next] : widgets });
  };

  const setKind = (kind: WidgetKind) => {
    const editingTile = widget ?? tile;
    const widgetType = kind === "text" ? "text_note" : "entity_state";
    const base = { id: widget?.id ?? `tile-${tile.x}-${tile.y}`, x: editingTile.x, y: editingTile.y };
    setWidget({ ...base, type: widgetType, w: 1, h: 1,
      options: { label: widgetKinds.find(([id]) => id === kind)?.[1] },
      source: kind === "text" ? {} : { type: kind } });
  };

  const configPayload = () => JSON.stringify({ yaml: JSON.stringify({ ...draft, name: draft.name.trim() }), device_uid: deviceUid || undefined });

  const save = async () => {
    setBusy("save");
    setError("");
    try {
      const path = dashboard ? `/api/dashboards/${encodeURIComponent(dashboard.id)}` : "/api/dashboards";
      const saved = await apiJson<DashboardRecord>(path, { method: dashboard ? "PUT" : "POST", body: configPayload() });
      setDraft({ ...saved.config, name: saved.name });
      onSave(saved);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to save dashboard.");
    } finally {
      setBusy(null);
    }
  };

  const preview = async () => {
    const controller = new AbortController();
    previewRequest.current = controller;
    setBusy("preview");
    setError("");
    try {
      const response = await apiFetch("/api/dashboards/preview", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: configPayload(), signal: controller.signal
      });
      if (!response.ok) {
        const payload: { detail?: unknown } = await response.json().catch(() => ({}));
        throw new Error(typeof payload.detail === "string" ? payload.detail : "Unable to render the preview.");
      }
      if (!response.headers.get("content-type")?.startsWith("image/")) throw new Error("The preview did not return an image.");
      const content = await response.blob();
      if (!controller.signal.aborted) setPreviewUrl(URL.createObjectURL(content));
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Unable to render the preview.");
    } finally {
      if (!controller.signal.aborted) setBusy(null);
    }
  };

  const remove = async () => {
    if (!dashboard || !window.confirm(`Delete “${dashboard.name}”? This removes its saved configuration.`)) return;
    setBusy("delete");
    setError("");
    try {
      await onDelete(dashboard);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to delete dashboard.");
      setBusy(null);
    }
  };

  return (
    <div className="side-stack">
      <section className="workflow-panel form-stack">
        <div className="preview-toolbar">
          <h2>{dashboard ? "Edit dashboard" : "New dashboard"}</h2>
          <button className="secondary-button" disabled={Boolean(busy)} onClick={() => {
            if (!dirty || window.confirm("Discard unsaved dashboard changes?")) onClose();
          }}>Close editor</button>
        </div>
        <label>
          Dashboard name
          <input value={draft.name} maxLength={100} disabled={Boolean(busy)} onChange={(event) => change({ ...draft, name: event.target.value })} />
        </label>
        <div className="dashboard-tiles" aria-label="Dashboard tile layout">
          {tiles.map((position) => {
            const item = widgetAt(draft.widgets, position);
            if (item && (item.x !== position.x || item.y !== position.y)) return null;
            return <button className="dashboard-tile" key={`${position.x}:${position.y}`}
              style={{ gridColumn: `${position.x + 1} / span ${item?.w ?? 1}`, gridRow: `${position.y + 1} / span ${item?.h ?? 1}` }}
              aria-pressed={position.x === tile.x && position.y === tile.y}
              aria-label={`Row ${position.y + 1}, column ${position.x + 1}: ${item ? widgetTitle(item) : "Empty tile"}`}
              onClick={() => setTile(position)} disabled={Boolean(busy)}>
              <span>{item ? widgetTitle(item) : "Add widget"}</span>
            </button>;
          })}
        </div>
        <WidgetFields key={`${tile.x}:${tile.y}:${widget?.id ?? "empty"}`} widget={widget} maxWidth={maxWidth}
          onKind={setKind} onChange={setWidget} disabled={Boolean(busy)} />
        <div className="dashboard-editor-actions">
          {devices.length > 0 && (
            <label>
              Preview for
              <select value={deviceUid} disabled={Boolean(busy)} onChange={(event) => { setDeviceUid(event.target.value); setPreviewUrl(null); }}>
                <option value="">Any display</option>
                {devices.map((device) => <option key={device.device_uid} value={device.device_uid}>{device.name}</option>)}
              </select>
            </label>
          )}
          {dashboard && <a className="secondary-link" href={apiPath(`/api/dashboards/${encodeURIComponent(dashboard.id)}/export`)}
            download={`${dashboard.id}.yaml`}>Download saved YAML</a>}
          <button className="secondary-button" disabled={Boolean(busy)} onClick={() => void preview()}>{busy === "preview" ? "Rendering…" : "Preview"}</button>
          <button className="primary-button" disabled={Boolean(busy) || !draft.name.trim()} onClick={() => void save()}>{busy === "save" ? "Saving…" : "Save dashboard"}</button>
        </div>
        {error && <p className="warning-text" role="alert">{error}</p>}
        {dashboard && dashboard.id !== "sample" && (
          <button className="danger-text-button" disabled={Boolean(busy)} onClick={() => void remove()}>Delete dashboard</button>
        )}
      </section>
      {previewUrl && <div ref={previewPanel} tabIndex={-1} className="dashboard-preview">
        <EpaperPreview title="Dashboard preview" caption={draft.name || "Untitled dashboard"} previewUrl={previewUrl} empty="Preview unavailable." />
      </div>}
    </div>
  );
}

function WidgetFields({ widget, maxWidth, onKind, onChange, disabled }: {
  widget?: DashboardWidget;
  maxWidth: number;
  onKind: (kind: WidgetKind) => void;
  onChange: (widget: DashboardWidget | null) => void;
  disabled: boolean;
}) {
  const kind = editorWidgetKind(widget);
  const knownKind = widgetKinds.some(([id]) => id === kind);
  const widths = (widget?.type === "text_note" ? [1, 2, 4] : [1, 2]).filter((width) => width <= maxWidth);
  return (
    <div className="dashboard-widget-fields form-stack">
      <label>
        Widget
        <select value={kind} onChange={(event) => onKind(event.target.value as WidgetKind)} disabled={disabled}>
          {!knownKind && <option value={kind}>{widget ? widget.type.replaceAll("_", " ") : "Choose a widget"}</option>}
          {widgetKinds.map(([id, label]) => <option value={id} key={id}>{label}</option>)}
        </select>
      </label>
      {widget && (
        <>
          <label>
            Label
            <input value={widget.options?.label ?? ""} maxLength={80} disabled={disabled}
              onChange={(event) => onChange({ ...widget, options: { ...widget.options, label: event.target.value } })} />
          </label>
          {kind === "text" && <label>
            Text
            <input value={widget.options?.text ?? ""} maxLength={120} disabled={disabled}
              onChange={(event) => onChange({ ...widget, options: { ...widget.options, text: event.target.value } })} />
          </label>}
          {widget.h === 1 && widths.length > 1 && ["entity_state", "text_note"].includes(widget.type) && <label>
            Width
            <select value={widget.w} disabled={disabled} onChange={(event) => onChange({ ...widget, w: Number(event.target.value) })}>
              {widths.map((width) => <option value={width} key={width}>{width === 1 ? "1 tile" : `${width} tiles`}</option>)}
            </select>
          </label>}
          {(kind === "home_assistant" || kind === "weather") && (
            <EntityPicker key={kind} weather={kind === "weather"} selected={widget.source?.entity_id ?? ""} disabled={disabled}
              onChange={(entityId) => onChange({ ...widget, source: { ...widget.source, entity_id: entityId || undefined } })} />
          )}
          <button className="secondary-button dashboard-remove-widget" disabled={disabled} onClick={() => onChange(null)}>Remove widget</button>
        </>
      )}
    </div>
  );
}

function EntityPicker({ weather, selected, disabled, onChange }: {
  weather: boolean;
  selected: string;
  disabled: boolean;
  onChange: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [entities, setEntities] = useState<HAEntity[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      const parameters = new URLSearchParams({ search: query.trim(), limit: "60" });
      if (weather) parameters.set("domain", "weather");
      try {
        const result = await apiJson<{ entities: HAEntity[]; error?: string | null }>(`/api/home-assistant/entities?${parameters}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setEntities(result.entities);
        setError(result.error ?? "");
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Unable to load entities.");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [query, weather]);

  return (
    <div className="form-stack entity-picker">
      <div className="entity-search">
        <label>
          Search Home Assistant entities
          <input type="search" value={query} placeholder={weather ? "Weather provider" : "Name or entity ID"} disabled={disabled}
            onChange={(event) => { setLoading(true); setQuery(event.target.value); }} />
        </label>
        {!weather && <button className="secondary-button" disabled={disabled} onClick={() => { if (query !== "battery") setLoading(true); setQuery("battery"); }}>Find batteries</button>}
      </div>
      <label>
        {weather ? "Weather entity" : "Home Assistant entity"}
        <select value={selected} disabled={disabled || loading} onChange={(event) => onChange(event.target.value)}>
          <option value="">{weather ? "Use the first available weather provider" : "Choose an entity"}</option>
          {selected && !entities.some((entity) => entity.entity_id === selected) && <option value={selected}>{selected}</option>}
          {entities.map((entity) => <option key={entity.entity_id} value={entity.entity_id}>
            {entity.name && entity.name !== entity.entity_id ? `${entity.name} · ${entity.entity_id}` : entity.entity_id}
          </option>)}
        </select>
      </label>
      {error ? <p className="warning-text" role="alert">{error}</p> : !loading && !entities.length && (
        <p className="muted-text">{query.trim() ? "No entities match your search." : "No entities available. Refresh the cache on the Home Assistant page."}</p>
      )}
    </div>
  );
}

function widgetAt(widgets: DashboardWidget[], { x, y }: Tile): DashboardWidget | undefined {
  return widgets.find((widget) => x >= widget.x && x < widget.x + widget.w && y >= widget.y && y < widget.y + widget.h);
}

function editorWidgetKind(widget?: DashboardWidget): string {
  const sourceKind = widget?.source?.type ?? "";
  if (sourceKind !== "text" && widgetKinds.some(([kind]) => kind === sourceKind)) return sourceKind;
  if (sourceKind === "entity" || widget?.source?.entity_id) return "home_assistant";
  return widget?.type === "text_note" ? "text" : sourceKind;
}

function widgetTitle(widget: DashboardWidget): string {
  return widget.options?.label || widget.source?.entity_id || widget.type.replaceAll("_", " ");
}
