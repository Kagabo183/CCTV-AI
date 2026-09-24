"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, formatTime, playBase64Audio } from "@/lib/api";
import { computeFindings, ROUTINE_EVENTS, suggestedQuestions } from "@/lib/findings";
import type { Audio, AskResponse, BoxTrack, Conversation, Message, PublicConfig, Track, User, VideoEvent, VideoSession, VideoSource, VisionRun } from "@/lib/types";
import { AiDetails } from "./AiDetails";
import { ChatPanel, type PendingState } from "./ChatPanel";
import { Insights } from "./Insights";
import { MicIcon, XIcon } from "./icons";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";
import { SessionPill, VideoStage, type VideoStageHandle } from "./VideoStage";

const LAST_SOURCE_KEY = "visionary:last-source";
const AI_DETAILS_KEY = "visionary:ai-details";

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

/**
 * Layout: top bar (brand, search, AI details, account) · cameras · the selected camera
 * (video, what it saw, what happened) · the assistant, which gets the most room after the video.
 */
export function Workspace({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [sources, setSources] = useState<VideoSource[]>([]);
  const [source, setSource] = useState<VideoSource | null>(null);
  const [session, setSession] = useState<VideoSession | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [history, setHistory] = useState<Conversation[]>([]);
  const [pending, setPending] = useState<PendingState>(null);
  const [error, setError] = useState<string | null>(null);
  const [language, setLanguage] = useState(user.preferred_language || "rw");
  const [audioByMessage, setAudioByMessage] = useState<Record<string, Audio>>({});
  const [voiceNote, setVoiceNote] = useState<string | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const [runs, setRuns] = useState<VisionRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [boxes, setBoxes] = useState<Record<string, BoxTrack>>({});
  const [showBoxes, setShowBoxes] = useState(true);
  const [events, setEvents] = useState<VideoEvent[]>([]);
  const [tracks, setTracks] = useState<Track[]>([]);
  const [selectedTrackId, setSelectedTrackId] = useState<number | null>(null);
  const [currentTime, setCurrentTime] = useState(0);
  // "AI details" (developer view) is remembered per browser. The workspace only renders client-side, after sign-in.
  const [aiDetails, setAiDetailsState] = useState(() => {
    try {
      return typeof window !== "undefined" && localStorage.getItem(AI_DETAILS_KEY) === "1";
    } catch {
      return false;
    }
  });
  const stageRef = useRef<VideoStageHandle>(null);
  const selectedRef = useRef<VideoSource | null>(null);
  useEffect(() => {
    selectedRef.current = source;
  }, [source]);

  function setAiDetails(v: boolean) {
    setAiDetailsState(v);
    try {
      localStorage.setItem(AI_DETAILS_KEY, v ? "1" : "0");
    } catch {}
  }

  const selectSource = useCallback(async (s: VideoSource) => {
    setSource(s);
    setNavOpen(false);
    setError(null);
    setVoiceNote(null);
    setMessages([]);
    setHistory([]);
    setConversationId(null);
    setSession(null);
    setRuns([]);
    setSelectedRunId(null);
    setEvents([]);
    setSelectedTrackId(null);
    setCurrentTime(0);
    try {
      localStorage.setItem(LAST_SOURCE_KEY, s.id);
    } catch {}
    if (s.status !== "ready") return;
    api.openSource(s.id)
      .then((sess) => {
        setSession(sess);
        return api.visionRuns(s.id).then(setRuns);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not open video"));
    try {
      const list = await api.conversations(s.id);
      setHistory(list);
      const [latest] = list;
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

  // Poll sources while any import is in progress; open the selected one when it finishes.
  const importing = sources.some((s) => s.status === "importing");
  useEffect(() => {
    if (!importing) return;
    const id = setInterval(() => {
      api
        .sources()
        .then((list) => {
          setSources(list);
          const current = selectedRef.current;
          const fresh = current ? list.find((s) => s.id === current.id) : undefined;
          if (!current || !fresh) return;
          if (current.status === "importing" && fresh.status === "ready") void selectSource(fresh);
          else setSource(fresh);
        })
        .catch(() => undefined);
    }, 1500);
    return () => clearInterval(id);
  }, [importing, selectSource]);

  function reimport(id: string) {
    api
      .importSource(id)
      .then((updated) => {
        setSources((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
        setSource(updated);
        setRuns([]);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not import this video"));
  }

  // Poll while any local run is queued/running.
  const runsBusy = runs.some((r) => r.status === "queued" || r.status === "running");
  useEffect(() => {
    if (!source || !runsBusy) return;
    const id = setInterval(() => {
      api.visionRuns(source.id).then(setRuns).catch(() => undefined);
    }, 1500);
    return () => clearInterval(id);
  }, [source, runsBusy]);

  // Default selection: the assistant's (primary) run, else the first completed one.
  const completedRuns = runs.filter((r) => r.status === "completed" && r.id);
  const activeRunId =
    selectedRunId && completedRuns.some((r) => r.id === selectedRunId)
      ? selectedRunId
      : (completedRuns.find((r) => r.is_primary) ?? completedRuns[0])?.id ?? null;
  const activeRun = completedRuns.find((r) => r.id === activeRunId) ?? null;

  // Events, tracks and boxes for the selected run.
  useEffect(() => {
    if (!source || !activeRunId) return;
    api.events(source.id, activeRunId).then(setEvents).catch(() => undefined);
    api.tracks(source.id, activeRunId).then(setTracks).catch(() => undefined);
    if (!boxes[activeRunId]) {
      api.boxes(source.id, activeRunId)
        .then((b) => setBoxes((prev) => ({ ...prev, [activeRunId]: b })))
        .catch(() => undefined);
    }
  }, [source, activeRunId, boxes]);

  const activeBoxes = activeRunId ? boxes[activeRunId] : undefined;
  const findings = useMemo(() => (activeBoxes ? computeFindings(activeBoxes, tracks) : null), [activeBoxes, tracks]);
  const notableEvents = useMemo(() => events.filter((e) => !ROUTINE_EVENTS.has(e.event_type)), [events]);
  const suggestions = useMemo(() => suggestedQuestions(findings, language), [findings, language]);

  async function ensureConversation(): Promise<string> {
    if (conversationId) return conversationId;
    const conv = await api.createConversation(source!.id, language);
    setConversationId(conv.id);
    return conv.id;
  }

  async function openConversation(id: string) {
    try {
      const detail = await api.conversation(id);
      if (detail.video_source_id && detail.video_source_id !== source?.id) {
        const other = sources.find((x) => x.id === detail.video_source_id);
        if (other) await selectSource(other);
      }
      setConversationId(detail.id);
      setLanguage(detail.language);
      setMessages(detail.messages);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not open that conversation");
    }
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
    // Refresh session state (the first question may have prepared the video) and the history list.
    const sid = res.switched_to_source?.id ?? source?.id;
    if (sid) {
      api.openSource(sid).then(setSession).catch(() => undefined);
      api.conversations(sid).then(setHistory).catch(() => undefined);
    }
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
    const temp = tempMessage("…", "voice", source.id);
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
        setVoiceNote("Spoken Kinyarwanda replies are not set up yet. The answer is shown as text.");
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

  function runVision(detector: string, tracker: string) {
    if (!source) return;
    api
      .runVision(source.id, detector, tracker)
      .then(() => api.visionRuns(source.id))
      .then(setRuns)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not start analysis"));
  }

  const seek = (s: number) => stageRef.current?.seek(s);
  const duration = typeof source?.metadata.duration_seconds === "number" ? (source.metadata.duration_seconds as number) : null;

  const sidebar = (
    <Sidebar
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
          setRuns([]);
          setEvents([]);
          setTracks([]);
          newConversation();
        }
      }}
    />
  );

  return (
    <div className="flex h-full flex-col">
      <TopBar
        user={user}
        sources={sources}
        history={history}
        currentSource={source}
        aiDetails={aiDetails}
        onAiDetails={setAiDetails}
        onSelectSource={selectSource}
        onOpenConversation={openConversation}
        onMenu={() => setNavOpen(true)}
        onLogout={onLogout}
      />

      <div className="min-h-0 flex-1 overflow-y-auto lg:grid lg:grid-cols-[224px_minmax(0,1fr)_minmax(380px,40%)] lg:overflow-hidden xl:grid-cols-[248px_minmax(0,1fr)_minmax(440px,36%)] 2xl:grid-cols-[264px_minmax(0,1fr)_minmax(520px,38%)]">
        <div className="hidden min-h-0 border-r border-line lg:block">{sidebar}</div>

        {navOpen && (
          <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Cameras">
            <div className="absolute inset-0 bg-black/60" onClick={() => setNavOpen(false)} />
            <div className="relative h-full w-[288px]">
              {sidebar}
              <button onClick={() => setNavOpen(false)} className="absolute right-2 top-2 grid h-10 w-10 place-items-center text-muted" aria-label="Close camera list">
                <XIcon />
              </button>
            </div>
          </div>
        )}

        <main className="min-h-0 lg:overflow-y-auto">
          <div className="mx-auto max-w-5xl space-y-6 p-4 lg:p-6">
            {source && (
              <div className="flex flex-wrap items-end justify-between gap-3">
                <div className="min-w-0">
                  <h1 className="truncate text-2xl font-semibold tracking-tight">{source.name}</h1>
                  <p className="mt-0.5 text-sm text-muted">
                    {[source.location, source.kind === "upload" ? "Recording" : source.metadata.delivery === "youtube" ? "Online video · watched in place, not downloaded" : "Camera", duration != null ? `${formatTime(duration)} long` : null].filter(Boolean).join(" · ")}
                  </p>
                </div>
                <SessionPill session={session} />
              </div>
            )}

            <VideoStage
              key={source?.id ?? "none"}
              ref={stageRef}
              source={source}
              session={session}
              analyzerIsMock={config?.analyzer_is_mock ?? false}
              overlay={showBoxes && activeRun && activeBoxes ? { boxes: activeBoxes, label: activeRun.label ?? "", highlightTrack: selectedTrackId } : null}
              boxesAvailable={Boolean(activeRun)}
              showBoxes={showBoxes}
              onToggleBoxes={setShowBoxes}
              technical={aiDetails}
              onTime={setCurrentTime}
              onRetryImport={source ? () => reimport(source.id) : undefined}
            />

            {source && runs.length > 0 && (
              <Insights
                findings={findings}
                notableEvents={notableEvents}
                runs={runs}
                onSeek={seek}
                onAnalyse={() => runVision("yolo", "bytetrack")}
                onImport={() => reimport(source.id)}
                onAskAbout={ask}
              />
            )}

            {source &&
              runs.length > 0 &&
              (aiDetails ? (
                <AiDetails
                  sourceId={source.id}
                  runs={runs}
                  selectedRunId={activeRunId}
                  events={activeRunId ? events : []}
                  tracks={activeRunId ? tracks : []}
                  selectedTrackId={selectedTrackId}
                  open
                  onOpenChange={(v) => {
                    if (!v) setAiDetails(false);
                  }}
                  onSelectRun={setSelectedRunId}
                  onSelectTrack={setSelectedTrackId}
                  onRun={runVision}
                  onSeek={seek}
                  onImport={() => reimport(source.id)}
                  onCancel={(runId) =>
                    api
                      .cancelRun(source.id, runId)
                      .then(() => api.visionRuns(source.id))
                      .then(setRuns)
                      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not cancel"))
                  }
                />
              ) : (
                <button onClick={() => setAiDetails(true)} className="min-h-10 cursor-pointer text-sm text-muted underline-offset-4 hover:text-text hover:underline">
                  Show AI details: models, confidence scores and every tracked object
                </button>
              ))}
          </div>
        </main>

        {source && (
          <a
            href="#assistant"
            className="fixed bottom-4 right-4 z-30 inline-flex min-h-12 items-center gap-2 rounded-full bg-accent px-5 text-sm font-semibold text-accent-ink shadow-2xl lg:hidden"
          >
            <MicIcon width={18} height={18} aria-hidden /> {language === "rw" ? "Baza kamera" : "Ask this camera"}
          </a>
        )}

        <aside id="assistant" className="h-[100dvh] min-h-0 scroll-mt-14 border-t border-line lg:h-auto lg:border-l lg:border-t-0">
          <ChatPanel
            source={source}
            messages={messages}
            pending={pending}
            error={error}
            config={config}
            language={language}
            audioByMessage={audioByMessage}
            voiceNote={voiceNote}
            suggestions={suggestions}
            history={history}
            conversationId={conversationId}
            currentTime={currentTime}
            onLanguageChange={changeLanguage}
            onAsk={ask}
            onVoice={askVoice}
            onSeek={seek}
            onNewConversation={newConversation}
            onOpenConversation={openConversation}
          />
        </aside>
      </div>
    </div>
  );
}
