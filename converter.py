"""Conversion engine: images via Pillow, audio/video via ffmpeg."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageSequence

IMAGE_FORMATS = [
    "png", "jpg", "jpeg", "webp", "bmp", "tiff", "tif",
    "gif", "ico", "tga", "ppm", "pcx",
]
AUDIO_FORMATS = [
    "mp3", "wav", "flac", "ogg", "m4a", "aac", "wma", "opus", "aiff",
]
VIDEO_FORMATS = [
    "mp4", "mkv", "avi", "mov", "webm", "flv", "wmv", "m4v", "mpeg", "mpg", "ts",
]

ALL_FORMATS = sorted(set(IMAGE_FORMATS + AUDIO_FORMATS + VIDEO_FORMATS))


def media_kind(ext: str) -> Optional[str]:
    ext = ext.lower().lstrip(".")
    if ext in IMAGE_FORMATS:
        return "image"
    if ext in AUDIO_FORMATS:
        return "audio"
    if ext in VIDEO_FORMATS:
        return "video"
    return None


def formats_for_kind(kind: str) -> list[str]:
    return {"image": IMAGE_FORMATS, "audio": AUDIO_FORMATS, "video": VIDEO_FORMATS}[kind]


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


# Encoder name → ffmpeg encoder. "copy" stream-copies without re-encoding.
AUDIO_CODECS = {
    "aac": "aac",
    "mp3": "libmp3lame",
    "opus": "libopus",
    "vorbis": "libvorbis",
    "flac": "flac",
    "ac3": "ac3",
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

    # Behaviour
    copy_metadata: bool = True
    overwrite_mode: str = "rename"            # "rename" / "overwrite" / "skip"


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
) -> None:
    """Run ffmpeg, optionally streaming logs and parsing progress timestamps."""
    if not ffmpeg_available():
        raise ConversionError(
            "ffmpeg не найден в PATH. Установите ffmpeg, чтобы конвертировать аудио и видео."
        )

    # ffmpeg writes progress to stderr by default; "-progress pipe:1" gives a
    # stable key=value stream on stdout that's easier to parse, but we'd lose
    # the human-readable error text. Keep stderr-based parsing.
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=_creationflags(),
    )

    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
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
        # ffmpeg progress lines look like:
        # frame= 123 fps= 24 q=28.0 size=...time=00:00:05.04 bitrate=...
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
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, timeout=10,
            creationflags=_creationflags(),
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
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=bit_rate",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, timeout=10,
            creationflags=_creationflags(),
        )
        out = (proc.stdout or "").strip()
        if not out or out == "N/A":
            return None
        return int(int(out) / 1000)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None


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


def _convert_image(src: Path, dst: Path, settings: ConversionSettings) -> None:
    img = Image.open(src)
    target = dst.suffix.lower().lstrip(".")

    is_animated = getattr(img, "is_animated", False)
    if is_animated and target in ("gif", "webp"):
        frames = [frame.copy() for frame in ImageSequence.Iterator(img)]
        frames[0].save(
            dst,
            save_all=True,
            append_images=frames[1:],
            loop=img.info.get("loop", 0),
            duration=img.info.get("duration", 100),
            disposal=2,
        )
        return

    img = _resize_image(img, settings)

    # Formats that don't accept alpha — flatten onto white.
    if target in ("jpg", "jpeg", "bmp", "pcx") and img.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
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
    elif target == "png":
        save_kwargs["optimize"] = True

    # EXIF: keep by default, strip if requested.
    if not settings.image_strip_exif:
        exif = img.info.get("exif")
        if exif:
            save_kwargs["exif"] = exif

    img.save(dst, **save_kwargs)


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
    elif encoder == "ac3":
        args += ["-b:a", f"{bitrate or 192}k"]
    # FLAC is lossless — bitrate doesn't apply.

    if settings.audio_sample_rate:
        args += ["-ar", str(settings.audio_sample_rate)]
    if settings.audio_channels:
        args += ["-ac", str(settings.audio_channels)]
    return args


def _convert_audio(
    src: Path,
    dst: Path,
    settings: ConversionSettings,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    duration_s: Optional[float] = None,
) -> None:
    target = dst.suffix.lower().lstrip(".")
    args = ["ffmpeg", "-y", "-i", str(src), "-vn"]

    # Pick a codec per target container — same as before but routed through
    # _audio_codec_args so user bitrate/sample-rate/channels get applied.
    container_codec = {
        "mp3": "mp3", "ogg": "vorbis", "opus": "opus",
        "m4a": "aac", "aac": "aac", "flac": "flac",
        "wav": "copy",  # wav will use pcm regardless; "copy" is replaced below
        "wma": "copy",  # wma encoder usually unavailable in default builds
        "aiff": "copy",
    }.get(target, "aac")

    if target == "wav":
        args += ["-c:a", "pcm_s16le"]
        if settings.audio_sample_rate:
            args += ["-ar", str(settings.audio_sample_rate)]
        if settings.audio_channels:
            args += ["-ac", str(settings.audio_channels)]
    elif target == "aiff":
        args += ["-c:a", "pcm_s16be"]
        if settings.audio_sample_rate:
            args += ["-ar", str(settings.audio_sample_rate)]
        if settings.audio_channels:
            args += ["-ac", str(settings.audio_channels)]
    else:
        args += _audio_codec_args(container_codec, settings)

    if settings.copy_metadata:
        args += ["-map_metadata", "0"]

    args.append(str(dst))
    _run_ffmpeg(args, on_log=on_log, on_progress=on_progress, duration_s=duration_s)


# ---------------------------------------------------------------------------
# Video conversion
# ---------------------------------------------------------------------------

def _video_codec_args(codec_key: str, settings: ConversionSettings) -> list[str]:
    encoder = VIDEO_ENCODERS[codec_key]
    args = ["-c:v", encoder]

    if settings.video_bitrate_mode == "cbr":
        kbps = max(100, settings.video_bitrate_kbps)
        args += ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
        # AV1 (svt-av1) wants -b:v; x264/x265 work the same way.
    else:
        crf = max(0, min(63, settings.video_crf))
        if encoder in ("libx264", "libx265"):
            args += ["-preset", "medium", "-crf", str(crf)]
        elif encoder == "libsvtav1":
            args += ["-preset", "8", "-crf", str(crf)]
    return args


def _video_filter(settings: ConversionSettings) -> Optional[str]:
    parts: list[str] = []
    if settings.video_height:
        # Keep aspect ratio, ensure even dimensions for x264/x265.
        parts.append(f"scale=-2:{settings.video_height}:flags=lanczos")
    if settings.video_fps:
        parts.append(f"fps={settings.video_fps}")
    return ",".join(parts) if parts else None


def _convert_video(
    src: Path,
    dst: Path,
    settings: ConversionSettings,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    duration_s: Optional[float] = None,
) -> None:
    target = dst.suffix.lower().lstrip(".")
    src_kind = media_kind(src.suffix)

    if src_kind == "image":
        raise ConversionError("Нельзя превратить картинку в видео в этой версии.")

    # Video -> audio: strip video stream.
    if src_kind == "video" and target in AUDIO_FORMATS:
        return _convert_audio(src, dst, settings, on_log, on_progress, duration_s)

    # Video codec
    video_codec = settings.video_codec or DEFAULT_VIDEO_CODEC_FOR_CONTAINER.get(target, "h264")
    if video_codec not in VIDEO_ENCODERS:
        raise ConversionError(f"Неизвестный видеокодек: {video_codec}")

    # Audio codec for the video container
    audio_codec = settings.audio_codec_for_video or _DEFAULT_AUDIO_FOR_CONTAINER.get(target, "aac")
    if audio_codec not in AUDIO_CODECS:
        raise ConversionError(f"Неизвестный аудиокодек: {audio_codec}")

    args = ["ffmpeg", "-y", "-i", str(src)]

    # GIF — special multi-pass palette path.
    if target == "gif":
        palette = dst.with_suffix(".palette.png")
        try:
            _run_ffmpeg(
                ["ffmpeg", "-y", "-i", str(src),
                 "-vf", "fps=15,scale=480:-1:flags=lanczos,palettegen",
                 str(palette)],
                on_log=on_log,
            )
            _run_ffmpeg(
                ["ffmpeg", "-y", "-i", str(src), "-i", str(palette),
                 "-lavfi", "fps=15,scale=480:-1:flags=lanczos [x]; [x][1:v] paletteuse",
                 str(dst)],
                on_log=on_log,
            )
        finally:
            if palette.exists():
                palette.unlink()
        return

    args += _video_codec_args(video_codec, settings)
    args += _audio_codec_args(audio_codec, settings)

    vf = _video_filter(settings)
    if vf:
        args += ["-vf", vf]

    if target in ("mp4", "m4v", "mov"):
        args += ["-movflags", "+faststart"]

    if settings.copy_metadata:
        args += ["-map_metadata", "0"]

    args.append(str(dst))
    _run_ffmpeg(args, on_log=on_log, on_progress=on_progress, duration_s=duration_s)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_video_thumbnail(src: Path, dst: Path, timestamp: str = "00:00:01") -> None:
    """Grab a single frame as a JPEG. Used to render a video preview thumbnail."""
    if not ffmpeg_available():
        raise ConversionError("ffmpeg не найден.")
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "ffmpeg", "-y", "-ss", timestamp, "-i", str(src),
        "-frames:v", "1", "-vf", "scale=480:-1:flags=lanczos",
        str(dst),
    ])


def convert(
    src: Path,
    dst: Path,
    *,
    audio_codec: Optional[str] = None,  # legacy positional support
    settings: Optional[ConversionSettings] = None,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    duration_s: Optional[float] = None,
) -> None:
    """Convert a single file using the given settings.

    ``audio_codec`` kept for backward compatibility — equivalent to setting
    ``settings.audio_codec_for_video``.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        raise ConversionError(f"Файл не найден: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    if settings is None:
        settings = default_settings()
    if audio_codec and not settings.audio_codec_for_video:
        settings.audio_codec_for_video = audio_codec

    src_kind = media_kind(src.suffix)
    dst_kind = media_kind(dst.suffix)
    if src_kind is None:
        raise ConversionError(f"Неизвестный исходный формат: {src.suffix}")
    if dst_kind is None:
        raise ConversionError(f"Неизвестный целевой формат: {dst.suffix}")

    if src_kind == "image" and dst_kind == "image":
        _convert_image(src, dst, settings)
        return

    if src_kind == "audio" and dst_kind == "audio":
        _convert_audio(src, dst, settings, on_log, on_progress, duration_s)
        return

    if dst_kind == "video" or src_kind == "video":
        _convert_video(src, dst, settings, on_log, on_progress, duration_s)
        return

    raise ConversionError(f"Не умею конвертировать {src_kind} → {dst_kind}.")


def estimate_output_size_bytes(
    src: Path,
    dst_ext: str,
    settings: ConversionSettings,
) -> Optional[int]:
    """Cheap heuristic to estimate output size in bytes. None if unknown."""
    src = Path(src)
    src_kind = media_kind(src.suffix)
    dst_kind = media_kind(dst_ext)
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

    duration = probe_duration(src) or 0
    if duration <= 0:
        return None

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
            convert(src, dst, settings=settings)
            res = ConversionResult(src=src, dst=dst, ok=True)
        except (ConversionError, Exception) as e:
            res = ConversionResult(src=src, dst=dst, ok=False, error=str(e))
        results.append(res)
        if on_done:
            on_done(src, res)
    return results


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
        s.video_height = 1920  # vertical 9:16 ~1080x1920
        s.video_bitrate_mode = "cbr"
        s.video_bitrate_kbps = 5000
        s.video_fps = 30
        s.audio_codec_for_video = "aac"
        s.audio_bitrate_kbps = 128
    return Preset(
        "stories", "Сторис / Reels 9:16",
        "H.264, 5 Мбит/с, 30 fps — формат сторис в Instagram/TikTok.",
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
