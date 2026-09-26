"use client";

import { useMemo, useState, type ReactNode } from "react";
import { formatTime } from "@/lib/api";
import { eventLabel, ROUTINE_EVENTS } from "@/lib/findings";
import type { Track, VideoEvent, VisionRun } from "@/lib/types";
import { ChevronDownIcon, PlayIcon, XIcon } from "./icons";
import { ObjectsPanel } from "./ObjectsPanel";

export const DETECTORS = [
  { id: "yolo", label: "YOLO26s · COCO, 80 classes (recommended)" },
  { id: "yolo_o365", label: "YOLO26s · Objects365, 365 classes" },
  { id: "rtdetr", label: "RT-DETR-L · COCO, 80 classes" },
  { id: "wildlife", label: "MegaDetector V6 + SpeciesNet · wildlife" },
];
export const TRACKERS = [
  { id: "bytetrack", label: "ByteTrack (recommended)" },
  { id: "botsort", label: "BoT-SORT" },
];
const COMPARE = [
  { detector: "yolo", tracker: "bytetrack" },
  { detector: "yolo_o365", tracker: "bytetrack" },
  { detector: "rtdetr", tracker: "bytetrack" },
];

type Tab = "objects" | "events" | "models";

type Props = {
  sourceId: string;
  runs: VisionRun[];
  selectedRunId: string | null;
  events: VideoEvent[];
  tracks: Track[];
  selectedTrackId: number | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSelectRun: (id: string) => void;
  onSelectTrack: (trackId: number | null) => void;
  onRun: (detector: string, tracker: string) => void;
  onSeek: (seconds: number) => void;
  onImport?: () => void;
  onCancel?: (runId: string) => void;
};

/**
 * AI details: the technical view behind the plain-language insights. Model runs and their comparison,
 * every tracked object with confidence, and the raw event log. Collapsed unless "AI details" is on.
 */
