"use client";

import { useEffect, useState } from "react";
import { AuthScreen } from "@/components/AuthScreen";
import { CamerasApp } from "@/components/cameras/CamerasApp";
import { NavRail, type Section } from "@/components/NavRail";
import { Workspace } from "@/components/Workspace";
import { api, ApiError } from "@/lib/api";
import type { User } from "@/lib/types";

const SECTIONS: Section[] = ["assistant", "cameras", "live", "events", "recordings", "gateways"];

function sectionFromHash(): Section {
  const h = typeof window !== "undefined" ? window.location.hash.slice(1) : "";
  return (SECTIONS as string[]).includes(h) ? (h as Section) : "assistant";
}

export default function Home() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [offline, setOffline] = useState(false);
  // Read once on the client; nothing section-specific renders before sign-in, so there is no hydration mismatch.
  const [section, setSection] = useState<Section>(sectionFromHash);

  // Only a real 401 means "signed out". If the server is restarting or unreachable,
  // keep the session and retry instead of showing the sign-in screen.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = () =>
      api
        .me()
        .then((u) => {
          setOffline(false);
          setUser(u);
        })
        .catch((e) => {
          if (e instanceof ApiError && e.status === 401) {
            setOffline(false);
            setUser(null);
          } else {
            setOffline(true);
            timer = setTimeout(check, 2000);
          }
        });
    check();
    return () => clearTimeout(timer);
  }, []);

  // The section lives in the URL hash so reload and the back button keep it.
  useEffect(() => {
    const onHash = () => setSection(sectionFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const [navTick, setNavTick] = useState(0); // every nav click, even on the current section, returns to its start page
  const go = (s: Section) => {
    if (window.location.hash.slice(1) !== s) window.location.hash = s;
    setSection(s);
    setNavTick((t) => t + 1);
  };

  if (user === undefined) {
    return (
      <div className="grid h-full place-items-center text-sm text-faint" role="status">
        {offline ? "Connecting to the Visionary server…" : "Loading…"}
      </div>
    );
  }
  if (user === null) return <AuthScreen onAuthed={setUser} />;
  return (
    <div className="flex h-full flex-col lg:flex-row">
      <NavRail section={section} onChange={go} />
      <main className="min-h-0 min-w-0 flex-1">
        {section === "assistant" ? (
          <Workspace
            user={user}
            onLogout={async () => {
              await api.logout().catch(() => undefined);
              setUser(null);
            }}
          />
        ) : (
          <CamerasApp
            section={section}
            navTick={navTick}
            onAsk={(cameraId) => {
              try {
                localStorage.setItem("visionary:last-source", cameraId); // the assistant opens on this camera
              } catch {}
              go("assistant");
            }}
          />
        )}
      </main>
    </div>
  );
}
