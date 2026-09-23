"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { User, VideoSource } from "@/lib/types";
import { Brand } from "./AuthScreen";
import { CameraIcon, LinkIcon, LogoutIcon, PlusIcon, TrashIcon, UploadIcon, XIcon } from "./icons";

const SOURCE_KIND_LABELS: Record<string, string> = { url: "Video URL", upload: "Uploaded video", local: "Local file" };

type Props = {
  user: User;
  sources: VideoSource[];
  selectedId: string | null;
  sourceKinds: string[];
  onSelect: (source: VideoSource) => void;
  onAdded: (source: VideoSource) => void;
  onDeleted: (id: string) => void;
  onLogout: () => void;
};

export function Sidebar({ user, sources, selectedId, sourceKinds, onSelect, onAdded, onDeleted, onLogout }: Props) {
  const [adding, setAdding] = useState(false);

  return (
    <aside className="flex h-full min-h-0 flex-col border-line bg-panel lg:border-r">
      <div className="flex items-center justify-between px-4 py-4">
        <Brand />
      </div>

      <div className="flex items-center justify-between px-4 pb-2 pt-3">
        <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-faint">Cameras &amp; videos</span>
        <button
          onClick={() => setAdding(true)}
          className="grid h-7 w-7 place-items-center rounded-md text-muted transition hover:bg-panel-2 hover:text-text"
          title="Add video source"
        >
          <PlusIcon width={16} height={16} />
        </button>
      </div>

      <nav className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-4">
        {sources.length === 0 && (
          <button
            onClick={() => setAdding(true)}
            className="mx-2 mt-2 w-[calc(100%-1rem)] rounded-lg border border-dashed border-line-2 p-4 text-left text-sm text-muted transition hover:border-accent/50 hover:text-text"
          >
            <span className="block font-medium text-text">Add your first video</span>
            Paste a video URL or upload a recording to start asking questions.
          </button>
        )}
        {sources.map((s) => {
          const active = s.id === selectedId;
          return (
            <div
              key={s.id}
              className={`group flex items-center gap-3 rounded-lg px-2.5 py-2 transition ${
                active ? "bg-panel-2 ring-1 ring-line-2" : "hover:bg-panel-2/60"
              }`}
            >
              <button onClick={() => onSelect(s)} className="flex min-w-0 flex-1 items-center gap-3 text-left">
                <span
                  className={`grid h-8 w-8 shrink-0 place-items-center rounded-md ${
                    active ? "bg-accent/15 text-accent" : "bg-bg text-muted"
                  }`}
                >
                  <CameraIcon width={16} height={16} />
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium">{s.name}</span>
                  <span className="block truncate text-xs text-faint">
                    {s.location ? `${s.location} · ` : ""}
                    {s.status === "ready" ? (SOURCE_KIND_LABELS[s.kind] ?? "Video URL") : s.status}
                  </span>
                </span>
              </button>
              <button
                onClick={async () => {
                  if (!confirm(`Remove “${s.name}”? Conversations about it will lose their video.`)) return;
                  await api.deleteSource(s.id);
                  onDeleted(s.id);
                }}
                className="hidden h-7 w-7 shrink-0 place-items-center rounded-md text-faint hover:bg-danger/10 hover:text-danger group-hover:grid"
                title="Remove"
              >
                <TrashIcon width={15} height={15} />
              </button>
            </div>
          );
        })}

        <div className="mx-2 mt-6 rounded-lg border border-line p-3 text-xs text-faint">
          <span className="font-medium text-muted">Live cameras</span>
          <p className="mt-1">RTSP / ONVIF / NVR connections are coming in the next phase.</p>
        </div>
      </nav>

      <div className="flex items-center gap-3 border-t border-line px-4 py-3">
        <div className="grid h-8 w-8 place-items-center rounded-full bg-panel-2 text-xs font-semibold uppercase text-muted">
          {(user.full_name || user.email).slice(0, 1)}
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm">{user.full_name || user.email}</p>
          {user.full_name && <p className="truncate text-xs text-faint">{user.email}</p>}
        </div>
        <button onClick={onLogout} className="text-faint transition hover:text-text" title="Sign out">
          <LogoutIcon width={16} height={16} />
        </button>
      </div>

      {adding && (
        <AddSourceDialog
          kinds={sourceKinds}
          onClose={() => setAdding(false)}
          onAdded={(s) => {
            setAdding(false);
            onAdded(s);
          }}
        />
      )}
    </aside>
  );
}

type SourceTab = "url" | "upload" | "local";
const TAB_LABELS: Record<SourceTab, string> = { url: "Video URL", upload: "Upload file", local: "Local file (dev)" };
const MAX_UPLOAD_MB = 500;

