"""Tests for the second wave of features: target size, subtitles, concat, …"""
from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import converter as c  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def _probe(path: Path, entries: str) -> str:
    return subprocess.run(["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0",
                           str(path)], capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("m2")
    srt = d / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:05,000\nПривет\n\n2\n00:00:05,000 --> 00:00:09,000\nМир\n",
                   encoding="utf-8")
    v1 = d / "a.mkv"
    _ff("-f", "lavfi", "-i", "testsrc=d=10:s=640x360:r=25", "-f", "lavfi", "-i", "sine=d=10",
        "-i", str(srt), "-map", "0", "-map", "1", "-map", "2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "srt", str(v1))
    v2 = d / "b.mp4"  # different size + no audio
    _ff("-f", "lavfi", "-i", "testsrc2=d=3:s=320x320:r=30", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(v2))
    a1 = d / "a.mp3"
    _ff("-f", "lavfi", "-i", "sine=f=300:d=4", str(a1))
    a2 = d / "b.wav"
    _ff("-f", "lavfi", "-i", "sine=f=500:d=2", "-ar", "22050", "-ac", "1", str(a2))
    return {"dir": d, "srt": srt, "v1": v1, "v2": v2, "a1": a1, "a2": a2}


@needs_ffmpeg
def test_target_size_is_respected(media, tmp_path):
    dst = tmp_path / "small.mp4"
    c.convert(media["v1"], dst, settings=c.ConversionSettings(target_size_mb=0.5), duration_s=10)
    size_mb = dst.stat().st_size / 1024 / 1024
    assert 0.25 < size_mb <= 0.55


@needs_ffmpeg
def test_target_size_for_audio(media, tmp_path):
    s = c.apply_target_size(c.ConversionSettings(target_size_mb=1), 60, "mp3")
    assert s.audio_bitrate_kbps == pytest.approx(134, abs=2)


@needs_ffmpeg
def test_extract_subtitles(media, tmp_path):
    dst = tmp_path / "out.srt"
    c.convert(media["v1"], dst)
    assert "Привет" in dst.read_text(encoding="utf-8")


@needs_ffmpeg
def test_extract_subtitles_without_any_fails_nicely(media, tmp_path):
    with pytest.raises(c.ConversionError, match="нет субтитров"):
        c.convert(media["v2"], tmp_path / "x.srt")


@needs_ffmpeg
@pytest.mark.parametrize("mode", ["burn_file", "burn_track"])
def test_burn_subtitles(media, tmp_path, mode):
    s = c.ConversionSettings(subtitle_mode=mode, subtitle_file=str(media["srt"]), trim_start_s=2,
                             trim_end_s=4)
    dst = tmp_path / f"{mode}.mp4"
    c.convert(media["v1"], dst, settings=s)
    assert c.probe_duration(dst) == pytest.approx(2, abs=0.3)


@needs_ffmpeg
@pytest.mark.parametrize("target,codec", [("mp4", "mov_text"), ("mkv", "subrip")])
def test_soft_subtitles(media, tmp_path, target, codec):
    s = c.ConversionSettings(subtitle_mode="soft_file", subtitle_file=str(media["srt"]))
    dst = tmp_path / f"soft.{target}"
    c.convert(media["v2"], dst, settings=s)
    assert codec in _probe(dst, "stream=codec_name")


@needs_ffmpeg
def test_soft_subtitles_unsupported_container(media, tmp_path):
    s = c.ConversionSettings(subtitle_mode="soft_file", subtitle_file=str(media["srt"]))
    with pytest.raises(c.ConversionError, match="субтитры"):
        c.convert(media["v2"], tmp_path / "x.avi", settings=s)


@needs_ffmpeg
def test_rotate_and_mute(media, tmp_path):
    dst = tmp_path / "rot.mp4"
    c.convert(media["v1"], dst, settings=c.ConversionSettings(video_rotate=90, video_mute=True))
    streams = _probe(dst, "stream=codec_type,width,height")
    assert "audio" not in streams
    assert "360,640" in streams


@needs_ffmpeg
def test_loudnorm(media, tmp_path):
    dst = tmp_path / "loud.flac"
    c.convert(media["a1"], dst, settings=c.ConversionSettings(audio_normalize=True))
    assert dst.exists()


@needs_ffmpeg
def test_concat_videos_with_different_sizes_and_missing_audio(media, tmp_path):
    dst = tmp_path / "joined.mp4"
    seen = []
    c.concat_files([media["v1"], media["v2"]], dst, on_progress=seen.append)
    assert c.probe_duration(dst) == pytest.approx(13, abs=0.5)
    assert "640,360" in _probe(dst, "stream=width,height")
    assert seen and max(seen) > 0.9


@needs_ffmpeg
def test_concat_audio(media, tmp_path):
    dst = tmp_path / "joined.mp3"
    c.concat_files([media["a1"], media["a2"]], dst)
    assert c.probe_duration(dst) == pytest.approx(6, abs=0.3)


@needs_ffmpeg
def test_render_sample(media, tmp_path):
    sample, projected = c.render_sample(media["v1"], "mp4", c.ConversionSettings(), seconds=2,
                                        out_dir=tmp_path)
    assert c.probe_duration(sample) == pytest.approx(2, abs=0.3)
    assert projected and projected > sample.stat().st_size * 3


def test_render_name():
    now = datetime.datetime(2026, 11, 18, 14, 5)
    s = c.ConversionSettings(video_height=720)
    src = Path("/x/Отпуск.mov")
    assert c.render_name("{name}", src, "mp4") == "Отпуск"
    assert c.render_name("{name}_{height}_{date}", src, "mp4", now=now, settings=s) == "Отпуск_720p_2026-11-18"
    assert c.render_name("{n} - {name}.{ext}", src, "mp4", index=7) == "007 - Отпуск.mov"
    assert c.render_name("a/b:c", src, "mp4") == "a_b_c"
    assert c.render_name("{height}", src, "mp4") == "Отпуск"  # empty → fallback


def test_humanize_error():
    assert "место" in c.humanize_error("Error: No space left on device")
    assert "Аппаратное" in c.humanize_error("[h264_nvenc] Cannot load nvcuda.dll")
    assert c.humanize_error("weird failure\nmore") == "weird failure"


def test_image_rotate_and_flip(tmp_path):
    src = tmp_path / "i.png"
    img = Image.new("RGB", (200, 100), "white")
    img.putpixel((0, 0), (255, 0, 0))
    img.save(src)
    dst = tmp_path / "o.png"
    c.convert(src, dst, settings=c.ConversionSettings(video_rotate=90, flip_horizontal=True))
    with Image.open(dst) as out:
        assert out.size == (100, 200)
        # rotate 90° cw moves (0,0) to top-right; mirroring brings it back to top-left
        assert out.getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.skipif(not c.HEIF_INPUTS, reason="pillow-heif not installed")
def test_heic_input(tmp_path):
    import pillow_heif
    src = tmp_path / "photo.heic"
    pillow_heif.from_pillow(Image.new("RGB", (64, 48), "blue")).save(src)
    assert c.media_kind(".heic") == "image" and c.target_kind(".heic") is None
    dst = tmp_path / "photo.jpg"
    c.convert(src, dst)
    with Image.open(dst) as out:
        assert out.size == (64, 48)
