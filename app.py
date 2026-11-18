"""Universal media converter — Material 3 / Material You GUI on Flet 1.x."""
from __future__ import annotations

import atexit
import dataclasses
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import flet as ft

from converter import (
    ALL_FORMATS,
    AUDIO_FORMATS,
    IMAGE_FORMATS,
    PRESETS,
    VIDEO_FORMATS,
    ConversionCancelled,
    ConversionSettings,
    StreamInfo,
    convert,
    default_settings,
    effective_duration,
    estimate_output_size_bytes,
    ffmpeg_available,
    file_checksum,
    get_preset,
    images_to_pdf,
    media_kind,
    probe_duration,
    probe_streams,
)
from preview import PreviewPane, cleanup_thumbnails
from settings_panel import SettingsPanel
from ui_kit import (
    DEFAULT_SEED,
    KIND_ICON,
    KIND_LABEL,
    SEED_COLORS,
    SHAPE_EXTRA_LARGE,
    SHAPE_LARGE,
    build_theme,
    fmt_time,
    full_width,
    human_size,
    label,
    pane,
    section,
)

try:
    import psutil
except ImportError:  # pause needs psutil
    psutil = None

# Target groups for the format picker. Video can also become a GIF.
TARGET_GROUPS: dict[str, list[str]] = {
    "image": IMAGE_FORMATS,
    "audio": AUDIO_FORMATS,
    "video": VIDEO_FORMATS + ["gif"],
}
GROUP_LABEL = {"image": "Картинка", "audio": "Аудио", "video": "Видео"}

# Settings that describe *how* the user works rather than a quality recipe —
# presets must not reset them.
_STICKY_FIELDS = (
    "overwrite_mode", "compute_checksum", "copy_metadata", "low_priority", "hwaccel",
    "trim_start_s", "trim_end_s", "watermark_text", "watermark_position",
    "watermark_opacity", "watermark_color",
)

PREF_PREFIX = "converter."


@dataclass(eq=False)
class FileItem:
    path: Path
    kind: Optional[str] = None
    status: str = "pending"  # pending | running | ok | error | skipped | cancelled
    message: str = ""
    dst: Optional[Path] = None
    duration_s: Optional[float] = None   # None = not probed yet
    streams: list[StreamInfo] = field(default_factory=list)
    audio_track: Optional[int] = None    # per-file audio stream choice
    estimated_size: Optional[int] = None
    checksum: Optional[str] = None
    progress: float = 0.0
    ui: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kind = media_kind(self.path.suffix)


class ConverterApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.files: list[FileItem] = []
        self.target_ext: Optional[str] = None
        self.target_group: str = "image"
        self.settings: ConversionSettings = default_settings()
        self.out_dir: Optional[Path] = None
        self.notify_enabled = True
        self.seed = DEFAULT_SEED
        self.selected: Optional[FileItem] = None

        # Run state
        self.running = False
        self._cancel = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._paused = False
        self._compact = False
        self._run_started = 0.0
        self._total_s = 0.0
        self._done_s = 0.0
        self._current: Optional[FileItem] = None
        self._pause_started = 0.0
        self._job_settings: ConversionSettings = self.settings
        self._last_output_dir: Optional[Path] = None

        # Log
        self._log_lines: list[str] = []
        self._log_lock = threading.Lock()
        self._log_view: Optional[ft.TextField] = None
        self._log_dirty = False

        self.prefs = ft.SharedPreferences()
        self.file_picker = ft.FilePicker()
        self.clipboard = ft.Clipboard()
        self.url_launcher = ft.UrlLauncher()

        self._setup_page()
        self.panel = SettingsPanel(self)
        self.preview = PreviewPane(self)
        self._build()
        threading.Thread(target=cleanup_thumbnails, daemon=True).start()

    # =================================================================== page
    def _setup_page(self) -> None:
        p = self.page
        p.title = "Конвертер"
        p.padding = 0
        p.spacing = 0
        p.theme_mode = ft.ThemeMode.SYSTEM
        p.theme = build_theme(self.seed)
        p.dark_theme = build_theme(self.seed)
        p.window.width = 1280
        p.window.height = 860
        p.window.min_width = 960
        p.window.min_height = 640
        # Intercept the close button so a running ffmpeg is killed first.
        p.window.prevent_close = True
        p.window.on_event = self._on_window_event
        # Belt and braces: session closed / interpreter exiting → stop ffmpeg.
        p.on_close = lambda _e: self.shutdown()
        atexit.register(self.shutdown)

    async def load_prefs(self) -> None:
        """Restore theme, seed color, output folder and sound from last time."""
        try:
            seed = await self.prefs.get(PREF_PREFIX + "seed")
            theme = await self.prefs.get(PREF_PREFIX + "theme")
            notify = await self.prefs.get(PREF_PREFIX + "notify")
            out_dir = await self.prefs.get(PREF_PREFIX + "out_dir")
        except Exception:
            return
        if isinstance(seed, str) and seed:
            self._apply_seed(seed, save=False)
        if theme in ("system", "light", "dark"):
            self._apply_theme_mode(theme, save=False)
        if isinstance(notify, bool):
            self.set_notify(notify, save=False)
            self.panel.sound_switch.value = notify
        if isinstance(out_dir, str) and out_dir and Path(out_dir).is_dir():
            self.set_out_dir(Path(out_dir), save=False)
        self.page.update()

    def _save_pref(self, key: str, value) -> None:
        async def _save():
            try:
                if value is None:
                    await self.prefs.remove(PREF_PREFIX + key)
                else:
                    await self.prefs.set(PREF_PREFIX + key, value)
            except Exception:
                pass
        self.page.run_task(_save)

    # ================================================================= layout
    def _build(self) -> None:
        self.app_bar = self._build_app_bar()
        self.ffmpeg_banner = self._build_ffmpeg_banner()

        self.left_pane = pane(
            self._build_target_section(),
            self._build_preset_section(),
            *self.panel.sections,
            padding=12,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        self.left_col = ft.Container(width=420, content=self.left_pane)

        self.queue_pane = self._build_queue_pane()
        self.action_bar = self._build_action_bar()
        right_col = ft.Column(
            expand=True, spacing=16,
            controls=[self.preview.control, self.queue_pane, self.action_bar],
        )
        self.body = ft.Row(
            expand=True, spacing=16,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[self.left_col, right_col],
        )
        self.page.add(
            ft.SafeArea(
                expand=True,
                content=ft.Container(
                    expand=True,
                    padding=ft.Padding.only(left=16, right=16, bottom=16),
                    content=ft.Column(
                        expand=True, spacing=12,
                        controls=[self.app_bar, self.ffmpeg_banner, self.body],
                    ),
                ),
            )
        )
        self._refresh_all()

    # ---------------------------------------------------------------- app bar
    def _build_app_bar(self) -> ft.Container:
        self.theme_toggle = ft.SegmentedButton(
            selected=["system"],
            show_selected_icon=False,
            segments=[
                ft.Segment(value="system", icon=ft.Icons.BRIGHTNESS_AUTO_OUTLINED,
                           tooltip="Как в системе"),
                ft.Segment(value="light", icon=ft.Icons.LIGHT_MODE_OUTLINED, tooltip="Светлая"),
                ft.Segment(value="dark", icon=ft.Icons.DARK_MODE_OUTLINED, tooltip="Тёмная"),
            ],
            on_change=lambda e: self._apply_theme_mode(e.control.selected[0]),
        )
        self.palette_menu = ft.PopupMenuButton(
            icon=ft.Icons.PALETTE_OUTLINED,
            tooltip="Цвет оформления (Material You)",
            items=self._palette_items(),
        )
        self.compact_btn = ft.IconButton(
            icon=ft.Icons.PICTURE_IN_PICTURE_ALT_OUTLINED,
            tooltip="Компактный режим поверх окон",
            on_click=self._toggle_compact,
        )
        self.app_title_sub = label("Картинки · аудио · видео", style=ft.TextThemeStyle.BODY_MEDIUM)
        return ft.Container(
            padding=ft.Padding.symmetric(vertical=12, horizontal=4),
            content=ft.Row(
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
                controls=[
                    ft.Container(
                        width=48, height=48, border_radius=SHAPE_LARGE,
                        bgcolor=ft.Colors.PRIMARY_CONTAINER,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(ft.Icons.AUTORENEW_ROUNDED, size=28,
                                        color=ft.Colors.ON_PRIMARY_CONTAINER),
                    ),
                    ft.Column(spacing=0, expand=True, controls=[
                        ft.Text("Конвертер", theme_style=ft.TextThemeStyle.TITLE_LARGE),
                        self.app_title_sub,
                    ]),
                    self.palette_menu,
                    self.theme_toggle,
                    ft.IconButton(icon=ft.Icons.TERMINAL_ROUNDED, tooltip="Журнал ffmpeg",
                                  on_click=self._open_log),
                    self.compact_btn,
                ],
            ),
        )

    def _palette_items(self) -> list[ft.PopupMenuItem]:
        items = []
        for name, color in SEED_COLORS:
            items.append(ft.PopupMenuItem(
                content=ft.Row(spacing=12, controls=[
                    ft.Container(width=20, height=20, border_radius=10, bgcolor=color),
                    ft.Text(name, expand=True),
                    ft.Icon(ft.Icons.CHECK_ROUNDED, size=18,
                            visible=color.lower() == self.seed.lower()),
                ]),
                on_click=lambda _e, c=color: self._apply_seed(c),
            ))
        return items

    def _apply_seed(self, seed: str, save: bool = True) -> None:
        self.seed = seed
        self.page.theme = build_theme(seed)
        self.page.dark_theme = build_theme(seed)
        self.palette_menu.items = self._palette_items()
        if save:
            self._save_pref("seed", seed)
        self.page.update()

    def _apply_theme_mode(self, mode: str, save: bool = True) -> None:
        self.page.theme_mode = {
            "light": ft.ThemeMode.LIGHT, "dark": ft.ThemeMode.DARK,
        }.get(mode, ft.ThemeMode.SYSTEM)
        self.theme_toggle.selected = [mode]
        if save:
            self._save_pref("theme", mode)
        self.page.update()

    # --------------------------------------------------------- ffmpeg banner
    def _build_ffmpeg_banner(self) -> ft.Container:
        if sys.platform == "win32":
            action = ft.FilledTonalButton("Установить через winget", icon=ft.Icons.DOWNLOAD_ROUNDED,
                                          on_click=self._install_ffmpeg_winget)
        else:
            cmd = "brew install ffmpeg" if sys.platform == "darwin" else "sudo apt install ffmpeg"
            action = ft.FilledTonalButton(f"Скопировать «{cmd}»", icon=ft.Icons.CONTENT_COPY_ROUNDED,
                                          on_click=lambda _e, c=cmd: self._copy(c))
        self.ffmpeg_status = label("", visible=False, color=ft.Colors.ON_TERTIARY_CONTAINER)
        return ft.Container(
            visible=not ffmpeg_available(),
            padding=16,
            border_radius=SHAPE_LARGE,
            bgcolor=ft.Colors.TERTIARY_CONTAINER,
            content=ft.Row(
                wrap=True, spacing=12, run_spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, color=ft.Colors.ON_TERTIARY_CONTAINER),
                    ft.Column(spacing=2, expand=True, controls=[
                        ft.Text("ffmpeg не найден", theme_style=ft.TextThemeStyle.TITLE_SMALL,
                                color=ft.Colors.ON_TERTIARY_CONTAINER),
                        ft.Text("Картинки конвертируются и без него, а для аудио и видео "
                                "нужен ffmpeg в PATH.",
                                theme_style=ft.TextThemeStyle.BODY_SMALL,
                                color=ft.Colors.ON_TERTIARY_CONTAINER),
                        self.ffmpeg_status,
                    ]),
                    action,
                    ft.TextButton("Инструкция", icon=ft.Icons.OPEN_IN_NEW_ROUNDED,
                                  on_click=self._open_ffmpeg_docs),
                    ft.TextButton("Проверить снова", icon=ft.Icons.REFRESH_ROUNDED,
                                  on_click=self._recheck_ffmpeg),
                ],
            ),
        )

    async def _open_ffmpeg_docs(self, _e) -> None:
        await self.url_launcher.launch_url("https://ffmpeg.org/download.html")

    def _install_ffmpeg_winget(self, _e) -> None:
        try:
            # winget may need an interactive console for the EULA on first use.
            subprocess.Popen(
                ["cmd", "/c", "start", "winget", "install", "--id=Gyan.FFmpeg", "-e",
                 "--source", "winget"],
                creationflags=0x00000010,  # CREATE_NEW_CONSOLE
            )
            msg = "Открыл окно winget. После установки перезапустите приложение."
        except Exception as e:  # noqa: BLE001
            msg = f"Не получилось запустить winget: {e}"
        self.ffmpeg_status.value = msg
        self.ffmpeg_status.visible = True

    def _recheck_ffmpeg(self, _e) -> None:
        if ffmpeg_available():
            self.ffmpeg_banner.visible = False
            self.toast("ffmpeg найден — всё готово!")
        else:
            self.ffmpeg_status.value = ("Пока не вижу ffmpeg в PATH. Если только что установили — "
                                        "перезапустите приложение.")
            self.ffmpeg_status.visible = True

    async def _copy(self, text: str) -> None:
        await self.clipboard.set(text)
        self.toast("Команда скопирована")

    # ------------------------------------------------------- target format
    def _build_target_section(self) -> ft.Container:
        self.group_buttons = ft.SegmentedButton(
            selected=[self.target_group],
            show_selected_icon=False,
            segments=[
                ft.Segment(value=g, label=GROUP_LABEL[g], icon=KIND_ICON[g])
                for g in ("image", "audio", "video")
            ],
            on_change=self._on_group_change,
        )
        self.format_chips = ft.Row(wrap=True, spacing=8, run_spacing=8)
        self.format_hint = label("", visible=False)
        return section(
            "Во что конвертировать", ft.Icons.SWAP_HORIZ_ROUNDED,
            ft.Row([self.group_buttons]),
            self.format_chips,
            self.format_hint,
        )

    def _on_group_change(self, e) -> None:
        self.target_group = e.control.selected[0]
        if self.target_ext not in TARGET_GROUPS[self.target_group]:
            self.target_ext = None
        self._refresh_all()

    def _set_target(self, ext: Optional[str]) -> None:
        self.target_ext = ext
        self._refresh_all()

    def _allowed_targets(self) -> set[str]:
        if not self.files:
            return set(ALL_FORMATS) | {"gif"}
        allowed: Optional[set[str]] = None
        for item in self.files:
            if item.kind == "image":
                s = set(IMAGE_FORMATS)
            elif item.kind == "audio":
                s = set(AUDIO_FORMATS)
            elif item.kind == "video":
                s = set(VIDEO_FORMATS) | set(AUDIO_FORMATS) | {"gif"}
            else:
                s = set()
            allowed = s if allowed is None else allowed & s
        return allowed or set()

    def _allowed_groups(self) -> set[str]:
        kinds = {f.kind for f in self.files}
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

    def _refresh_target(self) -> None:
        allowed = self._allowed_targets()
        groups = self._allowed_groups()

        # Keep the segmented button on a usable group (prefer the queue's own kind).
        if self.target_group not in groups and groups:
            kinds = [f.kind for f in self.files]
            preferred = next((k for k in kinds if k in groups), None)
            self.target_group = preferred or sorted(groups)[0]
        for seg in self.group_buttons.segments:
            seg.disabled = seg.value not in groups
        self.group_buttons.selected = [self.target_group]

        if self.target_ext and self.target_ext not in allowed:
            self.target_ext = None

        chips = []
        for ext in TARGET_GROUPS[self.target_group]:
            chips.append(ft.Chip(
                label=ext.upper(),
                selected=ext == self.target_ext,
                disabled=ext not in allowed,
                show_checkmark=True,
                on_select=lambda e, x=ext: self._set_target(x if e.control.selected else None),
            ))
        self.format_chips.controls = chips

        kinds = {f.kind for f in self.files}
        hint = ""
        if self.files and not allowed:
            hint = "В очереди файлы разных типов — общий формат подобрать нельзя."
        elif kinds == {"audio", "video"}:
            hint = "Аудио и видео вместе — можно сохранить только звук."
        elif kinds == {"video"} and self.target_group == "video":
            hint = "GIF — анимация без звука, до 720p."
        self.format_hint.value = hint
        self.format_hint.visible = bool(hint)
        self.format_hint.color = ft.Colors.ERROR if (self.files and not allowed) else ft.Colors.ON_SURFACE_VARIANT

    # ----------------------------------------------------------- presets
    def _build_preset_section(self) -> ft.Container:
        self.preset_dropdown = ft.Dropdown(
            label="Готовый рецепт",
            value="none",
            dense=True,
            border_radius=8,
            leading_icon=ft.Icons.AUTO_AWESOME_OUTLINED,
            on_select=self._on_preset,
        )
        self.preset_desc = label("", visible=False)
        return section(
            "Пресеты", ft.Icons.AUTO_AWESOME_OUTLINED,
            full_width(self.preset_dropdown),
            self.preset_desc,
            subtitle="Выставят формат и качество за вас",
        )

    def _refresh_presets(self) -> None:
        allowed = self._allowed_targets()
        opts = [ft.DropdownOption(key="none", text="— не выбран —")]
        for p in PRESETS:
            if p.target_ext in allowed:
                opts.append(ft.DropdownOption(key=p.key, text=p.label))
        self.preset_dropdown.options = opts
        if self.preset_dropdown.value not in {o.key for o in opts}:
            self.preset_dropdown.value = "none"
            self.preset_desc.visible = False

    def _on_preset(self, e) -> None:
        preset = get_preset(e.control.value or "")
        if preset is None:
            self.preset_desc.visible = False
            return
        new = default_settings()
        for name in _STICKY_FIELDS:
            setattr(new, name, getattr(self.settings, name))
        preset.apply(new)
        self.settings = new
        self.panel.sync()
        self.target_group = next(
            (g for g in ("video", "audio", "image")
             if preset.target_ext in TARGET_GROUPS[g] and g in self._allowed_groups()),
            self.target_group,
        )
        self.target_ext = preset.target_ext
        self.preset_desc.value = preset.description
        self.preset_desc.visible = True
        self._refresh_all()

    # ------------------------------------------------------------- queue
    def _build_queue_pane(self) -> ft.Container:
        self.queue_count = ft.Container(
            visible=False,
            padding=ft.Padding.symmetric(horizontal=8, vertical=2),
            border_radius=10,
            bgcolor=ft.Colors.SECONDARY_CONTAINER,
            content=ft.Text("0", theme_style=ft.TextThemeStyle.LABEL_MEDIUM,
                            color=ft.Colors.ON_SECONDARY_CONTAINER),
        )
        self.add_btn = ft.FilledTonalButton("Добавить", icon=ft.Icons.ADD_ROUNDED,
                                            on_click=self._pick_files)
        self.clear_btn = ft.TextButton("Очистить", icon=ft.Icons.CLEAR_ALL_ROUNDED,
                                       on_click=self._clear_files)
        self.queue_list = ft.ListView(expand=True, spacing=8)
        self.queue_empty = ft.Container(
            expand=True,
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=12,
                controls=[
                    ft.Container(
                        width=72, height=72, border_radius=36,
                        bgcolor=ft.Colors.PRIMARY_CONTAINER,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(ft.Icons.UPLOAD_FILE_ROUNDED, size=36,
                                        color=ft.Colors.ON_PRIMARY_CONTAINER),
                    ),
                    ft.Text("Очередь пуста", theme_style=ft.TextThemeStyle.TITLE_MEDIUM),
                    label("Добавьте картинки, аудио или видео — тип определится сам",
                          style=ft.TextThemeStyle.BODY_MEDIUM),
                    ft.FilledButton("Выбрать файлы", icon=ft.Icons.ADD_ROUNDED,
                                    on_click=self._pick_files),
                ],
            ),
        )
        return pane(
            ft.Row(
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.QUEUE_ROUNDED, color=ft.Colors.PRIMARY),
                    ft.Text("Очередь", theme_style=ft.TextThemeStyle.TITLE_MEDIUM),
                    self.queue_count,
                    ft.Container(expand=True),
                    self.clear_btn,
                    self.add_btn,
                ],
            ),
            ft.Stack([self.queue_empty, self.queue_list], expand=True),
            expand=True,
        )

    async def _pick_files(self, _e=None) -> None:
        files = await self.file_picker.pick_files(
            dialog_title="Выберите файлы", allow_multiple=True,
        )
        if files:
            self.add_paths([Path(f.path) for f in files if f.path])

    async def pick_out_dir(self) -> None:
        path = await self.file_picker.get_directory_path(dialog_title="Куда сохранять")
        if path:
            self.set_out_dir(Path(path))

    def set_out_dir(self, path: Optional[Path], save: bool = True) -> None:
        self.out_dir = path
        self.panel.show_out_dir(path)
        if save:
            self._save_pref("out_dir", str(path) if path else None)
        self.page.update()

    def set_notify(self, value: bool, save: bool = True) -> None:
        self.notify_enabled = value
        if save:
            self._save_pref("notify", value)

    def add_paths(self, paths: list[Path]) -> None:
        if self.running:
            return
        known = {f.path for f in self.files}
        added: list[FileItem] = []
        skipped: list[str] = []
        for path in paths:
            if path in known:
                continue
            item = FileItem(path)
            if item.kind is None:
                skipped.append(path.name)
                continue
            self.files.append(item)
            known.add(path)
            added.append(item)
        if skipped:
            self.toast(f"Не поддерживается: {', '.join(skipped[:3])}"
                       + (f" и ещё {len(skipped) - 3}" if len(skipped) > 3 else ""), error=True)
        if not added:
            return
        if self.selected is None:
            self._select(added[0], refresh=False)
        self._refresh_all()
        threading.Thread(target=self._probe_items, args=(added,), daemon=True).start()

    def _probe_items(self, items: list[FileItem]) -> None:
        """Read duration + streams once per file (never on the UI thread)."""
        for item in items:
            if item.kind in ("audio", "video"):
                item.duration_s = probe_duration(item.path) or 0.0
                if item.kind == "video":
                    item.streams = probe_streams(item.path)
            else:
                item.duration_s = 0.0
            item.estimated_size = self._estimate(item)
            if item is self.selected:
                self.preview.refresh_subtitle()
                self.preview.refresh_tracks()
            self._update_tile_text(item)
        self.page.update()

    def _clear_files(self, _e=None) -> None:
        if self.running:
            return
        self.files.clear()
        self._select(None, refresh=False)
        self._refresh_all()

    def _remove(self, item: FileItem) -> None:
        if self.running:
            return
        self.files = [f for f in self.files if f is not item]
        if self.selected is item:
            self._select(None, refresh=False)
        self._refresh_all()

    def _select(self, item: Optional[FileItem], refresh: bool = True) -> None:
        if item is not None and item is self.selected:
            item = None  # click again = deselect
        prev, self.selected = self.selected, item
        self.preview.show(item)
        for it in (prev, item):
            if it is not None and it in self.files:
                self._restyle_tile(it)
        if refresh:
            self.page.update()

    # ---------------------------------------------------------- tiles
    def _tile(self, item: FileItem) -> ft.Container:
        subtitle = ft.Text("", theme_style=ft.TextThemeStyle.BODY_SMALL,
                           color=ft.Colors.ON_SURFACE_VARIANT,
                           max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        bar = ft.ProgressBar(value=item.progress, year_2023=False,
                             visible=item.status == "running", border_radius=4)
        pct = ft.Text("", theme_style=ft.TextThemeStyle.LABEL_LARGE, color=ft.Colors.PRIMARY)
        trailing = self._tile_trailing(item, pct)
        tile = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            border_radius=SHAPE_LARGE,
            ink=True,
            on_click=lambda _e, it=item: self._select(it),
            content=ft.Row(
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Container(
                        width=40, height=40, border_radius=20,
                        bgcolor=ft.Colors.SECONDARY_CONTAINER,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(KIND_ICON.get(item.kind, ft.Icons.INSERT_DRIVE_FILE_OUTLINED),
                                        size=20, color=ft.Colors.ON_SECONDARY_CONTAINER),
                    ),
                    ft.Column(
                        spacing=4, expand=True,
                        controls=[
                            ft.Text(item.path.name, theme_style=ft.TextThemeStyle.BODY_LARGE,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            subtitle,
                            bar,
                        ],
                    ),
                    trailing,
                ],
            ),
        )
        item.ui = {"tile": tile, "subtitle": subtitle, "bar": bar, "pct": pct}
        self._restyle_tile(item)
        self._update_tile_text(item)
        return tile

    def _tile_trailing(self, item: FileItem, pct: ft.Text) -> ft.Control:
        st = item.status
        if st == "running":
            return pct
        if st == "ok":
            return ft.Row(spacing=0, tight=True, controls=[
                ft.IconButton(icon=ft.Icons.FOLDER_OPEN_OUTLINED, tooltip="Показать в папке",
                              on_click=lambda _e, p=item.dst: self.reveal_path(p)),
                ft.IconButton(icon=ft.Icons.OPEN_IN_NEW_ROUNDED, tooltip="Открыть",
                              on_click=lambda _e, p=item.dst: self.open_path(p)),
                ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, color=ft.Colors.PRIMARY),
            ])
        if st == "error":
            return ft.Icon(ft.Icons.ERROR_ROUNDED, color=ft.Colors.ERROR, tooltip=item.message)
        if st == "skipped":
            return ft.Icon(ft.Icons.REDO_ROUNDED, color=ft.Colors.OUTLINE, tooltip=item.message)
        if st == "cancelled":
            return ft.Icon(ft.Icons.CANCEL_OUTLINED, color=ft.Colors.OUTLINE, tooltip="Отменено")
        return ft.IconButton(icon=ft.Icons.CLOSE_ROUNDED, tooltip="Убрать из очереди",
                             visible=not self.running,
                             on_click=lambda _e, it=item: self._remove(it))

    def _restyle_tile(self, item: FileItem) -> None:
        tile = item.ui.get("tile")
        if tile is not None:
            tile.bgcolor = (ft.Colors.SECONDARY_CONTAINER if item is self.selected
                            else ft.Colors.SURFACE_CONTAINER_HIGH)

    def _update_tile_text(self, item: FileItem) -> None:
        sub: Optional[ft.Text] = item.ui.get("subtitle")
        if sub is None:
            return
        if item.status in ("error", "skipped"):
            sub.value = item.message.splitlines()[0] if item.message else "Ошибка"
            sub.color = ft.Colors.ERROR if item.status == "error" else ft.Colors.ON_SURFACE_VARIANT
            return
        sub.color = ft.Colors.ON_SURFACE_VARIANT
        try:
            size = human_size(item.path.stat().st_size)
        except OSError:
            size = "?"
        bits = [KIND_LABEL.get(item.kind or "", "Файл"), item.path.suffix.lstrip(".").upper(), size]
        if item.duration_s:
            bits.append(fmt_time(item.duration_s))
        text = " · ".join(bits)
        if item.status == "pending" and item.estimated_size and self.target_ext:
            text += f" · станет ~{human_size(item.estimated_size)}"
        if item.status == "ok" and item.dst is not None:
            try:
                text += f" · готово: {item.dst.name} · {human_size(item.dst.stat().st_size)}"
            except OSError:
                pass
            if item.checksum:
                algo = (self.settings.compute_checksum or "").upper()
                text += f" · {algo} {item.checksum[:10]}…"
        sub.value = text

    def _rebuild_tile(self, item: FileItem) -> None:
        old = item.ui.get("tile")
        new = self._tile(item)
        try:
            idx = self.queue_list.controls.index(old)
            self.queue_list.controls[idx] = new
        except ValueError:
            pass

    def _refresh_queue(self) -> None:
        self.queue_list.controls = [self._tile(it) for it in self.files]
        has = bool(self.files)
        self.queue_empty.visible = not has
        self.queue_list.visible = has
        self.queue_count.visible = has
        self.queue_count.content.value = str(len(self.files))
        self.clear_btn.visible = has
        self.clear_btn.disabled = self.add_btn.disabled = self.running

    # ======================================================= settings → UI
    def on_settings_changed(self, estimate: bool = True) -> None:
        """Called by SettingsPanel after the user tweaks anything."""
        if estimate:
            self._refresh_estimates()
            for item in self.files:
                self._update_tile_text(item)
        self.page.update()

    def _estimate(self, item: FileItem) -> Optional[int]:
        if not self.target_ext or item.duration_s is None:
            return None
        try:
            return estimate_output_size_bytes(item.path, self.target_ext, self.settings,
                                              duration_s=item.duration_s)
        except Exception:
            return None

    def _refresh_estimates(self) -> None:
        for item in self.files:
            item.estimated_size = self._estimate(item)

    def _refresh_all(self) -> None:
        self._refresh_target()
        self._refresh_presets()
        self._refresh_estimates()
        self.panel.update_visibility({f.kind for f in self.files if f.kind}, self.target_ext)
        self._refresh_queue()
        self._refresh_action_bar()
        self.page.update()

    # ========================================================== action bar
    def _build_action_bar(self) -> ft.Container:
        self.status_text = ft.Text("Добавьте файлы и выберите формат",
                                   theme_style=ft.TextThemeStyle.TITLE_SMALL,
                                   max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self.eta_text = label("", style=ft.TextThemeStyle.BODY_SMALL)
        self.progress = ft.ProgressBar(value=0, year_2023=False, visible=False, border_radius=4)
        self.pause_btn = ft.FilledTonalIconButton(
            icon=ft.Icons.PAUSE_ROUNDED, tooltip="Пауза", visible=False,
            on_click=self._toggle_pause,
        )
        self.cancel_btn = ft.OutlinedButton(
            "Отменить", icon=ft.Icons.STOP_ROUNDED, visible=False,
            on_click=lambda _e: self.cancel(),
        )
        self.open_dir_btn = ft.TextButton(
            "Открыть папку", icon=ft.Icons.FOLDER_OPEN_OUTLINED, visible=False,
            on_click=lambda _e: self._last_output_dir and self.reveal_path(self._last_output_dir),
        )
        self.convert_btn = ft.FilledButton(
            "Конвертировать",
            icon=ft.Icons.PLAY_ARROW_ROUNDED,
            height=56,
            style=ft.ButtonStyle(
                padding=ft.Padding.symmetric(horizontal=28),
                shape=ft.RoundedRectangleBorder(radius=SHAPE_LARGE),
                text_style=ft.TextStyle(size=16, weight=ft.FontWeight.W_500),
            ),
            on_click=lambda _e: self.start(),
        )
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=20, vertical=14),
            border_radius=SHAPE_EXTRA_LARGE,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Column(expand=True, spacing=6,
                              controls=[self.status_text, self.eta_text, self.progress]),
                    self.open_dir_btn,
                    self.pause_btn,
                    self.cancel_btn,
                    self.convert_btn,
                ],
            ),
        )

    def _refresh_action_bar(self) -> None:
        ready = bool(self.files) and bool(self.target_ext) and not self.running
        self.convert_btn.visible = not self.running
        self.convert_btn.disabled = not ready
        self.pause_btn.visible = self.running and psutil is not None
        self.cancel_btn.visible = self.running
        self.progress.visible = self.running
        self.left_pane.disabled = self.running
        if not self.running:
            if not self.files:
                self.status_text.value = "Добавьте файлы и выберите формат"
            elif not self.target_ext:
                self.status_text.value = "Выберите формат"
            elif not any(f.status in ("ok", "error", "skipped", "cancelled") for f in self.files):
                n = len(self.files)
                files_word = "файл" if n % 10 == 1 and n % 100 != 11 else (
                    "файла" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "файлов")
                self.status_text.value = f"Готово к конвертации: {n} {files_word} в {self.target_ext.upper()}"
                self.eta_text.value = ""

    # ============================================================ convert
    def start(self) -> None:
        if self.running or not self.files or not self.target_ext:
            return
        if self.target_ext not in self._allowed_targets():
            self.toast("Этот формат не подходит для файлов в очереди.", error=True)
            return
        if self.panel.vid_compat.visible:
            self.toast(self.panel.vid_compat.content.controls[1].value, error=True)
            return

        self.running = True
        self._cancel.clear()
        self._paused = False
        self.preview.stop()
        for item in self.files:
            item.status, item.message, item.dst = "pending", "", None
            item.progress, item.checksum = 0.0, None
        self.open_dir_btn.visible = False
        self.status_text.value = "Подготовка…"
        self.eta_text.value = ""
        self.progress.value = 0
        self._refresh_queue()
        self._refresh_action_bar()
        self.page.update()

        # Snapshot everything: changes in the UI must not leak into a running batch.
        job = (list(self.files), self.target_ext, dataclasses.replace(self.settings), self.out_dir)
        threading.Thread(target=self._run, args=job, daemon=True).start()
        threading.Thread(target=self._ticker, daemon=True).start()

    def cancel(self) -> None:
        if not self.running:
            return
        self._cancel.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            if self._paused and psutil is not None:
                try:
                    psutil.Process(proc.pid).resume()
                except Exception:
                    pass
            try:
                proc.kill()
            except Exception:
                pass
        self.status_text.value = "Отменяю…"
        self.cancel_btn.disabled = True
        self.page.update()

    def _toggle_pause(self, _e=None) -> None:
        proc = self._proc
        if psutil is None or proc is None or proc.poll() is not None:
            return
        try:
            p = psutil.Process(proc.pid)
            if self._paused:
                p.resume()
                self._run_started += time.time() - self._pause_started
            else:
                p.suspend()
                self._pause_started = time.time()
            self._paused = not self._paused
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            self.toast(f"Не удалось: {exc}", error=True)
            return
        self.pause_btn.icon = ft.Icons.PLAY_ARROW_ROUNDED if self._paused else ft.Icons.PAUSE_ROUNDED
        self.pause_btn.tooltip = "Продолжить" if self._paused else "Пауза"
        self.page.update()

    def _resolve_dst(self, item: FileItem, target: str, out_dir: Optional[Path],
                     settings: ConversionSettings, taken: set[Path]) -> Optional[Path]:
        """Apply the overwrite policy. ``taken`` = outputs of this batch so far."""
        folder = out_dir or item.path.parent
        dst = folder / f"{item.path.stem}.{target}"
        if dst.resolve() == item.path.resolve():
            dst = folder / f"{item.path.stem}_converted.{target}"
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

    def _run(self, files: list[FileItem], target: str, settings: ConversionSettings,
             out_dir: Optional[Path]) -> None:
        # Durations are probed in the background on add; finish any stragglers.
        self._job_settings = settings
        for item in files:
            if item.duration_s is None:
                item.duration_s = (probe_duration(item.path) or 0.0) if item.kind != "image" else 0.0
        self._total_s = sum(effective_duration(f.duration_s, settings) or 0 for f in files)
        self._done_s = 0.0
        self._run_started = time.time()

        if target == "pdf" and len(files) > 1 and all(f.kind == "image" for f in files):
            self._run_pdf(files, settings, out_dir)
            return

        ok = failed = skipped = 0
        taken: set[Path] = set()
        last_dst: Optional[Path] = None
        for idx, item in enumerate(files, start=1):
            if self._cancel.is_set():
                item.status = "cancelled"
                self._rebuild_tile(item)
                continue
            self._current = item
            item.status, item.progress = "running", 0.0
            self.status_text.value = f"{idx} из {len(files)} · {item.path.name}"
            self._rebuild_tile(item)
            self.page.update()

            dst = self._resolve_dst(item, target, out_dir, settings, taken)
            if dst is None:
                item.status, item.message = "skipped", "Пропущен: файл уже существует"
                skipped += 1
            else:
                taken.add(dst)
                file_settings = dataclasses.replace(settings, audio_track_index=item.audio_track)
                self._log(f"━━ {item.path.name} → {dst.name}")
                try:
                    convert(
                        item.path, dst,
                        settings=file_settings,
                        on_log=self._log,
                        on_progress=lambda f, it=item: setattr(it, "progress", f),
                        duration_s=item.duration_s or None,
                        on_proc=self._set_proc,
                        is_cancelled=self._cancel.is_set,
                    )
                    item.status, item.dst = "ok", dst
                    last_dst = dst
                    ok += 1
                    if settings.compute_checksum:
                        try:
                            item.checksum = file_checksum(dst, settings.compute_checksum)
                            self._log(f"{settings.compute_checksum.upper()} {dst.name}: {item.checksum}")
                        except OSError as e:
                            self._log(f"Checksum: {e}")
                except ConversionCancelled:
                    item.status = "cancelled"
                except Exception as e:  # noqa: BLE001 — every failure is shown per file
                    item.status, item.message = "error", str(e)
                    failed += 1
                    self._log(f"ОШИБКА: {e}")
                finally:
                    self._proc = None
            self._done_s += effective_duration(item.duration_s, settings) or 0
            item.progress = 1.0
            self._rebuild_tile(item)
            self.page.update()

        self._finish(ok, failed, skipped, len(files), last_dst)

    def _run_pdf(self, files: list[FileItem], settings: ConversionSettings,
                 out_dir: Optional[Path]) -> None:
        folder = out_dir or files[0].path.parent
        dst = folder / "combined.pdf"
        i = 1
        while dst.exists():
            dst = folder / f"combined_{i}.pdf"
            i += 1
        for item in files:
            item.status = "running"
            self._rebuild_tile(item)
        self.status_text.value = f"Собираю PDF из {len(files)} картинок…"
        self.page.update()
        ok = 0
        try:
            images_to_pdf([f.path for f in files], dst, settings)
            for item in files:
                item.status, item.dst = "ok", dst
            ok = len(files)
            self._log(f"PDF собран: {dst}")
        except Exception as e:  # noqa: BLE001
            for item in files:
                item.status, item.message = "error", str(e)
            self._log(f"PDF ОШИБКА: {e}")
        for item in files:
            self._rebuild_tile(item)
        self._finish(ok, len(files) - ok, 0, len(files), dst if ok else None, pdf=True)

    def _finish(self, ok: int, failed: int, skipped: int, total: int,
                last_dst: Optional[Path], pdf: bool = False) -> None:
        cancelled = self._cancel.is_set()
        self.running = False
        self._current = None
        self._paused = False
        self.cancel_btn.disabled = False
        self.pause_btn.icon = ft.Icons.PAUSE_ROUNDED
        if last_dst is not None:
            self._last_output_dir = last_dst.parent
            self.open_dir_btn.visible = True

        elapsed = fmt_time(time.time() - self._run_started)
        if pdf and ok:
            summary = f"PDF готов: {last_dst.name}"
        else:
            summary = f"Готово {ok} из {total}"
            if failed:
                summary += f" · ошибок {failed}"
            if skipped:
                summary += f" · пропущено {skipped}"
            if cancelled:
                summary += " · отменено"
        self.status_text.value = summary
        self.eta_text.value = f"Заняло {elapsed}"
        self.progress.value = 1
        self._refresh_queue()
        self._refresh_action_bar()
        self.page.update()

        if cancelled:
            self.toast("Конвертация отменена.")
        elif failed == 0 and ok:
            self.toast("Готово! PDF собран." if pdf else f"Готово! Сконвертировано файлов: {ok}.")
            self._beep()
        elif ok == 0 and failed:
            self.toast("Не удалось конвертировать ни одного файла. Подробности — в журнале.",
                       error=True)
            self._beep()
        else:
            self.toast(f"{summary}. Ошибки видно в очереди и в журнале.", error=True)
            self._beep()

    def _set_proc(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        if self._cancel.is_set():
            proc.kill()

    def _ticker(self) -> None:
        """Refresh overall progress, ETA, the running tile and the log ~3×/s."""
        while self.running:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(0.33)
        self._flush_log()

    def _tick(self) -> None:
        if self._paused:
            self.eta_text.value = "Пауза"
            self.page.update()
            return
        elapsed = time.time() - self._run_started
        cur = self._current
        if self._total_s > 0:
            cur_s = (effective_duration(cur.duration_s, self._job_settings) or 0) * cur.progress if cur else 0
            frac = (self._done_s + cur_s) / self._total_s
        else:
            n = len(self.files) or 1
            done = sum(1 for f in self.files if f.status not in ("pending", "running"))
            frac = (done + (cur.progress if cur else 0)) / n
        frac = max(0.0, min(1.0, frac))
        self.progress.value = frac
        if frac > 0.02 and elapsed > 1:
            remaining = elapsed / frac - elapsed
            self.eta_text.value = f"{int(frac * 100)}% · прошло {fmt_time(elapsed)} · осталось ~{fmt_time(remaining)}"
        else:
            self.eta_text.value = f"Прошло {fmt_time(elapsed)}"
        if cur is not None and cur.ui:
            cur.ui["bar"].value = cur.progress if cur.progress > 0 else None
            cur.ui["pct"].value = f"{int(cur.progress * 100)}%" if cur.progress > 0 else ""
        self._flush_log()
        self.page.update()

    # ================================================================ log
    def _log(self, line: str) -> None:
        with self._log_lock:
            self._log_lines.append(line)
            if len(self._log_lines) > 5000:
                del self._log_lines[:-5000]
            self._log_dirty = True

    def _flush_log(self) -> None:
        if self._log_view is None or not self._log_dirty:
            return
        with self._log_lock:
            self._log_view.value = "\n".join(self._log_lines[-800:])
            self._log_dirty = False

    def _open_log(self, _e=None) -> None:
        with self._log_lock:
            text = "\n".join(self._log_lines[-800:])
        self._log_view = ft.TextField(
            value=text, hint_text="Журнал пока пуст",
            multiline=True, min_lines=18, max_lines=18, read_only=True,
            text_size=12, border=ft.InputBorder.NONE,
            text_style=ft.TextStyle(font_family="monospace"),
        )

        def close(_e=None):
            self._log_view = None
            self.page.pop_dialog()

        def clear(_e=None):
            with self._log_lock:
                self._log_lines.clear()
            if self._log_view:
                self._log_view.value = ""

        self.page.show_dialog(ft.AlertDialog(
            icon=ft.Icon(ft.Icons.TERMINAL_ROUNDED),
            title=ft.Text("Журнал ffmpeg"),
            content=ft.Container(
                width=760, padding=12, border_radius=SHAPE_LARGE,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                content=self._log_view,
            ),
            actions=[
                ft.TextButton("Скопировать", icon=ft.Icons.CONTENT_COPY_ROUNDED,
                              on_click=lambda _e: self.page.run_task(
                                  self._copy, "\n".join(self._log_lines))),
                ft.TextButton("Очистить", on_click=clear),
                ft.FilledTonalButton("Закрыть", on_click=close),
            ],
            on_dismiss=lambda _e: setattr(self, "_log_view", None),
        ))

    # ============================================================ compact
    def _toggle_compact(self, _e=None) -> None:
        self._compact = not self._compact
        c = self._compact
        self.left_col.visible = not c
        self.preview.control.visible = not c
        self.queue_pane.visible = not c
        self.app_title_sub.visible = not c
        self.theme_toggle.visible = not c
        self.palette_menu.visible = not c
        w = self.page.window
        if c:
            self._saved_size = (w.width, w.height, w.min_width, w.min_height)
            w.min_width, w.min_height = 520, 220
            w.width, w.height = 640, 250
            w.always_on_top = True
            self.compact_btn.icon = ft.Icons.OPEN_IN_FULL_ROUNDED
            self.compact_btn.tooltip = "Обычный режим"
        else:
            width, height, min_w, min_h = getattr(self, "_saved_size", (1280, 860, 960, 640))
            w.min_width, w.min_height = min_w, min_h
            w.width, w.height = width, height
            w.always_on_top = False
            self.compact_btn.icon = ft.Icons.PICTURE_IN_PICTURE_ALT_OUTLINED
            self.compact_btn.tooltip = "Компактный режим поверх окон"
        self.page.update()

    # ============================================================= window
    async def _on_window_event(self, e) -> None:
        if e.type == ft.WindowEventType.CLOSE:
            self.shutdown()
            await self.page.window.destroy()

    def shutdown(self) -> None:
        """Kill a running ffmpeg so nothing is left behind after the window closes."""
        self._cancel.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                if psutil is not None and self._paused:
                    psutil.Process(proc.pid).resume()
                proc.kill()
            except Exception:
                pass
        self.preview.stop()

    # ======================================================== OS helpers
    def toast(self, message: str, error: bool = False) -> None:
        self.page.show_dialog(ft.SnackBar(
            content=ft.Text(message, color=ft.Colors.ON_ERROR_CONTAINER if error else None),
            bgcolor=ft.Colors.ERROR_CONTAINER if error else None,
            behavior=ft.SnackBarBehavior.FLOATING,
            show_close_icon=True,
            width=520,
        ))

    def open_path(self, path: Optional[Path]) -> None:
        if path is None or not Path(path).exists():
            self.toast("Файл не найден.", error=True)
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            self.toast(f"Не удалось открыть: {exc}", error=True)

    def reveal_path(self, path: Optional[Path]) -> None:
        if path is None:
            return
        path = Path(path)
        target = path if path.exists() else path.parent
        try:
            if sys.platform == "win32":
                if target.is_file():
                    subprocess.Popen(["explorer", f"/select,{target}"])
                else:
                    os.startfile(str(target))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(target)] if target.is_file()
                                 else ["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target.parent if target.is_file() else target)])
        except Exception as exc:  # noqa: BLE001
            self.toast(f"Не удалось открыть папку: {exc}", error=True)

    def _beep(self) -> None:
        if not self.notify_enabled:
            return
        try:
            if sys.platform == "win32":
                import winsound
                winsound.MessageBeep(winsound.MB_OK)
            elif sys.platform == "darwin":
                subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
            else:
                subprocess.Popen(["canberra-gtk-play", "-i", "complete"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


async def main(page: ft.Page) -> None:
    app = ConverterApp(page)
    await app.load_prefs()


if __name__ == "__main__":
    ft.run(main)
