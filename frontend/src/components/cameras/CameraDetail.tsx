"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ago, clock, countsText, DIAGNOSIS_HINT, STATE_LABEL, cams, summarizeEvents, type Camera, type CameraEvent, type Diagnostics } from "@/lib/cameras";
import { BoxIcon, ChatIcon, FilmIcon, PulseIcon, RefreshIcon, SlidersIcon, SpeakerIcon, TrashIcon, UserIcon, XIcon } from "../icons";
import { StateBadge } from "./CameraGrid";
import { LivePlayer, type LiveStats } from "./LivePlayer";
import { CheckList, Problem } from "./AddCameraWizard";

type Tab = "activity" | "recordings" | "health" | "settings";
type Evidence = { title: string; at: string | null; clip: { start: string; duration: number } | null };

export function CameraDetail({ cameraId, onBack, onAsk, onDeleted }: { cameraId: string; onBack: () => void; onAsk: (c: Camera) => void; onDeleted: () => void }) {
  const [cam, setCam] = useState<Camera | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<Tab>("activity");
  const [quality, setQuality] = useState<"auto" | "main" | "sub">("auto");
  const [boxes, setBoxes] = useState(true);
  const [muted, setMuted] = useState(true);
  const [stats, setStats] = useState<LiveStats | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const stage = useRef<HTMLDivElement>(null);

  const load = useCallback(() => cams.get(cameraId).then(setCam).catch((e) => setError(e.message)), [cameraId]);
  useEffect(() => {
    const first = setTimeout(load, 0);
    const id = setInterval(load, 2000); // live AI state (what is visible now) + connection health
    return () => {
      clearTimeout(first);
      clearInterval(id);
    };
  }, [load]);

  if (error && !cam) return <div className="p-6"><Problem title="Camera not available" text={error} /></div>;
  if (!cam) return <div className="p-6 text-sm text-faint" role="status">Loading camera…</div>;

  const can = (p: string) => cam.permissions.includes(p);
  const caps = cam.capabilities;
  const live = cam.live;
  const isNvr = cam.kind === "nvr";

  return (
    <div className="mx-auto w-full max-w-7xl space-y-4 p-4 lg:p-6">
      <div className="flex flex-wrap items-center gap-3">
        <button onClick={onBack} className="min-h-9 cursor-pointer rounded-lg px-2 text-sm text-muted hover:bg-panel-2" aria-label="Back to all cameras">
          ← Cameras
        </button>
        <h1 className="min-w-0 truncate text-xl font-semibold tracking-tight">{cam.name}</h1>
        <StateBadge state={cam.state} />
        {cam.location && <span className="text-sm text-muted">{cam.location}</span>}
        <div className="ml-auto flex items-center gap-2">
          {can("ai_query") && !isNvr && (
            <button onClick={() => onAsk(cam)} className="inline-flex min-h-10 cursor-pointer items-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-accent-ink hover:brightness-110">
              <ChatIcon width={16} height={16} aria-hidden /> Ask about this camera
            </button>
          )}
        </div>
      </div>

      {isNvr ? (
        <NvrChannels cam={cam} />
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
          <div className="space-y-3">
            <div ref={stage} className="relative aspect-video overflow-hidden rounded-2xl border border-line bg-black">
              {can("live_view") ? (
                <LivePlayer cameraId={cam.id} quality={quality} objects={live?.objects} resolution={live?.resolution} showBoxes={boxes} label={cam.name} muted={muted} onStats={setStats} />
              ) : (
                <p className="grid h-full place-items-center text-sm text-muted">Your role does not include live view.</p>
              )}
              {stats && <QualityBadge stats={stats} />}
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <div role="radiogroup" aria-label="Video quality" className="inline-flex rounded-lg border border-line p-0.5">
                {(["auto", "main", "sub"] as const).map((q) => (
                  <button key={q} role="radio" aria-checked={quality === q} onClick={() => setQuality(q)} className={`min-h-8 cursor-pointer rounded-md px-2.5 ${quality === q ? "bg-panel-2 text-text" : "text-muted hover:text-text"}`}>
                    {{ auto: "Auto", main: "High quality", sub: "Data saver" }[q]}
                  </button>
                ))}
              </div>
              <ToolButton active={boxes} onClick={() => setBoxes(!boxes)} label="AI boxes">
                <BoxIcon width={13} height={13} aria-hidden />
              </ToolButton>
              {caps.audio && can("audio") && (
                <ToolButton active={!muted} onClick={() => setMuted(!muted)} label={muted ? "Sound off" : "Sound on"}>
                  <SpeakerIcon width={13} height={13} aria-hidden />
                </ToolButton>
              )}
              <button
                onClick={() => {
                  const a = document.createElement("a"); // saved by the browser; the picture comes from the server
                  a.href = cams.snapshotUrl(cam.id, Date.now());
                  a.download = `${cam.name}-${new Date().toISOString().slice(0, 19).replace(/:/g, "-")}.jpg`;
                  a.click();
                }}
                className="inline-flex min-h-9 cursor-pointer items-center gap-1.5 rounded-lg px-2.5 text-muted hover:bg-panel-2 hover:text-text"
              >
                Snapshot
              </button>
              <button onClick={() => stage.current?.requestFullscreen?.()} className="inline-flex min-h-9 cursor-pointer items-center gap-1.5 rounded-lg px-2.5 text-muted hover:bg-panel-2 hover:text-text">
                Full screen
              </button>
              <span className="ml-auto text-faint">{cam.health.bitrate_kbps ? `${(cam.health.bitrate_kbps / 1000).toFixed(1)} Mbit/s from the camera` : ""}</span>
            </div>
            {caps.ptz && can("ptz") && <PtzPad cameraId={cam.id} />}
          </div>

          <aside className="space-y-3">
            <section className="rounded-xl border border-line bg-panel p-4" aria-labelledby="now-h">
              <h2 id="now-h" className="text-xs font-medium uppercase tracking-wider text-faint">Right now</h2>
              {cam.state !== "online" ? (
                <p className="mt-2 text-sm text-muted">{DIAGNOSIS_HINT[cam.health.diagnosis ?? ""] ?? `${STATE_LABEL[cam.state].label}. Last seen ${ago(cam.last_seen_at)}.`}</p>
              ) : !live ? (
                <p className="mt-2 text-sm text-muted">{caps.ai ? "Live AI is starting…" : "AI is switched off for this camera."}</p>
              ) : (
                <>
                  <p className="mt-2 text-2xl font-semibold tracking-tight">{countsText(live.counts)}</p>
                  {live.objects.some((o) => !(o.label in live.counts)) && (
                    <p className="mt-1 text-xs text-muted">+ {live.objects.filter((o) => !(o.label in live.counts)).length} object(s) the AI is not sure about</p>
                  )}
                  <p className="mt-2 text-xs text-faint">Updated {clock(live.at)} · AI reads {live.ai_fps ?? "?"} frames/s</p>
                </>
              )}
            </section>
            <div role="tablist" aria-label="Camera details" className="flex gap-1 rounded-lg border border-line p-0.5 text-xs">
              {([
                ["activity", "Activity", PulseIcon],
                ["recordings", "Playback", FilmIcon],
                ["health", "Connection", SlidersIcon],
                ["settings", "Settings", UserIcon],
              ] as const).map(([id, label, Icon]) => (
                <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)} className={`inline-flex min-h-8 flex-1 cursor-pointer items-center justify-center gap-1 rounded-md ${tab === id ? "bg-panel-2 text-text" : "text-muted hover:text-text"}`}>
                  <Icon width={12} height={12} aria-hidden /> {label}
                </button>
              ))}
            </div>
            <div role="tabpanel" className="rounded-xl border border-line bg-panel p-3">
              {tab === "activity" && <Activity cam={cam} onOpen={setEvidence} />}
              {tab === "recordings" && <Recordings cam={cam} />}
              {tab === "health" && <Health cam={cam} stats={stats} onChanged={load} />}
              {tab === "settings" && <Settings cam={cam} onChanged={load} onDeleted={onDeleted} />}
            </div>
          </aside>
        </div>
      )}
      {evidence && <EvidenceDialog cam={cam} evidence={evidence} onClose={() => setEvidence(null)} />}
    </div>
  );
}

