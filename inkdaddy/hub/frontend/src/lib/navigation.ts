export const primaryPages = [
  ["displays", "Displays"],
  ["photos", "Photos"],
  ["dashboards", "Dashboards"],
  ["integrations", "Integrations"],
  ["devices", "Devices"],
  ["settings", "Settings"],
] as const;

export const settingsPages = [
  ["settings", "General"],
  ["settings/updates", "Updates"],
  ["settings/diagnostics", "Diagnostics"],
] as const;

const pages = [
  ...primaryPages,
  ["devices/connect", "Connect a display"],
  ["integrations/home-assistant", "Home Assistant"],
  ...settingsPages.slice(1),
] as const;
export type RouteKey = typeof pages[number][0];

const oldBookmarks = new Map<string, RouteKey>([
  ["home", "displays"],
  ["connect", "devices/connect"],
  ["home-assistant", "integrations/home-assistant"],
  ["updates", "settings/updates"],
  ["diagnostics", "settings/diagnostics"],
]);

export function routeFromHash(hash: string): RouteKey {
  const path = hash.replace(/^#\/?/, "");
  return oldBookmarks.get(path) ?? pages.find(([route]) => route === path)?.[0] ?? "displays";
}
