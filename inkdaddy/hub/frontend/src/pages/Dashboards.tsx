import { useEffect, useRef, useState } from "react";
import { EpaperPreview } from "../components/EpaperPreview";
import { apiFetch, apiJson, apiPath } from "../lib/api";
import type { DashboardConfig, DashboardRecord, DashboardWidget, DeviceRecord } from "../lib/types";
import {
  createWidget, dashboardPresets, displayText, presetWidgets, tiles, WidgetGallery, WidgetSettings, WidgetSymbol, widgetAt, widgetKind,
  widgetKinds, widgetTitle, type Tile, type WidgetKind
} from "./DashboardWidgets";
import "./dashboards.css";

const emptyConfig: DashboardConfig = { name: "", grid: { cols: 4, rows: 2 }, widgets: [] };

function dashboardError(reason: unknown, fallback: string) {
  if (!(reason instanceof Error)) return fallback;
  try {
    const detail = JSON.parse(reason.message)?.detail;
    if (typeof detail === "string") return detail;
  } catch {
    return reason.message;
  }
  return reason.message;
}

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
  const editorHeading = useRef<HTMLHeadingElement>(null);
  const newButton = useRef<HTMLButtonElement>(null);

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
    window.requestAnimationFrame(() => editorHeading.current?.focus());
  };

  const close = () => {
    setEditing(false);
    setDirty(false);
    window.requestAnimationFrame(() => newButton.current?.focus());
  };

  const saved = (dashboard: DashboardRecord) => {
    setItems((previous) => previous.some((item) => item.id === dashboard.id)
      ? previous.map((item) => item.id === dashboard.id ? dashboard : item) : [...previous, dashboard]);
    setActive(dashboard);
    setMessage(`${dashboard.name} saved.`);
    if (!active) window.requestAnimationFrame(() => editorHeading.current?.focus());
  };

  const importDashboard = async (file: File) => {
    setImporting(true);
    setMessage("");
    setError("");
    try {
      const yaml = await file.text();
      if (!yaml.trim()) throw new Error("The dashboard file is empty.");
      const dashboard = await apiJson<DashboardRecord>("/api/dashboards/import", { method: "POST", body: JSON.stringify({ yaml }) });
      setItems((previous) => [...previous, dashboard]);
      setMessage(`${dashboard.name} imported.`);
    } catch (reason) {
      setError(dashboardError(reason, "Unable to import dashboard."));
    } finally {
      setImporting(false);
    }
  };

  const remove = async (dashboard: DashboardRecord) => {
    await apiJson(`/api/dashboards/${encodeURIComponent(dashboard.id)}`, { method: "DELETE" });
    setItems((previous) => previous.filter((item) => item.id !== dashboard.id));
    setActive(null);
    close();
    setMessage(`${dashboard.name} deleted.`);
  };

  return <div className="page-grid dd-page">
    <header className="page-header">
      <div><h1>Dashboards</h1><p>Build a screen with live Home Assistant data.</p></div>
      <div className="form-row">
        <button className="secondary-button" disabled={!ready || importing} onClick={() => importInput.current?.click()}>
          {importing ? "Importing…" : "Import YAML"}
        </button>
        <input ref={importInput} type="file" accept=".yaml,.yml,application/yaml,application/x-yaml" hidden onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = "";
          if (file) void importDashboard(file);
        }} />
        <button ref={newButton} className="primary-button" onClick={() => open(null)} disabled={editing && !active}>New dashboard</button>
      </div>
    </header>
    {error && <p className="warning-text" role="alert">{error}</p>}
    {message && <p className="preview-message" role="status">{message}</p>}
    <div className="dd-workspace" data-editing={editing}>
      <section className="dd-library workflow-panel" aria-label="Saved dashboards">
        <h2>Saved dashboards</h2>
        <div className="dd-library-items">{items.map((item) => <button key={item.id} className="dd-library-item"
          aria-pressed={editing && active?.id === item.id} onClick={() => open(item)} disabled={editing && active?.id === item.id}>
          <span className="dd-library-mark" aria-hidden="true"><i /><i /><i /></span>
          <span><strong>{item.name}</strong><small>{item.id === "sample" ? "Included dashboard" : `${item.config.widgets.length} widgets`}</small></span>
        </button>)}</div>
        {!items.length && !error && <p className="dd-hint">{ready ? "No dashboards yet." : "Loading dashboards…"}</p>}
      </section>
      {editing ? <DashboardEditor key={active?.id ?? "new"} dashboard={active} devices={devices} heading={editorHeading} onSave={saved}
        onDelete={remove} onDirty={setDirty} onClose={close} /> : <section className="dd-empty workflow-panel">
        <h2>What’s on your display?</h2>
        <p>Open a saved dashboard or start with a layout you can make your own.</p>
        <button className="secondary-button" onClick={() => open(null)}>Create a dashboard</button>
      </section>}
    </div>
  </div>;
}

