"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { formatTime, playBase64Audio } from "@/lib/api";
import { providerLabel } from "@/lib/findings";
import type { Audio, Conversation, Message, PublicConfig, VideoSource } from "@/lib/types";
import { useRecorder } from "@/lib/useRecorder";
import { AlertIcon, CameraIcon, ChevronDownIcon, EyeIcon, HistoryIcon, MicIcon, PlayIcon, PlusIcon, SendIcon, SpeakerIcon, StopIcon } from "./icons";

export type PendingState = null | { kind: "text" | "voice"; phase: "transcribing" | "analyzing" };

type Props = {
  source: VideoSource | null;
  messages: Message[];
  pending: PendingState;
  error: string | null;
  config: PublicConfig | null;
  language: string;
  audioByMessage: Record<string, Audio>;
  voiceNote: string | null;
  suggestions: string[];
  history: Conversation[];
  conversationId: string | null;
  currentTime: number;
  onLanguageChange: (lang: string) => void;
  onAsk: (question: string) => void;
  onVoice: (audio: Blob) => void;
  onSeek: (seconds: number) => void;
  onNewConversation: () => void;
  onOpenConversation: (id: string) => void;
};

const LANGS = [
  { id: "rw", label: "Kinyarwanda", short: "RW" },
  { id: "en", label: "English", short: "EN" },
];

