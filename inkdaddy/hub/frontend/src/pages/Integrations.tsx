import { useCallback, useEffect, useState } from "react";
import { apiJson } from "../lib/api";
import type { IntegrationRecord } from "../lib/types";

export function IntegrationsPage() {
  const [integrations, setIntegrations] = useState<IntegrationRecord[]>([]);
  const [message, setMessage] = useState("");

  const load = useCallback(async (signal?: AbortSignal) => {
    setMessage("Loading integrations…");
    try {
      const payload = await apiJson<{ integrations: IntegrationRecord[] }>("/api/integrations", { signal });
      if (signal?.aborted) return;
      setIntegrations(payload.integrations);
      setMessage("");
    } catch {
      if (!signal?.aborted) setMessage("Unable to load integrations.");
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  return <div className="page-grid">
    <header className="page-header"><h1>Integrations</h1></header>
    {message && <div className="button-row">
      <p role="status">{message}</p>
      {message.startsWith("Unable") && <button className="secondary-button" onClick={() => void load()}>Retry</button>}
    </div>}
    <section className="card-grid three">
      {integrations.map((integration) => {
        const thread = integration.id === "thread_border_router";
        const managed = integration.status === "managed_by_home_assistant";
        return <article className="asset-card integration-card" key={integration.id}>
          <div className="preview-toolbar">
            <h2>{integration.name}</h2>
            <span className={`status-badge ${integration.configured ? "status-good" : "status-neutral"}`}>
              {managed ? "Managed by Home Assistant" : thread ? "Externally managed" : integration.configured ? "Configured" : "Setup needed"}
            </span>
          </div>
          {integration.entity_count !== undefined && <p>{integration.entity_count} cached entities</p>}
          {integration.device_count !== undefined && <p>{integration.device_count} registered displays</p>}
          <div className="button-row">
            <a className="secondary-link" href={integration.manage_url} target={managed ? "_parent" : undefined}>
              {managed ? "Open Thread settings" : thread ? "Open Diagnostics" : integration.id === "home_assistant" ? "Open Home Assistant settings" : "Open Devices"}
            </a>
          </div>
        </article>;
      })}
    </section>
  </div>;
}
