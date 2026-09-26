"use client";

import { useEffect, useState } from "react";
import { ago, clock, countsText, DIAGNOSIS_HINT, STATE_LABEL, cams, summarizeEvents, type Camera, type CameraEvent, type Overview } from "@/lib/cameras";
import { CameraIcon, PlusIcon, ServerIcon } from "../icons";

export function StateBadge({ state, small = false, onMedia = false }: { state: Camera["state"]; small?: boolean; onMedia?: boolean }) {
  const s = STATE_LABEL[state] ?? STATE_LABEL.connecting;
  // over video/pictures the badge needs its own dark backdrop to stay readable on bright scenes
  const tone = onMedia ? "text-white border-white/10 bg-black/70 backdrop-blur" : { ok: "text-accent border-accent/30 bg-accent/10", warn: "text-warn border-warn/30 bg-warn/10", bad: "text-danger border-danger/30 bg-danger/10", idle: "text-muted border-line-2 bg-panel-2" }[s.tone];
  const dot = { ok: "bg-accent", warn: "bg-warn animate-pulse", bad: "bg-danger", idle: "bg-faint" }[s.tone];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border ${small ? "px-2 py-0.5 text-[11px]" : "px-2.5 py-1 text-xs"} font-medium ${tone}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} aria-hidden />
      {s.label}
    </span>
  );
}

