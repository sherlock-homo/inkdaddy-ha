import { useCallback, useEffect, useState, type FormEvent } from "react";
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
    <IntegrationRequestPanel />
  </div>;
}

type RelayStatus = "ready" | "unconfigured" | "invalid";
type DeliveryStatus = "queued" | "sending" | "retrying" | "delivered" | "failed";

type IntegrationRequest = {
  integration_id: string;
  integration_name: string;
  status: DeliveryStatus;
  updated_at: string;
};

type RequestQueue = { relay: RelayStatus; requests: IntegrationRequest[] };

function IntegrationRequestPanel() {
  const [name, setName] = useState("");
  const [queue, setQueue] = useState<RequestQueue | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [formError, setFormError] = useState("");
  const [loadError, setLoadError] = useState("");
  const pending = queue?.relay === "ready" &&
    queue.requests.some(({ status }) => ["queued", "sending", "retrying"].includes(status));

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const latest = await apiJson<RequestQueue>("/api/integration-requests", { signal });
      if (signal?.aborted) return;
      setQueue((current) => ({
        relay: latest.relay,
        requests: mergeRequests(current?.requests ?? [], latest.requests),
      }));
      setLoadError("");
    } catch {
      if (!signal?.aborted) setLoadError("Unable to load integration requests.");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (!pending) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => void load(controller.signal), 4000);
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [pending, load]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !name.trim()) return;
    setBusy(true);
    setFormError("");
    setMessage("");
    try {
      const { request, duplicate } = await apiJson<{ request: IntegrationRequest; duplicate: boolean }>(
        "/api/integration-requests",
        { method: "POST", body: JSON.stringify({ integration_name: name }) },
      );
      setQueue((current) => current && ({
        ...current,
        requests: mergeRequests(current.requests, [request]),
      }));
      setName("");
      setMessage(duplicate ? "This integration was already requested." : "Integration request saved.");
      void load();
    } catch (error) {
      setFormError(requestError(error, "Unable to save the request."));
    } finally {
      setBusy(false);
    }
  }

  async function retry(request: IntegrationRequest) {
    if (busy) return;
    setBusy(true);
    setFormError("");
    setMessage("");
    try {
      const updated = await apiJson<IntegrationRequest>(
        `/api/integration-requests/${request.integration_id}/retry`, { method: "POST" },
      );
      setQueue((current) => current && ({
        ...current,
        requests: mergeRequests(current.requests, [updated]),
      }));
      setMessage("Request queued for another delivery attempt.");
    } catch (error) {
      setFormError(requestError(error, "Unable to retry delivery."));
    } finally {
      setBusy(false);
    }
  }

  return <section className="workflow-panel form-stack" aria-labelledby="integration-request-title">
    <div>
      <h2 id="integration-request-title">Request Integration</h2>
      <p>Suggest a Home Assistant integration for inkDaddy. The request contains only the integration name and an identifier derived from it.</p>
    </div>
    {queue?.relay === "unconfigured" && <p className="muted-text">Requests stay on this installation until an owner relay is configured.</p>}
    {queue?.relay === "invalid" && <p className="warning-text">The owner relay configuration needs attention. Requests will stay here meanwhile.</p>}
    <form className="form-row" onSubmit={(event) => void submit(event)}>
      <label>Integration name
        <input value={name} maxLength={80} required placeholder="MQTT" disabled={busy || !queue}
          onChange={(event) => setName(event.target.value)} />
      </label>
      <button className="primary-button" disabled={busy || !queue || !name.trim()}>
        {queue?.relay === "ready" ? "Request integration" : "Save request"}
      </button>
    </form>
    {message && <p role="status">{message}</p>}
    {formError && <p className="warning-text" role="alert">{formError}</p>}
    {loadError && <div className="button-row">
      <p role="alert">{loadError}</p>
      <button className="secondary-button" onClick={() => void load()}>Reload requests</button>
    </div>}
    {queue && queue.requests.length > 0 && <ul className="playlist-list" aria-label="Saved integration requests">
      {queue.requests.map((request) => <li key={request.integration_id}>
        <strong>{request.integration_name}</strong>
        <div className="button-row">
          <span className={`status-badge ${request.status === "delivered" ? "status-good" : request.status === "failed" ? "status-warn" : "status-neutral"}`}>
            {deliveryLabel(request.status, queue.relay)}
          </span>
          {request.status === "failed" && queue.relay === "ready" && <button className="secondary-button"
            disabled={busy} onClick={() => void retry(request)}>Retry delivery</button>}
        </div>
      </li>)}
    </ul>}
  </section>;
}

function mergeRequests(saved: IntegrationRequest[], incoming: IntegrationRequest[]): IntegrationRequest[] {
  const rows = new Map(saved.map((row) => [row.integration_id, row]));
  incoming.forEach((row) => {
    const prior = rows.get(row.integration_id);
    if (!prior || row.updated_at >= prior.updated_at) rows.set(row.integration_id, row);
  });
  return [...rows.values()].sort((left, right) => right.updated_at.localeCompare(left.updated_at));
}

function deliveryLabel(status: DeliveryStatus, relay: RelayStatus): string {
  switch (status) {
    case "queued": return relay === "ready" ? "Queued for delivery" : "Saved locally";
    case "sending": return "Sending to owner relay";
    case "retrying": return relay === "ready" ? "Delivery will retry" : "Saved locally";
    case "delivered": return "Accepted by owner relay";
    case "failed": return "Delivery failed";
  }
}

const REQUEST_ERRORS = new Set([
  "Enter the integration name using letters, numbers, spaces, or common name punctuation.",
  "The daily limit for new integration requests was reached.",
  "The integration request queue has reached its capacity.",
  "Wait one minute before retrying delivery.",
  "Configure the owner relay before retrying.",
]);

function requestError(error: unknown, fallback: string): string {
  if (error instanceof Error) {
    try {
      const detail: unknown = JSON.parse(error.message).detail;
      if (typeof detail === "string" && REQUEST_ERRORS.has(detail)) return detail;
    } catch {
      // Ingress can return its own response.
    }
  }
  return fallback;
}
