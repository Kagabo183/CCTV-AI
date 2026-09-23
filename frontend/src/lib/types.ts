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
  status: "pending" | "ready" | "error";
  status_message: string | null;
  metadata: Record<string, unknown>;
  playback: Playback | null;
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
export type EvidenceItem = { description: string; timestamp_seconds: number | null };

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