function AddSourceDialog({
  kinds,
  onClose,
  onAdded,
}: {
  kinds: string[];
  onClose: () => void;
  onAdded: (s: VideoSource) => void;
}) {
  const tabs = (["url", "upload", "local"] as const).filter((k) => kinds.includes(k));
  const [kind, setKind] = useState<SourceTab>("url");
  const [uri, setUri] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [location, setLocation] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function pickFile(f: File | undefined) {
    if (!f) return;
    setError(null);
    if (!f.type.startsWith("video/") && !/\.(mp4|m4v|mov|webm|mkv|avi|mpe?g|3gp)$/i.test(f.name)) {
      setError("Please choose a video file (MP4, MOV, WebM, MKV, AVI).");
      return;
    }
    if (f.size > MAX_UPLOAD_MB * 1024 * 1024) {
      setError(`This file is ${Math.round(f.size / 1024 / 1024)} MB. The limit is ${MAX_UPLOAD_MB} MB.`);
      return;
    }
    setFile(f);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (kind === "upload") {
        if (!file) return;
        setProgress(0);
        onAdded(await api.uploadSource(file, name.trim() || guessName(file.name), location.trim(), setProgress));
      } else {
        onAdded(
          await api.addSource({ kind, uri: uri.trim(), name: name.trim() || guessName(uri), location: location.trim() || undefined }),
        );
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not add this video");
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }

  const ready = kind === "upload" ? file !== null : uri.trim().length > 0;
  const busyLabel =
    kind === "upload" ? (progress !== null && progress < 1 ? `Uploading ${Math.round(progress * 100)}%` : "Processing…") : "Validating…";

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4 backdrop-blur-sm" onClick={busy ? undefined : onClose}>
      <form
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-lg space-y-5 rounded-2xl border border-line-2 bg-panel p-6 shadow-2xl"
      >
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-lg font-semibold">Connect a video</h2>
            <p className="mt-1 text-sm text-muted">
              {kind === "upload" ? "The file is checked and stored privately on the server." : "The link is validated before it is added."}
            </p>
          </div>
          <button type="button" onClick={onClose} disabled={busy} className="text-faint hover:text-text disabled:opacity-40">
            <XIcon />
          </button>
        </div>

        {tabs.length > 1 && (
          <div
            className="grid gap-1 rounded-lg bg-bg p-1 text-sm"
            style={{ gridTemplateColumns: `repeat(${tabs.length}, minmax(0, 1fr))` }}
          >
            {tabs.map((k) => (
              <button
                type="button"
                key={k}
                disabled={busy}
                onClick={() => {
                  setKind(k);
                  setError(null);
                }}
                className={`rounded-md py-1.5 transition ${kind === k ? "bg-panel-2 text-text" : "text-muted hover:text-text"}`}
              >
                {TAB_LABELS[k]}
              </button>
            ))}
          </div>
        )}

        {kind === "upload" ? (
          <div>
            <label
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                pickFile(e.dataTransfer.files[0]);
              }}
              className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border border-dashed px-4 py-8 text-center transition ${
                dragging ? "border-accent bg-accent/5" : "border-line-2 hover:border-accent/50"
              }`}
            >
              <UploadIcon className={file ? "text-accent" : "text-faint"} width={24} height={24} />
              {file ? (
                <>
                  <span className="max-w-full truncate text-sm font-medium">{file.name}</span>
                  <span className="text-xs text-faint">{(file.size / 1024 / 1024).toFixed(1)} MB · click to choose another</span>
                </>
              ) : (
                <>
                  <span className="text-sm font-medium">Drop a video here or click to browse</span>
                  <span className="text-xs text-faint">MP4, MOV, WebM, MKV or AVI · up to {MAX_UPLOAD_MB} MB</span>
                </>
              )}
              <input
                type="file"
                accept="video/*,.mkv"
                className="hidden"
                disabled={busy}
                onChange={(e) => pickFile(e.target.files?.[0])}
              />
            </label>
            {progress !== null && (
              <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-line-2">
                <div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${Math.round(progress * 100)}%` }} />
              </div>
            )}
          </div>
        ) : (
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-muted">{kind === "url" ? "Video URL" : "File name in the local video folder"}</span>
            <div className="flex items-center gap-2 rounded-lg border border-line-2 bg-bg px-3 focus-within:border-accent/60">
              <LinkIcon className="shrink-0 text-faint" width={16} height={16} />
              <input
                autoFocus
                required
                value={uri}
                onChange={(e) => setUri(e.target.value)}
                placeholder={kind === "url" ? "https://example.com/security-video.mp4" : "gate-camera.mp4"}
                className="w-full bg-transparent py-2.5 text-sm outline-none placeholder:text-faint"
              />
            </div>
            {kind === "url" && (
              <span className="block text-xs text-faint">Direct links to MP4 / WebM / MOV files, or a YouTube link.</span>
            )}
          </label>
        )}

        <div className="grid grid-cols-2 gap-3">
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-muted">Camera name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Camera y'irembo"
              className="w-full rounded-lg border border-line-2 bg-bg px-3 py-2.5 text-sm outline-none placeholder:text-faint focus:border-accent/60"
            />
          </label>
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-muted">Location</span>
            <input
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              placeholder="irembo"
              className="w-full rounded-lg border border-line-2 bg-bg px-3 py-2.5 text-sm outline-none placeholder:text-faint focus:border-accent/60"
            />
          </label>
        </div>
        <p className="-mt-2 text-xs text-faint">
          The location lets you refer to this camera by voice, e.g. “camera yo ku <em>irembo</em>”.
        </p>

        {error && <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">{error}</p>}

        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="rounded-lg px-4 py-2 text-sm text-muted hover:text-text disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            disabled={busy || !ready}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-accent-ink transition hover:brightness-110 disabled:opacity-50"
          >
            {busy ? busyLabel : kind === "upload" ? "Upload" : "Connect"}
          </button>
        </div>
      </form>
    </div>
  );
}

function guessName(uri: string): string {
  try {
    const last = new URL(uri).pathname.split("/").filter(Boolean).pop() ?? "Video";
    return decodeURIComponent(last).replace(/\.[a-z0-9]+$/i, "").slice(0, 60) || "Video";
  } catch {
    return uri.split(/[\\/]/).pop()?.replace(/\.[a-z0-9]+$/i, "") || "Video";
  }
}
