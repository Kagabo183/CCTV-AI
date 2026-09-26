"use client";

import { useMemo } from "react";
import { formatTime } from "@/lib/api";
import { titleCase, wildlifeGroups, wildlifeLabel, type SpeciesGroup } from "@/lib/findings";
import type { BoxTrack, Track, VisionRun } from "@/lib/types";
import { ChevronDownIcon, PawIcon, PlayIcon, QuestionIcon } from "./icons";

type Props = {
  run: VisionRun | null;
  boxes: BoxTrack | null;
  tracks: Track[];
  selected: string | null; // species key, "unknown", or null
  onSelect: (key: string | null) => void;
  onSeek: (seconds: number) => void;
  onAskAbout: (question: string) => void;
};

/**
 * Wildlife from the dedicated pipeline (MegaDetector V6 + SpeciesNet). Species are listed only when the
 * classifier was confident; everything else is "Unknown animals" with its best guesses shown as possibilities.
 */
export function WildlifePanel({ run, boxes, tracks, selected, onSelect, onSeek, onAskAbout }: Props) {
  const groups = useMemo(() => wildlifeGroups(boxes, tracks), [boxes, tracks]);
  if (!run) return null;
  if (run.status !== "completed") {
    return (
      <section className="rounded-2xl border border-line bg-panel px-4 py-3 text-sm text-muted" aria-live="polite">
        <span className="font-medium text-text">Wildlife</span> ·{" "}
        {run.status === "running" ? `identifying animals… ${Math.round(run.progress * 100)}%` : run.status === "queued" ? "waiting for the GPU…" : run.error ?? run.status}
      </section>
    );
  }
  if (!groups.length) {
    return <section className="rounded-2xl border border-line bg-panel px-4 py-3 text-sm text-muted">Wildlife · no animals were detected in the analysed part of this video.</section>;
  }
  const current = groups.find((g) => g.key === selected) ?? null;

  return (
    <section aria-labelledby="wildlife-title" className="rounded-2xl border border-line bg-panel">
      <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line px-4 py-3">
        <h3 id="wildlife-title" className="flex items-center gap-2 text-sm font-semibold">
          <PawIcon width={16} height={16} className="text-accent" aria-hidden /> Wildlife
        </h3>
        <p className="text-[11px] text-faint">MegaDetector V6 finds animals · SpeciesNet names them · numbers = most at the same time</p>
      </header>
      <ul className="divide-y divide-line">
        {groups.map((g) => {
          const open = g.key === selected;
          return (
            <li key={g.key}>
              <button
                onClick={() => onSelect(open ? null : g.key)}
                aria-expanded={open}
                className={`flex min-h-12 w-full cursor-pointer items-center gap-3 px-4 py-2 text-left transition hover:bg-panel-2 ${open ? "bg-panel-2" : ""}`}
              >
                <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg ${g.certain ? "bg-accent/10 text-accent" : "bg-warn/15 text-warn"}`} aria-hidden>
                  {g.certain ? <PawIcon width={16} height={16} /> : <QuestionIcon width={16} height={16} />}
                </span>
                <span className="w-10 shrink-0 text-right font-mono text-xl tabular">{g.certain ? g.atOnce : g.animals}</span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">{titleCase(g.label)}</span>
                  <span className="block truncate text-xs text-muted">
                    {g.certain
                      ? `${g.animals} animal${g.animals === 1 ? "" : "s"} tracked · seen ${formatTime(g.first)}–${formatTime(g.last)}`
                      : g.candidates.length
                        ? `species uncertain · possibly ${g.candidates.slice(0, 4).join(", ")}`
                        : "species uncertain"}
                  </span>
                </span>
                {g.certain && <Confidence value={g.meanScore} />}
                <ChevronDownIcon width={14} height={14} className={`shrink-0 text-faint transition ${open ? "rotate-180" : ""}`} aria-hidden />
              </button>
              {open && current && <SpeciesDetail group={current} tracks={tracks} onSeek={onSeek} onAskAbout={onAskAbout} />}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Confidence({ value }: { value: number }) {
  const word = value >= 0.8 ? "high" : value >= 0.6 ? "medium" : "low";
  return (
    <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] ${word === "low" ? "bg-warn/10 text-warn" : "bg-panel-2 text-muted"}`} title={`average species confidence ${value.toFixed(2)}`}>
      {word} confidence
    </span>
  );
}

function SpeciesDetail({ group, tracks, onSeek, onAskAbout }: { group: SpeciesGroup; tracks: Track[]; onSeek: (s: number) => void; onAskAbout: (q: string) => void }) {
  const ids = new Set(group.trackIds);
  const list = tracks.filter((t) => ids.has(t.track_id)).sort((a, b) => b.last_seen - b.first_seen - (a.last_seen - a.first_seen));
  const shown = list.slice(0, 12);
  return (
    <div className="border-t border-line bg-bg/30 px-4 py-3 text-xs">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {group.peakAt != null && (
          <button onClick={() => onSeek(group.peakAt!)} className="inline-flex min-h-8 cursor-pointer items-center gap-1.5 rounded-md bg-accent px-3 font-semibold text-accent-ink transition hover:brightness-110">
            <PlayIcon width={10} height={10} aria-hidden /> Play busiest moment ({formatTime(group.peakAt)})
          </button>
        )}
        <button
          onClick={() => onAskAbout(group.certain ? `What are the ${group.label} doing?` : "What are the animals the AI could not identify?")}
          className="inline-flex min-h-8 cursor-pointer items-center rounded-md border border-line-2 px-3 text-muted transition hover:text-text"
        >
          Ask the AI about them
        </button>
        <span className="text-faint">Boxes on the video now show only these animals.</span>
      </div>
      <ol className="divide-y divide-line rounded-lg border border-line">
        {shown.map((t) => (
          <li key={t.track_id}>
            <button onClick={() => onSeek(t.first_seen)} className="flex min-h-9 w-full cursor-pointer items-center gap-3 px-3 py-1.5 text-left hover:bg-panel-2">
              <span className="w-16 shrink-0 font-mono text-faint">Animal #{t.track_id}</span>
              <span className="min-w-0 flex-1 truncate capitalize">
                {wildlifeLabel(t)}
                {t.wildlife && <span className="ml-1.5 font-mono normal-case text-faint">{t.wildlife.score.toFixed(2)}</span>}
              </span>
              <span className="inline-flex shrink-0 items-center gap-1 font-mono text-muted">
                <PlayIcon width={8} height={8} aria-hidden />
                {formatTime(t.first_seen)} → {formatTime(t.last_seen)}
              </span>
            </button>
          </li>
        ))}
      </ol>
      {list.length > shown.length && <p className="mt-1.5 text-faint">+{list.length - shown.length} more (longest in view shown first)</p>}
    </div>
  );
}