function ToolButton({ active, onClick, label, children }: { active: boolean; onClick: () => void; label: string; children: React.ReactNode }) {
  return (
    <button onClick={onClick} aria-pressed={active} className={`inline-flex min-h-9 cursor-pointer items-center gap-1.5 rounded-lg px-2.5 ${active ? "bg-accent/15 text-accent" : "text-muted hover:bg-panel-2 hover:text-text"}`}>
      {children} {label}
    </button>
  );
}

function QualityBadge({ stats }: { stats: LiveStats }) {
  const tone = { good: "bg-accent", fair: "bg-warn", poor: "bg-danger" }[stats.quality];
  const detail = [stats.rttMs != null ? `${stats.rttMs} ms` : null, stats.fps != null ? `${stats.fps} fps` : null, stats.lossPct ? `${stats.lossPct}% lost` : null, stats.relayed ? "relayed" : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="pointer-events-none absolute left-2 top-2 inline-flex items-center gap-1.5 rounded-md bg-black/65 px-2 py-1 text-[11px] text-white/90 backdrop-blur" title={detail}>
      <span className={`h-1.5 w-1.5 rounded-full ${tone}`} aria-hidden />
      {{ good: "Good connection", fair: "Fair connection", poor: "Poor connection" }[stats.quality]}
      <span className="hidden text-white/60 sm:inline">· {detail}</span>
    </div>
  );
}