/** The conversational core: ask a camera by voice or text; answers point back to moments in the video. */
export function ChatPanel(props: Props) {
  const { source, messages, pending, error, config, language, audioByMessage, voiceNote, suggestions } = props;
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const recorder = useRecorder();
  const disabled = !source || pending !== null;
  const rw = language === "rw";

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length, pending]);

  function send(text = draft) {
    const q = text.trim();
    if (!q || disabled) return;
    props.onAsk(q);
    setDraft("");
  }

  async function toggleMic() {
    if (recorder.recording) {
      const blob = await recorder.stop();
      if (blob && blob.size > 0) props.onVoice(blob);
    } else if (!disabled) {
      await recorder.start();
    }
  }

  const momentQuestion =
    props.currentTime >= 1 ? (rw ? `Ni iki kiri kuba ku munota ${formatTime(props.currentTime)}?` : `What is happening at ${formatTime(props.currentTime)}?`) : null;

  return (
    <section aria-label="Camera assistant" className="flex h-full min-h-0 flex-col bg-panel">
      <header className="flex items-center gap-2 border-b border-line px-5 py-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold leading-tight">{rw ? "Baza kamera" : "Ask your camera"}</h2>
          <p className="mt-0.5 flex items-center gap-1.5 truncate text-xs text-muted">
            <CameraIcon width={12} height={12} className="shrink-0" aria-hidden />
            <span className="truncate">{source ? source.name : "No camera selected"}</span>
          </p>
        </div>
        <div className="flex rounded-lg bg-bg p-0.5 text-xs font-medium" role="radiogroup" aria-label="Answer language">
          {LANGS.map((l) => (
            <button
              key={l.id}
              role="radio"
              aria-checked={language === l.id}
              onClick={() => props.onLanguageChange(l.id)}
              title={l.label}
              className={`min-h-8 cursor-pointer rounded-md px-2.5 transition ${language === l.id ? "bg-panel-2 text-text" : "text-muted hover:text-text"}`}
            >
              <span className="hidden 2xl:inline">{l.label}</span>
              <span className="2xl:hidden">{l.short}</span>
            </button>
          ))}
        </div>
        <HistoryMenu history={props.history} currentId={props.conversationId} onOpen={props.onOpenConversation} />
        <button
          onClick={props.onNewConversation}
          disabled={!source || messages.length === 0}
          className="grid h-9 w-9 cursor-pointer place-items-center rounded-lg text-muted transition hover:bg-panel-2 hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
          aria-label="New conversation"
          title="New conversation"
        >
          <PlusIcon width={16} height={16} aria-hidden />
        </button>
      </header>

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-5 py-6" aria-live="polite">
        {messages.length === 0 && !pending ? (
          <Welcome source={source} rw={rw} suggestions={suggestions} onMic={toggleMic} onAsk={send} disabled={disabled} />
        ) : (
          <div className="mx-auto max-w-2xl space-y-6">
            {messages.map((m) =>
              m.role === "user" ? (
                <UserBubble key={m.id} message={m} />
              ) : (
                <AssistantMessage key={m.id} message={m} source={source} audio={audioByMessage[m.id]} onSeek={props.onSeek} rw={rw} />
              ),
            )}
            {pending && <Thinking phase={pending.phase} rw={rw} />}
          </div>
        )}
        {error && (
          <div role="alert" className="mx-auto mt-4 flex max-w-2xl gap-2 rounded-xl border border-danger/30 bg-danger/10 px-3 py-2.5 text-sm text-danger">
            <AlertIcon className="mt-0.5 shrink-0" width={16} height={16} aria-hidden />
            <span>{error}</span>
          </div>
        )}
      </div>

      <div className="border-t border-line p-4">
        {messages.length > 0 && !recorder.recording && (
          <div className="mb-2.5 flex gap-2 overflow-x-auto pb-0.5">
            {momentQuestion && (
              <Suggestion onClick={() => send(momentQuestion)} disabled={disabled}>
                <PlayIcon width={9} height={9} aria-hidden /> {rw ? `Baza ku ${formatTime(props.currentTime)}` : `Ask about ${formatTime(props.currentTime)}`}
              </Suggestion>
            )}
            {suggestions.slice(1, 3).map((s) => (
              <Suggestion key={s} onClick={() => send(s)} disabled={disabled}>
                {s}
              </Suggestion>
            ))}
          </div>
        )}
        {(voiceNote || recorder.error) && <p className="mb-2 text-xs text-warn">{recorder.error || voiceNote}</p>}
        {recorder.recording ? (
          <div className="flex items-center gap-3 rounded-2xl border border-danger/40 bg-danger/10 p-2 pl-4" role="status">
            <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-danger" aria-hidden />
            <span className="flex-1 text-sm">{rw ? "Ndakumva… vuga ikibazo cyawe" : "Listening… ask your question"}</span>
            <button onClick={toggleMic} className="recording inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-xl bg-danger px-4 text-sm font-semibold text-white">
              <StopIcon width={16} height={16} aria-hidden /> {rw ? "Ohereza" : "Send"}
            </button>
          </div>
        ) : (
          <div className="flex items-end gap-2">
            <button
              onClick={toggleMic}
              disabled={disabled}
              className="grid h-12 w-12 shrink-0 cursor-pointer place-items-center rounded-2xl bg-accent text-accent-ink transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40"
              aria-label={rw ? "Vuga ikibazo" : "Speak your question"}
              title={rw ? "Vuga ikibazo" : "Speak your question"}
            >
              <MicIcon width={20} height={20} aria-hidden />
            </button>
            <div className="flex min-h-12 flex-1 items-end rounded-2xl border border-line-2 bg-bg pr-1.5 focus-within:border-accent/60">
              <label htmlFor="ask-input" className="sr-only">
                {rw ? "Andika ikibazo" : "Type a question"}
              </label>
              <textarea
                id="ask-input"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
                rows={1}
                maxLength={2000}
                disabled={!source}
                placeholder={source ? (rw ? "Andika ikibazo cyawe…" : "Type your question…") : "Select a camera first"}
                className="max-h-36 min-h-12 flex-1 resize-none bg-transparent px-4 py-3.5 text-[15px] outline-none placeholder:text-faint"
              />
              <button
                onClick={() => send()}
                disabled={disabled || !draft.trim()}
                className="mb-2 grid h-8 w-8 cursor-pointer place-items-center rounded-lg bg-accent text-accent-ink transition hover:brightness-110 disabled:cursor-not-allowed disabled:bg-panel-2 disabled:text-faint"
                aria-label="Send"
              >
                <SendIcon width={15} height={15} aria-hidden />
              </button>
            </div>
          </div>
        )}
        {config && (config.stt_is_placeholder || config.tts_is_placeholder) && (
          <p className="mt-2 text-[11px] text-faint">
            {config.stt_is_placeholder && "Speech recognition is a development placeholder. "}
            {config.tts_is_placeholder && "Spoken replies are not set up yet; answers appear as text."}
          </p>
        )}
      </div>
    </section>
  );
}

