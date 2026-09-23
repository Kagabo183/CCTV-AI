"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { User } from "@/lib/types";
import { EyeIcon } from "./icons";

export function AuthScreen({ onAuthed }: { onAuthed: (user: User) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = mode === "login" ? await api.login(email, password) : await api.register(email, password, name);
      onAuthed(res.user);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="grid min-h-full lg:grid-cols-[1.1fr_1fr]">
      <section className="relative hidden overflow-hidden border-r border-line bg-panel lg:block">
        <div className="scanlines absolute inset-0" />
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_30%_40%,color-mix(in_srgb,var(--accent)_14%,transparent),transparent_60%)]" />
        <div className="relative flex h-full flex-col justify-between p-12">
          <Brand />
          <div className="max-w-md space-y-6">
            <p className="font-mono text-xs uppercase tracking-[0.2em] text-accent">Camera assistant</p>
            <h1 className="text-4xl font-semibold leading-tight tracking-tight">
              Vugana na camera zawe
              <span className="block text-muted">mu Kinyarwanda.</span>
            </h1>
            <div className="space-y-3 rounded-xl border border-line-2 bg-bg/60 p-4 text-sm backdrop-blur">
              <p className="text-muted">
                <span className="mr-2 font-mono text-xs text-faint">YOU</span>Ni iki kiri kuba kuri camera yo ku irembo?
              </p>
              <p>
                <span className="mr-2 font-mono text-xs text-accent">AI</span>Hari abantu babiri n&apos;imodoka imwe
                ihagaze hafi y&apos;irembo.
              </p>
            </div>
          </div>
          <p className="text-xs text-faint">Footage stays on the server. Provider keys never reach the browser.</p>
        </div>
      </section>

      <section className="flex items-center justify-center p-6">
        <form onSubmit={submit} className="w-full max-w-sm space-y-5">
          <div className="lg:hidden">
            <Brand />
          </div>
          <div>
            <h2 className="text-2xl font-semibold">{mode === "login" ? "Sign in" : "Create account"}</h2>
            <p className="mt-1 text-sm text-muted">
              {mode === "login" ? "Welcome back. Your cameras are waiting." : "Start talking to your video sources."}
            </p>
          </div>
          {mode === "register" && <Field label="Full name" value={name} onChange={setName} autoComplete="name" />}
          <Field label="Email" type="email" value={email} onChange={setEmail} autoComplete="email" required />
          <Field
            label="Password"
            type="password"
            value={password}
            onChange={setPassword}
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            required
            minLength={mode === "register" ? 8 : undefined}
          />
          {error && <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">{error}</p>}
          <button
            disabled={busy}
            className="w-full rounded-lg bg-accent py-2.5 text-sm font-semibold text-accent-ink transition hover:brightness-110 disabled:opacity-60"
          >
            {busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
          </button>
          <p className="text-center text-sm text-muted">
            {mode === "login" ? "New here?" : "Already have an account?"}{" "}
            <button
              type="button"
              className="text-text underline-offset-4 hover:underline"
              onClick={() => {
                setMode(mode === "login" ? "register" : "login");
                setError(null);
              }}
            >
              {mode === "login" ? "Create an account" : "Sign in"}
            </button>
          </p>
        </form>
      </section>
    </main>
  );
}

export function Brand() {
  return (
    <div className="flex items-center gap-2.5">
      <div className="grid h-8 w-8 place-items-center rounded-lg bg-accent/15 text-accent ring-1 ring-accent/30">
        <EyeIcon width={17} height={17} />
      </div>
      <span className="text-[15px] font-semibold tracking-tight">Visionary</span>
    </div>
  );
}

function Field({
  label,
  onChange,
  ...props
}: { label: string; onChange: (v: string) => void } & Omit<React.InputHTMLAttributes<HTMLInputElement>, "onChange">) {
  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-medium text-muted">{label}</span>
      <input
        {...props}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg border border-line-2 bg-panel px-3 py-2.5 text-sm outline-none transition placeholder:text-faint focus:border-accent/60 focus:ring-2 focus:ring-accent/15"
      />
    </label>
  );
}
