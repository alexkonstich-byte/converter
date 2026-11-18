"""Queue logic used by the UI (no Flet needed)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import queue_logic as q  # noqa: E402
from converter import ConversionSettings  # noqa: E402


def test_allowed_targets():
    assert "gif" in q.allowed_targets(["video"])
    assert "srt" in q.allowed_targets(["video"])
    assert q.allowed_targets(["audio", "video"]) == set(q.AUDIO_FORMATS)
    assert q.allowed_targets(["image", "audio"]) == set()
    assert "mp4" not in q.allowed_targets(["image"])


def test_allowed_groups():
    assert q.allowed_groups([]) == {"image", "audio", "video"}
    assert q.allowed_groups(["video"]) == {"audio", "video"}
    assert q.allowed_groups(["audio", "video"]) == {"audio"}
    assert q.allowed_groups(["image", "video"]) == set()


def test_can_concat():
    assert q.can_concat(["video", "video"], "mp4")
    assert q.can_concat(["audio", "video"], "mp3")
    assert not q.can_concat(["audio", "video"], "mp4")
    assert not q.can_concat(["video"], "mp4")
    assert not q.can_concat(["image", "image"], "png")


def test_auto_jobs():
    assert q.auto_jobs(["video", "video"], "mp4") == 1
    assert q.auto_jobs(["image"] * 5, "webp") >= 1


def test_plural():
    assert [q.files_word(n) for n in (1, 2, 5, 11, 21, 22, 112)] == [
        "файл", "файла", "файлов", "файлов", "файл", "файла", "файлов"]


def test_output_path_template_structure_and_collisions(tmp_path):
    root = tmp_path / "in"
    (root / "sub").mkdir(parents=True)
    src = root / "sub" / "clip.mov"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    s = ConversionSettings(name_template="{name}_{format}", keep_structure=True)
    taken: set[Path] = set()
    first = q.output_path(src, "mp4", out, root, s, 1, taken)
    assert first == out / "sub" / "clip_mp4.mp4"
    taken.add(first)
    # Same name claimed earlier in the batch → numbered, never overwritten.
    assert q.output_path(src, "mp4", out, root, s, 2, taken) == out / "sub" / "clip_mp4_1.mp4"


def test_output_path_policies(tmp_path):
    src = tmp_path / "a.png"
    src.write_bytes(b"x")
    (tmp_path / "a.jpg").write_bytes(b"x")
    s = ConversionSettings(overwrite_mode="skip")
    assert q.output_path(src, "jpg", None, None, s, 1, set()) is None
    s.overwrite_mode = "overwrite"
    assert q.output_path(src, "jpg", None, None, s, 1, set()) == tmp_path / "a.jpg"
    s.overwrite_mode = "rename"
    assert q.output_path(src, "jpg", None, None, s, 1, set()) == tmp_path / "a_1.jpg"
    # Converting to the same format next to the source never clobbers it.
    assert q.output_path(src, "png", None, None, s, 1, set()) == tmp_path / "a_converted.png"


def test_scan_folder(tmp_path):
    (tmp_path / "b").mkdir()
    for name in ("b/2.mp3", "1.png", "notes.txt", ".hidden.png", "b/10.mp4"):
        (tmp_path / name).write_bytes(b"x")
    found = [p.relative_to(tmp_path).as_posix() for p in q.scan_folder(tmp_path)]
    assert found == ["1.png", "b/10.mp4", "b/2.mp3"]
    assert [p.name for p in q.scan_folder(tmp_path, recursive=False)] == ["1.png"]


def test_settings_roundtrip():
    s = ConversionSettings(image_quality=70, video_crf=26, trim_start_s=5, name_template="{n}")
    data = q.settings_to_dict(s)
    assert "trim_start_s" not in data
    restored = q.settings_from_dict(data)
    assert restored.image_quality == 70 and restored.video_crf == 26
    assert restored.trim_start_s is None and restored.name_template == "{n}"
    recipe = q.settings_to_dict(s, behaviour=False)
    assert "name_template" not in recipe and "overwrite_mode" not in recipe


def test_settings_from_dict_ignores_garbage():
    s = q.settings_from_dict({"image_quality": "high", "unknown": 1, "video_height": 720})
    assert s.image_quality == 90 and s.video_height == 720
