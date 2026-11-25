import { useCallback, useEffect, useState } from "react";
import { apiJson } from "../lib/api";

export type Framing = "fit" | "fill" | "intelligent";
type Settings = {
  mode: "supervisor" | "standalone";
  version: string;
  photo_upload_mode: Framing;
  scheduled_refresh_minutes: number;
  target: { width: number; height: number };
};

export function FramingOptions() {
  return <>
    <option value="fit">Fit whole photo</option>
    <option value="fill">Center crop</option>
    <option value="intelligent">Intelligent Crop</option>
  </>;
}

export function SimplePage({ route }: { route: "settings" | "diagnostics" | "updates" }) {
  if (route === "updates") return <UpdatesPage />;
  if (route === "diagnostics") return <DiagnosticsPage />;
  return <SettingsPage />;
}

function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [mode, setMode] = useState<Framing>("fit");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const load = useCallback(async () => {
    setBusy(true);
    setFeedback("");
    try {
      const saved = await apiJson<Settings>("/api/settings");
      setSettings(saved);
      setMode(saved.photo_upload_mode);
    } catch {
      setFeedback("Unable to load settings.");
    } finally {
      setBusy(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function save() {
    setBusy(true);
    setFeedback("");
    try {
      const saved = await apiJson<Settings>("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ photo_upload_mode: mode })
      });
      setSettings(saved);
      setMode(saved.photo_upload_mode);
      setFeedback("Photo framing saved.");
    } catch {
      setFeedback("Unable to save photo framing.");
    } finally {
      setBusy(false);
    }
  }
  return <div className="page-grid">
    <header className="page-header"><h1>Settings</h1></header>
    <section className="workflow-panel form-stack">
      {settings ? <>
        <label>Default photo framing
          <select value={mode} disabled={busy} onChange={(event) => setMode(event.target.value as Framing)}><FramingOptions /></select>
        </label>
        <div className="button-row">
          <button className="primary-button" disabled={busy || mode === settings.photo_upload_mode} onClick={() => void save()}>Save framing</button>
        </div>
        <dl className="update-list">
          <div><dt>Display size</dt><dd>{settings.target.width} × {settings.target.height}</dd></div>
          <div><dt>Default refresh interval</dt><dd>{settings.scheduled_refresh_minutes} minutes</dd></div>
        </dl>
        <a className="secondary-link" href="#devices">Manage display schedules</a>
      </> : !busy && <div className="button-row"><button className="secondary-button" onClick={() => void load()}>Retry</button></div>}
      {(feedback || busy) && <p role="status">{feedback || "Working…"}</p>}
    </section>
  </div>;
}

type Diagnostics = {
  app: string;
  version: string;
  mode: "supervisor" | "standalone";
  matter_controller: "ready" | "not_configured" | "unavailable";
  registered_displays: number;
  matter_links: number;
  ready_photos: number;
  cached_entities: number;
  last_displayed_at: string | null;
};

function DiagnosticsPage() {
  const [report, setReport] = useState<Diagnostics | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      setReport(await apiJson<Diagnostics>("/api/diagnostics"));
    } catch {
      setError("Unable to load diagnostics.");
    } finally {
      setBusy(false);
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  return <div className="page-grid">
    <header className="page-header">
      <h1>Diagnostics</h1>
      <button className="secondary-button" disabled={busy} onClick={() => void refresh()}>{busy ? "Checking…" : "Refresh diagnostics"}</button>
    </header>
    {error && <p role="alert">{error}</p>}
    {report && <section className="update-panel">
      <dl className="update-list">
        <div><dt>App version</dt><dd>{report.version}</dd></div>
        <div><dt>Installation</dt><dd>{report.mode === "supervisor" ? "Home Assistant app" : "Standalone"}</dd></div>
        <div><dt>Matter frame service</dt><dd>{{ ready: "Ready", not_configured: "Not configured", unavailable: "Unavailable" }[report.matter_controller]}</dd></div>
        <div><dt>Registered displays</dt><dd>{report.registered_displays}</dd></div>
        <div><dt>Linked Matter displays</dt><dd>{report.matter_links}</dd></div>
        <div><dt>Last confirmed refresh</dt><dd>{report.last_displayed_at ? new Date(report.last_displayed_at).toLocaleString() : "No confirmation yet"}</dd></div>
        <div><dt>Ready photos</dt><dd>{report.ready_photos}</dd></div>
        <div><dt>Cached Home Assistant entities</dt><dd>{report.cached_entities}</dd></div>
      </dl>
      {report.mode === "supervisor" && <div className="button-row">
        <a className="secondary-link" href="/config/thread" target="_parent">Thread settings</a>
        <a className="secondary-link" href="/config/logs" target="_parent">Home Assistant logs</a>
      </div>}
    </section>}
  </div>;
}

type HubUpdate = {
  status: string;
  current_version: string;
  latest_version?: string | null;
  update_available?: boolean;
  auto_update?: boolean;
  manage_path?: string;
};

function DisplayFirmware() {
  type Display = { device_id: string; name: string; firmware_version?: string };
  const [registry, setRegistry] = useState<{ available: boolean; devices: Display[] } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void apiJson<{ available: boolean; devices: Display[] }>("/api/matter-frames/home-assistant", { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) setRegistry(result); })
      .catch(() => { if (!controller.signal.aborted) setRegistry({ available: false, devices: [] }); });
    return () => controller.abort();
  }, []);
  if (registry === null) return <p role="status">Loading registered displays…</p>;
  if (!registry.available || !registry.devices.length) return <>
    <p>{registry.available ? "Home Assistant has no registered inkDaddy displays." : "Home Assistant’s device registry is unavailable."}</p>
    <a className="secondary-link" href="/config/devices/dashboard" target="_parent">Open Home Assistant devices</a>
  </>;
  return <ul className="firmware-displays">{registry.devices.map((device) =>
    <li key={device.device_id}>
      <a href={`/config/devices/device/${encodeURIComponent(device.device_id)}`} target="_parent">{device.name}</a>
      <span>{device.firmware_version ? `Firmware ${device.firmware_version}` : "Firmware version unavailable"}</span>
    </li>
  )}</ul>;
}

