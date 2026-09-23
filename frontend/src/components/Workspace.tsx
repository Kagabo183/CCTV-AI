"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, playBase64Audio } from "@/lib/api";
import type { Audio, AskResponse, Message, PublicConfig, User, VideoSession, VideoSource } from "@/lib/types";
import { ChatPanel, type PendingState } from "./ChatPanel";
import { CameraIcon, XIcon } from "./icons";
import { Sidebar } from "./Sidebar";
import { VideoStage, type VideoStageHandle } from "./VideoStage";

const LAST_SOURCE_KEY = "visionary:last-source";

function tempMessage(content: string, input_mode: "text" | "voice", sourceId: string): Message {
  return {
    id: `temp-${Date.now()}`,
    role: "user",
    content,
    language: null,
    input_mode,
    video_source_id: sourceId,
    confidence: null,
    timestamps: [],
    evidence: [],
    metadata: {},
    created_at: new Date().toISOString(),
  };
}

export function Workspace({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [sources, setSources] = useState<VideoSource[]>([]);
  const [source, setSource] = useState<VideoSource | null>(null);
  const [session, setSession] = useState<VideoSession | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState<PendingState>(null);
  const [error, setError] = useState<string | null>(null);
  const [language, setLanguage] = useState(user.preferred_language || "rw");
  const [audioByMessage, setAudioByMessage] = useState<Record<string, Audio>>({});
  const [voiceNote, setVoiceNote] = useState<string | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const stageRef = useRef<VideoStageHandle>(null);

  const selectSource = useCallback(async (s: VideoSource) => {
    setSource(s);
    setNavOpen(false);
    setError(null);
    setVoiceNote(null);
    setMessages([]);
    setConversationId(null);
    setSession(null);
    try {
      localStorage.setItem(LAST_SOURCE_KEY, s.id);
    } catch {}
    if (s.status !== "ready") return;
    api.openSource(s.id).then(setSession).catch((e) => setError(e instanceof ApiError ? e.message : "Could not open video"));
    try {
      const [latest] = await api.conversations(s.id);
      if (latest) {
        const detail = await api.conversation(latest.id);
        setConversationId(detail.id);
        setLanguage(detail.language);
        setMessages(detail.messages);
      }
    } catch {}
  }, []);

  useEffect(() => {
    api.config().then(setConfig).catch(() => undefined);
    api.sources().then((list) => {
      setSources(list);
      let last: string | null = null;
      try {
        last = localStorage.getItem(LAST_SOURCE_KEY);
      } catch {}
      const initial = list.find((s) => s.id === last) ?? list[0];
      if (initial) void selectSource(initial);
    });
  }, [selectSource]);

  // Poll while the backend prepares the video (e.g. uploading to the analyzer).
  useEffect(() => {
    if (!session || !source || (session.status !== "preparing" && session.status !== "expired")) return;
    const id = setInterval(() => {
      api.session(source.id, session.id).then(setSession).catch(() => undefined);
    }, 2000);
    return () => clearInterval(id);
  }, [session, source]);

  async function ensureConversation(): Promise<string> {
    if (conversationId) return conversationId;
    const conv = await api.createConversation(source!.id, language);
    setConversationId(conv.id);
    return conv.id;
  }

  function applyResponse(tempId: string, res: AskResponse) {
    setMessages((prev) => [...prev.filter((m) => m.id !== tempId), res.user_message, res.assistant_message]);
    if (res.audio) {
      setAudioByMessage((prev) => ({ ...prev, [res.assistant_message.id]: res.audio! }));
      playBase64Audio(res.audio);
    }
    if (res.switched_to_source) {
      const next = res.switched_to_source;
      setSources((prev) => (prev.some((s) => s.id === next.id) ? prev : [next, ...prev]));
      setSource(next);
    }
    // Refresh session state (the first question may have prepared the video).
    if (source) api.openSource(res.switched_to_source?.id ?? source.id).then(setSession).catch(() => undefined);
  }

  function fail(tempId: string, e: unknown) {
    setMessages((prev) => prev.map((m) => (m.id === tempId ? { ...m, metadata: { failed: true } } : m)));
    setError(e instanceof ApiError ? e.message : "Something went wrong. Please try again.");
  }

  async function ask(question: string) {
    if (!source) return;
    setError(null);
    const temp = tempMessage(question, "text", source.id);
    setMessages((prev) => [...prev, temp]);
    setPending({ kind: "text", phase: "analyzing" });
    try {
      const id = await ensureConversation();
      applyResponse(temp.id, await api.ask(id, question, !config?.tts_is_placeholder));
    } catch (e) {
      fail(temp.id, e);
    } finally {
      setPending(null);
    }
  }

  async function askVoice(audio: Blob) {
    if (!source) return;
    setError(null);
    setVoiceNote(null);
    const temp = tempMessage("🎙️ …", "voice", source.id);
    setMessages((prev) => [...prev, temp]);
    setPending({ kind: "voice", phase: "transcribing" });
    const phaseTimer = setTimeout(() => setPending({ kind: "voice", phase: "analyzing" }), 1500);
    try {
      const id = await ensureConversation();
      const res = await api.askVoice(id, audio, true);
      applyResponse(temp.id, res);
      if (res.transcript?.is_placeholder) {
        setVoiceNote("Speech recognition is a development placeholder: your recording was replaced with a sample question.");
      } else if (!res.tts_available) {
        setVoiceNote("Kinyarwanda voice replies are not configured yet. The answer is shown as text.");
      }
    } catch (e) {
      fail(temp.id, e);
    } finally {
      clearTimeout(phaseTimer);
      setPending(null);
    }
  }

  function newConversation() {
    setConversationId(null);
    setMessages([]);
    setError(null);
  }

  function changeLanguage(lang: string) {
    if (lang === language) return;
    setLanguage(lang);
    if (messages.length) newConversation(); // language is fixed per conversation
  }

  const sidebar = (
    <Sidebar
      user={user}
      sources={sources}
      selectedId={source?.id ?? null}
      sourceKinds={config?.source_kinds ?? ["url"]}
      onSelect={selectSource}
      onAdded={(s) => {
        setSources((prev) => [s, ...prev]);
        void selectSource(s);
      }}
      onDeleted={(id) => {
        setSources((prev) => prev.filter((s) => s.id !== id));
        if (source?.id === id) {
          setSource(null);
          setSession(null);
          newConversation();
        }
      }}
      onLogout={onLogout}
    />
  );

  return (
    <div className="flex h-full flex-col lg:grid lg:grid-cols-[272px_minmax(0,1fr)_minmax(360px,420px)]">
      <div className="hidden min-h-0 lg:block">{sidebar}</div>

      {navOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-black/60" onClick={() => setNavOpen(false)} />
          <div className="relative h-full w-[280px]">
            {sidebar}
            <button onClick={() => setNavOpen(false)} className="absolute right-3 top-4 text-faint">
              <XIcon />
            </button>
          </div>
        </div>
      )}

      <main className="min-h-0 overflow-y-auto">
        <div className="flex items-center gap-3 border-b border-line px-4 py-3 lg:hidden">
          <button onClick={() => setNavOpen(true)} className="flex items-center gap-2 text-sm font-medium">
            <CameraIcon width={16} height={16} className="text-accent" />
            {source?.name ?? "Select camera"}
          </button>
        </div>
        <div className="mx-auto max-w-5xl space-y-5 p-4 lg:p-8">
          <div className="hidden items-end justify-between lg:flex">
            <div>
              <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-faint">
                {source ? (source.kind === "upload" ? "Uploaded video" : source.kind === "local" ? "Local video" : "Video source") : "Workspace"}
              </p>
              <h1 className="mt-1 text-xl font-semibold tracking-tight">{source?.name ?? "Talk to your cameras"}</h1>
              {source?.location && <p className="text-sm text-muted">Location: {source.location}</p>}
            </div>
          </div>
          <VideoStage key={source?.id ?? "none"} ref={stageRef} source={source} session={session} analyzerIsMock={config?.analyzer_is_mock ?? false} />
        </div>
      </main>

      <div className="h-[70vh] min-h-0 lg:h-full">
        <ChatPanel
          source={source}
          messages={messages}
          pending={pending}
          error={error}
          config={config}
          language={language}
          audioByMessage={audioByMessage}
          voiceNote={voiceNote}
          onLanguageChange={changeLanguage}
          onAsk={ask}
          onVoice={askVoice}
          onSeek={(s) => stageRef.current?.seek(s)}
          onNewConversation={newConversation}
        />
      </div>
    </div>
  );
}
