export type User = {
  id: string;
  email: string;
  full_name: string | null;
  preferred_language: string;
};

export type Playback = { type: "direct" | "youtube" | "proxy"; url: string };

export type VideoSource = {
  id: string;
  name: string;
  location: string | null;
  kind: string;
  uri: string;
  status: "pending" | "importing" | "ready" | "error";
  status_message: string | null;
  metadata: Record<string, unknown>;
  playback: Playback | null;
  import_progress?: number | null;
  created_at: string;
};

export type VideoSession = {
  id: string;
  video_source_id: string;
  analyzer: string;
  status: "preparing" | "ready" | "error" | "expired";
  status_message: string | null;
  provider_ref_expires_at: string | null;
};

export type TimeRef = { start_seconds: number; end_seconds: number | null; label: string };
export type EvidenceLevel = "detection" | "tracking" | "rule" | "model_interpretation";
export type EvidenceItem = { description: string; timestamp_seconds: number | null; level?: EvidenceLevel | null };

export type Message = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  language: string | null;
  input_mode: string | null;
  video_source_id: string | null;
  confidence: number | null;
  timestamps: TimeRef[];
  evidence: EvidenceItem[];
  metadata: {
    analyzer?: string;
    model?: string;
    insufficient_evidence?: boolean;
    failed?: boolean;
    events?: { event_type: string; description: string; start_seconds: number | null }[];
    tools_used?: string[];
    escalated?: boolean;
    evidence_levels?: EvidenceLevel[];
    understanding_providers?: string[];
    agent_llm?: "gemini" | "local";
    agent_model?: string;
  };
  created_at: string;
};

export type Conversation = {
  id: string;
  video_source_id: string | null;
  title: string | null;
  language: string;
  created_at: string;
  updated_at: string;
};

export type ConversationDetail = Conversation & { messages: Message[] };

export type Audio = { mime_type: string; base64: string; provider: string };

export type AskResponse = {
  user_message: Message;
  assistant_message: Message;
  switched_to_source: VideoSource | null;
  transcript: { text: string; language: string; confidence: number | null; provider: string; is_placeholder: boolean } | null;
  audio: Audio | null;
  tts_available: boolean;
};

export type PublicConfig = {
  analyzer: string;
  analyzer_is_mock: boolean;
  stt_provider: string;
  stt_is_placeholder: boolean;
  tts_provider: string;
  tts_is_placeholder: boolean;
  source_kinds: string[];
  default_language: string;
};

export type VisionRun = {
  id: string | null;
  available: boolean;
  unsupported_reason?: string | null;
  queue_position?: number | null; // 1 = next to run
  waiting_for?: string | null; // the run currently using the GPU
  status: "disabled" | "unsupported" | "not_started" | "queued" | "running" | "completed" | "failed" | "cancelled";
  progress: number;
  detector: string | null;
  weights: string | null;
  tracker: string | null;
  label: string | null;
  is_primary: boolean;
  error: string | null;
  tracks_by_class: Record<string, number>;
  events_by_type: Record<string, number>;
  performance: {
    processing_fps?: number;
    realtime_factor?: number;
    detect_ms_p50?: number;
    detect_ms_p95?: number;
    track_ms_p50?: number;
    gpu_peak_mb?: number | null;
    cpu_percent_avg?: number;
    sample_fps?: number;
    duration_seconds?: number;
    resolution?: [number, number];
    frames_processed?: number;
  };
  quality: {
    tracks?: { total?: number; short_lived?: number; mean_seconds?: number; uncertain?: number; by_class?: Record<string, number> };
    detections?: Record<string, { count: number; mean_confidence: number }>;
  };
  max_simultaneous?: Record<string, { count: number; at: number }>;
  settings?: { weights?: string; image_size?: number; tiling?: { tile_size: number; overlap: number } | null; confidence?: number; confirm_confidence?: number };
};

/** Per-frame tracked boxes: o = [track_id, class, confidence, x1, y1, x2, y2] in source pixels. */
export type BoxTrack = {
  resolution: [number, number];
  sample_fps: number;
  confirm_confidence?: number;
  frames: { t: number; o: [number, string, number, number, number, number, number][] }[];
};

export type VideoEvent = {
  id: string;
  event_type: string;
  evidence_level: EvidenceLevel;
  object_class: string | null;
  track_id: number | null;
  zone: string | null;
  description: string;
  start_time: number | null;
  end_time: number | null;
  confidence: number | null;
  detector: string;
};

export type Track = {
  track_id: number;
  object_class: string; // "unknown" when the detector was not confident
  candidate_class: string | null;
  uncertain: boolean;
  mean_confidence: number;
  max_confidence: number;
  first_seen: number;
  last_seen: number;
  frames: number;
  class_votes: Record<string, number>;
  last_bbox: number[];
};

export type DescribeResult = {
  track_id: number;
  description: string;
  observations: string[];
  confidence: number | null;
  insufficient_evidence: boolean;
  provider: string;
  model: string | null;
  window: [number, number];
};
