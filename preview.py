"""Preview pane: image thumbnail, inline audio player, video player/thumbnail.

Images are always decoded with Pillow into a downscaled JPEG/PNG, so even
formats Flutter can't show itself (TGA, PCX, TIFF, AVIF…) get a preview and
huge photos don't go over the wire at full size.
"""
from __future__ import annotations

import hashlib
import io
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import flet as ft
from PIL import Image

from converter import extract_video_thumbnail, humanize_error
from ui_kit import KIND_LABEL, SHAPE_LARGE, SHAPE_MEDIUM, fmt_time, human_size, label

try:
    import flet_audio as fta
except ImportError:  # optional dependency
    fta = None

try:
    import flet_video as ftv
except ImportError:  # optional dependency (needs libmpv on Linux)
    ftv = None

if TYPE_CHECKING:
    from app import ConverterApp, FileItem

PREVIEW_HEIGHT = 240
THUMB_DIR = Path(tempfile.gettempdir()) / "converter_app_thumbs"


def image_preview_bytes(path: Path, max_side: int = 960) -> bytes:
    with Image.open(path) as img:
        img.seek(0)
        img.thumbnail((max_side, max_side))
        has_alpha = img.mode in ("RGBA", "LA", "P", "PA")
        img = img.convert("RGBA" if has_alpha else "RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG" if has_alpha else "JPEG", quality=85)
        return buf.getvalue()


def video_thumbnail_bytes(path: Path) -> bytes:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    try:
        mtime = int(path.stat().st_mtime)
    except OSError:
        mtime = 0
    key = hashlib.md5(f"{path}|{mtime}".encode("utf-8")).hexdigest()[:16]
    thumb = THUMB_DIR / f"{key}.jpg"
    if not thumb.exists():
        extract_video_thumbnail(path, thumb)
    return thumb.read_bytes()


def cleanup_thumbnails() -> None:
    """Drop cached video thumbnails from previous sessions."""
    if THUMB_DIR.exists():
        for f in THUMB_DIR.glob("*.jpg"):
            try:
                f.unlink()
            except OSError:
                pass


