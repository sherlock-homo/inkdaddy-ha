import type { CSSProperties } from "react";
import type { DashboardTheme, PanelColor } from "../lib/types";

const colors: Record<PanelColor, string> = {
  blue: "#0000ff", yellow: "#ffff00", green: "#00b400", red: "#ff0000", black: "#000000", white: "#ffffff"
};
const colorNames = Object.keys(colors) as PanelColor[];
const schemes = [
  { name: "Cobalt & sun", primary: "blue", accent: "yellow" },
  { name: "Poppy & leaf", primary: "red", accent: "green" },
  { name: "Ink & paper", primary: "black", accent: "white" }
] as const;
export const defaultTheme: DashboardTheme = { primary: "blue", accent: "yellow" };
const colorName = (color: PanelColor) => color[0].toUpperCase() + color.slice(1);

export function dashboardColorStyle(theme: DashboardTheme): CSSProperties {
  return { "--tile-primary": colors[theme.primary], "--tile-accent": colors[theme.accent] } as CSSProperties;
}

export function DashboardColors({ theme, disabled, onChange }: {
  theme: DashboardTheme; disabled: boolean; onChange: (next: DashboardTheme) => void;
}) {
  return <details className="dd-colors">
    <summary><strong>Dashboard colors</strong><span className="dd-color-swatch" style={{ background: colors[theme.primary] }} />
      <span className="dd-color-swatch" style={{ background: colors[theme.accent] }} />
      <span className="dd-color-summary">{colorName(theme.primary)} / {colorName(theme.accent)}</span>
    </summary>
    <div className="dd-color-controls">
      <div className="dd-color-schemes" role="group" aria-label="Color schemes">
        {schemes.map(({ name, primary, accent }) => <button key={name} type="button" disabled={disabled}
          aria-pressed={theme.primary === primary && theme.accent === accent} onClick={() => onChange({ primary, accent })}>
          {name}
        </button>)}
      </div>
      <div className="dd-field-pair">{(["primary", "accent"] as const).map((role) =>
        <label key={role}>{role === "primary" ? "Primary color" : "Accent color"}
          <select disabled={disabled} value={theme[role]} onChange={(event) => onChange({ ...theme, [role]: event.target.value as PanelColor })}>
            {colorNames.map((color) => <option key={color} value={color}>{colorName(color)}</option>)}
          </select>
        </label>)}</div>
    </div>
  </details>;
}
