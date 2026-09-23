"use client";

import { useState } from "react";
import { formatTime } from "@/lib/api";
import type { VideoEvent, VisionRun } from "@/lib/types";
import { PlayIcon } from "./icons";

const EVENT_LABELS: Record<string, string> = {
  object_appeared: "Appeared",
  object_disappeared: "Left view",
  person_entered: "Person entered",
  person_exited: "Person exited",
  vehicle_entered: "Vehicle entered",
  vehicle_exited: "Vehicle exited",
  dwell_in_zone: "Stayed in zone",
  loitering: "Long time in view",
  crowd_detected: "Crowd",
};

export const DETECTORS = [
  { id: "yolo", label: "YOLO26s" },
  { id: "rtdetr", label: "RT-DETR-L" },
];
export const TRACKERS = [
  { id: "bytetrack", label: "ByteTrack" },
  { id: "botsort", label: "BoT-SORT" },
];
const COMPARE = [
  { detector: "yolo", tracker: "bytetrack" },
  { detector: "rtdetr", tracker: "bytetrack" },
];

type Props = {
  runs: VisionRun[];
  selectedRunId: string | null;
  events: VideoEvent[];
  showBoxes: boolean;
  overlaySupported: boolean;
  onSelectRun: (id: string) => void;
  onToggleBoxes: (v: boolean) => void;
  onRun: (detector: string, tracker: string) => void;
  onSeek: (seconds: number) => void;
};

