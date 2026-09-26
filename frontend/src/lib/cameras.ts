// Live camera platform: types and API calls (/api/cameras, /api/gateways).
// The browser never receives camera passwords, gateway secrets or RTSP URLs with credentials:
// live video is opened with a short-lived token scoped to one camera stream.

import { ApiError } from "./api";

export type CameraState = "online" | "offline" | "connecting" | "auth_failed" | "stream_error" | "gateway_offline" | "disconnected";

export type StreamInfo = { role: "main" | "sub" | "low"; uri: string; codec?: string; width?: number; height?: number; fps?: number; audio?: boolean };

export type LiveObject = { track_id: number; label: string; confidence: number; bbox: number[]; detector: string };

export type LiveAiState = {
  status: "starting" | "running" | "waiting_for_stream" | "reconnecting" | "stopped";
  counts: Record<string, number>;
  objects: LiveObject[];
  at?: string;
  resolution?: [number, number];
  latency_ms?: number;
  ai_fps?: number;
  frames: number;
  reconnects: number;
  dropped: number;
};

export type Camera = {
  id: string;
  name: string;
  location: string | null;
  kind: "camera_rtsp" | "camera_onvif" | "camera_hls" | "camera_vendor" | "nvr" | "nvr_channel";
  parent_id: string | null;
  gateway_id: string | null;
  state: CameraState;
  last_seen_at: string | null;
  role: "owner" | "admin" | "operator" | "viewer" | null;
  permissions: string[];
  connection: { type: string | null; address: string | null; vendor: string | null };
  device: Record<string, string | undefined>;
  capabilities: Capabilities;
  streams: StreamInfo[];
  channel: string | null;
  ai_profile: Record<string, boolean>;
  health: {
    diagnosis?: string;
    bitrate_kbps?: number | null;
    reconnects?: number;
    viewers?: number;
    codecs?: string[];
    online_since?: string;
    reconnect_count?: number;
    last_disconnect?: string;
    last_reconnect?: string;
  };
  live: LiveAiState | null;
  created_at: string;
  channels?: { id: string; name: string; state: CameraState; channel: string }[];
};

export type Check = { name: string; ok: boolean | null; detail: string; code: string; ms: number | null; explanation?: string; next_step?: string };

export type ProbeResult = {
  ok: boolean;
  diagnosis: string;
  explanation: string;
  next_step: string;
  checks: Check[];
  capabilities: Record<string, unknown>;
  device: Record<string, string | undefined>;
  channels: { key: string; name: string; streams: StreamInfo[] }[];
};

export type ConnectionSpec = { type: "rtsp" | "onvif" | "nvr" | "hls" | "vendor"; url?: string; sub_url?: string; xaddr?: string; vendor?: string; device_id?: string };

export type LiveTicket = {
  whep_url: string;
  hls_url: string;
  stream: string;
  codec: string | null;
  transcoded: boolean;
  available_streams: string[];
  state: CameraState;
  message: string | null; // why there is no video, in plain words (when the camera is not online)
  expires_in: number;
  audio: boolean;
  ice_servers: RTCIceServer[];
};

/** What the camera can do in Visionary: the UI only shows controls for what is true. */
export type Capabilities = {
  video: boolean;
  audio: boolean;
  talk: boolean;
  ptz: boolean;
  recording: boolean;
  playback: boolean;
  events: boolean;
  snapshot: boolean;
  h264: boolean;
  h265: boolean;
  substream: boolean;
  ai: boolean;
  onvif_profiles: string[];
};

export type CameraEvent = {
  id: string;
  camera_id: string;
  type: string;
  object: string | null;
  track_id: number | null;
  zone: string | null;
  description: string;
  confidence: number | null;
  evidence: string;
  detector: string;
  occurred_at: string | null;
  bbox?: number[] | null;
  species?: string | null;
  species_candidate?: string | null;
  clip?: { start: string; duration: number } | null;
};

export type Discovered = {
  name: string | null;
  ip: string;
  xaddrs: string[];
  id: string;
  hardware?: string | null;
  location?: string | null;
  onvif?: boolean;
  profiles?: string[];
  profile_t?: boolean;
  already_added?: boolean;
  kind_hint?: "camera" | "nvr";
};

export type Connector = { id: string; name: string; status: "available" | "untested" | "needs_configuration" | "via_standard"; how: string };

export type Diagnostics = ProbeResultBase & { health: Camera["health"]; state: CameraState };
type ProbeResultBase = { ok: boolean; diagnosis: string; explanation: string; next_step: string; checks: Check[] };

export type Gateway = {
  id: string;
  name: string;
  location: string | null;
  status: "pending" | "online" | "offline" | "revoked";
  version: string | null;
  host: string | null;
  networks: string[];
  health: { uptime?: number; streams?: Record<string, { running: boolean; restarts: number }> };
  last_heartbeat_at: string | null;
  created_at: string;
  camera_count?: number;
  enrollment_token?: string;
  expires_at?: string | null;
  cameras?: { id: string; name: string; state: CameraState }[];
};

