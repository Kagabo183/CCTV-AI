"use client";

// Add a camera: where it is + how it connects -> address + login -> test (every check, plain-language
// diagnosis) -> what the camera can do (streams, channels) -> name + AI -> save. Nothing is stored until "Save".
// Network jargon (NAT, RTSP ports, STUN/TURN) stays out of the main path; details live under "Advanced".

import { useEffect, useMemo, useState } from "react";
import { ApiError } from "@/lib/api";
import { cams, type Camera, type ConnectionSpec, type Connector, type Discovered, type Gateway, type ProbeResult } from "@/lib/cameras";
import { AlertIcon, CameraIcon, CheckIcon, LinkIcon, SearchIcon, ServerIcon, ShieldIcon, XIcon } from "../icons";

type Method = "discover" | "onvif" | "rtsp" | "nvr" | "hls" | "cloud";
type Where = "local" | "gateway" | "internet" | "cloud";
type Step = "method" | "details" | "test" | "save";

const CHECK_LABEL: Record<string, string> = {
  dns: "Address lookup", reachable: "Network", onvif: "ONVIF", rtsp: "Video service (RTSP)", authentication: "Login", profiles: "Video profiles",
  stream: "Video stream", codec: "Video format", resolution: "Resolution", fps: "Frame rate", latency: "Start-up time", stability: "Stability", substream: "Sub-stream",
  channels: "Channels", rtsp_authentication: "Video login", gateway: "Gateway", audio: "Audio", ptz: "Pan / tilt / zoom",
};

const WHERE: { id: Where; title: string; text: string }[] = [
  { id: "local", title: "Same network", text: "The camera is on the same local network as this Visionary server." },
  { id: "gateway", title: "Another site, through a Visionary Gateway", text: "Recommended for cameras somewhere else. A small gateway on that network connects out to Visionary: no router changes." },
  { id: "internet", title: "Internet stream address", text: "The camera or its recorder already publishes a stream on the Internet (public address or web link)." },
  { id: "cloud", title: "Manufacturer's cloud", text: "The camera is registered with its maker's cloud service (for example Imou)." },
];

const METHODS: { id: Method; title: string; text: string; icon: typeof CameraIcon; where: Where[] }[] = [
  { id: "discover", title: "Find cameras on my network", text: "Looks for ONVIF cameras and recorders automatically.", icon: SearchIcon, where: ["local", "gateway"] },
  { id: "onvif", title: "ONVIF camera", text: "Most IP cameras (Hikvision, Dahua, Axis, Uniview…). Needs the camera's address.", icon: CameraIcon, where: ["local", "gateway", "internet"] },
  { id: "rtsp", title: "RTSP stream link", text: "A link like rtsp://192.168.1.20:554/stream1 from the camera's manual.", icon: LinkIcon, where: ["local", "gateway", "internet"] },
  { id: "nvr", title: "NVR / DVR recorder", text: "Adds each recorder channel as its own camera.", icon: ServerIcon, where: ["local", "gateway", "internet"] },
  { id: "hls", title: "Web stream link (HLS)", text: "An https://…/index.m3u8 video address.", icon: LinkIcon, where: ["internet"] },
];

