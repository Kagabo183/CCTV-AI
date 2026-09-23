"use client";

import { useCallback, useRef, useState } from "react";

const MAX_SECONDS = 60;

/** Microphone capture via MediaRecorder. Resolves with the recorded blob on stop. */
export function useRecorder() {
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const resolveRef = useRef<((b: Blob | null) => void) | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef<Blob | null>(null); // recording that hit the time limit before stop()

  const start = useCallback(async () => {
    setError(null);
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError("This browser cannot record audio.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"].find((m) =>
        MediaRecorder.isTypeSupported(m),
      );
      const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      const chunks: BlobPart[] = [];
      recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      recorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = chunks.length ? new Blob(chunks, { type: (recorder.mimeType || "audio/webm").split(";")[0] }) : null;
        if (resolveRef.current) resolveRef.current(blob);
        else pendingRef.current = blob;
        resolveRef.current = null;
      };
      pendingRef.current = null;
      recorder.start();
      recorderRef.current = recorder;
      setRecording(true);
      timerRef.current = setTimeout(() => recorder.state === "recording" && recorder.stop(), MAX_SECONDS * 1000);
    } catch {
      setError("Microphone access was denied.");
    }
  }, []);

  const stop = useCallback((): Promise<Blob | null> => {
    const recorder = recorderRef.current;
    if (timerRef.current) clearTimeout(timerRef.current);
    setRecording(false);
    if (!recorder || recorder.state !== "recording") {
      const pending = pendingRef.current;
      pendingRef.current = null;
      return Promise.resolve(pending);
    }
    return new Promise((resolve) => {
      resolveRef.current = resolve;
      recorder.stop();
    });
  }, []);

  return { recording, error, start, stop };
}
