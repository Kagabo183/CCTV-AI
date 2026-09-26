import type { BoxTrack, Track, VideoEvent } from "./types";

export const VEHICLE_CLASSES = new Set(["car", "truck", "bus", "motorcycle", "train", "boat", "airplane", "van", "pickup truck", "sports car", "suv"]);

export type ClassFinding = {
  cls: string;
  /** Most seen in one frame at the same time: the reliable count. */
  peak: number;
  peakAt: number;
  /** Separate track ids: over-counts objects that leave and come back. */
  tracks: number;
};

export type Findings = {
  classes: ClassFinding[]; // confident classes, largest peak first
  uncertain: number; // tracks the detector could not identify
  people: ClassFinding | null;
  vehicles: ClassFinding | null; // all vehicle classes together
  /** Per-second maximum simultaneously visible people / vehicles, for the activity chart. */
  timeline: { t: number; people: number; vehicles: number }[];
  duration: number;
};

/**
 * Counts from the stored per-frame boxes, with the same rule the assistant uses:
 * a box counts only when its confidence reaches the run's confirm threshold.
 */
export function computeFindings(boxes: BoxTrack, tracks: Track[]): Findings {
  const confirm = boxes.confirm_confidence ?? 0.5;
  const peak = new Map<string, { n: number; t: number }>();
  let vehiclePeak = { n: 0, t: 0 };
  const perSecond = new Map<number, { people: number; vehicles: number }>();
  for (const f of boxes.frames) {
    const counts = new Map<string, number>();
    let vehicles = 0;
    for (const o of f.o) {
      if (o[2] < confirm) continue;
      const cls = o[1].toLowerCase(); // Objects365 names are capitalised
      counts.set(cls, (counts.get(cls) ?? 0) + 1);
      if (VEHICLE_CLASSES.has(cls)) vehicles++;
    }
    for (const [cls, n] of counts) {
      const p = peak.get(cls);
      if (!p || n > p.n) peak.set(cls, { n, t: f.t });
    }
    if (vehicles > vehiclePeak.n) vehiclePeak = { n: vehicles, t: f.t };
    const sec = Math.floor(f.t);
    const cur = perSecond.get(sec) ?? { people: 0, vehicles: 0 };
    perSecond.set(sec, { people: Math.max(cur.people, counts.get("person") ?? 0), vehicles: Math.max(cur.vehicles, vehicles) });
  }

  const trackCount = new Map<string, number>();
  for (const t of tracks) trackCount.set(t.object_class.toLowerCase(), (trackCount.get(t.object_class.toLowerCase()) ?? 0) + 1);

  const classes = [...peak.entries()]
    .map(([cls, p]) => ({ cls, peak: p.n, peakAt: p.t, tracks: trackCount.get(cls) ?? 0 }))
    .filter((c) => c.cls !== "unknown")
    .sort((a, b) => b.peak - a.peak || b.tracks - a.tracks);
  const vehicleTracks = tracks.filter((t) => VEHICLE_CLASSES.has(t.object_class.toLowerCase())).length;
  const duration = boxes.frames.length ? boxes.frames[boxes.frames.length - 1].t : 0;
  const timeline = Array.from({ length: Math.floor(duration) + 1 }, (_, t) => ({ t, ...(perSecond.get(t) ?? { people: 0, vehicles: 0 }) }));

  return {
    classes,
    uncertain: trackCount.get("unknown") ?? 0,
    people: classes.find((c) => c.cls === "person") ?? null,
    vehicles: vehiclePeak.n ? { cls: "vehicle", peak: vehiclePeak.n, peakAt: vehiclePeak.t, tracks: vehicleTracks } : null,
    timeline,
    duration,
  };
}

const PLURAL: Record<string, string> = { person: "people", bus: "buses", "traffic light": "traffic lights", bench: "benches", vehicle: "vehicles" };

export function plural(cls: string, n: number): string {
  if (n === 1) return cls;
  return PLURAL[cls] ?? (cls.endsWith("s") ? cls : `${cls}s`);
}

/** Routine per-track bookkeeping; hidden by default so rule events stand out. */
export const ROUTINE_EVENTS = new Set(["object_appeared", "object_disappeared"]);

export const EVENT_LABELS: Record<string, string> = {
  object_appeared: "Appeared",
  object_disappeared: "Left view",
  person_entered: "Person entered",
  person_exited: "Person exited",
  vehicle_entered: "Vehicle entered",
  vehicle_exited: "Vehicle exited",
  line_crossed: "Crossed line",
  dwell_in_zone: "Stayed in zone",
  loitering: "Long time in view",
  crowd_detected: "Crowd",
  observation: "Video AI note",
};

export function eventLabel(e: Pick<VideoEvent, "event_type">): string {
  return EVENT_LABELS[e.event_type] ?? e.event_type.replace(/_/g, " ");
}

/** Human names for provenance / provider ids shown in the UI. */
export function providerLabel(id: string): string {
  return ({ local_vlm: "on this computer", gemini: "Gemini", together: "Together AI" } as Record<string, string>)[id] ?? id;
}

