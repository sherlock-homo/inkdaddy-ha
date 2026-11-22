import { navItems } from "../lib/mockData";
import type { RouteKey } from "../lib/types";
import { Logo } from "./Logo";

interface ShellProps {
  activeRoute: RouteKey;
  onNavigate: (route: RouteKey) => void;
  children: React.ReactNode;
}

export function Shell({ activeRoute, onNavigate, children }: ShellProps) {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Logo />
        <nav className="nav-list" aria-label="Primary">
          {navItems.map(([key, label]) => (
            <button
              key={key}
              className={key === activeRoute ? "nav-item nav-item-active" : "nav-item"}
              aria-current={key === activeRoute ? "page" : undefined}
              onClick={() => onNavigate(key)}
            >
              <span className="nav-icon" aria-hidden="true" />
              <span>{label}</span>
            </button>
          ))}
        </nav>
      </aside>
      <main className="main-panel">{children}</main>
    </div>
  );
}
