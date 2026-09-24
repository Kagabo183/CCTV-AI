"""Where the pipeline's frames come from.

sequential  one reader, frame after frame (local files: disk is fast)
parallel    online streams: YouTube serves each connection at only ~2-3x real time,
            so the video is cut into contiguous segments read by several connections at
            once. Sampled frames are handed to the detector strictly in time order, so the
            tracker sees the same sequence as with one reader. Buffered frames are kept
            JPEG-compressed to bound memory.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_DONE = object()


def sequential(cap: cv2.VideoCapture, step: int, end_frame: int | None) -> Iterator[tuple[int, np.ndarray]]:
    index = 0
    while end_frame is None or index < end_frame:
        if not cap.grab():
            return
        if index % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                return
            yield index, frame
        index += 1


def parallel(url: str, src_fps: float, step: int, end_frame: int, workers: int = 4) -> Iterator[tuple[int, np.ndarray]]:
    """Read [0, end_frame) of a network stream with `workers` connections; yield sampled frames in order."""
    per = -(-end_frame // workers)
    per += (-per) % step  # segment starts stay on the sampling grid
    segments = [(s, min(s + per, end_frame)) for s in range(0, end_frame, per)]
    queues: list[queue.Queue[object]] = [queue.Queue() for _ in segments]
    stop = threading.Event()

    def read(start: int, end: int, out: queue.Queue[object]) -> None:
        cap = cv2.VideoCapture(url)
        try:
            if not cap.isOpened():
                raise RuntimeError("could not open stream")
            if start:
                cap.set(cv2.CAP_PROP_POS_MSEC, start / src_fps * 1000.0)
            index = start
            while index < end and not stop.is_set():
                if not cap.grab():
                    break
                if index % step == 0:
                    ok, frame = cap.retrieve()
                    if not ok:
                        break
                    ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
                    if ok:
                        out.put((index, jpg))
                index += 1
        except Exception:  # noqa: BLE001 - a failed segment ends early; the others still count
            logger.exception("Stream segment %s-%s failed", start, end)
        finally:
            cap.release()
            out.put(_DONE)

    threads = [threading.Thread(target=read, args=(s, e, q), daemon=True) for (s, e), q in zip(segments, queues)]
    for t in threads:
        t.start()
    try:
        for q in queues:
            while True:
                item = q.get()
                if item is _DONE:
                    break
                index, jpg = item  # type: ignore[misc]
                frame = cv2.imdecode(jpg, cv2.IMREAD_COLOR)
                if frame is not None:
                    yield index, frame
    finally:
        stop.set()
