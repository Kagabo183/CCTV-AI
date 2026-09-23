"use client";

import { useEffect, useState } from "react";
import { AuthScreen } from "@/components/AuthScreen";
import { Workspace } from "@/components/Workspace";
import { api } from "@/lib/api";
import type { User } from "@/lib/types";

export default function Home() {
  const [user, setUser] = useState<User | null | undefined>(undefined);

  useEffect(() => {
    api.me().then(setUser).catch(() => setUser(null));
  }, []);

  if (user === undefined) {
    return <div className="grid h-full place-items-center text-sm text-faint">Loading…</div>;
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
