import { useEffect, useRef, type ReactNode } from "react";
import { primaryPages, settingsPages, type RouteKey } from "../lib/navigation";
import { Logo } from "./Logo";
import "./navigation.css";

export function Shell({ activeRoute, children }: { activeRoute: RouteKey; children: ReactNode }) {
  const main = useRef<HTMLElement>(null);
  const lastRoute = useRef(activeRoute);
  const section = activeRoute.split("/")[0];

  useEffect(() => {
    if (lastRoute.current === activeRoute) return;
    lastRoute.current = activeRoute;
    main.current?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }, [activeRoute]);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Logo />
        <nav className="nav-list" aria-label="Primary">
          {primaryPages.map(([path, label]) => (
            <a key={path} href={`#${path}`}
              className={path === section ? "nav-item nav-item-active" : "nav-item"}
              aria-current={path === section ? (path === activeRoute ? "page" : "location") : undefined}>
              {label}
            </a>
          ))}
        </nav>
      </aside>
      <main className="main-panel" tabIndex={-1} ref={main}>
        {section === "settings" && <nav className="section-links" aria-label="Settings sections">
          {settingsPages.map(([path, label]) => (
            <a key={path} href={`#${path}`} aria-current={path === activeRoute ? "page" : undefined}>{label}</a>
          ))}
        </nav>}
        {(activeRoute === "devices/connect" || activeRoute === "integrations/home-assistant") &&
          <a className="section-back" href={`#${section}`}>
            <span aria-hidden="true">‹</span> {section === "devices" ? "Devices" : "Integrations"}
          </a>}
        {children}
      </main>
    </div>
  );
}
