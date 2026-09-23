"""Real detector + tracker + event engine on a real clip (needs GPU, weights, sample video).

Skipped automatically when unavailable. Fetch the clip with:
  curl -L -o sample_videos/people-detection.mp4 https://github.com/intel-iot-devkit/sample-videos/raw/master/people-detection.mp4
"""

from __future__ import annotations

from pathlib import Path

import pytest

VIDEO = Path("sample_videos/people-detection.mp4")
WEIGHTS = Path("models/yolo26s.pt")


def _cuda() -> bool:
    try:
        import torch

        return torch.cuda.is_available()
    except ImportError:
        return False


@pytest.mark.skipif(not (VIDEO.exists() and WEIGHTS.exists() and _cuda()), reason="needs CUDA, yolo26s.pt and the sample clip")
def test_people_are_detected_tracked_and_turned_into_events() -> None:
    from app.vision.detectors import build_detector
    from app.vision.events import SceneConfig
    from app.vision.pipeline import VisionPipeline
    from app.vision.trackers import build_tracker

    pipeline = VisionPipeline(build_detector("yolo", weights_dir=Path("models")), build_tracker("bytetrack", processing_fps=10), SceneConfig(), sample_fps=10)
    result = pipeline.run(VIDEO, max_seconds=35)

    people = [t for t in result.tracks.values() if t.object_class == "person"]
    assert len(people) >= 3  # the hallway clip shows several people by 30 s
    assert max(t.last_seen - t.first_seen for t in people) > 3  # identities persist across frames
    assert result.stats["detections"]["person"]["mean_confidence"] > 0.6
    assert "object_appeared" in {e.event_type for e in result.events}
    assert max(s.counts.get("person", 0) for s in result.snapshots) >= 2
    assert result.stats["processing_fps"] > 10  # comfortably faster than the 10 fps sampling rate