export function AiDetails(props: Props) {
  const { sourceId, runs, selectedRunId, events, tracks, selectedTrackId, open, onOpenChange, onSelectRun, onRun, onSeek, onCancel } = props;
  const [tab, setTab] = useState<Tab>("objects");
  const [classFilter, setClassFilter] = useState<string | null>(null);

  if (runs.length === 1 && (runs[0].status === "disabled" || runs[0].status === "unsupported")) return null;

  const real = runs.filter((r) => r.id);
  const completed = real.filter((r) => r.status === "completed");
  const active = real.filter((r) => r.status === "queued" || r.status === "running");
  const failed = real.filter((r) => r.status === "failed");
  const selected = completed.find((r) => r.id === selectedRunId) ?? null;
  const model = selected ? describeModel(selected) : null;

  return (
    <Card>
      <button
        onClick={() => onOpenChange(!open)}
        aria-expanded={open}
        aria-controls="ai-details-body"
        className="flex w-full cursor-pointer items-center gap-3 px-4 py-3 text-left transition hover:bg-panel-2"
      >
        <span className="mr-auto min-w-0">
          <span className="block text-sm font-semibold">AI details</span>
          <span className="block truncate text-xs text-faint">
            {selected ? `${selected.label}${model ? ` · ${model}` : ""} · runs on this computer` : "Models, confidence scores and every tracked object"}
          </span>
        </span>
        <ChevronDownIcon width={16} height={16} className={`shrink-0 text-muted transition ${open ? "rotate-180" : ""}`} aria-hidden />
      </button>
      {open && (
      <div id="ai-details-body" className="border-t border-line">
            {active.length > 0 && <Progress runs={active} onCancel={onCancel} />}
      {failed.length > 0 && (
        <div role="alert" className="space-y-1 border-b border-line bg-danger/5 px-4 py-2 text-xs text-danger">
          {failed.map((r) => (
            <p key={r.id}>
              {r.label} could not finish: {r.error ?? "unknown error"}. Re-run it from the Models tab.
            </p>
          ))}
        </div>
      )}

      {real.length === 0 && (
        <div className="px-4 py-6 text-sm text-muted">
          <p>This video has not been analysed yet.</p>
          <button onClick={() => onRun("yolo", "bytetrack")} className="mt-3 min-h-9 cursor-pointer rounded-md bg-accent px-3 font-semibold text-accent-ink transition hover:brightness-110">
            Analyse video
          </button>
        </div>
      )}

      {(selected || real.length > 0) && (
        <>
          <div role="tablist" aria-label="Analysis details" className="flex gap-1 border-y border-line px-3 text-sm">
            {(
              [
                ["objects", "Objects", selected ? tracks.length : null],
                ["events", "Event log", selected ? events.length : null],
                ["models", "Models", completed.length],
              ] as [Tab, string, number | null][]
            ).map(([id, label, n]) => {
              const disabled = id !== "models" && !selected;
              const current = selected ? tab === id : id === "models";
              return (
                <button
                  key={id}
                  role="tab"
                  id={`tab-${id}`}
                  aria-selected={current}
                  aria-controls={`panel-${id}`}
                  disabled={disabled}
                  onClick={() => setTab(id)}
                  className={`-mb-px inline-flex min-h-10 cursor-pointer items-center gap-1.5 border-b-2 px-3 transition disabled:cursor-not-allowed disabled:opacity-40 ${
                    current ? "border-accent text-text" : "border-transparent text-muted hover:text-text"
                  }`}
                >
                  {label}
                  {n !== null && <span className="rounded bg-panel-2 px-1.5 font-mono text-[11px] text-muted">{n}</span>}
                </button>
              );
            })}
          </div>

          <div role="tabpanel" id={`panel-${selected ? tab : "models"}`} aria-labelledby={`tab-${selected ? tab : "models"}`}>
            {selected && tab === "objects" && (
              <ObjectsPanel
                key={selected.id}
                sourceId={sourceId}
                runId={selected.id!}
                tracks={tracks}
                filter={classFilter}
                onFilter={setClassFilter}
                selectedTrackId={selectedTrackId}
                onSelect={props.onSelectTrack}
                onSeek={onSeek}
              />
            )}
            {selected && tab === "events" && <EventsList events={events} onSeek={onSeek} />}
            {(tab === "models" || !selected) && (
              <Models runs={real} selectedRunId={selectedRunId} onSelectRun={onSelectRun} onRun={onRun} onCancel={onCancel} />
            )}
          </div>
        </>
      )}
      </div>
      )}
    </Card>
  );
}

function Card({ children }: { children: ReactNode }) {
  return <section className="overflow-hidden rounded-2xl border border-line bg-panel">{children}</section>;
}

function describeModel(r: VisionRun): string | null {
  const parts: string[] = [];
  const res = r.performance.resolution;
  if (res) parts.push(`${res[0]}×${res[1]}`);
  if (r.settings?.tiling) parts.push("small-object mode");
  return parts.length ? parts.join(" · ") : null;
}

// ------------------------------------------------------------------ running / queued runs
function Progress({ runs, onCancel }: { runs: VisionRun[]; onCancel?: (id: string) => void }) {
  return (
    <div className="space-y-2 border-b border-line px-4 py-3" aria-live="polite">
      {runs.map((r) => (
        <div key={r.id} className="flex items-center gap-3 text-xs">
          <span className="w-44 shrink-0 truncate text-muted">
            {r.status === "running" ? "Analysing with " : "Waiting: "}
            <span className="text-text">{r.label}</span>
          </span>
          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-line" role="progressbar" aria-valuenow={Math.round(r.progress * 100)} aria-valuemin={0} aria-valuemax={100} aria-label={r.label ?? "analysis"}>
            <div className={`h-full rounded-full ${r.status === "running" ? "bg-accent" : "bg-line-2"} transition-all`} style={{ width: `${Math.max(2, r.progress * 100)}%` }} />
          </div>
          <span className="w-24 shrink-0 text-right font-mono text-muted">
            {r.status === "running" ? `${Math.round(r.progress * 100)}%` : r.queue_position ? `#${r.queue_position} in queue` : "queued"}
          </span>
          {onCancel && (
            <button onClick={() => onCancel(r.id!)} className="inline-flex min-h-8 cursor-pointer items-center gap-1 rounded-md px-2 text-faint transition hover:bg-danger/10 hover:text-danger" aria-label={`Cancel ${r.label}`}>
              <XIcon width={13} height={13} aria-hidden /> Cancel
            </button>
          )}
        </div>
      ))}
      {runs.some((r) => r.status === "queued") && <p className="text-[11px] text-faint">Analyses run one at a time on the GPU.</p>}
    </div>
  );
}

