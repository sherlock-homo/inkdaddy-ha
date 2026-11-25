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
    <section className="card-grid">
      {integrations.map((integration) => {
        const thread = integration.id === "thread_border_router";
        const managed = integration.status === "managed_by_home_assistant";
        return <article className="asset-card integration-card" key={integration.id}>
          <div className="preview-toolbar">
            <h2>{integration.name}</h2>
            <span className={`status-badge ${integration.configured ? "status-good" : "status-neutral"}`}>
              {integrationSummary(integration)}
            </span>
          </div>
          <div className="button-row">
            <a className="secondary-link" href={integration.manage_url} target={managed ? "_parent" : undefined}>
              {managed ? "Thread settings" : thread ? "Diagnostics" : integration.id === "home_assistant" ? "Home Assistant settings" : "Manage devices"}
            </a>
          </div>
        </article>;
      })}
      <IntegrationRequestPanel />
    </section>
  </div>;
}

function integrationSummary(integration: IntegrationRecord): string {
  if (integration.status === "managed_by_home_assistant") return "Managed by Home Assistant";
  if (integration.id === "thread_border_router") return "External";
  if (integration.device_count !== undefined) {
    return `${integration.device_count} ${integration.device_count === 1 ? "display" : "displays"}`;
  }
  if (integration.status === "connected" && integration.entity_count !== undefined) {
    return `${integration.entity_count} ${integration.entity_count === 1 ? "entity" : "entities"}`;
  }
  return integration.status === "connected" ? "Connected" : "Setup needed";
}

type RelayStatus = "ready" | "unconfigured" | "invalid";
type DeliveryStatus = "queued" | "sending" | "retrying" | "delivered" | "failed";

type IntegrationRequest = {
  integration_id: string;
  integration_name: string;
  status: DeliveryStatus;
  updated_at: string;
};

type RequestStatus = { relay: RelayStatus; request: IntegrationRequest | null };

function IntegrationRequestPanel() {
  const [name, setName] = useState("");
  const [relay, setRelay] = useState<RelayStatus | null>(null);
  const [request, setRequest] = useState<IntegrationRequest | null>(null);
  const [duplicate, setDuplicate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [revision, setRevision] = useState(0);
  const pendingId = relay === "ready" && request && ["queued", "sending", "retrying"].includes(request.status)
    ? request.integration_id : null;

  useEffect(() => {
    const controller = new AbortController();
    const path = "/api/integration-requests/status" + (pendingId ? `?integration_id=${pendingId}` : "");
    async function refresh() {
      try {
        const latest = await apiJson<RequestStatus>(path, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setRelay(latest.relay);
        const updated = latest.request;
        if (updated) setRequest(current =>
          current?.integration_id === updated.integration_id && updated.updated_at >= current.updated_at ? updated : current);
        setLoadError("");
      } catch {
        if (!controller.signal.aborted) setLoadError(pendingId ? "Couldn’t check request status." : "Couldn’t load the request form.");
      }
    }
    void refresh();
    const timer = pendingId ? window.setInterval(() => void refresh(), 4000) : undefined;
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [pendingId, revision]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !name.trim()) return;
    setBusy(true);
    setFormError("");
    try {
      const result = await apiJson<{ request: IntegrationRequest; duplicate: boolean }>(
        "/api/integration-requests",
        { method: "POST", body: JSON.stringify({ integration_name: name }) },
      );
      setRequest(result.request);
      setDuplicate(result.duplicate);
      setLoadError("");
      setName("");
    } catch (error) {
      setFormError(requestError(error, "Couldn’t save the request. Try again."));
    } finally {
      setBusy(false);
    }
  }

  async function retry() {
    if (!request || busy) return;
    setBusy(true);
    setFormError("");
    try {
      const updated = await apiJson<IntegrationRequest>(
        `/api/integration-requests/${request.integration_id}/retry`, { method: "POST" },
      );
      setDuplicate(false);
      setRequest(updated);
    } catch (error) {
      setFormError(requestError(error, "Couldn’t resend the request. Try again."));
    } finally {
      setBusy(false);
    }
  }

  return <article className="asset-card integration-card" aria-labelledby="integration-request-title">
    <div className="preview-toolbar"><h2 id="integration-request-title">Request an integration</h2></div>
    <form className="form-stack" onSubmit={submit}>
      <label>Integration name
        <input value={name} minLength={2} maxLength={80} required placeholder="e.g. Philips Hue" disabled={busy || !relay}
          onChange={(event) => setName(event.target.value)} />
      </label>
      <div className="button-row"><button className="primary-button" disabled={busy || !relay || !name.trim()}>
        {relay && relay !== "ready" ? "Save request" : "Send request"}
      </button></div>
    </form>
    {relay && relay !== "ready" && !request && <p className="muted-text">Delivery is off. Requests will be saved here.</p>}
    {request && <div className="button-row">
      <span role="status">{requestMessage(request, relay, duplicate)}</span>
      {request.status === "failed" && relay === "ready" &&
        <button className="secondary-button" disabled={busy} onClick={retry}>Retry delivery</button>}
    </div>}
    {formError && <p className="warning-text" role="alert">{formError}</p>}
    {loadError && <div className="button-row">
      <span role="alert">{loadError}</span>
      <button className="secondary-button" disabled={busy} onClick={() => setRevision(revision + 1)}>Retry</button>
    </div>}
  </article>;
}

function requestMessage(request: IntegrationRequest, relay: RelayStatus | null, duplicate: boolean): string {
  const name = request.integration_name;
  switch (request.status) {
    case "failed": return `The request for ${name} couldn’t be sent.`;
    case "delivered": return duplicate ? `${name} was already requested.` : `Request for ${name} sent.`;
    case "sending": return `Sending the request for ${name}…`;
    case "retrying": return relay === "ready" ? `Delivery for ${name} is delayed. We'll try again.` : `Request for ${name} saved here.`;
    case "queued":
      if (duplicate) return relay === "ready" ? `A request for ${name} is already queued.` : `A request for ${name} is already saved here.`;
      return relay === "ready" ? `Request for ${name} queued.` : `Request for ${name} saved here.`;
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
