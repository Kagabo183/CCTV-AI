"use client";

import { useCallback, useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";
import { formatTime } from "@/lib/api";
import type { BoxTrack, VideoSession, VideoSource } from "@/lib/types";
import { loadYouTubeApi, youtubeIdFromEmbed, type YTPlayer } from "@/lib/youtube";
import { BoxOverlay } from "./BoxOverlay";
import { AlertIcon, BoxIcon, CameraIcon } from "./icons";

export type VideoStageHandle = { seek: (seconds: number) => void };

// Keyed by source id in the parent, so local state resets per video.
type Props = {
  source: VideoSource | null;
  session: VideoSession | null;
  analyzerIsMock: boolean;
  overlay?: { boxes: BoxTrack; label: string; highlightTrack?: number | null } | null;
  /** Detections exist for this video: show the on-video toggle. */
  boxesAvailable?: boolean;
  showBoxes?: boolean;
  onToggleBoxes?: (v: boolean) => void;
  technical?: boolean;
  onTime?: (seconds: number) => void;
  onRetryImport?: () => void;
  ref?: Ref<VideoStageHandle>;
};

export function VideoStage({ source, analyzerIsMock, overlay, boxesAvailable, showBoxes, onToggleBoxes, technical, onTime, onRetryImport, ref }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const ytHost = useRef<HTMLDivElement>(null);
  const ytPlayer = useRef<YTPlayer | null>(null);
  const [ytReady, setYtReady] = useState(false);
  const [current, setCurrent] = useState(0);
  const [playError, setPlayError] = useState(false);
  const youtubeId = source?.playback?.type === "youtube" ? ((source.metadata.youtube_id as string | undefined) ?? youtubeIdFromEmbed(source.playback.url)) : null;

  // YouTube: the IFrame API player, so we can read its time (overlay, "ask about 0:42") and seek.
  useEffect(() => {
    if (!youtubeId || !ytHost.current) return;
    let cancelled = false;
    const mount = document.createElement("div");
    ytHost.current.appendChild(mount);
    loadYouTubeApi()
      .then((YT) => {
        if (cancelled) return;
        ytPlayer.current = new YT.Player(mount, {
          videoId: youtubeId,
          width: "100%",
          height: "100%",
          playerVars: { rel: 0, modestbranding: 1, playsinline: 1, origin: window.location.origin },
          events: { onReady: () => !cancelled && setYtReady(true), onError: () => !cancelled && setPlayError(true) },
        });
      })
      .catch(() => !cancelled && setPlayError(true));
    return () => {
      cancelled = true;
      ytPlayer.current?.destroy();
      ytPlayer.current = null;
      mount.remove();
    };
  }, [youtubeId]);

  useEffect(() => {
    if (!ytReady) return;
    const id = setInterval(() => {
      const t = ytPlayer.current?.getCurrentTime();
      if (typeof t === "number") {
        setCurrent(t);
        onTime?.(t);
      }
    }, 250);
    return () => clearInterval(id);
  }, [ytReady, onTime]);

  const clock = useCallback((): number | null => {
    if (youtubeId) return ytReady && ytPlayer.current ? ytPlayer.current.getCurrentTime() : null;
    return videoRef.current ? videoRef.current.currentTime : null;
  }, [youtubeId, ytReady]);

  useImperativeHandle(
    ref,
    () => ({
      seek(seconds: number) {
        if (youtubeId) {
          ytPlayer.current?.seekTo(seconds, true);
          ytPlayer.current?.playVideo();
          return;
        }
        const v = videoRef.current;
        if (v) {
          v.currentTime = seconds;
          void v.play().catch(() => undefined);
        }
      },
    }),
    [youtubeId],
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

  if (source.status === "importing" || source.status === "error") {
    const pct = Math.round((source.import_progress ?? 0) * 100);
    const live = Boolean(source.metadata.is_live);
    return (
      <div className="grid aspect-video w-full place-items-center rounded-2xl border border-line bg-panel p-6">
        <div className="w-full max-w-sm text-center">
          {source.status === "importing" ? (
            <>
              <p className="text-sm font-medium">{live ? "Recording a clip from the stream…" : "Importing video…"}</p>
              <p className="mt-1 text-xs text-muted">Getting a copy that plays here. Analysis starts automatically when it is ready.</p>
              <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-line-2">
                {pct > 0 ? (
                  <div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${pct}%` }} />
                ) : (
                  <div className="h-full w-1/3 animate-pulse rounded-full bg-accent/60" />
                )}
              </div>
              <p className="mt-2 font-mono text-[11px] text-faint">{pct > 0 ? `${pct}%` : "starting…"}</p>
            </>
          ) : (
            <>
              <AlertIcon className="mx-auto text-danger" width={22} height={22} />
              <p className="mt-2 text-sm font-medium">This video could not be imported</p>
              <p className="mt-1 text-xs text-muted">{source.status_message}</p>
              {onRetryImport && source.kind !== "upload" && (
                <button onClick={onRetryImport} className="mt-4 rounded-lg border border-line-2 px-3 py-1.5 text-xs text-muted hover:text-text">
                  Try again
                </button>
              )}
            </>
          )}
        </div>
      </div>
    );
  }

  const playback = source.playback;
  return (
    <div className="space-y-3">
      <div className="relative aspect-video w-full overflow-hidden rounded-2xl border border-line bg-black">
        {youtubeId ? (
          <div ref={ytHost} className="h-full w-full [&>iframe]:h-full [&>iframe]:w-full" title={source.name} />
        ) : playback ? (
          <video
            key={source.id}
            ref={videoRef}
            src={playback.url}
            controls
            playsInline
            preload="metadata"
            className="h-full w-full bg-black object-contain"
            onTimeUpdate={(e) => {
              setCurrent(e.currentTarget.currentTime);
              onTime?.(e.currentTarget.currentTime);
            }}
            onError={() => setPlayError(true)}
          />
        ) : null}
        {overlay && playback && !playError && <BoxOverlay clock={clock} boxes={overlay.boxes} label={overlay.label} highlightTrack={overlay.highlightTrack ?? null} technical={technical} />}

        <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between bg-gradient-to-b from-black/70 to-transparent p-3">
          <div className={`hidden max-w-[50%] items-center gap-2 truncate rounded-md bg-black/50 px-2 py-1 font-mono text-[11px] uppercase tracking-wider text-white/90 backdrop-blur ${youtubeId ? "" : "sm:flex"}`}>
            <span className="h-1.5 w-1.5 rounded-full bg-danger" />
            {source.location || source.name}
          </div>
          <div className="pointer-events-auto ml-auto flex items-center gap-2">
            {boxesAvailable && onToggleBoxes && (
              <button
                onClick={() => onToggleBoxes(!showBoxes)}
                aria-pressed={showBoxes}
                className={`inline-flex min-h-8 cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-md px-2.5 text-xs backdrop-blur transition ${
                  showBoxes ? "bg-accent/90 text-accent-ink" : "bg-black/50 text-white/85 hover:bg-black/70"
                }`}
              >
                <BoxIcon width={13} height={13} aria-hidden /> {showBoxes ? "Hide" : "Show"} detections
              </button>
            )}
            <div className="rounded-md bg-black/50 px-2 py-1 font-mono text-[11px] text-white/80 backdrop-blur">{formatTime(current)}</div>
          </div>
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

      {analyzerIsMock && (
        <p className="rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">Mock analyzer: answers are placeholders until a video AI is configured.</p>
      )}
    </div>
  );
}

export function SessionPill({ session }: { session: VideoSession | null }) {
  if (!session) return null;
  const map = {
    preparing: { label: "Preparing video for analysis…", cls: "border-line-2 text-muted", dot: "bg-warn animate-pulse" },
    ready: { label: "Ready for questions", cls: "border-accent/30 text-accent", dot: "bg-accent" },
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
