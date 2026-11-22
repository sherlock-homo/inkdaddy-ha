import { useEffect, useState } from "react";
import { apiJson } from "../lib/api";
import type { AlbumRecord, DashboardRecord, PhotoRecord, Playback, PlaybackSource } from "../lib/types";

interface Choice extends PlaybackSource {
  title: string;
}

export function PlaylistEditor({ path, playlist, defaultSource, initialSource, onSave, onDirtyChange, onBusyChange }: {
  path: string;
  playlist: PlaybackSource[];
  defaultSource: PlaybackSource;
  initialSource: PlaybackSource | null;
  onSave: (state: Playback) => void;
  onDirtyChange: (dirty: boolean) => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [original] = useState(playlist);
  const [entries, setEntries] = useState(() => initialSource && playlist.length < 100
    ? [...(playlist.length ? playlist : [defaultSource]), initialSource] : playlist);
  const [choices, setChoices] = useState<Choice[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [loading, setLoading] = useState(true);
  const dirty = entries.length !== original.length || entries.some((entry, index) => sourceKey(entry) !== sourceKey(original[index]));
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const options = { signal: controller.signal };
    Promise.all([
      apiJson<{ dashboards: DashboardRecord[] }>("/api/dashboards", options),
      apiJson<{ photos: PhotoRecord[] }>("/api/photos", options),
      apiJson<{ albums: AlbumRecord[] }>("/api/albums", options)
    ]).then(([{ dashboards }, { photos }, { albums }]) => {
      if (controller.signal.aborted) return;
      setChoices([
        ...(!dashboards.some((item) => item.id === "sample") ? [{ type: "dashboard" as const, id: "sample", title: "Sample dashboard" }] : []),
        ...dashboards.map((item) => ({ type: "dashboard" as const, id: item.id, title: item.name })),
        ...albums.filter((item) => item.item_count > 0).map((item) => ({ type: "album" as const, id: item.id, title: item.name })),
        ...photos.filter((item) => item.frame_url).map((item) => ({ type: "photo" as const, id: item.id, title: item.original_filename }))
      ]);
    }).catch(() => {
      if (!controller.signal.aborted) setError("Unable to load playlist choices.");
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [loadAttempt]);

  const add = (key: string) => {
    const choice = choices.find((item) => sourceKey(item) === key);
    if (!choice) return;
    setEntries([...entries, { type: choice.type, id: choice.id }]);
  };

  const move = (index: number, offset: number) => {
    const next = [...entries];
    const [entry] = next.splice(index, 1);
    next.splice(index + offset, 0, entry);
    setEntries(next);
  };

  const save = async () => {
    setBusy(true);
    onBusyChange(true);
    setError("");
    try {
      const state = await apiJson<Playback>(path, { method: "PUT", body: JSON.stringify({ playlist: entries }) });
      onSave(state);
    } catch {
      setError("Unable to save playlist. Check whether its items are still available.");
    } finally {
      setBusy(false);
      onBusyChange(false);
    }
  };

  return (
    <div className="form-stack playlist-editor">
      <p className="muted-text">The display cycles through these items each hour. An empty playlist uses its selected source.</p>
      {entries.length > 0 && (
        <ol className="playlist-list">
          {entries.map((entry, index) => {
            const title = choices.find((item) => sourceKey(item) === sourceKey(entry))?.title ?? `${entry.type} ${entry.id}`;
            return (
              <li key={`${sourceKey(entry)}:${index}`}>
                <strong>{title}</strong>
                <div className="playlist-item-actions">
                  <button className="icon-button" aria-label={`Move ${title} up`} disabled={index === 0 || busy} onClick={() => move(index, -1)}>↑</button>
                  <button className="icon-button" aria-label={`Move ${title} down`} disabled={index === entries.length - 1 || busy} onClick={() => move(index, 1)}>↓</button>
                  <button className="icon-button" aria-label={`Remove ${title} from playlist`} disabled={busy} onClick={() => setEntries(entries.filter((_, position) => position !== index))}>×</button>
                </div>
              </li>
            );
          })}
        </ol>
      )}
      <label>
        Add to playlist
        <select value="" disabled={busy || loading || !choices.length || entries.length >= 100} onChange={(event) => add(event.target.value)}>
          <option value="">{loading ? "Loading choices…" : "Choose a dashboard, album, or photo"}</option>
          {choices.map((item) => <option value={sourceKey(item)} key={sourceKey(item)}>{item.title} ({item.type})</option>)}
        </select>
      </label>
      <div className="playlist-item-actions">
        {!loading && !choices.length && <button className="secondary-button" disabled={busy} onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Reload choices</button>}
        <button className="primary-button" disabled={busy || !dirty} onClick={() => void save()}>{busy ? "Saving…" : "Save playlist"}</button>
      </div>
      {entries.length >= 100 && <p>Playlists support up to 100 items.</p>}
      {error && <p className="warning-text" role="alert">{error}</p>}
    </div>
  );
}

function sourceKey({ type, id }: PlaybackSource): string {
  return `${type}:${id}`;
}