export type Overview = {
  cameras: number;
  states: Partial<Record<CameraState, number>>;
  media_server: boolean;
  viewers: number;
  bytes_sent: number;
  live_ai: Record<string, { running: boolean; fps: number; latency_ms: number; detectors: string[]; frames: number; reconnects: number; dropped: number }>;
};

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, { ...init, credentials: "same-origin", headers: { "Content-Type": "application/json", ...init.headers } });
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d: { msg: string }) => d.msg).join(", ") : data.detail ?? `Request failed (${res.status})`;
    throw new ApiError(detail, res.status, data.code);
  }
  return data as T;
}
const body = (b: unknown) => JSON.stringify(b);

export const cams = {
  list: () => call<Camera[]>("/cameras"),
  get: (id: string) => call<Camera>(`/cameras/${id}`),
  overview: () => call<Overview>("/cameras-overview"),
  discover: (gateway_id?: string) => call<{ devices: Discovered[]; networks?: string[]; message?: string }>("/cameras/discover", { method: "POST", body: body({ gateway_id, timeout: 3 }) }),
  test: (connection: ConnectionSpec, username?: string, password?: string, gateway_id?: string) =>
    call<ProbeResult>("/cameras/test-connection", { method: "POST", body: body({ connection, username, password, gateway_id }) }),
  add: (b: { name: string; location?: string; connection: ConnectionSpec; username?: string; password?: string; channels?: string[]; gateway_id?: string; ai_profile?: Record<string, boolean> }) =>
    call<Camera[]>("/cameras", { method: "POST", body: body(b) }),
  update: (id: string, b: { name?: string; location?: string; ai_profile?: Record<string, boolean>; username?: string; password?: string; url?: string; sub_url?: string }) =>
    call<Camera>(`/cameras/${id}`, { method: "PATCH", body: body(b) }),
  remove: (id: string) => call<void>(`/cameras/${id}`, { method: "DELETE" }),
  connect: (id: string) => call<Camera>(`/cameras/${id}/connect`, { method: "POST" }),
  disconnect: (id: string) => call<Camera>(`/cameras/${id}/disconnect`, { method: "POST" }),
  live: (id: string, quality: "auto" | "main" | "sub", h265: boolean) => call<LiveTicket>(`/cameras/${id}/live?quality=${quality}&h265=${h265}`),
  events: (id: string, minutes: number) => call<CameraEvent[]>(`/cameras/${id}/events?minutes=${minutes}`),
  capabilities: (id: string) => call<Capabilities>(`/cameras/${id}/capabilities`),
  diagnostics: (id: string) => call<Diagnostics>(`/cameras/${id}/diagnostics`),
  refresh: (id: string) => call<Camera>(`/cameras/${id}/refresh`, { method: "POST" }),
  ptzInfo: (id: string) => call<{ presets: { token: string; name: string }[]; position: { pan: number | null; tilt: number | null; zoom: number | null } }>(`/cameras/${id}/ptz`),
  ptz: (id: string, b: { action: "move" | "stop" | "preset"; pan?: number; tilt?: number; zoom?: number; preset?: string }) =>
    call<{ ok: boolean }>(`/cameras/${id}/ptz`, { method: "POST", body: body(b) }),
  recentEvents: (minutes = 60, limit = 50) => call<CameraEvent[]>(`/cameras-events?minutes=${minutes}&limit=${limit}`),
  connectors: () => call<Connector[]>("/cameras-connectors"),
  recordings: (id: string) => call<{ recording: boolean; retention: string; spans: { start: string; duration: number }[] }>(`/cameras/${id}/recordings`),
  playbackUrl: (id: string, start: string, duration: number) => `/api/cameras/${id}/playback?start=${encodeURIComponent(start)}&duration=${Math.round(duration)}`,
  snapshotUrl: (id: string, bust: number) => `/api/cameras/${id}/snapshot?t=${bust}`,
  shares: (id: string) => call<{ id: string; email: string; role: string; permissions: string[] }[]>(`/cameras/${id}/shares`),
  share: (id: string, email: string, role: string) => call<{ id: string }>(`/cameras/${id}/shares`, { method: "POST", body: body({ email, role, permissions: [] }) }),
  unshare: (id: string, shareId: string) => call<void>(`/cameras/${id}/shares/${shareId}`, { method: "DELETE" }),

  gateways: () => call<Gateway[]>("/gateways"),
  gateway: (id: string) => call<Gateway>(`/gateways/${id}`),
  createGateway: (name: string, location?: string) => call<Gateway>("/gateways", { method: "POST", body: body({ name, location }) }),
  revokeGateway: (id: string) => call<void>(`/gateways/${id}`, { method: "DELETE" }),
};

