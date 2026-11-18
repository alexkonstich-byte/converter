"""Conversion engine: images via Pillow, audio/video via ffmpeg."""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont, ImageSequence
# PDF writing internally encodes raster pages via the JPEG plugin; importing
# it explicitly guarantees it's registered before PdfImagePlugin runs.
from PIL import JpegImagePlugin  # noqa: F401
from PIL import PngImagePlugin   # noqa: F401

# AVIF: Pillow ≥ 11.2 can do it natively; older versions need pillow-avif-plugin.
try:
    import pillow_avif  # noqa: F401
    AVIF_AVAILABLE = True
except ImportError:
    try:
        from PIL import features as _pil_features
        AVIF_AVAILABLE = bool(_pil_features.check("avif"))
    except Exception:
        AVIF_AVAILABLE = False

IMAGE_FORMATS = [
    "png", "jpg", "jpeg", "webp", "bmp", "tiff", "tif",
    "gif", "ico", "tga", "ppm", "pcx", "pdf",
]
if AVIF_AVAILABLE:
    IMAGE_FORMATS.append("avif")
AUDIO_FORMATS = [
    "mp3", "wav", "flac", "ogg", "m4a", "aac", "wma", "opus", "aiff",
]
VIDEO_FORMATS = [
    "mp4", "mkv", "avi", "mov", "webm", "flv", "wmv", "m4v", "mpeg", "mpg", "ts",
]

# Subtitles can be extracted from a video (target only).
SUBTITLE_FORMATS = ["srt", "ass", "vtt"]

ALL_FORMATS = sorted(set(IMAGE_FORMATS + AUDIO_FORMATS + VIDEO_FORMATS))

# Read-only image inputs: iPhone photos (HEIC) and camera RAW files.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_INPUTS = ["heic", "heif"]
except ImportError:
    HEIF_INPUTS = []
