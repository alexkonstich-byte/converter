"""Material You media converter — Flet GUI."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import warnings
import webbrowser
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
    PRESETS,
    VIDEO_FORMATS,
    ConversionResult,
    ConversionSettings,
    batch_convert,
    convert,
    default_settings,
    estimate_output_size_bytes,
    extract_video_thumbnail,
    ffmpeg_available,
    formats_for_kind,
    get_preset,
    media_kind,
    probe_duration,
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
        self.dst: Path | None = None
        self.duration_s: float = 0.0
        self.estimated_size: int | None = None


class ConverterApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.files: list[FileItem] = []
        self.target_ext: str | None = None
        self.settings: ConversionSettings = default_settings()
        self.out_dir: Path | None = None
        self.is_dark: bool = True
        self.running: bool = False

        # Conversion progress / ETA bookkeeping.
        self._conv_start_ts: float | None = None
        self._per_item_progress: float = 0.0
        self._total_duration_s: float = 0.0
        self._completed_duration_s: float = 0.0
        self._current_duration_s: float = 0.0
        self._eta_thread: threading.Thread | None = None
        self._eta_stop = threading.Event()

        # Log console
        self.log_lines: list[str] = []
        self._log_lock = threading.Lock()
        self._log_textarea: ft.TextField | None = None
        self._notify_enabled: bool = True

        # Preview state
        self.selected_item: FileItem | None = None
        self.preview_audio: ft.Audio | None = None
        self.preview_video = None
        self.is_playing: bool = False
        self._has_started: bool = False
        self._duration_ms: int = 0
        self._seeking: bool = False
        self.last_output_folder: Path | None = None
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
        self.preset_card = self._build_preset_card()
        self.format_card = self._build_format_card()
        self.settings_card = self._build_settings_card()
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
        self.eta_text = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.log_btn = ft.IconButton(
            icon=ft.Icons.TERMINAL_ROUNDED,
            tooltip="Показать журнал ffmpeg",
            on_click=self._open_log_dialog,
        )
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

        # "Open output folder" — visible only after at least one successful
        # conversion, jumps straight to where files were saved.
        self.open_output_btn = ft.OutlinedButton(
            "Открыть папку",
            icon=ft.Icons.FOLDER_OPEN_OUTLINED,
            visible=False,
            on_click=self._open_last_output_folder,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=24)),
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
                        controls=[self.status_text, self.eta_text, self.progress],
                    ),
                    self.log_btn,
                    self.open_output_btn,
                    self.convert_btn,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
        )

        # Layout: left column with cards, right side with file queue.
        # Left column scrolls — settings can grow tall.
        left_col = ft.Column(
            spacing=16,
            width=380,
            scroll=ft.ScrollMode.AUTO,
            controls=[
                self.drop_card,
                self.preset_card,
                self.format_card,
                self.settings_card,
                self.output_card,
            ],
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

        self._refresh_files()

        # First-run: if ffmpeg isn't available, prompt the user with an
        # install dialog (winget option + manual instructions link).
        if not ffmpeg_available():
            # Defer slightly so the page finishes mounting before opening
            # the modal — avoids flicker.
            threading.Timer(0.4, self._show_ffmpeg_install_dialog).start()

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

    # ----- Smart presets ------------------------------------------------
    def _build_preset_card(self) -> ft.Container:
        options = [ft.dropdown.Option(key="__none", text="— не выбрано —")]
        for p in PRESETS:
            options.append(ft.dropdown.Option(key=p.key, text=p.label))

        self.preset_dropdown = ft.Dropdown(
            label="Готовый пресет",
            value="__none",
            options=options,
            on_change=self._on_preset_change,
            border_radius=16,
            filled=True,
        )
        self.preset_description = ft.Text(
            "", size=12, color=ft.Colors.ON_SURFACE_VARIANT, visible=False,
        )

        return ft.Container(
            padding=20,
            border_radius=24,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Column(
                spacing=10,
                controls=[
                    ft.Row([
                        ft.Icon(ft.Icons.AUTO_AWESOME_OUTLINED, color=ft.Colors.PRIMARY),
                        ft.Text("Smart Presets", size=16, weight=ft.FontWeight.W_600),
                    ]),
                    ft.Text(
                        "Готовые рецепты под платформы — автоматически выставляют все параметры.",
                        size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    self.preset_dropdown,
                    self.preset_description,
                ],
            ),
        )

    # ----- Settings card ------------------------------------------------
    def _help(self, message: str) -> ft.IconButton:
        """Click-to-open help button — opens a dialog with the explanation."""
        return ft.IconButton(
            icon=ft.Icons.HELP_OUTLINE_ROUNDED,
            icon_size=16,
            tooltip="Что это?",
            on_click=lambda _e, m=message: self._show_help_dialog(m),
            style=ft.ButtonStyle(
                color={"": ft.Colors.OUTLINE, "hovered": ft.Colors.PRIMARY},
                padding=ft.padding.all(2),
            ),
        )

    def _show_help_dialog(self, message: str) -> None:
        dlg = ft.AlertDialog(
            modal=False,
            title=ft.Row([
                ft.Icon(ft.Icons.HELP_OUTLINE_ROUNDED, color=ft.Colors.PRIMARY),
                ft.Text("Подсказка"),
            ]),
            content=ft.Container(
                width=440,
                content=ft.Text(message, size=13, selectable=True),
            ),
            actions=[
                ft.TextButton(
                    "Понятно",
                    on_click=lambda _e: self.page.close(dlg),
                ),
            ],
        )
        self.page.open(dlg)

    def _row_with_help(self, control: ft.Control, help_text: str) -> ft.Row:
        return ft.Row(
            spacing=4,
            controls=[ft.Container(expand=True, content=control), self._help(help_text)],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _build_settings_card(self) -> ft.Container:
        self.image_settings = self._build_image_settings()
        self.audio_settings = self._build_audio_settings()
        self.video_settings = self._build_video_settings()

        return ft.Container(
            padding=20,
            border_radius=24,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Column(
                spacing=14,
                controls=[
                    ft.Row([
                        ft.Icon(ft.Icons.TUNE_ROUNDED, color=ft.Colors.PRIMARY),
                        ft.Text("Параметры качества", size=16, weight=ft.FontWeight.W_600),
                    ]),
                    self.image_settings,
                    self.audio_settings,
                    self.video_settings,
                ],
            ),
        )

    def _section_header(self, title: str, icon) -> ft.Row:
        return ft.Row(
            spacing=6,
            controls=[
                ft.Icon(icon, size=16, color=ft.Colors.SECONDARY),
                ft.Text(title, size=14, weight=ft.FontWeight.W_500),
            ],
        )

    def _build_image_settings(self) -> ft.Container:
        self.image_quality_slider = ft.Slider(
            min=1, max=100, value=self.settings.image_quality, divisions=99,
            label="{value}%",
            on_change=self._on_image_quality_change,
        )
        self.image_quality_value = ft.Text(
            f"{self.settings.image_quality}%",
            size=12, color=ft.Colors.ON_SURFACE_VARIANT,
        )

        self.image_strip_exif_switch = ft.Switch(
            value=self.settings.image_strip_exif,
            on_change=self._on_strip_exif_change,
            scale=0.8,
        )

        self.image_resize_w = ft.TextField(
            label="Ширина", hint_text="px", width=100,
            input_filter=ft.NumbersOnlyInputFilter(),
            on_change=self._on_resize_change,
            border_radius=12, filled=True,
        )
        self.image_resize_h = ft.TextField(
            label="Высота", hint_text="px", width=100,
            input_filter=ft.NumbersOnlyInputFilter(),
            on_change=self._on_resize_change,
            border_radius=12, filled=True,
        )
        self.image_keep_aspect_switch = ft.Switch(
            value=True,
            on_change=self._on_keep_aspect_change,
            scale=0.8,
        )

        return ft.Container(
            visible=False,
            padding=ft.padding.all(12),
            border_radius=14,
            bgcolor=ft.Colors.SURFACE,
            content=ft.Column(
                spacing=8,
                controls=[
                    self._section_header("Изображения", ft.Icons.IMAGE_OUTLINED),
                    self._row_with_help(
                        ft.Row([
                            ft.Text("Качество", size=13, width=88),
                            ft.Container(expand=True, content=self.image_quality_slider),
                            self.image_quality_value,
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        "Качество для JPEG/WebP. Шкала 1–100. На JPEG 80–90 — золотая середина: "
                        "глаз почти не видит разницы с 100, а файл вдвое легче. Для PNG настройка "
                        "не используется (это lossless).",
                    ),
                    self._row_with_help(
                        ft.Row([
                            ft.Text("Удалить EXIF", size=13, expand=True),
                            self.image_strip_exif_switch,
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        "EXIF — невидимые метаданные камеры: модель, GPS-координаты съёмки, "
                        "дата, настройки. Удаление защищает приватность при публикации.",
                    ),
                    self._row_with_help(
                        ft.Row([
                            ft.Text("Размер", size=13, width=88),
                            self.image_resize_w,
                            ft.Text("×", size=14),
                            self.image_resize_h,
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
                        "Изменение размеров. Можно указать только ширину — высота посчитается "
                        "автоматически с сохранением пропорций. Оставьте пустыми, чтобы не "
                        "трогать оригинал.",
                    ),
                    self._row_with_help(
                        ft.Row([
                            ft.Text("Сохранять пропорции", size=13, expand=True),
                            self.image_keep_aspect_switch,
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        "Если включено, картинка не растягивается. Заполняется только одна "
                        "сторона — вторая считается сама.",
                    ),
                ],
            ),
        )

    def _build_audio_settings(self) -> ft.Container:
        self.audio_bitrate_dropdown = ft.Dropdown(
            label="Битрейт", value="auto", border_radius=12, filled=True,
            options=[
                ft.dropdown.Option("auto", "Авто"),
                ft.dropdown.Option("96",  "96 kbps"),
                ft.dropdown.Option("128", "128 kbps"),
                ft.dropdown.Option("192", "192 kbps"),
                ft.dropdown.Option("256", "256 kbps"),
                ft.dropdown.Option("320", "320 kbps"),
            ],
            on_change=self._on_audio_bitrate_change,
        )
        self.audio_sample_dropdown = ft.Dropdown(
            label="Частота", value="auto", border_radius=12, filled=True,
            options=[
                ft.dropdown.Option("auto",  "Авто"),
                ft.dropdown.Option("44100", "44.1 kHz · CD"),
                ft.dropdown.Option("48000", "48 kHz · видео"),
                ft.dropdown.Option("96000", "96 kHz · студия"),
            ],
            on_change=self._on_sample_rate_change,
        )
        self.audio_channels_dropdown = ft.Dropdown(
            label="Каналы", value="auto", border_radius=12, filled=True,
            options=[
                ft.dropdown.Option("auto", "Авто"),
                ft.dropdown.Option("1",    "Моно"),
                ft.dropdown.Option("2",    "Стерео"),
                ft.dropdown.Option("6",    "5.1"),
            ],
            on_change=self._on_channels_change,
        )

        return ft.Container(
            visible=False,
            padding=ft.padding.all(12),
            border_radius=14,
            bgcolor=ft.Colors.SURFACE,
            content=ft.Column(
                spacing=8,
                controls=[
                    self._section_header("Аудио", ft.Icons.MUSIC_NOTE_OUTLINED),
                    self._row_with_help(
                        self.audio_bitrate_dropdown,
                        "Битрейт — сколько данных в секунду. Чем больше, тем чище звук и "
                        "тяжелее файл. Для MP3 минимум 192 kbps, для AAC хватает 192. "
                        "FLAC всегда без потерь — настройка игнорируется.",
                    ),
                    self._row_with_help(
                        self.audio_sample_dropdown,
                        "Частота дискретизации — сколько раз в секунду «фотографируется» "
                        "звук. 44.1 kHz — стандарт CD и музыки. 48 kHz — стандарт видео. "
                        "Выше — только если знаете зачем.",
                    ),
                    self._row_with_help(
                        self.audio_channels_dropdown,
                        "Количество звуковых каналов. Моно — 1, стерео — 2, 5.1 — 6. "
                        "«Авто» сохраняет оригинал.",
                    ),
                ],
            ),
        )

    def _build_video_settings(self) -> ft.Container:
        self.video_codec_dropdown = ft.Dropdown(
            label="Кодек", value="auto", border_radius=12, filled=True,
            options=[
                ft.dropdown.Option("auto", "Авто"),
                ft.dropdown.Option("h264", "H.264"),
                ft.dropdown.Option("h265", "H.265 / HEVC"),
                ft.dropdown.Option("av1",  "AV1"),
            ],
            on_change=self._on_video_codec_change,
        )

        self.video_bitrate_mode_dropdown = ft.Dropdown(
            label="Режим", value="crf", border_radius=12, filled=True,
            options=[
                ft.dropdown.Option("crf", "CRF · качество"),
                ft.dropdown.Option("cbr", "CBR · битрейт"),
            ],
            on_change=self._on_video_mode_change,
        )

        self.video_crf_slider = ft.Slider(
            min=14, max=35, value=self.settings.video_crf, divisions=21,
            label="{value}",
            on_change=self._on_video_crf_change,
        )
        self.video_crf_value = ft.Text(
            f"CRF {self.settings.video_crf}", size=12, color=ft.Colors.ON_SURFACE_VARIANT,
        )

        self.video_bitrate_field = ft.TextField(
            label="Битрейт, Mbps", value="8",
            input_filter=ft.InputFilter(regex_string=r"[0-9.]"),
            on_change=self._on_video_bitrate_change,
            border_radius=12, filled=True, visible=False,
        )

        self.video_fps_dropdown = ft.Dropdown(
            label="FPS", value="auto", border_radius=12, filled=True,
            options=[
                ft.dropdown.Option("auto", "Авто"),
                ft.dropdown.Option("24",   "24 · кино"),
                ft.dropdown.Option("30",   "30 · стандарт"),
                ft.dropdown.Option("60",   "60 · плавно"),
            ],
            on_change=self._on_fps_change,
        )

        self.video_resolution_dropdown = ft.Dropdown(
            label="Разрешение", value="auto", border_radius=12, filled=True,
            options=[
                ft.dropdown.Option("auto", "Авто"),
                ft.dropdown.Option("480",  "480p · SD"),
                ft.dropdown.Option("720",  "720p · HD"),
                ft.dropdown.Option("1080", "1080p · FHD"),
                ft.dropdown.Option("1440", "1440p · 2K"),
                ft.dropdown.Option("2160", "2160p · 4K"),
            ],
            on_change=self._on_resolution_change,
        )

        return ft.Container(
            visible=False,
            padding=ft.padding.all(12),
            border_radius=14,
            bgcolor=ft.Colors.SURFACE,
            content=ft.Column(
                spacing=8,
                controls=[
                    self._section_header("Видео", ft.Icons.MOVIE_OUTLINED),
                    self._row_with_help(
                        self.video_codec_dropdown,
                        "Кодек — алгоритм сжатия. H.264 играет везде. H.265/HEVC даёт ту же "
                        "картинку при меньшем размере, но требует чуть более мощного железа "
                        "для проигрывания. AV1 — новейший, ещё компактнее, но кодируется "
                        "медленнее всех.",
                    ),
                    self._row_with_help(
                        self.video_bitrate_mode_dropdown,
                        "CRF — постоянное визуальное качество, размер плавающий. "
                        "Хорош для архива и когда не важен размер. CBR — фиксированный "
                        "битрейт, размер предсказуем. Хорош для стриминга и лимитов.",
                    ),
                    self._row_with_help(
                        ft.Row([
                            ft.Container(expand=True, content=self.video_crf_slider),
                            self.video_crf_value,
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        "Constant Rate Factor — чем меньше число, тем выше качество и больше "
                        "файл. Для H.264/H.265: 18 — почти без потерь, 23 — стандарт, "
                        "28 — ощутимое сжатие. Шкала логарифмическая: −6 пунктов = ~×2 размера.",
                    ),
                    self._row_with_help(
                        self.video_bitrate_field,
                        "Целевой битрейт в мегабитах в секунду. Ориентиры для Full HD: "
                        "5–8 Mbps — веб, 12–20 Mbps — высокое качество, 35+ Mbps — мастер-копия. "
                        "Для 4K умножьте на 4.",
                    ),
                    self._row_with_help(
                        self.video_fps_dropdown,
                        "Частота кадров. 24 — киноощущение, 30 — стандарт ТВ и YouTube, "
                        "60 — плавность спорта и игр. Уменьшение fps уменьшает размер, "
                        "увеличение — нет (новые кадры просто дублируются).",
                    ),
                    self._row_with_help(
                        self.video_resolution_dropdown,
                        "Разрешение по высоте; ширина считается с сохранением пропорций. "
                        "Уменьшение даёт сильное сокращение веса. Увеличение бессмысленно — "
                        "пиксели не появятся.",
                    ),
                ],
            ),
        )

    def _build_output_card(self) -> ft.Container:
        self.out_path_text = ft.Text(
            "Рядом с исходными файлами",
            size=13, color=ft.Colors.ON_SURFACE_VARIANT,
            no_wrap=False, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
        )

        self.overwrite_dropdown = ft.Dropdown(
            label="Если файл существует",
            value=self.settings.overwrite_mode,
            border_radius=12, filled=True,
            options=[
                ft.dropdown.Option("rename",    "Новое имя"),
                ft.dropdown.Option("overwrite", "Перезаписать"),
                ft.dropdown.Option("skip",      "Пропустить"),
            ],
            on_change=self._on_overwrite_change,
        )

        self.notify_sound_switch = ft.Switch(
            value=True,
            on_change=self._on_notify_change,
            scale=0.8,
        )
        self._notify_enabled = True

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
                        ft.TextButton("Сбросить", on_click=self._reset_out_dir),
                    ]),
                    self._row_with_help(
                        self.overwrite_dropdown,
                        "Что делать, если файл с таким именем уже лежит в папке вывода. "
                        "По умолчанию — добавить суффикс _1, _2 и сохранить рядом.",
                    ),
                    self._row_with_help(
                        ft.Row([
                            ft.Text("Звук по завершении", size=13, expand=True),
                            self.notify_sound_switch,
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        "Системный звук, когда вся очередь обработана. Удобно, если "
                        "конвертируете большую пачку и переключаетесь на другие задачи.",
                    ),
                ],
            ),
        )

    def _on_overwrite_change(self, e) -> None:
        self.settings.overwrite_mode = e.control.value

    def _on_notify_change(self, e) -> None:
        self._notify_enabled = bool(e.control.value)

    def _build_preview_card(self) -> ft.Container:
        # Image control reused for image previews and video thumbnails.
        self.preview_image = ft.Image(
            src="",
            fit=ft.ImageFit.CONTAIN,
            border_radius=12,
            width=480, height=240,
            visible=False,
        )

        # Video playback slot — ft.Video instances are mounted into this
        # container per file; show_controls=True draws play/pause + a seek
        # bar natively.
        self.preview_video_slot = ft.Container(
            visible=False,
            height=260,
            border_radius=12,
            content=None,
        )

        # Audio playback widgets — ft.Audio is invisible; we drive a
        # seek slider + play button manually.
        self.preview_play_btn = ft.IconButton(
            icon=ft.Icons.PLAY_ARROW_ROUNDED,
            icon_size=32,
            tooltip="Воспроизвести",
            on_click=self._toggle_playback,
        )
        self.preview_position_text = ft.Text("0:00 / 0:00", size=12,
                                             color=ft.Colors.ON_SURFACE_VARIANT)
        self.preview_seek = ft.Slider(
            min=0, max=1.0, value=0.0,
            on_change_start=self._on_seek_start,
            on_change=self._on_seek_change,
            on_change_end=self._on_seek_end,
            expand=True,
        )
        self.preview_audio_panel = ft.Container(
            visible=False,
            padding=ft.padding.symmetric(horizontal=12, vertical=10),
            border_radius=12,
            bgcolor=ft.Colors.SURFACE,
            content=ft.Column(
                spacing=4,
                controls=[
                    ft.Row(
                        controls=[
                            ft.Container(
                                width=44, height=44, border_radius=12,
                                bgcolor=ft.Colors.SECONDARY_CONTAINER,
                                alignment=ft.alignment.center,
                                content=ft.Icon(ft.Icons.MUSIC_NOTE_OUTLINED,
                                                color=ft.Colors.ON_SECONDARY_CONTAINER),
                            ),
                            self.preview_play_btn,
                            ft.Container(expand=True, content=self.preview_seek),
                            self.preview_position_text,
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=10,
                    ),
                ],
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
                                self.preview_video_slot,
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
        self._refresh_target_allowed()
        self._refresh_estimated_sizes()
        self.page.update()

    def _on_codec_change(self, _e) -> None:
        val = self.codec_dropdown.value
        self.settings.audio_codec_for_video = None if val == "auto" else val
        self._refresh_estimated_sizes()

    # ----- Preset / settings handlers -----------------------------------
    def _on_preset_change(self, _e) -> None:
        key = self.preset_dropdown.value or "__none"
        if key == "__none":
            self.preset_description.visible = False
            self.preset_description.value = ""
            self.page.update()
            return
        preset = get_preset(key)
        if not preset:
            return
        # Reset settings to defaults so previous tweaks don't leak through.
        self.settings = default_settings()
        preset.apply(self.settings)
        # Sync controls to the new settings.
        self._sync_controls_from_settings()
        # Suggest the preset's target extension.
        if preset.target_ext in self._allowed_target_exts():
            self._set_target_ext(preset.target_ext)
        self.preset_description.value = preset.description
        self.preset_description.visible = True
        self._refresh_estimated_sizes()
        self.page.update()

    def _sync_controls_from_settings(self) -> None:
        s = self.settings
        # Image
        self.image_quality_slider.value = s.image_quality
        self.image_quality_value.value = f"{s.image_quality}%"
        self.image_strip_exif_switch.value = s.image_strip_exif
        self.image_resize_w.value = str(s.image_resize_width) if s.image_resize_width else ""
        self.image_resize_h.value = str(s.image_resize_height) if s.image_resize_height else ""
        self.image_keep_aspect_switch.value = s.image_keep_aspect
        # Audio
        self.audio_bitrate_dropdown.value = (
            str(s.audio_bitrate_kbps) if s.audio_bitrate_kbps else "auto"
        )
        self.audio_sample_dropdown.value = (
            str(s.audio_sample_rate) if s.audio_sample_rate else "auto"
        )
        self.audio_channels_dropdown.value = (
            str(s.audio_channels) if s.audio_channels else "auto"
        )
        # Video
        self.video_codec_dropdown.value = s.video_codec or "auto"
        self.video_bitrate_mode_dropdown.value = s.video_bitrate_mode
        self.video_crf_slider.value = s.video_crf
        self.video_crf_value.value = f"CRF {int(s.video_crf)}"
        self.video_bitrate_field.value = f"{s.video_bitrate_kbps / 1000:.1f}"
        self.video_bitrate_field.visible = s.video_bitrate_mode == "cbr"
        self.video_crf_slider.visible = s.video_bitrate_mode == "crf"
        self.video_fps_dropdown.value = str(s.video_fps) if s.video_fps else "auto"
        self.video_resolution_dropdown.value = str(s.video_height) if s.video_height else "auto"
        self.codec_dropdown.value = s.audio_codec_for_video or "auto"

    # Image
    def _on_image_quality_change(self, e) -> None:
        v = int(e.control.value)
        self.settings.image_quality = v
        self.image_quality_value.value = f"{v}%"
        self.image_quality_value.update()
        self._refresh_estimated_sizes()

    def _on_strip_exif_change(self, e) -> None:
        self.settings.image_strip_exif = bool(e.control.value)

    def _on_resize_change(self, _e) -> None:
        try:
            self.settings.image_resize_width = int(self.image_resize_w.value) if self.image_resize_w.value else None
        except ValueError:
            self.settings.image_resize_width = None
        try:
            self.settings.image_resize_height = int(self.image_resize_h.value) if self.image_resize_h.value else None
        except ValueError:
            self.settings.image_resize_height = None
        self._refresh_estimated_sizes()

    def _on_keep_aspect_change(self, e) -> None:
        self.settings.image_keep_aspect = bool(e.control.value)

    # Audio
    def _on_audio_bitrate_change(self, e) -> None:
        v = e.control.value
        self.settings.audio_bitrate_kbps = None if v == "auto" else int(v)
        self._refresh_estimated_sizes()

    def _on_sample_rate_change(self, e) -> None:
        v = e.control.value
        self.settings.audio_sample_rate = None if v == "auto" else int(v)
        self._refresh_estimated_sizes()

    def _on_channels_change(self, e) -> None:
        v = e.control.value
        self.settings.audio_channels = None if v == "auto" else int(v)
        self._refresh_estimated_sizes()

    # Video
    def _on_video_codec_change(self, e) -> None:
        v = e.control.value
        self.settings.video_codec = None if v == "auto" else v

    def _on_video_mode_change(self, e) -> None:
        v = e.control.value
        self.settings.video_bitrate_mode = v
        self.video_bitrate_field.visible = v == "cbr"
        self.video_crf_slider.visible = v == "crf"
        self.page.update()
        self._refresh_estimated_sizes()

    def _on_video_crf_change(self, e) -> None:
        v = int(e.control.value)
        self.settings.video_crf = v
        self.video_crf_value.value = f"CRF {v}"
        self.video_crf_value.update()
        self._refresh_estimated_sizes()

    def _on_video_bitrate_change(self, e) -> None:
        try:
            mbps = float(e.control.value or "0")
        except ValueError:
            mbps = 0
        if mbps > 0:
            self.settings.video_bitrate_kbps = int(mbps * 1000)
            self._refresh_estimated_sizes()

    def _on_fps_change(self, e) -> None:
        v = e.control.value
        self.settings.video_fps = None if v == "auto" else int(v)

    def _on_resolution_change(self, e) -> None:
        v = e.control.value
        self.settings.video_height = None if v == "auto" else int(v)
        self._refresh_estimated_sizes()

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

        # Show only the relevant settings sections.
        target_kind = media_kind(self.target_ext) if self.target_ext else None
        # Image section: relevant when there are images in queue OR target is image.
        self.image_settings.visible = "image" in kinds or target_kind == "image"
        # Audio section: when audio is in queue, OR target is audio, OR there's a
        # video target with an audio track (always).
        self.audio_settings.visible = (
            "audio" in kinds or target_kind in ("audio", "video")
            or "video" in kinds
        )
        # Video section: when video is in queue and target is video.
        self.video_settings.visible = (
            "video" in kinds and target_kind == "video"
        )

    def _refresh_estimated_sizes(self) -> None:
        if not self.target_ext:
            for item in self.files:
                item.estimated_size = None
        else:
            for item in self.files:
                # Only estimate sizes for compatible types.
                if item.kind == "image" and self.target_ext in IMAGE_FORMATS:
                    item.estimated_size = estimate_output_size_bytes(
                        item.path, self.target_ext, self.settings,
                    )
                elif item.kind == "audio" and self.target_ext in AUDIO_FORMATS:
                    item.estimated_size = estimate_output_size_bytes(
                        item.path, self.target_ext, self.settings,
                    )
                elif item.kind == "video":
                    item.estimated_size = estimate_output_size_bytes(
                        item.path, self.target_ext, self.settings,
                    )
                else:
                    item.estimated_size = None
        try:
            self._refresh_files()
        except Exception:
            pass

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
            # On success, item.message holds the destination path. Offer
            # buttons to reveal the file in the OS file manager and to
            # open it in the system default app.
            dst_str = item.message
            trailing = ft.Row(
                spacing=2, tight=True,
                controls=[
                    ft.IconButton(
                        icon=ft.Icons.FOLDER_OPEN_OUTLINED,
                        icon_size=20,
                        tooltip="Открыть папку с файлом",
                        on_click=lambda _e, p=dst_str: self._open_in_explorer(Path(p)),
                    ),
                    ft.IconButton(
                        icon=ft.Icons.OPEN_IN_NEW_ROUNDED,
                        icon_size=20,
                        tooltip="Открыть файл",
                        on_click=lambda _e, p=dst_str: self._open_file(Path(p)),
                    ),
                    ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.TERTIARY),
                ],
            )
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
        if item.estimated_size and item.status == "pending":
            subtitle_text += f" → ~{human_size(item.estimated_size)}"
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
        self._detach_audio()
        self._detach_video()
        self.is_playing = False
        self._has_started = False
        self._seeking = False
        self.preview_play_btn.icon = ft.Icons.PLAY_ARROW_ROUNDED
        self.selected_item = None
        self.preview_image.src = ""
        self.preview_image.visible = False
        self.preview_audio_panel.visible = False
        self.preview_placeholder.visible = True
        self.preview_subtitle.value = ""
        self.preview_subtitle.visible = False
        self.preview_seek.value = 0
        self.preview_position_text.value = "0:00 / 0:00"

    def _detach_audio(self) -> None:
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

    def _detach_video(self) -> None:
        if getattr(self, "preview_video", None):
            try:
                self.preview_video.pause()
            except Exception:
                pass
        self.preview_video = None
        self.preview_video_slot.content = None
        self.preview_video_slot.visible = False

    def _render_preview(self, item: FileItem) -> None:
        try:
            size = human_size(item.path.stat().st_size)
        except OSError:
            size = "?"
        self.preview_subtitle.value = (
            f"{item.path.name} · {KIND_LABEL.get(item.kind or '', 'Файл')} · {size}"
        )
        self.preview_subtitle.visible = True

        self._detach_audio()
        self._detach_video()
        self.is_playing = False
        self._has_started = False
        self._seeking = False
        self._duration_ms = 0
        self.preview_play_btn.icon = ft.Icons.PLAY_ARROW_ROUNDED
        self.preview_seek.value = 0
        self.preview_position_text.value = "0:00 / 0:00"

        if item.kind == "image":
            self.preview_image.src = str(item.path)
            self.preview_image.visible = True
            self.preview_audio_panel.visible = False
            self.preview_placeholder.visible = False
        elif item.kind == "audio":
            self.preview_image.visible = False
            self.preview_placeholder.visible = False
            self.preview_audio_panel.visible = True
            self._attach_audio(item.path)
        elif item.kind == "video":
            self.preview_audio_panel.visible = False
            self.preview_image.visible = False
            self.preview_placeholder.visible = False
            self._mount_video(item.path)
        else:
            self._clear_preview()

        self.page.update()

    def _attach_audio(self, src: Path) -> None:
        # ft.Audio's underlying audioplayers backend is finicky with raw
        # Windows paths — feed it a proper file:// URI instead.
        try:
            uri = Path(src).resolve().as_uri()
        except ValueError:
            uri = str(src)
        audio = ft.Audio(
            src=uri,
            autoplay=False,
            volume=1.0,
            on_loaded=self._on_audio_loaded,
            on_duration_changed=self._on_audio_duration,
            on_position_changed=self._on_audio_position,
            on_state_changed=self._on_audio_state,
        )
        self.page.overlay.append(audio)
        self.preview_audio = audio
        self.page.update()

    def _mount_video(self, src: Path) -> None:
        try:
            resource = Path(src).resolve().as_uri()
        except ValueError:
            resource = str(src)
        try:
            video = ft.Video(
                playlist=[ft.VideoMedia(resource=resource)],
                autoplay=False,
                show_controls=True,  # native play/pause + seek bar
                muted=False,
                aspect_ratio=16 / 9,
                fit=ft.ImageFit.CONTAIN,
                on_error=self._on_video_error,
            )
        except Exception:
            self._show_video_fallback(src)
            return
        self.preview_video = video
        self.preview_video_slot.content = video
        self.preview_video_slot.visible = True

    def _on_video_error(self, _e) -> None:
        item = self.selected_item
        if item:
            self._show_video_fallback(item.path)

    def _show_video_fallback(self, src: Path) -> None:
        # mpv backend missing or unreadable file — fall back to a thumbnail
        # plus an "open in default player" hint.
        self._detach_video()
        self.preview_image.visible = True
        threading.Thread(
            target=self._generate_and_show_thumbnail,
            args=(self.selected_item,) if self.selected_item else (FileItem(Path(src)),),
            daemon=True,
        ).start()
        self._toast(
            "Предпросмотр видео недоступен. Откройте файл во встроенном проигрывателе системы.",
            error=True,
        )

    def _on_audio_loaded(self, _e) -> None:
        # Once metadata is ready, asking for duration reliably returns it.
        if not self.preview_audio:
            return
        try:
            d = self.preview_audio.get_duration()
            if d:
                self._duration_ms = int(d)
                self._update_position_label(0)
        except Exception:
            pass

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
        # Sync seek slider unless the user is actively dragging it.
        if self._duration_ms > 0 and not self._seeking:
            self.preview_seek.value = max(0.0, min(1.0, pos / self._duration_ms))
            try:
                self.preview_seek.update()
            except Exception:
                pass

    def _on_audio_state(self, e) -> None:
        # When playback completes, reset the play icon.
        state = getattr(e, "data", "") or ""
        if state.lower() in ("completed", "stopped"):
            self.is_playing = False
            self._has_started = False
            self.preview_play_btn.icon = ft.Icons.PLAY_ARROW_ROUNDED
            self.preview_seek.value = 0
            self.page.update()

    def _update_position_label(self, pos_ms: int) -> None:
        def fmt(ms: int) -> str:
            s = max(0, ms // 1000)
            return f"{s // 60}:{s % 60:02d}"
        self.preview_position_text.value = f"{fmt(pos_ms)} / {fmt(self._duration_ms)}"
        try:
            self.preview_position_text.update()
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
                # play() starts from beginning; resume() continues from
                # the last position (or last seek). Use resume after the
                # first start to preserve seek behaviour.
                if self._has_started:
                    self.preview_audio.resume()
                else:
                    self.preview_audio.play()
                    self._has_started = True
                self.is_playing = True
                self.preview_play_btn.icon = ft.Icons.PAUSE_ROUNDED
        except Exception as exc:
            self._toast(f"Не удалось воспроизвести: {exc}", error=True)
        self.page.update()

    # Seek-slider handlers ------------------------------------------------
    def _on_seek_start(self, _e) -> None:
        self._seeking = True

    def _on_seek_change(self, _e) -> None:
        # Update the label live while dragging, but don't seek yet.
        if self._duration_ms <= 0:
            return
        target_ms = int(self.preview_seek.value * self._duration_ms)
        self._update_position_label(target_ms)

    def _on_seek_end(self, _e) -> None:
        self._seeking = False
        if not self.preview_audio or self._duration_ms <= 0:
            return
        target_ms = int(self.preview_seek.value * self._duration_ms)
        try:
            self.preview_audio.seek(target_ms)
            # If playback wasn't started yet, seek implicitly starts it on
            # some backends — track the started flag so resume() works next.
            if not self._has_started and self.is_playing:
                self._has_started = True
        except Exception:
            pass

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

    # ----- OS integration -----------------------------------------------
    def _open_in_explorer(self, path: Path) -> None:
        """Open the OS file manager focused on the given file (or its folder)."""
        path = Path(path)
        target = path if path.exists() else path.parent
        try:
            if sys.platform == "win32":
                if target.is_file():
                    # /select, takes the path as part of the same arg.
                    subprocess.Popen(["explorer", f"/select,{target}"])
                else:
                    os.startfile(str(target))
            elif sys.platform == "darwin":
                if target.is_file():
                    subprocess.Popen(["open", "-R", str(target)])
                else:
                    subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target.parent if target.is_file() else target)])
        except Exception as exc:
            self._toast(f"Не удалось открыть проводник: {exc}", error=True)

    def _open_last_output_folder(self, _e=None) -> None:
        if self.last_output_folder and self.last_output_folder.exists():
            self._open_in_explorer(self.last_output_folder)
        else:
            self._toast("Папка не найдена.", error=True)

    def _open_file(self, path: Path) -> None:
        """Open a file with the OS default application."""
        path = Path(path)
        if not path.exists():
            self._toast("Файл не найден.", error=True)
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            self._toast(f"Не удалось открыть файл: {exc}", error=True)

    # ----- log + notify -------------------------------------------------
    def _log(self, line: str) -> None:
        """Append a line to the in-memory ffmpeg log."""
        with self._log_lock:
            self.log_lines.append(line)
            # Cap to last 5000 lines to avoid unbounded growth.
            if len(self.log_lines) > 5000:
                self.log_lines = self.log_lines[-5000:]
            # Live-update the text in the dialog if open.
            if getattr(self, "_log_textarea", None):
                try:
                    self._log_textarea.value = "\n".join(self.log_lines[-1000:])
                    self._log_textarea.update()
                except Exception:
                    pass

    def _open_log_dialog(self, _e=None) -> None:
        with self._log_lock:
            text = "\n".join(self.log_lines[-1000:]) or "Журнал пока пуст."
        self._log_textarea = ft.TextField(
            value=text,
            multiline=True,
            min_lines=20,
            max_lines=20,
            text_size=11,
            read_only=True,
            border=ft.InputBorder.NONE,
            bgcolor=ft.Colors.SURFACE,
            color=ft.Colors.ON_SURFACE,
            text_style=ft.TextStyle(font_family="Consolas, Menlo, monospace"),
        )
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Журнал ffmpeg"),
            content=ft.Container(
                width=720, height=420,
                content=self._log_textarea,
            ),
            actions=[
                ft.TextButton("Очистить", on_click=lambda _e: self._clear_log()),
                ft.TextButton("Закрыть", on_click=lambda _e: self.page.close(dlg)),
            ],
        )
        self.page.open(dlg)

    def _clear_log(self) -> None:
        with self._log_lock:
            self.log_lines.clear()
        if self._log_textarea:
            self._log_textarea.value = ""
            try:
                self._log_textarea.update()
            except Exception:
                pass

    def _notify_complete(self, ok: int, total: int) -> None:
        """Beep + system notification + toast at end of batch."""
        if ok == total and total > 0:
            self._toast(f"Готово — {ok} файлов конвертировано.")
        elif ok == 0:
            self._toast("Не удалось конвертировать ни одного файла.", error=True)
        else:
            self._toast(f"Готово {ok} из {total}. Остальное смотрите в списке.", error=True)

        if not getattr(self, "_notify_enabled", True):
            return
        try:
            if sys.platform == "win32":
                import winsound
                winsound.MessageBeep(winsound.MB_OK)
            elif sys.platform == "darwin":
                subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
            else:
                # Linux: try canberra-gtk-play, fall back to terminal bell.
                try:
                    subprocess.Popen(["canberra-gtk-play", "-i", "complete"])
                except FileNotFoundError:
                    print("\a", end="", flush=True)
        except Exception:
            pass

    # ----- ffmpeg install helper ---------------------------------------
    def _show_ffmpeg_install_dialog(self) -> None:
        """First-run dialog when ffmpeg isn't on PATH."""
        status_label = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT, visible=False)
        progress = ft.ProgressRing(width=18, height=18, stroke_width=2, visible=False)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=ft.Colors.TERTIARY),
                ft.Text("Нужен ffmpeg"),
            ]),
            content=ft.Container(
                width=520,
                content=ft.Column(
                    spacing=10, tight=True,
                    controls=[
                        ft.Text(
                            "Аудио и видео конвертируются через ffmpeg. У вас в системе он "
                            "не найден. Картинки работают и без него.",
                            size=13,
                        ),
                        ft.Text(
                            "Установить можно автоматически (требуется winget — есть в Windows 10/11), "
                            "либо открыть инструкцию и установить вручную.",
                            size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.Row([progress, status_label],
                               vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                    ],
                ),
            ),
            actions=[
                ft.TextButton(
                    "Открыть инструкцию",
                    icon=ft.Icons.OPEN_IN_NEW_ROUNDED,
                    on_click=lambda _e: webbrowser.open(
                        "https://www.gyan.dev/ffmpeg/builds/"
                    ),
                ),
                ft.FilledTonalButton(
                    "Установить через winget",
                    icon=ft.Icons.DOWNLOAD_ROUNDED,
                    on_click=lambda _e: self._install_ffmpeg_winget(status_label, progress),
                ),
                ft.TextButton(
                    "Я уже установил",
                    on_click=lambda _e: self._recheck_ffmpeg(dlg, status_label),
                ),
                ft.TextButton("Позже", on_click=lambda _e: self.page.close(dlg)),
            ],
        )
        self._ffmpeg_dialog = dlg
        self.page.open(dlg)

    def _install_ffmpeg_winget(self, status: ft.Text, progress: ft.ProgressRing) -> None:
        if sys.platform != "win32":
            self._toast("Автоустановка через winget доступна только на Windows.", error=True)
            return
        status.value = "Запускаю winget…"
        status.visible = True
        progress.visible = True
        try:
            status.update(); progress.update()
        except Exception:
            pass

        def worker():
            try:
                # winget needs an interactive console for the EULA on first use,
                # so launch in a new visible terminal and let the user accept.
                subprocess.Popen(
                    ["cmd", "/c", "start", "winget",
                     "install", "--id=Gyan.FFmpeg", "-e", "--source", "winget"],
                    creationflags=0x00000010,  # CREATE_NEW_CONSOLE
                )
                self._safe_run(lambda: setattr(status, "value",
                    "Открыл окно winget. После установки нажмите «Я уже установил»."))
            except FileNotFoundError:
                self._safe_run(lambda: setattr(status, "value",
                    "winget не найден. Воспользуйтесь инструкцией."))
                self._safe_run(lambda: setattr(status, "color", ft.Colors.ERROR))
            except Exception as e:
                self._safe_run(lambda: setattr(status, "value", f"Ошибка: {e}"))
                self._safe_run(lambda: setattr(status, "color", ft.Colors.ERROR))
            finally:
                self._safe_run(lambda: setattr(progress, "visible", False))
                try:
                    self.page.update()
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _recheck_ffmpeg(self, dlg: ft.AlertDialog, status: ft.Text) -> None:
        if ffmpeg_available():
            self.page.close(dlg)
            self._toast("ffmpeg найден — всё готово!")
        else:
            status.value = "Пока не вижу ffmpeg в PATH. Откройте новое окно приложения после установки."
            status.color = ft.Colors.ERROR
            status.visible = True
            try:
                status.update()
            except Exception:
                pass

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
        self.eta_text.value = ""
        for item in self.files:
            item.status = "pending"
            item.message = ""
            item.dst = None
        self._refresh_files()

        # Probe durations up front so the ETA has something to base on.
        self._total_duration_s = 0.0
        self._completed_duration_s = 0.0
        self._per_item_progress = 0.0
        self._current_duration_s = 0.0
        for item in self.files:
            item.duration_s = 0.0
            if item.kind in ("audio", "video"):
                item.duration_s = probe_duration(item.path) or 0.0
            self._total_duration_s += item.duration_s

        self._conv_start_ts = time.time()
        self._eta_stop.clear()
        self._eta_thread = threading.Thread(target=self._eta_ticker, daemon=True)
        self._eta_thread.start()

        thread = threading.Thread(target=self._run_conversion, daemon=True)
        thread.start()

    def _resolve_dst(self, item: FileItem) -> Path | None:
        """Apply the user's overwrite policy to pick a destination path.

        Returns None to signal "skip this file".
        """
        out_dir = self.out_dir or item.path.parent
        dst = out_dir / f"{item.path.stem}.{self.target_ext}"
        if dst.resolve() == item.path.resolve():
            dst = out_dir / f"{item.path.stem}_converted.{self.target_ext}"
        if not dst.exists():
            return dst
        policy = self.settings.overwrite_mode
        if policy == "overwrite":
            return dst
        if policy == "skip":
            return None
        # rename: append _1, _2, …
        i = 1
        while True:
            candidate = out_dir / f"{item.path.stem}_{i}.{self.target_ext}"
            if not candidate.exists():
                return candidate
            i += 1

    def _run_conversion(self) -> None:
        total = len(self.files)
        ok_count = 0
        skipped_count = 0
        last_dst: Path | None = None
        for idx, item in enumerate(self.files, start=1):
            item.status = "running"
            self._current_duration_s = item.duration_s
            self._per_item_progress = 0.0
            self.status_text.value = f"[{idx}/{total}] {item.path.name}"
            self._safe_update()

            dst = self._resolve_dst(item)
            if dst is None:
                item.status = "error"
                item.message = "Пропущен — файл уже существует."
                skipped_count += 1
                self._completed_duration_s += item.duration_s
                self._safe_update()
                continue

            try:
                self._log(f"--- {item.path.name} → {dst.name} ---")
                convert(
                    item.path, dst,
                    settings=self.settings,
                    on_log=self._log,
                    on_progress=self._on_item_progress,
                    duration_s=item.duration_s or None,
                )
                item.status = "ok"
                item.message = str(dst)
                item.dst = dst
                ok_count += 1
                last_dst = dst
            except Exception as e:
                item.status = "error"
                item.message = str(e)
                self._log(f"ERROR: {e}")
            self._completed_duration_s += item.duration_s
            self._per_item_progress = 1.0
            self._safe_update()

        self._eta_stop.set()
        if last_dst is not None:
            self.last_output_folder = last_dst.parent
            self.open_output_btn.visible = True

        elapsed = self._fmt_eta(time.time() - (self._conv_start_ts or time.time()))
        self.progress.value = 1.0
        self.status_text.value = (
            f"Готово: {ok_count} из {total}"
            + (f" · пропущено {skipped_count}" if skipped_count else "")
            + f" · заняло {elapsed}"
        )
        self.eta_text.value = ""
        self.running = False
        self.convert_btn.disabled = False
        self._safe_update()
        self._notify_complete(ok_count, total)

    def _on_item_progress(self, fraction: float) -> None:
        # Called from inside _run_ffmpeg; lightweight bookkeeping only.
        self._per_item_progress = max(0.0, min(1.0, fraction))

    def _eta_ticker(self) -> None:
        """Poll progress + elapsed time and update the ETA label every 0.5s."""
        while not self._eta_stop.is_set():
            try:
                self._update_eta_display()
            except Exception:
                pass
            self._eta_stop.wait(0.5)

    def _update_eta_display(self) -> None:
        if not self._conv_start_ts:
            return
        elapsed = time.time() - self._conv_start_ts

        if self._total_duration_s > 0:
            done = self._completed_duration_s + self._current_duration_s * self._per_item_progress
            done = min(done, self._total_duration_s)
            fraction = done / self._total_duration_s if self._total_duration_s else 0
        else:
            # No durations probed (image-only batches). Fall back to file count.
            n = len(self.files)
            done_files = sum(1 for f in self.files if f.status in ("ok", "error"))
            fraction = (done_files + self._per_item_progress) / max(1, n)

        fraction = max(0.0, min(1.0, fraction))
        self.progress.value = fraction

        if fraction > 0.02:
            total = elapsed / fraction
            remaining = max(0.0, total - elapsed)
            self.eta_text.value = (
                f"Прошло: {self._fmt_eta(elapsed)} · Осталось: ~{self._fmt_eta(remaining)}"
            )
        else:
            self.eta_text.value = f"Прошло: {self._fmt_eta(elapsed)}"

        try:
            self.progress.update()
            self.eta_text.update()
        except Exception:
            pass

    @staticmethod
    def _fmt_eta(seconds: float) -> str:
        seconds = int(max(0, seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def _safe_update(self) -> None:
        try:
            self._refresh_files()
        except Exception:
            pass


def main(page: ft.Page) -> None:
    ConverterApp(page)


if __name__ == "__main__":
    ft.app(target=main)
