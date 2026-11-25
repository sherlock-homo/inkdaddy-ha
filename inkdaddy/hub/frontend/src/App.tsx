import { useEffect, useState } from "react";
import { Shell } from "./components/Shell";
import { apiJson } from "./lib/api";
import type { PlaybackSource } from "./lib/types";
import { routeFromHash, type RouteKey } from "./lib/navigation";
import { Connect } from "./pages/Connect";
import { DashboardsPage, type DashboardDraft } from "./pages/Dashboards";
import { DevicesPage } from "./pages/Devices";
import { HomeAssistantPage } from "./pages/HomeAssistant";
import { Home } from "./pages/Home";
import { IntegrationsPage } from "./pages/Integrations";
import { PhotosPage } from "./pages/Photos";
import { SimplePage } from "./pages/SimplePages";

export function App() {
  const [displayUid, setDisplayUid] = useState("");
  const [playlistSource, setPlaylistSource] = useState<PlaybackSource | null>(null);
  const [dashboardDraft, setDashboardDraft] = useState<DashboardDraft | null>(null);
  const hasDashboardDraft = dashboardDraft !== null;
  const [route, setRouteState] = useState<RouteKey>(() => routeFromHash(window.location.hash));
  const [offline, setOffline] = useState(false);

  const openDisplay = (uid?: string, source: PlaybackSource | null = null) => {
    if (uid) setDisplayUid(uid);
    setPlaylistSource(source);
    window.location.hash = "displays";
  };

  useEffect(() => {
    const controller = new AbortController();
    apiJson<{ ok: boolean }>("/api/health", { signal: controller.signal })
      .then((health) => { if (!controller.signal.aborted) setOffline(!health.ok); })
      .catch(() => { if (!controller.signal.aborted) setOffline(true); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const onHashChange = () => {
      const next = routeFromHash(window.location.hash);
      if (window.location.hash && window.location.hash !== `#${next}`) {
        window.history.replaceState(window.history.state, "", `#${next}`);
      }
      if (next !== "displays") setPlaylistSource(null);
      setRouteState(next);
    };
    onHashChange();
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    if (!hasDashboardDraft) return;
    const warnBeforeLeaving = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = true;
    };
    window.addEventListener("beforeunload", warnBeforeLeaving);
    return () => window.removeEventListener("beforeunload", warnBeforeLeaving);
  }, [hasDashboardDraft]);

  return (
    <Shell activeRoute={route}>
      {offline && <div className="connection-warning" role="alert">
        <span>Couldn’t reach inkDaddy.</span>
        <button className="secondary-button" onClick={() => window.location.reload()}>Reload</button>
      </div>}
      {route === "displays" && <Home
        onConnect={() => { window.location.hash = "devices/connect"; }}
        preferredUid={displayUid}
        onDisplaySelected={setDisplayUid}
        playlistSource={playlistSource}
        onPlaylistSourceUsed={() => setPlaylistSource(null)}
      />}
      {route === "devices/connect" && <Connect onOpenDisplay={openDisplay} />}
      {route === "photos" && <PhotosPage onAddToPlaylist={(source) => openDisplay(undefined, source)} />}
      {route === "dashboards" && <DashboardsPage draft={dashboardDraft} onDraftChange={setDashboardDraft} />}
      {route === "integrations" && <IntegrationsPage />}
      {route === "devices" && <DevicesPage onConnect={() => { window.location.hash = "devices/connect"; }} onOpenDisplay={openDisplay} />}
      {route === "integrations/home-assistant" && <HomeAssistantPage />}
      {route === "settings" && <SimplePage route="settings" />}
      {route === "settings/updates" && <SimplePage route="updates" />}
      {route === "settings/diagnostics" && <SimplePage route="diagnostics" />}
    </Shell>
  );
}