function PtzPad({ cameraId }: { cameraId: string }) {
  const [presets, setPresets] = useState<{ token: string; name: string }[]>([]);
  const [msg, setMsg] = useState("");
  useEffect(() => {
    cams.ptzInfo(cameraId).then((r) => setPresets(r.presets)).catch((e) => setMsg(e.message));
  }, [cameraId]);
  const move = (pan: number, tilt: number, zoom: number) => cams.ptz(cameraId, { action: "move", pan, tilt, zoom }).catch((e) => setMsg(e.message));
  const stop = () => cams.ptz(cameraId, { action: "stop" }).catch(() => undefined);
  // hold to move, release to stop (pointer + keyboard)
  const hold = (pan: number, tilt: number, zoom: number, label: string, text: string) => (
    <button
      aria-label={label}
      onPointerDown={() => move(pan, tilt, zoom)}
      onPointerUp={stop}
      onPointerLeave={stop}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && !e.repeat && move(pan, tilt, zoom)}
      onKeyUp={(e) => (e.key === "Enter" || e.key === " ") && stop()}
      className="grid h-10 w-10 cursor-pointer select-none place-items-center rounded-lg bg-panel-2 text-sm hover:bg-line active:bg-accent/20"
    >
      {text}
    </button>
  );
  return (
    <section aria-label="Pan, tilt and zoom" className="flex w-fit max-w-full flex-wrap items-center gap-4 rounded-xl border border-line bg-panel p-3">
      <div className="grid grid-cols-3 gap-1">
        <span />
        {hold(0, 0.5, 0, "Tilt up", "▲")}
        <span />
        {hold(-0.5, 0, 0, "Pan left", "◀")}
        <span className="grid h-10 w-10 place-items-center text-[10px] text-faint">PTZ</span>
        {hold(0.5, 0, 0, "Pan right", "▶")}
        <span />
        {hold(0, -0.5, 0, "Tilt down", "▼")}
        <span />
      </div>
      <div className="flex flex-col gap-1">
        {hold(0, 0, 0.5, "Zoom in", "+")}
        {hold(0, 0, -0.5, "Zoom out", "−")}
      </div>
      {presets.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className="text-faint">Presets</span>
          {presets.map((p) => (
            <button key={p.token} onClick={() => cams.ptz(cameraId, { action: "preset", preset: p.token }).catch((e) => setMsg(e.message))} className="min-h-8 cursor-pointer rounded-md bg-panel-2 px-2.5 hover:bg-line">
              {p.name}
            </button>
          ))}
        </div>
      )}
      <p className="w-full text-xs text-faint">Hold a button to move; release to stop.</p>
      {msg && <p className="w-full text-xs text-danger">{msg}</p>}
    </section>
  );
}

