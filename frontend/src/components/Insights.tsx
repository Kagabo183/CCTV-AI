"use client";

import { useState, type ReactNode } from "react";
import { formatTime } from "@/lib/api";
import { eventLabel, groups, type Findings, type Group } from "@/lib/findings";
import type { VideoEvent, VisionRun } from "@/lib/types";
import { ActivityChart } from "./ActivityChart";
import { BellIcon, BoxIcon, CarIcon, PawIcon, PlayIcon, QuestionIcon, UserIcon } from "./icons";

type Props = {
  findings: Findings | null;
  notableEvents: VideoEvent[];
  runs: VisionRun[];
  onSeek: (seconds: number) => void;
  onAnalyse: () => void;
  onImport?: () => void;
  onAskAbout: (question: string) => void;
  /** The wildlife pipeline has species for this video: drop the general detector's animal guesses. */
  hideAnimals?: boolean;
};

const ICONS: Record<Group["key"], ReactNode> = {
  people: <UserIcon width={18} height={18} />,
  vehicles: <CarIcon width={18} height={18} />,
  animals: <PawIcon width={18} height={18} />,
  objects: <BoxIcon width={18} height={18} />,
  unknown: <QuestionIcon width={18} height={18} />,
};

/** What the camera saw and what happened, in plain language. Technical detail lives in AI details. */
export function Insights({ findings, notableEvents, runs, onSeek, onAnalyse, onImport, onAskAbout, hideAnimals }: Props) {
  const running = runs.find((r) => r.status === "running" || r.status === "queued");
  const unsupported = runs.length === 1 && runs[0].status === "unsupported" ? runs[0] : null;
  const disabled = runs.length === 1 && runs[0].status === "disabled";
  const analysed = runs.some((r) => r.status === "completed");

  if (disabled) return null;

  return (
    <section aria-labelledby="insights-title" className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="insights-title" className="text-lg font-semibold tracking-tight">
          What the camera sees
        </h2>
        {running && (
          <p className="flex items-center gap-2 text-sm text-muted" aria-live="polite">
            <span className="h-2 w-2 animate-pulse rounded-full bg-warn" aria-hidden />
            {running.status === "running" ? `Watching the video… ${Math.round(running.progress * 100)}%` : "Waiting for the GPU…"}
          </p>
        )}
      </div>

      {unsupported ? (
        <Empty>
          <p>{unsupported.unsupported_reason}</p>
          {onImport && <Primary onClick={onImport}>Import video</Primary>}
        </Empty>
      ) : !analysed && !running ? (
        <Empty>
          <p>This video has not been watched by the AI yet.</p>
          <Primary onClick={onAnalyse}>Watch this video</Primary>
        </Empty>
      ) : !findings ? (
        <SkeletonCards />
      ) : (
        <>
          <GroupCards findings={findings} notable={notableEvents.length} onSeek={onSeek} onAskAbout={onAskAbout} hideAnimals={hideAnimals} />
          <KeyMoments findings={findings} events={notableEvents} onSeek={onSeek} />
          <div className="rounded-2xl border border-line bg-panel">
            <ActivityChart timeline={findings.timeline} duration={findings.duration} events={notableEvents} onSeek={onSeek} />
          </div>
        </>
      )}
    </section>
  );
}

