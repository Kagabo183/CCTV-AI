"use client";

import { useEffect, useRef, useState } from "react";
import { formatTime } from "@/lib/api";
import type { Findings } from "@/lib/findings";
import { eventLabel } from "@/lib/findings";
import type { VideoEvent } from "@/lib/types";
import { TableIcon } from "./icons";

const HEIGHT = 132;
const PAD = { top: 12, right: 12, bottom: 22, left: 28 };
const SERIES = [
  { key: "people", label: "People", color: "var(--series-people)" },
  { key: "vehicles", label: "Vehicles", color: "var(--series-vehicles)" },
] as const;

type Props = {
  timeline: Findings["timeline"];
  duration: number;
  events: VideoEvent[]; // notable (non-routine) events, drawn as ticks under the plot
  onSeek: (seconds: number) => void;
};

/** How many people / vehicles were visible at the same time, second by second. Click to jump the video there. */
export function ActivityChart({ timeline, duration, events, onSeek }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [hover, setHover] = useState<number | null>(null);
  const [asTable, setAsTable] = useState(false);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(240, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const shown = SERIES.filter((s) => timeline.some((p) => p[s.key] > 0));
  const maxY = Math.max(1, ...timeline.flatMap((p) => shown.map((s) => p[s.key])));
  const niceMax = maxY <= 4 ? maxY : Math.ceil(maxY / 2) * 2;
  const plotW = width - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const span = Math.max(duration, 1);
  const x = (t: number) => PAD.left + (t / span) * plotW;
  const y = (v: number) => PAD.top + plotH - (v / niceMax) * plotH;
  const tickStep = [5, 10, 15, 30, 60, 120, 300, 600].find((s) => span / s <= Math.max(3, Math.floor(plotW / 90))) ?? 1200;
  const xTicks = Array.from({ length: Math.floor(span / tickStep) + 1 }, (_, i) => i * tickStep);
  const point = hover !== null ? timeline[hover] : null;

  function path(key: "people" | "vehicles") {
    // step line: the count holds for the whole second
    return timeline.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p[key]).toFixed(1)}H${x(Math.min(p.t + 1, span)).toFixed(1)}`).join("");
  }

  function indexAt(clientX: number) {
    const rect = wrap.current!.getBoundingClientRect();
    const t = ((clientX - rect.left - PAD.left) / plotW) * span;
    return Math.max(0, Math.min(timeline.length - 1, Math.floor(t)));
  }

  if (!shown.length) return <p className="px-4 py-3 text-sm text-muted">No people or vehicles were confidently detected in this video.</p>;

  return (
    <figure className="px-4 pb-3 pt-2">
      <figcaption className="mb-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        <span className="font-medium text-text">Visible at the same time</span>
        <span className="flex items-center gap-3" aria-label="Legend">
          {shown.map((s) => (
            <span key={s.key} className="inline-flex items-center gap-1.5 text-muted">
              <span className="h-0.5 w-4 rounded" style={{ background: s.color }} aria-hidden />
              {s.label}
            </span>
          ))}
        </span>
        <button
          onClick={() => setAsTable(!asTable)}
          aria-pressed={asTable}
          className="ml-auto inline-flex min-h-8 cursor-pointer items-center gap-1.5 rounded-md px-2 text-muted transition hover:bg-panel-2 hover:text-text"
        >
          <TableIcon width={14} height={14} aria-hidden /> {asTable ? "Show chart" : "Show as table"}
        </button>
      </figcaption>

      {asTable ? (
        <div className="max-h-56 overflow-y-auto rounded-lg border border-line">
          <table className="w-full text-xs tabular">
            <thead className="sticky top-0 bg-panel text-left text-faint">
              <tr>
                <th className="px-3 py-1.5 font-medium">Time</th>
                {shown.map((s) => (
                  <th key={s.key} className="px-3 py-1.5 font-medium">
                    {s.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-line font-mono">
              {timeline
                .filter((p) => shown.some((s) => p[s.key] > 0))
                .map((p) => (
                  <tr key={p.t} className="cursor-pointer hover:bg-panel-2" onClick={() => onSeek(p.t)}>
                    <td className="px-3 py-1 text-muted">{formatTime(p.t)}</td>
                    {shown.map((s) => (
                      <td key={s.key} className="px-3 py-1">
                        {p[s.key]}
                      </td>
                    ))}
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div
          ref={wrap}
          className="relative cursor-crosshair select-none"
          tabIndex={0}
          role="slider"
          aria-label="Activity over time. Use arrow keys to move, Enter to jump the video to that moment."
          aria-valuemin={0}
          aria-valuemax={Math.floor(span)}
          aria-valuenow={hover ?? 0}
          aria-valuetext={point ? `${formatTime(point.t)}: ${shown.map((s) => `${point[s.key]} ${s.label.toLowerCase()}`).join(", ")}` : "No time selected"}
          onMouseMove={(e) => setHover(indexAt(e.clientX))}
          onMouseLeave={() => setHover(null)}
          onClick={(e) => onSeek(timeline[indexAt(e.clientX)].t)}
          onKeyDown={(e) => {
            if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
              e.preventDefault();
              const step = e.shiftKey ? 10 : 1;
              setHover((h) => Math.max(0, Math.min(timeline.length - 1, (h ?? 0) + (e.key === "ArrowRight" ? step : -step))));
            } else if (e.key === "Enter" && hover !== null) onSeek(timeline[hover].t);
          }}
        >
          <svg width={width} height={HEIGHT} className="block" aria-hidden>
            {/* recessive grid: baseline + top value */}
            {[0, niceMax].map((v) => (
              <g key={v}>
                <line x1={PAD.left} x2={width - PAD.right} y1={y(v)} y2={y(v)} stroke="var(--line)" strokeWidth={1} />
                <text x={PAD.left - 6} y={y(v) + 3.5} textAnchor="end" className="fill-faint font-mono text-[10px]">
                  {v}
                </text>
              </g>
            ))}
            {xTicks.map((t) => (
              <text key={t} x={x(t)} y={HEIGHT - 6} textAnchor={t === 0 ? "start" : "middle"} className="fill-faint font-mono text-[10px]">
                {formatTime(t)}
              </text>
            ))}
            {/* notable events as ticks just above the axis */}
            {events.map((e) =>
              e.start_time == null ? null : (
                <line key={e.id} x1={x(e.start_time)} x2={x(e.start_time)} y1={PAD.top + plotH + 2} y2={PAD.top + plotH + 7} stroke="var(--warn)" strokeWidth={1.5} />
              ),
            )}
            {shown.map((s) => (
              <path key={s.key} d={path(s.key)} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" />
            ))}
            {point && (
              <g>
                <line x1={x(point.t + 0.5)} x2={x(point.t + 0.5)} y1={PAD.top} y2={PAD.top + plotH} stroke="var(--muted)" strokeWidth={1} strokeDasharray="2 3" />
                {shown.map((s) => (
                  <circle key={s.key} cx={x(point.t + 0.5)} cy={y(point[s.key])} r={4} fill={s.color} stroke="var(--panel)" strokeWidth={2} />
                ))}
              </g>
            )}
          </svg>
          {point && (
            <div
              className="pointer-events-none absolute top-0 z-10 min-w-32 rounded-lg border border-line-2 bg-bg/95 px-2.5 py-2 text-xs shadow-lg"
              style={{ left: Math.min(x(point.t + 0.5) + 10, width - 150) }}
              role="status"
            >
              <p className="mb-1 font-mono text-faint">{formatTime(point.t)}</p>
              {shown.map((s) => (
                <p key={s.key} className="flex items-center gap-2">
                  <span className="h-0.5 w-3 rounded" style={{ background: s.color }} aria-hidden />
                  <span className="text-muted">{s.label}</span>
                  <span className="ml-auto font-mono text-text">{point[s.key]}</span>
                </p>
              ))}
              {events.filter((e) => e.start_time != null && Math.floor(e.start_time) === point.t).slice(0, 2).map((e) => (
                <p key={e.id} className="mt-1 text-warn">
                  {eventLabel(e)}
                </p>
              ))}
              <p className="mt-1 text-faint">Click to play from here</p>
            </div>
          )}
        </div>
      )}
    </figure>
  );
}
