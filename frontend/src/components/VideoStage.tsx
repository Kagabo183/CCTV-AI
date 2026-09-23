"use client";

import { useImperativeHandle, useRef, useState, type Ref } from "react";
import { formatTime } from "@/lib/api";
import type { BoxTrack, VideoSession, VideoSource } from "@/lib/types";
import { BoxOverlay } from "./BoxOverlay";
import { AlertIcon, CameraIcon } from "./icons";

export type VideoStageHandle = { seek: (seconds: number) => void };

// Keyed by source id in the parent, so local state resets per video.
type Props = {
  source: VideoSource | null;
  session: VideoSession | null;
  analyzerIsMock: boolean;
  overlay?: { boxes: BoxTrack; label: string } | null;
  ref?: Ref<VideoStageHandle>;
};

export function VideoStage({ source, session, analyzerIsMock, overlay, ref }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [youtubeStart, setYoutubeStart] = useState<number | null>(null);
  const [current, setCurrent] = useState(0);
  const [playError, setPlayError] = useState(false);

  useImperativeHandle(
    ref,
    () => ({
      seek(seconds: number) {
        if (source?.playback?.type === "youtube") {
          setYoutubeStart(Math.floor(seconds));
          return;
        }
        const v = videoRef.current;
        if (v) {
          v.currentTime = seconds;
          void v.play().catch(() => undefined);
        }
      },
    }),
    [source],
  );

  if (!source) {
    return (
      <div className="grid aspect-video w-full place-items-center rounded-2xl border border-line bg-panel">
        <div className="text-center">
          <div className="mx-auto grid h-12 w-12 place-items-center rounded-xl bg-panel-2 text-faint">
            <CameraIcon width={22} height={22} />
          </div>
          <p className="mt-3 text-sm font-medium">No camera selected</p>
          <p className="mt-1 text-sm text-muted">Choose a video on the left, or connect a new one.</p>
        </div>
      </div>
    );
  }

  const playback = source.playback;
  return (
    <div className="space-y-3">
      <div className="relative aspect-video w-full overflow-hidden rounded-2xl border border-line bg-black">
        {playback?.type === "youtube" ? (
          <iframe
            key={`${source.id}-${youtubeStart}`}
            className="h-full w-full"
            src={`${playback.url}?rel=0&modestbranding=1${youtubeStart !== null ? `&start=${youtubeStart}&autoplay=1` : ""}`}
            allow="autoplay; encrypted-media; picture-in-picture"
            referrerPolicy="strict-origin-when-cross-origin"
            allowFullScreen
            title={source.name}
          />
        ) : playback ? (
          <video
            key={source.id}
            ref={videoRef}
            src={playback.url}
            controls
            playsInline
            preload="metadata"
            className="h-full w-full bg-black object-contain"
            onTimeUpdate={(e) => setCurrent(e.currentTarget.currentTime)}
            onError={() => setPlayError(true)}
          />
        ) : null}
        {overlay && playback && playback.type !== "youtube" && !playError && <BoxOverlay videoRef={videoRef} boxes={overlay.boxes} label={overlay.label} />}

        <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between bg-gradient-to-b from-black/70 to-transparent p-3">
          <div className="flex items-center gap-2 rounded-md bg-black/50 px-2 py-1 font-mono text-[11px] uppercase tracking-wider text-white/90 backdrop-blur">
            <span className="h-1.5 w-1.5 rounded-full bg-danger" />
            {source.location || source.name}
          </div>
          {playback?.type !== "youtube" && (
            <div className="rounded-md bg-black/50 px-2 py-1 font-mono text-[11px] text-white/80 backdrop-blur">{formatTime(current)}</div>
          )}
        </div>

        {playError && (
          <div className="absolute inset-0 grid place-items-center bg-black/80 p-6 text-center">
            <div>
              <AlertIcon className="mx-auto text-warn" width={22} height={22} />
              <p className="mt-2 text-sm">Your browser can&apos;t play this video format.</p>
              <p className="mt-1 text-xs text-muted">You can still ask questions: analysis runs on the server.</p>
            </div>
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 text-xs">
        <SessionPill session={session} />
        {analyzerIsMock && (
          <span className="rounded-full border border-warn/30 bg-warn/10 px-2.5 py-1 text-warn">
            Mock analyzer: add GEMINI_API_KEY for real answers
          </span>
        )}
        {typeof source.metadata.duration_seconds === "number" && (
          <span className="rounded-full border border-line px-2.5 py-1 text-muted">
            {formatTime(source.metadata.duration_seconds as number)} long
          </span>
        )}
      </div>
    </div>
  );
}

function SessionPill({ session }: { session: VideoSession | null }) {
  if (!session) return null;
  const map = {
    preparing: { label: "Preparing video for analysis…", cls: "border-line-2 text-muted", dot: "bg-warn animate-pulse" },
    ready: { label: "Ready: ask anything", cls: "border-accent/30 text-accent", dot: "bg-accent" },
    error: { label: session.status_message || "Could not prepare video", cls: "border-danger/30 text-danger", dot: "bg-danger" },
    expired: { label: "Re-preparing…", cls: "border-line-2 text-muted", dot: "bg-warn animate-pulse" },
  }[session.status];
  return (
    <span className={`inline-flex items-center gap-2 rounded-full border px-2.5 py-1 ${map.cls}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${map.dot}`} />
      {map.label}
    </span>
  );
}
