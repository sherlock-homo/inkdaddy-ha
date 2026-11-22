export type RouteKey =
  | "home"
  | "connect"
  | "photos"
  | "dashboards"
  | "integrations"
  | "devices"
  | "home-assistant"
  | "settings"
  | "updates"
  | "diagnostics";

export interface DisplayStatus {
  name: string;
  batteryPercent: number;
  lastRefresh: string;
  nextWake: string;
  activeMode: string;
  meshConnected: boolean;
  provisioned: boolean;
}

export interface PhotoRecord {
  id: string;
  original_filename: string;
  processed_status: string;
  width: number | null;
  height: number | null;
  palette: string | null;
  checksum: string | null;
  preview_url: string | null;
  frame_url: string | null;
  uploaded_at: string | null;
}

export interface AlbumRecord {
  id: string;
  name: string;
  mode: string;
  item_count: number;
}

export interface DeviceRecord {
  id: string;
  device_uid: string;
  name: string;
  hardware_target: string;
  firmware_version: string | null;
  provisioned: boolean;
  mesh_connected: boolean;
  refresh_interval_minutes: number;
  refresh_interval_warning: boolean;
  selected_source_type: string;
  selected_source_id: string | null;
  last_seen_at: string | null;
  last_refresh_result: string | null;
  last_frame_id: string | null;
  battery_percent: number | null;
  battery_voltage: number | null;
}

export interface DeviceHistoryRecord {
  status: string;
  battery_percent: number | null;
  battery_voltage: number | null;
  created_at: string;
}

export interface HAConfig {
  base_url: string | null;
  configured: boolean;
  enabled: boolean;
  mode?: "supervisor" | "manual";
  location_name?: string | null;
  time_zone?: string | null;
  last_validated_at: string | null;
  token_preview: string | null;
  token_page_url?: string | null;
  entity_count: number;
}

export type PlaybackSource = { type: "dashboard" | "photo" | "album"; id: string };

export interface FrameSlot {
  scheduled_at: string;
  source_type: "dashboard" | "photo";
  source_id: string;
  title: string;
  frame_id: string;
  preview_url: string;
}

export interface Playback {
  paused: boolean;
  syncing_controls?: boolean;
  playlist: PlaybackSource[];
  current: FrameSlot | null;
  pending?: FrameSlot | null;
  pending_status?: "waiting" | "failed" | null;
  upcoming: FrameSlot[];
}

export interface DashboardWidget {
  id: string;
  type: string;
  x: number;
  y: number;
  w: number;
  h: number;
  source?: { type?: string; entity_id?: string } & Record<string, unknown>;
  options?: { label?: string; text?: string; unit?: string } & Record<string, unknown>;
}

export interface DashboardConfig {
  name: string;
  widgets: DashboardWidget[];
  grid: { cols: number; rows: number };
  [key: string]: unknown;
}

export interface DashboardRecord {
  id: string;
  name: string;
  yaml: string;
  active: boolean;
  config: DashboardConfig;
}

export interface HAEntity {
  entity_id: string;
  domain: string;
  name: string | null;
  state: string | null;
  attributes: Record<string, unknown>;
  source_reported_at?: string | null;
  updated_at: string | null;
}

export interface IntegrationRecord {
  id: string;
  name: string;
  status: string;
  configured: boolean | null;
  enabled: boolean | null;
  entity_count?: number;
  device_count?: number;
  manage_url: string;
}
