import { useEffect, useRef, useState, type FormEvent } from "react";
import { apiJson, apiPath } from "../lib/api";
import type { PhotoRecord } from "../lib/types";
import { FramingOptions, type Framing } from "./SimplePages";

type Crop = { mode: Framing; x: number | null; y: number | null; zoom: number };
export type FramingPhoto = PhotoRecord & {
  framing?: Crop | null;
  revision?: string | null;
  can_edit_framing?: boolean;
};
type Preview = { path: string; url: string; x: number; y: number; differsFromSaved: boolean };

function defaultCrop(mode: Framing): Crop {
  return { mode, x: null, y: null, zoom: 1 };
}

export function PhotoFramingEditor({ photo, onSaved, onCancel }: {
  photo: FramingPhoto;
  onSaved: (photo: FramingPhoto) => void;
  onCancel: () => void;
}) {
  const [crop, setCrop] = useState<Crop>(() => photo.framing ?? defaultCrop("fit"));
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const basePath = `/api/photos/${encodeURIComponent(photo.id)}`;
  const params = new URLSearchParams({ mode: crop.mode, zoom: String(crop.zoom) });
  if (crop.x !== null && crop.y !== null) {
    params.set("x", String(crop.x));
    params.set("y", String(crop.y));
  }
  const framingChanged = !photo.framing
    || crop.mode !== photo.framing.mode || crop.zoom !== photo.framing.zoom
    || crop.x !== photo.framing.x || crop.y !== photo.framing.y;
  if (!framingChanged) params.set("compare_saved", "true");
  const previewPath = `${basePath}/framing-preview?${params}`;
  const previewReady = preview?.path === previewPath;
  const x = crop.x ?? (previewReady ? preview.x : 0.5);
  const y = crop.y ?? (previewReady ? preview.y : 0.5);
  const canSave = previewReady && (framingChanged || preview.differsFromSaved);

  useEffect(() => { heading.current?.focus(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    setPreviewError("");
    const timer = window.setTimeout(async () => {
      try {
        const response = await fetch(apiPath(previewPath), { signal: controller.signal });
        if (!response.ok || !response.headers.get("Content-Type")?.startsWith("image/png")) {
          throw new Error(await response.text());
        }
        const blob = await response.blob();
        if (controller.signal.aborted) return;
        setPreview({
          path: previewPath,
          url: URL.createObjectURL(blob),
          x: readFocus(response, "X-InkDaddy-Focus-X"),
          y: readFocus(response, "X-InkDaddy-Focus-Y"),
          differsFromSaved: response.headers.get("X-InkDaddy-Preview-Changed") === "true",
        });
      } catch (error) {
        if (!controller.signal.aborted) setPreviewError(framingError(error, "Unable to preview this framing."));
      }
    }, 250);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [previewPath]);
  const previewUrl = preview?.url;
  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl); }, [previewUrl]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (saving || !canSave || !photo.revision) return;
    setSaving(true);
    setSaveError("");
    try {
      const updated = await apiJson<FramingPhoto>(`${basePath}/framing`, {
        method: "PUT", body: JSON.stringify({ ...crop, revision: photo.revision }),
      });
      onSaved(updated);
    } catch (error) {
      setSaveError(framingError(error, "Unable to save this framing."));
    } finally {
      setSaving(false);
    }
  }

  return <form className="workflow-panel form-stack photo-framing-editor" onSubmit={(event) => void save(event)}>
    <div>
      <h2 ref={heading} tabIndex={-1}>Edit framing</h2>
      <p>{photo.original_filename}</p>
    </div>
    <div className="photo-framing-previews">
      {photo.preview_url && <figure>
        <img src={apiPath(photo.preview_url)} alt="Current framing" />
        <figcaption>Current</figcaption>
      </figure>}
      <figure>
        {preview ? <img src={preview.url} alt="Draft framing" /> : <div className="preview-placeholder" />}
        <figcaption role="status">{previewReady ? "Draft" : previewError ? "Draft unavailable" : "Preparing draft…"}</figcaption>
      </figure>
    </div>
    <label>Framing
      <select aria-label="Framing" value={crop.mode} onChange={(event) => setCrop(defaultCrop(event.target.value as Framing))} disabled={saving}>
        <FramingOptions />
      </select>
    </label>
    {crop.mode !== "fit" && <fieldset className="photo-framing-controls" disabled={saving}>
      <legend>Crop position</legend>
      <label>Horizontal position
        <input type="range" min="0" max="1" step="0.01" value={x}
          aria-valuetext={`${Math.round(x * 100)} percent across`}
          onChange={(event) => setCrop({ ...crop, x: Number(event.target.value), y })} />
      </label>
      <label>Vertical position
        <input type="range" min="0" max="1" step="0.01" value={y}
          aria-valuetext={`${Math.round(y * 100)} percent down`}
          onChange={(event) => setCrop({ ...crop, x, y: Number(event.target.value) })} />
      </label>
      <label>Zoom <output>{crop.zoom.toFixed(2)}×</output>
        <input type="range" aria-label="Zoom" min="1" max="3" step="0.05" value={crop.zoom}
          onChange={(event) => setCrop({ ...crop, zoom: Number(event.target.value) })} />
      </label>
      <button type="button" className="secondary-button"
        disabled={crop.x === null && crop.zoom === 1} onClick={() => setCrop(defaultCrop(crop.mode))}>
        {crop.mode === "intelligent" ? "Use automatic focus" : "Reset crop"}
      </button>
    </fieldset>}
    {(previewError || saveError) && <p role="alert" className="warning-text">{saveError || previewError}</p>}
    <div className="button-row">
      <button className="primary-button" disabled={!canSave || saving}>
        {saving ? "Saving…" : "Save framing"}
      </button>
      <button type="button" className="secondary-button" disabled={saving} onClick={onCancel}>Cancel</button>
    </div>
  </form>;
}

function readFocus(response: Response, name: string): number {
  const value = response.headers.get(name);
  const number = value === null ? 0.5 : Number(value);
  return Number.isFinite(number) && number >= 0 && number <= 1 ? number : 0.5;
}

function framingError(error: unknown, fallback: string): string {
  if (error instanceof Error) {
    try {
      const detail = JSON.parse(error.message).detail;
      if (typeof detail === "string") return detail;
    } catch {
      // The Home Assistant proxy can return a non-JSON error.
    }
  }
  return fallback;
}
