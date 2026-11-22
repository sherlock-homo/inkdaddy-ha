import { useEffect, useState } from "react";
import { Shell } from "./components/Shell";
import { apiJson } from "./lib/api";
import type { PlaybackSource, RouteKey } from "./lib/types";
import { Connect } from "./pages/Connect";
import { DashboardsPage } from "./pages/Dashboards";
import { DevicesPage } from "./pages/Devices";
import { HomeAssistantPage } from "./pages/HomeAssistant";
import { Home } from "./pages/Home";
import { IntegrationsPage } from "./pages/Integrations";
import { PhotosPage } from "./pages/Photos";
import { SimplePage } from "./pages/SimplePages";

const routes: RouteKey[] = [
  "home",
  "connect",
  "photos",
  "dashboards",
  "integrations",
  "devices",
  "home-assistant",
  "settings",
  "updates",
  "diagnostics"
];

function routeFromHash(): RouteKey {
  const hash = window.location.hash.replace(/^#\/?/, "");
  return routes.includes(hash as RouteKey) ? (hash as RouteKey) : "home";
}

export function App() {
  const [displayUid, setDisplayUid] = useState("");
  const [playlistSource, setPlaylistSource] = useState<PlaybackSource | null>(null);
  const [route, setRouteState] = useState<RouteKey>(routeFromHash);
  const [health, setHealth] = useState<"loading" | "ok" | "offline">("loading");

  const setRoute = (nextRoute: RouteKey) => {
    if (nextRoute !== "home") setPlaylistSource(null);
    window.location.hash = nextRoute;
    setRouteState(nextRoute);
  };

  const openDisplay = (uid?: string, source: PlaybackSource | null = null) => {
    if (uid) setDisplayUid(uid);
    setPlaylistSource(source);
    setRoute("home");
  };

  useEffect(() => {
    apiJson<{ ok: boolean }>("/api/health")
      .then((health) => setHealth(health.ok ? "ok" : "offline"))
      .catch(() => setHealth("offline"));
  }, []);

  useEffect(() => {
    const onHashChange = () => setRouteState(routeFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  return (
    <Shell activeRoute={route} onNavigate={setRoute}>
      <div className="top-strip">
        <span className={`status-dot ${health === "ok" ? "good" : health === "loading" ? "neutral" : "danger"}`} />
        <span>Hub API: {health}</span>
      </div>
      {route === "home" && <Home
        onConnect={() => setRoute("connect")}
        preferredUid={displayUid}
        onDisplaySelected={setDisplayUid}
        playlistSource={playlistSource}
        onPlaylistSourceUsed={() => setPlaylistSource(null)}
      />}
      {route === "connect" && <Connect onOpenDisplay={openDisplay} />}
      {route === "photos" && <PhotosPage onAddToPlaylist={(source) => openDisplay(undefined, source)} />}
      {route === "dashboards" && <DashboardsPage />}
      {route === "integrations" && <IntegrationsPage />}
      {route === "devices" && <DevicesPage onConnect={() => setRoute("connect")} onOpenDisplay={openDisplay} />}
      {route === "home-assistant" && <HomeAssistantPage />}
      {route !== "home" &&
        route !== "connect" &&
        route !== "photos" &&
        route !== "dashboards" &&
        route !== "integrations" &&
        route !== "devices" &&
        route !== "home-assistant" && <SimplePage route={route} />}
    </Shell>
  );
}
