"use client";

import { useEffect, useRef, type RefObject } from "react";
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
 * Draws a run's tracked boxes over the <video>, in sync with playback.
 * Boxes are in source-pixel coordinates; the video is letterboxed (object-contain),
 * so we map them into the displayed picture rectangle.
 */
export function BoxOverlay({ videoRef, boxes, label }: { videoRef: RefObject<HTMLVideoElement | null>; boxes: BoxTrack; label: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    let raf = 0;
    const maxGap = 2.5 / Math.max(boxes.sample_fps || 10, 1); // don't show stale boxes across long gaps
    const draw = () => {
      raf = requestAnimationFrame(draw);
      const video = videoRef.current;
      const canvas = canvasRef.current;
      if (!video || !canvas) return;
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

      const [sw, sh] = boxes.resolution ?? [video.videoWidth, video.videoHeight];
      if (!sw || !sh) return;
      const scale = Math.min(cw / sw, ch / sh);
      const ox = (cw - sw * scale) / 2;
      const oy = (ch - sh * scale) / 2;

      const i = frameAt(boxes.frames, video.currentTime + 0.001);
      if (i < 0 || video.currentTime - boxes.frames[i].t > maxGap) return;
      ctx.font = "600 11px ui-sans-serif, system-ui, sans-serif";
      ctx.lineWidth = 2;
      for (const [id, cls, conf, x1, y1, x2, y2] of boxes.frames[i].o) {
        const color = COLORS[cls] ?? "#cccccc";
        const x = ox + x1 * scale;
        const y = oy + y1 * scale;
        const w = (x2 - x1) * scale;
        const h = (y2 - y1) * scale;
        ctx.strokeStyle = color;
        ctx.strokeRect(x, y, w, h);
        const text = `#${id} ${cls} ${conf.toFixed(2)}`;
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
  }, [boxes, videoRef]);

  return (
    <>
      <canvas ref={canvasRef} className="pointer-events-none absolute inset-0 h-full w-full" />
      <div className="pointer-events-none absolute bottom-14 right-3 rounded-md bg-black/60 px-2 py-1 font-mono text-[11px] text-white/90 backdrop-blur">
        {label}
      </div>
    </>
  );
}