function UpdatesPage() {
  const [update, setUpdate] = useState<HubUpdate | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const check = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      setUpdate(await apiJson<HubUpdate>("/api/updates/hub/check"));
    } catch {
      setError("Unable to check for updates.");
    } finally {
      setBusy(false);
    }
  }, []);
  useEffect(() => { void check(); }, [check]);

  const managed = update?.status === "managed_by_home_assistant";
  return <div className="page-grid">
    <header className="page-header"><h1>Updates</h1></header>
    {error && <p role="alert">{error}</p>}
    <div className="update-grid">
      <section className="update-panel">
        <h2>inkDaddy app</h2>
        {update && <dl className="update-list">
          <div><dt>Installed</dt><dd>{update.current_version}</dd></div>
          {update.latest_version && update.latest_version !== update.current_version && <div><dt>Latest version</dt><dd>{update.latest_version}</dd></div>}
          {managed && typeof update.auto_update === "boolean" && <div><dt>Automatic updates</dt><dd>{update.auto_update ? "On" : "Off"}</dd></div>}
        </dl>}
        {managed ? <>
          {update.latest_version === update.current_version && <p>Up to date.</p>}
          <a className="secondary-link" href={update.manage_path ?? "/config/apps"} target="_parent">
            {update.update_available ? "Update in Home Assistant" : "Manage app in Home Assistant"}
          </a>
        </> : <>
          {update && <p>{update.update_available ? "A newer release is available." : update.status === "current" ? "Up to date." : update.status === "not_configured" ? "A release channel has not been configured." : "Release information is unavailable."}</p>}
          <button className="secondary-button" disabled={busy} onClick={() => void check()}>{busy ? "Checking…" : "Check for updates"}</button>
        </>}
      </section>
      <section className="update-panel">
        <h2>Display firmware</h2>
        {managed ? <DisplayFirmware /> : update && <p>Check your Matter controller for firmware updates.</p>}
      </section>
    </div>
  </div>;
}
