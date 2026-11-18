"""Engine tests. Media is generated on the fly with ffmpeg's lavfi sources."""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import converter as c  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def video(tmp_path_factory) -> Path:
    """6 s 320x240 video with TWO audio tracks (sine 440 Hz + sine 880 Hz)."""
    d = tmp_path_factory.mktemp("media")
    out = d / "клип с пробелом.mp4"  # Cyrillic + space on purpose
    _ff("-f", "lavfi", "-i", "testsrc=d=6:s=320x240:r=25",
        "-f", "lavfi", "-i", "sine=f=440:d=6",
        "-f", "lavfi", "-i", "sine=f=880:d=6",
        "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-metadata", "title=SECRET", str(out))
    return out


def _streams(path: Path) -> list[dict]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
    return [line.split(",") for line in out.strip().splitlines()]


def _title(path: Path) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags=title",
         "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True).stdout.strip()


# --------------------------------------------------------------------- video

@needs_ffmpeg
def test_gif_respects_trim(video, tmp_path):
    s = c.ConversionSettings(trim_start_s=1, trim_end_s=3)
    dst = tmp_path / "out.gif"
    c.convert(video, dst, settings=s, duration_s=6)
    assert c.probe_duration(dst) == pytest.approx(2.0, abs=0.25)


@needs_ffmpeg
def test_progress_is_relative_to_trimmed_fragment(video, tmp_path):
    seen: list[float] = []
    s = c.ConversionSettings(trim_start_s=1, trim_end_s=3)
    c.convert(video, tmp_path / "o.mp4", settings=s, duration_s=6, on_progress=seen.append)
    assert seen and max(seen) > 0.9  # previously stuck around 2/6 ≈ 0.33


@needs_ffmpeg
def test_audio_track_selection_for_audio_extraction(video, tmp_path):
    tracks = [st for st in c.probe_streams(video) if st.kind == "audio"]
    assert len(tracks) == 2
    s = c.ConversionSettings(audio_track_index=tracks[1].index)
    dst = tmp_path / "a.wav"
    c.convert(video, dst, settings=s)
    # 880 Hz track → ~1760 zero crossings per second, 440 Hz → ~880.
    assert _zero_crossings_per_s(dst) == pytest.approx(1760, rel=0.05)


def _zero_crossings_per_s(path: Path) -> float:
    import array
    import wave
    with wave.open(str(path)) as w:
        rate, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        samples = array.array("h", w.readframes(n))[::ch]
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    return crossings / (len(samples) / rate)


@needs_ffmpeg
def test_missing_audio_track_falls_back(video, tmp_path):
    s = c.ConversionSettings(audio_track_index=42)
    logs: list[str] = []
    c.convert(video, tmp_path / "o.mkv", settings=s, on_log=logs.append)
    assert any("нет аудиодорожки #42" in line for line in logs)


@needs_ffmpeg
def test_metadata_strip(video, tmp_path):
    keep, strip = tmp_path / "keep.mkv", tmp_path / "strip.mkv"
    c.convert(video, keep, settings=c.ConversionSettings(copy_metadata=True))
    c.convert(video, strip, settings=c.ConversionSettings(copy_metadata=False))
    assert _title(keep) == "SECRET"
    assert _title(strip) == ""


@needs_ffmpeg
def test_stories_preset_is_vertical(video, tmp_path):
    s = c.default_settings()
    c.get_preset("stories").apply(s)
    s.video_bitrate_kbps = 300
    dst = tmp_path / "story.mp4"
    c.convert(video, dst, settings=s)
    w, h = next((int(x[2]), int(x[3])) for x in _streams(dst) if x[1] == "video")
    assert h == 1920 and w == 1080


@pytest.mark.parametrize("text", ["it's 12:30 — 100% «ок»", "a\\b;c,d[e]"])
@needs_ffmpeg
def test_watermark_special_characters(video, tmp_path, text):
    s = c.ConversionSettings(watermark_text=text, trim_end_s=1)
    c.convert(video, tmp_path / "wm.mp4", settings=s)


