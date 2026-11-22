type EpaperPreviewProps = {
  title: string;
  caption: string;
  previewUrl?: string | null;
  empty: string;
  children?: React.ReactNode;
};

export function EpaperPreview({ title, caption, previewUrl, empty, children }: EpaperPreviewProps) {
  return (
    <section className="preview-panel" aria-label={title}>
      <div className="preview-toolbar">
        <div>
          <h2>{title}</h2>
          <p>{caption}</p>
        </div>
      </div>
      <div className="epaper-frame">
        {previewUrl ? (
          <img className="epaper-image" src={previewUrl} alt={title} />
        ) : (
          <p className="epaper-empty">{empty}</p>
        )}
      </div>
      {children}
    </section>
  );
}