export function CameraGrid({ cameras, overview, onOpen, onAdd, loading }: { cameras: Camera[]; overview: Overview | null; onOpen: (c: Camera) => void; onAdd: () => void; loading: boolean }) {
  const [bust, setBust] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setBust(Date.now()), 30000); // snapshots refresh every 30 s (live video is on the camera page)
    return () => clearInterval(id);
  }, []);
  const visible = cameras.filter((c) => c.kind !== "nvr");
  const nvrs = cameras.filter((c) => c.kind === "nvr");
  const online = overview?.states.online ?? visible.filter((c) => c.state === "online").length;
  const problems = visible.filter((c) => ["offline", "auth_failed", "stream_error", "gateway_offline"].includes(c.state));

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 p-4 lg:p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Cameras</h1>
          <p className="mt-1 text-sm text-muted">
            {visible.length === 0 ? "No cameras connected yet." : `${online} of ${visible.length} online`}
            {overview && overview.viewers > 0 ? ` · ${overview.viewers} watching now` : ""}
            {nvrs.length ? ` · ${nvrs.length} recorder${nvrs.length > 1 ? "s" : ""}` : ""}
          </p>
        </div>
        <button onClick={onAdd} className="inline-flex min-h-10 cursor-pointer items-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-accent-ink hover:brightness-110">
          <PlusIcon width={16} height={16} aria-hidden /> Add camera
        </button>
      </div>

      {overview && !overview.media_server && (
        <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger" role="alert">
          The live video server is not running, so cameras cannot be shown. Restart the Visionary backend.
        </p>
      )}

      {problems.length > 0 && (
        <section aria-label="Cameras that need attention" className="rounded-xl border border-danger/25 bg-danger/5 p-3">
          <p className="text-sm font-medium text-danger">{problems.length === 1 ? "1 camera needs attention" : `${problems.length} cameras need attention`}</p>
          <ul className="mt-2 space-y-1.5">
            {problems.map((c) => (
              <li key={c.id}>
                <button onClick={() => onOpen(c)} className="w-full cursor-pointer rounded-lg px-2 py-1.5 text-left text-sm hover:bg-panel-2">
                  <span className="font-medium">{c.name}</span>
                  <span className="text-muted"> · {DIAGNOSIS_HINT[c.health.diagnosis ?? ""] ?? STATE_LABEL[c.state]?.label}</span>
                  <span className="text-faint"> · last seen {ago(c.last_seen_at)}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {visible.length > 0 && <RecentEvents cameras={visible} onOpen={onOpen} />}

      {loading && visible.length === 0 ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="aspect-video animate-pulse rounded-xl bg-panel" />
          ))}
        </div>
      ) : visible.length === 0 ? (
        <EmptyState onAdd={onAdd} />
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {visible.map((c) => (
            <li key={c.id}>
              <button onClick={() => onOpen(c)} className="group block w-full cursor-pointer overflow-hidden rounded-xl border border-line bg-panel text-left transition hover:border-line-2">
                <div className="relative aspect-video bg-black">
                  {c.state === "online" ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={cams.snapshotUrl(c.id, bust)} alt={`Latest picture from ${c.name}`} className="h-full w-full object-cover opacity-90 transition group-hover:opacity-100" loading="lazy" />
                  ) : (
                    <div className="grid h-full place-items-center text-faint">
                      <CameraIcon width={28} height={28} aria-hidden />
                    </div>
                  )}
                  <div className="absolute left-2 top-2">
                    {c.state === "online" ? (
                      <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-black/70 px-2 py-0.5 text-[11px] font-semibold tracking-wide text-white backdrop-blur">
                        <span className="h-1.5 w-1.5 rounded-full bg-danger" aria-hidden /> LIVE
                      </span>
                    ) : (
                      <StateBadge state={c.state} small onMedia />
                    )}
                  </div>
                  {c.gateway_id && (
                    <span className="absolute right-2 top-2 inline-flex items-center gap-1 rounded bg-black/60 px-1.5 py-0.5 text-[10px] text-white/80" title="Connected through a Visionary Gateway">
                      <ServerIcon width={11} height={11} aria-hidden /> via gateway
                    </span>
                  )}
                </div>
                <div className="space-y-1 p-3">
                  <p className="truncate text-sm font-semibold">{c.name}</p>
                  <p className="truncate text-xs text-muted">{c.location || c.device.model || c.connection.type?.toUpperCase()}</p>
                  <p className="truncate text-sm">{c.state === "online" && c.live ? countsText(c.live.counts) : <span className="text-faint">{c.state === "online" ? "Live AI starting…" : `Last seen ${ago(c.last_seen_at)}`}</span>}</p>
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function EmptyState({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="rounded-2xl border border-dashed border-line-2 p-8 text-center">
      <CameraIcon className="mx-auto text-faint" width={30} height={30} aria-hidden />
      <p className="mt-3 font-medium">Connect your first camera</p>
      <p className="mx-auto mt-1 max-w-md text-sm text-muted">
        Visionary works with ONVIF and RTSP cameras, NVR/DVR recorders, and cameras on another site through a Visionary Gateway. You will test the connection before anything is saved.
      </p>
      <button onClick={onAdd} className="mt-4 inline-flex min-h-10 cursor-pointer items-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-accent-ink hover:brightness-110">
        <PlusIcon width={16} height={16} aria-hidden /> Add camera
      </button>
    </div>
  );
}

function RecentEvents({ cameras, onOpen }: { cameras: Camera[]; onOpen: (c: Camera) => void }) {
  const [events, setEvents] = useState<CameraEvent[] | null>(null);
  useEffect(() => {
    const load = () => cams.recentEvents(60, 120).then(setEvents).catch(() => setEvents([]));
    const first = setTimeout(load, 0);
    const id = setInterval(load, 10000);
    return () => {
      clearTimeout(first);
      clearInterval(id);
    };
  }, []);
  const byId = Object.fromEntries(cameras.map((c) => [c.id, c]));
  const lines = summarizeEvents(events ?? []).slice(0, 8);
  return (
    <section aria-labelledby="recent-h" className="rounded-xl border border-line bg-panel p-3">
      <h2 id="recent-h" className="px-1 text-sm font-medium">Recent AI events</h2>
      {events === null ? (
        <p className="mt-2 px-1 text-xs text-faint">Loading…</p>
      ) : lines.length === 0 ? (
        <p className="mt-2 px-1 text-sm text-muted">Nothing in the last hour.</p>
      ) : (
        <ol className="mt-1 grid gap-x-4 sm:grid-cols-2">
          {lines.map((l) => (
            <li key={l.key}>
              <button onClick={() => byId[l.cameraId] && onOpen(byId[l.cameraId])} className="flex w-full cursor-pointer gap-2 rounded-md px-1 py-1 text-left text-sm hover:bg-panel-2">
                <span className="w-16 shrink-0 font-mono text-xs leading-5 text-faint">{clock(l.at)}</span>
                <span className="min-w-0 flex-1 truncate">{l.text}</span>
                <span className="shrink-0 truncate text-xs leading-5 text-accent">{byId[l.cameraId]?.name}</span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
