"use client";

// "What the camera sees" for a live camera in the assistant: the live AI's current picture and the last
// 20 minutes, in plain language (recorded videos use Insights, which is built around a finished analysis).

import { useEffect, useState } from "react";
import { clock, countsText, DIAGNOSIS_HINT, STATE_LABEL, cams, summarizeEvents, type Camera, type CameraEvent } from "@/lib/cameras";

export function LiveInsights({ cameraId, onAskAbout }: { cameraId: string; onAskAbout: (q: string) => void }) {
  const [cam, setCam] = useState<Camera | null>(null);
  const [events, setEvents] = useState<CameraEvent[]>([]);
  useEffect(() => {
    let alive = true;
    const tick = () => {
      cams.get(cameraId).then((c) => alive && setCam(c)).catch(() => undefined);
      cams.events(cameraId, 20).then((e) => alive && setEvents(e)).catch(() => undefined);
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [cameraId]);

  const lines = summarizeEvents(events).slice(0, 8);
  const live = cam?.live;
  return (
    <section aria-labelledby="live-insights-title" className="space-y-3">
      <h2 id="live-insights-title" className="text-lg font-semibold tracking-tight">What the camera sees</h2>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
        <div className="rounded-2xl border border-line bg-panel p-4">
          <p className="text-xs font-medium uppercase tracking-wider text-faint">Right now</p>
          {!cam ? (
            <p className="mt-2 text-sm text-faint">Loading…</p>
          ) : cam.state !== "online" ? (
            <p className="mt-2 text-sm text-muted">{DIAGNOSIS_HINT[cam.health.diagnosis ?? ""] ?? STATE_LABEL[cam.state].label}</p>
          ) : (
            <>
              <p className="mt-2 text-2xl font-semibold tracking-tight" aria-live="polite">{live ? countsText(live.counts) : "Live AI starting…"}</p>
              {live?.at && <p className="mt-1 text-xs text-faint">Updated {clock(live.at)}</p>}
            </>
          )}
          <button onClick={() => onAskAbout("What is happening right now?")} className="mt-3 min-h-9 cursor-pointer rounded-lg bg-panel-2 px-3 text-sm hover:bg-line">
            Ask what is happening
          </button>
        </div>
        <div className="rounded-2xl border border-line bg-panel p-4">
          <p className="text-xs font-medium uppercase tracking-wider text-faint">Last 20 minutes</p>
          {lines.length === 0 ? (
            <p className="mt-2 text-sm text-muted">Nothing detected.</p>
          ) : (
            <ol className="mt-2 space-y-1">
              {lines.map((l) => (
                <li key={l.key} className="flex gap-2 text-sm">
                  <span className="w-16 shrink-0 font-mono text-xs leading-5 text-faint">{clock(l.at)}</span>
                  <span className="min-w-0">{l.text}</span>
                </li>
              ))}
            </ol>
          )}
          <button onClick={() => onAskAbout("What happened in the last 20 minutes?")} className="mt-3 min-h-9 cursor-pointer rounded-lg bg-panel-2 px-3 text-sm hover:bg-line">
            Summarise the last 20 minutes
          </button>
        </div>
      </div>
    </section>
  );
}
