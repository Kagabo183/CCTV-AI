"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { Conversation, User, VideoSource } from "@/lib/types";
import { CameraIcon, ChatIcon, EyeIcon, LogoutIcon, SearchIcon } from "./icons";

type Props = {
  user: User;
  sources: VideoSource[];
  history: Conversation[];
  currentSource: VideoSource | null;
  aiDetails: boolean;
  onAiDetails: (v: boolean) => void;
  onSelectSource: (s: VideoSource) => void;
  onOpenConversation: (id: string) => void;
  onMenu: () => void;
  onLogout: () => void;
};

export function TopBar({ user, sources, history, currentSource, aiDetails, onAiDetails, onSelectSource, onOpenConversation, onMenu, onLogout }: Props) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-line bg-panel px-3 lg:px-4">
      <button onClick={onMenu} className="grid h-10 w-10 cursor-pointer place-items-center rounded-lg text-muted hover:bg-panel-2 lg:hidden" aria-label="Open camera list">
        <CameraIcon width={18} height={18} />
      </button>
      <div className="flex items-center gap-2.5">
        <span className="grid h-8 w-8 place-items-center rounded-lg bg-accent/15 text-accent" aria-hidden>
          <EyeIcon width={17} height={17} />
        </span>
        <div className="hidden leading-tight sm:block">
          <p className="text-[15px] font-semibold tracking-tight">Visionary</p>
          <p className="text-[11px] text-muted">Talk to your cameras</p>
        </div>
      </div>

      <SearchBox sources={sources} history={history} currentSource={currentSource} onSelectSource={onSelectSource} onOpenConversation={onOpenConversation} />

      <button
        role="switch"
        aria-checked={aiDetails}
        onClick={() => onAiDetails(!aiDetails)}
        className="hidden min-h-9 cursor-pointer items-center gap-2 rounded-lg px-2.5 text-xs text-muted transition hover:bg-panel-2 hover:text-text md:inline-flex"
        title="Show model names, confidence scores and object tables"
      >
        <span className={`relative h-4 w-7 rounded-full transition ${aiDetails ? "bg-accent" : "bg-line-2"}`} aria-hidden>
          <span className={`absolute top-0.5 h-3 w-3 rounded-full bg-bg transition-all ${aiDetails ? "left-3.5" : "left-0.5"}`} />
        </span>
        AI details
      </button>

      <UserMenu user={user} onLogout={onLogout} />
    </header>
  );
}

function SearchBox({ sources, history, currentSource, onSelectSource, onOpenConversation }: Pick<Props, "sources" | "history" | "currentSource" | "onSelectSource" | "onOpenConversation">) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        box.current?.querySelector("input")?.focus();
      }
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", key);
    };
  }, []);

  const results = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const cams = sources.filter((s) => !needle || `${s.name} ${s.location ?? ""}`.toLowerCase().includes(needle)).slice(0, 6);
    const convs = history.filter((c) => !needle || (c.title ?? "").toLowerCase().includes(needle)).slice(0, 6);
    return [...cams.map((s) => ({ kind: "camera" as const, s })), ...convs.map((c) => ({ kind: "conversation" as const, c }))];
  }, [q, sources, history]);

  function choose(i: number) {
    const r = results[i];
    if (!r) return;
    if (r.kind === "camera") onSelectSource(r.s);
    else onOpenConversation(r.c.id);
    setOpen(false);
    setQ("");
  }

  return (
    <div ref={box} className="relative mx-auto min-w-0 flex-1 sm:max-w-xl">
      <label className="flex min-h-10 items-center gap-2 rounded-lg border border-line-2 bg-bg px-3 text-sm focus-within:border-accent/50">
        <SearchIcon width={16} height={16} className="shrink-0 text-faint" aria-hidden />
        <span className="sr-only">Search cameras and past questions</span>
        <input
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setOpen(true);
            setActive(0);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActive((a) => Math.min(a + 1, results.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((a) => Math.max(a - 1, 0));
            } else if (e.key === "Enter") choose(active);
            else if (e.key === "Escape") setOpen(false);
          }}
          role="combobox"
          aria-expanded={open}
          aria-controls="search-results"
          aria-activedescendant={open && results[active] ? `search-${active}` : undefined}
          placeholder="Search cameras, questions"
          className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-faint"
        />
        <kbd className="hidden rounded border border-line-2 px-1.5 font-mono text-[10px] text-faint sm:inline">Ctrl K</kbd>
      </label>
      {open && (
        <ul id="search-results" role="listbox" className="absolute inset-x-0 top-12 z-50 max-h-96 overflow-y-auto rounded-xl border border-line-2 bg-panel p-1.5 shadow-2xl">
          {results.length === 0 && <li className="px-3 py-3 text-sm text-muted">Nothing matches “{q}”.</li>}
          {results.map((r, i) => (
            <li
              key={r.kind === "camera" ? r.s.id : r.c.id}
              id={`search-${i}`}
              role="option"
              aria-selected={i === active}
              onMouseEnter={() => setActive(i)}
              onClick={() => choose(i)}
              className={`flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 text-sm ${i === active ? "bg-panel-2" : ""}`}
            >
              {r.kind === "camera" ? (
                <>
                  <CameraIcon width={15} height={15} className="shrink-0 text-faint" aria-hidden />
                  <span className="min-w-0 flex-1 truncate">{r.s.name}</span>
                  <span className="text-xs text-faint">{r.s.id === currentSource?.id ? "open now" : "camera"}</span>
                </>
              ) : (
                <>
                  <ChatIcon width={15} height={15} className="shrink-0 text-faint" aria-hidden />
                  <span className="min-w-0 flex-1 truncate">{r.c.title || "Untitled conversation"}</span>
                  <span className="text-xs text-faint">{new Date(r.c.updated_at).toLocaleDateString()}</span>
                </>
              )}
            </li>
          ))}
          {history.length > 0 && <li className="px-3 pb-1 pt-2 text-[11px] text-faint">Past questions are from the camera that is open.</li>}
        </ul>
      )}
    </div>
  );
}

function UserMenu({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-label="Account"
        className="grid h-9 w-9 cursor-pointer place-items-center rounded-full bg-panel-2 text-sm font-semibold uppercase text-muted transition hover:text-text"
      >
        {(user.full_name || user.email).slice(0, 1)}
      </button>
      {open && (
        <div className="absolute right-0 top-11 z-50 w-60 rounded-xl border border-line-2 bg-panel p-1.5 shadow-2xl">
          <div className="px-3 py-2">
            <p className="truncate text-sm">{user.full_name || user.email}</p>
            {user.full_name && <p className="truncate text-xs text-faint">{user.email}</p>}
          </div>
          <button onClick={onLogout} className="flex min-h-9 w-full cursor-pointer items-center gap-2 rounded-lg px-3 text-sm text-muted hover:bg-panel-2 hover:text-text">
            <LogoutIcon width={15} height={15} aria-hidden /> Sign out
          </button>
        </div>
      )}
    </div>
  );
}