function DashboardEditor({ dashboard, devices, heading, onSave, onDelete, onDirty, onClose }: {
  dashboard: DashboardRecord | null;
  devices: DeviceRecord[];
  heading: React.RefObject<HTMLHeadingElement | null>;
  onSave: (record: DashboardRecord) => void;
  onDelete: (record: DashboardRecord) => Promise<void>;
  onDirty: (dirty: boolean) => void;
  onClose: () => void;
}) {
  const savedConfig = dashboard ? { ...dashboard.config, name: dashboard.name } : emptyConfig;
  const [draft, setDraft] = useState<DashboardConfig>(savedConfig);
  const [tile, setTile] = useState<Tile>(tiles[0]);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [chosenDevice, setChosenDevice] = useState<string>();
  const [busy, setBusy] = useState<"save" | "preview" | "delete" | null>(null);
  const [error, setError] = useState("");
  const previewPanel = useRef<HTMLDivElement>(null);
  const previewRequest = useRef<AbortController | null>(null);
  const inspector = useRef<HTMLHeadingElement>(null);
  const widget = widgetAt(draft.widgets, tile);
  const kind = widget && widgetKind(widget);
  const vacant = tiles.find((position) => !widgetAt(draft.widgets, position));
  const occupied = draft.widgets.reduce((sum, item) => sum + item.w * item.h, 0);
  const dirty = JSON.stringify(draft) !== JSON.stringify(savedConfig);
  const deviceUid = chosenDevice ?? (devices.length === 1 ? devices[0].device_uid : "");

  useEffect(() => onDirty(dirty), [dirty, onDirty]);
  useEffect(() => {
    if (!previewUrl) return;
    previewPanel.current?.focus();
    return () => URL.revokeObjectURL(previewUrl);
  }, [previewUrl]);
  useEffect(() => () => previewRequest.current?.abort(), []);

  const change = (config: DashboardConfig) => {
    setDraft(config);
    setPreviewUrl(null);
    setError("");
  };

  const focusInspector = () => window.requestAnimationFrame(() => inspector.current?.focus());

  const selectTile = (position: Tile) => {
    setTile(position);
    focusInspector();
  };

  const updateWidget = (next: DashboardWidget) => {
    const widgets = draft.widgets.map((item) => item === widget ? next : item);
    change({ ...draft, widgets });
    if (next.x !== tile.x || next.y !== tile.y) setTile({ x: next.x, y: next.y });
  };

  const addWidget = (kind: WidgetKind) => {
    change({ ...draft, widgets: [...draft.widgets, createWidget(kind, tile, draft.widgets)] });
    focusInspector();
  };

  const removeWidget = () => {
    change({ ...draft, widgets: draft.widgets.filter((item) => item !== widget) });
    focusInspector();
  };

  const configPayload = () => JSON.stringify({ yaml: JSON.stringify({ ...draft, name: draft.name.trim() }), device_uid: deviceUid || undefined });

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy || !draft.name.trim()) return;
    setBusy("save");
    setError("");
    try {
      const path = dashboard ? `/api/dashboards/${encodeURIComponent(dashboard.id)}` : "/api/dashboards";
      const saved = await apiJson<DashboardRecord>(path, { method: dashboard ? "PUT" : "POST", body: configPayload() });
      setDraft({ ...saved.config, name: saved.name });
      onSave(saved);
    } catch (reason) {
      setError(dashboardError(reason, "Unable to save dashboard."));
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
      setError(dashboardError(reason, "Unable to delete dashboard."));
      setBusy(null);
    }
  };

  return <div className="dd-editor-stack">
    <form className="dd-editor" onSubmit={(event) => void save(event)}>
      <header className="dd-editor-top">
        <h2 ref={heading} tabIndex={-1}>{dashboard ? "Edit dashboard" : "New dashboard"}</h2>
        <button type="button" className="dd-close" disabled={Boolean(busy)} onClick={() => {
          if (!dirty || window.confirm("Discard unsaved dashboard changes?")) onClose();
        }}>Close editor</button>
      </header>
      <div className="dd-editor-body">
        <label className="dd-name">Dashboard name
          <input value={draft.name} maxLength={100} disabled={Boolean(busy)} placeholder="e.g. Kitchen display"
            onChange={(event) => change({ ...draft, name: event.target.value })} />
        </label>
        {!dashboard && !draft.widgets.length && <section className="dd-presets" aria-label="Dashboard presets">
          <h3>Start with a layout</h3>
          <div>{Object.entries(dashboardPresets).map(([id, preset]) => <button key={id} type="button" disabled={Boolean(busy)} onClick={() => {
            change({ ...draft, name: draft.name || preset.name, widgets: presetWidgets(preset) });
            setTile(tiles[0]);
            focusInspector();
          }}><span className="dd-preset-diagram" data-preset={id} aria-hidden="true"><i /><i /><i /><i /></span>
            <span><strong>{preset.name}</strong><small>{preset.detail}</small></span></button>)}</div>
        </section>}
        <div className="dd-builder">
          <section className="dd-layout" aria-labelledby="dd-layout-title">
            <div className="dd-section-heading">
              <div><h3 id="dd-layout-title">Layout</h3><p>{occupied} of 8 tiles used</p></div>
              <button type="button" className="secondary-button dd-add-widget" disabled={Boolean(busy) || !vacant}
                onClick={() => { if (vacant) selectTile(vacant); }}>Add widget</button>
            </div>
            <div className="dd-canvas" aria-label="Dashboard tile layout">
              {tiles.map((position) => {
                const item = widgetAt(draft.widgets, position);
                if (item && (item.x !== position.x || item.y !== position.y)) return null;
                const kind = item ? widgetKind(item) : undefined;
                const note = kind === "text" ? displayText(item?.options?.text) : "";
                return <button type="button" className="dd-tile" key={`${position.x}:${position.y}`} data-filled={Boolean(item)} data-kind={kind}
                  style={{ gridColumn: `${position.x + 1} / span ${item?.w ?? 1}`, gridRow: `${position.y + 1} / span ${item?.h ?? 1}` }}
                  aria-pressed={position.x === tile.x && position.y === tile.y} disabled={Boolean(busy)}
                  aria-label={`${item ? widgetTitle(item) : "Add widget"}, row ${position.y + 1}, column ${position.x + 1}`}
                  onClick={() => selectTile(position)}>
                  {item ? <><WidgetSymbol kind={kind} /><strong>{widgetTitle(item)}</strong>{note && <small>{note}</small>}</> :
                    <span className="dd-empty-tile" aria-hidden="true">+</span>}
                </button>;
              })}
            </div>
          </section>
          <section className="dd-inspector" aria-labelledby="dd-inspector-title">
            <div className="dd-section-heading">
              <div><h3 tabIndex={-1} ref={inspector} id="dd-inspector-title">{widget ? "Edit widget" : "Choose a widget"}</h3>
                <p>{widget ? (kind ? widgetKinds[kind].name : widget.type.replaceAll("_", " ")) :
                  `Row ${tile.y + 1}, column ${tile.x + 1}`}</p></div>
            </div>
            {widget ? <WidgetSettings key={widget.id} widget={widget} widgets={draft.widgets} disabled={Boolean(busy)} onChange={updateWidget}
              onRemove={removeWidget} /> : <WidgetGallery disabled={Boolean(busy)} onAdd={addWidget} />}
          </section>
        </div>
        <footer className="dd-editor-footer">
          <div className="dd-preview-actions">
            {devices.length > 0 && <label>Preview for<select value={deviceUid} disabled={Boolean(busy)} onChange={(event) => {
              setChosenDevice(event.target.value); setPreviewUrl(null);
            }}><option value="">Any display</option>
              {devices.map((device) => <option key={device.device_uid} value={device.device_uid}>{device.name}</option>)}
            </select></label>}
            <button type="button" className="secondary-button" disabled={Boolean(busy) || !draft.widgets.length} onClick={() => void preview()}>{busy === "preview" ? "Rendering…" : "Preview"}</button>
          </div>
          <button type="submit" className="primary-button" disabled={Boolean(busy) || !draft.name.trim()}>{busy === "save" ? "Saving…" : "Save dashboard"}</button>
        </footer>
        {error && <p className="warning-text" role="alert">{error}</p>}
        {dashboard && <div className="dd-secondary-actions">
          <a className="secondary-link" href={apiPath(`/api/dashboards/${encodeURIComponent(dashboard.id)}/export`)} download={`${dashboard.id}.yaml`}>Download saved YAML</a>
          {dashboard.id !== "sample" && <button type="button" className="danger-text-button" disabled={Boolean(busy)} onClick={() => void remove()}>Delete dashboard</button>}
        </div>}
      </div>
    </form>
    {previewUrl && <div ref={previewPanel} tabIndex={-1} className="dashboard-preview">
      <EpaperPreview title="Dashboard preview" caption={devices.find((device) => device.device_uid === deviceUid)?.name || draft.name || "Untitled dashboard"}
        previewUrl={previewUrl} empty="Preview unavailable.">
        <button className="secondary-button" onClick={() => { setPreviewUrl(null); heading.current?.focus(); }}>Back to editor</button>
      </EpaperPreview>
    </div>}
  </div>;
}
