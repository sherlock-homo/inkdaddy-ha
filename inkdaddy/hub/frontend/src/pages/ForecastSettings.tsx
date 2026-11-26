import { useEffect, useId, useRef, useState } from "react";
import { apiJson } from "../lib/api";
import type { DashboardWidget } from "../lib/types";

type ForecastLocation = { location: string; latitude: number; longitude: number; timezone: string };

export const atlantaForecast = {
  type: "forecast", location: "Atlanta, GA", latitude: 33.749, longitude: -84.388, timezone: "America/New_York"
};

export function ForecastSettings({ widget, disabled, onChange }: {
  widget: DashboardWidget; disabled: boolean; onChange: (next: DashboardWidget) => void;
}) {
  const [query, setQuery] = useState("");
  const [places, setPlaces] = useState<ForecastLocation[]>([]);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const inputId = useId();
  const request = useRef<AbortController | null>(null);
  const city = typeof widget.source?.location === "string" ? widget.source.location : atlantaForecast.location;
  const unit = widget.source?.unit === "C" ? "C" : "F";
  const searchTerm = query.trim();
  useEffect(() => () => request.current?.abort(), []);

  const search = async () => {
    if (searchTerm.length < 2 || disabled) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setMessage("");
    setPlaces([]);
    try {
      const params = new URLSearchParams({ query: searchTerm });
      const { locations } = await apiJson<{ locations: ForecastLocation[] }>(
        `/api/dashboards/forecast/locations?${params}`, { signal: controller.signal });
      if (!controller.signal.aborted) {
        setPlaces(locations);
        setMessage(locations.length ? "" : "No cities found. Try a nearby city.");
      }
    } catch {
      if (!controller.signal.aborted) setMessage("City search is unavailable. Try again later.");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  };

  return <div className="dd-forecast-settings">
    <span className="dd-city-choice">Forecast for <strong>{city}</strong></span>
    <div className="dd-city-field">
      <label htmlFor={inputId}>Find a city</label>
      <div className="dd-city-search">
        <input id={inputId} type="search" value={query} maxLength={80} placeholder="City name" disabled={disabled}
          onChange={(event) => {
            request.current?.abort();
            setQuery(event.target.value); setLoading(false); setPlaces([]); setMessage("");
          }} onKeyDown={(event) => {
            if (event.key === "Enter") { event.preventDefault(); void search(); }
          }} />
        <button type="button" className="secondary-button" disabled={disabled || loading || searchTerm.length < 2}
          aria-label="Search cities" onClick={() => void search()}>Search</button>
      </div>
    </div>
    {loading && <p className="dd-hint" role="status">Searching cities…</p>}
    {message && <p className="dd-hint" role="status">{message}</p>}
    {places.length > 0 && <div className="dd-entity-results" aria-label="Matching cities">
      {places.map((place) => <button type="button" key={`${place.latitude}:${place.longitude}:${place.location}`}
        disabled={disabled} onClick={() => {
          onChange({ ...widget, source: { ...widget.source, type: "forecast", ...place, unit } });
          setPlaces([]); setQuery(""); setMessage("");
        }}>{place.location}</button>)}
    </div>}
    <label>Forecast temperatures
      <select value={unit} disabled={disabled} onChange={(event) =>
        onChange({ ...widget, source: { ...widget.source, type: "forecast", unit: event.target.value } })}>
        <option value="F">Fahrenheit (°F)</option><option value="C">Celsius (°C)</option>
      </select>
    </label>
    <small className="dd-hint">Forecast by <a href="https://www.7timer.info/" target="_blank" rel="noreferrer">7Timer!</a>.
      {" "}City search by <a href="https://open-meteo.com/" target="_blank" rel="noreferrer">Open-Meteo</a> and <a href="https://www.geonames.org/" target="_blank" rel="noreferrer">GeoNames</a>.</small>
  </div>;
}
