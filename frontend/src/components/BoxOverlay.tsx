"use client";

import { useEffect, useRef } from "react";
import type { BoxTrack } from "@/lib/types";

const COLORS: Record<string, string> = {
  person: "#50c878",
  car: "#3caaff",
  truck: "#3c78ff",
  bus: "#5a5aff",
  motorcycle: "#ff78dc",
  bicycle: "#ffc83c",
};

/** Index of the last frame at or before t (binary search). */
function frameAt(frames: BoxTrack["frames"], t: number): number {
  let lo = 0;
  let hi = frames.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (frames[mid].t <= t) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

/**
 * Draws a run's tracked boxes over the player, in sync with playback.
 * `clock` returns the player's current time (HTML <video> or the YouTube player).
 * Boxes are in source-pixel coordinates; the picture is letterboxed (contain),
 * so we map them into the displayed picture rectangle.
 */
export function BoxOverlay({
  clock,
  boxes,
  label,
  highlightTrack = null,
  technical = false,
  only = null,
  labels = null,
}: {
  clock: () => number | null;
  boxes: BoxTrack;
  label: string;
  highlightTrack?: number | null;
  /** AI details on: track ids, confidence and the model name. Off: plain labels only. */
  technical?: boolean;
  /** Draw only these track ids (e.g. one species). */
  only?: Set<number> | null;
  /** Display names per track id (e.g. "possible hippopotamus"). */
  labels?: Record<number, string> | null;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    let raf = 0;
    const maxGap = 2.5 / Math.max(boxes.sample_fps || 10, 1); // don't show stale boxes across long gaps
    const confirm = boxes.confirm_confidence ?? 0.5; // below: the detector is not confident about the class
    const draw = () => {
      raf = requestAnimationFrame(draw);
      const now = clock();
      const canvas = canvasRef.current;
      if (now === null || !canvas) return;
      const dpr = window.devicePixelRatio || 1;
      const cw = canvas.clientWidth;
      const ch = canvas.clientHeight;
      if (canvas.width !== Math.round(cw * dpr) || canvas.height !== Math.round(ch * dpr)) {
        canvas.width = Math.round(cw * dpr);
        canvas.height = Math.round(ch * dpr);
      }
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cw, ch);

      const [sw, sh] = boxes.resolution ?? [0, 0];
      if (!sw || !sh) return;
      const scale = Math.min(cw / sw, ch / sh);
      const ox = (cw - sw * scale) / 2;
      const oy = (ch - sh * scale) / 2;

      const i = frameAt(boxes.frames, now + 0.001);
      if (i < 0 || now - boxes.frames[i].t > maxGap) return;
      ctx.font = "600 11px ui-sans-serif, system-ui, sans-serif";
      ctx.lineWidth = 2;
      for (const [id, rawCls, conf, x1, y1, x2, y2] of boxes.frames[i].o) {
        if (only && !only.has(id)) continue;
        const cls = labels?.[id] ?? rawCls;
        const uncertain = conf < confirm || cls.startsWith("possible") || cls === "animal"; // weak box, or species not confirmed
        const highlighted = highlightTrack === id;
        const color = uncertain ? "#9aa3b2" : (COLORS[cls] ?? "#e0e0e0");
        const x = ox + x1 * scale;
        const y = oy + y1 * scale;
        const w = (x2 - x1) * scale;
        const h = (y2 - y1) * scale;
        ctx.setLineDash(uncertain ? [6, 4] : []);
        ctx.lineWidth = highlighted ? 4 : 2;
        ctx.strokeStyle = highlighted ? "#ffffff" : color;
        ctx.strokeRect(x, y, w, h);
        ctx.setLineDash([]);
        ctx.lineWidth = 2;
        // Never state a weak guess as fact: "? person 0.41"
        const text = technical ? `#${id} ${uncertain ? "? " : ""}${cls} ${conf.toFixed(2)}` : cls.startsWith("possible") ? `${cls}?` : uncertain ? "?" : cls;
        const tw = ctx.measureText(text).width + 8;
        const ty = y > 16 ? y - 16 : y;
        ctx.fillStyle = color;
        ctx.fillRect(x, ty, tw, 16);
        ctx.fillStyle = "#000";
        ctx.fillText(text, x + 4, ty + 12);
      }
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [boxes, clock, highlightTrack, technical, only, labels]);

  return (
    <>
      <canvas ref={canvasRef} className="pointer-events-none absolute inset-0 h-full w-full" />
      {technical && label && (
        <div className="pointer-events-none absolute bottom-14 right-3 rounded-md bg-black/60 px-2 py-1 font-mono text-[11px] text-white/90 backdrop-blur">{label}</div>
      )}
    </>
  );
}
