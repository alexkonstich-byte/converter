"""Material You media converter — Flet GUI."""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import warnings
from pathlib import Path

# Audio/Video controls were marked deprecated in flet 0.26 (moved to
# flet-audio / flet-video packages) but still ship with 0.27.x. Silence
# the noisy DeprecationWarning since downgrading the dependency would
# also remove them.
warnings.filterwarnings(
    "ignore",
    message=r".*(Audio|Video)\(\) is deprecated.*",
    category=DeprecationWarning,
)

import flet as ft

from converter import (
    ALL_FORMATS,
    AUDIO_CODECS,
    AUDIO_FORMATS,
    IMAGE_FORMATS,
    VIDEO_FORMATS,
    ConversionResult,
    batch_convert,
    extract_video_thumbnail,
    ffmpeg_available,
    formats_for_kind,
    media_kind,
)


# Material You seed color — generates the full tonal palette in Flet.
SEED_COLOR = ft.Colors.DEEP_PURPLE

KIND_ICON = {
    "image": ft.Icons.IMAGE_OUTLINED,
    "audio": ft.Icons.MUSIC_NOTE_OUTLINED,
    "video": ft.Icons.MOVIE_OUTLINED,
}
KIND_LABEL = {"image": "Изображение", "audio": "Аудио", "video": "Видео"}


def human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


class FileItem:
    def __init__(self, path: Path):
        self.path = path
        self.kind = media_kind(path.suffix)
        self.status: str = "pending"  # pending | running | ok | error
        self.message: str = ""


class ConverterApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.files: list[FileItem] = []
        self.target_ext: str | None = None
        self.audio_codec: str | None = None  # for video targets only
        self.out_dir: Path | None = None
        self.is_dark: bool = True
        self.running: bool = False

        self.selected_item: FileItem | None = None
        self.preview_audio: ft.Audio | None = None
        self.is_playing: bool = False
        self.thumb_dir = Path(tempfile.gettempdir()) / "converter_app_thumbs"
        self.thumb_dir.mkdir(parents=True, exist_ok=True)

        self._build_page()
        self._build_ui()

    # ----- page setup ----------------------------------------------------
    def _build_page(self) -> None:
        p = self.page
        p.title = "Универсальный конвертер"
        p.window_width = 1100
        p.window_height = 740
        p.window_min_width = 880
        p.window_min_height = 600
        p.padding = 0
        p.theme_mode = ft.ThemeMode.DARK
        p.theme = ft.Theme(
            color_scheme_seed=SEED_COLOR,
            use_material3=True,
            visual_density=ft.VisualDensity.COMFORTABLE,
        )
        p.dark_theme = ft.Theme(
            color_scheme_seed=SEED_COLOR,
            use_material3=True,
            visual_density=ft.VisualDensity.COMFORTABLE,
        )
        p.fonts = {"Inter": "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"}

        # File picker — registered once on the page.
        self.file_picker = ft.FilePicker(on_result=self._on_files_picked)
        self.dir_picker = ft.FilePicker(on_result=self._on_dir_picked)
        p.overlay.extend([self.file_picker, self.dir_picker])

    # ----- UI ------------------------------------------------------------
    def _build_ui(self) -> None:
        self.theme_btn = ft.IconButton(
            icon=ft.Icons.LIGHT_MODE_OUTLINED,
            tooltip="Сменить тему",
            on_click=self._toggle_theme,
        )

        header = ft.Container(
            padding=ft.padding.symmetric(horizontal=28, vertical=20),
            content=ft.Row(
                controls=[
                    ft.Container(
                        width=44, height=44, border_radius=14,
                        bgcolor=ft.Colors.PRIMARY_CONTAINER,
                        content=ft.Icon(ft.Icons.AUTORENEW_ROUNDED, color=ft.Colors.ON_PRIMARY_CONTAINER, size=24),
                        alignment=ft.alignment.center,
                    ),
                    ft.Column(
                        spacing=2,
                        controls=[
                            ft.Text("Универсальный конвертер", size=22, weight=ft.FontWeight.W_600),
                            ft.Text(
                                "Картинки · Аудио · Видео — всё в одном",
                                size=13, color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                        ],
                    ),
                    ft.Container(expand=True),
                    self.theme_btn,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        self.drop_card = self._build_drop_card()
        self.format_card = self._build_format_card()
        self.output_card = self._build_output_card()
        self.preview_card = self._build_preview_card()
        self.files_list = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)
        self.empty_state = ft.Container(
            alignment=ft.alignment.center,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
                controls=[
                    ft.Icon(ft.Icons.INBOX_OUTLINED, size=44, color=ft.Colors.OUTLINE),
                    ft.Text("Файлы появятся здесь", color=ft.Colors.ON_SURFACE_VARIANT),
                ],
            ),
            expand=True,
        )

        files_card = ft.Container(
            expand=True,
            padding=20,
            border_radius=24,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Column(
                spacing=12,
                expand=True,
                controls=[
                    ft.Row([
                        ft.Icon(ft.Icons.FOLDER_OPEN_OUTLINED, color=ft.Colors.PRIMARY),
                        ft.Text("Очередь", size=16, weight=ft.FontWeight.W_600),
                        ft.Container(expand=True),
                        ft.TextButton(
                            "Очистить",
                            icon=ft.Icons.DELETE_OUTLINE,
                            on_click=self._clear_files,
                        ),
                    ]),
                    ft.Container(
                        expand=True,
                        content=ft.Stack(
                            controls=[self.empty_state, self.files_list],
                            expand=True,
                        ),
                    ),
                ],
            ),
        )

        self.progress = ft.ProgressBar(value=0, visible=False, border_radius=8)
        self.status_text = ft.Text("", size=13, color=ft.Colors.ON_SURFACE_VARIANT)
        self.convert_btn = ft.FilledButton(
            "Конвертировать",
            icon=ft.Icons.PLAY_ARROW_ROUNDED,
            height=48,
            on_click=self._start_conversion,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=24),
                padding=ft.padding.symmetric(horizontal=24),
            ),
        )

        action_bar = ft.Container(
            padding=ft.padding.symmetric(horizontal=20, vertical=14),
            border_radius=20,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Row(
                controls=[
                    ft.Column(
                        expand=True,
                        spacing=4,
                        controls=[self.status_text, self.progress],
                    ),
                    self.convert_btn,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        # Layout: left column with cards, right side with file queue.
        left_col = ft.Column(
            spacing=16,
            width=380,
            controls=[self.drop_card, self.format_card, self.output_card],
        )

        right_col = ft.Column(
            spacing=16,
            expand=True,
            controls=[self.preview_card, files_card, action_bar],
        )

        body = ft.Container(
            expand=True,
            padding=ft.padding.symmetric(horizontal=24, vertical=8),
            content=ft.Row(
                spacing=16,
                expand=True,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[left_col, right_col],
            ),
        )

        self.page.add(
            ft.Column(
                expand=True,
                spacing=0,
                controls=[
                    header,
                    body,
                    ft.Container(height=16),
                ],
            )
        )

        if not ffmpeg_available():
            self._toast(
                "ffmpeg не найден — аудио и видео работать не будут. "
                "Установите ffmpeg и добавьте в PATH.",
                error=True,
            )

        self._refresh_files()

    def _build_drop_card(self) -> ft.Container:
        return ft.Container(
            padding=20,
            border_radius=24,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Column(
                spacing=12,
                controls=[
                    ft.Row([
                        ft.Icon(ft.Icons.UPLOAD_FILE_OUTLINED, color=ft.Colors.PRIMARY),
                        ft.Text("Файлы", size=16, weight=ft.FontWeight.W_600),
                    ]),
                    ft.Text(
                        "Добавьте картинки, аудио или видео — программа определит тип автоматически.",
                        size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    ft.FilledTonalButton(
                        "Выбрать файлы",
                        icon=ft.Icons.ADD_ROUNDED,
                        on_click=lambda _: self.file_picker.pick_files(allow_multiple=True),
                        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=18)),
                    ),
                ],
            ),
        )

    def _build_format_card(self) -> ft.Container:
        # Grouped dropdown: image / audio / video options.
        options: list[ft.dropdown.Option] = []
        for kind, items in (
            ("Изображения", IMAGE_FORMATS),
            ("Аудио", AUDIO_FORMATS),
            ("Видео", VIDEO_FORMATS),
        ):
            options.append(ft.dropdown.Option(key=f"__group_{kind}", text=f"— {kind} —", disabled=True))
            for ext in items:
                options.append(ft.dropdown.Option(key=ext, text=f".{ext}"))

        self.format_dropdown = ft.Dropdown(
            label="Целевой формат",
            options=options,
            on_change=self._on_format_change,
            border_radius=16,
            filled=True,
        )

        # Quick-pick chips for the most common targets.
        chips_row = ft.Row(
            wrap=True, spacing=6, run_spacing=6,
            controls=[
                self._chip("png"), self._chip("jpg"), self._chip("webp"),
                self._chip("mp3"), self._chip("wav"), self._chip("flac"),
                self._chip("mp4"), self._chip("mkv"), self._chip("gif"),
            ],
        )

        self.format_hint = ft.Text(
            "", size=12, color=ft.Colors.ON_SURFACE_VARIANT,
            visible=False,
        )

        # Audio codec dropdown — only relevant when target is a video container.
        codec_options = [
            ft.dropdown.Option(key="auto", text="Авто (по контейнеру)"),
            ft.dropdown.Option(key="aac", text="AAC"),
            ft.dropdown.Option(key="mp3", text="MP3"),
            ft.dropdown.Option(key="opus", text="Opus"),
            ft.dropdown.Option(key="vorbis", text="Vorbis"),
            ft.dropdown.Option(key="flac", text="FLAC (без потерь)"),
            ft.dropdown.Option(key="ac3", text="AC-3 (Dolby Digital)"),
            ft.dropdown.Option(key="copy", text="Без перекодирования"),
        ]
        self.codec_dropdown = ft.Dropdown(
            label="Аудиодорожка в видео",
            value="auto",
            options=codec_options,
            on_change=self._on_codec_change,
            border_radius=16,
            filled=True,
        )
        self.codec_panel = ft.Container(
            visible=False,
            content=ft.Column(
                spacing=6,
                controls=[
                    self.codec_dropdown,
                    ft.Text(
                        "Эта настройка применяется только при сохранении в видео-формат. "
                        "Не все кодеки совместимы со всеми контейнерами — если ffmpeg откажется, "
                        "вернитесь к «Авто».",
                        size=11, color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                ],
            ),
        )

        return ft.Container(
            padding=20,
            border_radius=24,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Column(
                spacing=12,
                controls=[
                    ft.Row([
                        ft.Icon(ft.Icons.SWAP_HORIZ_ROUNDED, color=ft.Colors.PRIMARY),
                        ft.Text("Во что конвертировать", size=16, weight=ft.FontWeight.W_600),
                    ]),
                    self.format_dropdown,
                    self.format_hint,
                    ft.Text("Часто используют", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                    chips_row,
                    self.codec_panel,
                ],
            ),
        )

    def _chip(self, ext: str) -> ft.Chip:
        return ft.Chip(
            label=ft.Text(f".{ext}"),
            on_select=lambda e, x=ext: self._set_target_ext(x),
            selected=False,
            show_checkmark=False,
            data=ext,
        )

    def _build_output_card(self) -> ft.Container:
        self.out_path_text = ft.Text(
            "Рядом с исходными файлами",
            size=13, color=ft.Colors.ON_SURFACE_VARIANT,
            no_wrap=False, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
        )
        return ft.Container(
            padding=20,
            border_radius=24,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Column(
                spacing=10,
                controls=[
                    ft.Row([
                        ft.Icon(ft.Icons.SAVE_ALT_OUTLINED, color=ft.Colors.PRIMARY),
                        ft.Text("Куда сохранять", size=16, weight=ft.FontWeight.W_600),
                    ]),
                    self.out_path_text,
                    ft.Row(spacing=8, controls=[
                        ft.OutlinedButton(
                            "Выбрать папку",
                            icon=ft.Icons.FOLDER_OUTLINED,
                            on_click=lambda _: self.dir_picker.get_directory_path(),
                            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=18)),
                        ),
                        ft.TextButton(
                            "Сбросить",
                            on_click=self._reset_out_dir,
                        ),
                    ]),
                ],
            ),
        )

    def _build_preview_card(self) -> ft.Container:
        # Image control reused for image previews and video thumbnails.
        self.preview_image = ft.Image(
            src="",
            fit=ft.ImageFit.CONTAIN,
            border_radius=12,
            width=480, height=240,
            visible=False,
        )

        # Audio playback widgets (audio control itself is invisible).
        self.preview_play_btn = ft.IconButton(
            icon=ft.Icons.PLAY_ARROW_ROUNDED,
            icon_size=28,
            tooltip="Воспроизвести",
            on_click=self._toggle_playback,
        )
        self.preview_position_text = ft.Text("0:00 / 0:00", size=12,
                                             color=ft.Colors.ON_SURFACE_VARIANT)
        self.preview_audio_panel = ft.Container(
            visible=False,
            padding=ft.padding.symmetric(horizontal=12, vertical=10),
            border_radius=12,
            bgcolor=ft.Colors.SURFACE,
            content=ft.Row(
                controls=[
                    ft.Container(
                        width=44, height=44, border_radius=12,
                        bgcolor=ft.Colors.SECONDARY_CONTAINER,
                        alignment=ft.alignment.center,
                        content=ft.Icon(ft.Icons.MUSIC_NOTE_OUTLINED,
                                        color=ft.Colors.ON_SECONDARY_CONTAINER),
                    ),
                    self.preview_play_btn,
                    self.preview_position_text,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=10,
            ),
        )

        # Default placeholder shown when nothing is selected.
        self.preview_placeholder = ft.Container(
            height=200,
            alignment=ft.alignment.center,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=6,
                controls=[
                    ft.Icon(ft.Icons.PREVIEW_OUTLINED, size=36, color=ft.Colors.OUTLINE),
                    ft.Text("Кликните по файлу в очереди для предпросмотра",
                            size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ],
            ),
        )

        self.preview_title = ft.Text("Предпросмотр", size=16, weight=ft.FontWeight.W_600)
        self.preview_subtitle = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                                        max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                                        visible=False)

        return ft.Container(
            padding=20,
            border_radius=24,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Column(
                spacing=10,
                controls=[
                    ft.Row([
                        ft.Icon(ft.Icons.VISIBILITY_OUTLINED, color=ft.Colors.PRIMARY),
                        self.preview_title,
                    ]),
                    self.preview_subtitle,
                    ft.Container(
                        alignment=ft.alignment.center,
                        content=ft.Stack(
                            controls=[
                                self.preview_placeholder,
                                self.preview_image,
                            ],
                        ),
                    ),
                    self.preview_audio_panel,
                ],
            ),
        )

    # ----- handlers ------------------------------------------------------
    def _toggle_theme(self, _e) -> None:
        self.is_dark = not self.is_dark
        self.page.theme_mode = ft.ThemeMode.DARK if self.is_dark else ft.ThemeMode.LIGHT
        self.theme_btn.icon = ft.Icons.LIGHT_MODE_OUTLINED if self.is_dark else ft.Icons.DARK_MODE_OUTLINED
        self.page.update()

    def _on_files_picked(self, e: ft.FilePickerResultEvent) -> None:
        if not e.files:
            return
        added = 0
        for f in e.files:
            path = Path(f.path)
            if media_kind(path.suffix) is None:
                self._toast(f"Пропустил {path.name} — формат не поддерживается.", error=True)
                continue
            if any(item.path == path for item in self.files):
                continue
            self.files.append(FileItem(path))
            added += 1
        if added:
            # Auto-preview the first newly-added file when nothing is selected.
            if not self.selected_item:
                self._select_item(self.files[-added])
            else:
                self._refresh_files()

    def _on_dir_picked(self, e: ft.FilePickerResultEvent) -> None:
        if e.path:
            self.out_dir = Path(e.path)
            self.out_path_text.value = str(self.out_dir)
            self.out_path_text.color = ft.Colors.ON_SURFACE
            self.page.update()

    def _reset_out_dir(self, _e) -> None:
        self.out_dir = None
        self.out_path_text.value = "Рядом с исходными файлами"
        self.out_path_text.color = ft.Colors.ON_SURFACE_VARIANT
        self.page.update()

    def _on_format_change(self, _e) -> None:
        ext = self.format_dropdown.value
        if ext and not ext.startswith("__group_"):
            self._set_target_ext(ext, from_dropdown=True)

    def _set_target_ext(self, ext: str, from_dropdown: bool = False) -> None:
        self.target_ext = ext
        if not from_dropdown:
            self.format_dropdown.value = ext
        # sync chips
        for chip in self._iter_chips():
            chip.selected = (chip.data == ext)
        # Codec panel only matters for video targets.
        self.codec_panel.visible = ext in VIDEO_FORMATS and ext != "gif"
        self.page.update()

    def _on_codec_change(self, _e) -> None:
        val = self.codec_dropdown.value
        self.audio_codec = None if val == "auto" else val

    def _iter_chips(self):
        # walk format_card to find chips
        for col in self.format_card.content.controls:
            if isinstance(col, ft.Row):
                for child in col.controls:
                    if isinstance(child, ft.Chip):
                        yield child

    def _clear_files(self, _e) -> None:
        if self.running:
            return
        self.files.clear()
        self._clear_preview()
        self._refresh_files()

    def _refresh_files(self) -> None:
        self.files_list.controls.clear()
        for item in self.files:
            self.files_list.controls.append(self._file_row(item))
        # toggle empty state visibility
        self.empty_state.visible = len(self.files) == 0
        self.files_list.visible = len(self.files) > 0
        self._refresh_target_allowed()
        self.page.update()

    def _allowed_target_exts(self) -> set[str]:
        """Targets compatible with every queued file.

        image -> image only. audio -> audio only. video -> video or audio
        (we strip the video stream when going to an audio target). Empty
        queue means the user hasn't constrained anything yet.
        """
        if not self.files:
            return set(ALL_FORMATS)
        per_file: list[set[str]] = []
        for item in self.files:
            if item.kind == "image":
                per_file.append(set(IMAGE_FORMATS))
            elif item.kind == "audio":
                per_file.append(set(AUDIO_FORMATS))
            elif item.kind == "video":
                per_file.append(set(VIDEO_FORMATS) | set(AUDIO_FORMATS))
            else:
                per_file.append(set())
        allowed = per_file[0]
        for s in per_file[1:]:
            allowed &= s
        return allowed

    def _refresh_target_allowed(self) -> None:
        allowed = self._allowed_target_exts()

        # Dropdown: disable forbidden options.
        for opt in self.format_dropdown.options:
            if isinstance(opt.key, str) and opt.key.startswith("__group_"):
                opt.disabled = True
                continue
            opt.disabled = opt.key not in allowed

        # Chips: disable + un-select forbidden quick picks.
        for chip in self._iter_chips():
            chip.disabled = chip.data not in allowed
            if chip.disabled:
                chip.selected = False

        # Drop a stale selection if the queue no longer permits it.
        if self.target_ext and self.target_ext not in allowed:
            self.target_ext = None
            self.format_dropdown.value = None
            self.codec_panel.visible = False
        else:
            self.codec_panel.visible = (
                self.target_ext in VIDEO_FORMATS and self.target_ext != "gif"
            )

        # Hint line under the dropdown explains why options are dimmed.
        kinds = {item.kind for item in self.files if item.kind}
        if not self.files:
            self.format_hint.visible = False
            self.format_hint.value = ""
        elif not allowed:
            self.format_hint.visible = True
            self.format_hint.value = (
                "В очереди файлы разных типов — общий целевой формат подобрать нельзя. "
                "Уберите часть файлов, чтобы продолжить."
            )
            self.format_hint.color = ft.Colors.ERROR
        elif kinds == {"image"}:
            self.format_hint.visible = True
            self.format_hint.value = "В очереди только изображения — доступны форматы картинок."
            self.format_hint.color = ft.Colors.ON_SURFACE_VARIANT
        elif kinds == {"audio"}:
            self.format_hint.visible = True
            self.format_hint.value = "В очереди только аудио — доступны аудиоформаты."
            self.format_hint.color = ft.Colors.ON_SURFACE_VARIANT
        elif kinds == {"video"}:
            self.format_hint.visible = True
            self.format_hint.value = "В очереди только видео — можно сохранить как видео или извлечь звук."
            self.format_hint.color = ft.Colors.ON_SURFACE_VARIANT
        elif kinds == {"audio", "video"}:
            self.format_hint.visible = True
            self.format_hint.value = "Микс из аудио и видео — доступны только аудиоформаты."
            self.format_hint.color = ft.Colors.ON_SURFACE_VARIANT
        else:
            self.format_hint.visible = False
            self.format_hint.value = ""

    def _file_row(self, item: FileItem) -> ft.Control:
        try:
            size = human_size(item.path.stat().st_size)
        except OSError:
            size = "?"
        kind = item.kind or "file"
        leading = ft.Container(
            width=44, height=44, border_radius=12,
            bgcolor=ft.Colors.SECONDARY_CONTAINER,
            alignment=ft.alignment.center,
            content=ft.Icon(KIND_ICON.get(kind, ft.Icons.INSERT_DRIVE_FILE_OUTLINED),
                            color=ft.Colors.ON_SECONDARY_CONTAINER),
        )

        if item.status == "running":
            trailing = ft.ProgressRing(width=20, height=20, stroke_width=2)
        elif item.status == "ok":
            trailing = ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.TERTIARY)
        elif item.status == "error":
            trailing = ft.Icon(ft.Icons.ERROR, color=ft.Colors.ERROR, tooltip=item.message)
        else:
            trailing = ft.IconButton(
                icon=ft.Icons.CLOSE_ROUNDED,
                icon_size=18,
                tooltip="Убрать",
                on_click=lambda _e, p=item.path: self._remove_file(p),
            )

        subtitle_text = f"{KIND_LABEL.get(kind, 'Файл')} · {item.path.suffix.lstrip('.').upper()} · {size}"
        if item.status == "error" and item.message:
            subtitle_text = item.message

        is_selected = self.selected_item is not None and self.selected_item.path == item.path
        return ft.Container(
            padding=ft.padding.symmetric(horizontal=12, vertical=8),
            border_radius=16,
            bgcolor=ft.Colors.PRIMARY_CONTAINER if is_selected else ft.Colors.SURFACE,
            on_click=lambda _e, it=item: self._select_item(it),
            tooltip="Открыть в предпросмотре",
            content=ft.Row(
                controls=[
                    leading,
                    ft.Column(
                        spacing=2, expand=True,
                        controls=[
                            ft.Text(item.path.name, weight=ft.FontWeight.W_500,
                                    overflow=ft.TextOverflow.ELLIPSIS, max_lines=1),
                            ft.Text(subtitle_text, size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                                    overflow=ft.TextOverflow.ELLIPSIS, max_lines=1),
                        ],
                    ),
                    trailing,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
            ),
        )

    def _remove_file(self, path: Path) -> None:
        self.files = [f for f in self.files if f.path != path]
        if self.selected_item and self.selected_item.path == path:
            self._clear_preview()
        self._refresh_files()

    # ----- preview -------------------------------------------------------
    def _select_item(self, item: FileItem) -> None:
        # Toggle off if same item is clicked twice.
        if self.selected_item and self.selected_item.path == item.path:
            self._clear_preview()
            self._refresh_files()
            return
        self.selected_item = item
        self._render_preview(item)
        self._refresh_files()

    def _clear_preview(self) -> None:
        if self.preview_audio:
            try:
                self.preview_audio.pause()
            except Exception:
                pass
            try:
                self.page.overlay.remove(self.preview_audio)
            except (ValueError, Exception):
                pass
            self.preview_audio = None
        self.is_playing = False
        self.preview_play_btn.icon = ft.Icons.PLAY_ARROW_ROUNDED
        self.selected_item = None
        self.preview_image.src = ""
        self.preview_image.visible = False
        self.preview_audio_panel.visible = False
        self.preview_placeholder.visible = True
        self.preview_subtitle.value = ""
        self.preview_subtitle.visible = False

    def _render_preview(self, item: FileItem) -> None:
        try:
            size = human_size(item.path.stat().st_size)
        except OSError:
            size = "?"
        self.preview_subtitle.value = (
            f"{item.path.name} · {KIND_LABEL.get(item.kind or '', 'Файл')} · {size}"
        )
        self.preview_subtitle.visible = True

        # Detach any prior audio.
        if self.preview_audio:
            try:
                self.preview_audio.pause()
                self.page.overlay.remove(self.preview_audio)
            except Exception:
                pass
            self.preview_audio = None
        self.is_playing = False
        self.preview_play_btn.icon = ft.Icons.PLAY_ARROW_ROUNDED

        if item.kind == "image":
            self.preview_image.src = str(item.path)
            self.preview_image.visible = True
            self.preview_audio_panel.visible = False
            self.preview_placeholder.visible = False
        elif item.kind == "audio":
            self.preview_image.visible = False
            self.preview_placeholder.visible = False
            self.preview_audio_panel.visible = True
            self.preview_position_text.value = "0:00 / 0:00"
            self._attach_audio(item.path)
        elif item.kind == "video":
            # Show a thumbnail (extracted via ffmpeg) and let the play button
            # play the video's audio track as a quick listen.
            self.preview_audio_panel.visible = True
            self.preview_position_text.value = "0:00 / 0:00"
            self.preview_placeholder.visible = False
            self.preview_image.src = ""
            self.preview_image.visible = False
            self._attach_audio(item.path)
            threading.Thread(
                target=self._generate_and_show_thumbnail, args=(item,), daemon=True,
            ).start()
        else:
            self._clear_preview()

        self.page.update()

    def _attach_audio(self, src: Path) -> None:
        audio = ft.Audio(
            src=str(src),
            autoplay=False,
            on_duration_changed=self._on_audio_duration,
            on_position_changed=self._on_audio_position,
            on_state_changed=self._on_audio_state,
        )
        self.page.overlay.append(audio)
        self.preview_audio = audio
        self._duration_ms = 0
        self.page.update()

    def _on_audio_duration(self, e) -> None:
        try:
            self._duration_ms = int(e.duration)
        except (AttributeError, ValueError, TypeError):
            self._duration_ms = 0
        self._update_position_label(0)

    def _on_audio_position(self, e) -> None:
        try:
            pos = int(e.position)
        except (AttributeError, ValueError, TypeError):
            pos = 0
        self._update_position_label(pos)

    def _on_audio_state(self, e) -> None:
        # When playback completes, reset the play icon.
        state = getattr(e, "data", "") or ""
        if state.lower() in ("completed", "stopped"):
            self.is_playing = False
            self.preview_play_btn.icon = ft.Icons.PLAY_ARROW_ROUNDED
            self.page.update()

    def _update_position_label(self, pos_ms: int) -> None:
        def fmt(ms: int) -> str:
            s = max(0, ms // 1000)
            return f"{s // 60}:{s % 60:02d}"
        self.preview_position_text.value = f"{fmt(pos_ms)} / {fmt(self._duration_ms)}"
        try:
            self.page.update()
        except Exception:
            pass

    def _toggle_playback(self, _e) -> None:
        if not self.preview_audio:
            return
        try:
            if self.is_playing:
                self.preview_audio.pause()
                self.is_playing = False
                self.preview_play_btn.icon = ft.Icons.PLAY_ARROW_ROUNDED
            else:
                # Audio.play() restarts; resume() continues. Use play on first
                # press, resume afterwards.
                self.preview_audio.play()
                self.is_playing = True
                self.preview_play_btn.icon = ft.Icons.PAUSE_ROUNDED
        except Exception as exc:
            self._toast(f"Не удалось воспроизвести: {exc}", error=True)
        self.page.update()

    def _thumbnail_path(self, src: Path) -> Path:
        try:
            mtime = int(src.stat().st_mtime)
        except OSError:
            mtime = 0
        key = hashlib.md5(f"{src}|{mtime}".encode("utf-8")).hexdigest()[:16]
        return self.thumb_dir / f"{key}.jpg"

    def _generate_and_show_thumbnail(self, item: FileItem) -> None:
        thumb = self._thumbnail_path(item.path)
        if not thumb.exists():
            try:
                extract_video_thumbnail(item.path, thumb)
            except Exception:
                # ffmpeg missing or extraction failed — leave the placeholder.
                self._safe_run(lambda: self._show_thumb_fallback("Не удалось получить превью видео."))
                return
        # Only update if the user is still looking at this file.
        if self.selected_item and self.selected_item.path == item.path:
            self._safe_run(lambda: self._set_thumb(thumb))

    def _set_thumb(self, thumb: Path) -> None:
        self.preview_image.src = str(thumb)
        self.preview_image.visible = True
        self.preview_placeholder.visible = False
        self.page.update()

    def _show_thumb_fallback(self, message: str) -> None:
        self.preview_image.visible = False
        self.preview_placeholder.visible = True
        # repurpose the placeholder text
        col = self.preview_placeholder.content
        if isinstance(col, ft.Column) and len(col.controls) >= 2:
            col.controls[1].value = message
        self.page.update()

    def _safe_run(self, fn) -> None:
        try:
            fn()
        except Exception:
            pass

    def _toast(self, message: str, error: bool = False) -> None:
        self.page.open(
            ft.SnackBar(
                content=ft.Text(message),
                bgcolor=ft.Colors.ERROR_CONTAINER if error else ft.Colors.INVERSE_SURFACE,
            )
        )

    # ----- conversion ----------------------------------------------------
    def _start_conversion(self, _e) -> None:
        if self.running:
            return
        if not self.files:
            self._toast("Сначала добавьте файлы.", error=True)
            return
        if not self.target_ext:
            self._toast("Выберите целевой формат.", error=True)
            return

        # Defensive: UI already disables incompatible targets, but double-check
        # in case state drifted between selection and click.
        allowed = self._allowed_target_exts()
        if self.target_ext not in allowed:
            self._toast(
                "Этот формат несовместим с файлами в очереди.",
                error=True,
            )
            return

        self.running = True
        self.convert_btn.disabled = True
        self.progress.visible = True
        self.progress.value = 0
        self.status_text.value = "Готовлюсь…"
        for item in self.files:
            item.status = "pending"
            item.message = ""
        self._refresh_files()

        thread = threading.Thread(target=self._run_conversion, daemon=True)
        thread.start()

    def _run_conversion(self) -> None:
        total = len(self.files)
        ok_count = 0
        for idx, item in enumerate(self.files, start=1):
            item.status = "running"
            self.status_text.value = f"[{idx}/{total}] {item.path.name}"
            self.progress.value = (idx - 1) / total
            self._safe_update()

            out_dir = self.out_dir or item.path.parent
            dst = out_dir / f"{item.path.stem}.{self.target_ext}"
            if dst.resolve() == item.path.resolve():
                dst = out_dir / f"{item.path.stem}_converted.{self.target_ext}"

            try:
                from converter import convert
                convert(item.path, dst, audio_codec=self.audio_codec)
                item.status = "ok"
                item.message = str(dst)
                ok_count += 1
            except Exception as e:
                item.status = "error"
                item.message = str(e)
            self._safe_update()

        self.progress.value = 1.0
        self.status_text.value = f"Готово: {ok_count} из {total}"
        self.running = False
        self.convert_btn.disabled = False
        self._safe_update()
        if ok_count == total:
            self._toast(f"Готово — {ok_count} файлов конвертировано.")
        elif ok_count == 0:
            self._toast("Не удалось конвертировать ни одного файла.", error=True)
        else:
            self._toast(f"Готово {ok_count} из {total}. Остальное смотрите в списке.", error=True)

    def _safe_update(self) -> None:
        try:
            self._refresh_files()
        except Exception:
            pass


def main(page: ft.Page) -> None:
    ConverterApp(page)


if __name__ == "__main__":
    ft.app(target=main)
