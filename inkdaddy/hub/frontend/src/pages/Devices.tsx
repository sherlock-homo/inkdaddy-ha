import { useCallback, useEffect, useState, type FormEvent } from "react";
import { apiJson } from "../lib/api";
import type { DashboardRecord, DeviceHistoryRecord, DeviceRecord } from "../lib/types";

export function DevicesPage({ onConnect, onOpenDisplay }: { onConnect: () => void; onOpenDisplay: (uid: string) => void }) {
  const [devices, setDevices] = useState<DeviceRecord[] | null>(null);
  const [dashboards, setDashboards] = useState<DashboardRecord[] | null>(null);
  const [dashboardError, setDashboardError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (signal?: AbortSignal) => {
    setBusy(true);
    setError("");
    setDashboardError("");
    const [registry, layouts] = await Promise.allSettled([
      apiJson<{ devices: DeviceRecord[] }>("/api/devices", { signal }),
      apiJson<{ dashboards: DashboardRecord[] }>("/api/dashboards", { signal }),
    ]);
    if (signal?.aborted) return;
    if (registry.status === "fulfilled") setDevices(registry.value.devices);
    else setError("Unable to load displays.");
    if (layouts.status === "fulfilled") setDashboards(layouts.value.dashboards);
    else {
      setDashboards(null);
      setDashboardError("Unable to load dashboards. Refresh the device list to try again.");
    }
    setBusy(false);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  return <div className="page-grid">
    <header className="page-header"><h1>Devices</h1><button className="primary-button" onClick={onConnect}>Connect a display</button></header>
    {error && <p role="alert">{error}</p>}
    <div className="button-row">
      <button className="secondary-button" disabled={busy} onClick={() => void load()}>{busy ? "Loading displays…" : "Refresh device list"}</button>
    </div>
    {devices && !devices.length && <section className="empty-state compact"><h2>No displays registered</h2></section>}
    <section className="card-grid">
      {devices?.map((device) => <DeviceCard key={device.device_uid} device={device} dashboards={dashboards} dashboardError={dashboardError}
        refreshing={busy} onOpenDisplay={onOpenDisplay}
        onUpdate={(updated) => setDevices((rows) => rows?.map((row) => row.device_uid === updated.device_uid ? updated : row) ?? null)} />)}
    </section>
  </div>;
}

function DeviceCard({ device, dashboards, dashboardError, refreshing, onOpenDisplay, onUpdate }: {
  device: DeviceRecord;
  dashboards: DashboardRecord[] | null;
  dashboardError: string;
  refreshing: boolean;
  onOpenDisplay: (uid: string) => void;
  onUpdate: (device: DeviceRecord) => void;
}) {
  const [name, setName] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [history, setHistory] = useState<DeviceHistoryRecord[] | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [renameMessage, setRenameMessage] = useState("");
  const [renameError, setRenameError] = useState("");
  const [interval, setInterval] = useState<string | null>(null);
  const [draftDashboard, setDraftDashboard] = useState<string | null>();
  const [savingSchedule, setSavingSchedule] = useState(false);
  const [scheduleMessage, setScheduleMessage] = useState("");
  const [scheduleError, setScheduleError] = useState("");
  const route = `/api/devices/${encodeURIComponent(device.device_uid)}`;
  const minutes = Number(interval ?? device.refresh_interval_minutes);

  const savedDashboard = device.interlace_dashboard_id ?? null;
  const dashboard = draftDashboard === undefined ? savedDashboard : draftDashboard;
  const intervalValid = Number.isInteger(minutes) && minutes >= 10 && minutes <= 3600;
  const canSave = intervalValid && (minutes !== device.refresh_interval_minutes || dashboard !== savedDashboard)
    && (dashboard === null || dashboard === savedDashboard || dashboards?.some((item) => item.id === dashboard));

  function clearScheduleFeedback() {
    setScheduleMessage("");
    setScheduleError("");
  }

  async function saveSchedule(event: FormEvent) {
    event.preventDefault();
    if (!canSave || refreshing || savingSchedule || saving) return;
    setSavingSchedule(true);
    clearScheduleFeedback();
    try {
      onUpdate(await apiJson<DeviceRecord>(route, {
        method: "PUT", body: JSON.stringify({
          refresh_interval_minutes: minutes !== device.refresh_interval_minutes ? minutes : undefined,
          interlace_dashboard_id: dashboard !== savedDashboard ? dashboard : undefined,
        }),
      }));
      setInterval(null);
      setDraftDashboard(undefined);
      setScheduleMessage("Playback schedule saved.");
    } catch {
      setScheduleError("Unable to save the playback schedule. Refresh the device list and try again.");
    } finally {
      setSavingSchedule(false);
    }
  }

  async function rename(event: FormEvent) {
    event.preventDefault();
    if (!name?.trim() || refreshing || saving || savingSchedule) return;
    setSaving(true);
    setRenameError("");
    setRenameMessage("");
    try {
      onUpdate(await apiJson<DeviceRecord>(route, { method: "PUT", body: JSON.stringify({ name }) }));
      setName(null);
      setRenameMessage("Display renamed.");
    } catch {
      setRenameError("Unable to save the display name.");
    } finally {
      setSaving(false);
    }
  }

  async function loadHistory() {
    setLoadingHistory(true);
    setHistoryError("");
    try {
      setHistory((await apiJson<{ history: DeviceHistoryRecord[] }>(`${route}/history?limit=8`)).history);
    } catch {
      setHistoryError("Unable to load device history.");
    } finally {
      setLoadingHistory(false);
    }
  }

  return <article className="asset-card device-card">
    <div className="preview-toolbar">
      <h2>{device.name}</h2>
      <span className={`status-badge ${device.provisioned ? "status-good" : "status-warn"}`}>{device.provisioned ? "Registered" : "Setup needed"}</span>
    </div>
    <dl className="mini-list">
      <div><dt>Battery</dt><dd>{device.battery_percent == null ? "Unavailable" : `${Math.round(device.battery_percent)}%`}</dd></div>
      <div><dt>Firmware</dt><dd>{device.firmware_version ?? "Unknown"}</dd></div>
      <div><dt>Last seen</dt><dd>{device.last_seen_at ? localDate(device.last_seen_at) : "Never"}</dd></div>
    </dl>
    <div className="button-row">
      <button className="primary-button" onClick={() => onOpenDisplay(device.device_uid)}>Open Display</button>
      {name === null && <button className="secondary-button" onClick={() => { setRenameMessage(""); setName(device.name); }}>Rename</button>}
    </div>
    {name !== null && <form className="form-stack" onSubmit={(event) => void rename(event)}>
      <label>Display name <input required maxLength={255} value={name} disabled={saving} onChange={(event) => setName(event.target.value)} /></label>
      <div className="button-row">
        <button className="primary-button" disabled={saving || refreshing || !name.trim() || name.trim() === device.name}>Save name</button>
        <button className="secondary-button" type="button" disabled={saving} onClick={() => { setName(null); setRenameError(""); }}>Cancel</button>
      </div>
    </form>}
    {renameMessage && <p role="status">{renameMessage}</p>}
    {renameError && <p className="warning-text" role="alert">{renameError}</p>}
    <form className="form-stack" onSubmit={(event) => void saveSchedule(event)}>
      <label>Playback mode
        <select value={dashboard ? "alternate" : "playlist"} disabled={savingSchedule}
          onChange={(event) => { setDraftDashboard(event.target.value === "alternate" ? savedDashboard || "sample" : null); clearScheduleFeedback(); }}>
          <option value="playlist">Playlist</option>
          <option value="alternate">Alternate photos and dashboard</option>
        </select>
      </label>
      {dashboard && <>
        <label>Full-screen dashboard
          <select value={dashboard} disabled={savingSchedule || !dashboards}
            onChange={(event) => { setDraftDashboard(event.target.value); clearScheduleFeedback(); }}>
            {!dashboards?.some((item) => item.id === dashboard) &&
              <option value={dashboard}>{dashboardError ? dashboard : "Loading dashboard…"}</option>}
            {dashboards?.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
          </select>
        </label>
        {dashboardError && <p className="warning-text" role="alert">{dashboardError}</p>}
      </>}
      <label>{dashboard ? "Cycle length (minutes)" : "Refresh interval (minutes)"}
        <input type="number" required min={10} max={3600} step={1} disabled={savingSchedule}
          value={interval ?? device.refresh_interval_minutes}
          onChange={(event) => { setInterval(event.target.value); clearScheduleFeedback(); }} />
      </label>
      {dashboard && intervalValid && <p className="muted-text">
        Each photo and dashboard stays on screen for {minutes / 2} minutes.
      </p>}
      {intervalValid && (dashboard ? minutes / 2 : minutes) < 15 && <p className="warning-text">Frequent updates use more battery.</p>}
      <div className="button-row">
        <button className="secondary-button" disabled={savingSchedule || saving || refreshing || !canSave}>
          {savingSchedule ? "Saving schedule…" : "Save schedule"}
        </button>
      </div>
    </form>
    {scheduleMessage && <p role="status">{scheduleMessage}</p>}
    {scheduleError && <p role="alert">{scheduleError}</p>}
    <details onToggle={(event) => {
      if (event.currentTarget.open && history === null && !loadingHistory && !historyError) void loadHistory();
    }}>
      <summary>Recent activity</summary>
      {loadingHistory && <p role="status">Loading activity…</p>}
      {historyError && <p role="alert">{historyError}</p>}
      {history && !history.length && <p>No device events recorded.</p>}
      {history && history.length > 0 && <ul className="history-list">
        {history.map((event, index) => <li key={index}>{statusLabel(event.status)} · {localDate(event.created_at)}</li>)}
      </ul>}
      <button className="secondary-button" disabled={loadingHistory} onClick={() => void loadHistory()}>Refresh activity</button>
    </details>
  </article>;
}

function statusLabel(status: string): string {
  if (["displayed_hub_frame", "refresh:ok", "ok"].includes(status)) return "Frame confirmed";
  if (status === "matter_frame_failed") return "Frame delivery failed";
  if (status === "manual_refresh_requested") return "Refresh requested";
  if (status === "heartbeat") return "Display checked in";
  return "Display status reported";
}

function localDate(timestamp: string): string {
  return new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(timestamp) ? timestamp : `${timestamp}Z`).toLocaleString();
}
