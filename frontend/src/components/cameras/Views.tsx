"use client";

import { useEffect, useMemo, useState } from "react";
import { ago, clock, countsText, cams, summarizeEvents, type Camera, type CameraEvent, type Gateway } from "@/lib/cameras";
import { CameraIcon, PlusIcon, ServerIcon, TrashIcon } from "../icons";
import { StateBadge } from "./CameraGrid";
import { LivePlayer } from "./LivePlayer";
import { Recordings } from "./CameraDetail";
import { Problem } from "./AddCameraWizard";

// ---------------------------------------------------------------------------------------------- live wall
export function LiveWall({ cameras, onOpen }: { cameras: Camera[]; onOpen: (c: Camera) => void }) {
  const online = cameras.filter((c) => c.kind !== "nvr" && c.permissions.includes("live_view"));
  const [layout, setLayout] = useState<1 | 4 | 9>(4);
  const shown = online.slice(0, layout);
  return (
    <div className="flex h-full flex-col gap-3 p-3 lg:p-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold tracking-tight">Live</h1>
        <p className="text-sm text-muted">{online.length > layout ? `Showing ${layout} of ${online.length} cameras` : `${online.length} camera${online.length === 1 ? "" : "s"}`} · data-saver streams</p>
        <div role="radiogroup" aria-label="Layout" className="ml-auto inline-flex rounded-lg border border-line p-0.5 text-xs">
          {([1, 4, 9] as const).map((n) => (
            <button key={n} role="radio" aria-checked={layout === n} onClick={() => setLayout(n)} className={`min-h-8 cursor-pointer rounded-md px-2.5 ${layout === n ? "bg-panel-2" : "text-muted hover:text-text"}`}>
              {n === 1 ? "1" : n === 4 ? "2×2" : "3×3"}
            </button>
          ))}
        </div>
      </div>
      {online.length === 0 ? (
        <p className="text-sm text-muted">No cameras yet.</p>
      ) : (
        <ul className={`grid min-h-0 flex-1 gap-2 ${layout === 1 ? "grid-cols-1" : layout === 4 ? "grid-cols-1 sm:grid-cols-2" : "grid-cols-2 lg:grid-cols-3"}`}>
          {shown.map((c) => (
            <li key={c.id} className="relative overflow-hidden rounded-xl border border-line bg-black">
              <div className="aspect-video">
                <LivePlayer cameraId={c.id} quality={layout === 1 ? "auto" : "sub"} compact={layout !== 1} label={c.name} objects={c.live?.objects} resolution={c.live?.resolution} showBoxes={layout === 1} />
              </div>
              <button onClick={() => onOpen(c)} className="absolute inset-x-0 bottom-0 flex cursor-pointer items-center gap-2 bg-gradient-to-t from-black/85 to-transparent px-3 pb-2 pt-6 text-left">
                <span className="truncate text-sm font-medium text-white">{c.name}</span>
                <span className="ml-auto truncate text-xs text-white/80">{c.live ? countsText(c.live.counts) : ""}</span>
              </button>
              {c.state !== "online" && (
                <div className="absolute left-2 top-2">
                  <StateBadge state={c.state} small onMedia />
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------- events
export function EventsFeed({ cameras, onOpen }: { cameras: Camera[]; onOpen: (c: Camera) => void }) {
  const [minutes, setMinutes] = useState(60);
  const [filter, setFilter] = useState("all");
  const [events, setEvents] = useState<CameraEvent[] | null>(null);
  const list = useMemo(() => cameras.filter((c) => c.kind !== "nvr" && c.permissions.includes("playback")), [cameras]);
  const ids = list.map((c) => c.id).join(",");
  useEffect(() => {
    let alive = true;
    const load = async () => {
      const all = await Promise.all(list.map((c) => cams.events(c.id, minutes).catch(() => [] as CameraEvent[])));
      if (alive) setEvents(all.flat().sort((a, b) => (b.occurred_at ?? "").localeCompare(a.occurred_at ?? "")));
    };
    load();
    const id = setInterval(load, 15000);
    return () => {
      alive = false;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids, minutes]);
  const byId = Object.fromEntries(cameras.map((c) => [c.id, c]));
  const kinds: Record<string, (e: CameraEvent) => boolean> = {
    all: (e) => e.type !== "object_disappeared",
    people: (e) => e.object === "person" && e.type !== "object_disappeared",
    vehicles: (e) => ["car", "truck", "bus", "motorcycle"].includes(e.object ?? "") && e.type !== "object_disappeared",
    animals: (e) => e.type.startsWith("animal") || e.detector === "live:wildlife",
    zones: (e) => ["person_entered", "person_exited", "vehicle_entered", "vehicle_exited", "dwell_in_zone", "loitering", "crowd_detected"].includes(e.type),
  };
  const shown = summarizeEvents((events ?? []).filter(kinds[filter]));
  return (
    <div className="mx-auto w-full max-w-4xl space-y-4 p-4 lg:p-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold tracking-tight">Events</h1>
        <div role="radiogroup" aria-label="Show" className="inline-flex flex-wrap rounded-lg border border-line p-0.5 text-xs">
          {Object.keys(kinds).map((k) => (
            <button key={k} role="radio" aria-checked={filter === k} onClick={() => setFilter(k)} className={`min-h-8 cursor-pointer rounded-md px-2.5 capitalize ${filter === k ? "bg-panel-2" : "text-muted hover:text-text"}`}>
              {k}
            </button>
          ))}
        </div>
        <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} aria-label="Time range" className="ml-auto min-h-9 rounded-lg border border-line-2 bg-bg px-2 text-sm">
          <option value={20}>Last 20 minutes</option>
          <option value={60}>Last hour</option>
          <option value={360}>Last 6 hours</option>
          <option value={1440}>Last 24 hours</option>
        </select>
      </div>
      <p className="text-xs text-faint">Events come from the live AI (detection, tracking and zone rules), not from a person watching. “Possible” means the AI was not sure.</p>
      {events === null ? (
        <p className="text-sm text-faint" role="status">Loading…</p>
      ) : shown.length === 0 ? (
        <p className="text-sm text-muted">Nothing in this period.</p>
      ) : (
        <ol className="divide-y divide-line rounded-xl border border-line bg-panel">
          {shown.slice(0, 300).map((e) => (
            <li key={e.key} className="flex items-start gap-3 px-3 py-2">
              <span className="w-20 shrink-0 font-mono text-xs leading-5 text-faint">{clock(e.at)}</span>
              <span className="min-w-0 flex-1 text-sm">{e.text}</span>
              {byId[e.cameraId] && (
                <button onClick={() => onOpen(byId[e.cameraId])} className="shrink-0 cursor-pointer truncate rounded px-1.5 text-xs text-accent hover:bg-accent/10">
                  {byId[e.cameraId].name}
                </button>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------- recordings
export function RecordingsView({ cameras }: { cameras: Camera[] }) {
  const list = cameras.filter((c) => c.kind !== "nvr" && c.permissions.includes("playback"));
  const [id, setId] = useState<string>("");
  const cam = list.find((c) => c.id === id) ?? list[0];
  return (
    <div className="mx-auto w-full max-w-3xl space-y-4 p-4 lg:p-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold tracking-tight">Recordings</h1>
        {list.length > 0 && (
          <select value={cam?.id} onChange={(e) => setId(e.target.value)} aria-label="Camera" className="ml-auto min-h-9 rounded-lg border border-line-2 bg-bg px-2 text-sm">
            {list.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        )}
      </div>
      {cam ? (
        <div className="rounded-xl border border-line bg-panel p-3">
          <Recordings key={cam.id} cam={cam} />
        </div>
      ) : (
        <p className="text-sm text-muted">No cameras with playback access.</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------- gateways
export function GatewaysView() {
  const [list, setList] = useState<Gateway[] | null>(null);
  const [created, setCreated] = useState<Gateway | null>(null);
  const [name, setName] = useState("");
  const [location, setLocation] = useState("");
  const [error, setError] = useState("");
  const load = () => cams.gateways().then(setList).catch((e) => setError(e.message));
  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);
  const hub = typeof window !== "undefined" ? window.location.origin : "";
  return (
    <div className="mx-auto w-full max-w-4xl space-y-5 p-4 lg:p-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Gateways</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted">
          A gateway is a small computer (PC, mini-PC or Raspberry Pi) on a site with cameras. It connects out to Visionary over the Internet, so you do not open ports on the router or expose cameras.
        </p>
      </div>

      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setError("");
          try {
            setCreated(await cams.createGateway(name.trim(), location.trim() || undefined));
            setName("");
            setLocation("");
            load();
          } catch (err) {
            setError(err instanceof Error ? err.message : "Could not create the gateway");
          }
        }}
        className="flex flex-wrap items-end gap-2 rounded-xl border border-line bg-panel p-3"
      >
        <label className="min-w-40 flex-1 text-sm">
          <span className="font-medium">New gateway name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} required placeholder="Farm office" className="mt-1.5 min-h-10 w-full rounded-lg border border-line-2 bg-bg px-3" />
        </label>
        <label className="min-w-40 flex-1 text-sm">
          <span className="font-medium">Place (optional)</span>
          <input value={location} onChange={(e) => setLocation(e.target.value)} placeholder="Nyagatare" className="mt-1.5 min-h-10 w-full rounded-lg border border-line-2 bg-bg px-3" />
        </label>
        <button className="inline-flex min-h-10 cursor-pointer items-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-accent-ink hover:brightness-110">
          <PlusIcon width={15} height={15} aria-hidden /> Create
        </button>
      </form>
      {error && <Problem title="Something went wrong" text={error} />}

      {created?.enrollment_token && (
        <section className="space-y-2 rounded-xl border border-accent/30 bg-accent/5 p-4" aria-labelledby="enrol-h">
          <h2 id="enrol-h" className="text-sm font-semibold">Install “{created.name}”</h2>
          <p className="text-sm text-muted">On the gateway computer, run these two commands. The code works once and expires {created.expires_at ? `at ${clock(created.expires_at)}` : "soon"}. It is shown only now.</p>
          <pre className="overflow-x-auto rounded-lg bg-bg p-3 font-mono text-xs leading-relaxed">
{`python -m gateway_agent.visionary_gateway enroll --hub ${hub} --token ${created.enrollment_token}
python -m gateway_agent.visionary_gateway run`}
          </pre>
          <button onClick={() => setCreated(null)} className="min-h-8 cursor-pointer rounded-md px-2 text-xs text-muted hover:bg-panel-2">
            Done
          </button>
        </section>
      )}

      {list === null ? (
        <p className="text-sm text-faint" role="status">Loading…</p>
      ) : list.length === 0 ? (
        <p className="text-sm text-muted">No gateways yet. Cameras on this server&apos;s own network do not need one.</p>
      ) : (
        <ul className="space-y-2">
          {list.map((g) => (
            <li key={g.id} className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-panel p-3">
              <ServerIcon width={18} height={18} className={g.status === "online" ? "text-accent" : "text-faint"} aria-hidden />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">
                  {g.name} {g.location && <span className="font-normal text-muted">· {g.location}</span>}
                </p>
                <p className="text-xs text-muted">
                  {g.status === "pending" ? "Waiting for installation" : g.status === "online" ? `Online · version ${g.version ?? "?"} · ${g.camera_count ?? 0} camera(s) · last heartbeat ${ago(g.last_heartbeat_at)}` : `Offline · ${g.camera_count ?? 0} camera(s) · last heartbeat ${ago(g.last_heartbeat_at)}`}
                </p>
              </div>
              <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${g.status === "online" ? "bg-accent/10 text-accent" : g.status === "pending" ? "bg-warn/10 text-warn" : "bg-danger/10 text-danger"}`}>{g.status}</span>
              <button
                onClick={async () => {
                  if (!window.confirm(`Revoke “${g.name}”? It is disconnected at once and its cameras stop.`)) return;
                  await cams.revokeGateway(g.id);
                  load();
                }}
                className="inline-flex min-h-8 cursor-pointer items-center gap-1 rounded-md px-2 text-xs text-danger hover:bg-danger/10"
              >
                <TrashIcon width={12} height={12} aria-hidden /> Revoke
              </button>
            </li>
          ))}
        </ul>
      )}
      <p className="flex items-center gap-2 text-xs text-faint">
        <CameraIcon width={13} height={13} aria-hidden /> To add a camera behind a gateway, choose the gateway in Cameras → Add camera.
      </p>
    </div>
  );
}