try:
    import rawpy
    RAW_INPUTS = ["dng", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf", "pef", "srw"]
except ImportError:
    rawpy = None
    RAW_INPUTS = []
IMAGE_INPUT_FORMATS = IMAGE_FORMATS + HEIF_INPUTS + RAW_INPUTS
AUDIO_INPUT_FORMATS = AUDIO_FORMATS + ["m4b", "amr", "ac3", "mka", "wv"]
VIDEO_INPUT_FORMATS = VIDEO_FORMATS + ["3gp", "mts", "m2ts", "vob", "ogv", "divx", "f4v", "asf"]


def media_kind(ext: str) -> Optional[str]:
    """Kind of a file we can READ (inputs are a superset of outputs)."""
    ext = ext.lower().lstrip(".")
    if ext in IMAGE_INPUT_FORMATS:
        return "image"
    if ext in AUDIO_INPUT_FORMATS:
        return "audio"
    if ext in VIDEO_INPUT_FORMATS:
        return "video"
    return None


def target_kind(ext: str) -> Optional[str]:
    """Kind of a format we can WRITE."""
    ext = ext.lower().lstrip(".")
    if ext in IMAGE_FORMATS:
        return "image"
    if ext in AUDIO_FORMATS:
        return "audio"
    if ext in VIDEO_FORMATS:
        return "video"
    if ext in SUBTITLE_FORMATS:
        return "subtitle"
    return None


def formats_for_kind(kind: str) -> list[str]:
    return {"image": IMAGE_FORMATS, "audio": AUDIO_FORMATS, "video": VIDEO_FORMATS}[kind]


def _bundled_dirs() -> list[Path]:
    """Folders next to the app where a bundled ffmpeg may live (release builds)."""
    dirs: list[Path] = []
    bases = [Path(sys.executable).resolve().parent, Path(sys.argv[0] or ".").resolve().parent,
             Path(__file__).resolve().parent]
    try:  # the real .exe of a `flet build` app (sys.executable may point elsewhere)
        import psutil
        bases.insert(0, Path(psutil.Process().exe()).resolve().parent)
    except Exception:
        pass
    for base in bases:
        for d in (base, base / "ffmpeg", base / "bin", base.parent, base.parent / "ffmpeg"):
            if d not in dirs:
                dirs.append(d)
    return dirs


_TOOL_CACHE: dict[str, str] = {}


def find_tool(name: str) -> Optional[str]:
    """Full path to ffmpeg/ffprobe: $CONVERTER_FFMPEG_DIR, next to the app, then PATH."""
    if name in _TOOL_CACHE:
        return _TOOL_CACHE[name]
    exe = name + (".exe" if os.name == "nt" else "")
    candidates = []
    env_dir = os.environ.get("CONVERTER_FFMPEG_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / exe)
    candidates += [d / exe for d in _bundled_dirs()]
    found = next((str(c) for c in candidates if c.is_file()), None) or shutil.which(name)
    if found:  # don't cache misses: the user may install ffmpeg while the app runs
        _TOOL_CACHE[name] = found
    return found


def _ffprobe() -> str:
    return find_tool("ffprobe") or "ffprobe"


def ffmpeg_available() -> bool:
    return find_tool("ffmpeg") is not None


def ffprobe_available() -> bool:
    return find_tool("ffprobe") is not None


# Encoder name → ffmpeg encoder. "copy" stream-copies without re-encoding.
AUDIO_CODECS = {
    "aac": "aac",
    "mp3": "libmp3lame",
    "opus": "libopus",
    "vorbis": "libvorbis",
    "flac": "flac",
    "ac3": "ac3",
    "wma": "wmav2",
    "copy": "copy",
}

# Default audio codec per video container — picked when the user leaves it on auto.
_DEFAULT_AUDIO_FOR_CONTAINER = {
    "mp4": "aac", "m4v": "aac", "mov": "aac",
    "mkv": "aac",
    "webm": "opus",
    "avi": "mp3",
    "flv": "aac",
    "wmv": "aac",
    "mpeg": "mp3", "mpg": "mp3", "ts": "aac",
}

# Video codec key → ffmpeg encoder.
VIDEO_ENCODERS = {
    "h264": "libx264",
    "h265": "libx265",
    "av1": "libsvtav1",  # SVT-AV1 is faster than libaom and ships with most builds.
}

DEFAULT_VIDEO_CODEC_FOR_CONTAINER = {
    "mp4": "h264", "mov": "h264", "m4v": "h264",
    "mkv": "h264",
    "webm": "av1",
    "avi": "h264",
    "flv": "h264",
    "wmv": "h264",
    "mpeg": "h264", "mpg": "h264", "ts": "h264",
}


@dataclass
class ConversionSettings:
    """User-controllable encoding parameters. ``None`` means "leave as source"."""

    # Image
    image_quality: int = 90                  # 1..100 (jpg/webp/avif)
    image_strip_exif: bool = False
    image_resize_width: Optional[int] = None
    image_resize_height: Optional[int] = None
    image_keep_aspect: bool = True

    # Audio (used for audio-only conversions and for the audio track inside video)
    audio_bitrate_kbps: Optional[int] = None  # None = codec default
    audio_sample_rate: Optional[int] = None   # None / 44100 / 48000
    audio_channels: Optional[int] = None      # None / 1 / 2 / 6
    audio_codec_for_video: Optional[str] = None  # one of AUDIO_CODECS keys, or None

    # Video
    video_codec: Optional[str] = None         # None / "h264" / "h265" / "av1"
    video_bitrate_mode: str = "crf"           # "crf" / "cbr"
    video_crf: int = 20                       # quality (lower = better)
    video_bitrate_kbps: int = 8000            # used only for "cbr"
    video_fps: Optional[int] = None           # None = keep / 24 / 30 / 60
    video_height: Optional[int] = None        # None = keep / 480 / 720 / 1080 / 2160
    video_aspect: Optional[str] = None        # None = keep / "9:16" / "1:1" / "16:9" (center crop)

    video_rotate: int = 0                     # 0 / 90 / 180 / 270 (clockwise) — images too
    flip_horizontal: bool = False             # mirror — images too
    flip_vertical: bool = False
    video_mute: bool = False                  # drop the audio track
    # Size target: overrides bitrate so the result fits (MB). Video and audio.
    target_size_mb: Optional[float] = None

    # Audio processing
    audio_normalize: bool = False             # EBU R128 loudness normalisation

    # Subtitles (video → video)
    subtitle_mode: Optional[str] = None       # None / "burn_file" / "soft_file" / "burn_track"
    subtitle_file: Optional[str] = None       # .srt/.ass/.vtt for *_file modes
    subtitle_track: Optional[int] = None      # stream index for burn_track / extraction

    # Hardware acceleration
    hwaccel: Optional[str] = None             # None=CPU / "nvenc" / "qsv" / "amf" / "videotoolbox"

    # Trim (video / audio)
    trim_start_s: Optional[float] = None
    trim_end_s: Optional[float] = None

    # Track selection (None = default first stream)
    audio_track_index: Optional[int] = None

    # Watermark (text only for now)
    watermark_text: Optional[str] = None
    watermark_position: str = "bottom-right"  # top-left / top-right / bottom-left / bottom-right / center
    watermark_opacity: float = 0.7            # 0..1
    watermark_color: str = "white"            # css color name / #RRGGBB

    # Behaviour
    copy_metadata: bool = True
    overwrite_mode: str = "rename"            # "rename" / "overwrite" / "skip"
    low_priority: bool = False                # run ffmpeg with reduced CPU priority
    compute_checksum: Optional[str] = None    # None / "md5" / "sha256"
    name_template: str = "{name}"             # see render_name()
    keep_structure: bool = False              # mirror sub-folders of an added folder


def default_settings() -> ConversionSettings:
    return ConversionSettings()


@dataclass
class ConversionResult:
    src: Path
    dst: Path
    ok: bool
    error: str = ""


class ConversionError(Exception):
    pass


class ConversionCancelled(ConversionError):
    """Raised when the caller cancelled the job (ffmpeg was killed)."""


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe plumbing
# ---------------------------------------------------------------------------

def _creationflags() -> int:
    return 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW on Windows


def _run_ffmpeg(
    args: list[str],
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    duration_s: Optional[float] = None,
    on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
    low_priority: bool = False,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    """Run ffmpeg, streaming logs and parsing progress timestamps.

    ``on_proc`` is invoked with the live ``subprocess.Popen`` so the caller
    can suspend/resume or kill it. ``low_priority`` lowers CPU scheduling
    priority so the system stays responsive during long encodes.
    ``is_cancelled`` is polled on every output line; when it returns True
    the process is killed and :class:`ConversionCancelled` is raised.
    """
    if not ffmpeg_available():
        raise ConversionError(
            "ffmpeg не найден в PATH. Установите ffmpeg, чтобы конвертировать аудио и видео."
        )

    # Never let ffmpeg wait for keyboard input, keep the log readable.
    if args and args[0] == "ffmpeg":
        args = [find_tool("ffmpeg") or "ffmpeg", "-hide_banner", "-nostdin", *args[1:]]

    flags = _creationflags()
    if low_priority and os.name == "nt":
        # BELOW_NORMAL_PRIORITY_CLASS — the OS schedules ffmpeg less
        # aggressively, so the rest of the desktop stays smooth.
        flags |= 0x00004000
    preexec = None
    if os.name != "nt":
        def _child_setup(nice=low_priority):
            if nice:
                # POSIX: run the child with nice(10) so it has lower priority.
                try:
                    os.nice(10)
                except Exception:
                    pass
            if sys.platform.startswith("linux"):
                # Kill ffmpeg if our process dies abruptly (no orphaned encodes).
                try:
                    import ctypes
                    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, 9)  # PR_SET_PDEATHSIG, SIGKILL
                except Exception:
                    pass
        preexec = _child_setup

    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # ffmpeg prints UTF-8 (file names!). Without an explicit encoding a
        # Russian Windows decodes it as cp1251 and the log turns to garbage.
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=flags,
        preexec_fn=preexec,
    )
    if on_proc:
        try:
            on_proc(proc)
        except Exception:
            pass

    tail: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if is_cancelled and is_cancelled():
                proc.kill()
                break
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 200:
                tail = tail[-200:]
            if on_log:
                try:
                    on_log(line)
                except Exception:
                    pass
            if on_progress and duration_s and "time=" in line:
                idx = line.find("time=")
                ts = line[idx + 5:idx + 16]
                secs = _parse_ts(ts)
                if secs is not None and duration_s > 0:
                    try:
                        on_progress(min(1.0, secs / duration_s))
                    except Exception:
                        pass
        proc.wait()
    finally:
        # Whatever happened (exception in a callback, cancel, …) — never
        # leave an orphaned ffmpeg running in the background.
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    if is_cancelled and is_cancelled():
        raise ConversionCancelled("Отменено пользователем.")
    if proc.returncode != 0:
        last = "\n".join(tail[-8:])
        raise ConversionError(f"ffmpeg вернул код {proc.returncode}:\n{last}")


def _parse_ts(ts: str) -> Optional[float]:
    try:
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return None


def probe_duration(path: Path) -> Optional[float]:
    """Return media duration in seconds, or None if ffprobe is unavailable."""
    if not ffprobe_available():
        return None
    try:
        proc = subprocess.run(
            [
                _ffprobe(), "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=_creationflags(),
        )
        out = (proc.stdout or "").strip()
        return float(out) if out else None
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None


def probe_video_bitrate_kbps(path: Path) -> Optional[int]:
    """Best-effort source video bitrate, used for output-size estimation."""
    if not ffprobe_available():
        return None
    try:
        proc = subprocess.run(
            [
                _ffprobe(), "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=bit_rate",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=_creationflags(),
        )
        out = (proc.stdout or "").strip()
        if not out or out == "N/A":
            return None
        return int(int(out) / 1000)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None


@dataclass
class StreamInfo:
    index: int
    kind: str                  # "audio" / "subtitle"
    codec: str
    language: Optional[str]
    title: Optional[str]
    channels: Optional[int]    # audio only

    @property
    def label(self) -> str:
        bits = []
        if self.language:
            bits.append(self.language.upper())
        if self.title:
            bits.append(self.title)
        if self.codec:
            bits.append(self.codec)
        if self.kind == "audio" and self.channels:
            bits.append(f"{self.channels}ch")
        return " · ".join(bits) if bits else f"#{self.index}"


def probe_streams(path: Path) -> list[StreamInfo]:
    """Enumerate audio + subtitle streams via ffprobe (JSON output)."""
    if not ffprobe_available():
        return []
    try:
        proc = subprocess.run(
            [
                _ffprobe(), "-v", "error",
                "-show_entries",
                "stream=index,codec_name,codec_type,channels:stream_tags=language,title",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=_creationflags(),
        )
        data = json.loads(proc.stdout or "{}")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return []

    streams: list[StreamInfo] = []
    for st in data.get("streams", []):
        kind = st.get("codec_type")
        if kind not in ("audio", "subtitle"):
            continue
        tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        lang = tags.get("language")
        streams.append(StreamInfo(
            index=int(st.get("index", -1)),
            kind=kind,
            codec=st.get("codec_name", "?"),
            language=None if lang in (None, "und") else lang,
            title=tags.get("title"),
            channels=st.get("channels"),
        ))
    return streams


# ---------------------------------------------------------------------------
# Hardware-accelerated encoder discovery
# ---------------------------------------------------------------------------

# Encoder lookup: (codec, hwaccel) → ffmpeg encoder name.
HW_ENCODERS = {
    ("h264", "nvenc"):        "h264_nvenc",
    ("h264", "qsv"):          "h264_qsv",
    ("h264", "amf"):          "h264_amf",
    ("h264", "videotoolbox"): "h264_videotoolbox",
    ("h265", "nvenc"):        "hevc_nvenc",
    ("h265", "qsv"):          "hevc_qsv",
    ("h265", "amf"):          "hevc_amf",
    ("h265", "videotoolbox"): "hevc_videotoolbox",
    ("av1",  "nvenc"):        "av1_nvenc",
    ("av1",  "qsv"):          "av1_qsv",
    ("av1",  "amf"):          "av1_amf",
}

_HWACCEL_CACHE: Optional[set[str]] = None


def detect_available_encoders() -> set[str]:
    """Run ``ffmpeg -encoders`` once and cache the names that are present."""
    global _HWACCEL_CACHE
    if _HWACCEL_CACHE is not None:
        return _HWACCEL_CACHE
    if not ffmpeg_available():
        _HWACCEL_CACHE = set()
        return _HWACCEL_CACHE
    try:
        proc = subprocess.run(
            [find_tool("ffmpeg") or "ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=_creationflags(),
        )
    except (subprocess.TimeoutExpired, OSError):
        _HWACCEL_CACHE = set()
        return _HWACCEL_CACHE

    names: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        # Lines look like " V..... libx264              H.264 / AVC ..."
        m = re.match(r"\s+[A-Z\.]{6}\s+(\S+)\s+", line)
        if m:
            names.add(m.group(1))
    _HWACCEL_CACHE = names
    return names


def detect_available_hwaccels(codec: str) -> list[str]:
    """Return the hwaccel keys (nvenc/qsv/amf/videotoolbox) usable for this codec."""
    encoders = detect_available_encoders()
    available: list[str] = []
    for (c, hw), enc_name in HW_ENCODERS.items():
        if c == codec and enc_name in encoders:
            available.append(hw)
    return available


# ---------------------------------------------------------------------------
# Checksum helpers
# ---------------------------------------------------------------------------

def file_checksum(path: Path, algo: str = "sha256") -> str:
    """Compute MD5 or SHA-256 of a file, returned as hex digest."""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Fonts (watermark) — need Cyrillic glyphs, so prefer well-known system fonts.
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = [
    # Windows
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
]


def find_font_file() -> Optional[str]:
    """First existing system font with Cyrillic support, or None."""
    for cand in _FONT_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return None


def _load_font(size: int) -> ImageFont.ImageFont:
    path = find_font_file()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1 has no size argument
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Image conversion
# ---------------------------------------------------------------------------

def _resize_image(img: Image.Image, settings: ConversionSettings) -> Image.Image:
    target_w = settings.image_resize_width
    target_h = settings.image_resize_height
    if not target_w and not target_h:
        return img

    src_w, src_h = img.size
    if settings.image_keep_aspect:
        if target_w and not target_h:
            ratio = target_w / src_w
            new_size = (target_w, max(1, int(src_h * ratio)))
        elif target_h and not target_w:
            ratio = target_h / src_h
            new_size = (max(1, int(src_w * ratio)), target_h)
        else:
            ratio = min(target_w / src_w, target_h / src_h)
            new_size = (max(1, int(src_w * ratio)), max(1, int(src_h * ratio)))
    else:
        new_size = (target_w or src_w, target_h or src_h)
    return img.resize(new_size, Image.LANCZOS)


def _stamp_watermark(img: Image.Image, settings: ConversionSettings) -> Image.Image:
    text = (settings.watermark_text or "").strip()
    if not text:
        return img
    img = img.convert("RGBA") if img.mode != "RGBA" else img.copy()
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    # Pick a font size proportional to the shorter image side.
    short = min(img.size)
    size = max(12, int(short * 0.045))
    font = _load_font(size)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = max(8, size // 4)
    pos = settings.watermark_position
    if pos == "top-left":
        xy = (pad, pad)
    elif pos == "top-right":
        xy = (img.size[0] - tw - pad, pad)
    elif pos == "bottom-left":
        xy = (pad, img.size[1] - th - pad)
    elif pos == "center":
        xy = ((img.size[0] - tw) // 2, (img.size[1] - th) // 2)
    else:  # bottom-right
        xy = (img.size[0] - tw - pad, img.size[1] - th - pad)

    alpha = max(0.0, min(1.0, settings.watermark_opacity))
    color = settings.watermark_color or "white"
    # Soft dark backplate for contrast.
    plate = (0, 0, 0, int(255 * alpha * 0.4))
    draw.rectangle(
        [xy[0] - pad // 2, xy[1] - pad // 2,
         xy[0] + tw + pad // 2, xy[1] + th + pad // 2],
        fill=plate,
    )
    draw.text(xy, text, font=font, fill=_color_with_alpha(color, alpha))
    return Image.alpha_composite(img, layer)


def _color_with_alpha(name: str, alpha: float) -> tuple[int, int, int, int]:
    a = int(255 * max(0.0, min(1.0, alpha)))
    presets = {
        "white": (255, 255, 255), "black": (0, 0, 0),
        "yellow": (255, 220, 60), "red": (240, 70, 70),
    }
    if name.startswith("#") and len(name) == 7:
        try:
            r = int(name[1:3], 16); g = int(name[3:5], 16); b = int(name[5:7], 16)
            return (r, g, b, a)
        except ValueError:
            pass
    rgb = presets.get(name.lower(), (255, 255, 255))
    return rgb + (a,)


def _flatten_alpha(img: Image.Image) -> Image.Image:
    """Composite transparent pixels onto white (for formats without alpha)."""
    if img.mode in ("RGBA", "LA", "P", "PA"):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _convert_animated(img: Image.Image, dst: Path, target: str,
                      settings: ConversionSettings) -> None:
    """GIF/WebP → GIF/WebP keeping every frame and its own delay."""
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(img):
        durations.append(int(frame.info.get("duration", img.info.get("duration", 100)) or 100))
        f = frame.convert("RGBA")
        f = _orient_image(f, settings)
        f = _resize_image(f, settings)
        f = _stamp_watermark(f, settings)
        frames.append(f)

    save_kwargs: dict = {
        "save_all": True,
        "append_images": frames[1:],
        "loop": img.info.get("loop", 0),
        "duration": durations,
    }
    if target == "webp":
        save_kwargs["quality"] = max(1, min(100, settings.image_quality))
        save_kwargs["method"] = 4
    else:
        save_kwargs["disposal"] = 2
        save_kwargs["optimize"] = False
    frames[0].save(dst, **save_kwargs)


def open_image(src: Path) -> Image.Image:
    """Image.open that also understands camera RAW files (via rawpy)."""
    if rawpy is not None and src.suffix.lower().lstrip(".") in RAW_INPUTS:
        with rawpy.imread(str(src)) as raw:
            rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
        return Image.fromarray(rgb)
    return Image.open(src)


def _orient_image(img: Image.Image, settings: ConversionSettings) -> Image.Image:
    """Rotate clockwise / mirror according to settings."""
    rot = settings.video_rotate % 360
    if rot == 90:
        img = img.transpose(Image.Transpose.ROTATE_270)
    elif rot == 180:
        img = img.transpose(Image.Transpose.ROTATE_180)
    elif rot == 270:
        img = img.transpose(Image.Transpose.ROTATE_90)
    if settings.flip_horizontal:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if settings.flip_vertical:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return img


def _convert_image(src: Path, dst: Path, settings: ConversionSettings) -> None:
    target = dst.suffix.lower().lstrip(".")
    # Context manager closes the source handle (Windows keeps files locked otherwise).
    with open_image(src) as img:
        exif = img.info.get("exif")
        if getattr(img, "is_animated", False) and target in ("gif", "webp"):
            _convert_animated(img, dst, target, settings)
            return

        img.load()
        img = _orient_image(img, settings)
        img = _resize_image(img, settings)
        img = _stamp_watermark(img, settings)

        # Formats that don't accept alpha — flatten onto white.
        if target in ("jpg", "jpeg", "bmp", "pcx", "pdf"):
            img = _flatten_alpha(img)
        elif img.mode == "P":
            img = img.convert("RGBA")

        save_kwargs: dict = {}
        quality = max(1, min(100, settings.image_quality))
        if target in ("jpg", "jpeg"):
            save_kwargs["quality"] = quality
            save_kwargs["optimize"] = True
            save_kwargs["progressive"] = True
        elif target == "webp":
            save_kwargs["quality"] = quality
            save_kwargs["method"] = 6  # smaller file at slight CPU cost
        elif target == "avif":
            save_kwargs["quality"] = quality
        elif target == "png":
            save_kwargs["optimize"] = True

        # EXIF: keep by default (only for formats that can carry it), strip if requested.
        if exif and not settings.image_strip_exif and target in (
            "jpg", "jpeg", "webp", "png", "tiff", "tif", "avif",
        ):
            save_kwargs["exif"] = exif

        img.save(dst, **save_kwargs)


def images_to_pdf(srcs: list[Path], dst: Path, settings: ConversionSettings) -> None:
    """Combine multiple images into one PDF document, preserving order."""
    if not srcs:
        raise ConversionError("Нет картинок для PDF.")
    pages: list[Image.Image] = []
    for src in srcs:
        with open_image(src) as img:
            img.load()
            page = _orient_image(img, settings)
            page = _stamp_watermark(_resize_image(page, settings), settings)
            pages.append(_flatten_alpha(page))
    dst.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(dst, save_all=True, append_images=pages[1:], format="PDF")


# ---------------------------------------------------------------------------
# Audio conversion
# ---------------------------------------------------------------------------

def _audio_codec_args(codec_key: str, settings: ConversionSettings) -> list[str]:
    encoder = AUDIO_CODECS[codec_key]
    if encoder == "copy":
        return ["-c:a", "copy"]
    args = ["-c:a", encoder]

    bitrate = settings.audio_bitrate_kbps
    if encoder == "aac":
        args += ["-b:a", f"{bitrate or 192}k"]
    elif encoder == "libmp3lame":
        if bitrate:
            args += ["-b:a", f"{bitrate}k"]
        else:
            args += ["-q:a", "2"]
    elif encoder == "libopus":
        args += ["-b:a", f"{bitrate or 128}k"]
    elif encoder == "libvorbis":
        if bitrate:
            args += ["-b:a", f"{bitrate}k"]
        else:
            args += ["-q:a", "5"]
    elif encoder in ("ac3", "wmav2"):
        args += ["-b:a", f"{bitrate or 192}k"]
    # FLAC / PCM are lossless — bitrate doesn't apply.

    args += _audio_format_args(settings)
    return args


def _audio_format_args(settings: ConversionSettings) -> list[str]:
    args: list[str] = []
    if settings.audio_sample_rate:
        args += ["-ar", str(settings.audio_sample_rate)]
    if settings.audio_channels:
        args += ["-ac", str(settings.audio_channels)]
    return args


LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"


def _audio_filter_args(settings: ConversionSettings, codec_args: list[str]) -> list[str]:
    """Loudness normalisation (impossible with stream copy)."""
    if settings.audio_normalize and "copy" not in codec_args:
        return ["-af", LOUDNORM_FILTER]
    return []


def _metadata_args(settings: ConversionSettings) -> list[str]:
    # ffmpeg copies global tags by default, so "don't copy" must be explicit.
    return ["-map_metadata", "0" if settings.copy_metadata else "-1"]


def _trim_input_args(settings: ConversionSettings) -> list[str]:
    """-ss / -to placed BEFORE -i: fast seek, timestamps refer to the source."""
    args: list[str] = []
    start = settings.trim_start_s or 0
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    if settings.trim_end_s and settings.trim_end_s > start:
        args += ["-to", f"{settings.trim_end_s:.3f}"]
    return args


def effective_duration(duration_s: Optional[float], settings: ConversionSettings) -> Optional[float]:
    """Length of the actually encoded fragment (used for progress/ETA)."""
    if not duration_s:
        return duration_s
    start = max(0.0, settings.trim_start_s or 0.0)
    end = duration_s
    if settings.trim_end_s and settings.trim_end_s > start:
        end = min(duration_s, settings.trim_end_s)
    return max(0.0, end - start) or duration_s


def _valid_audio_track(src: Path, settings: ConversionSettings,
                       on_log: Optional[Callable[[str], None]]) -> Optional[int]:
    """The chosen audio stream index, if this particular file really has it."""
    idx = settings.audio_track_index
    if idx is None:
        return None
    if any(s.index == idx and s.kind == "audio" for s in probe_streams(src)):
        return idx
    if on_log:
        on_log(f"[converter] В {src.name} нет аудиодорожки #{idx} — беру дорожку по умолчанию.")
    return None


# Target container → codec key (see AUDIO_CODECS). Raw PCM handled separately.
_AUDIO_TARGET_CODEC = {
    "mp3": "mp3", "ogg": "vorbis", "opus": "opus",
    "m4a": "aac", "aac": "aac", "flac": "flac", "wma": "wma",
}


def _convert_audio(
    src: Path,
    dst: Path,
    settings: ConversionSettings,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    duration_s: Optional[float] = None,
    on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    target = dst.suffix.lower().lstrip(".")
    args = ["ffmpeg", "-y", *_trim_input_args(settings), "-i", str(src)]

    track = _valid_audio_track(src, settings, on_log)
    if track is not None:
        args += ["-map", f"0:{track}"]
    else:
        args += ["-map", "0:a:0"]
    args += ["-vn", "-sn"]

    if target == "wav":
        codec = ["-c:a", "pcm_s16le", *_audio_format_args(settings)]
    elif target == "aiff":
        codec = ["-c:a", "pcm_s16be", *_audio_format_args(settings)]
    else:
        codec = _audio_codec_args(_AUDIO_TARGET_CODEC.get(target, "aac"), settings)
    args += codec + _audio_filter_args(settings, codec)

    args += _metadata_args(settings)
    args.append(str(dst))
    _run_ffmpeg(args, on_log=on_log, on_progress=on_progress, duration_s=duration_s,
                on_proc=on_proc, low_priority=settings.low_priority,
                is_cancelled=is_cancelled)


# ---------------------------------------------------------------------------
# Video conversion
# ---------------------------------------------------------------------------

# Which codecs each container can actually hold — checked before running
# ffmpeg so the user gets a clear message instead of a cryptic ffmpeg error.
_CONTAINER_VIDEO_CODECS = {
    "webm": {"av1"},
    "mpeg": {"h264"}, "mpg": {"h264"},
}
_CONTAINER_AUDIO_CODECS = {
    "webm": {"opus", "vorbis"},
    "mp4": {"aac", "mp3", "opus", "flac", "ac3", "copy"},
    "m4v": {"aac", "mp3", "ac3", "copy"},
    "mov": {"aac", "mp3", "flac", "ac3", "copy"},
    "avi": {"mp3", "aac", "ac3", "copy"},
    "flv": {"aac", "mp3", "copy"},
    "wmv": {"aac", "mp3", "ac3", "wma", "copy"},
    "mpeg": {"mp3", "ac3", "copy"}, "mpg": {"mp3", "ac3", "copy"},
    "ts": {"aac", "mp3", "ac3", "opus", "copy"},
}

AUDIO_CODEC_LABELS = {
    "aac": "AAC", "mp3": "MP3", "opus": "Opus", "vorbis": "Vorbis",
    "flac": "FLAC", "ac3": "AC-3", "wma": "WMA", "copy": "без перекодирования",
}
VIDEO_CODEC_LABELS = {"h264": "H.264", "h265": "H.265", "av1": "AV1"}


def check_video_compat(target: str, video_codec: str, audio_codec: str) -> Optional[str]:
    """Human-readable reason why this combination won't work, or None if OK."""
    v_allowed = _CONTAINER_VIDEO_CODECS.get(target)
    if v_allowed and video_codec not in v_allowed:
        names = ", ".join(VIDEO_CODEC_LABELS[c] for c in sorted(v_allowed))
        return f".{target} поддерживает только видеокодек {names}."
    a_allowed = _CONTAINER_AUDIO_CODECS.get(target)
    if a_allowed and audio_codec not in a_allowed:
        names = ", ".join(AUDIO_CODEC_LABELS[c] for c in sorted(a_allowed) if c != "copy")
        return f".{target} не принимает звук {AUDIO_CODEC_LABELS.get(audio_codec, audio_codec)}. Подойдёт: {names}."
    return None


def _resolve_video_encoder(codec_key: str, settings: ConversionSettings) -> str:
    """Pick the actual ffmpeg encoder name based on codec + hwaccel choice.

    Falls back to the software encoder if the requested HW one isn't built
    into the user's ffmpeg.
    """
    if settings.hwaccel and settings.hwaccel != "none":
        hw_name = HW_ENCODERS.get((codec_key, settings.hwaccel))
        if hw_name and hw_name in detect_available_encoders():
            return hw_name
    return VIDEO_ENCODERS[codec_key]


def _video_codec_args(codec_key: str, settings: ConversionSettings) -> list[str]:
    encoder = _resolve_video_encoder(codec_key, settings)
    args = ["-c:v", encoder]

    if settings.video_bitrate_mode == "cbr":
        kbps = max(100, settings.video_bitrate_kbps)
        args += ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
    else:
        crf = settings.video_crf
        if encoder in ("libx264", "libx265"):
            args += ["-preset", "medium", "-crf", str(max(0, min(51, crf)))]
        elif encoder == "libsvtav1":
            # SVT-AV1's scale is 0..63 and ~+6 "softer" than x264 at the same number.
            args += ["-preset", "8", "-crf", str(max(0, min(63, crf + 6)))]
        elif encoder.endswith("_nvenc"):
            # NVENC uses constant-quality (-cq) instead of CRF.
            args += ["-preset", "p5", "-rc", "vbr", "-cq", str(max(0, min(51, crf))), "-b:v", "0"]
        elif encoder.endswith("_qsv"):
            args += ["-preset", "medium", "-global_quality", str(max(1, min(51, crf)))]
        elif encoder.endswith("_amf"):
            q = str(max(0, min(51, crf)))
            args += ["-quality", "balanced", "-rc", "cqp", "-qp_i", q, "-qp_p", q]
        elif encoder.endswith("_videotoolbox"):
            # VideoToolbox: -q:v 1..100 where HIGHER means BETTER — the inverse
            # of CRF. CRF 18 → ~68, CRF 23 → ~59, CRF 28 → ~50.
            q = int(round(100 - crf * 1.8))
            args += ["-q:v", str(max(1, min(100, q)))]

    if encoder in ("libx264", "libx265") or encoder.endswith(("_nvenc", "_qsv", "_amf")):
        # 10-bit / 4:4:4 sources otherwise produce files most players can't open.
        args += ["-pix_fmt", "yuv420p"]
    if encoder == "libx265":
        args += ["-tag:v", "hvc1"]  # lets Apple devices play HEVC in MP4/MOV
    return args


def _escape_filter_path(path: str) -> str:
    """Escape a file path for use inside a single-quoted filtergraph option."""
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _watermark_filter(settings: ConversionSettings, textfile: Path) -> str:
    pos_map = {
        "top-left":     "x=20:y=20",
        "top-right":    "x=w-tw-20:y=20",
        "bottom-left":  "x=20:y=h-th-20",
        "bottom-right": "x=w-tw-20:y=h-th-20",
        "center":       "x=(w-tw)/2:y=(h-th)/2",
    }
    pos = pos_map.get(settings.watermark_position, pos_map["bottom-right"])
    alpha = max(0.0, min(1.0, settings.watermark_opacity))
    color = settings.watermark_color or "white"
    parts = [
        # textfile= sidesteps drawtext's multi-level escaping of quotes, colons, %.
        f"drawtext=textfile='{_escape_filter_path(str(textfile))}'",
        "expansion=none",
        f"fontcolor={color}@{alpha:.2f}",
        "fontsize=h*0.045",
        f"box=1:boxcolor=black@{alpha * 0.4:.2f}:boxborderw=8",
        pos,
    ]
    font = find_font_file()
    if font:
        # Without an explicit font, drawtext on Windows has no fontconfig and fails.
        parts.insert(1, f"fontfile='{_escape_filter_path(font)}'")
    return ":".join(parts)


def _aspect_crop_filter(aspect: Optional[str]) -> Optional[str]:
    """Center-crop to W:H (e.g. "9:16") without stretching."""
    if not aspect:
        return None
    try:
        w, h = (int(x) for x in aspect.split(":"))
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return (f"crop=w='trunc(min(iw,ih*{w}/{h})/2)*2'"
            f":h='trunc(min(ih,iw*{h}/{w})/2)*2'")


def _scale_filter(height: int, aspect: Optional[str]) -> str:
    """Scale to the given height; with a crop aspect the width is exact too."""
    if aspect:
        try:
            w, h = (int(x) for x in aspect.split(":"))
            width = int(round(height * w / h / 2)) * 2
            return f"scale={width}:{height}:flags=lanczos,setsar=1"
        except (ValueError, ZeroDivisionError):
            pass
    # Keep aspect ratio, ensure even dimensions for x264/x265.
    return f"scale=-2:{height}:flags=lanczos"


def _orient_filters(settings: ConversionSettings) -> list[str]:
    rot = settings.video_rotate % 360
    parts = {90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}.get(rot, [])
    if settings.flip_horizontal:
        parts.append("hflip")
    if settings.flip_vertical:
        parts.append("vflip")
    return parts


def _subtitle_burn_filter(src: Path, settings: ConversionSettings) -> Optional[str]:
    """subtitles= filter for burning text into the picture (needs libass)."""
    mode = settings.subtitle_mode
    if mode == "burn_file" and settings.subtitle_file:
        f = f"subtitles=filename='{_escape_filter_path(str(settings.subtitle_file))}'"
    elif mode == "burn_track":
        subs = [st for st in probe_streams(src) if st.kind == "subtitle"]
        rel = next((n for n, st in enumerate(subs) if st.index == settings.subtitle_track), 0)
        if not subs:
            raise ConversionError("В видео нет встроенных субтитров, вшивать нечего.")
        f = f"subtitles=filename='{_escape_filter_path(str(src))}':si={rel}"
    else:
        return None
    start = settings.trim_start_s or 0
    if start > 0:
        # Input -ss resets timestamps to 0, but the subtitle file still starts
        # at the original 0 — shift the clock for the filter and back.
        f = f"setpts=PTS+{start:.3f}/TB,{f},setpts=PTS-STARTPTS"
    return f


def _video_filter(settings: ConversionSettings, textfile: Optional[Path],
                  subtitle_filter: Optional[str] = None) -> Optional[str]:
    parts: list[str] = _orient_filters(settings)
    if subtitle_filter:
        parts.append(subtitle_filter)
    crop = _aspect_crop_filter(settings.video_aspect)
    if crop:
        parts.append(crop)
    if settings.video_height:
        parts.append(_scale_filter(settings.video_height, settings.video_aspect))
    if settings.video_fps:
        parts.append(f"fps={settings.video_fps}")
    if textfile is not None:
        parts.append(_watermark_filter(settings, textfile))
    return ",".join(parts) if parts else None


class _WatermarkFile:
    """Temp UTF-8 file with the watermark text (for drawtext=textfile=)."""

    def __init__(self, settings: ConversionSettings):
        self.text = (settings.watermark_text or "").strip()
        self.path: Optional[Path] = None

    def __enter__(self) -> Optional[Path]:
        if not self.text:
            return None
        fd, name = tempfile.mkstemp(prefix="converter_wm_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.text)
        self.path = Path(name)
        return self.path

    def __exit__(self, *exc) -> None:
        if self.path:
            try:
                self.path.unlink()
            except OSError:
                pass


def _convert_gif(src, dst, settings, on_log, on_progress, duration_s, on_proc, is_cancelled) -> None:
    """Two-pass palette GIF — honours trim, fps, height and watermark."""
    fps = settings.video_fps or 15
    height = settings.video_height
    scale = f"scale=-2:{min(height, 720)}" if height else "scale=480:-2"
    trim = _trim_input_args(settings)
    with _WatermarkFile(settings) as wm:
        base = [f"fps={fps}", *_orient_filters(settings)]
        crop = _aspect_crop_filter(settings.video_aspect)
        if crop:
            base.append(crop)
        base.append(f"{scale}:flags=lanczos")
        if wm is not None:
            base.append(_watermark_filter(settings, wm))
        chain = ",".join(base)
        palette = Path(tempfile.gettempdir()) / f"converter_palette_{os.getpid()}_{id(dst)}.png"
        try:
            _run_ffmpeg(
                ["ffmpeg", "-y", *trim, "-i", str(src), "-vf", f"{chain},palettegen=stats_mode=diff",
                 str(palette)],
                on_log=on_log, on_proc=on_proc, low_priority=settings.low_priority,
                is_cancelled=is_cancelled,
            )
            _run_ffmpeg(
                ["ffmpeg", "-y", *trim, "-i", str(src), "-i", str(palette),
                 "-lavfi", f"{chain} [x]; [x][1:v] paletteuse=dither=sierra2_4a",
                 "-loop", "0", str(dst)],
                on_log=on_log, on_progress=on_progress, duration_s=duration_s,
                on_proc=on_proc, low_priority=settings.low_priority,
                is_cancelled=is_cancelled,
            )
        finally:
            if palette.exists():
                palette.unlink()


def _convert_video(
    src: Path,
    dst: Path,
    settings: ConversionSettings,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    duration_s: Optional[float] = None,
    on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    target = dst.suffix.lower().lstrip(".")
    src_kind = media_kind(src.suffix)

    if src_kind != "video":
        raise ConversionError("Видео можно сделать только из видео.")

    # Video -> audio: strip video stream.
    if target in AUDIO_FORMATS:
        return _convert_audio(src, dst, settings, on_log, on_progress, duration_s,
                              on_proc, is_cancelled)

    if target in SUBTITLE_FORMATS:
        return _extract_subtitles(src, dst, settings, on_log, on_progress, duration_s,
                                  on_proc, is_cancelled)

    # GIF — palette path (no audio).
    if target == "gif":
        return _convert_gif(src, dst, settings, on_log, on_progress, duration_s,
                            on_proc, is_cancelled)

    if target not in VIDEO_FORMATS:
        raise ConversionError(f"Не умею сохранять видео в .{target}.")

    video_codec = settings.video_codec or DEFAULT_VIDEO_CODEC_FOR_CONTAINER.get(target, "h264")
    if video_codec not in VIDEO_ENCODERS:
        raise ConversionError(f"Неизвестный видеокодек: {video_codec}")
    audio_codec = settings.audio_codec_for_video or _DEFAULT_AUDIO_FOR_CONTAINER.get(target, "aac")
    if audio_codec not in AUDIO_CODECS:
        raise ConversionError(f"Неизвестный аудиокодек: {audio_codec}")
    problem = check_video_compat(target, video_codec, audio_codec)
    if problem:
        raise ConversionError(problem)

    trim = _trim_input_args(settings)
    args = ["ffmpeg", "-y", *trim, "-i", str(src)]

    # Soft subtitles from a file → second input.
    sub_codec = None
    if settings.subtitle_mode == "soft_file" and settings.subtitle_file:
        sub_codec = _SOFT_SUB_CODEC.get(target)
        if sub_codec is None:
            raise ConversionError(
                f".{target} не умеет хранить отключаемые субтитры. Выберите MKV, MP4, MOV "
                "или WebM — либо вшейте субтитры в картинку.")
        args += [*trim, "-i", str(settings.subtitle_file)]

    # Video + one audio track ("?" = don't fail on silent videos).
    args += ["-map", "0:v:0"]
    if not settings.video_mute:
        track = _valid_audio_track(src, settings, on_log)
        args += ["-map", f"0:{track}" if track is not None else "0:a:0?"]
    if sub_codec:
        args += ["-map", "1:0", "-c:s", sub_codec]

    if settings.video_mute:
        audio_args = ["-an"]
    else:
        if settings.audio_normalize and audio_codec == "copy":
            audio_codec = _DEFAULT_AUDIO_FOR_CONTAINER.get(target, "aac")
        audio_args = _audio_codec_args(audio_codec, settings)
        audio_args += _audio_filter_args(settings, audio_args)

    with _WatermarkFile(settings) as wm:
        vf = _video_filter(settings, wm, _subtitle_burn_filter(src, settings))
        tail = []
        if vf:
            tail += ["-vf", vf]
        if target in ("mp4", "m4v", "mov"):
            tail += ["-movflags", "+faststart"]
        tail += _metadata_args(settings)

        args += _video_codec_args(video_codec, settings) + audio_args + tail
        args.append(str(dst))
        _run_ffmpeg(args, on_log=on_log, on_progress=on_progress, duration_s=duration_s,
                    on_proc=on_proc, low_priority=settings.low_priority,
                    is_cancelled=is_cancelled)


_SOFT_SUB_CODEC = {
    "mkv": "srt", "mp4": "mov_text", "m4v": "mov_text", "mov": "mov_text", "webm": "webvtt",
}


def _extract_subtitles(src, dst, settings, on_log, on_progress, duration_s, on_proc,
                       is_cancelled) -> None:
    subs = [st for st in probe_streams(src) if st.kind == "subtitle"]
    if not subs:
        raise ConversionError("В этом видео нет субтитров.")
    idx = settings.subtitle_track if any(st.index == settings.subtitle_track for st in subs) \
        else subs[0].index
    _run_ffmpeg(
        ["ffmpeg", "-y", *_trim_input_args(settings), "-i", str(src), "-map", f"0:{idx}",
         str(dst)],
        on_log=on_log, on_progress=on_progress, duration_s=duration_s, on_proc=on_proc,
        is_cancelled=is_cancelled,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_video_thumbnail(src: Path, dst: Path, timestamp: float = 1.0) -> None:
    """Grab a single frame as a JPEG. Used to render a video preview thumbnail."""
    if not ffmpeg_available():
        raise ConversionError("ffmpeg не найден.")
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(src) or 0
    if duration and timestamp >= duration:
        timestamp = duration / 3  # very short clip
    _run_ffmpeg([
        "ffmpeg", "-y", "-ss", f"{timestamp:.2f}", "-i", str(src),
        "-frames:v", "1", "-vf", "scale=640:-2:flags=lanczos",
        str(dst),
    ])


def convert(
    src: Path,
    dst: Path,
    *,
    settings: Optional[ConversionSettings] = None,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    duration_s: Optional[float] = None,
    on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    """Convert a single file using the given settings.

    ``duration_s`` is the full source duration; progress is reported relative
    to the trimmed fragment. On failure or cancel a half-written ``dst`` is
    removed so it doesn't look like a finished file.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        raise ConversionError(f"Файл не найден: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    settings = settings or default_settings()

    src_kind = media_kind(src.suffix)
    dst_kind = target_kind(dst.suffix)
    if src_kind is None:
        raise ConversionError(f"Неизвестный исходный формат: {src.suffix}")
    if dst_kind is None:
        raise ConversionError(f"Неизвестный целевой формат: {dst.suffix}")

    existed_before = dst.exists()
    duration = effective_duration(duration_s, settings)
    settings = apply_target_size(settings, duration, dst.suffix)
    try:
        if src_kind == "image" and dst_kind == "image":
            _convert_image(src, dst, settings)
        elif src_kind == "audio" and dst_kind == "audio":
            _convert_audio(src, dst, settings, on_log, on_progress, duration, on_proc, is_cancelled)
        elif src_kind == "video" and (dst_kind in ("video", "audio", "subtitle")
                                      or dst.suffix.lower() == ".gif"):
            _convert_video(src, dst, settings, on_log, on_progress, duration, on_proc, is_cancelled)
        else:
            raise ConversionError(
                f"Не умею конвертировать {src_kind} → {dst_kind}."
            )
    except BaseException:
        if not existed_before and dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        raise


def apply_target_size(settings: ConversionSettings, duration_s: Optional[float],
                      dst_ext: str) -> ConversionSettings:
    """Turn "fit into N MB" into concrete bitrates (returns a modified copy)."""
    size = settings.target_size_mb
    kind = target_kind(dst_ext)
    if not size or size <= 0 or not duration_s or kind not in ("video", "audio"):
        return settings
    total_kbps = size * 8 * 1024 * 1024 / 1000 / duration_s * 0.96  # ~4% container overhead
    s = dataclasses.replace(settings)
    if kind == "audio":
        s.audio_bitrate_kbps = int(max(32, min(320, total_kbps)))
        return s
    audio = 0 if s.video_mute else (s.audio_bitrate_kbps or 128)
    if not s.video_mute and total_kbps - audio < 200:
        audio = 64  # tiny budget: give the picture what's left
    s.audio_bitrate_kbps = audio or None
    s.video_bitrate_mode = "cbr"
    s.video_bitrate_kbps = int(max(50, total_kbps - audio))
    return s


def estimate_output_size_bytes(
    src: Path,
    dst_ext: str,
    settings: ConversionSettings,
    duration_s: Optional[float] = None,
) -> Optional[int]:
    """Cheap heuristic to estimate output size in bytes. None if unknown.

    Pass ``duration_s`` when you already know it — otherwise ffprobe is run,
    which is too slow to do on every slider tick.
    """
    src = Path(src)
    dst_ext = dst_ext.lower().lstrip(".")
    src_kind = media_kind(src.suffix)
    dst_kind = target_kind(dst_ext)
    if src_kind is None or dst_kind is None:
        return None

    try:
        src_size = src.stat().st_size
    except OSError:
        return None

    if src_kind == "image" and dst_kind == "image":
        # Lossy formats: quality scaling. Lossless: roughly source size.
        target = dst_ext.lower().lstrip(".")
        if target in ("jpg", "jpeg"):
            return int(src_size * (settings.image_quality / 90) * 0.4)
        if target == "webp":
            return int(src_size * (settings.image_quality / 90) * 0.25)
        if target == "png":
            return int(src_size * 1.1)
        return src_size

    if duration_s is None:
        duration_s = probe_duration(src)
    duration = effective_duration(duration_s, settings) or 0
    if duration <= 0:
        return None

    if src_kind == "video" and dst_ext in ("gif", *SUBTITLE_FORMATS):
        return None
    if settings.target_size_mb and dst_kind in ("audio", "video"):
        return int(settings.target_size_mb * 1024 * 1024)

    if dst_kind == "audio":
        if dst_ext in ("flac", "wav", "aiff"):
            channels = settings.audio_channels or 2
            sr = settings.audio_sample_rate or 44100
            bytes_per_sec = sr * 2 * channels  # 16-bit
            return int(duration * bytes_per_sec * (0.55 if dst_ext == "flac" else 1.0))
        kbps = settings.audio_bitrate_kbps or {
            "mp3": 192, "aac": 192, "m4a": 192, "ogg": 160, "opus": 128,
        }.get(dst_ext, 160)
        return int(duration * kbps * 1000 / 8)

    if dst_kind == "video":
        if settings.video_bitrate_mode == "cbr":
            v_kbps = settings.video_bitrate_kbps
        else:
            # CRF — rough estimate based on resolution + crf value.
            height = settings.video_height or 1080
            base = {2160: 25000, 1080: 8000, 720: 4000, 480: 1500}
            ref = next((v for h, v in base.items() if height >= h), 1500)
            v_kbps = int(ref * (1.5 ** ((23 - settings.video_crf) / 6)))
            v_kbps = max(300, min(60000, v_kbps))
        a_kbps = settings.audio_bitrate_kbps or 192
        return int(duration * (v_kbps + a_kbps) * 1000 / 8)

    return None


def batch_convert(
    files: list[Path],
    target_ext: str,
    out_dir: Path,
    settings: Optional[ConversionSettings] = None,
    on_progress: Optional[Callable[[int, int, Path], None]] = None,
    on_done: Optional[Callable[[Path, ConversionResult], None]] = None,
) -> list[ConversionResult]:
    target_ext = target_ext.lower().lstrip(".")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = settings or default_settings()
    results: list[ConversionResult] = []
    total = len(files)
    for idx, src in enumerate(files, start=1):
        src = Path(src)
        if on_progress:
            on_progress(idx, total, src)
        dst = out_dir / f"{src.stem}.{target_ext}"
        if dst.resolve() == src.resolve():
            dst = out_dir / f"{src.stem}_converted.{target_ext}"
        try:
            convert(src, dst, settings=settings, duration_s=probe_duration(src))
            res = ConversionResult(src=src, dst=dst, ok=True)
        except Exception as e:
            res = ConversionResult(src=src, dst=dst, ok=False, error=str(e))
        results.append(res)
        if on_done:
            on_done(src, res)
    return results


def render_sample(
    src: Path,
    target_ext: str,
    settings: ConversionSettings,
    *,
    seconds: float = 5.0,
    duration_s: Optional[float] = None,
    out_dir: Optional[Path] = None,
    on_log: Optional[Callable[[str], None]] = None,
    on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> tuple[Path, Optional[int]]:
    """Encode a short piece from the middle to judge quality and size.

    Returns the sample path and the projected size of the full conversion.
    """
    src = Path(src)
    duration_s = duration_s if duration_s is not None else probe_duration(src)
    full = effective_duration(duration_s, settings) or 0
    start0 = settings.trim_start_s or 0
    if full and full > seconds:
        start = start0 + (full - seconds) / 2
    else:
        start = start0
    s = dataclasses.replace(settings, trim_start_s=start,
                            trim_end_s=start + min(seconds, full or seconds),
                            target_size_mb=None)
    if settings.target_size_mb and full:
        s = apply_target_size(dataclasses.replace(settings, trim_start_s=None, trim_end_s=None),
                              full, target_ext)
        s.trim_start_s, s.trim_end_s, s.target_size_mb = start, start + min(seconds, full), None
    folder = Path(out_dir or tempfile.gettempdir()) / "converter_samples"
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / f"{src.stem}_sample.{target_ext.lstrip('.')}"
    if dst.exists():
        dst.unlink()
    convert(src, dst, settings=s, duration_s=duration_s, on_log=on_log, on_proc=on_proc,
            is_cancelled=is_cancelled)
    sample_len = min(seconds, full or seconds)
    projected = None
    if full and sample_len:
        projected = int(dst.stat().st_size * full / sample_len)
    return dst, projected


# ---------------------------------------------------------------------------
# Concatenation — several audio or video files into one
# ---------------------------------------------------------------------------

def probe_video_size(path: Path) -> Optional[tuple[int, int]]:
    if not ffprobe_available():
        return None
    try:
        proc = subprocess.run(
            [_ffprobe(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=_creationflags(),
        )
        w, h = (int(x) for x in (proc.stdout or "").strip().split("x")[:2])
        return w, h
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _has_audio(path: Path) -> bool:
    return any(st.kind == "audio" for st in probe_streams(path))


def concat_files(
    srcs: list[Path],
    dst: Path,
    settings: Optional[ConversionSettings] = None,
    *,
    durations: Optional[list[float]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    """Join audio (→ audio) or video (→ video / audio) files end to end.

    Inputs may differ in resolution, fps, sample rate: every part is
    normalised (scaled + letterboxed to the first video's frame) first.
    """
    settings = settings or default_settings()
    srcs = [Path(p) for p in srcs]
    if len(srcs) < 2:
        raise ConversionError("Для склейки нужно хотя бы два файла.")
    dst = Path(dst)
    target = dst.suffix.lower().lstrip(".")
    kinds = {media_kind(p.suffix) for p in srcs}
    if kinds - {"audio", "video"}:
        raise ConversionError("Склеивать можно только аудио и видео.")
    out_kind = target_kind(target)
    video_out = out_kind == "video"
    if video_out and kinds != {"video"}:
        raise ConversionError("Видео можно склеить только из видео.")
    if durations is None:
        durations = [probe_duration(p) or 0 for p in srcs]
    total = sum(durations) or None
    settings = apply_target_size(settings, total, target)

    args = ["ffmpeg", "-y"]
    for p in srcs:
        args += ["-i", str(p)]
    chains: list[str] = []
    labels: list[str] = []
    want_audio = not (video_out and settings.video_mute)
    n_inputs = len(srcs)

    if video_out:
        w, h = probe_video_size(srcs[0]) or (1280, 720)
        if settings.video_height:
            w, h = int(round(w * settings.video_height / h / 2)) * 2, settings.video_height
        fps = settings.video_fps or 30
    for i, p in enumerate(srcs):
        if video_out:
            chains.append(
                f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{i}]")
            labels.append(f"[v{i}]")
        if want_audio:
            if _has_audio(p):
                chains.append(f"[{i}:a:0]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
            else:  # silent part: synthesise silence of the same length
                args += ["-f", "lavfi", "-t", f"{durations[i] or 1:.3f}",
                         "-i", "anullsrc=r=48000:cl=stereo"]
                chains.append(f"[{n_inputs}:a]anull[a{i}]")
                n_inputs += 1
            labels.append(f"[a{i}]")

    v, a = int(video_out), int(want_audio)
    graph = ";".join(chains) + ";" + "".join(labels) + f"concat=n={len(srcs)}:v={v}:a={a}"
    outs = []
    if video_out:
        graph += "[vc]"
        outs.append("vc")
    if want_audio:
        graph += "[ac]"
        outs.append("ac")
    post_v = _orient_filters(settings)
    if video_out and post_v:
        graph += f";[vc]{','.join(post_v)}[vo]"
        outs[0] = "vo"
    if want_audio and settings.audio_normalize:
        graph += f";[ac]{LOUDNORM_FILTER}[ao]"
        outs[-1] = "ao"
    args += ["-filter_complex", graph]
    for o in outs:
        args += ["-map", f"[{o}]"]

    if video_out:
        vcodec = settings.video_codec or DEFAULT_VIDEO_CODEC_FOR_CONTAINER.get(target, "h264")
        acodec = settings.audio_codec_for_video or _DEFAULT_AUDIO_FOR_CONTAINER.get(target, "aac")
        if acodec == "copy":
            acodec = _DEFAULT_AUDIO_FOR_CONTAINER.get(target, "aac")
        problem = check_video_compat(target, vcodec, acodec)
        if problem:
            raise ConversionError(problem)
        args += _video_codec_args(vcodec, settings)
        if want_audio:
            args += _audio_codec_args(acodec, settings)
        if target in ("mp4", "m4v", "mov"):
            args += ["-movflags", "+faststart"]
    elif target == "wav":
        args += ["-c:a", "pcm_s16le", *_audio_format_args(settings)]
    elif target == "aiff":
        args += ["-c:a", "pcm_s16be", *_audio_format_args(settings)]
    else:
        args += _audio_codec_args(_AUDIO_TARGET_CODEC.get(target, "aac"), settings)
    args.append(str(dst))

    existed = dst.exists()
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run_ffmpeg(args, on_log=on_log, on_progress=on_progress, duration_s=total,
                    on_proc=on_proc, low_priority=settings.low_priority,
                    is_cancelled=is_cancelled)
    except BaseException:
        if not existed and dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        raise


# ---------------------------------------------------------------------------
# Output names and human-friendly errors
# ---------------------------------------------------------------------------

NAME_TOKENS = {
    "{name}": "имя исходного файла",
    "{ext}": "расширение исходного файла",
    "{format}": "новый формат",
    "{n}": "номер в очереди (001)",
    "{date}": "дата (2026-11-18)",
    "{time}": "время (14-05)",
    "{height}": "высота видео (720p)",
}
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def render_name(template: str, src: Path, target: str, index: int = 1,
                settings: Optional[ConversionSettings] = None,
                now: Optional[datetime.datetime] = None) -> str:
    """Build an output file stem from a template like "{name}_{height}"."""
    now = now or datetime.datetime.now()
    height = f"{settings.video_height}p" if settings and settings.video_height else ""
    values = {
        "{name}": src.stem,
        "{ext}": src.suffix.lstrip(".").lower(),
        "{format}": target.lower(),
        "{n}": f"{index:03d}",
        "{date}": now.strftime("%Y-%m-%d"),
        "{time}": now.strftime("%H-%M"),
        "{height}": height,
    }
    out = template or "{name}"
    for k, v in values.items():
        out = out.replace(k, v)
    out = _BAD_NAME_CHARS.sub("_", out).strip(" ._-")
    out = re.sub(r"([_\- ])\1+", r"\1", out)  # "a__b" left by an empty token → "a_b"
    return out or src.stem


_ERROR_HINTS: list[tuple[str, str]] = [
    (r"No space left on device", "На диске закончилось место."),
    (r"Permission denied|Access is denied", "Нет доступа к файлу или папке — проверьте права или "
                                            "закройте файл в других программах."),
    (r"No such file or directory", "Файл не найден — возможно, его переместили или удалили."),
    (r"Invalid data found when processing input|moov atom not found|"
     r"could not find codec parameters", "Файл повреждён или недокачан."),
    (r"Unknown encoder|Encoder not found|Unrecognized option", "В вашем ffmpeg нет нужного "
                                                               "кодека — обновите ffmpeg."),
    (r"Cannot load nvcuda|No NVENC capable devices|OpenEncodeSessionEx failed|"
     r"Error initializing output stream.*(nvenc|qsv|amf)|Failed to create.*(qsv|amf)",
     "Аппаратное кодирование недоступно на этом компьютере — переключитесь на «Процессор»."),
    (r"matches no streams|Stream map .* matches no streams|does not contain any stream",
     "В файле нет нужной дорожки (например, звука или видео)."),
    (r"Could not open encoder before EOF|Nothing was written into output file",
     "После обрезки ничего не осталось — проверьте время начала и конца."),
    (r"Subtitle encoding currently only possible from text to text|"
     r"Unable to open .*\.(srt|ass|vtt)|Error initializing filter 'subtitles'",
     "Не получилось прочитать субтитры."),
    (r"codec not currently supported in container|Could not find tag for codec",
     "Этот кодек нельзя положить в выбранный контейнер."),
    (r"cannot identify image file|cannot write mode|encoder .* not available",
     "Pillow не смог прочитать или записать эту картинку."),
]


def humanize_error(message: str) -> str:
    """Short Russian explanation of a conversion error (first line for the UI)."""
    for pattern, hint in _ERROR_HINTS:
        if re.search(pattern, message, re.IGNORECASE):
            return hint
    first = message.strip().splitlines()[0] if message.strip() else "Неизвестная ошибка"
    return first[:200]


# ---------------------------------------------------------------------------
# Smart presets — apply to ConversionSettings + suggest target extension.
# ---------------------------------------------------------------------------

@dataclass
class Preset:
    key: str
    label: str
    description: str
    target_ext: str
    apply: Callable[[ConversionSettings], None]


def _preset_youtube_1080() -> Preset:
    def apply(s: ConversionSettings) -> None:
        s.video_codec = "h264"
        s.video_height = 1080
        s.video_bitrate_mode = "cbr"
        s.video_bitrate_kbps = 8000
        s.video_fps = None  # keep
        s.audio_codec_for_video = "aac"
        s.audio_bitrate_kbps = 192
        s.audio_sample_rate = 48000
    return Preset(
        "youtube_1080", "YouTube 1080p (MP4)",
        "H.264, 8 Мбит/с, AAC 192 kbps, 48 кГц — рекомендованные параметры YouTube.",
        "mp4", apply,
    )


def _preset_youtube_4k() -> Preset:
    def apply(s: ConversionSettings) -> None:
        s.video_codec = "h265"
        s.video_height = 2160
        s.video_bitrate_mode = "cbr"
        s.video_bitrate_kbps = 35000
        s.audio_codec_for_video = "aac"
        s.audio_bitrate_kbps = 192
        s.audio_sample_rate = 48000
    return Preset(
        "youtube_4k", "YouTube 4K (MP4)",
        "H.265, 35 Мбит/с — для аплоада в 4K без артефактов.",
        "mp4", apply,
    )


def _preset_stories() -> Preset:
    def apply(s: ConversionSettings) -> None:
        s.video_codec = "h264"
        s.video_aspect = "9:16"  # center-crop horizontal footage to vertical
        s.video_height = 1920    # → 1080x1920
        s.video_bitrate_mode = "cbr"
        s.video_bitrate_kbps = 5000
        s.video_fps = 30
        s.audio_codec_for_video = "aac"
        s.audio_bitrate_kbps = 128
    return Preset(
        "stories", "Сторис / Reels 9:16",
        "Кадрирует в вертикаль 1080×1920, H.264, 5 Мбит/с, 30 fps — для Instagram/TikTok.",
        "mp4", apply,
    )


def _preset_telegram() -> Preset:
    def apply(s: ConversionSettings) -> None:
        s.video_codec = "h264"
        s.video_height = 720
        s.video_bitrate_mode = "cbr"
        s.video_bitrate_kbps = 1500
        s.audio_codec_for_video = "aac"
        s.audio_bitrate_kbps = 128
    return Preset(
        "telegram", "Сжать для Telegram",
        "720p H.264, 1.5 Мбит/с — небольшой файл, отправляется без перепакования.",
        "mp4", apply,
    )


def _preset_email() -> Preset:
    def apply(s: ConversionSettings) -> None:
        s.video_codec = "h264"
        s.video_height = 480
        s.video_bitrate_mode = "cbr"
        s.video_bitrate_kbps = 800
        s.audio_codec_for_video = "aac"
        s.audio_bitrate_kbps = 96
    return Preset(
        "email", "Сжать для почты",
        "480p, 800 кбит/с — лёгкий файл, проходит лимиты почты (≤25 МБ за минуту).",
        "mp4", apply,
    )


def _preset_archive_flac() -> Preset:
    def apply(s: ConversionSettings) -> None:
        s.audio_sample_rate = 44100
        s.audio_channels = 2
    return Preset(
        "archive_flac", "Архив FLAC",
        "Аудио без потерь — для бэкапа музыкальной коллекции.",
        "flac", apply,
    )


def _preset_mp3_high() -> Preset:
    def apply(s: ConversionSettings) -> None:
        s.audio_bitrate_kbps = 320
        s.audio_sample_rate = 44100
        s.audio_channels = 2
    return Preset(
        "mp3_high", "MP3 320 kbps",
        "Максимальное качество MP3 — почти неотличимо от lossless на слух.",
        "mp3", apply,
    )


def _preset_aac_streaming() -> Preset:
    def apply(s: ConversionSettings) -> None:
        s.audio_bitrate_kbps = 192
        s.audio_sample_rate = 44100
        s.audio_channels = 2
    return Preset(
        "aac_streaming", "AAC 192 kbps",
        "Стриминговый стандарт — Apple Music, Spotify Premium.",
        "m4a", apply,
    )


def _preset_web_jpg() -> Preset:
    def apply(s: ConversionSettings) -> None:
        s.image_quality = 85
        s.image_strip_exif = True
        s.image_resize_width = 1920
    return Preset(
        "web_jpg", "JPEG для веба",
        "1920px, качество 85, EXIF убран — оптимально для сайта.",
        "jpg", apply,
    )


def _preset_web_webp() -> Preset:
    def apply(s: ConversionSettings) -> None:
        s.image_quality = 82
        s.image_strip_exif = True
        s.image_resize_width = 1920
    return Preset(
        "web_webp", "WebP для веба",
        "На ~30% легче JPEG при том же визуальном качестве.",
        "webp", apply,
    )


PRESETS: list[Preset] = [
    _preset_youtube_1080(),
    _preset_youtube_4k(),
    _preset_stories(),
    _preset_telegram(),
    _preset_email(),
    _preset_archive_flac(),
    _preset_mp3_high(),
    _preset_aac_streaming(),
    _preset_web_jpg(),
    _preset_web_webp(),
]


def get_preset(key: str) -> Optional[Preset]:
    for p in PRESETS:
        if p.key == key:
            return p
    return None
