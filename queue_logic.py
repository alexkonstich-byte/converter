"""UI-independent queue logic: target formats, output paths, persistence.

Kept free of Flet so it can be unit-tested (see tests/test_queue_logic.py).
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Iterable, Optional

from converter import (
    ALL_FORMATS,
    AUDIO_FORMATS,
    IMAGE_FORMATS,
    SUBTITLE_FORMATS,
    VIDEO_FORMATS,
    ConversionSettings,
    media_kind,
    render_name,
    target_kind,
)

# Target groups for the format picker. Video can also become a GIF or subtitles.
TARGET_GROUPS: dict[str, list[str]] = {
    "image": IMAGE_FORMATS,
    "audio": AUDIO_FORMATS,
    "video": VIDEO_FORMATS + ["gif"] + SUBTITLE_FORMATS,
}

# Settings that describe *how* the user works rather than a quality recipe —
# presets must not reset them.
STICKY_FIELDS = (
    "overwrite_mode", "compute_checksum", "copy_metadata", "low_priority", "hwaccel",
    "trim_start_s", "trim_end_s", "watermark_text", "watermark_position",
    "watermark_opacity", "watermark_color", "name_template", "keep_structure",
    "subtitle_mode", "subtitle_file",
)

# Per-session / per-file values that must not be remembered between launches
# or stored in user presets.
_TRANSIENT_FIELDS = {
    "trim_start_s", "trim_end_s", "audio_track_index", "subtitle_track",
    "subtitle_file", "subtitle_mode", "target_size_mb",
}
_BEHAVIOUR_FIELDS = {
    "overwrite_mode", "compute_checksum", "copy_metadata", "low_priority",
    "name_template", "keep_structure", "hwaccel",
}


def allowed_targets(kinds: Iterable[Optional[str]]) -> set[str]:
    """Formats every queued file can be converted to."""
    kinds = list(kinds)
    if not kinds:
        return set(ALL_FORMATS) | {"gif"} | set(SUBTITLE_FORMATS)
    allowed: Optional[set[str]] = None
    for kind in kinds:
        if kind == "image":
            s = set(IMAGE_FORMATS)
        elif kind == "audio":
            s = set(AUDIO_FORMATS)
        elif kind == "video":
            s = set(VIDEO_FORMATS) | set(AUDIO_FORMATS) | {"gif"} | set(SUBTITLE_FORMATS)
        else:
            s = set()
        allowed = s if allowed is None else allowed & s
    return allowed or set()


def allowed_groups(kinds: Iterable[Optional[str]]) -> set[str]:
    kinds = set(kinds)
    if not kinds:
        return {"image", "audio", "video"}
    groups = set()
    if kinds == {"image"}:
        groups.add("image")
    if kinds <= {"audio", "video"}:
        groups.add("audio")
    if kinds == {"video"}:
        groups.add("video")
    return groups


def can_concat(kinds: list[Optional[str]], target: Optional[str]) -> bool:
    """Joining needs ≥2 audio/video files and an audio or video target."""
    if len(kinds) < 2 or not target:
        return False
    tk = target_kind(target)
    if tk == "audio":
        return set(kinds) <= {"audio", "video"}
    if tk == "video":
        return set(kinds) == {"video"}
    return False


def auto_jobs(kinds: Iterable[Optional[str]], target: Optional[str]) -> int:
    """How many files to convert at once when the user leaves it on "Auto".

    ffmpeg already uses every core for video, so videos go one by one;
    Pillow and audio encoders are single-threaded and scale well.
    """
    cpus = os.cpu_count() or 2
    kinds = set(kinds)
    if "video" in kinds and target_kind(target or "") in ("video", None):
        return 1
    if kinds == {"image"}:
        return max(1, min(8, cpus))
    return max(1, min(4, cpus // 2))


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def files_word(n: int) -> str:
    return plural(n, "файл", "файла", "файлов")


def output_path(src: Path, target: str, out_dir: Optional[Path], root: Optional[Path],
                settings: ConversionSettings, index: int, taken: set[Path]) -> Optional[Path]:
    """Where to write ``src`` — template, folder structure and overwrite policy.

    ``root`` is the folder the file was added from (for keep_structure).
    ``taken`` holds outputs already claimed by this batch. None = skip.
    """
    folder = out_dir or src.parent
    if out_dir is not None and settings.keep_structure and root is not None:
        try:
            folder = out_dir / src.parent.relative_to(root)
        except ValueError:
            pass
    stem = render_name(settings.name_template, src, target, index, settings)
    dst = folder / f"{stem}.{target}"
    if _same_file(dst, src):
        dst = folder / f"{stem}_converted.{target}"
    if not dst.exists() and dst not in taken:
        return dst
    if settings.overwrite_mode == "overwrite" and dst not in taken:
        return dst
    if settings.overwrite_mode == "skip":
        return None
    i = 1
    while True:
        cand = folder / f"{dst.stem}_{i}.{target}"
        if not cand.exists() and cand not in taken:
            return cand
        i += 1


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def scan_folder(folder: Path, recursive: bool = True) -> list[Path]:
    """Supported media files inside a folder, sorted naturally by path."""
    pattern = "**/*" if recursive else "*"
    found = [p for p in folder.glob(pattern)
             if p.is_file() and not p.name.startswith(".") and media_kind(p.suffix)]
    return sorted(found, key=lambda p: [part.lower() for part in p.relative_to(folder).parts])


# ----------------------------------------------------------------- persistence
def settings_to_dict(s: ConversionSettings, *, behaviour: bool = True) -> dict:
    """JSON-friendly snapshot. ``behaviour=False`` → quality recipe only (presets)."""
    out = {}
    for f in dataclasses.fields(s):
        if f.name in _TRANSIENT_FIELDS:
            continue
        if not behaviour and f.name in _BEHAVIOUR_FIELDS:
            continue
        out[f.name] = getattr(s, f.name)
    return out


def settings_from_dict(data: dict, base: Optional[ConversionSettings] = None) -> ConversionSettings:
    """Apply known keys with matching types onto ``base`` (unknown keys ignored)."""
    s = dataclasses.replace(base) if base else ConversionSettings()
    defaults = ConversionSettings()
    for f in dataclasses.fields(s):
        if f.name not in data or f.name in _TRANSIENT_FIELDS:
            continue
        value = data[f.name]
        default = getattr(defaults, f.name)
        if value is None or default is None or isinstance(value, type(default)) or (
            isinstance(default, float) and isinstance(value, int)
        ):
            setattr(s, f.name, value)
    return s
