import { useCallback, useEffect, useState, type FormEvent } from "react";
import { apiJson } from "../lib/api";
import type { DeviceHistoryRecord, DeviceRecord } from "../lib/types";

export function DevicesPage({ onConnect, onOpenDisplay }: { onConnect: () => void; onOpenDisplay: (uid: string) => void }) {
  const [devices, setDevices] = useState<DeviceRecord[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (signal?: AbortSignal) => {
    setBusy(true);
    setError("");
    try {
      const result = await apiJson<{ devices: DeviceRecord[] }>("/api/devices", { signal });
      if (!signal?.aborted) setDevices(result.devices);
    } catch {
      if (!signal?.aborted) setError("Unable to load displays.");
    } finally {
      if (!signal?.aborted) setBusy(false);
    }
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
      {devices?.map((device) => <DeviceCard key={device.device_uid} device={device} refreshing={busy} onOpenDisplay={onOpenDisplay}
        onRename={(updated) => setDevices((rows) => rows?.map((row) => row.device_uid === updated.device_uid ? updated : row) ?? null)} />)}
    </section>
  </div>;
}

function DeviceCard({ device, refreshing, onOpenDisplay, onRename }: {
  device: DeviceRecord;
  refreshing: boolean;
  onOpenDisplay: (uid: string) => void;
  onRename: (device: DeviceRecord) => void;
}) {
  const [name, setName] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [history, setHistory] = useState<DeviceHistoryRecord[] | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [renameMessage, setRenameMessage] = useState("");
  const [renameError, setRenameError] = useState("");
  const route = `/api/devices/${encodeURIComponent(device.device_uid)}`;

  async function rename(event: FormEvent) {
    event.preventDefault();
    if (!name?.trim() || refreshing || saving) return;
    setSaving(true);
    setRenameError("");
    setRenameMessage("");
    try {
      onRename(await apiJson<DeviceRecord>(route, { method: "PUT", body: JSON.stringify({ name }) }));
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
