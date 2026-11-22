import { useEffect, useState, type FormEvent } from "react";
import { apiJson } from "../lib/api";
import type { DeviceRecord } from "../lib/types";

type MatterNode = { node_id: string; device_uid: string | null; linked: boolean };
type MatterStatus = { enabled: boolean; available: boolean; nodes: MatterNode[] };
type HomeAssistantDisplay = { device_id: string; name: string; device_uid: string };

export function Connect() {
  const [devices, setDevices] = useState<DeviceRecord[]>([]);
  const [matter, setMatter] = useState<MatterStatus | null>(null);
  const [haDisplays, setHaDisplays] = useState<HomeAssistantDisplay[]>([]);
  const [haSelection, setHaSelection] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);

  const load = async (refresh = false) => {
    const [displayList, matterStatus, ha] = await Promise.all([
      apiJson<{ devices: DeviceRecord[] }>("/api/devices"),
      apiJson<MatterStatus>(refresh ? "/api/matter-frames?refresh=true" : "/api/matter-frames"),
      apiJson<{devices: HomeAssistantDisplay[]}>("/api/matter-frames/home-assistant")
    ]);
    setDevices(displayList.devices);
    setMatter(matterStatus);
    setHaDisplays(ha.devices);
  };

  useEffect(() => {
    void load().catch(() => {
      setFailed(true);
      setMessage("Could not load display connections.");
    });
  }, []);

  const connect = async (route: "pair" | "link" | "pair-home-assistant", body: Record<string, string>) => {
    setBusy(true);
    setFailed(false);
    setMessage(route === "link" ? "Linking the display." : "Connecting to the display over Thread…");
    try {
      const node = await apiJson<MatterNode>(`/api/matter-frames/${route}`, {
        method: "POST",
        body: JSON.stringify(body)
      });
      setMessage(node.linked
        ? "Secure Matter delivery is enabled. Open Display to see what’s queued."
        : "Matter pairing finished. Wait for the display identity to appear, then select Link display.");
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

  const checkConnections = async () => {
    setBusy(true);
    setMessage("");
    setFailed(false);
    try {
      await load(waiting);
    } catch {
      setFailed(true);
      setMessage("Could not reload display connections.");
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
          placeholder="Paste the Matter sharing code"
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
        <div>
          <h1>Connect a display</h1>
          <p>Pair inkDaddy with Home Assistant, then enable secure delivery over Thread.</p>
        </div>
      </header>

      <section className="connect-flow">
        <div className="qr-card">
          <div>
            <h2>1. Pair in Home Assistant</h2>
            <p>Add a Matter device in the Home Assistant mobile app. Scan the QR code on the ePaper display.</p>
          </div>
        </div>
        {matter?.enabled && (
          <div className="info-panel vertical">
            <h2>2. Share with inkDaddy</h2>
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
            ) : codeForm}
            {!matter.available && <p>The Matter controller is starting or temporarily unavailable.</p>}
          </div>
        )}
      </section>

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
          {waiting && <p>Waiting for a shared device to report its inkDaddy identity.</p>}
        </section>
      )}

      <section className="info-panel vertical">
        <h2>Your displays</h2>
        {registeredDisplays.map((device) => (
          <div className="row-item" key={device.device_uid}>
            <strong>{device.name}</strong>
            <span>{matterDisplays.has(device.device_uid) ? "Matter" : "Thread UDP"}</span>
          </div>
        ))}
        {registeredDisplays.length === 0 && <p>No display has connected yet.</p>}
        <div className="button-row">
          <button className="secondary-button" disabled={busy} onClick={() => void checkConnections()}>
            Check connections
          </button>
        </div>
        {message && <p role={failed ? "alert" : "status"}>{message}</p>}
      </section>
    </div>
  );
}

function connectionMessage(error: unknown): string {
  if (error instanceof Error) {
    try {
      const detail: unknown = JSON.parse(error.message).detail;
      if (typeof detail === "string") return detail;
    } catch {
      // Fall back to the connection message below.
    }
  }
  return "Could not connect the display. Check the sharing window and try again.";
}