function NvrChannels({ cam }: { cam: Camera }) {
  return (
    <section className="rounded-xl border border-line bg-panel p-4">
      <h2 className="text-sm font-medium">Channels</h2>
      <p className="mt-1 text-xs text-muted">Each recorder channel is its own camera in the camera list.</p>
      <ul className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {(cam.channels ?? []).map((c) => (
          <li key={c.id} className="flex items-center justify-between rounded-lg border border-line px-3 py-2 text-sm">
            {c.name.split(" · ").pop()} <StateBadge state={c.state} small />
          </li>
        ))}
      </ul>
    </section>
  );
}

export function Activity({ cam, onOpen }: { cam: Camera; onOpen?: (e: Evidence) => void }) {
  const [minutes, setMinutes] = useState(60);
  const [events, setEvents] = useState<CameraEvent[] | null>(null);
  useEffect(() => {
    const load = () => cams.events(cam.id, minutes).then(setEvents).catch(() => setEvents([]));
    const first = setTimeout(load, 0);
    const id = setInterval(load, 10000);
    return () => {
      clearTimeout(first);
      clearInterval(id);
    };
  }, [cam.id, minutes]);
  const byKey = new Map((events ?? []).map((e) => [e.id, e]));
  const shown = summarizeEvents(events ?? []);
  return (
    <div>
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium">What happened</p>
        <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} className="min-h-8 rounded-md border border-line-2 bg-bg px-2 text-xs" aria-label="Time range">
          <option value={20}>Last 20 min</option>
          <option value={60}>Last hour</option>
          <option value={360}>Last 6 hours</option>
          <option value={1440}>Last 24 hours</option>
        </select>
      </div>
      {events === null ? (
        <p className="mt-3 text-xs text-faint">Loading…</p>
      ) : shown.length === 0 ? (
        <p className="mt-3 text-sm text-muted">Nothing in this period.</p>
      ) : (
        <ol className="mt-2 max-h-80 space-y-0.5 overflow-y-auto pr-1">
          {shown.slice(0, 80).map((l) => {
            const first = byKey.get(l.firstId);
            const clip = first?.clip ?? null;
            return (
              <li key={l.key}>
                <button
                  onClick={() => onOpen?.({ title: l.text, at: l.at, clip })}
                  className="flex w-full cursor-pointer gap-2 rounded-md px-1.5 py-1 text-left text-sm hover:bg-panel-2"
                  title={clip ? "Open the recording of this moment" : "Recording is off: no video for this moment"}
                >
                  <span className="w-16 shrink-0 font-mono text-xs leading-5 text-faint">{clock(l.at)}</span>
                  <span className="min-w-0 flex-1">{l.text}</span>
                  {first?.evidence === "device" && <span className="h-5 shrink-0 self-start rounded bg-panel-2 px-1.5 text-[10px] leading-5 text-muted">camera</span>}
                  {clip && <FilmIcon width={12} height={12} className="mt-1 shrink-0 text-faint" aria-hidden />}
                </button>
              </li>
            );
          })}
        </ol>
      )}
      <p className="mt-2 text-[11px] text-faint">“camera” marks alarms reported by the camera itself; the rest come from Visionary&apos;s AI.</p>
    </div>
  );
}