class PreviewPane:
    def __init__(self, app: "ConverterApp"):
        self.app = app
        self.item: Optional["FileItem"] = None
        self._audio = None
        self._duration_ms = 0
        self._playing = False
        self._seeking = False
        self._token = 0  # bumps on every selection; stale background jobs bail out

        self.title = ft.Text("Предпросмотр", theme_style=ft.TextThemeStyle.TITLE_MEDIUM,
                             max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self.subtitle = label("Выберите файл в очереди", max_lines=1,
                              overflow=ft.TextOverflow.ELLIPSIS)

        self.placeholder = ft.Container(
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=8,
                controls=[
                    ft.Icon(ft.Icons.PHOTO_LIBRARY_OUTLINED, size=40, color=ft.Colors.OUTLINE),
                    label("Здесь появится картинка, видео или плеер"),
                ],
            ),
        )
        self.loading = ft.Container(
            visible=False, alignment=ft.Alignment.CENTER,
            content=ft.ProgressRing(year_2023=False),
        )
        self.image = ft.Image(src=b"", fit=ft.BoxFit.CONTAIN, visible=False,
                              border_radius=SHAPE_MEDIUM, gapless_playback=True)
        self.video_slot = ft.Container(visible=False, border_radius=SHAPE_MEDIUM,
                                       clip_behavior=ft.ClipBehavior.ANTI_ALIAS)

        # Inline audio player (M3: tonal play button + slider + timestamps).
        self.play_btn = ft.FilledTonalIconButton(
            icon=ft.Icons.PLAY_ARROW_ROUNDED, tooltip="Воспроизвести",
            on_click=self._toggle_play,
        )
        self.seek = ft.Slider(min=0, max=1, value=0, expand=True, year_2023=False,
                              on_change_start=self._seek_start,
                              on_change=self._seek_change,
                              on_change_end=self._seek_end)
        self.pos_text = label("0:00 / 0:00", style=ft.TextThemeStyle.LABEL_MEDIUM)
        self.audio_panel = ft.Container(
            visible=False,
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=16,
                controls=[
                    ft.Container(
                        width=88, height=88, border_radius=SHAPE_LARGE * 2,
                        bgcolor=ft.Colors.TERTIARY_CONTAINER,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(ft.Icons.MUSIC_NOTE_ROUNDED, size=44,
                                        color=ft.Colors.ON_TERTIARY_CONTAINER),
                    ),
                    ft.Row([self.play_btn, self.seek, self.pos_text],
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ],
            ),
        )

        self.track_dropdown = ft.Dropdown(
            label="Звуковая дорожка", dense=True, visible=False,
            border_radius=8, on_select=self._on_track, expand=True,
            leading_icon=ft.Icons.AUDIOTRACK_OUTLINED,
        )
        self.sub_dropdown = ft.Dropdown(
            label="Субтитры", dense=True, visible=False,
            border_radius=8, on_select=self._on_sub_track, expand=True,
            leading_icon=ft.Icons.SUBTITLES_OUTLINED,
        )
        self.open_btn = ft.TextButton(
            "Открыть", icon=ft.Icons.OPEN_IN_NEW_ROUNDED, visible=False,
            on_click=lambda _e: self.item and self.app.open_path(self.item.path),
        )

        # Header buttons; the app appends its own (e.g. "test render").
        self.actions = ft.Row([self.open_btn], spacing=0, tight=True)

        self.control = ft.Container(
            padding=20,
            border_radius=28,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            content=ft.Column(
                spacing=12,
                controls=[
                    ft.Row(
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Icon(ft.Icons.VISIBILITY_OUTLINED, color=ft.Colors.PRIMARY),
                            ft.Column([self.title, self.subtitle], spacing=0, expand=True),
                            self.actions,
                        ],
                    ),
                    ft.Container(
                        height=PREVIEW_HEIGHT,
                        border_radius=SHAPE_LARGE,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                        padding=8,
                        content=ft.Stack(
                            expand=True,
                            controls=[
                                ft.Container(self.placeholder, expand=True),
                                ft.Container(self.image, expand=True, alignment=ft.Alignment.CENTER),
                                ft.Container(self.video_slot, expand=True),
                                ft.Container(self.audio_panel, expand=True, padding=12),
                                ft.Container(self.loading, expand=True),
                            ],
                        ),
                    ),
                    ft.Row([self.track_dropdown, self.sub_dropdown]),
                ],
            ),
        )

    # ---------------------------------------------------------------- API
    def show(self, item: Optional["FileItem"]) -> None:
        self._token += 1
        self._stop_media()
        self.item = item
        for c in (self.image, self.video_slot, self.audio_panel, self.loading):
            c.visible = False
        self.track_dropdown.visible = False
        self.sub_dropdown.visible = False
        self.open_btn.visible = False
        self.placeholder.visible = item is None

        if item is None:
            self.title.value = "Предпросмотр"
            self.subtitle.value = "Выберите файл в очереди"
            return

        self.title.value = item.path.name
        self.refresh_subtitle()
        self.open_btn.visible = True

        if item.kind == "image":
            self._load_async(lambda: image_preview_bytes(item.path), self._show_image_bytes)
        elif item.kind == "audio":
            self._mount_audio(item.path)
        elif item.kind == "video":
            self._mount_video(item.path)
            self.refresh_tracks()

    def refresh_subtitle(self) -> None:
        item = self.item
        if not item:
            return
        try:
            size = human_size(item.path.stat().st_size)
        except OSError:
            size = "?"
        bits = [KIND_LABEL.get(item.kind or "", "Файл"), item.path.suffix.lstrip(".").upper(), size]
        if item.duration_s:
            bits.append(fmt_time(item.duration_s))
            if item.kind == "audio" and self._duration_ms <= 0:
                self._duration_ms = int(item.duration_s * 1000)
                self._update_pos(int(self.seek.value * self._duration_ms))
        self.subtitle.value = " · ".join(bits)

    def refresh_tracks(self) -> None:
        """Fill the per-file audio-track picker once the probe has finished."""
        item = self.item
        if not item or item.kind != "video":
            self.track_dropdown.visible = self.sub_dropdown.visible = False
            return
        subs = [t for t in item.streams if t.kind == "subtitle"]
        self.sub_dropdown.visible = bool(subs)
        if subs:
            self.sub_dropdown.options = [
                ft.DropdownOption(key=str(t.index), text=f"#{n} · {t.label}")
                for n, t in enumerate(subs, start=1)
            ]
            chosen = item.subtitle_track if any(t.index == item.subtitle_track for t in subs) \
                else subs[0].index
            self.sub_dropdown.value = str(chosen)
        tracks = [t for t in item.streams if t.kind == "audio"]
        if len(tracks) < 2:
            self.track_dropdown.visible = False
            return
        self.track_dropdown.options = [ft.DropdownOption(key="default", text="Первая (по умолчанию)")] + [
            ft.DropdownOption(key=str(t.index), text=f"#{n} · {t.label}")
            for n, t in enumerate(tracks, start=1)
        ]
        self.track_dropdown.value = "default" if item.audio_track is None else str(item.audio_track)
        self.track_dropdown.visible = True

    def stop(self) -> None:
        self._stop_media()

    # ------------------------------------------------------------ helpers
    def _load_async(self, producer, consumer) -> None:
        token = self._token
        self.loading.visible = True

        def work():
            try:
                data = producer()
                err = None
            except Exception as e:  # noqa: BLE001 — shown to the user
                data, err = None, e
            if token != self._token:
                return  # user already picked another file
            self.loading.visible = False
            if err is not None:
                self.placeholder.visible = True
                self.placeholder.content.controls[1].value = (
                    f"Не удалось показать: {humanize_error(str(err))}")
            else:
                consumer(data)
            self.app.page.update()

        threading.Thread(target=work, daemon=True).start()

    def _show_image_bytes(self, data: bytes) -> None:
        self.image.src = data
        self.image.visible = True
        self.placeholder.visible = False

    def _on_sub_track(self, e) -> None:
        if self.item and e.control.value:
            self.item.subtitle_track = int(e.control.value)

    def _on_track(self, e) -> None:
        if self.item:
            v = e.control.value
            self.item.audio_track = None if v in (None, "default") else int(v)

    # -------------------------------------------------------------- video
    def _mount_video(self, path: Path) -> None:
        if ftv is not None:
            try:
                video = ftv.Video(
                    playlist=[ftv.VideoMedia(path.resolve().as_uri())],
                    autoplay=False,
                    fit=ft.BoxFit.CONTAIN,
                    fill_color=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                    on_error=lambda _e: self._video_fallback(path),
                    expand=True,
                )
                self.video_slot.content = video
                self.video_slot.visible = True
                self.placeholder.visible = False
                return
            except Exception:
                pass
        self._video_fallback(path)

    def _video_fallback(self, path: Path) -> None:
        self.video_slot.visible = False
        self.video_slot.content = None
        self._load_async(lambda: video_thumbnail_bytes(path), self._show_image_bytes)

    # -------------------------------------------------------------- audio
    def _mount_audio(self, path: Path) -> None:
        self.audio_panel.visible = True
        self.placeholder.visible = False
        self._duration_ms = int((self.item.duration_s or 0) * 1000) if self.item else 0
        self._set_play_icon(False)
        self.seek.value = 0
        self._update_pos(0)
        if fta is None:
            self.play_btn.disabled = True
            self.play_btn.tooltip = "Установите flet-audio для прослушивания"
            return
        self.play_btn.disabled = False
        try:
            src = path.resolve().as_uri()
        except ValueError:
            src = str(path)
        self._audio = fta.Audio(
            src=src,
            on_duration_change=self._on_duration,
            on_position_change=self._on_position,
            on_state_change=self._on_state,
        )

    def _stop_media(self) -> None:
        audio, self._audio = self._audio, None
        self._playing = False
        if audio is not None:
            async def _release():
                try:
                    await audio.release()
                except Exception:
                    pass
            self.app.page.run_task(_release)
        video = self.video_slot.content
        self.video_slot.content = None
        if video is not None and ftv is not None:
            async def _pause():
                try:
                    await video.pause()
                except Exception:
                    pass
            self.app.page.run_task(_pause)

    async def _toggle_play(self, _e) -> None:
        if not self._audio:
            return
        try:
            if self._playing:
                await self._audio.pause()
            else:
                pos = int(self.seek.value * self._duration_ms) if self._duration_ms else 0
                await self._audio.play(pos)
            self._set_play_icon(not self._playing)
        except Exception as exc:
            self.app.toast(f"Не удалось воспроизвести: {exc}", error=True)

    def _set_play_icon(self, playing: bool) -> None:
        self._playing = playing
        self.play_btn.icon = ft.Icons.PAUSE_ROUNDED if playing else ft.Icons.PLAY_ARROW_ROUNDED
        self.play_btn.tooltip = "Пауза" if playing else "Воспроизвести"

    def _on_duration(self, e) -> None:
        d = getattr(e, "duration", None)
        ms = getattr(d, "in_milliseconds", None) or getattr(d, "milliseconds", None) or d
        try:
            self._duration_ms = int(ms)
        except (TypeError, ValueError):
            return
        self._update_pos(int(self.seek.value * self._duration_ms))
        self.pos_text.update()

    def _on_position(self, e) -> None:
        if self._seeking or self._duration_ms <= 0:
            return
        pos = int(getattr(e, "position", 0) or 0)
        self.seek.value = max(0.0, min(1.0, pos / self._duration_ms))
        self._update_pos(pos)
        self.seek.update()
        self.pos_text.update()

    def _on_state(self, e) -> None:
        state = getattr(e, "state", None)
        name = getattr(state, "value", str(state)).lower()
        if name in ("completed", "stopped"):
            self.seek.value = 0
            self._update_pos(0)
        if name in ("completed", "stopped", "paused"):
            self._set_play_icon(False)
        elif name == "playing":
            self._set_play_icon(True)
        self.play_btn.update()
        self.seek.update()

    def _update_pos(self, pos_ms: int) -> None:
        self.pos_text.value = f"{fmt_time(pos_ms / 1000)} / {fmt_time(self._duration_ms / 1000)}"

    def _seek_start(self, _e) -> None:
        self._seeking = True

    def _seek_change(self, _e) -> None:
        if self._duration_ms > 0:
            self._update_pos(int(self.seek.value * self._duration_ms))

    async def _seek_end(self, _e) -> None:
        self._seeking = False
        if self._audio and self._duration_ms > 0 and self._playing:
            try:
                await self._audio.seek(int(self.seek.value * self._duration_ms))
            except Exception:
                pass
