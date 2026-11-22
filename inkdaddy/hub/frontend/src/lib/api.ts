export function apiPath(path: string): string {
  return path.startsWith("/api/") ? path.slice(1) : path;
}

export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const method = (init?.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) headers.set("X-InkDaddy-Request", "1");
  return fetch(apiPath(path), { ...init, headers });
}

export async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await apiFetch(path, { ...init, headers });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed with ${response.status}`);
  }
  return (await response.json()) as T;
}

export function statusTone(status: string): "status-good" | "status-warn" | "status-danger" | "status-neutral" {
  if (["ready", "connected", "current", "ok"].includes(status)) {
    return "status-good";
  }
  if (["not_configured", "no_devices", "diagnostics_required", "no_firmware_asset"].includes(status)) {
    return "status-warn";
  }
  if (["error", "failed", "offline"].includes(status)) {
    return "status-danger";
  }
  return "status-neutral";
}
