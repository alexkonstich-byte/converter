"""Conversion engine: images via Pillow, audio/video via ffmpeg."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
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


@dataclass
class ConversionResult:
    src: Path
    dst: Path
    ok: bool
    error: str = ""


class ConversionError(Exception):
    pass


def _convert_image(src: Path, dst: Path) -> None:
    img = Image.open(src)
    target = dst.suffix.lower().lstrip(".")

    # Animated GIF/WebP support: keep frames if both ends are animated formats.
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

    # Formats that don't accept alpha — flatten onto white.
    if target in ("jpg", "jpeg", "bmp", "pcx") and img.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
    elif img.mode == "P":
        img = img.convert("RGBA")

    save_kwargs: dict = {}
    if target in ("jpg", "jpeg"):
        save_kwargs["quality"] = 95
        save_kwargs["optimize"] = True
    elif target == "webp":
        save_kwargs["quality"] = 95
    elif target == "png":
        save_kwargs["optimize"] = True

    img.save(dst, **save_kwargs)


def _run_ffmpeg(args: list[str]) -> None:
    if not ffmpeg_available():
        raise ConversionError(
            "ffmpeg не найден в PATH. Установите ffmpeg, чтобы конвертировать аудио и видео."
        )
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x08000000  # CREATE_NO_WINDOW
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        creationflags=creationflags,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        raise ConversionError("ffmpeg: " + " | ".join(tail) if tail else "ffmpeg failed")


def _convert_audio(src: Path, dst: Path) -> None:
    target = dst.suffix.lower().lstrip(".")
    args = ["ffmpeg", "-y", "-i", str(src), "-vn"]
    if target == "mp3":
        args += ["-codec:a", "libmp3lame", "-q:a", "2"]
    elif target == "ogg":
        args += ["-codec:a", "libvorbis", "-q:a", "5"]
    elif target == "opus":
        args += ["-codec:a", "libopus", "-b:a", "128k"]
    elif target == "m4a" or target == "aac":
        args += ["-codec:a", "aac", "-b:a", "192k"]
    elif target == "flac":
        args += ["-codec:a", "flac"]
    elif target == "wav":
        args += ["-codec:a", "pcm_s16le"]
    args.append(str(dst))
    _run_ffmpeg(args)


# Codec name → ffmpeg encoder name. "copy" stream-copies without re-encoding.
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


def _audio_codec_args(codec_key: str) -> list[str]:
    """Return ffmpeg args for the given audio codec key."""
    encoder = AUDIO_CODECS[codec_key]
    if encoder == "copy":
        return ["-c:a", "copy"]
    args = ["-c:a", encoder]
    # Sensible bitrate defaults for lossy codecs.
    if encoder == "aac":
        args += ["-b:a", "192k"]
    elif encoder == "libmp3lame":
        args += ["-q:a", "2"]
    elif encoder == "libopus":
        args += ["-b:a", "128k"]
    elif encoder == "libvorbis":
        args += ["-q:a", "5"]
    elif encoder == "ac3":
        args += ["-b:a", "192k"]
    return args


def _convert_video(src: Path, dst: Path, audio_codec: str | None = None) -> None:
    target = dst.suffix.lower().lstrip(".")
    src_kind = media_kind(src.suffix)

    # Image -> video isn't supported here; route image-to-image instead.
    if src_kind == "image":
        raise ConversionError("Нельзя превратить картинку в видео в этой версии.")

    # Video -> audio: strip video stream.
    if src_kind == "video" and target in AUDIO_FORMATS:
        return _convert_audio(src, dst)

    # Resolve audio codec: explicit user pick wins, else container default.
    if not audio_codec:
        audio_codec = _DEFAULT_AUDIO_FOR_CONTAINER.get(target, "aac")
    if audio_codec not in AUDIO_CODECS:
        raise ConversionError(f"Неизвестный аудиокодек: {audio_codec}")

    args = ["ffmpeg", "-y", "-i", str(src)]
    if target == "webm":
        args += ["-c:v", "libvpx-vp9", "-b:v", "1M"]
        args += _audio_codec_args(audio_codec)
    elif target in ("mp4", "m4v", "mov"):
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
        args += _audio_codec_args(audio_codec)
        args += ["-movflags", "+faststart"]
    elif target == "mkv":
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
        args += _audio_codec_args(audio_codec)
    elif target == "gif":
        # Build a palette pass for higher quality animated GIFs.
        palette = dst.with_suffix(".palette.png")
        try:
            _run_ffmpeg([
                "ffmpeg", "-y", "-i", str(src),
                "-vf", "fps=15,scale=480:-1:flags=lanczos,palettegen",
                str(palette),
            ])
            _run_ffmpeg([
                "ffmpeg", "-y", "-i", str(src), "-i", str(palette),
                "-lavfi", "fps=15,scale=480:-1:flags=lanczos [x]; [x][1:v] paletteuse",
                str(dst),
            ])
        finally:
            if palette.exists():
                palette.unlink()
        return
    else:
        # Containers we don't have a specific recipe for — let ffmpeg pick.
        args += _audio_codec_args(audio_codec)
    args.append(str(dst))
    _run_ffmpeg(args)


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


def convert(src: Path, dst: Path, audio_codec: str | None = None) -> None:
    """Convert a single file. Caller picks the destination extension.

    audio_codec only applies to video-to-video conversions (controls the
    embedded audio track). Other paths ignore it.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        raise ConversionError(f"Файл не найден: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    src_kind = media_kind(src.suffix)
    dst_kind = media_kind(dst.suffix)
    if src_kind is None:
        raise ConversionError(f"Неизвестный исходный формат: {src.suffix}")
    if dst_kind is None:
        raise ConversionError(f"Неизвестный целевой формат: {dst.suffix}")

    # Image -> image
    if src_kind == "image" and dst_kind == "image":
        _convert_image(src, dst)
        return

    # Audio -> audio
    if src_kind == "audio" and dst_kind == "audio":
        _convert_audio(src, dst)
        return

    # Anything involving video, or video-audio routing
    if dst_kind == "video" or src_kind == "video":
        _convert_video(src, dst, audio_codec=audio_codec)
        return

    # Audio -> image or image -> audio doesn't make sense.
    raise ConversionError(
        f"Не умею конвертировать {src_kind} → {dst_kind}."
    )


def batch_convert(
    files: list[Path],
    target_ext: str,
    out_dir: Path,
    on_progress: Optional[Callable[[int, int, Path], None]] = None,
    on_done: Optional[Callable[[Path, ConversionResult], None]] = None,
) -> list[ConversionResult]:
    target_ext = target_ext.lower().lstrip(".")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[ConversionResult] = []
    total = len(files)
    for idx, src in enumerate(files, start=1):
        src = Path(src)
        if on_progress:
            on_progress(idx, total, src)
        dst = out_dir / f"{src.stem}.{target_ext}"
        # Avoid overwriting source when extensions match.
        if dst.resolve() == src.resolve():
            dst = out_dir / f"{src.stem}_converted.{target_ext}"
        try:
            convert(src, dst)
            res = ConversionResult(src=src, dst=dst, ok=True)
        except (ConversionError, Exception) as e:
            res = ConversionResult(src=src, dst=dst, ok=False, error=str(e))
        results.append(res)
        if on_done:
            on_done(src, res)
    return results