function GroupCards({ findings, notable, onSeek, onAskAbout, hideAnimals }: { findings: Findings; notable: number; onSeek: (s: number) => void; onAskAbout: (q: string) => void; hideAnimals?: boolean }) {
  const list = groups(findings).filter((g) => !(hideAnimals && g.key === "animals"));
  if (!list.length) return <Empty>Nothing was clearly seen in this video.</Empty>;
  return (
    <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {list.map((g) => {
        const unknown = g.key === "unknown";
        return (
          <li key={g.key}>
            <button
              onClick={() => (unknown ? onAskAbout("What objects could the AI not identify?") : g.at != null && onSeek(g.at))}
              className={`group flex h-full w-full cursor-pointer items-start gap-3 rounded-2xl border p-4 text-left transition ${
                unknown ? "border-dashed border-warn/40 bg-warn/5 hover:bg-warn/10" : "border-line bg-panel hover:border-accent/40 hover:bg-panel-2"
              }`}
            >
              <span className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ${unknown ? "bg-warn/15 text-warn" : "bg-accent/10 text-accent"}`} aria-hidden>
                {ICONS[g.key]}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-baseline gap-2">
                  <span className="font-mono text-2xl font-medium leading-none tabular">{g.count}</span>
                  <span className="text-sm font-medium">{g.label}</span>
                </span>
                <span className="mt-1 block text-xs leading-snug text-muted">
                  {g.key === "people" || g.key === "vehicles" || g.key === "animals" ? "at the same time" : g.detail}
                  {g.at != null && (
                    <>
                      {" · "}
                      <span className="text-faint group-hover:text-accent">
                        most at <span className="font-mono">{formatTime(g.at)}</span>
                      </span>
                    </>
                  )}
                </span>
                {(g.key === "vehicles" || g.key === "animals") && g.detail && <span className="mt-0.5 block truncate text-[11px] text-faint">{g.detail}</span>}
                {unknown && <span className="mt-0.5 block text-[11px] text-faint group-hover:text-warn">Ask the AI about them</span>}
              </span>
            </button>
          </li>
        );
      })}
      <li>
        <div className="flex h-full items-start gap-3 rounded-2xl border border-line bg-panel p-4">
          <span className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ${notable ? "bg-warn/15 text-warn" : "bg-panel-2 text-faint"}`} aria-hidden>
            <BellIcon width={18} height={18} />
          </span>
          <span className="min-w-0">
            <span className="flex items-baseline gap-2">
              <span className="font-mono text-2xl font-medium leading-none tabular">{notable}</span>
              <span className="text-sm font-medium">{notable === 1 ? "event" : "events"}</span>
            </span>
            <span className="mt-1 block text-xs text-muted">{notable ? "see What happened below" : "no alerts in this video"}</span>
          </span>
        </div>
      </li>
    </ul>
  );
}

type Moment = { t: number; title: string; detail: string; kind: "peak" | "event" };

function KeyMoments({ findings, events, onSeek }: { findings: Findings; events: VideoEvent[]; onSeek: (s: number) => void }) {
  const [all, setAll] = useState(false);
  const moments: Moment[] = [
    ...events.filter((e) => e.start_time != null).map((e) => ({ t: e.start_time!, title: eventLabel(e), detail: e.description, kind: "event" as const })),
    ...(findings.people ? [{ t: findings.people.peakAt, title: "Busiest moment for people", detail: `${findings.people.peak} ${findings.people.peak === 1 ? "person" : "people"} in view at once`, kind: "peak" as const }] : []),
    ...(findings.vehicles ? [{ t: findings.vehicles.peakAt, title: "Busiest moment for traffic", detail: `${findings.vehicles.peak} ${findings.vehicles.peak === 1 ? "vehicle" : "vehicles"} in view at once`, kind: "peak" as const }] : []),
  ].sort((a, b) => a.t - b.t);
  if (!moments.length) return null;
  const shown = all ? moments : moments.slice(0, 5);
  return (
    <div className="rounded-2xl border border-line bg-panel">
      <h3 className="border-b border-line px-4 py-3 text-sm font-semibold">What happened</h3>
      <ol className="divide-y divide-line">
        {shown.map((m, i) => (
          <li key={`${m.t}-${i}`}>
            <button onClick={() => onSeek(m.t)} className="group flex min-h-12 w-full cursor-pointer items-center gap-4 px-4 py-2.5 text-left transition hover:bg-panel-2">
              <span className="inline-flex w-16 shrink-0 items-center gap-1.5 font-mono text-sm text-muted group-hover:text-accent">
                <PlayIcon width={10} height={10} aria-hidden />
                {formatTime(m.t)}
              </span>
              <span className={`h-2 w-2 shrink-0 rounded-full ${m.kind === "event" ? "bg-warn" : "bg-accent"}`} aria-hidden />
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-medium">{m.title}</span>
                <span className="block truncate text-xs text-muted">{m.detail}</span>
              </span>
              <span className="sr-only">{m.kind === "event" ? "event" : "highlight"}. Play from here.</span>
            </button>
          </li>
        ))}
      </ol>
      {moments.length > 5 && (
        <button onClick={() => setAll(!all)} className="min-h-10 w-full cursor-pointer border-t border-line text-xs text-muted hover:text-text">
          {all ? "Show fewer" : `Show all ${moments.length} moments`}
        </button>
      )}
    </div>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-2xl border border-line bg-panel px-5 py-6 text-sm text-muted">{children}</div>;
}

function Primary({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button onClick={onClick} className="mt-3 min-h-10 cursor-pointer rounded-lg bg-accent px-4 text-sm font-semibold text-accent-ink transition hover:brightness-110">
      {children}
    </button>
  );
}

function SkeletonCards() {
  return (
    <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3" aria-busy="true" aria-label="Loading">
      {[0, 1, 2].map((i) => (
        <li key={i} className="h-32 animate-pulse rounded-2xl border border-line bg-panel" />
      ))}
    </ul>
  );
}