// ------------------------------------------------------------------ events
function EventsList({ events, onSeek }: { events: VideoEvent[]; onSeek: (s: number) => void }) {
  const [showRoutine, setShowRoutine] = useState(false);
  const [type, setType] = useState<string | null>(null);
  const routine = events.filter((e) => ROUTINE_EVENTS.has(e.event_type)).length;
  const pool = showRoutine ? events : events.filter((e) => !ROUTINE_EVENTS.has(e.event_type));
  const types = useMemo(() => {
    const c = new Map<string, number>();
    for (const e of pool) c.set(e.event_type, (c.get(e.event_type) ?? 0) + 1);
    return [...c.entries()].sort((a, b) => b[1] - a[1]);
  }, [pool]);
  const list = type ? pool.filter((e) => e.event_type === type) : pool;

  return (
    <div>
      <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-4 py-2.5">
        <Chip active={type === null} onClick={() => setType(null)}>
          All <span className="font-mono text-faint">{pool.length}</span>
        </Chip>
        {types.map(([t, n]) => (
          <Chip key={t} active={type === t} onClick={() => setType(type === t ? null : t)}>
            {eventLabel({ event_type: t })} <span className="font-mono text-faint">{n}</span>
          </Chip>
        ))}
        {routine > 0 && (
          <label className="ml-auto inline-flex min-h-8 cursor-pointer items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={showRoutine} onChange={(e) => (setShowRoutine(e.target.checked), setType(null))} className="accent-[var(--accent)]" />
            Include every appear / leave ({routine})
          </label>
        )}
      </div>
      {list.length === 0 ? (
        <p className="px-4 py-4 text-sm text-muted">
          No rule events (zones, lines, crowds, loitering) in this video.
          {routine > 0 && !showRoutine && " Tick “Include every appear / leave” to see each object coming and going."}
        </p>
      ) : (
        <ol className="max-h-96 divide-y divide-line overflow-y-auto">
          {list.map((e) => (
            <li key={e.id}>
              <button
                onClick={() => e.start_time != null && onSeek(e.start_time)}
                className="group flex min-h-10 w-full cursor-pointer items-center gap-3 px-4 py-2 text-left text-sm transition hover:bg-panel-2"
              >
                <span className="inline-flex w-14 shrink-0 items-center gap-1 font-mono text-xs text-muted group-hover:text-accent">
                  <PlayIcon width={9} height={9} aria-hidden />
                  {formatTime(e.start_time ?? 0)}
                </span>
                <span className="w-32 shrink-0 text-xs font-medium">{eventLabel(e)}</span>
                <span className="min-w-0 flex-1 truncate text-xs text-muted">{e.description}</span>
                {e.zone && <span className="shrink-0 rounded bg-bg px-1.5 py-0.5 text-[11px] text-muted">{e.zone}</span>}
              </button>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

export function Chip({ active, onClick, children, dashed }: { active: boolean; onClick: () => void; children: ReactNode; dashed?: boolean }) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`inline-flex min-h-8 cursor-pointer items-center gap-1.5 rounded-md border px-2.5 text-xs capitalize transition ${
        active ? "border-accent/50 bg-accent/10 text-text" : "border-line-2 text-muted hover:text-text"
      } ${dashed ? "border-dashed" : ""}`}
    >
      {children}
    </button>
  );
}

// ------------------------------------------------------------------ models: runs, new run, comparison
function Models({ runs, selectedRunId, onSelectRun, onRun, onCancel }: { runs: VisionRun[]; selectedRunId: string | null; onSelectRun: (id: string) => void; onRun: (d: string, t: string) => void; onCancel?: (id: string) => void }) {
  const [detector, setDetector] = useState("yolo");
  const [tracker, setTracker] = useState("bytetrack");
  const completed = runs.filter((r) => r.status === "completed");
  const has = (d: string, t: string) => runs.some((r) => r.detector === d && r.tracker === t && r.status !== "failed" && r.status !== "cancelled");
  const missing = COMPARE.filter((c) => !has(c.detector, c.tracker));

  return (
    <div>
      <div className="border-b border-line px-4 py-3">
        <p className="mb-2 text-xs text-muted">Results shown above come from:</p>
        <ul className="flex flex-wrap gap-2">
          {runs.map((r) => {
            const done = r.status === "completed";
            const current = r.id === selectedRunId;
            return (
              <li key={r.id}>
                <button
                  onClick={() => done && onSelectRun(r.id!)}
                  disabled={!done}
                  aria-pressed={current}
                  className={`inline-flex min-h-9 items-center gap-2 rounded-lg border px-3 text-xs transition ${done ? "cursor-pointer" : "cursor-default"} ${
                    current ? "border-accent/50 bg-accent/10 text-text" : "border-line-2 text-muted hover:text-text"
                  }`}
                  title={r.error ?? undefined}
                >
                  <StatusDot status={r.status} />
                  <span className="font-medium">{r.label}</span>
                  <span className="text-faint">{statusText(r)}</span>
                  {r.is_primary && <span className="rounded bg-bg px-1.5 text-[11px] text-muted">used by assistant</span>}
                </button>
              </li>
            );
          })}
        </ul>
      </div>

      <div className="flex flex-wrap items-end gap-3 border-b border-line bg-bg/30 px-4 py-3 text-xs">
        <label className="flex flex-col gap-1 text-muted">
          Detector
          <select value={detector} onChange={(e) => setDetector(e.target.value)} className="min-h-9 rounded-md border border-line-2 bg-bg px-2 text-text">
            {DETECTORS.map((d) => (
              <option key={d.id} value={d.id}>
                {d.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-muted">
          Tracker
          <select value={tracker} onChange={(e) => setTracker(e.target.value)} className="min-h-9 rounded-md border border-line-2 bg-bg px-2 text-text">
            {TRACKERS.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
              </option>
            ))}
          </select>
        </label>
        <button onClick={() => onRun(detector, tracker)} className="min-h-9 cursor-pointer rounded-md border border-line-2 px-3 text-text transition hover:border-accent/50">
          {has(detector, tracker) ? "Re-run" : "Run"}
        </button>
        {missing.length > 0 && (
          <button onClick={() => missing.forEach((c) => onRun(c.detector, c.tracker))} className="min-h-9 cursor-pointer rounded-md bg-accent px-3 font-semibold text-accent-ink transition hover:brightness-110">
            Compare all detectors
          </button>
        )}
        {runs.some((r) => r.status === "running" || r.status === "queued") && onCancel && <span className="text-faint">Cancel running analyses from the progress bar above.</span>}
      </div>

      {completed.length > 0 && <Comparison runs={completed} selectedRunId={selectedRunId} />}
    </div>
  );
}

function StatusDot({ status }: { status: VisionRun["status"] }) {
  const cls = status === "completed" ? "bg-accent" : status === "running" || status === "queued" ? "animate-pulse bg-warn" : status === "cancelled" ? "bg-faint" : "bg-danger";
  return <span className={`h-1.5 w-1.5 rounded-full ${cls}`} aria-hidden />;
}

function statusText(r: VisionRun): string {
  if (r.status === "running") return `${Math.round(r.progress * 100)}%`;
  if (r.status === "queued") return r.queue_position ? `queued #${r.queue_position}` : "queued";
  if (r.status === "failed") return "failed";
  if (r.status === "cancelled") return "cancelled";
  return "";
}

type Row = { label: string; hint?: string; value: (r: VisionRun) => string; best?: "high" | "low"; num?: (r: VisionRun) => number | undefined };

const ROWS: Row[] = [
  { label: "Throughput", hint: "frames processed per second (decode + detect + track + events)", value: (r) => fmt(r.performance.processing_fps, " fps"), num: (r) => r.performance.processing_fps, best: "high" },
  { label: "Speed vs real time", value: (r) => fmt(r.performance.realtime_factor, "×"), num: (r) => r.performance.realtime_factor, best: "high" },
  { label: "Detect latency p50 / p95", value: (r) => `${fmt(r.performance.detect_ms_p50)} / ${fmt(r.performance.detect_ms_p95, " ms")}`, num: (r) => r.performance.detect_ms_p50, best: "low" },
  { label: "GPU memory", hint: "PyTorch peak for this model (weights + activations); the CUDA runtime adds ~250 MB", value: (r) => fmt(r.performance.gpu_peak_mb, " MB"), num: (r) => r.performance.gpu_peak_mb ?? undefined, best: "low" },
  { label: "CPU", value: (r) => fmt(r.performance.cpu_percent_avg, " %") },
  { label: "Tracks", hint: "distinct track ids; more than the real object count means fragmentation or duplicates", value: (r) => classList(r.tracks_by_class) || "0" },
  { label: "Short tracks (< 1 s)", hint: "flicker, duplicate boxes or broken identities (lower is better)", value: (r) => String(r.quality.tracks?.short_lived ?? "–"), num: (r) => r.quality.tracks?.short_lived, best: "low" },
  { label: "Not identified", hint: "tracks labelled 'unknown': low confidence or unstable class", value: (r) => String(r.quality.tracks?.uncertain ?? "–") },
  { label: "Mean track length", hint: "longer = identities held through motion and occlusion", value: (r) => fmt(r.quality.tracks?.mean_seconds, " s") },
  { label: "Events", value: (r) => String(Object.values(r.events_by_type).reduce((a, b) => a + b, 0)) },
];

function Comparison({ runs, selectedRunId }: { runs: VisionRun[]; selectedRunId: string | null }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[520px] text-xs tabular">
        <caption className="px-4 pb-1 pt-3 text-left text-xs text-muted">
          Model comparison on this video. <span className="text-accent">Green</span> marks the best value in a row.
        </caption>
        <thead>
          <tr className="text-left text-faint">
            <th scope="col" className="px-4 py-2 font-medium">
              Metric
            </th>
            {runs.map((r) => (
              <th scope="col" key={r.id} className={`px-4 py-2 font-medium ${r.id === selectedRunId ? "text-text" : ""}`}>
                {r.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {ROWS.map((row) => {
            const nums = row.num ? runs.map((r) => row.num!(r)) : [];
            const valid = nums.filter((n): n is number => typeof n === "number");
            const target = runs.length > 1 && valid.length > 1 ? (row.best === "high" ? Math.max(...valid) : row.best === "low" ? Math.min(...valid) : undefined) : undefined;
            return (
              <tr key={row.label}>
                <th scope="row" className="px-4 py-2 text-left font-normal text-muted">
                  {row.label}
                  {row.hint && <span className="mt-0.5 block text-[11px] text-faint">{row.hint}</span>}
                </th>
                {runs.map((r, i) => (
                  <td key={r.id} className={`px-4 py-2 align-top font-mono ${target !== undefined && nums[i] === target ? "text-accent" : ""}`}>
                    {row.value(r)}
                    {target !== undefined && nums[i] === target && <span className="sr-only"> (best)</span>}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function fmt(v: number | null | undefined, unit = ""): string {
  return v === null || v === undefined ? "–" : `${Number.isInteger(v) ? v : v.toFixed(1)}${unit}`;
}

function classList(counts: Record<string, number>): string {
  const all = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const top = all.slice(0, 5).map(([k, v]) => `${v} ${k}`).join(", ");
  return all.length > 5 ? `${top} +${all.length - 5} more` : top;
}
