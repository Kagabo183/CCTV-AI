import type {
  AskResponse,
  Conversation,
  ConversationDetail,
  PublicConfig,
  User,
  VideoEvent,
  VideoSession,
  VideoSource,
  VisionRun,
  BoxTrack,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code?: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const isForm = init.body instanceof FormData;
  const res = await fetch(`/api${path}`, {
    ...init,
    credentials: "same-origin",
    headers: isForm ? init.headers : { "Content-Type": "application/json", ...init.headers },
  });
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((d: { msg: string }) => d.msg).join(", ")
      : data.detail ?? `Request failed (${res.status})`;
    throw new ApiError(detail, res.status, data.code);
  }
  return data as T;
}

const json = (body: unknown) => JSON.stringify(body);

export const api = {
  config: () => request<PublicConfig>("/config"),

  me: () => request<User>("/auth/me"),
  login: (email: string, password: string) =>
    request<{ user: User }>("/auth/login", { method: "POST", body: json({ email, password }) }),
  register: (email: string, password: string, full_name: string) =>
    request<{ user: User }>("/auth/register", { method: "POST", body: json({ email, password, full_name }) }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),

  sources: () => request<VideoSource[]>("/video-sources"),
  addSource: (body: { name: string; location?: string; uri: string; kind?: string }) =>
    request<VideoSource>("/video-sources", { method: "POST", body: json(body) }),
  uploadSource: (file: File, name: string, location: string, onProgress: (fraction: number) => void) =>
    new Promise<VideoSource>((resolve, reject) => {
      // XHR (not fetch) so we can report upload progress.
      const form = new FormData();
      form.append("file", file);
      form.append("name", name);
      if (location) form.append("location", location);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/video-sources/upload");
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
      xhr.onload = () => {
        let data: { detail?: string; code?: string } = {};
        try {
          data = JSON.parse(xhr.responseText);
        } catch {}
        if (xhr.status >= 200 && xhr.status < 300) resolve(data as unknown as VideoSource);
        else reject(new ApiError(typeof data.detail === "string" ? data.detail : `Upload failed (${xhr.status})`, xhr.status, data.code));
      };
      xhr.onerror = () => reject(new ApiError("Upload failed: network error", 0));
      xhr.send(form);
    }),
  deleteSource: (id: string) => request<void>(`/video-sources/${id}`, { method: "DELETE" }),
  openSource: (id: string) => request<VideoSession>(`/video-sources/${id}/open`, { method: "POST" }),
  session: (sourceId: string, sessionId: string) =>
    request<VideoSession>(`/video-sources/${sourceId}/sessions/${sessionId}`),

  visionRuns: (sourceId: string) => request<VisionRun[]>(`/video-sources/${sourceId}/vision/runs`),
  runVision: (sourceId: string, detector: string, tracker: string) =>
    request<VisionRun>(`/video-sources/${sourceId}/vision/run`, { method: "POST", body: json({ detector, tracker }) }),
  boxes: (sourceId: string, runId: string) => request<BoxTrack>(`/video-sources/${sourceId}/vision/runs/${runId}/boxes`),
  events: (sourceId: string, runId: string) =>
    request<VideoEvent[]>(`/video-sources/${sourceId}/events?limit=500&evidence_level=rule&vision_run_id=${runId}`),

  conversations: (sourceId: string) => request<Conversation[]>(`/conversations?video_source_id=${sourceId}`),
  conversation: (id: string) => request<ConversationDetail>(`/conversations/${id}`),
  createConversation: (video_source_id: string, language: string) =>
    request<Conversation>("/conversations", { method: "POST", body: json({ video_source_id, language }) }),
  ask: (conversationId: string, question: string, speak: boolean) =>
    request<AskResponse>(`/conversations/${conversationId}/messages`, {
      method: "POST",
      body: json({ question, speak }),
    }),
  askVoice: (conversationId: string, audio: Blob, speak: boolean) => {
    const form = new FormData();
    const ext = audio.type.includes("ogg") ? "ogg" : audio.type.includes("mp4") ? "m4a" : "webm";
    form.append("audio", audio, `question.${ext}`);
    form.append("speak", String(speak));
    return request<AskResponse>(`/conversations/${conversationId}/voice`, { method: "POST", body: form });
  },
};

export function formatTime(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

export function playBase64Audio(audio: { mime_type: string; base64: string }): HTMLAudioElement {
  const el = new Audio(`data:${audio.mime_type};base64,${audio.base64}`);
  void el.play().catch(() => undefined);
  return el;
}
