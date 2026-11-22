import { useEffect, useState } from "react";
import { apiJson } from "../lib/api";
import type { AlbumRecord, DashboardRecord, PhotoRecord, Playback, PlaybackSource } from "../lib/types";

interface Choice extends PlaybackSource {
  title: string;
}

export function PlaylistEditor({ path, playlist, onSave }: {
  path: string;
  playlist: PlaybackSource[];
  onSave: (state: Playback) => void;
}) {
  const [entries, setEntries] = useState(playlist);
  const [choices, setChoices] = useState<Choice[]>([]);
  const [choiceKey, setChoiceKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };
    Promise.all([
      apiJson<{ dashboards: DashboardRecord[] }>("/api/dashboards", options),
      apiJson<{ photos: PhotoRecord[] }>("/api/photos", options),
      apiJson<{ albums: AlbumRecord[] }>("/api/albums", options)
    ]).then(([{ dashboards }, { photos }, { albums }]) => {
      if (controller.signal.aborted) return;
      setChoices([
        ...dashboards.map((item) => ({ type: "dashboard" as const, id: item.id, title: item.name })),
        ...albums.map((item) => ({ type: "album" as const, id: item.id, title: item.name })),
        ...photos.filter((item) => item.frame_url).map((item) => ({ type: "photo" as const, id: item.id, title: item.original_filename }))
      ]);
    }).catch((reason: Error) => {
      if (!controller.signal.aborted) setError(reason.message);
    });
    return () => controller.abort();
  }, []);

  const add = () => {
    const choice = choices.find((item) => sourceKey(item) === choiceKey);
    if (!choice) return;
    setEntries([...entries, { type: choice.type, id: choice.id }]);
    setChoiceKey("");
  };

  const move = (index: number, offset: number) => {
    const next = [...entries];
    const [entry] = next.splice(index, 1);
    next.splice(index + offset, 0, entry);
    setEntries(next);
  };

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      const state = await apiJson<Playback>(path, { method: "PUT", body: JSON.stringify({ playlist: entries }) });
      onSave(state);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to save playlist.");
    } finally {
      setBusy(false);
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
        <select value={choiceKey} disabled={busy} onChange={(event) => setChoiceKey(event.target.value)}>
          <option value="">Choose a dashboard, album, or photo</option>
          {choices.map((item) => <option value={sourceKey(item)} key={sourceKey(item)}>{item.title} ({item.type})</option>)}
        </select>
      </label>
      <div className="playlist-item-actions">
        <button className="secondary-button" disabled={!choiceKey || busy} onClick={add}>Add</button>
        <button className="primary-button" disabled={busy} onClick={() => void save()}>{busy ? "Saving…" : "Save playlist"}</button>
      </div>
      {error && <p className="warning-text" role="alert">{error}</p>}
    </div>
  );
}

function sourceKey({ type, id }: PlaybackSource): string {
  return `${type}:${id}`;
}
