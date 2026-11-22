import { useCallback, useEffect, useState } from "react";
import { apiJson } from "../lib/api";
import type { HAConfig, HAEntity } from "../lib/types";

export function HomeAssistantPage() {
  const [config, setConfig] = useState<HAConfig | null>(null);
  const [entities, setEntities] = useState<HAEntity[]>([]);
  const [domains, setDomains] = useState<string[]>([]);
  const [resultsRevision, setResultsRevision] = useState(0);
  const [loadingEntities, setLoadingEntities] = useState(true);
  const [resultsError, setResultsError] = useState("");
  const [configError, setConfigError] = useState("");
  const [baseUrl, setBaseUrl] = useState("http://homeassistant.local:8123");
  const [token, setToken] = useState("");
  const [search, setSearch] = useState("");
  const [domain, setDomain] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const managed = config?.mode === "supervisor";

  const loadConfig = useCallback(async (signal?: AbortSignal) => {
    setConfigError("");
    try {
      const saved = await apiJson<HAConfig>("/api/home-assistant/config", { signal });
      if (signal?.aborted) return;
      setConfig(saved);
      if (saved.mode !== "supervisor" && saved.base_url) setBaseUrl(saved.base_url);
    } catch {
      if (!signal?.aborted) setConfigError("Unable to load the Home Assistant connection.");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadConfig(controller.signal);
    return () => controller.abort();
  }, [loadConfig]);

  useEffect(() => {
    const controller = new AbortController();
    apiJson<{ domains: Array<{ domain: string }> }>("/api/home-assistant/devices", { signal: controller.signal })
      .then(({ domains }) => setDomains(domains.map((row) => row.domain).sort()))
      .catch(() => {});
    return () => controller.abort();
  }, [resultsRevision]);

  useEffect(() => {
    const controller = new AbortController();
    setLoadingEntities(true);
    setResultsError("");
    const timer = window.setTimeout(async () => {
      const query = new URLSearchParams({ limit: "100" });
      if (search.trim()) query.set("search", search.trim());
      if (domain) query.set("domain", domain);
      try {
        const result = await apiJson<{ entities: HAEntity[] }>(`/api/home-assistant/entities?${query}`, { signal: controller.signal });
        if (!controller.signal.aborted) setEntities(result.entities);
      } catch {
        if (!controller.signal.aborted) setResultsError("Unable to load cached entities.");
      } finally {
        if (!controller.signal.aborted) setLoadingEntities(false);
      }
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [search, domain, resultsRevision]);

  const save = async () => {
    setBusy(true);
    try {
      const payload = await apiJson<HAConfig>("/api/home-assistant/config", {
        method: "PUT",
        body: JSON.stringify({ base_url: baseUrl, token, enabled: true, validate: Boolean(token) })
      });
      setConfig(payload);
      setToken("");
      setMessage("Home Assistant configuration saved.");
      setResultsRevision((revision) => revision + 1);
    } catch {
      setMessage("Unable to save the connection. Check the URL and access token.");
    } finally {
      setBusy(false);
    }
  };

  const refresh = async () => {
    setBusy(true);
    try {
      const payload = await apiJson<{ error?: string | null }>("/api/home-assistant/entities/refresh", { method: "POST" });
      if (payload.error) {
        setMessage("Home Assistant could not refresh the entity cache.");
      } else {
        setMessage("Home Assistant entities refreshed.");
        setResultsRevision((revision) => revision + 1);
        await loadConfig();
      }
    } catch {
      setMessage("Unable to refresh Home Assistant entities.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page-grid">
      <header className="page-header">
        <div>
          <h1>Home Assistant</h1>
          <p>Manage the Home Assistant entities available to your dashboards.</p>
        </div>
        <span className={`status-badge ${config?.configured ? "status-good" : "status-warn"}`}>
          {config?.configured ? (managed ? "Managed by Supervisor" : "Configured") : config ? "Connection unavailable" : "Checking connection"}
        </span>
      </header>

      {configError && <div className="button-row"><p role="alert">{configError}</p>
        <button className="secondary-button" onClick={() => void loadConfig()}>Retry connection check</button>
      </div>}
      <section className="settings-grid">
        {managed ? (
          <section className="workflow-panel form-stack">
            <div className="preview-toolbar"><h2>Home Assistant app connection</h2></div>
            <dl className="mini-list">
              <div><dt>Location</dt><dd>{config.location_name ?? "Unavailable"}</dd></div>
              <div><dt>Time zone</dt><dd>{config.time_zone ?? "Unavailable"}</dd></div>
            </dl>
            <button className="secondary-button" disabled={busy || !config.configured} onClick={() => void refresh()}>
              {busy ? "Refreshing…" : "Refresh entities"}
            </button>
            {message && <p className="muted-text" role="status">{message}</p>}
          </section>
        ) : config ? <div className="workflow-panel">
          <div className="form-stack">
            <label>
              Home Assistant URL
              <input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
            </label>
            <label>
              Long-lived access token
              <input type="password" value={token} autoComplete="off"
                onChange={(event) => setToken(event.target.value)}
                placeholder={config.configured ? "Stored token" : "Paste token"} />
            </label>
            <div className="button-row">
              {config.token_page_url && <a className="secondary-link" href={config.token_page_url} target="_blank" rel="noreferrer">
                Open token page
              </a>}
              <button className="primary-button" disabled={busy} onClick={() => void save()}>
                {busy ? "Working..." : "Save and validate"}
              </button>
              <button className="secondary-button" disabled={busy || !config?.configured} onClick={() => void refresh()}>
                Refresh entities
              </button>
            </div>
            {message && <p className="muted-text" role="status">{message}</p>}
          </div>
        </div> : !configError && <p className="muted-text" role="status">Loading connection…</p>}

        <div className="info-panel vertical">
          <h2>Cache</h2>
          <div className="row-item">
            <span>Entities</span>
            <strong>{config?.entity_count ?? entities.length}</strong>
          </div>
          {!managed && <div className="row-item">
            <span>Last validated</span>
            <strong>{config?.last_validated_at ? new Date(config.last_validated_at).toLocaleString() : "Never"}</strong>
          </div>}
        </div>
      </section>

      <section className="workflow-panel">
        <div className="form-row">
          <label>
            Search
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="temperature, door, light..." />
          </label>
          <label>
            Domain
            <select value={domain} onChange={(event) => setDomain(event.target.value)}>
              <option value="">All</option>
              {domains.map((item) => (
                <option key={item} value={item}>{item}</option>
              ))}
            </select>
          </label>
        </div>
      </section>

      <section className="table-panel">
        {resultsError && <div className="button-row"><p role="alert">{resultsError}</p>
          <button className="secondary-button" onClick={() => setResultsRevision((revision) => revision + 1)}>Retry search</button>
        </div>}
        {loadingEntities && <p role="status">Searching cached entities…</p>}
        {!loadingEntities && !resultsError && entities.length === 100 && <p className="muted-text">Showing 100 results. Narrow your search to see fewer.</p>}
        {!loadingEntities && !resultsError && entities.map((entity) => (
          <div className="table-row" key={entity.entity_id}>
            <div>
              <strong>{entity.name ?? entity.entity_id}</strong>
              <span>{entity.entity_id}</span>
            </div>
            <span className="status-badge status-neutral">{entity.domain}</span>
            <strong>{entity.state ?? "unknown"}</strong>
          </div>
        ))}
        {!loadingEntities && !resultsError && !entities.length && (
          <section className="empty-state compact">
            <h2>{search || domain ? "No matching entities" : "No cached entities"}</h2>
            <p>{search || domain ? "Try a different search." : "Refresh entities to make them available in dashboards."}</p>
          </section>
        )}
      </section>
    </div>
  );
}