function Welcome({ source, rw, suggestions, onMic, onAsk, disabled }: { source: VideoSource | null; rw: boolean; suggestions: string[]; onMic: () => void; onAsk: (q: string) => void; disabled: boolean }) {
  if (!source) {
    return (
      <div className="flex h-full flex-col items-center justify-center text-center">
        <CameraIcon width={28} height={28} className="text-faint" aria-hidden />
        <p className="mt-3 font-medium">Choose a camera to start</p>
        <p className="mt-1 max-w-xs text-sm text-muted">Pick one on the left, or add a camera link or a recording.</p>
      </div>
    );
  }
  return (
    <div className="flex h-full flex-col items-center justify-center text-center">
      <button
        onClick={onMic}
        disabled={disabled}
        className="grid h-24 w-24 cursor-pointer place-items-center rounded-full bg-accent/15 text-accent ring-1 ring-accent/30 transition hover:bg-accent hover:text-accent-ink disabled:cursor-not-allowed disabled:opacity-40"
        aria-label={rw ? "Kanda uvuge" : "Tap and speak"}
      >
        <MicIcon width={34} height={34} aria-hidden />
      </button>
      <p className="mt-4 text-lg font-semibold">{rw ? "Kanda uvuge, cyangwa wandike" : "Tap and speak, or type"}</p>
      <p className="mt-1 max-w-sm text-sm text-muted">
        {rw
          ? "Baza mu Kinyarwanda cyangwa mu Cyongereza ku byo iyi kamera yabonye. Ibisubizo bikwereka aho byabereye muri video."
          : "Ask in Kinyarwanda or English about anything this camera recorded. Answers show you where it happened in the video."}
      </p>
      <div className="mt-6 flex w-full max-w-md flex-col gap-2">
        {suggestions.map((s) => (
          <button
            key={s}
            onClick={() => onAsk(s)}
            disabled={disabled}
            className="min-h-11 cursor-pointer rounded-xl border border-line-2 px-4 text-left text-sm text-muted transition hover:border-accent/50 hover:bg-panel-2 hover:text-text"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

function Suggestion({ onClick, disabled, children }: { onClick: () => void; disabled: boolean; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="inline-flex min-h-8 shrink-0 cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-full border border-line-2 px-3 text-xs text-muted transition hover:border-accent/50 hover:text-text disabled:opacity-40"
    >
      {children}
    </button>
  );
}

function HistoryMenu({ history, currentId, onOpen }: { history: Conversation[]; currentId: string | null; onOpen: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-label="Past conversations"
        title="Past conversations"
        disabled={history.length === 0}
        className="grid h-9 w-9 cursor-pointer place-items-center rounded-lg text-muted transition hover:bg-panel-2 hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
      >
        <HistoryIcon width={16} height={16} aria-hidden />
      </button>
      {open && (
        <ul className="absolute right-0 top-11 z-40 max-h-80 w-72 overflow-y-auto rounded-xl border border-line-2 bg-panel p-1.5 shadow-2xl">
          <li className="px-3 pb-1 pt-1.5 text-[11px] text-faint">Past conversations about this camera</li>
          {history.map((c) => (
            <li key={c.id}>
              <button
                onClick={() => {
                  onOpen(c.id);
                  setOpen(false);
                }}
                aria-current={c.id === currentId || undefined}
                className={`flex w-full cursor-pointer flex-col rounded-lg px-3 py-2 text-left hover:bg-panel-2 ${c.id === currentId ? "bg-panel-2" : ""}`}
              >
                <span className="truncate text-sm">{c.title || "Untitled conversation"}</span>
                <span className="text-[11px] text-faint">
                  {new Date(c.updated_at).toLocaleString()} · {c.language === "rw" ? "Kinyarwanda" : "English"}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function UserBubble({ message }: { message: Message }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%]">
        <div className={`rounded-2xl rounded-br-md px-4 py-2.5 text-[15px] ${message.metadata.failed ? "bg-panel-2 text-muted line-through decoration-danger/50" : "bg-accent/15 text-text"}`}>
          {message.content}
        </div>
        {message.input_mode === "voice" && (
          <p className="mt-1 flex items-center justify-end gap-1 text-[11px] text-faint">
            <MicIcon width={11} height={11} aria-hidden /> spoken
          </p>
        )}
      </div>
    </div>
  );
}

function certainty(c: number, rw: boolean) {
  if (c >= 0.7) return { label: rw ? "Byizewe" : "Confident", cls: "bg-accent" };
  if (c >= 0.4) return { label: rw ? "Hafi kwizerwa" : "Fairly sure", cls: "bg-warn" };
  return { label: rw ? "Ntibyizewe neza" : "Not sure", cls: "bg-danger" };
}

function AssistantMessage({ message, source, audio, onSeek, rw }: { message: Message; source: VideoSource | null; audio?: Audio; onSeek: (s: number) => void; rw: boolean }) {
  const [how, setHow] = useState(false);
  const isMock = message.metadata.analyzer === "mock";
  const sure = certainty(message.confidence ?? 0, rw);
  const first = message.timestamps[0];

  return (
    <article className="flex gap-3">
      <div className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-accent/15 text-accent" aria-hidden>
        <EyeIcon width={15} height={15} />
      </div>
      <div className="min-w-0 flex-1 space-y-3">
        <p className="text-[15px] leading-relaxed">{message.content}</p>

        {source && message.timestamps.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-bg/50 p-2.5">
            <span className="inline-flex items-center gap-1.5 text-xs text-muted">
              <CameraIcon width={12} height={12} aria-hidden />
              <span className="max-w-[180px] truncate">{source.name}</span>
            </span>
            {message.timestamps.map((t, i) => (
              <button
                key={i}
                onClick={() => onSeek(t.start_seconds)}
                className="inline-flex min-h-7 cursor-pointer items-center gap-1.5 rounded-md border border-line-2 bg-panel px-2 font-mono text-[11px] text-muted transition hover:border-accent/50 hover:text-accent"
                title={t.label ?? "Jump to this moment"}
              >
                <PlayIcon width={9} height={9} aria-hidden />
                {formatTime(t.start_seconds)}
                {t.end_seconds != null && t.end_seconds > t.start_seconds && `–${formatTime(t.end_seconds)}`}
              </button>
            ))}
            {first && (
              <button
                onClick={() => onSeek(first.start_seconds)}
                className="ml-auto inline-flex min-h-8 cursor-pointer items-center gap-1.5 rounded-lg bg-accent px-3 text-xs font-semibold text-accent-ink transition hover:brightness-110"
              >
                <PlayIcon width={10} height={10} aria-hidden /> {rw ? "Reba aho byabereye" : "Play clip"}
              </button>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
          {source && message.timestamps.length === 0 && (
            <span className="inline-flex items-center gap-1.5">
              <CameraIcon width={12} height={12} aria-hidden />
              <span className="max-w-[160px] truncate">{source.name}</span>
            </span>
          )}
          {!isMock && (
            <span className="inline-flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-full ${sure.cls}`} aria-hidden />
              {sure.label}
            </span>
          )}
          {message.metadata.insufficient_evidence && <span className="text-warn">{rw ? "Ntibigaragara neza muri video" : "Not clearly visible in the video"}</span>}
          {isMock && <span className="text-warn">mock analyzer</span>}
          {audio && (
            <button onClick={() => playBase64Audio(audio)} className="inline-flex min-h-7 cursor-pointer items-center gap-1 hover:text-text">
              <SpeakerIcon width={13} height={13} aria-hidden /> {rw ? "Umva" : "Listen"}
            </button>
          )}
          <button onClick={() => setHow(!how)} aria-expanded={how} className="inline-flex min-h-7 cursor-pointer items-center gap-1 hover:text-text">
            {rw ? "Nabimenye nte?" : "How I know"} <ChevronDownIcon width={12} height={12} className={`transition ${how ? "rotate-180" : ""}`} aria-hidden />
          </button>
        </div>

        {how && (
          <div className="space-y-2 rounded-xl border border-line bg-bg/40 p-3 text-xs text-muted">
            <Provenance message={message} />
            {message.evidence.length > 0 ? (
              <ul className="space-y-1.5">
                {message.evidence.map((e, i) => (
                  <li key={i} className="flex gap-2">
                    {e.timestamp_seconds != null && (
                      <button onClick={() => onSeek(e.timestamp_seconds!)} className="shrink-0 cursor-pointer font-mono text-accent hover:underline">
                        {formatTime(e.timestamp_seconds)}
                      </button>
                    )}
                    <span>
                      {e.level && <span className="mr-1.5 text-faint">{LEVEL_LABEL[e.level] ?? e.level}:</span>}
                      {e.description}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-faint">No separate evidence items were recorded for this answer.</p>
            )}
          </div>
        )}
      </div>
    </article>
  );
}

const LEVEL_LABEL: Record<string, string> = {
  detection: "seen by the detector",
  tracking: "followed over time",
  rule: "event rule",
  model_interpretation: "video AI opinion",
};

/** Where the answer came from, in plain words. */
function Provenance({ message }: { message: Message }) {
  const tools = message.metadata.tools_used;
  if (!tools) return null;
  const parts: string[] = [];
  if (tools.some((t) => t !== "analyze_video_clip")) parts.push("counted from what the detector saw and tracked");
  if (message.metadata.escalated) {
    const who = message.metadata.understanding_providers?.map((p) => providerLabel(p.split(":")[0])).join(", ");
    parts.push(`the video AI looked at the footage${who ? ` (${who})` : ""}`);
  }
  if (message.metadata.agent_llm === "local") parts.push("answered on this computer, nothing sent to the cloud");
  return <p>{parts.length ? `This answer was ${parts.join("; ")}.` : "No tools were used for this answer."}</p>;
}

function Thinking({ phase, rw }: { phase: "transcribing" | "analyzing"; rw: boolean }) {
  return (
    <div className="flex gap-3" role="status">
      <div className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-accent/15 text-accent" aria-hidden>
        <EyeIcon width={15} height={15} />
      </div>
      <div className="flex items-center gap-2.5 text-sm text-muted">
        <span className="flex gap-1" aria-hidden>
          {[0, 1, 2].map((i) => (
            <span key={i} className="typing-dot h-1.5 w-1.5 rounded-full bg-accent" style={{ animationDelay: `${i * 0.15}s` }} />
          ))}
        </span>
        {phase === "transcribing" ? (rw ? "Ndumva ibyo wavuze…" : "Listening to your question…") : rw ? "Ndimo kureba video…" : "Watching the video…"}
      </div>
    </div>
  );
}