@needs_ffmpeg
def test_incompatible_container_codec_is_explained(video, tmp_path):
    s = c.ConversionSettings(video_codec="h264")
    with pytest.raises(c.ConversionError, match="webm"):
        c.convert(video, tmp_path / "o.webm", settings=s)


@needs_ffmpeg
def test_wma_is_real_wma(video, tmp_path):
    dst = tmp_path / "a.wma"
    c.convert(video, dst)
    assert ["wmav2", "audio"] in [x[:2] for x in _streams(dst)]


@needs_ffmpeg
def test_cancel_kills_ffmpeg_and_removes_partial_file(video, tmp_path):
    cancel = threading.Event()
    procs: list[subprocess.Popen] = []

    def on_log(_line: str) -> None:
        cancel.set()

    dst = tmp_path / "cancel.mp4"
    with pytest.raises(c.ConversionCancelled):
        c.convert(video, dst, on_log=on_log, on_proc=procs.append, is_cancelled=cancel.is_set)
    assert procs and procs[0].poll() is not None
    assert not dst.exists()


@needs_ffmpeg
def test_cyrillic_filename_log_is_decoded(video, tmp_path):
    logs: list[str] = []
    c.convert(video, tmp_path / "выход.mp3", on_log=logs.append)
    assert any("клип с пробелом" in line for line in logs)


def test_effective_duration():
    s = c.ConversionSettings(trim_start_s=10, trim_end_s=25)
    assert c.effective_duration(60, s) == 15
    assert c.effective_duration(20, s) == 10
    assert c.effective_duration(60, c.ConversionSettings()) == 60


def test_videotoolbox_quality_direction(monkeypatch):
    monkeypatch.setattr(c, "detect_available_encoders", lambda: {"h264_videotoolbox"})
    def q(crf):
        args = c._video_codec_args("h264", c.ConversionSettings(hwaccel="videotoolbox", video_crf=crf))
        return int(args[args.index("-q:v") + 1])
    assert q(18) > q(28)  # lower CRF (better) must give higher -q:v (better)


# -------------------------------------------------------------------- images

def _rgba(path: Path, size=(200, 100)) -> Path:
    Image.new("RGBA", size, (255, 0, 0, 128)).save(path)
    return path


def test_image_png_to_jpg_flattens_alpha(tmp_path):
    dst = tmp_path / "o.jpg"
    c.convert(_rgba(tmp_path / "i.png"), dst)
    with Image.open(dst) as im:
        assert im.mode == "RGB"


def test_animated_gif_resized_and_keeps_frames(tmp_path):
    src = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (200, 100), col) for col in ("red", "green", "blue")]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=0)
    dst = tmp_path / "out.webp"
    c.convert(src, dst, settings=c.ConversionSettings(image_resize_width=50))
    with Image.open(dst) as im:
        assert im.n_frames == 3
        assert im.size == (50, 25)


def test_source_file_is_not_locked(tmp_path):
    src = _rgba(tmp_path / "i.png")
    c.convert(src, tmp_path / "o.webp")
    src.unlink()  # would fail on Windows if the handle leaked


def test_image_watermark(tmp_path):
    dst = tmp_path / "wm.png"
    c.convert(_rgba(tmp_path / "i.png", (400, 300)), dst,
              settings=c.ConversionSettings(watermark_text="Привет"))
    assert dst.exists()


def test_images_to_pdf(tmp_path):
    srcs = [_rgba(tmp_path / f"{i}.png") for i in range(3)]
    dst = tmp_path / "all.pdf"
    c.images_to_pdf(srcs, dst, c.ConversionSettings())
    assert dst.read_bytes().count(b"/Type /Page\n") + dst.read_bytes().count(b"/Type /Page ") >= 1


def test_estimate_accepts_dotted_ext(tmp_path):
    src = _rgba(tmp_path / "i.png")
    assert c.estimate_output_size_bytes(src, ".jpg", c.ConversionSettings()) is not None
