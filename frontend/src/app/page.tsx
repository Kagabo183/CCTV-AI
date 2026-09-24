"use client";

import { useEffect, useState } from "react";
import { AuthScreen } from "@/components/AuthScreen";
import { Workspace } from "@/components/Workspace";
import { api, ApiError } from "@/lib/api";
import type { User } from "@/lib/types";

export default function Home() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [offline, setOffline] = useState(false);

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

  if (user === undefined) {
    return (
      <div className="grid h-full place-items-center text-sm text-faint" role="status">
        {offline ? "Connecting to the Visionary server…" : "Loading…"}
      </div>
    );
  }
  if (user === null) return <AuthScreen onAuthed={setUser} />;
  return (
    <Workspace
      user={user}
      onLogout={async () => {
        await api.logout().catch(() => undefined);
        setUser(null);
      }}
    />
  );
}
