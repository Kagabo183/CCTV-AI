"use client";

import { useCallback, useEffect, useState } from "react";
import { cams, type Camera, type Overview } from "@/lib/cameras";
import { AddCameraWizard } from "./AddCameraWizard";
import { CameraDetail } from "./CameraDetail";
import { CameraGrid } from "./CameraGrid";
import { EventsFeed, GatewaysView, LiveWall, RecordingsView } from "./Views";

export type CameraSection = "cameras" | "live" | "events" | "recordings" | "gateways";

export function CamerasApp({ section, navTick = 0, onAsk }: { section: CameraSection; navTick?: number; onAsk: (cameraId: string) => void }) {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  const [opened, setOpened] = useState<{ id: string; section: CameraSection; tick: number } | null>(null);
  // switching sections, or clicking the current one in the nav, closes the camera page
  const open = opened?.section === section && opened.tick === navTick ? opened.id : null;
  const setOpen = (id: string | null) => setOpened(id ? { id, section, tick: navTick } : null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(
    () =>
      Promise.all([cams.list(), cams.overview()])
        .then(([list, ov]) => {
          setCameras(list);
          setOverview(ov);
        })
        .catch(() => undefined) // keep the last list; the shell shows connectivity problems
        .finally(() => setLoading(false)),
    [],
  );
  useEffect(() => {
    const first = setTimeout(load, 0);
    const id = setInterval(load, 5000);
    return () => {
      clearTimeout(first);
      clearInterval(id);
    };
  }, [load]);

  const openCam = (c: Camera) => setOpen(c.id);
  let body: React.ReactNode;
  if (open) body = <CameraDetail cameraId={open} onBack={() => setOpen(null)} onAsk={(c) => onAsk(c.id)} onDeleted={() => { setOpen(null); load(); }} />;
  else if (section === "live") body = <LiveWall cameras={cameras} onOpen={openCam} />;
  else if (section === "events") body = <EventsFeed cameras={cameras} onOpen={openCam} />;
  else if (section === "recordings") body = <RecordingsView cameras={cameras} />;
  else if (section === "gateways") body = <GatewaysView />;
  else body = <CameraGrid cameras={cameras} overview={overview} onOpen={openCam} onAdd={() => setAdding(true)} loading={loading} />;

  return (
    <div className="h-full overflow-y-auto">
      {body}
      {adding && (
        <AddCameraWizard
          onClose={() => setAdding(false)}
          onAdded={(made) => {
            setAdding(false);
            load();
            const first = made.find((c) => c.kind !== "nvr") ?? made[0];
            if (first && made.length === 1) setOpen(first.id);
          }}
        />
      )}
    </div>
  );
}
