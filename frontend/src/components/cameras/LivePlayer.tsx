"use client";

// Live camera video over WebRTC (WHEP), served by the media server. Each (re)connection asks the backend
// for a fresh 5-minute ticket scoped to this one camera stream; the camera itself is never contacted.

import { useEffect, useRef, useState } from "react";
import { browserDecodesH265, cams, type LiveObject } from "@/lib/cameras";
import { AlertIcon, RefreshIcon } from "../icons";

type Status = "connecting" | "playing" | "error";

/** Viewer-side connection quality, from WebRTC statistics (what this browser actually receives). */
export type LiveStats = {
  rttMs: number | null;
  fps: number | null;
  kbps: number | null;
  lossPct: number | null;
  width: number | null;
  height: number | null;
  relayed: boolean;
  quality: "good" | "fair" | "poor";
};

function rate(s: Omit<LiveStats, "quality">): LiveStats["quality"] {
  if ((s.lossPct ?? 0) > 5 || (s.rttMs ?? 0) > 600 || (s.fps ?? 30) < 5) return "poor";
  if ((s.lossPct ?? 0) > 1 || (s.rttMs ?? 0) > 250 || (s.fps ?? 30) < 12) return "fair";
  return "good";
}

export function LivePlayer({
  cameraId,
  quality = "auto",
  objects,
  resolution,
  showBoxes = false,
  compact = false,
  label,
  muted = true,
  onStats,
}: {
  cameraId: string;
  quality?: "auto" | "main" | "sub";
  objects?: LiveObject[];
  resolution?: [number, number];
  showBoxes?: boolean;
  compact?: boolean;
  label?: string;
  muted?: boolean;
  onStats?: (s: LiveStats | null) => void;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const [status, setStatus] = useState<Status>("connecting");
  const [message, setMessage] = useState("");
  const [info, setInfo] = useState<{ stream: string; codec: string | null; transcoded: boolean } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const statsCb = useRef(onStats);
  useEffect(() => {
    statsCb.current = onStats;
  }, [onStats]);

  useEffect(() => {
    let pc: RTCPeerConnection | null = null;
    let sessionUrl: string | null = null;
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let statsTimer: ReturnType<typeof setInterval> | undefined;
    let prev: { bytes: number; at: number; lost: number; received: number } | null = null;

    const sample = async () => {
      if (!pc || !statsCb.current) return;
      const report = await pc.getStats();
      let video: RTCInboundRtpStreamStats | undefined;
      let pair: RTCIceCandidatePairStats | undefined;
      const byId = new Map<string, RTCStats>();
      report.forEach((r) => {
        byId.set(r.id, r);
        if (r.type === "inbound-rtp" && (r as RTCInboundRtpStreamStats).kind === "video") video = r as RTCInboundRtpStreamStats;
        if (r.type === "candidate-pair" && (r as RTCIceCandidatePairStats).nominated && (r as RTCIceCandidatePairStats).state === "succeeded") pair = r as RTCIceCandidatePairStats;
      });
      if (!video) return;
      const now = performance.now();
      const bytes = video.bytesReceived ?? 0;
      const lost = Math.max(0, video.packetsLost ?? 0);
      const received = video.packetsReceived ?? 0;
      const kbps = prev ? Math.round(((bytes - prev.bytes) * 8) / (now - prev.at)) : null;
      const newPackets = prev ? received - prev.received + (lost - prev.lost) : 0;
      const lossPct = prev && newPackets > 0 ? Math.round(((lost - prev.lost) / newPackets) * 1000) / 10 : null;
      prev = { bytes, at: now, lost, received };
      const local = pair ? (byId.get(pair.localCandidateId) as (RTCStats & { candidateType?: string }) | undefined) : undefined;
      const base = {
        rttMs: pair?.currentRoundTripTime != null ? Math.round(pair.currentRoundTripTime * 1000) : null,
        fps: video.framesPerSecond != null ? Math.round(video.framesPerSecond) : null,
        kbps,
        lossPct,
        width: video.frameWidth ?? null,
        height: video.frameHeight ?? null,
        relayed: local?.candidateType === "relay",
      };
      statsCb.current({ ...base, quality: rate(base) });
    };

    const fail = (text: string) => {
      if (cancelled) return;
      setStatus("error");
      setMessage(text);
      retry = setTimeout(() => setAttempt((a) => a + 1), Math.min(30000, 2000 * 2 ** Math.min(attempt, 4)));
    };

    (async () => {
      setStatus("connecting");
      try {
        const ticket = await cams.live(cameraId, quality, browserDecodesH265());
        if (cancelled) return;
        setInfo({ stream: ticket.stream, codec: ticket.codec, transcoded: ticket.transcoded });
        if (ticket.state !== "online" && ticket.state !== "connecting") {
          fail(ticket.message ?? "The camera is not sending video right now.");
          return;
        }
        pc = new RTCPeerConnection({ iceServers: ticket.ice_servers });
        pc.addTransceiver("video", { direction: "recvonly" });
        if (ticket.audio) pc.addTransceiver("audio", { direction: "recvonly" });
        pc.ontrack = (e) => {
          if (video.current && e.streams[0]) video.current.srcObject = e.streams[0];
        };
        pc.onconnectionstatechange = () => {
          if (!pc) return;
          if (pc.connectionState === "connected") {
            setStatus("playing");
            clearInterval(statsTimer);
            statsTimer = setInterval(sample, 2000);
          }
          if (pc.connectionState === "failed")
            fail("Unable to connect: the network between this device and Visionary blocks live video. A relay (TURN) server may be needed. Retrying…");
          if (pc.connectionState === "disconnected") fail("The live connection dropped. Reconnecting…");
        };
        await pc.setLocalDescription(await pc.createOffer());
        await waitForIce(pc);
        const res = await fetch(ticket.whep_url, { method: "POST", headers: { "Content-Type": "application/sdp" }, body: pc.localDescription?.sdp });
        if (!res.ok) {
          fail(res.status === 401 ? "Live view is not allowed for your account." : res.status === 404 ? "The camera stream is not available yet." : "Could not open live video.");
          return;
        }
        const loc = res.headers.get("Location");
        sessionUrl = loc ? new URL(loc, new URL(ticket.whep_url, window.location.href)).pathname : null;
        await pc.setRemoteDescription({ type: "answer", sdp: await res.text() });
      } catch (e) {
        fail(e instanceof Error ? e.message : "Could not open live video.");
      }
    })();

    return () => {
      cancelled = true;
      clearTimeout(retry);
      clearInterval(statsTimer);
      statsCb.current?.(null);
      pc?.close();
      if (sessionUrl) fetch(`/media/webrtc${sessionUrl}`, { method: "DELETE" }).catch(() => undefined);
    };
  }, [cameraId, quality, attempt]);

  return (
    <div className="relative h-full w-full bg-black">
      <video ref={video} autoPlay muted={muted} playsInline className="h-full w-full object-contain" aria-label={label ? `Live video: ${label}` : "Live video"} />
      {showBoxes && status === "playing" && objects && resolution && <LiveBoxes objects={objects} resolution={resolution} />}
      {status !== "playing" && (
        <div className="absolute inset-0 grid place-items-center bg-black/70 p-4 text-center" role="status">
          {status === "connecting" ? (
            <div className="flex items-center gap-2 text-sm text-white/80">
              <span className="h-2 w-2 animate-pulse rounded-full bg-accent" /> Connecting to live video…
            </div>
          ) : (
            <div className="max-w-xs">
              <AlertIcon className="mx-auto text-warn" width={compact ? 18 : 22} height={compact ? 18 : 22} aria-hidden />
              <p className={`mt-2 text-white/90 ${compact ? "text-xs" : "text-sm"}`}>{message}</p>
              <button onClick={() => setAttempt((a) => a + 1)} className="mt-3 inline-flex min-h-9 cursor-pointer items-center gap-1.5 rounded-lg bg-white/10 px-3 text-xs text-white hover:bg-white/20">
                <RefreshIcon width={13} height={13} aria-hidden /> Try again
              </button>
            </div>
          )}
        </div>
      )}
      {!compact && info && status === "playing" && (
        <div className="pointer-events-none absolute bottom-2 right-2 rounded bg-black/55 px-1.5 py-0.5 font-mono text-[10px] text-white/75">
          {info.stream === "web" ? "H.264 (converted)" : `${info.stream} · ${info.codec ?? ""}`}
        </div>
      )}
    </div>
  );
}

function waitForIce(pc: RTCPeerConnection): Promise<void> {
  // Send the offer with candidates included (WHEP without trickle); don't wait forever on slow interfaces.
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      if (pc.iceGatheringState === "complete") {
        pc.removeEventListener("icegatheringstatechange", done);
        resolve();
      }
    };
    pc.addEventListener("icegatheringstatechange", done);
    setTimeout(resolve, 2000);
  });
}

const BOX_COLOR: Record<string, string> = { person: "var(--series-people)", car: "var(--series-vehicles)", truck: "var(--series-vehicles)", bus: "var(--series-vehicles)" };

function LiveBoxes({ objects, resolution }: { objects: LiveObject[]; resolution: [number, number] }) {
  const [w, h] = resolution;
  return (
    <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="xMidYMid meet" aria-hidden>
      {objects.map((o) => {
        const [x1, y1, x2, y2] = o.bbox;
        const color = BOX_COLOR[o.label] ?? (o.detector === "wildlife" ? "var(--accent)" : "var(--warn)");
        const unsure = o.label.startsWith("possible") || o.label === "animal";
        return (
          <g key={`${o.detector}-${o.track_id}`}>
            <rect x={x1} y={y1} width={x2 - x1} height={y2 - y1} fill="none" stroke={color} strokeWidth={Math.max(2, w / 400)} strokeDasharray={unsure ? `${w / 120} ${w / 160}` : undefined} rx={w / 300} />
            <text x={x1 + 4} y={Math.max(y1 - 6, 14)} fill={color} fontSize={Math.max(12, w / 60)} fontFamily="system-ui" fontWeight={600} paintOrder="stroke" stroke="black" strokeWidth={3}>
              {o.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