export function AddCameraWizard({ onClose, onAdded }: { onClose: () => void; onAdded: (c: Camera[]) => void }) {
  const [step, setStep] = useState<Step>("method");
  const [where, setWhere] = useState<Where>("local");
  const [method, setMethod] = useState<Method>("onvif");
  const [gateways, setGateways] = useState<Gateway[]>([]);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [gatewayId, setGatewayId] = useState("");
  const [address, setAddress] = useState("");
  const [subUrl, setSubUrl] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [found, setFound] = useState<Discovered[] | null>(null);
  const [foundMsg, setFoundMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [error, setError] = useState("");
  const [channels, setChannels] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [location, setLocation] = useState("");
  const [ai, setAi] = useState({ people: true, vehicles: true, objects: true, wildlife: false, behavior: true, record: true });

  useEffect(() => {
    cams.gateways().then((g) => setGateways(g.filter((x) => x.status !== "pending"))).catch(() => undefined);
    cams.connectors().then(setConnectors).catch(() => undefined);
  }, []);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  const onlineGateways = gateways.filter((g) => g.status === "online");
  const imou = connectors.find((c) => c.id === "imou");
  const methods = METHODS.filter((m) => m.where.includes(where));
  const effectiveMethod: Method = where === "cloud" ? "cloud" : methods.some((m) => m.id === method) ? method : methods[0].id;
  const kind: "onvif" | "rtsp" | "nvr" | "hls" | "vendor" =
    effectiveMethod === "cloud" ? "vendor" : effectiveMethod === "discover" ? "onvif" : effectiveMethod;
  const viaGateway = where === "gateway" ? gatewayId : "";
  const spec = (): ConnectionSpec =>
    kind === "vendor"
      ? { type: "vendor", vendor: "imou", device_id: deviceId.trim() }
      : kind === "rtsp" || kind === "hls"
        ? { type: kind, url: address.trim(), ...(kind === "rtsp" && subUrl.trim() ? { sub_url: subUrl.trim() } : {}) }
        : { type: kind, xaddr: address.trim() };

  const methodReady =
    where === "local" || where === "internet" || (where === "gateway" && !!gatewayId) || (where === "cloud" && imou?.status === "untested");

  async function discover() {
    setBusy(true);
    setFound(null);
    setFoundMsg("");
    try {
      const r = await cams.discover(viaGateway || undefined);
      setFound(r.devices);
      setFoundMsg(r.message ?? (r.devices.length ? "" : "No ONVIF devices answered. Some cameras have discovery switched off: enter the address instead."));
    } catch (e) {
      setFoundMsg(e instanceof Error ? e.message : "Search failed");
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setBusy(true);
    setError("");
    setProbe(null);
    setStep("test");
    try {
      const r = await cams.test(spec(), username || undefined, password || undefined, viaGateway || undefined);
      setProbe(r);
      setChannels(r.channels.map((c) => c.key));
      if (!name) setName(r.device.model ? `${r.device.manufacturer ?? ""} ${r.device.model}`.trim() : kind === "nvr" ? "Recorder" : "Camera");
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : "The test could not run.");
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true);
    setError("");
    try {
      const made = await cams.add({
        name: name.trim(),
        location: location.trim() || undefined,
        connection: spec(),
        username: username || undefined,
        password: password || undefined,
        channels: kind === "nvr" ? channels : undefined,
        gateway_id: viaGateway || undefined,
        ai_profile: {
          general: ai.people || ai.vehicles || ai.objects,
          people: ai.people,
          vehicles: ai.vehicles,
          objects: ai.objects,
          wildlife: ai.wildlife,
          species: ai.wildlife,
          behavior: ai.behavior,
          record: ai.record,
        },
      });
      setPassword("");
      onAdded(made);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save the camera.");
    } finally {
      setBusy(false);
    }
  }

  const detailsValid =
    kind === "vendor"
      ? deviceId.trim().length > 3
      : address.trim().length > 3 &&
        (kind !== "rtsp" || /^rtsps?:\/\//i.test(address.trim())) &&
        (kind !== "hls" || /^https?:\/\//i.test(address.trim()));
  const publicAddress = useMemo(() => where === "internet" && /^(rtsp|http)/i.test(address) && !/:\/\/(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(address), [where, address]);

  return (
    <div className="fixed inset-0 z-50 flex items-stretch justify-center bg-black/70 backdrop-blur-sm sm:items-center sm:p-6" role="dialog" aria-modal="true" aria-labelledby="add-camera-title">
      <div className="flex max-h-full w-full max-w-2xl flex-col overflow-hidden border border-line bg-panel sm:rounded-2xl">
        <header className="flex items-center gap-3 border-b border-line px-4 py-3">
          <h2 id="add-camera-title" className="text-base font-semibold">Add camera</h2>
          <ol className="ml-2 hidden items-center gap-1 text-[11px] text-faint sm:flex" aria-label="Steps">
            {(["method", "details", "test", "save"] as Step[]).map((s, i) => (
              <li key={s} className={`rounded-full px-2 py-0.5 ${step === s ? "bg-accent/15 text-accent" : ""}`} aria-current={step === s ? "step" : undefined}>
                {i + 1}. {{ method: "Connection", details: "Address", test: "Test", save: "Name & AI" }[s]}
              </li>
            ))}
          </ol>
          <button onClick={onClose} className="ml-auto grid h-9 w-9 cursor-pointer place-items-center rounded-lg text-muted hover:bg-panel-2" aria-label="Close">
            <XIcon width={16} height={16} />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {step === "method" && (
            <div className="space-y-5">
              <fieldset>
                <legend className="text-sm font-medium">Where is the camera?</legend>
                <div className="mt-3 grid gap-2">
                  {WHERE.map((w) => (
                    <label key={w.id} className={`flex cursor-pointer gap-3 rounded-xl border p-3 transition ${where === w.id ? "border-accent/60 bg-accent/5" : "border-line hover:border-line-2"}`}>
                      <input type="radio" name="where" className="mt-1 h-4 w-4 accent-[var(--accent)]" checked={where === w.id} onChange={() => setWhere(w.id)} />
                      <span>
                        <span className="block text-sm font-medium">{w.title}</span>
                        <span className="mt-0.5 block text-xs text-muted">{w.text}</span>
                      </span>
                    </label>
                  ))}
                </div>
              </fieldset>

              {where === "gateway" &&
                (gateways.length === 0 ? (
                  <Note>
                    No Visionary Gateway yet. Create one under <strong>Gateways</strong> and install it on a computer at that site (it takes two commands), then come back here.
                  </Note>
                ) : (
                  <div>
                    <label htmlFor="gw" className="text-sm font-medium">Which site?</label>
                    <select id="gw" value={gatewayId} onChange={(e) => setGatewayId(e.target.value)} className="mt-2 min-h-10 w-full rounded-lg border border-line-2 bg-bg px-3 text-sm">
                      <option value="">Choose a gateway…</option>
                      {gateways.map((g) => (
                        <option key={g.id} value={g.id} disabled={g.status !== "online"}>
                          {g.name}
                          {g.location ? ` · ${g.location}` : ""}
                          {g.status !== "online" ? " (offline)" : ""}
                        </option>
                      ))}
                    </select>
                    {onlineGateways.length === 0 && <p className="mt-1.5 text-xs text-warn">All gateways are offline. Check that the gateway computer is on and has Internet access.</p>}
                  </div>
                ))}

              {where === "cloud" && (
                <Note>
                  {imou?.status === "untested" ? (
                    <>Imou cloud cameras connect through the official Imou Open Platform. This connector has not been verified with a real Imou account yet.</>
                  ) : (
                    <>
                      Cloud connectors use each manufacturer&apos;s official platform. Imou needs a developer app set up on the Visionary server (IMOU_APP_ID). Until then, add the camera as an ONVIF or RTSP camera on its own network, or through a Visionary Gateway.
                    </>
                  )}
                </Note>
              )}

              {where !== "cloud" && (
                <fieldset>
                  <legend className="text-sm font-medium">How is the camera connected?</legend>
                  <div className="mt-3 grid gap-2 sm:grid-cols-2">
                    {methods.map((m) => (
                      <label key={m.id} className={`flex cursor-pointer gap-3 rounded-xl border p-3 transition ${effectiveMethod === m.id ? "border-accent/60 bg-accent/5" : "border-line hover:border-line-2"}`}>
                        <input type="radio" name="method" className="sr-only" checked={effectiveMethod === m.id} onChange={() => setMethod(m.id)} />
                        <m.icon width={18} height={18} className={effectiveMethod === m.id ? "mt-0.5 text-accent" : "mt-0.5 text-muted"} aria-hidden />
                        <span>
                          <span className="block text-sm font-medium">{m.title}</span>
                          <span className="mt-0.5 block text-xs text-muted">{m.text}</span>
                        </span>
                      </label>
                    ))}
                  </div>
                </fieldset>
              )}
            </div>
          )}

          {step === "details" && (
            <div className="space-y-4">
              {effectiveMethod === "discover" && (
                <div className="rounded-xl border border-line p-3">
                  <button onClick={discover} disabled={busy} className="inline-flex min-h-9 cursor-pointer items-center gap-2 rounded-lg bg-panel-2 px-3 text-sm hover:bg-line disabled:opacity-60">
                    <SearchIcon width={15} height={15} aria-hidden /> {busy ? "Searching…" : found ? "Search again" : viaGateway ? "Search that site's network" : "Search the network"}
                  </button>
                  {foundMsg && <p className="mt-2 text-xs text-muted">{foundMsg}</p>}
                  {found && found.length > 0 && (
                    <ul className="mt-3 space-y-1.5" aria-label="Cameras found">
                      {found.map((d) => {
                        const addr = d.xaddrs[0] ?? d.ip;
                        return (
                          <li key={d.id}>
                            <button
                              disabled={d.already_added}
                              onClick={() => {
                                setAddress(addr);
                                setName(d.name ?? "");
                                if (d.kind_hint === "nvr") setMethod("nvr");
                              }}
                              className={`flex w-full cursor-pointer items-center justify-between gap-3 rounded-lg border px-3 py-2 text-left text-sm disabled:cursor-not-allowed disabled:opacity-60 ${address === addr ? "border-accent/60 bg-accent/5" : "border-line hover:border-line-2"}`}
                            >
                              <span className="min-w-0">
                                <span className="block truncate font-medium">
                                  {d.name ?? "ONVIF device"}
                                  {d.hardware && d.hardware !== d.name && <span className="font-normal text-muted"> · {d.hardware}</span>}
                                </span>
                                <span className="block text-xs text-muted">
                                  {`${d.already_added ? "Already added · " : ""}ONVIF detected${d.profiles?.length ? ` · Profile ${d.profiles.join(", ")}` : ""}${d.kind_hint === "nvr" ? " · recorder" : ""}`}
                                </span>
                              </span>
                              <span className="shrink-0 font-mono text-xs text-muted">{d.ip}</span>
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              )}
              {kind === "vendor" ? (
                <Field id="dev" label="Imou device serial number" hint="The serial number printed on the camera or shown in the Imou Life app." value={deviceId} onChange={setDeviceId} mono />
              ) : (
                <Field
                  id="addr"
                  label={kind === "rtsp" ? "Stream link (main stream)" : kind === "hls" ? "Web stream link" : kind === "nvr" ? "Recorder address" : "Camera address"}
                  hint={
                    kind === "rtsp"
                      ? "rtsp://192.168.1.20:554/Streaming/Channels/101 — a username and password in the link are moved to secure storage."
                      : kind === "hls"
                        ? "https://example.com/live/camera1/index.m3u8"
                        : where === "internet"
                          ? "Public address or host name, optionally with port (cam.example.com:8080)."
                          : "IP address, optionally with port (192.168.1.20 or 192.168.1.20:8080)."
                  }
                  value={address}
                  onChange={setAddress}
                  placeholder={kind === "rtsp" ? "rtsp://" : kind === "hls" ? "https://" : "192.168.1.20"}
                  mono
                />
              )}
              {kind !== "hls" && (
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field id="user" label="Username" value={username} onChange={setUsername} autoComplete="off" />
                  <Field id="pass" label="Password" value={password} onChange={setPassword} type="password" autoComplete="new-password" />
                </div>
              )}
              <p className="flex items-start gap-2 text-xs text-faint">
                <ShieldIcon width={13} height={13} className="mt-0.5 shrink-0" aria-hidden /> The password is encrypted on the server and never sent back to any browser.
              </p>
              {publicAddress && (
                <Note tone="warn">
                  Cameras reachable directly from the Internet are a common target for attacks. If this camera is on a private network, a Visionary Gateway is safer: it needs no open ports.
                </Note>
              )}
              {kind === "rtsp" && (
                <details className="rounded-xl border border-line p-3">
                  <summary className="cursor-pointer text-sm font-medium">Advanced settings</summary>
                  <div className="mt-3 space-y-3">
                    <Field id="sub" label="Sub-stream link (optional)" hint="A smaller stream for phones, the live wall and the AI. Saves bandwidth." value={subUrl} onChange={setSubUrl} placeholder="rtsp://" mono />
                    <p className="text-xs text-faint">Video is pulled over RTSP/TCP by Visionary&apos;s media server and shown to browsers as WebRTC. Cameras are never exposed to viewers directly.</p>
                  </div>
                </details>
              )}
            </div>
          )}

          {step === "test" && (
            <div className="space-y-4">
              {busy && !probe && (
                <p className="flex items-center gap-2 text-sm text-muted" role="status">
                  <span className="h-2 w-2 animate-pulse rounded-full bg-accent" /> Testing the connection (network, login, stream, video format)…
                </p>
              )}
              {error && <Problem title="The test could not run" text={error} />}
              {probe && (
                <>
                  {probe.ok ? (
                    <p className="flex items-center gap-2 rounded-lg border border-accent/30 bg-accent/10 px-3 py-2 text-sm text-accent">
                      <CheckIcon width={16} height={16} aria-hidden /> Connected. {probe.device.model ? `${probe.device.manufacturer ?? ""} ${probe.device.model}` : ""}
                    </p>
                  ) : (
                    <Problem title={probe.explanation} text={probe.next_step} />
                  )}
                  <CheckList checks={probe.checks} />
                  {probe.ok && <Capabilities probe={probe} kind={kind} channels={channels} setChannels={setChannels} />}
                </>
              )}
            </div>
          )}

          {step === "save" && (
            <div className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field id="name" label={kind === "nvr" ? "Recorder name" : "Camera name"} value={name} onChange={setName} placeholder="Front gate" />
                <Field id="loc" label="Place (optional)" hint="Used when you ask by place, e.g. “irembo”." value={location} onChange={setLocation} placeholder="Irembo" />
              </div>
              <fieldset className="space-y-2">
                <legend className="text-sm font-medium">What should the AI watch for?</legend>
                <div className="grid gap-2 sm:grid-cols-2">
                  <Toggle checked={ai.people} onChange={(v) => setAi({ ...ai, people: v })} title="People" text="Counts, arrivals and departures." />
                  <Toggle checked={ai.vehicles} onChange={(v) => setAi({ ...ai, vehicles: v })} title="Vehicles" text="Cars, trucks, buses, motorcycles." />
                  <Toggle checked={ai.objects} onChange={(v) => setAi({ ...ai, objects: v })} title="Other objects" text="Bags, bicycles and other common objects." />
                  <Toggle checked={ai.wildlife} onChange={(v) => setAi({ ...ai, wildlife: v })} title="Wildlife and species" text="Animals with species names. Uses more GPU: turn on only for wildlife cameras." />
                  <Toggle checked={ai.behavior} onChange={(v) => setAi({ ...ai, behavior: v })} title="Behaviour events" text="Zones, loitering (long stays) and crowds." />
                  <Toggle checked={ai.record} onChange={(v) => setAi({ ...ai, record: v })} title="Record video" text="For playback and for opening the video behind an event." />
                </div>
              </fieldset>
              {error && <Problem title="Could not save" text={error} />}
            </div>
          )}
        </div>

        <footer className="flex items-center gap-2 border-t border-line px-4 py-3">
          {step !== "method" && (
            <button onClick={() => setStep(step === "save" ? "test" : step === "test" ? "details" : "method")} className="min-h-10 cursor-pointer rounded-lg px-3 text-sm text-muted hover:bg-panel-2">
              Back
            </button>
          )}
          <div className="ml-auto" />
          {step === "method" && (
            <Primary onClick={() => setStep("details")} disabled={!methodReady}>
              Next
            </Primary>
          )}
          {step === "details" && (
            <Primary onClick={test} disabled={!detailsValid || busy}>
              Test connection
            </Primary>
          )}
          {step === "test" && (
            <>
              {probe && !probe.ok && (
                <button onClick={test} disabled={busy} className="min-h-10 cursor-pointer rounded-lg px-3 text-sm text-text hover:bg-panel-2">
                  Test again
                </button>
              )}
              <Primary onClick={() => setStep("save")} disabled={!probe?.ok || (kind === "nvr" && channels.length === 0)}>
                Next
              </Primary>
            </>
          )}
          {step === "save" && (
            <Primary onClick={save} disabled={busy || !name.trim()}>
              {busy ? "Saving…" : kind === "nvr" ? `Save recorder and ${channels.length} camera${channels.length === 1 ? "" : "s"}` : "Save camera"}
            </Primary>
          )}
        </footer>
      </div>
    </div>
  );
}

/** Pass/fail list used by the wizard test and by camera diagnostics. */
export function CheckList({ checks }: { checks: ProbeResult["checks"] }) {
  return (
    <ul className="divide-y divide-line rounded-xl border border-line text-sm" aria-label="Connection checks">
      {checks.map((c, i) => (
        <li key={i} className="flex items-start gap-3 px-3 py-2">
          <span
            className={`mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full ${c.ok ? "bg-accent/15 text-accent" : c.ok === false ? "bg-danger/15 text-danger" : "bg-panel-2 text-faint"}`}
            aria-label={c.ok ? "passed" : c.ok === false ? "failed" : "not applicable"}
          >
            {c.ok ? <CheckIcon width={12} height={12} /> : c.ok === false ? <XIcon width={11} height={11} /> : "–"}
          </span>
          <span className="min-w-0 flex-1">
            <span className="font-medium">{CHECK_LABEL[c.name] ?? (c.name.charAt(0).toUpperCase() + c.name.slice(1)).replace(/_/g, " ")}</span>
            <span className="text-muted"> — {c.ok === false && c.explanation ? c.explanation : c.detail}</span>
            {c.ok === false && c.next_step && <span className="mt-0.5 block text-xs text-text/80">{c.next_step}</span>}
          </span>
          {c.ms != null && <span className="font-mono text-xs text-faint">{Math.round(c.ms)} ms</span>}
        </li>
      ))}
    </ul>
  );
}

function Capabilities({ probe, kind, channels, setChannels }: { probe: ProbeResult; kind: string; channels: string[]; setChannels: (c: string[]) => void }) {
  const caps = probe.capabilities;
  const rows: [string, unknown][] = [
    ["Video", caps.h265 ? (caps.h264 ? "H.264 + H.265" : "H.265") : caps.h264 ? "H.264" : "—"],
    ["Audio", caps.audio],
    ["Pan / tilt / zoom", caps.ptz],
    ["Camera events", caps.events],
    ["Snapshots", caps.snapshot],
    ["ONVIF profiles", Array.isArray(caps.onvif_profiles) ? (caps.onvif_profiles as string[]).join(", ") || "—" : caps.onvif ? "yes" : "no"],
  ];
  const main = probe.channels[0];
  return (
    <div className="space-y-3">
      <section aria-labelledby="caps-h" className="rounded-xl border border-line p-3">
        <h3 id="caps-h" className="text-sm font-medium">What this camera supports</h3>
        <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm sm:grid-cols-3">
          {rows.map(([k, v]) => (
            <div key={k}>
              <dt className="text-xs text-faint">{k}</dt>
              <dd>{typeof v === "boolean" ? (v ? "Yes" : "No") : v == null ? "Unknown" : String(v)}</dd>
            </div>
          ))}
        </dl>
        {kind !== "nvr" && main && (
          <ul className="mt-3 space-y-1 text-xs text-muted">
            {main.streams.map((s) => (
              <li key={s.role}>
                <span className="font-medium text-text">{s.role === "main" ? "Main stream" : s.role === "sub" ? "Sub-stream" : "Low stream"}</span>: {s.width && s.height ? `${s.width}×${s.height}` : "?"} · {s.codec ?? "?"}
                {s.fps ? ` · ${Math.round(s.fps)} fps` : ""}
              </li>
            ))}
          </ul>
        )}
      </section>
      {kind === "nvr" && (
        <fieldset className="rounded-xl border border-line p-3">
          <legend className="px-1 text-sm font-medium">Channels to add as cameras</legend>
          <div className="mt-1 grid gap-1.5 sm:grid-cols-2">
            {probe.channels.map((c) => (
              <label key={c.key} className="flex min-h-9 cursor-pointer items-center gap-2 rounded-lg px-2 text-sm hover:bg-panel-2">
                <input type="checkbox" checked={channels.includes(c.key)} onChange={(e) => setChannels(e.target.checked ? [...channels, c.key] : channels.filter((k) => k !== c.key))} className="h-4 w-4 accent-[var(--accent)]" />
                {c.name} <span className="text-xs text-faint">{c.streams[0]?.width ? `${c.streams[0].width}×${c.streams[0].height}` : ""}</span>
              </label>
            ))}
          </div>
        </fieldset>
      )}
    </div>
  );
}

function Field(p: { id: string; label: string; value: string; onChange: (v: string) => void; hint?: string; placeholder?: string; type?: string; mono?: boolean; autoComplete?: string }) {
  return (
    <div>
      <label htmlFor={p.id} className="text-sm font-medium">{p.label}</label>
      <input
        id={p.id}
        type={p.type ?? "text"}
        value={p.value}
        onChange={(e) => p.onChange(e.target.value)}
        placeholder={p.placeholder}
        autoComplete={p.autoComplete}
        spellCheck={false}
        className={`mt-1.5 min-h-10 w-full rounded-lg border border-line-2 bg-bg px-3 text-sm focus:border-accent/60 focus:outline-none ${p.mono ? "font-mono" : ""}`}
        aria-describedby={p.hint ? `${p.id}-hint` : undefined}
      />
      {p.hint && (
        <p id={`${p.id}-hint`} className="mt-1 text-xs text-faint">
          {p.hint}
        </p>
      )}
    </div>
  );
}

function Toggle({ checked, onChange, title, text }: { checked: boolean; onChange: (v: boolean) => void; title: string; text: string }) {
  return (
    <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-line p-3 hover:border-line-2">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="mt-0.5 h-4 w-4 accent-[var(--accent)]" />
      <span>
        <span className="block text-sm font-medium">{title}</span>
        <span className="block text-xs text-muted">{text}</span>
      </span>
    </label>
  );
}

function Note({ children, tone = "info" }: { children: React.ReactNode; tone?: "info" | "warn" }) {
  return (
    <div className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-sm ${tone === "warn" ? "border-warn/30 bg-warn/10" : "border-line-2 bg-panel-2"}`}>
      <AlertIcon width={15} height={15} className={`mt-0.5 shrink-0 ${tone === "warn" ? "text-warn" : "text-muted"}`} aria-hidden />
      <p className="text-text/90">{children}</p>
    </div>
  );
}

function Primary({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled?: boolean }) {
  return (
    <button onClick={onClick} disabled={disabled} className="min-h-10 cursor-pointer rounded-lg bg-accent px-4 text-sm font-semibold text-accent-ink hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50">
      {children}
    </button>
  );
}

export function Problem({ title, text }: { title: string; text: string }) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm" role="alert">
      <AlertIcon width={16} height={16} className="mt-0.5 shrink-0 text-danger" aria-hidden />
      <div>
        <p className="font-medium text-danger">{title}</p>
        {text && <p className="mt-0.5 text-text/85">{text}</p>}
      </div>
    </div>
  );
}
