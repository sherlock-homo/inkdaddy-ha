import { useCallback, useEffect, useState, type FormEvent } from "react";
import { apiJson } from "../lib/api";
import type { DeviceRecord, Playback } from "../lib/types";

type MatterNode = { node_id: string; device_uid: string | null; linked: boolean };
type MatterStatus = { enabled: boolean; available: boolean; nodes: MatterNode[] };
type HomeAssistantDisplay = { device_id: string; name: string; device_uid: string };

export function Connect({ onOpenDisplay }: { onOpenDisplay: (uid: string) => void }) {
  const [devices, setDevices] = useState<DeviceRecord[]>([]);
  const [matter, setMatter] = useState<MatterStatus | null>(null);
  const [haDisplays, setHaDisplays] = useState<HomeAssistantDisplay[]>([]);
  const [discoveryAvailable, setDiscoveryAvailable] = useState<boolean | null>(null);
  const [linkedUid, setLinkedUid] = useState("");
  const [firstFrameStatus, setFirstFrameStatus] = useState("");
  const [loadError, setLoadError] = useState("");
  const [haSelection, setHaSelection] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);

  const load = useCallback(async (refresh = false, signal?: AbortSignal) => {
    const [displays, status, ha] = await Promise.allSettled([
      apiJson<{ devices: DeviceRecord[] }>("/api/devices", { signal }),
      apiJson<MatterStatus>(refresh ? "/api/matter-frames/check" : "/api/matter-frames", { method: refresh ? "POST" : "GET", signal }),
      apiJson<{ available: boolean; devices: HomeAssistantDisplay[] }>("/api/matter-frames/home-assistant", { signal })
    ]);
    if (signal?.aborted) return;
    if (displays.status === "fulfilled") setDevices(displays.value.devices);
    if (status.status === "fulfilled") setMatter(status.value);
    if (ha.status === "fulfilled") {
      setDiscoveryAvailable(ha.value.available);
      setHaDisplays(ha.value.devices);
    } else {
      setDiscoveryAvailable(false);
      setHaDisplays([]);
    }
    setLoadError(displays.status === "rejected" || status.status === "rejected" ? "Could not load display connections." : "");
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(false, controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (!linkedUid) return;
    const controller = new AbortController();
    let timeout: number | undefined;
    async function check() {
      try {
        const state = await apiJson<Playback>(`/api/devices/${encodeURIComponent(linkedUid)}/playback`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (state.current) {
          setFirstFrameStatus("The display confirmed its first frame.");
          return;
        }
        setFirstFrameStatus(state.pending_status === "failed"
          ? "The first refresh failed. Open Display to retry."
          : state.pending ? "First frame queued. Waiting for display confirmation." : "Preparing the first frame.");
      } catch {
        if (controller.signal.aborted) return;
        setFirstFrameStatus("The display is linked. Open Display to check the first refresh.");
      }
      timeout = window.setTimeout(() => void check(), 4000);
    }
    void check();
    return () => {
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [linkedUid]);

  const connect = async (route: "pair" | "link" | "pair-home-assistant", body: Record<string, string>) => {
    setBusy(true);
    setLinkedUid("");
    setFirstFrameStatus("");
    setFailed(false);
    setMessage(route === "link" ? "Linking the display." : "Connecting to the display over Thread…");
    try {
      const node = await apiJson<MatterNode>(`/api/matter-frames/${route}`, {
        method: "POST",
        body: JSON.stringify(body)
      });
      if (node.linked && node.device_uid) {
        setLinkedUid(node.device_uid);
        setMessage("Display connected.");
      } else {
        setMessage("Display shared. Check connections, then choose Link display.");
      }
      await load();
    } catch (error) {
      setFailed(true);
      setMessage(connectionMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const pair = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!code.trim()) return;
    const sharingCode = code.trim();
    setCode("");
    void connect("pair", { code: sharingCode });
  };

  const setupButtons = async (uid: string) => {
    setBusy(true);
    setFailed(false);
    setMessage("Setting up Home Assistant playback buttons…");
    try {
      await apiJson(`/api/matter-frames/home-assistant/${encodeURIComponent(uid)}/playback-buttons`, { method: "POST" });
      setMessage("Next, Previous, Play/Pause and Refresh are ready on the Home Assistant device page.");
    } catch (error) {
      setFailed(true);
      setMessage(connectionMessage(error, "Could not set up playback buttons. Try again."));
    } finally {
      setBusy(false);
    }
  };

  const checkConnections = async () => {
    setBusy(true);
    try {
      await load(waiting);
    } finally {
      setBusy(false);
    }
  };

  const candidates = matter?.nodes.filter((node) => !node.linked && node.device_uid) ?? [];
  const waiting = matter?.nodes.some((node) => !node.linked && !node.device_uid) ?? false;
  const matterDisplays = new Set(matter?.nodes.filter((node) => node.linked).map((node) => node.device_uid));
  const registeredDisplays = devices.filter((device) => matterDisplays.has(device.device_uid) || device.last_seen_at);
  const unlinkedHaDisplays = haDisplays.filter((device) => !matterDisplays.has(device.device_uid));
  const nativeSelection = unlinkedHaDisplays.find((device) => device.device_id === haSelection)
    ?? unlinkedHaDisplays[0];

  const codeForm = (
    <form className="pairing-code-form" onSubmit={pair} autoComplete="off">
      <p>Open the display’s Matter device page in Home Assistant and choose <strong>Share device</strong>.</p>
      <label>
        Sharing code
        <input
          value={code}
          onChange={(event) => setCode(event.target.value)}
          disabled={busy || !matter?.available}
          maxLength={515}
          spellCheck={false}
        />
      </label>
      <div className="button-row">
        <button className="primary-button" disabled={busy || !matter?.available || !code.trim()}>
          {busy ? "Connecting…" : "Connect with code"}
        </button>
      </div>
    </form>
  );

  return (
    <div className="page-grid">
      <header className="page-header">
        <h1>Connect a display</h1>
      </header>

      <section className="connect-flow">
        <div className="qr-card">
          <div>
            <h2>Pair in Home Assistant</h2>
            <p>In the Home Assistant mobile app, add a Matter device and scan the code on your display.</p>
            <div className="button-row">
              <button className="secondary-button" disabled={busy} onClick={() => void checkConnections()}>Check connections</button>
            </div>
          </div>
        </div>
        {matter?.enabled && (
          <div className="info-panel vertical">
            <h2>Share with inkDaddy</h2>
            {nativeSelection ? (
              <>
                <label>
                  Home Assistant display
                  <select value={nativeSelection.device_id} disabled={busy} onChange={(event) => setHaSelection(event.target.value)}>
                    {unlinkedHaDisplays.map((device) => <option key={device.device_id} value={device.device_id}>{device.name}</option>)}
                  </select>
                </label>
                <div className="button-row">
                  <button className="primary-button" disabled={busy || !matter.available}
                    onClick={() => void connect("pair-home-assistant", { device_id: nativeSelection.device_id })}>
                    {busy ? "Connecting…" : "Connect display"}
                  </button>
                </div>
                <details><summary>Use a sharing code</summary>{codeForm}</details>
              </>
            ) : <>
              {discoveryAvailable === false && <p>Automatic discovery is unavailable. You can use a sharing code.</p>}
              {codeForm}
            </>}
            {!matter.available && <p>The Matter controller is starting or temporarily unavailable.</p>}
          </div>
        )}
      </section>

      {matter && !matter.enabled && <p role="status">Matter delivery is unavailable in this installation.</p>}
      {loadError && <p className="warning-text" role="alert">{loadError}</p>}
      {(message || linkedUid) && <section className="info-panel vertical">
        <p role={failed ? "alert" : "status"}>{message}</p>
        {firstFrameStatus && <p role="status">{firstFrameStatus}</p>}
        {linkedUid && <div className="button-row">
          <button className="primary-button" onClick={() => onOpenDisplay(linkedUid)}>Open display</button>
        </div>}
      </section>}

      {(candidates.length > 0 || waiting) && (
        <section className="info-panel vertical">
          <h2>Shared displays</h2>
          {candidates.map((node) => (
            <div className="row-item" key={node.node_id}>
              <span>{node.device_uid}</span>
              <button className="secondary-button" disabled={busy} onClick={() => void connect("link", { node_id: node.node_id })}>
                Link display
              </button>
            </div>
          ))}
          {waiting && <p>Waiting for the shared display…</p>}
        </section>
      )}

      {registeredDisplays.length > 0 && <section className="info-panel vertical">
        <h2>Connected displays</h2>
        {registeredDisplays.map((device) => (
          <div className="row-item" key={device.device_uid}>
            <strong>{device.name}</strong>
            <div className="button-row">
              <button className="secondary-button" onClick={() => onOpenDisplay(device.device_uid)}>Open display</button>
              {matterDisplays.has(device.device_uid) && haDisplays.some((row) => row.device_uid === device.device_uid) && (
                <button className="secondary-button" disabled={busy} onClick={() => void setupButtons(device.device_uid)}>
                  Set up playback buttons
                </button>
              )}
            </div>
          </div>
        ))}
      </section>}
    </div>
  );
}

function connectionMessage(error: unknown, fallback = "Could not connect the display. Check the sharing window and try again."): string {
  if (error instanceof Error) {
    try {
      const detail: unknown = JSON.parse(error.message).detail;
      if (typeof detail === "string" && detail.length <= 160) return detail;
    } catch {
      // Fall back to the connection message below.
    }
  }
  return fallback;
}
