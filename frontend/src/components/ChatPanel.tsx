"use client";

import { useEffect, useRef, useState } from "react";
import { formatTime, playBase64Audio } from "@/lib/api";
import type { Audio, Message, PublicConfig, VideoSource } from "@/lib/types";
import { useRecorder } from "@/lib/useRecorder";
import { AlertIcon, ChatIcon, EyeIcon, MicIcon, PlayIcon, PlusIcon, SendIcon, SpeakerIcon, StopIcon } from "./icons";

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
  onLanguageChange: (lang: string) => void;
  onAsk: (question: string) => void;
  onVoice: (audio: Blob) => void;
  onSeek: (seconds: number) => void;
  onNewConversation: () => void;
};

const SUGGESTIONS: Record<string, string[]> = {
  rw: ["Ni iki kiri kuba kuri iyi video?", "Hari abantu bangahe?", "Hari imodoka ihagaze hafi y'irembo?"],
  en: ["What is happening in this video?", "How many people are there?", "Is there a vehicle near the gate?"],
};

export function ChatPanel(props: Props) {
  const { source, messages, pending, error, config, language, audioByMessage, voiceNote } = props;
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const recorder = useRecorder();
  const disabled = !source || pending !== null;

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

  return (
    <section className="flex h-full min-h-0 flex-col border-line bg-panel lg:border-l">
      <header className="flex items-center justify-between border-b border-line px-5 py-3.5">
        <div className="flex items-center gap-2.5">
          <div className="grid h-7 w-7 place-items-center rounded-md bg-accent/15 text-accent">
            <EyeIcon width={15} height={15} />
          </div>
          <div>
            <h2 className="text-sm font-semibold leading-tight">AI camera assistant</h2>
            <p className="text-xs text-faint">{source ? `Watching ${source.name}` : "No camera selected"}</p>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="flex rounded-md bg-bg p-0.5 text-[11px] font-medium">
            {(["rw", "en"] as const).map((l) => (
              <button
                key={l}
                onClick={() => props.onLanguageChange(l)}
                className={`rounded px-2 py-1 uppercase transition ${language === l ? "bg-panel-2 text-text" : "text-faint hover:text-muted"}`}
                title={l === "rw" ? "Kinyarwanda" : "English"}
              >
                {l}
              </button>
            ))}
          </div>
          <button
            onClick={props.onNewConversation}
            disabled={!source}
            className="grid h-7 w-7 place-items-center rounded-md text-muted transition hover:bg-panel-2 hover:text-text disabled:opacity-40"
            title="New conversation"
          >
            <PlusIcon width={15} height={15} />
          </button>
        </div>
      </header>

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-5">
        {messages.length === 0 && !pending && (
          <div className="flex h-full flex-col items-center justify-center text-center">
            <div className="grid h-11 w-11 place-items-center rounded-xl bg-panel-2 text-faint">
              <ChatIcon />
            </div>
            <p className="mt-3 text-sm font-medium">{source ? "Ask about this video" : "Select a camera to begin"}</p>
            <p className="mt-1 max-w-[260px] text-xs text-muted">
              Type or press the microphone and speak in Kinyarwanda. Answers are grounded in the selected video.
            </p>
            {source && (
              <div className="mt-5 flex w-full max-w-xs flex-col gap-2">
                {SUGGESTIONS[language]?.map((s) => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="rounded-lg border border-line-2 px-3 py-2 text-left text-sm text-muted transition hover:border-accent/40 hover:text-text"
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {messages.map((m) =>
          m.role === "user" ? (
            <UserBubble key={m.id} message={m} />
          ) : (
            <AssistantMessage key={m.id} message={m} audio={audioByMessage[m.id]} onSeek={props.onSeek} />
          ),
        )}

        {pending && <Thinking phase={pending.phase} />}
        {error && (
          <div className="flex gap-2 rounded-lg border border-danger/30 bg-danger/10 px-3 py-2.5 text-sm text-danger">
            <AlertIcon className="mt-0.5 shrink-0" width={16} height={16} />
            <span>{error}</span>
          </div>
        )}
      </div>

      <div className="border-t border-line p-4">
        {(voiceNote || recorder.error) && <p className="mb-2 text-xs text-warn">{recorder.error || voiceNote}</p>}
        <div className="flex items-end gap-2">
          <button
            onClick={toggleMic}
            disabled={!source || (pending !== null && !recorder.recording)}
            className={`grid h-11 w-11 shrink-0 place-items-center rounded-xl transition disabled:opacity-40 ${
              recorder.recording ? "recording bg-danger text-white" : "bg-accent text-accent-ink hover:brightness-110"
            }`}
            title={recorder.recording ? "Stop and send" : "Speak (Kinyarwanda)"}
          >
            {recorder.recording ? <StopIcon /> : <MicIcon />}
          </button>
          <div className="flex min-h-11 flex-1 items-end rounded-xl border border-line-2 bg-bg pr-1.5 focus-within:border-accent/50">
            <textarea
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
              placeholder={recorder.recording ? "Listening…" : source ? "Andika ikibazo… / Type a question…" : "Select a camera first"}
              className="max-h-32 min-h-11 flex-1 resize-none bg-transparent px-3.5 py-3 text-sm outline-none placeholder:text-faint"
            />
            <button
              onClick={() => send()}
              disabled={disabled || !draft.trim()}
              className="mb-1.5 grid h-8 w-8 place-items-center rounded-lg bg-panel-2 text-text transition hover:bg-line-2 disabled:opacity-30"
              title="Send"
            >
              <SendIcon width={16} height={16} />
            </button>
          </div>
        </div>
        {config && (config.stt_is_placeholder || config.tts_is_placeholder) && (
          <p className="mt-2 text-[11px] text-faint">
            {config.stt_is_placeholder && "Speech recognition: development placeholder. "}
            {config.tts_is_placeholder && "Kinyarwanda voice replies: not configured yet."}
          </p>
        )}
      </div>
    </section>
  );
}

function UserBubble({ message }: { message: Message }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%]">
        <div
          className={`rounded-2xl rounded-br-md px-3.5 py-2.5 text-sm ${
            message.metadata.failed ? "bg-panel-2 text-muted line-through decoration-danger/50" : "bg-panel-2"
          }`}
        >
          {message.content}
        </div>
        {message.input_mode === "voice" && (
          <p className="mt-1 flex items-center justify-end gap-1 text-[11px] text-faint">
            <MicIcon width={11} height={11} /> spoken
          </p>
        )}
      </div>
    </div>
  );
}

function AssistantMessage({ message, audio, onSeek }: { message: Message; audio?: Audio; onSeek: (s: number) => void }) {
  const [showEvidence, setShowEvidence] = useState(false);
  const isMock = message.metadata.analyzer === "mock";
  const confidence = message.confidence ?? 0;

  return (
    <div className="flex gap-3">
      <div className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-md bg-accent/15 text-accent">
        <EyeIcon width={14} height={14} />
      </div>
      <div className="min-w-0 flex-1 space-y-2.5">
        <p className="text-sm leading-relaxed">{message.content}</p>

        {message.timestamps.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {message.timestamps.map((t, i) => (
              <button
                key={i}
                onClick={() => onSeek(t.start_seconds)}
                className="group inline-flex items-center gap-1.5 rounded-md border border-line-2 bg-bg px-2 py-1 font-mono text-[11px] text-muted transition hover:border-accent/50 hover:text-accent"
                title="Jump to this moment"
              >
                <PlayIcon width={10} height={10} />
                {formatTime(t.start_seconds)}
                {t.end_seconds != null && t.end_seconds > t.start_seconds && `–${formatTime(t.end_seconds)}`}
                {t.label && <span className="max-w-[140px] truncate font-sans text-faint group-hover:text-accent/80">{t.label}</span>}
              </button>
            ))}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3 text-[11px] text-faint">
          {!isMock && (
            <span className="inline-flex items-center gap-1.5" title="How strongly the video supports this answer">
              <span className="h-1 w-12 overflow-hidden rounded-full bg-line-2">
                <span
                  className={`block h-full rounded-full ${confidence > 0.7 ? "bg-accent" : confidence > 0.4 ? "bg-warn" : "bg-danger"}`}
                  style={{ width: `${Math.round(confidence * 100)}%` }}
                />
              </span>
              {Math.round(confidence * 100)}%
            </span>
          )}
          {message.metadata.insufficient_evidence && <span className="text-warn">Not clearly visible</span>}
          <Provenance message={message} />
          {isMock && <span className="text-warn">mock analyzer</span>}
          {message.evidence.length > 0 && (
            <button onClick={() => setShowEvidence(!showEvidence)} className="hover:text-muted">
              {showEvidence ? "Hide" : "Show"} evidence ({message.evidence.length})
            </button>
          )}
          {audio && (
            <button onClick={() => playBase64Audio(audio)} className="inline-flex items-center gap-1 hover:text-muted">
              <SpeakerIcon width={12} height={12} /> Play
            </button>
          )}
        </div>

        {showEvidence && (
          <ul className="space-y-1 border-l border-line-2 pl-3 text-xs text-muted">
            {message.evidence.map((e, i) => (
              <li key={i}>
                {e.level && <span className={`mr-1.5 rounded px-1 py-px text-[10px] ${LEVEL_STYLE[e.level] ?? ""}`}>{LEVEL_LABEL[e.level] ?? e.level}</span>}
                {e.timestamp_seconds != null && (
                  <button onClick={() => onSeek(e.timestamp_seconds!)} className="mr-1.5 font-mono text-accent/80 hover:text-accent">
                    {formatTime(e.timestamp_seconds)}
                  </button>
                )}
                {e.description}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

const LEVEL_LABEL: Record<string, string> = {
  detection: "detection",
  tracking: "tracking",
  rule: "event rule",
  model_interpretation: "video AI",
};
const LEVEL_STYLE: Record<string, string> = {
  detection: "bg-line-2 text-muted",
  tracking: "bg-line-2 text-muted",
  rule: "bg-accent/10 text-accent",
  model_interpretation: "bg-warn/10 text-warn",
};

/** Where the answer came from: local vision memory, and/or a video-model escalation. */
function Provenance({ message }: { message: Message }) {
  const tools = message.metadata.tools_used;
  if (!tools) return null;
  const local = tools.some((t) => t !== "analyze_video_clip");
  return (
    <span className="inline-flex items-center gap-1.5" title={`Tools: ${tools.join(", ") || "none"}`}>
      {local && <span className="rounded bg-accent/10 px-1.5 py-px text-accent">local vision</span>}
      {message.metadata.escalated && <span className="rounded bg-warn/10 px-1.5 py-px text-warn">Gemini video</span>}
    </span>
  );
}

function Thinking({ phase }: { phase: "transcribing" | "analyzing" }) {
  return (
    <div className="flex gap-3">
      <div className="grid h-7 w-7 shrink-0 place-items-center rounded-md bg-accent/15 text-accent">
        <EyeIcon width={14} height={14} />
      </div>
      <div className="flex items-center gap-2.5 text-sm text-muted">
        <span className="flex gap-1">
          {[0, 1, 2].map((i) => (
            <span key={i} className="typing-dot h-1.5 w-1.5 rounded-full bg-accent" style={{ animationDelay: `${i * 0.15}s` }} />
          ))}
        </span>
        {phase === "transcribing" ? "Ndumva… (transcribing)" : "Ndimo kureba video… (analyzing)"}
      </div>
    </div>
  );
}