/** Local detection + tracking: run model combinations, overlay their boxes, compare metrics. */
export function VisionLab({ runs, selectedRunId, events, showBoxes, overlaySupported, onSelectRun, onToggleBoxes, onRun, onSeek }: Props) {
  const [detector, setDetector] = useState("yolo");
  const [tracker, setTracker] = useState("bytetrack");
  if (runs.length === 1 && runs[0].status === "disabled") return null;
  if (runs.length === 1 && runs[0].status === "unsupported") {
    return (
      <section className="rounded-2xl border border-line bg-panel px-4 py-3">
        <h2 className="text-sm font-semibold">Vision lab</h2>
        <p className="mt-1 text-sm text-muted">{runs[0].unsupported_reason}</p>
        <p className="mt-1 text-xs text-faint">Questions in the chat still work: they are answered by Gemini directly from the YouTube link.</p>
      </section>
    );
  }

  const real = runs.filter((r) => r.id);
  const completed = real.filter((r) => r.status === "completed");
  const busy = real.some((r) => r.status === "queued" || r.status === "running");
  const has = (d: string, t: string) => real.some((r) => r.detector === d && r.tracker === t && r.status !== "failed");
  const missingForCompare = COMPARE.filter((c) => !has(c.detector, c.tracker));
  const selected = real.find((r) => r.id === selectedRunId) ?? null;

  return (
    <section className="rounded-2xl border border-line bg-panel">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold">Vision lab</h2>
          <p className="text-xs text-faint">Local detection &amp; tracking on this GPU: no cloud calls</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <select value={detector} onChange={(e) => setDetector(e.target.value)} className="rounded-md border border-line-2 bg-bg px-2 py-1.5 outline-none">
            {DETECTORS.map((d) => (
              <option key={d.id} value={d.id}>
                {d.label}
              </option>
            ))}
          </select>
          <span className="text-faint">+</span>
          <select value={tracker} onChange={(e) => setTracker(e.target.value)} className="rounded-md border border-line-2 bg-bg px-2 py-1.5 outline-none">
            {TRACKERS.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
              </option>
            ))}
          </select>
          <button onClick={() => onRun(detector, tracker)} className="rounded-md border border-line-2 px-2.5 py-1.5 text-muted transition hover:border-accent/50 hover:text-text">
            {has(detector, tracker) ? "Re-run" : "Run"}
          </button>
          {missingForCompare.length > 0 && (
            <button
              onClick={() => missingForCompare.forEach((c) => onRun(c.detector, c.tracker))}
              className="rounded-md bg-accent px-2.5 py-1.5 font-semibold text-accent-ink transition hover:brightness-110"
            >
              Compare YOLO vs RT-DETR
            </button>
          )}
        </div>
      </header>

      {real.length === 0 ? (
        <p className="px-4 py-4 text-sm text-muted">No local analysis yet. Pick a model and press Run.</p>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
            {real.map((r) => {
              const active = r.id === selectedRunId;
              const running = r.status === "queued" || r.status === "running";
              return (
                <button
                  key={r.id}
                  onClick={() => r.status === "completed" && onSelectRun(r.id!)}
                  className={`inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-xs transition ${
                    active ? "border-accent/50 bg-accent/10 text-text" : "border-line-2 text-muted hover:text-text"
                  } ${r.status !== "completed" ? "cursor-default" : ""}`}
                  title={r.error ?? undefined}
                >
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${
                      running ? "animate-pulse bg-warn" : r.status === "completed" ? "bg-accent" : "bg-danger"
                    }`}
                  />
                  <span className="font-medium">{r.label}</span>
                  {running && <span>{r.status === "queued" ? "queued" : `${Math.round(r.progress * 100)}%`}</span>}
                  {r.status === "failed" && <span className="text-danger">failed</span>}
                  {r.is_primary && <span className="rounded bg-bg px-1 text-[10px] text-faint">assistant</span>}
                </button>
              );
            })}
            {completed.length > 0 && (
              <label className={`ml-auto inline-flex items-center gap-2 text-xs ${overlaySupported ? "text-muted" : "text-faint"}`}>
                <input type="checkbox" checked={showBoxes && overlaySupported} disabled={!overlaySupported} onChange={(e) => onToggleBoxes(e.target.checked)} />
                {overlaySupported ? "Show boxes on video" : "Box overlay not available for YouTube"}
              </label>
            )}
          </div>

          {real
            .filter((r) => r.status === "failed")
            .map((r) => (
              <p key={r.id} className="border-b border-line px-4 py-2 text-xs text-danger">
                {r.label} failed: {r.error ?? "unknown error"}
              </p>
            ))}
          {completed.length > 0 && <Comparison runs={completed} selectedRunId={selectedRunId} />}
          {busy && completed.length === 0 && <p className="px-4 py-3 text-sm text-muted">Analysing… results appear here when the run finishes.</p>}

          {selected?.status === "completed" && (
            <div className="border-t border-line">
              <p className="px-4 pt-3 text-xs font-medium text-muted">
                Events from {selected.label} ({events.length})
              </p>
              <ol className="max-h-56 divide-y divide-line overflow-y-auto">
                {events.length === 0 && <li className="px-4 py-3 text-sm text-muted">No events detected.</li>}
                {events.map((e) => (
                  <li key={e.id}>
                    <button
                      onClick={() => e.start_time != null && onSeek(e.start_time)}
                      className="group flex w-full items-center gap-3 px-4 py-2 text-left text-sm transition hover:bg-panel-2"
                    >
                      <span className="inline-flex w-14 shrink-0 items-center gap-1 font-mono text-[11px] text-muted group-hover:text-accent">
                        <PlayIcon width={9} height={9} />
                        {formatTime(e.start_time ?? 0)}
                      </span>
                      <span className="w-32 shrink-0 text-xs font-medium">{EVENT_LABELS[e.event_type] ?? e.event_type}</span>
                      <span className="min-w-0 flex-1 truncate text-xs text-muted">{e.description}</span>
                      {e.zone && <span className="shrink-0 rounded bg-bg px-1.5 py-0.5 text-[10px] text-faint">{e.zone}</span>}
                    </button>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </>
      )}
    </section>
  );
}

type Row = { label: string; hint?: string; value: (r: VisionRun) => string; best?: "high" | "low"; num?: (r: VisionRun) => number | undefined };

const ROWS: Row[] = [
  { label: "Throughput", hint: "frames processed per second (decode + detect + track + events)", value: (r) => fmt(r.performance.processing_fps, " fps"), num: (r) => r.performance.processing_fps, best: "high" },
  { label: "Speed vs real time", value: (r) => fmt(r.performance.realtime_factor, "×"), num: (r) => r.performance.realtime_factor, best: "high" },
  { label: "Detect latency p50 / p95", value: (r) => `${fmt(r.performance.detect_ms_p50)} / ${fmt(r.performance.detect_ms_p95, " ms")}`, num: (r) => r.performance.detect_ms_p50, best: "low" },
  { label: "GPU memory (tensors)", hint: "PyTorch peak for this model alone (weights + activations); the CUDA runtime adds ~250 MB on top", value: (r) => fmt(r.performance.gpu_peak_mb, " MB"), num: (r) => r.performance.gpu_peak_mb ?? undefined, best: "low" },
  { label: "CPU", value: (r) => fmt(r.performance.cpu_percent_avg, " %") },
  { label: "Tracks", hint: "distinct track ids; more than the real object count means fragmentation or duplicates", value: (r) => classList(r.tracks_by_class) || "0" },
  { label: "Short tracks (< 1 s)", hint: "flicker, duplicate boxes or broken identities", value: (r) => String(r.quality.tracks?.short_lived ?? "–"), num: (r) => r.quality.tracks?.short_lived, best: "low" },
  { label: "Mean track length", hint: "longer = identities held through motion and occlusion", value: (r) => fmt(r.quality.tracks?.mean_seconds, " s") },
  { label: "Detections (mean conf.)", value: (r) => Object.entries(r.quality.detections ?? {}).map(([k, v]) => `${k} ${v.count} (${v.mean_confidence.toFixed(2)})`).join(", ") || "–" },
  { label: "Events", value: (r) => String(Object.values(r.events_by_type).reduce((a, b) => a + b, 0)) },
];

function Comparison({ runs, selectedRunId }: { runs: VisionRun[]; selectedRunId: string | null }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[520px] text-xs">
        <thead>
          <tr className="text-left text-faint">
            <th className="px-4 py-2 font-medium">Metric</th>
            {runs.map((r) => (
              <th key={r.id} className={`px-4 py-2 font-medium ${r.id === selectedRunId ? "text-text" : ""}`}>
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
                <td className="px-4 py-2 text-muted" title={row.hint}>
                  {row.label}
                  {row.hint && <span className="ml-1 text-faint">ⓘ</span>}
                </td>
                {runs.map((r, i) => (
                  <td key={r.id} className={`px-4 py-2 font-mono ${target !== undefined && nums[i] === target ? "text-accent" : ""}`}>
                    {row.value(r)}
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
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${v} ${k}`)
    .join(", ");
}
