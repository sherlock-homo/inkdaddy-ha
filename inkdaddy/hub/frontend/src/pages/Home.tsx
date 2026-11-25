import { useCallback, useEffect, useRef, useState } from "react";
import { EpaperPreview } from "../components/EpaperPreview";
import { StatusBadge } from "../components/StatusBadge";
import { apiJson, apiPath } from "../lib/api";
import type { DeviceRecord, FrameSlot, Playback, PlaybackSource } from "../lib/types";
import { PlaylistEditor } from "./PlaylistEditor";

type PlaybackAction = "previous_content" | "next_content" | "refresh_current" | "pause" | "play";

const actionMessages: Record<PlaybackAction, string> = {
  previous_content: "Previous item selected.",
  next_content: "Next item selected.",
  refresh_current: "Refresh requested.",
  pause: "Slideshow paused.",
  play: "Slideshow resumed."
};

export function Home({ onConnect, preferredUid, onDisplaySelected, playlistSource, onPlaylistSourceUsed }: {
  onConnect: () => void;
  preferredUid: string;
  onDisplaySelected: (uid: string) => void;
  playlistSource: PlaybackSource | null;
  onPlaylistSourceUsed: () => void;
}) {
  const [devices, setDevices] = useState<DeviceRecord[]>([]);
  const [selectedUid, setSelectedUid] = useState(preferredUid);
  const [registryReady, setRegistryReady] = useState(false);
  const [registryError, setRegistryError] = useState("");
  const [playback, setPlayback] = useState<Playback | null>(null);
  const [playbackError, setPlaybackError] = useState("");
  const [actionError, setActionError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [editingPath, setEditingPath] = useState<string | null>(null);
  const [draftSource, setDraftSource] = useState<PlaybackSource | null>(null);
  const [draftDirty, setDraftDirty] = useState(false);
  const [savingPlaylist, setSavingPlaylist] = useState(false);
  const actionInFlight = useRef(false);
  const actionRevision = useRef(0);
  const display = devices.find((device) => device.device_uid === selectedUid)
    ?? devices.find((device) => device.mesh_connected) ?? devices[0];
  const deviceUid = display?.device_uid;
  const playbackPath = deviceUid ? `/api/devices/${encodeURIComponent(deviceUid)}/playback` : null;
  const editingQueue = editingPath !== null && editingPath === playbackPath;

  useEffect(() => {
    if (!playlistSource || !playback || !playbackPath) return;
    setDraftSource(playlistSource);
    setDraftDirty(true);
    setEditingPath(playbackPath);
    onPlaylistSourceUsed();
  }, [playlistSource, playback, playbackPath, onPlaylistSourceUsed]);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      try {
        const payload = await apiJson<{ devices: DeviceRecord[] }>("/api/devices", { signal: controller.signal });
        if (controller.signal.aborted) return;
        setDevices(payload.devices);
        setSelectedUid((current) => payload.devices.some((device) => device.device_uid === current)
          ? current : (payload.devices.find((device) => device.mesh_connected) ?? payload.devices[0])?.device_uid ?? "");
        setRegistryError("");
        setRegistryReady(true);
      } catch {
        if (!controller.signal.aborted) setRegistryError("Display registry unavailable.");
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 12000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    setPlayback(null);
    setPlaybackError("");
    setActionError("");
    setMessage("");
    setEditingPath(null);
    setDraftSource(null);
    setDraftDirty(false);
    if (!playbackPath) return;
    const controller = new AbortController();
    let inFlight = false;
    const load = async () => {
      if (inFlight || actionInFlight.current) return;
      inFlight = true;
      const revision = actionRevision.current;
      try {
        const next = await apiJson<Playback>(playbackPath, { signal: controller.signal });
        if (controller.signal.aborted || actionRevision.current !== revision) return;
        setPlayback(next);
        setPlaybackError("");
      } catch {
        if (!controller.signal.aborted && actionRevision.current === revision) {
          setPlaybackError("Unable to check playback. Retrying…");
        }
      } finally {
        inFlight = false;
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 4000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [playbackPath]);

  const performAction = async (action: PlaybackAction) => {
    if (!deviceUid || actionInFlight.current) return;
    actionInFlight.current = true;
    ++actionRevision.current;
    setBusy(true);
    setMessage("");
    setActionError("");
    try {
      const result = await apiJson<{ playback: Playback }>(
        `/api/devices/${encodeURIComponent(deviceUid)}/manual-refresh?action=${action}`,
        { method: "POST" }
      );
      ++actionRevision.current;
      setPlayback(result.playback);
      setMessage(actionMessages[action]);
    } catch {
      setActionError("Unable to send the playback command. Check the display and try again.");
    } finally {
      actionInFlight.current = false;
      setBusy(false);
    }
  };

  const playlistSaved = useCallback((next: Playback) => {
    ++actionRevision.current;
    setPlayback(next);
    setEditingPath(null);
    setDraftSource(null);
    setDraftDirty(false);
    setMessage("Playlist saved.");
  }, []);

  function togglePlaylist() {
    if (editingQueue) {
      if (draftDirty && !window.confirm("Discard changes to this playlist?")) return;
      setEditingPath(null);
      setDraftSource(null);
      setDraftDirty(false);
    } else {
      setEditingPath(playbackPath);
    }
  }

  const current = playback?.current;
  const pending = playback?.pending;
  const pendingFailed = playback?.pending_status === "failed";
  const upcoming = playback?.upcoming ?? [];
  let scheduleLabel = `Refreshes every ${display?.refresh_interval_minutes ?? 60} minutes`;
  if (display?.interlace_dashboard_id) scheduleLabel = `Photo and dashboard alternate every ${display.refresh_interval_minutes / 2} minutes`;
  if (playback?.paused) scheduleLabel = "Slideshow paused";
  const controlsDisabled = busy || editingQueue || !playback || Boolean(playbackError);

  return (
    <div className="page-grid">
      <header className="page-header">
        <h1>Displays</h1>
        {devices.length > 1 && (
          <label className="display-picker">
            Display
            <select value={deviceUid} disabled={busy || editingQueue} onChange={(event) => {
              setPlayback(null);
              setSelectedUid(event.target.value);
              onDisplaySelected(event.target.value);
            }}>
              {devices.map((device) => <option key={device.device_uid} value={device.device_uid}>{device.name}</option>)}
            </select>
          </label>
        )}
        {registryReady && (!display || !display.provisioned) && (
          <button className="primary-button" onClick={onConnect}>Connect a display</button>
        )}
      </header>

      {registryError && <p className="warning-text" role="alert">{registryError}</p>}
      {!registryReady && !registryError && <p className="muted-text" role="status">Loading displays…</p>}
      {registryReady && !display && (
        <section className="empty-state compact">
          <div><h2>No displays yet</h2>
            {playlistSource && <p>Connect a display to add this item to its playlist.</p>}
          </div>
        </section>
      )}

      {display && (
        <>
          <div className="playback-layout">
            <EpaperPreview
              title="On display"
              caption={current ? current.title : display.name}
              previewUrl={current ? apiPath(current.preview_url) : null}
              empty="The display hasn’t confirmed its first frame yet."
            >
              <div className="playback-controls" aria-label="Slideshow controls">
                <button className="secondary-button" disabled={controlsDisabled} onClick={() => void performAction("previous_content")}>
                  <span aria-hidden="true">‹</span> Previous
                </button>
                <button className="primary-button" disabled={controlsDisabled} onClick={() => void performAction(playback?.paused ? "play" : "pause")}>
                  {playback?.paused ? "Resume" : "Pause"}
                </button>
                <button className="secondary-button" disabled={controlsDisabled} onClick={() => void performAction("next_content")}>
                  Next <span aria-hidden="true">›</span>
                </button>
                <button className="icon-button playback-refresh" aria-label="Refresh current frame" title="Refresh current frame" disabled={controlsDisabled} onClick={() => void performAction("refresh_current")}>
                  ↻
                </button>
              </div>
              {pending && (
                <div className="pending-frame">
                  <img src={apiPath(pending.preview_url)} alt="" />
                  <div>
                    <strong>{pending.title}</strong>
                    {pendingFailed ? (
                      <span role="alert">Refresh failed. Select Refresh to try again.</span>
                    ) : (
                      <span role="status">Waiting for display confirmation</span>
                    )}
                  </div>
                </div>
              )}
              {message && <p className="preview-message" role="status">{message}</p>}
              {playback?.syncing_controls && (
                <p className="preview-message" role="status">Syncing play/pause with the display…</p>
              )}
              {actionError && <p className="warning-text" role="alert">{actionError}</p>}
              {playbackError && <p className="warning-text" role="alert">{playbackError}</p>}
            </EpaperPreview>

            <section className="workflow-panel upcoming-panel" aria-labelledby="upcoming-title">
              <div className="preview-toolbar upcoming-header">
                <div>
                  <h2 id="upcoming-title">Up next</h2>
                  <p>{scheduleLabel}</p>
                </div>
                <button className="secondary-button" disabled={busy || savingPlaylist || !playback} onClick={togglePlaylist}>
                  {editingQueue ? "Close editor" : "Edit playlist"}
                </button>
              </div>
              {editingQueue && playbackPath && playback ? (
                <PlaylistEditor
                  key={playbackPath}
                  path={playbackPath}
                  playlist={playback.playlist}
                  alternating={Boolean(display.interlace_dashboard_id)}
                  defaultSource={display.selected_source_id && ["dashboard", "album", "photo"].includes(display.selected_source_type)
                    ? { type: display.selected_source_type as PlaybackSource["type"], id: display.selected_source_id }
                    : { type: "dashboard", id: "sample" }}
                  initialSource={draftSource}
                  onSave={playlistSaved}
                  onDirtyChange={setDraftDirty}
                  onBusyChange={setSavingPlaylist}
                />
              ) : (
                upcoming.length ? <ol className="upcoming-list">{upcoming.map((frame) => <UpcomingFrame key={frame.scheduled_at} frame={frame} />)}</ol>
                  : <p className="muted-text">{playback ? "No frames are scheduled." : "Checking the schedule…"}</p>
              )}
            </section>
          </div>

          <section className="display-details" aria-label="Display status">
            <span><StatusBadge tone={display.mesh_connected ? "good" : "warn"}>{display.mesh_connected ? "Thread online at last check" : "No recent Thread connection"}</StatusBadge></span>
            <span><strong>Battery</strong> {display.battery_percent == null ? "Unavailable" : `${Math.round(display.battery_percent)}%`}</span>
            <span><strong>Last seen</strong> {display.last_seen_at ? formatDate(display.last_seen_at) : "Never"}</span>
          </section>
        </>
      )}
    </div>
  );
}

function UpcomingFrame({ frame }: { frame: FrameSlot }) {
  return (
    <li>
      <img src={apiPath(frame.preview_url)} loading="lazy" alt="" />
      <span>
        <strong>{frame.title}</strong>
        <time dateTime={frame.scheduled_at}>{formatDate(frame.scheduled_at)}</time>
      </span>
    </li>
  );
}

function formatDate(value: string): string {
  const timestamp = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`;
  const moment = new Date(timestamp);
  return moment.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit",
    second: moment.getSeconds() ? "2-digit" : undefined });
}
