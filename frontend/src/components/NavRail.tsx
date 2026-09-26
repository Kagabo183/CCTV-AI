"use client";

import { ChatIcon, EyeIcon, FilmIcon, GridIcon, PulseIcon, ServerIcon, CameraIcon } from "./icons";

export type Section = "assistant" | "cameras" | "live" | "events" | "recordings" | "gateways";

const ITEMS: { id: Section; label: string; icon: typeof ChatIcon }[] = [
  { id: "cameras", label: "Cameras", icon: GridIcon },
  { id: "live", label: "Live", icon: CameraIcon },
  { id: "assistant", label: "Assistant", icon: ChatIcon },
  { id: "events", label: "Events", icon: PulseIcon },
  { id: "recordings", label: "Recordings", icon: FilmIcon },
  { id: "gateways", label: "Gateways", icon: ServerIcon },
];

/** Left rail on desktop, bottom tab bar on phones. */
export function NavRail({ section, onChange }: { section: Section; onChange: (s: Section) => void }) {
  return (
    <nav aria-label="Main" className="order-last flex shrink-0 border-t border-line bg-panel lg:order-first lg:w-[76px] lg:flex-col lg:border-r lg:border-t-0">
      <div className="hidden h-14 place-items-center border-b border-line lg:grid" aria-hidden>
        <span className="grid h-9 w-9 place-items-center rounded-lg bg-accent/15 text-accent">
          <EyeIcon width={18} height={18} />
        </span>
      </div>
      <ul className="flex flex-1 justify-around lg:flex-col lg:justify-start lg:gap-1 lg:p-2">
        {ITEMS.map((it) => {
          const active = section === it.id;
          return (
            <li key={it.id} className={it.id === "recordings" || it.id === "gateways" ? "hidden sm:block" : ""}>
              <button
                onClick={() => onChange(it.id)}
                aria-current={active ? "page" : undefined}
                className={`flex min-h-14 w-full min-w-14 cursor-pointer flex-col items-center justify-center gap-1 rounded-xl px-1 text-[11px] transition ${
                  active ? "text-accent lg:bg-accent/10" : "text-muted hover:text-text lg:hover:bg-panel-2"
                }`}
              >
                <it.icon width={19} height={19} aria-hidden />
                {it.label}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