export const STATE_LABEL: Record<CameraState, { label: string; tone: "ok" | "warn" | "bad" | "idle" }> = {
  online: { label: "Online", tone: "ok" },
  connecting: { label: "Connecting", tone: "warn" },
  offline: { label: "Offline", tone: "bad" },
  auth_failed: { label: "Login refused", tone: "bad" },
  stream_error: { label: "No video", tone: "bad" },
  gateway_offline: { label: "Gateway offline", tone: "bad" },
  disconnected: { label: "Disconnected", tone: "idle" },
};

// Plain-language diagnosis (mirrors backend app/cameras/diagnostics.py GUIDANCE for the codes seen in status).
export const DIAGNOSIS_HINT: Record<string, string> = {
  CAMERA_OFFLINE: "The camera does not answer. Check its power and network cable.",
  NETWORK_UNREACHABLE: "The camera's network cannot be reached from here.",
  AUTHENTICATION_FAILED: "The camera rejected the username or password.",
  INVALID_CREDENTIALS: "The camera needs a username and password.",
  LINK_EXPIRED: "The stream link was refused: its access token has probably expired. Paste a new link in Settings.",
  STREAM_NOT_FOUND: "The camera answers, but this video stream does not exist (it may have restarted or the path changed).",
  STREAM_TIMEOUT: "The camera connected but sent no video in time.",
  UNSUPPORTED_CODEC: "The camera sends a video format Visionary cannot read.",
  GATEWAY_OFFLINE: "The gateway on the camera's network is offline.",
  RTSP_DISABLED: "The camera does not accept video (RTSP) connections on this port.",
  DNS_FAILED: "The camera's address could not be found.",
};

export const OBJECT_NAMES: Record<string, [string, string]> = {
  person: ["person", "people"],
  car: ["car", "cars"],
  truck: ["truck", "trucks"],
  bus: ["bus", "buses"],
  motorcycle: ["motorcycle", "motorcycles"],
  bicycle: ["bicycle", "bicycles"],
  unknown: ["unidentified object", "unidentified objects"],
  animal: ["animal (species unknown)", "animals (species unknown)"],
};

export function countsText(counts: Record<string, number> | undefined): string {
  const entries = Object.entries(counts ?? {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return "Nothing detected";
  return entries
    .slice(0, 4)
    .map(([k, n]) => `${n} ${(OBJECT_NAMES[k] ?? [k, k.endsWith("s") ? k : `${k}s`])[n === 1 ? 0 : 1]}`)
    .join(" · ");
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

export function clock(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "?";
}

export function browserDecodesH265(): boolean {
  try {
    const caps = typeof RTCRtpReceiver !== "undefined" ? RTCRtpReceiver.getCapabilities?.("video") : null;
    return !!caps?.codecs.some((c) => /h265|hevc/i.test(c.mimeType));
  } catch {
    return false;
  }
}

export type EventLine = { key: string; at: string | null; text: string; cameraId: string; count: number; firstId: string };

/**
 * Plain-language activity: "object appeared" events are grouped per minute and object type
 * ("3 people appeared"); zone, crowd, loitering and wildlife events are shown as they are.
 * "Left the view" events are left out (they mirror arrivals).
 */
export function summarizeEvents(events: CameraEvent[]): EventLine[] {
  const out: EventLine[] = [];
  const groups = new Map<string, EventLine>();
  const seen = new Set<string>();
  for (const e of events) {
    if (e.type === "object_disappeared") continue;
    if (e.type === "object_appeared" && e.object && !e.object.startsWith("unidentified")) {
      const minute = (e.occurred_at ?? "").slice(0, 16);
      const key = `${e.camera_id}|${minute}|${e.object}`;
      const g = groups.get(key);
      if (g) {
        g.count += 1;
        continue;
      }
      const line: EventLine = { key, at: e.occurred_at, text: "", cameraId: e.camera_id, count: 1, firstId: e.id };
      groups.set(key, line);
      out.push(line);
      (line as EventLine & { object: string }).object = e.object;
      continue;
    }
    // the same rule firing again within a minute (e.g. "5 animals in view") is shown once
    const text = e.description.replace(/ #\d+/g, "").replace(/\s*\(threshold \d+\)/, "");
    const key = `${e.camera_id}|${(e.occurred_at ?? "").slice(0, 16)}|${text}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ key, at: e.occurred_at, text, cameraId: e.camera_id, count: 1, firstId: e.id });
  }
  for (const line of groups.values()) {
    const object = (line as EventLine & { object: string }).object;
    const [one, many] = OBJECT_NAMES[object] ?? [object, object.endsWith("s") ? object : `${object}s`];
    line.text = line.count === 1 ? `${/^[aeiou]/i.test(one) ? "An" : "A"} ${one} appeared` : `${line.count} ${many} appeared`;
  }
  return out;
}
