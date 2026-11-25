import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiJson, apiPath } from "../lib/api";
import type { AlbumRecord, PhotoRecord, PlaybackSource } from "../lib/types";
import { PhotoFramingEditor, type FramingPhoto } from "./PhotoFramingEditor";
import { FramingOptions, type Framing } from "./SimplePages";
import "./photos.css";

type Album = AlbumRecord & { photos: PhotoRecord[] };
type AlbumDraft = { id: string | null; name: string; photoIds: string[] };
type UploadError = { filename: string | null; detail: string };

export function PhotosPage({ onAddToPlaylist }: { onAddToPlaylist: (source: PlaybackSource) => void }) {
  const [photos, setPhotos] = useState<FramingPhoto[]>([]);
  const [albums, setAlbums] = useState<AlbumRecord[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [draft, setDraft] = useState<AlbumDraft | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);
  const [framingPhoto, setFramingPhoto] = useState<FramingPhoto | null>(null);
  const framingTrigger = useRef<HTMLButtonElement>(null);
  const [mode, setMode] = useState<Framing | null>(null);
  const [libraryBusy, setLibraryBusy] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [uploadErrors, setUploadErrors] = useState<UploadError[]>([]);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLibraryBusy(true);
    const queries = { signal };
    const results = await Promise.allSettled([
      apiJson<{ photos: FramingPhoto[] }>("/api/photos", queries),
      apiJson<{ albums: AlbumRecord[] }>("/api/albums", queries),
      apiJson<{ photo_upload_mode: Framing }>("/api/settings", queries)
    ]);
    if (signal?.aborted) return;
    const [photoList, albumList, settings] = results;
    if (photoList.status === "fulfilled") setPhotos(photoList.value.photos);
    if (albumList.status === "fulfilled") setAlbums(albumList.value.albums);
    if (settings.status === "fulfilled") setMode((current) => current ?? settings.value.photo_upload_mode);
    setLibraryBusy(false);
    setLoaded(photoList.status === "fulfilled");
    setLoadError(photoList.status === "rejected" || albumList.status === "rejected" ? "Unable to load the photo library." : "");
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function upload(files: File[]) {
    if (!files.length) return;
    setMessage("");
    setError("");
    setUploadErrors([]);
    setUploading(true);
    try {
      const body = new FormData();
      files.forEach((file) => body.append("files", file));
      if (mode) body.set("mode", mode);
      const result = await apiJson<{ photos: FramingPhoto[]; errors: UploadError[] }>("/api/photos/batch", { method: "POST", body });
      setPhotos((current) => [...result.photos].reverse().concat(current));
      setUploadErrors(result.errors);
      setMessage(result.photos.length ? `Added ${result.photos.length} ${result.photos.length === 1 ? "photo" : "photos"}.` : "");
    } catch (reason) {
      setError(uploadFailure(reason));
    } finally {
      setUploading(false);
    }
  }

  async function editAlbum(album: AlbumRecord) {
    setBusy(true);
    setError("");
    try {
      const saved = await apiJson<Album>(`/api/albums/${album.id}`);
      setDraft({ id: saved.id, name: saved.name, photoIds: saved.photos.map((photo) => photo.id) });
    } catch {
      setError("Unable to open this album.");
    } finally {
      setBusy(false);
    }
  }

  async function saveAlbum(event: FormEvent) {
    event.preventDefault();
    if (!draft?.name.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const album = await apiJson<Album>(draft.id ? `/api/albums/${draft.id}` : "/api/albums", {
        method: draft.id ? "PUT" : "POST",
        body: JSON.stringify({ name: draft.name, photo_ids: draft.photoIds })
      });
      setAlbums((current) => draft.id ? current.map((row) => row.id === album.id ? album : row) : [album, ...current]);
      setSelected(new Set());
      setDraft(null);
      setMessage("Album saved.");
    } catch {
      setError("Unable to save the album. Check whether its photos are still available.");
    } finally {
      setBusy(false);
    }
  }

  async function rename(event: FormEvent) {
    event.preventDefault();
    if (!renaming?.name.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const photo = await apiJson<FramingPhoto>(`/api/photos/${renaming.id}`, {
        method: "PUT",
        body: JSON.stringify({ name: renaming.name })
      });
      setPhotos((current) => current.map((row) => row.id === photo.id ? photo : row));
      setRenaming(null);
      setMessage("Photo renamed.");
    } catch {
      setError("Unable to rename the photo.");
    } finally {
      setBusy(false);
    }
  }

  async function remove(type: "photos" | "albums", id: string) {
    const isPhoto = type === "photos";
    if (!window.confirm(isPhoto
      ? "Delete this photo? Albums and playlists will skip it."
      : "Delete this album? Its photos stay in the library and playlists will skip the album.")) return;
    setBusy(true);
    setError("");
    try {
      await apiJson(`/api/${type}/${id}`, { method: "DELETE" });
      if (isPhoto) {
        setPhotos((current) => current.filter((item) => item.id !== id));
        setSelected((current) => new Set([...current].filter((value) => value !== id)));
        setDraft((current) => current ? { ...current, photoIds: current.photoIds.filter((value) => value !== id) } : current);
        setRenaming((current) => current?.id === id ? null : current);
        await load();
      } else {
        setAlbums((current) => current.filter((item) => item.id !== id));
        setDraft((current) => current?.id === id ? null : current);
      }
      setMessage(isPhoto ? "Photo deleted." : "Album deleted.");
    } catch {
      setError(isPhoto ? "Unable to delete the photo." : "Unable to delete the album.");
    } finally {
      setBusy(false);
    }
  }

  function move(position: number, direction: number) {
    if (!draft) return;
    const photoIds = [...draft.photoIds];
    const [id] = photoIds.splice(position, 1);
    photoIds.splice(position + direction, 0, id);
    setDraft({ ...draft, photoIds });
  }

  function toggleSelected(id: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function closeFraming() {
    setFramingPhoto(null);
    window.requestAnimationFrame(() => framingTrigger.current?.focus());
  }

  const controlsBusy = busy || uploading || libraryBusy || framingPhoto !== null;
  const selectedPhoto = selected.size === 1 ? photos.find((photo) => selected.has(photo.id) && photo.frame_url) : null;
  const attachable = draft ? [...selected].filter((id) => !draft.photoIds.includes(id)) : [];
  const photoNames = new Map(photos.map((photo) => [photo.id, photo.original_filename]));

  return <div className="page-grid">
    <header className="page-header"><h1>Photos</h1></header>
    <section className="workflow-panel form-stack">
      <div className="form-row">
        <label>Framing
          <select value={mode ?? ""} disabled={controlsBusy} onChange={(event) => setMode(event.target.value as Framing)}>
            {!mode && <option value="">Saved default</option>}
            <FramingOptions />
          </select>
        </label>
        <label>Upload photos
          <input type="file" accept="image/jpeg,image/png,image/webp,image/bmp" multiple disabled={controlsBusy}
            onChange={(event) => {
              const files = Array.from(event.currentTarget.files ?? []);
              event.currentTarget.value = "";
              void upload(files);
            }} />
        </label>
      </div>
      {(message || uploading) && <p role="status">{uploading ? "Processing photos…" : message}</p>}
      {uploadErrors.length > 0 && <div role="alert">
        <strong>{uploadErrors.length} {uploadErrors.length === 1 ? "photo could" : "photos could"} not be added</strong>
        <ul>{uploadErrors.map(({ filename, detail }, index) =>
          <li key={index}>{filename || "Unnamed file"} · {detail}</li>)}</ul>
      </div>}
      {error && <p className="warning-text" role="alert">{error}</p>}
      {loadError && <div className="button-row"><p role="alert">{loadError}</p><button className="secondary-button" disabled={controlsBusy} onClick={() => void load()}>Retry loading</button></div>}
    </section>

    {framingPhoto && <PhotoFramingEditor key={framingPhoto.id} photo={framingPhoto}
      onCancel={closeFraming} onSaved={(photo) => {
        setPhotos((current) => current.map((row) => row.id === photo.id ? photo : row));
        closeFraming();
        setMessage("Framing saved.");
      }} />}

    <section className="content-layout photo-library">
      <aside className="side-stack photo-actions">
        {selected.size > 0 && <section className="info-panel vertical">
          <h2>{selected.size} {selected.size === 1 ? "photo" : "photos"} selected</h2>
          <div className="button-row">
            <button className="secondary-button" disabled={controlsBusy || Boolean(draft)}
              onClick={() => setDraft({ id: null, name: "", photoIds: [...selected] })}>Create album</button>
            {selectedPhoto && <button className="secondary-button" disabled={controlsBusy || Boolean(draft)}
              onClick={() => onAddToPlaylist({ type: "photo", id: selectedPhoto.id })}>Add to playlist</button>}
          </div>
        </section>}
        {draft && <form className="info-panel vertical" onSubmit={(event) => void saveAlbum(event)}>
          <h2>{draft.id ? "Edit album" : "New album"}</h2>
          <label>Album name
            <input required maxLength={255} value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
          </label>
          {draft.photoIds.length > 0 && <ol className="playlist-list">
            {draft.photoIds.map((id, position) => {
              const name = photoNames.get(id) ?? "Photo unavailable";
              return <li key={id}><strong>{name}</strong><div className="playlist-item-actions">
                <button type="button" className="icon-button" aria-label={`Move ${name} up`} disabled={controlsBusy || position === 0} onClick={() => move(position, -1)}>↑</button>
                <button type="button" className="icon-button" aria-label={`Move ${name} down`} disabled={controlsBusy || position === draft.photoIds.length - 1} onClick={() => move(position, 1)}>↓</button>
                <button type="button" className="icon-button" aria-label={`Remove ${name} from album`} disabled={controlsBusy}
                  onClick={() => setDraft({ ...draft, photoIds: draft.photoIds.filter((value) => value !== id) })}>×</button>
              </div></li>;
            })}
          </ol>}
          <div className="button-row">
            <button type="button" className="secondary-button" disabled={controlsBusy || !attachable.length}
              onClick={() => setDraft({ ...draft, photoIds: draft.photoIds.concat(attachable) })}>Add selected photos</button>
            <button className="primary-button" disabled={controlsBusy || !draft.name.trim()}>Save album</button>
            <button type="button" className="secondary-button" disabled={controlsBusy} onClick={() => setDraft(null)}>Cancel</button>
          </div>
        </form>}
        <section className="info-panel vertical">
          <h2>Albums</h2>
          {albums.map((album) => <div className="form-stack" key={album.id}>
            <div className="row-item"><strong>{album.name}</strong><span>{album.item_count} {album.item_count === 1 ? "photo" : "photos"}</span></div>
            <div className="button-row">
              <button className="secondary-button" disabled={controlsBusy || Boolean(draft)} onClick={() => void editAlbum(album)}>Edit</button>
              <button className="secondary-button" disabled={controlsBusy || Boolean(draft) || album.item_count === 0}
                onClick={() => onAddToPlaylist({ type: "album", id: album.id })}>Add to playlist</button>
              <button className="secondary-button" disabled={controlsBusy || Boolean(draft)} onClick={() => void remove("albums", album.id)}>Delete</button>
            </div>
          </div>)}
          {loaded && !albums.length && <p>No albums yet.</p>}
        </section>
      </aside>
      <div className="card-grid photo-cards">
        {photos.map((photo) => <article className="asset-card" key={photo.id}>
          <button className={selected.has(photo.id) ? "asset-select selected" : "asset-select"}
            aria-label={`Select ${photo.original_filename}`} aria-pressed={selected.has(photo.id)}
            disabled={controlsBusy} onClick={() => toggleSelected(photo.id)}>
            {photo.preview_url ? <img src={apiPath(photo.preview_url)} loading="lazy" alt="" /> : <div className="preview-placeholder" />}
          </button>
          {renaming?.id === photo.id ? <form className="form-stack" onSubmit={(event) => void rename(event)}>
            <label>Photo name <input value={renaming.name} maxLength={512} onChange={(event) => setRenaming({ ...renaming, name: event.target.value })} /></label>
            <div className="button-row">
              <button className="primary-button" disabled={controlsBusy || !renaming.name.trim()}>Save name</button>
              <button className="secondary-button" type="button" disabled={controlsBusy} onClick={() => setRenaming(null)}>Cancel</button>
            </div>
          </form> : <>
            <div className="asset-meta"><strong>{photo.original_filename}</strong>{!photo.frame_url && <span>Frame unavailable</span>}</div>
            <div className="button-row">
              {photo.can_edit_framing && <button className="secondary-button" disabled={controlsBusy}
                onClick={(event) => {
                  setMessage("");
                  framingTrigger.current = event.currentTarget;
                  setFramingPhoto(photo);
                }}>Edit framing</button>}
              <button className="secondary-button" disabled={controlsBusy} onClick={() => setRenaming({ id: photo.id, name: photo.original_filename })}>Rename</button>
              <button className="secondary-button" disabled={controlsBusy} onClick={() => void remove("photos", photo.id)}>Delete</button>
            </div>
          </>}
        </article>)}
        {loaded && !photos.length && <section className="empty-state compact"><h2>No photos yet</h2></section>}
        {!loaded && !loadError && <p role="status">Loading photo library…</p>}
      </div>
    </section>
  </div>;
}

function uploadFailure(error: unknown): string {
  if (error instanceof Error) {
    try {
      const detail = JSON.parse(error.message).detail;
      if (detail === "Batch uploads are limited to 128 MB of images." || detail === "Batch uploads are limited to 50 images.") return detail;
    } catch {
      // The response may come from the Home Assistant proxy.
    }
  }
  return "Unable to upload these photos. Try a smaller batch.";
}
