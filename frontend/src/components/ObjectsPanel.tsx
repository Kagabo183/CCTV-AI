"use client";

import { useMemo, useState } from "react";
import { ApiError, api, formatTime } from "@/lib/api";
import { providerLabel, VEHICLE_CLASSES } from "@/lib/findings";
import type { DescribeResult, Track } from "@/lib/types";
import { PlayIcon, QuestionIcon } from "./icons";
import { Chip } from "./AiDetails";

type Props = {
  sourceId: string;
  runId: string;
  tracks: Track[];
  filter: string | null; // class name, "vehicles", "unknown" or null for all
  onFilter: (f: string | null) => void;
  selectedTrackId: number | null;
  onSelect: (trackId: number | null) => void;
  onSeek: (seconds: number) => void;
};

type Sort = "time" | "duration" | "confidence";

/** Every tracked object (including ones the detector could not identify), with an inspector per object. */
export function ObjectsPanel({ sourceId, runId, tracks, filter, onFilter, selectedTrackId, onSelect, onSeek }: Props) {
  const [sort, setSort] = useState<Sort>("time");
  const [descriptions, setDescriptions] = useState<Record<number, DescribeResult | { error: string } | "loading">>({});

  const counts = useMemo(() => {
    const c = new Map<string, number>();
    for (const t of tracks) c.set(t.object_class, (c.get(t.object_class) ?? 0) + 1);
    return [...c.entries()].sort((a, b) => (a[0] === "unknown" ? 1 : b[0] === "unknown" ? -1 : b[1] - a[1]));
  }, [tracks]);
  const vehicleCount = tracks.filter((t) => VEHICLE_CLASSES.has(t.object_class.toLowerCase())).length;
  const visible = useMemo(() => {
    const list = filter === "vehicles" ? tracks.filter((t) => VEHICLE_CLASSES.has(t.object_class.toLowerCase())) : filter ? tracks.filter((t) => t.object_class === filter) : tracks;
    const key: Record<Sort, (t: Track) => number> = { time: (t) => t.first_seen, duration: (t) => -(t.last_seen - t.first_seen), confidence: (t) => -t.mean_confidence };
    return [...list].sort((a, b) => key[sort](a) - key[sort](b));
  }, [tracks, filter, sort]);
  const selected = tracks.find((t) => t.track_id === selectedTrackId) ?? null;
  const described = selected ? descriptions[selected.track_id] : undefined;

  async function describe(t: Track) {
    setDescriptions((d) => ({ ...d, [t.track_id]: "loading" }));
    try {
      const result = await api.describeTrack(sourceId, runId, t.track_id);
      setDescriptions((d) => ({ ...d, [t.track_id]: result }));
    } catch (e) {
      setDescriptions((d) => ({ ...d, [t.track_id]: { error: e instanceof ApiError ? e.message : "The video AI could not answer" } }));
    }
  }

  if (tracks.length === 0) return <p className="px-4 py-4 text-sm text-muted">Nothing was tracked in this video.</p>;

  return (
    <div>
      <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-4 py-2.5">
        <Chip active={filter === null} onClick={() => onFilter(null)}>
          All <span className="font-mono text-faint">{tracks.length}</span>
        </Chip>
        {vehicleCount > 0 && counts.filter(([c]) => VEHICLE_CLASSES.has(c.toLowerCase())).length > 1 && (
          <Chip active={filter === "vehicles"} onClick={() => onFilter(filter === "vehicles" ? null : "vehicles")}>
            All vehicles <span className="font-mono text-faint">{vehicleCount}</span>
          </Chip>
        )}
        {counts.map(([cls, n]) => (
          <Chip key={cls} active={filter === cls} dashed={cls === "unknown"} onClick={() => onFilter(filter === cls ? null : cls)}>
            {cls === "unknown" ? "Not identified" : cls} <span className="font-mono text-faint">{n}</span>
          </Chip>
        ))}
        <label className="ml-auto inline-flex items-center gap-2 text-xs text-muted">
          Sort
          <select value={sort} onChange={(e) => setSort(e.target.value as Sort)} className="min-h-8 rounded-md border border-line-2 bg-bg px-2 text-text">
            <option value="time">First seen</option>
            <option value="duration">Longest in view</option>
            <option value="confidence">Most confident</option>
          </select>
        </label>
      </div>

      <div className="grid lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <ol className="max-h-80 divide-y divide-line overflow-y-auto border-line lg:border-r" aria-label="Tracked objects">
          {visible.map((t) => {
            const active = t.track_id === selectedTrackId;
            return (
              <li key={t.track_id}>
                <button
                  onClick={() => {
                    onSelect(t.track_id);
                    onSeek(t.first_seen);
                  }}
                  aria-current={active || undefined}
                  className={`flex min-h-10 w-full cursor-pointer items-center gap-3 px-4 py-2 text-left text-xs transition hover:bg-panel-2 ${active ? "bg-panel-2 shadow-[inset_2px_0_0_var(--accent)]" : ""}`}
                >
                  <span className="w-10 shrink-0 font-mono text-faint">#{t.track_id}</span>
                  <span className="min-w-0 flex-1 truncate capitalize">
                    {t.uncertain ? (
                      <>
                        <span className="text-warn">Not identified</span>
                        {t.candidate_class && <span className="text-faint"> · maybe {t.candidate_class}</span>}
                      </>
                    ) : (
                      t.object_class
                    )}
                  </span>
                  <Confidence value={t.mean_confidence} />
                  <span className="w-24 shrink-0 text-right font-mono text-faint">
                    {formatTime(t.first_seen)}–{formatTime(t.last_seen)}
                  </span>
                </button>
              </li>
            );
          })}
        </ol>

        <div className="px-4 py-3 text-xs" aria-live="polite">
          {!selected ? (
            <p className="text-muted">Choose an object to see its details, highlight it on the video and play the moment it appears.</p>
          ) : (
            <div className="space-y-3">
              <div className="flex items-baseline justify-between gap-2">
                <h3 className="text-sm font-semibold capitalize">
                  {selected.uncertain ? "Not identified" : selected.object_class} <span className="font-mono font-normal text-faint">#{selected.track_id}</span>
                </h3>
                <span className="font-mono text-faint">
                  {formatTime(selected.first_seen)}–{formatTime(selected.last_seen)}
                </span>
              </div>
              {selected.uncertain && (
                <p className="flex items-start gap-1.5 rounded-lg border border-warn/30 bg-warn/5 px-2.5 py-2 text-warn">
                  <QuestionIcon width={14} height={14} className="mt-px shrink-0" aria-hidden />
                  The detector was not confident (or kept changing its mind), so this object is not counted. Ask the video AI to look at it.
                </p>
              )}
              <dl className="grid grid-cols-[120px_1fr] gap-y-1.5">
                <dt className="text-faint">Detector says</dt>
                <dd className="capitalize">{selected.candidate_class ?? selected.object_class}</dd>
                <dt className="text-faint">Confidence</dt>
                <dd className="font-mono">
                  average {selected.mean_confidence.toFixed(2)} · best {selected.max_confidence.toFixed(2)}
                </dd>
                <dt className="text-faint">Labels over time</dt>
                <dd className="font-mono">
                  {Object.entries(selected.class_votes)
                    .sort((a, b) => b[1] - a[1])
                    .map(([k, v]) => `${k} ×${v}`)
                    .join(", ")}
                </dd>
                <dt className="text-faint">In view</dt>
                <dd className="font-mono">
                  {Math.max(0, selected.last_seen - selected.first_seen).toFixed(1)} s · {selected.frames} frames
                </dd>
              </dl>
              <div className="flex flex-wrap gap-2">
                <button onClick={() => onSeek(selected.first_seen)} className="inline-flex min-h-9 cursor-pointer items-center gap-1.5 rounded-md border border-line-2 px-3 text-muted transition hover:text-text">
                  <PlayIcon width={10} height={10} aria-hidden /> Play from {formatTime(selected.first_seen)}
                </button>
                <button
                  onClick={() => describe(selected)}
                  disabled={described === "loading"}
                  aria-busy={described === "loading"}
                  className="min-h-9 cursor-pointer rounded-md bg-accent px-3 font-semibold text-accent-ink transition hover:brightness-110 disabled:cursor-wait disabled:opacity-60"
                >
                  {described === "loading" ? "Asking the video AI…" : "Ask video AI what this is"}
                </button>
              </div>
              {described && described !== "loading" && (
                <div className="rounded-lg border border-line-2 bg-bg/40 p-3">
                  {"error" in described ? (
                    <p role="alert" className="text-danger">
                      {described.error}
                    </p>
                  ) : (
                    <>
                      <p className="mb-1 text-[11px] text-muted">
                        Video AI interpretation ({providerLabel(described.provider)}
                        {described.model ? `, ${described.model}` : ""}){described.confidence != null ? ` · confidence ${described.confidence.toFixed(2)}` : ""}. A model&apos;s opinion, not a measurement.
                      </p>
                      <p className="text-sm leading-relaxed text-text">{described.description}</p>
                    </>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Confidence({ value }: { value: number }) {
  const low = value < 0.5;
  return (
    <span className="inline-flex shrink-0 items-center gap-1.5 sm:w-16" title={`average confidence ${value.toFixed(2)}`}>
      <span className="hidden h-1 w-8 overflow-hidden rounded-full bg-line sm:block" aria-hidden>
        <span className={`block h-full rounded-full ${low ? "bg-warn" : "bg-muted"}`} style={{ width: `${Math.round(value * 100)}%` }} />
      </span>
      <span className={`font-mono ${low ? "text-warn" : "text-muted"}`}>{value.toFixed(2)}</span>
    </span>
  );
}