function EvidenceDialog({ cam, evidence, onClose }: { cam: Camera; evidence: Evidence; onClose: () => void }) {
  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/75 p-4" role="dialog" aria-modal="true" aria-labelledby="evidence-title">
      <div className="w-full max-w-3xl overflow-hidden rounded-2xl border border-line bg-panel">
        <header className="flex items-center gap-3 border-b border-line px-4 py-3">
          <h2 id="evidence-title" className="min-w-0 flex-1 truncate text-sm font-semibold">
            {evidence.title} <span className="font-normal text-muted">· {cam.name} · {clock(evidence.at)}</span>
          </h2>
          <button onClick={onClose} className="grid h-9 w-9 cursor-pointer place-items-center rounded-lg text-muted hover:bg-panel-2" aria-label="Close">
            <XIcon width={16} height={16} />
          </button>
        </header>
        {evidence.clip && cam.permissions.includes("playback") ? (
          <video src={cams.playbackUrl(cam.id, evidence.clip.start, evidence.clip.duration)} controls autoPlay className="aspect-video w-full bg-black" aria-label="Recorded video of this moment" />
        ) : (
          <p className="p-6 text-sm text-muted">{cam.permissions.includes("playback") ? "Recording was off for this camera, so there is no video of this moment." : "Your role does not include playback."}</p>
        )}
        <p className="px-4 py-2 text-xs text-faint">From 5 seconds before to 15 seconds after the event.</p>
      </div>
    </div>
  );
}

