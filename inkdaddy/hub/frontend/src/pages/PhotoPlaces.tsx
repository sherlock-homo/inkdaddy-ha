import { useEffect, useState } from "react";
import { apiJson } from "../lib/api";

type PlaceStatus = { enabled: boolean; pending: number; resolved: number; unavailable: number };

export function PhotoPlaces() {
  const [open, setOpen] = useState(false);
  return <details className="photo-places" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Photo locations</summary>
    {open && <PlaceControls />}
  </details>;
}

function PlaceControls() {
  const [status, setStatus] = useState<PlaceStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  const pending = (status?.pending ?? 0) > 0;

  useEffect(() => {
    const controller = new AbortController();
    const refresh = () => apiJson<PlaceStatus>("/api/photo-places", { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) { setStatus(data); setError(""); }
      })
      .catch(() => { if (!controller.signal.aborted) setError("Couldn’t load photo location settings."); });
    if (!busy) void refresh();
    const timer = pending && !busy ? window.setInterval(refresh, 4000) : undefined;
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [attempt, pending, busy]);

  async function change(lookup: boolean) {
    if (!status || busy) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const next = lookup
        ? await apiJson<PlaceStatus>("/api/photo-places/lookup", { method: "POST" })
        : await apiJson<PlaceStatus>("/api/photo-places", { method: "PUT", body: JSON.stringify({ enabled: !status.enabled }) });
      setStatus(next);
      setMessage(lookup ? "Requested place names for this library." : next.enabled ? "Automatic lookup is on." : "Automatic lookup is off.");
    } catch {
      setError(lookup ? "Couldn’t start the lookup. Try again." : "Couldn’t save the setting. Try again.");
    } finally { setBusy(false); }
  }

  return <div className="form-stack">
    <p>Lookup sends the GPS coordinates of photos without a place name to Photon. Results are saved in Home Assistant.</p>
    <div className="button-row">
      <button className="secondary-button" type="button" disabled={!status || busy || pending} onClick={() => void change(true)}>Find place names</button>
    </div>
    {status && <label className="photo-places-auto">
      <input type="checkbox" checked={status.enabled} disabled={busy} onChange={() => void change(false)} />
      Look up new uploads automatically
    </label>}
    <small>Map data © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a>.</small>
    {status && (pending || status.resolved > 0 || status.unavailable > 0) && <p className="muted-text" role="status">
      {[pending && "Looking up places…", status.resolved > 0 && `Place names found for ${status.resolved} ${status.resolved === 1 ? "photo" : "photos"}.`,
        status.unavailable > 0 && `No match for ${status.unavailable}.`].filter(Boolean).join(" ")}
    </p>}
    {message && <p role="status">{message}</p>}
    {error && <div className="button-row"><span role="alert">{error}</span>
      {!status && <button className="secondary-button" type="button" onClick={() => setAttempt((n) => n + 1)}>Retry</button>}
    </div>}
  </div>;
}