export const ANIMAL_CLASSES = new Set(["bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "rabbit", "animal"]);

export type Group = { key: "people" | "vehicles" | "animals" | "objects" | "unknown"; label: string; count: number; at: number | null; detail: string };

/**
 * The plain-language view: what kinds of things were seen, most at the same moment.
 * Animals and "other objects" take the largest single class in the group (per-class peaks can't be summed).
 */
export function groups(f: Findings): Group[] {
  const out: Group[] = [];
  if (f.people) out.push({ key: "people", label: plural("person", f.people.peak), count: f.people.peak, at: f.people.peakAt, detail: "at the same time" });
  if (f.vehicles) {
    const kinds = f.classes.filter((c) => VEHICLE_CLASSES.has(c.cls)).map((c) => plural(c.cls, 2));
    out.push({ key: "vehicles", label: f.vehicles.peak === 1 ? "vehicle" : "vehicles", count: f.vehicles.peak, at: f.vehicles.peakAt, detail: kinds.slice(0, 3).join(", ") });
  }
  const animals = f.classes.filter((c) => ANIMAL_CLASSES.has(c.cls));
  if (animals.length) {
    const top = animals[0];
    out.push({ key: "animals", label: top.peak === 1 ? "animal" : "animals", count: top.peak, at: top.peakAt, detail: animals.slice(0, 3).map((c) => plural(c.cls, 2)).join(", ") });
  }
  const others = f.classes.filter((c) => c.cls !== "person" && !VEHICLE_CLASSES.has(c.cls) && !ANIMAL_CLASSES.has(c.cls));
  if (others.length) {
    out.push({ key: "objects", label: others.length === 1 ? "other object type" : "other object types", count: others.length, at: null, detail: others.slice(0, 3).map((c) => plural(c.cls, 2)).join(", ") + (others.length > 3 ? "…" : "") });
  }
  if (f.uncertain) out.push({ key: "unknown", label: "need a closer look", count: f.uncertain, at: null, detail: "the detector was not sure" });
  return out;
}

/** Short spoken-style questions that fit what is in this video. */
export function suggestedQuestions(f: Findings | null, lang: string): string[] {
  const rw = lang === "rw";
  const qs: string[] = [rw ? "Ni iki kiri kuba muri iyi video?" : "What is happening in this video?"];
  if (f?.people) qs.push(rw ? "Ni abantu bangahe bagaragaye icyarimwe?" : "How many people were there at the same time?");
  if (f?.vehicles) qs.push(rw ? "Ni imodoka zingahe zagaragaye?" : "How many vehicles were there?");
  if (f?.uncertain) qs.push(rw ? "Ni ibihe bintu bitamenyekanye neza?" : "What objects could the AI not identify?");
  return qs.slice(0, 4);
}

export type SpeciesGroup = {
  key: string; // species name, or "unknown"
  label: string; // "hippopotamus", "Unknown animals"
  certain: boolean;
  animals: number; // separate tracks
  atOnce: number; // most visible in one frame
  peakAt: number | null;
  first: number;
  last: number;
  trackIds: number[];
  meanScore: number;
  candidates: string[]; // for uncertain animals: the best guesses
};

/** Wildlife run -> species list. Uncertain animals are grouped, never forced into a species. */
export function wildlifeGroups(boxes: BoxTrack | null, tracks: Track[]): SpeciesGroup[] {
  const groups = new Map<string, SpeciesGroup>();
  for (const t of tracks) {
    const w = t.wildlife;
    if (!w) continue;
    const key = w.certain && w.species ? w.species : "unknown";
    const g =
      groups.get(key) ??
      { key, label: key === "unknown" ? "Unknown animals" : key, certain: key !== "unknown", animals: 0, atOnce: 0, peakAt: null, first: t.first_seen, last: t.last_seen, trackIds: [], meanScore: 0, candidates: [] };
    g.animals += 1;
    g.first = Math.min(g.first, t.first_seen);
    g.last = Math.max(g.last, t.last_seen);
    g.trackIds.push(t.track_id);
    g.meanScore += w.score;
    if (!w.certain && w.candidate && !g.candidates.includes(w.candidate)) g.candidates.push(w.candidate);
    groups.set(key, g);
  }
  if (boxes) {
    const confirm = boxes.confirm_confidence ?? 0.5;
    const trackKey = new Map<number, string>();
    for (const [key, g] of groups) for (const id of g.trackIds) trackKey.set(id, key);
    for (const f of boxes.frames) {
      const counts = new Map<string, number>();
      for (const o of f.o) {
        const key = trackKey.get(o[0]);
        if (key && o[2] >= confirm) counts.set(key, (counts.get(key) ?? 0) + 1);
      }
      for (const [key, n] of counts) {
        const g = groups.get(key)!;
        if (n > g.atOnce) {
          g.atOnce = n;
          g.peakAt = f.t;
        }
      }
    }
  }
  const list = [...groups.values()].map((g) => ({ ...g, meanScore: g.animals ? g.meanScore / g.animals : 0, atOnce: g.atOnce || 1 }));
  return list.sort((a, b) => (a.key === "unknown" ? 1 : b.key === "unknown" ? -1 : b.atOnce - a.atOnce || b.animals - a.animals));
}

/** How a wildlife track is named on the video and in lists. */
export function wildlifeLabel(t: Track): string {
  const w = t.wildlife;
  if (!w) return t.object_class;
  if (w.certain && w.species) return w.species;
  return w.candidate ? `possible ${w.candidate}` : "animal";
}

export function titleCase(s: string): string {
  return s.replace(/\b\w/g, (c) => c.toUpperCase());
}