export function Recordings({ cam }: { cam: Camera }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof cams.recordings>> | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  const [when, setWhen] = useState(() => toLocalInput(new Date(Date.now() - 5 * 60000)));
  const [msg, setMsg] = useState("");
  const can = cam.permissions.includes("playback");
  useEffect(() => {
    if (can) cams.recordings(cam.id).then(setData).catch(() => setData({ recording: false, retention: "", spans: [] }));
  }, [cam.id, can]);
  if (!can) return <p className="text-sm text-muted">Your role does not include playback.</p>;
  if (!data) return <p className="text-xs text-faint">Loading…</p>;
  if (!data.recording) return <p className="text-sm text-muted">Recording is off for this camera. Turn it on in Settings.</p>;
  const spans = [...data.spans].reverse();
  const playAt = () => {
    const t = new Date(when).getTime();
    const span = data.spans.find((s) => t >= new Date(s.start).getTime() && t < new Date(s.start).getTime() + s.duration * 1000);
    if (!span) {
      setMsg("No recording at that time.");
      setPlaying(null);
      return;
    }
    setMsg("");
    const left = (new Date(span.start).getTime() + span.duration * 1000 - t) / 1000;
    setPlaying(cams.playbackUrl(cam.id, new Date(t).toISOString(), Math.max(5, Math.min(300, left))));
  };
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted">
          Date and time
          <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} className="mt-1 block min-h-9 rounded-md border border-line-2 bg-bg px-2 text-sm text-text" />
        </label>
        <button onClick={playAt} className="min-h-9 cursor-pointer rounded-md bg-accent px-3 text-sm font-medium text-accent-ink hover:brightness-110">
          Play
        </button>
      </div>
      {msg && <p className="text-xs text-warn">{msg}</p>}
      {playing && <video key={playing} src={playing} controls autoPlay className="aspect-video w-full rounded-lg bg-black" aria-label="Recorded video" />}
      <p className="text-xs text-muted">Video is kept for {data.retention}.</p>
      {spans.length === 0 ? (
        <p className="text-sm text-muted">No recordings yet.</p>
      ) : (
        <ul className="max-h-56 space-y-1 overflow-y-auto">
          {spans.map((s) => (
            <li key={s.start} className="flex items-center justify-between gap-2 rounded-md px-1.5 py-1 text-sm hover:bg-panel-2">
              <span>
                {new Date(s.start).toLocaleDateString()} {clock(s.start)} <span className="text-faint">· {Math.max(1, Math.round(s.duration / 60))} min</span>
              </span>
              <button onClick={() => setPlaying(cams.playbackUrl(cam.id, s.start, Math.min(300, s.duration)))} className="min-h-8 cursor-pointer rounded-md px-2 text-xs text-accent hover:bg-accent/10">
                Play
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function Health({ cam, stats, onChanged }: { cam: Camera; stats: LiveStats | null; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [diagError, setDiagError] = useState("");
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    await fn().catch(() => undefined);
    setBusy(false);
    onChanged();
  };
  const run = async () => {
    setBusy(true);
    setDiag(null);
    setDiagError("");
    try {
      setDiag(await cams.diagnostics(cam.id));
    } catch (e) {
      setDiagError(e instanceof Error ? e.message : "Diagnostics failed");
    } finally {
      setBusy(false);
    }
  };
  const h = cam.health;
  const rows: [string, string][] = [
    ["State", STATE_LABEL[cam.state].label],
    ["Diagnosis", h.diagnosis === "OK" || !h.diagnosis ? "No problem found" : DIAGNOSIS_HINT[h.diagnosis] ?? h.diagnosis],
    ["Last seen", ago(cam.last_seen_at)],
    ["Reconnects", `${h.reconnect_count ?? 0}${h.last_reconnect ? ` · last ${ago(h.last_reconnect)}` : ""}`],
    ["Last disconnect", h.last_disconnect ? `${new Date(h.last_disconnect).toLocaleString()}` : "—"],
    ["Video format", (h.codecs ?? []).join(", ") || cam.streams.map((s) => s.codec).filter(Boolean).join(", ") || "—"],
    ["Connection", `${cam.connection.type?.toUpperCase() ?? "?"}${cam.gateway_id ? " via gateway" : ""}`],
    ["Device", [cam.device.manufacturer, cam.device.model, cam.device.firmware].filter(Boolean).join(" · ") || "—"],
    ["Your view", stats ? `${stats.quality} · ${stats.rttMs ?? "?"} ms · ${stats.fps ?? "?"} fps · ${stats.lossPct ?? 0}% lost${stats.relayed ? " · relayed" : ""}` : "not watching"],
    ["Live AI", cam.live ? `${cam.live.status}, ${cam.live.latency_ms ?? "?"} ms per frame` : "off"],
  ];
  return (
    <div className="space-y-3">
      <dl className="space-y-1.5 text-sm">
        {rows.map(([k, v]) => (
          <div key={k} className="flex justify-between gap-3">
            <dt className="shrink-0 text-muted">{k}</dt>
            <dd className="text-right">{v}</dd>
          </div>
        ))}
      </dl>
      <button onClick={run} disabled={busy} className="inline-flex min-h-9 w-full cursor-pointer items-center justify-center gap-1.5 rounded-lg bg-panel-2 text-sm hover:bg-line disabled:opacity-60">
        <RefreshIcon width={13} height={13} aria-hidden /> {busy && !diag ? "Testing the camera…" : "Run connection diagnostics"}
      </button>
      {diagError && <Problem title="Diagnostics could not run" text={diagError} />}
      {diag && (
        <div className="space-y-2">
          {!diag.ok && <Problem title={diag.explanation} text={diag.next_step} />}
          <CheckList checks={diag.checks} />
        </div>
      )}
      {cam.permissions.includes("camera_settings") && (
        <div className="flex gap-2">
          {cam.state === "disconnected" ? (
            <button disabled={busy} onClick={() => act(() => cams.connect(cam.id))} className="min-h-9 flex-1 cursor-pointer rounded-lg bg-panel-2 text-sm hover:bg-line">
              Connect
            </button>
          ) : (
            <>
              <button disabled={busy} onClick={() => act(() => cams.connect(cam.id))} className="min-h-9 flex-1 cursor-pointer rounded-lg bg-panel-2 text-sm hover:bg-line">
                Reconnect now
              </button>
              <button disabled={busy} onClick={() => act(() => cams.disconnect(cam.id))} className="min-h-9 flex-1 cursor-pointer rounded-lg text-sm text-muted hover:bg-panel-2">
                Pause camera
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

const AI_TOGGLES: [string, string][] = [
  ["people", "People"],
  ["vehicles", "Vehicles"],
  ["objects", "Other objects"],
  ["wildlife", "Wildlife and species"],
  ["behavior", "Behaviour events (zones, loitering, crowds)"],
  ["record", "Record video"],
];

function Settings({ cam, onChanged, onDeleted }: { cam: Camera; onChanged: () => void; onDeleted: () => void }) {
  const [shares, setShares] = useState<{ id: string; email: string; role: string }[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("viewer");
  const [msg, setMsg] = useState("");
  const [pw, setPw] = useState({ username: "", password: "" });
  const [link, setLink] = useState("");
  const [linkBusy, setLinkBusy] = useState(false);
  const linkCamera = cam.kind === "camera_rtsp" || cam.kind === "camera_hls";
  const canShare = cam.permissions.includes("share");
  const canEdit = cam.permissions.includes("camera_settings");
  useEffect(() => {
    if (canShare) cams.shares(cam.id).then(setShares).catch(() => undefined);
  }, [cam.id, canShare]);
  const profile = cam.ai_profile;
  const on = (k: string) => (k === "record" || k === "behavior" || k === "people" || k === "vehicles" || k === "objects" ? profile[k] !== false : !!profile[k]);
  const toggle = async (key: string, value: boolean) => {
    const next = { ...Object.fromEntries(AI_TOGGLES.map(([k]) => [k, on(k)])), [key]: value } as Record<string, boolean>;
    const patch = { [key]: value, general: next.people || next.vehicles || next.objects, ...(key === "wildlife" ? { species: value } : {}) };
    await cams.update(cam.id, { ai_profile: patch }).catch((e) => setMsg(e.message));
    onChanged();
  };
  return (
    <div className="space-y-4 text-sm">
      {canEdit && (
        <fieldset className="space-y-1.5">
          <legend className="text-xs font-medium uppercase tracking-wider text-faint">AI on this camera</legend>
          {AI_TOGGLES.map(([k, label]) => (
            <label key={k} className="flex min-h-8 cursor-pointer items-center gap-2">
              <input type="checkbox" checked={on(k)} onChange={(e) => toggle(k, e.target.checked)} className="h-4 w-4 accent-[var(--accent)]" />
              {label}
            </label>
          ))}
          <p className="text-xs text-faint">Only what is switched on uses the GPU. Tracking is always on (the events need it).</p>
        </fieldset>
      )}
      {canEdit && (
        <form
          className="space-y-1.5"
          onSubmit={async (e) => {
            e.preventDefault();
            await cams.update(cam.id, pw).then(() => setMsg("Login updated. Reconnecting…")).catch((err) => setMsg(err.message));
            setPw({ username: "", password: "" });
            onChanged();
          }}
        >
          <p className="text-xs font-medium uppercase tracking-wider text-faint">Camera login</p>
          <div className="flex gap-1.5">
            <input value={pw.username} onChange={(e) => setPw({ ...pw, username: e.target.value })} placeholder="Username" aria-label="Camera username" autoComplete="off" className="min-h-9 min-w-0 flex-1 rounded-md border border-line-2 bg-bg px-2" />
            <input value={pw.password} onChange={(e) => setPw({ ...pw, password: e.target.value })} placeholder="New password" aria-label="New camera password" type="password" autoComplete="new-password" className="min-h-9 min-w-0 flex-1 rounded-md border border-line-2 bg-bg px-2" />
          </div>
          <button disabled={!pw.password && !pw.username} className="min-h-8 cursor-pointer rounded-md bg-panel-2 px-2.5 text-xs hover:bg-line disabled:opacity-50">Save login</button>
        </form>
      )}
      {canEdit && linkCamera && (
        <form
          className="space-y-1.5"
          onSubmit={async (e) => {
            e.preventDefault();
            setLinkBusy(true);
            setMsg("Testing the new link…");
            try {
              await cams.update(cam.id, { url: link.trim() });
              setMsg("New link works. The camera is reconnecting.");
              setLink("");
            } catch (err) {
              setMsg(err instanceof Error ? err.message : "The new link does not work.");
            } finally {
              setLinkBusy(false);
              onChanged();
            }
          }}
        >
          <label htmlFor="new-link" className="text-xs font-medium uppercase tracking-wider text-faint">Stream link</label>
          <p className="font-mono text-xs text-muted">{cam.connection.address}</p>
          <div className="flex gap-1.5">
            <input id="new-link" value={link} onChange={(e) => setLink(e.target.value)} placeholder={cam.kind === "camera_hls" ? "https://…/index.m3u8" : "rtsp://…"} spellCheck={false} autoComplete="off" className="min-h-9 min-w-0 flex-1 rounded-md border border-line-2 bg-bg px-2 font-mono text-xs" />
            <button disabled={linkBusy || link.trim().length < 8} className="min-h-9 cursor-pointer rounded-md bg-panel-2 px-2.5 text-xs hover:bg-line disabled:opacity-50">Replace</button>
          </div>
          <p className="text-xs text-faint">The new link is tested first; the camera keeps its history and settings.</p>
        </form>
      )}
      {canEdit && (
        <button
          onClick={() => cams.refresh(cam.id).then(() => setMsg("Capabilities updated from the camera.")).catch((e) => setMsg(e.message)).finally(onChanged)}
          className="inline-flex min-h-8 cursor-pointer items-center gap-1.5 rounded-md bg-panel-2 px-2.5 text-xs hover:bg-line"
        >
          <RefreshIcon width={12} height={12} aria-hidden /> Re-read what the camera supports
        </button>
      )}
      {canShare && (
        <div className="space-y-1.5">
          <p className="text-xs font-medium uppercase tracking-wider text-faint">Shared with</p>
          {shares.length === 0 && <p className="text-muted">Only you.</p>}
          <ul className="space-y-1">
            {shares.map((s) => (
              <li key={s.id} className="flex items-center justify-between gap-2">
                <span className="truncate">
                  {s.email} <span className="text-faint">· {s.role}</span>
                </span>
                <button onClick={() => cams.unshare(cam.id, s.id).then(() => setShares(shares.filter((x) => x.id !== s.id)))} className="min-h-8 cursor-pointer rounded px-2 text-xs text-danger hover:bg-danger/10">
                  Remove
                </button>
              </li>
            ))}
          </ul>
          <form
            className="flex gap-1.5"
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                await cams.share(cam.id, email, role);
                setShares(await cams.shares(cam.id));
                setEmail("");
              } catch (err) {
                setMsg(err instanceof Error ? err.message : "Could not share");
              }
            }}
          >
            <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="Email" type="email" required aria-label="Share with email" className="min-h-9 min-w-0 flex-1 rounded-md border border-line-2 bg-bg px-2" />
            <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="Role" className="min-h-9 rounded-md border border-line-2 bg-bg px-1.5 text-xs">
              <option value="viewer">Viewer</option>
              <option value="operator">Operator</option>
              {cam.role === "owner" && <option value="admin">Admin</option>}
            </select>
            <button className="min-h-9 cursor-pointer rounded-md bg-panel-2 px-2.5 text-xs hover:bg-line">Share</button>
          </form>
          <p className="text-xs text-faint">Viewers: live, recordings, questions. Operators also download and control PTZ. Admins also change settings.</p>
        </div>
      )}
      {msg && <p className="text-xs text-muted" role="status">{msg}</p>}
      {cam.permissions.includes("delete") && (
        <button
          onClick={async () => {
            if (!window.confirm(`Remove “${cam.name}”? Its AI history is deleted now; recorded video is deleted when it expires.`)) return;
            await cams.remove(cam.id);
            onDeleted();
          }}
          className="inline-flex min-h-9 cursor-pointer items-center gap-1.5 rounded-lg px-2.5 text-xs text-danger hover:bg-danger/10"
        >
          <TrashIcon width={13} height={13} aria-hidden /> Remove camera
        </button>
      )}
    </div>
  );
}
