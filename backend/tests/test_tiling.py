from app.vision.detectors import TilingConfig, merge_detections
from app.vision.types import Detection


def test_windows_cover_frame_with_overlap() -> None:
    wins = TilingConfig(tile_size=640, overlap=0.25).windows(2560, 1440)
    # every pixel is covered and the last tiles are flush with the edges
    assert min(w[0] for w in wins) == 0 and max(w[2] for w in wins) == 2560
    assert min(w[1] for w in wins) == 0 and max(w[3] for w in wins) == 1440
    assert all(w[2] - w[0] == 640 and w[3] - w[1] == 640 for w in wins)
    xs = sorted({w[0] for w in wins})
    assert all(b - a <= 480 for a, b in zip(xs, xs[1:]))  # step = 640 * (1 - 0.25)


def test_auto_tile_size_matches_benchmarked_sizes() -> None:
    auto = TilingConfig()
    assert {w[2] - w[0] for w in auto.windows(1280, 720)} == {320}
    assert {w[2] - w[0] for w in auto.windows(2560, 1440)} == {640}


def test_small_frame_is_one_tile() -> None:
    assert TilingConfig(tile_size=640).windows(400, 300) == [(0, 0, 400, 300)]


def _d(cls: int, conf: float, box: tuple[float, float, float, float]) -> Detection:
    return Detection(class_id=cls, class_name=str(cls), confidence=conf, bbox=box)


def test_merge_fuses_overlapping_same_class_only() -> None:
    dets = [
        _d(0, 0.9, (100, 100, 120, 140)),
        _d(0, 0.4, (102, 101, 121, 150)),  # same person cut by a tile edge
        _d(2, 0.5, (100, 100, 120, 140)),  # different class, kept
        _d(0, 0.6, (300, 300, 320, 340)),  # separate person
    ]
    out = merge_detections(dets, 0.5)
    people = sorted((d for d in out if d.class_id == 0), key=lambda d: d.bbox[0])
    assert len(people) == 2 and len([d for d in out if d.class_id == 2]) == 1
    assert people[0].confidence == 0.9 and people[0].bbox == (100, 100, 121, 150)


def test_long_videos_are_sampled_sparsely() -> None:
    from app.core.config import get_settings
    from app.services.vision import sample_fps_for

    s = get_settings()
    assert sample_fps_for(s, 90) == s.vision_sample_fps  # short clip: full rate
    assert sample_fps_for(s, None) == s.vision_sample_fps
    long = sample_fps_for(s, 56 * 60)  # documentary: capped at VIDEO_IMPORT_MAX_SECONDS and VISION_MAX_FRAMES
    assert 1.0 <= long < s.vision_sample_fps
    assert long * min(56 * 60, s.video_import_max_seconds) <= s.vision_max_frames + 1
