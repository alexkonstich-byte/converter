"""Universal media converter — Material 3 / Material You GUI on Flet 1.x."""
from __future__ import annotations

import atexit
import dataclasses
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import flet as ft

import dialogs
import os_utils
from converter import (
    PRESETS,
    SUBTITLE_FORMATS,
    ConversionCancelled,
    ConversionSettings,
    StreamInfo,
    concat_files,
    convert,
    default_settings,
    effective_duration,
    estimate_output_size_bytes,
    ffmpeg_available,
    file_checksum,
    get_preset,
    humanize_error,
    images_to_pdf,
    media_kind,
    probe_duration,
    probe_streams,
    render_name,
    render_sample,
    target_kind,
)
from preview import PreviewPane, cleanup_thumbnails
from queue_logic import (
    STICKY_FIELDS,
    TARGET_GROUPS,
    allowed_groups,
    allowed_targets,
    auto_jobs,
    can_concat,
    files_word,
    output_path,
    scan_folder,
    settings_from_dict,
    settings_to_dict,
)
from settings_panel import SettingsPanel
from shapes import Backdrop, ExpressiveLoader, ShapeBadge, expressive_button_style, make_spinner
from ui_kit import (
    DEFAULT_SEED,
    KIND_ICON,
    KIND_LABEL,
    PANE_BG,
    SEED_COLORS,
    SHAPE_EXTRA_LARGE,
    SHAPE_LARGE,
    build_theme,
    fmt_time,
    human_size,
    label,
    pane,
    section,
)

try:
    import psutil
except ImportError:  # pause needs psutil
    psutil = None

try:  # OS drag & drop — needs the custom client built by `flet build`
    import flet_dropzone
except ImportError:
    flet_dropzone = None

GROUP_LABEL = {"image": "Картинка", "audio": "Аудио", "video": "Видео"}
PREF_PREFIX = "converter."
USER_PRESET_PREFIX = "user:"


@dataclass(eq=False)
class FileItem:
    path: Path
    root: Optional[Path] = None          # folder it was added from (keep structure)
    kind: Optional[str] = None
    status: str = "pending"  # pending | running | ok | error | skipped | cancelled
    message: str = ""
    dst: Optional[Path] = None
    duration_s: Optional[float] = None   # None = not probed yet
    streams: list[StreamInfo] = field(default_factory=list)
    audio_track: Optional[int] = None    # per-file audio stream choice
    subtitle_track: Optional[int] = None  # per-file subtitle stream choice
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
        self.jobs = 0              # 0 = auto
        self.concat = False
        self.user_presets: dict[str, dict] = {}
        self._text_focus = False
        self._window_focused = True
        self._prefs_loaded = False

        # Run state
        self.running = False
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._procs: dict[int, subprocess.Popen] = {}
        self._active: set[FileItem] = set()
        self._paused = False
        self._pause_started = 0.0
        self._compact = False
        self._run_started = 0.0
        self._total_s = 0.0
        self._done_s = 0.0
        self._job_settings: ConversionSettings = self.settings
        self._batch: list[FileItem] = []
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
        self.spinner = make_spinner(page)  # drives every slowly rotating M3 shape
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
        p.on_keyboard_event = self._on_key
        # Belt and braces: session closed / interpreter exiting → stop ffmpeg.
        p.on_close = lambda _e: self.shutdown()
        atexit.register(self.shutdown)

    # ------------------------------------------------------------ prefs
    async def load_prefs(self) -> None:
        """Restore theme, colors, folder, settings, format and user presets."""
        async def get(key):
            try:
                return await self.prefs.get(PREF_PREFIX + key)
            except Exception:
                return None

        seed, theme = await get("seed"), await get("theme")
        notify, out_dir = await get("notify"), await get("out_dir")
        settings_json, target = await get("settings"), await get("target")
        jobs, presets_json = await get("jobs"), await get("user_presets")

        if isinstance(seed, str) and seed:
            self._apply_seed(seed, save=False)
        if theme in ("system", "light", "dark"):
            self._apply_theme_mode(theme, save=False)
        if isinstance(notify, bool):
            self.notify_enabled = notify
            self.panel.sound_switch.value = notify
        if isinstance(out_dir, str) and out_dir and Path(out_dir).is_dir():
            self.set_out_dir(Path(out_dir), save=False)
        if isinstance(settings_json, str):
            try:
                self.settings = settings_from_dict(json.loads(settings_json))
            except (ValueError, TypeError):
                pass
        if isinstance(jobs, int):
            self.jobs = jobs
            self.panel.jobs.value = str(jobs)
        if isinstance(presets_json, str):
            try:
                data = json.loads(presets_json)
                if isinstance(data, dict):
                    self.user_presets = data
            except ValueError:
                pass
        if isinstance(target, str) and "/" in target:
            group, ext = target.split("/", 1)
            if group in TARGET_GROUPS and ext in TARGET_GROUPS[group]:
                self.target_group, self.target_ext = group, ext
        self.panel.sync()
        self._prefs_loaded = True
        self._refresh_all()

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

    def _save_settings(self) -> None:
        if self._prefs_loaded:
            self._save_pref("settings", json.dumps(settings_to_dict(self.settings),
                                                   ensure_ascii=False))

    def _save_target(self) -> None:
        if self._prefs_loaded:
            self._save_pref("target", f"{self.target_group}/{self.target_ext}"
                            if self.target_ext else None)

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

        self.sample_btn = ft.TextButton(
            "Проба 5 с", icon=ft.Icons.SCIENCE_OUTLINED, visible=False,
            tooltip="Сконвертировать 5 секунд из середины и прикинуть итоговый размер",
            on_click=lambda _e: self._run_sample(),
        )
        self.preview.actions.controls.insert(0, self.sample_btn)

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
        content: ft.Control = ft.Column(
            expand=True, spacing=12,
            controls=[self.app_bar, self.ffmpeg_banner, self.body],
        )
        if flet_dropzone is not None and os_utils.is_packaged():
            content = flet_dropzone.Dropzone(
                content=content, expand=True,
                on_dropped=self._on_dropped,
                on_entered=lambda _e: self._drop_highlight(True),
                on_exited=lambda _e: self._drop_highlight(False),
            )
        self.backdrop = Backdrop(self.spinner)
        self.page.add(
            ft.Stack(
                expand=True,
                controls=[
                    self.backdrop.control,
                    ft.SafeArea(
                        expand=True,
                        content=ft.Container(
                            expand=True,
                            padding=ft.Padding.only(left=16, right=16, bottom=16),
                            content=content,
                        ),
                    ),
                ],
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
        self.hotkeys_btn = ft.IconButton(
            icon=ft.Icons.KEYBOARD_OUTLINED, tooltip="Горячие клавиши (F1)",
            on_click=lambda _e: dialogs.show_hotkeys(self.page),
        )
        self.app_title_sub = label("Картинки · аудио · видео", style=ft.TextThemeStyle.BODY_MEDIUM)
        return ft.Container(
            padding=ft.Padding.symmetric(vertical=12, horizontal=4),
            content=ft.Row(
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
                controls=[
                    ShapeBadge("cookie9", 52, ft.Icons.AUTORENEW_ROUNDED,
                               spinner=self.spinner, speed=0.5, icon_size=26),
                    ft.Column(spacing=0, expand=True, controls=[
                        ft.Text("Конвертер", theme_style=ft.TextThemeStyle.TITLE_LARGE,
                                weight=ft.FontWeight.W_600),
                        self.app_title_sub,
                    ]),
                    self.palette_menu,
                    self.theme_toggle,
                    self.hotkeys_btn,
                    ft.IconButton(icon=ft.Icons.TERMINAL_ROUNDED, tooltip="Журнал ffmpeg (Ctrl+L)",
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
                                          on_click=lambda _e, c=cmd: self.page.run_task(self._copy, c))
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
        self.toast("Скопировано")

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
        self._save_target()
        self._refresh_all()

    def _set_target(self, ext: Optional[str]) -> None:
        self.target_ext = ext
        self._save_target()
        self._refresh_all()

    def _kinds(self) -> list[Optional[str]]:
        return [f.kind for f in self.files]

    def _refresh_target(self) -> None:
        allowed = allowed_targets(self._kinds())
        groups = allowed_groups(self._kinds())

        # Keep the segmented button on a usable group (prefer the queue's own kind).
        if self.target_group not in groups and groups:
            preferred = next((k for k in self._kinds() if k in groups), None)
            self.target_group = preferred or sorted(groups)[0]
        for seg in self.group_buttons.segments:
            seg.disabled = seg.value not in groups
        self.group_buttons.selected = [self.target_group]

        if self.target_ext and (self.target_ext not in allowed
                                or self.target_ext not in TARGET_GROUPS[self.target_group]):
            self.target_ext = None

        chips = []
        for ext in TARGET_GROUPS[self.target_group]:
            chips.append(ft.Chip(
                label=ext.upper(),
                selected=ext == self.target_ext,
                disabled=ext not in allowed,
                show_checkmark=True,
                leading=ft.Icon(ft.Icons.SUBTITLES_OUTLINED, size=16)
                if ext in SUBTITLE_FORMATS else None,
                tooltip="Извлечь субтитры" if ext in SUBTITLE_FORMATS else None,
                on_select=lambda e, x=ext: self._set_target(x if e.control.selected else None),
            ))
        self.format_chips.controls = chips

        kinds = set(self._kinds())
        hint = ""
        if self.files and not allowed:
            hint = "В очереди файлы разных типов — общий формат подобрать нельзя."
        elif kinds == {"audio", "video"}:
            hint = "Аудио и видео вместе — можно сохранить только звук."
        elif kinds == {"video"} and self.target_group == "video":
            hint = "GIF — анимация без звука. SRT/ASS/VTT — извлечь субтитры."
        self.format_hint.value = hint
        self.format_hint.visible = bool(hint)
        self.format_hint.color = (ft.Colors.ERROR if (self.files and not allowed)
                                  else ft.Colors.ON_SURFACE_VARIANT)

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
        self.preset_save_btn = ft.IconButton(
            icon=ft.Icons.BOOKMARK_ADD_OUTLINED, tooltip="Сохранить текущие настройки как пресет",
            on_click=lambda _e: self._ask_save_preset(),
        )
        self.preset_delete_btn = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE_ROUNDED, tooltip="Удалить мой пресет", visible=False,
            on_click=lambda _e: self._delete_preset(),
        )
        self.preset_desc = label("", visible=False)
        self.preset_dropdown.expand = True
        return section(
            "Пресеты", ft.Icons.AUTO_AWESOME_OUTLINED,
            ft.Row([self.preset_dropdown, self.preset_save_btn, self.preset_delete_btn],
                   spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self.preset_desc,
            subtitle="Готовые и свои рецепты формата и качества",
        )

    def _refresh_presets(self) -> None:
        allowed = allowed_targets(self._kinds())
        opts = [ft.DropdownOption(key="none", text="— не выбран —")]
        for name, data in sorted(self.user_presets.items()):
            if data.get("target") in allowed:
                opts.append(ft.DropdownOption(key=USER_PRESET_PREFIX + name, text=f"★ {name}"))
        for p in PRESETS:
            if p.target_ext in allowed:
                opts.append(ft.DropdownOption(key=p.key, text=p.label))
        self.preset_dropdown.options = opts
        if self.preset_dropdown.value not in {o.key for o in opts}:
            self.preset_dropdown.value = "none"
            self.preset_desc.visible = False
        self.preset_delete_btn.visible = (self.preset_dropdown.value or "").startswith(
            USER_PRESET_PREFIX)
        self.preset_save_btn.disabled = not self.target_ext

    def _on_preset(self, e) -> None:
        key = e.control.value or ""
        new = default_settings()
        for name in STICKY_FIELDS:
            setattr(new, name, getattr(self.settings, name))
        if key.startswith(USER_PRESET_PREFIX):
            data = self.user_presets.get(key[len(USER_PRESET_PREFIX):]) or {}
            new = settings_from_dict(data.get("settings", {}), new)
            target, desc = data.get("target"), "Ваш пресет."
        else:
            preset = get_preset(key)
            if preset is None:
                self.preset_desc.visible = False
                self._refresh_presets()
                return
            preset.apply(new)
            target, desc = preset.target_ext, preset.description
        self.settings = new
        self.panel.sync()
        groups = allowed_groups(self._kinds())
        self.target_group = next(
            (g for g in ("video", "audio", "image") if target in TARGET_GROUPS[g] and g in groups),
            self.target_group,
        )
        self.target_ext = target
        self.preset_desc.value = desc
        self.preset_desc.visible = True
        self._save_settings()
        self._save_target()
        self._refresh_all()

    def _ask_save_preset(self) -> None:
        if not self.target_ext:
            self.toast("Сначала выберите формат.", error=True)
            return
        dialogs.ask_preset_name(self.page, self._save_preset, list(self.user_presets))

    def _save_preset(self, name: str) -> None:
        self.user_presets[name] = {
            "target": self.target_ext,
            "settings": settings_to_dict(self.settings, behaviour=False),
        }
        self._save_pref("user_presets", json.dumps(self.user_presets, ensure_ascii=False))
        self._refresh_presets()
        self.preset_dropdown.value = USER_PRESET_PREFIX + name
        self.preset_delete_btn.visible = True
        self.toast(f"Пресет «{name}» сохранён")
        self.page.update()

    def _delete_preset(self) -> None:
        key = self.preset_dropdown.value or ""
        name = key[len(USER_PRESET_PREFIX):]
        if self.user_presets.pop(name, None) is not None:
            self._save_pref("user_presets", json.dumps(self.user_presets, ensure_ascii=False))
            self.preset_dropdown.value = "none"
            self.preset_desc.visible = False
            self._refresh_presets()
            self.toast(f"Пресет «{name}» удалён")
            self.page.update()

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
        self.add_btn = ft.FilledTonalButton("Файлы", icon=ft.Icons.ADD_ROUNDED,
                                            tooltip="Добавить файлы (Ctrl+O)",
                                            style=expressive_button_style(20, 8),
                                            on_click=self._pick_files)
        self.add_dir_btn = ft.OutlinedButton("Папку", icon=ft.Icons.CREATE_NEW_FOLDER_OUTLINED,
                                             tooltip="Добавить папку целиком (Ctrl+Shift+O)",
                                             style=expressive_button_style(20, 8),
                                             on_click=self._pick_folder)
        self.clear_btn = ft.TextButton("Очистить", icon=ft.Icons.CLEAR_ALL_ROUNDED,
                                       on_click=self._clear_files)
        self.queue_list = ft.ReorderableListView(
            expand=True, spacing=8, on_reorder=self._on_reorder,
            show_default_drag_handles=False,
        )
        drop_hint = ("Перетащите файлы или папку сюда"
                     if flet_dropzone is not None and os_utils.is_packaged()
                     else "Добавьте картинки, аудио или видео — тип определится сам")
        self.queue_empty = ft.Container(
            expand=True,
            alignment=ft.Alignment.CENTER,
            border_radius=SHAPE_LARGE,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=12,
                controls=[
                    ShapeBadge("clover4", 104, ft.Icons.UPLOAD_FILE_ROUNDED,
                               spinner=self.spinner, speed=0.3, icon_size=44),
                    ft.Text("Очередь пуста", theme_style=ft.TextThemeStyle.TITLE_MEDIUM),
                    label(drop_hint, style=ft.TextThemeStyle.BODY_MEDIUM),
                    ft.Row(
                        alignment=ft.MainAxisAlignment.CENTER,
                        controls=[
                            ft.FilledButton("Выбрать файлы", icon=ft.Icons.ADD_ROUNDED, height=48,
                                            style=expressive_button_style(24, 10),
                                            on_click=self._pick_files),
                            ft.OutlinedButton("Добавить папку", height=48,
                                              icon=ft.Icons.CREATE_NEW_FOLDER_OUTLINED,
                                              style=expressive_button_style(24, 10),
                                              on_click=self._pick_folder),
                        ],
                    ),
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
                    self.add_dir_btn,
                    self.add_btn,
                ],
            ),
            ft.Stack([self.queue_empty, self.queue_list], expand=True),
            expand=True,
        )

    async def _pick_files(self, _e=None) -> None:
        if self.running:
            return
        files = await self.file_picker.pick_files(
            dialog_title="Выберите файлы", allow_multiple=True,
        )
        if files:
            self.add_paths([Path(f.path) for f in files if f.path])

    async def _pick_folder(self, _e=None) -> None:
        if self.running:
            return
        path = await self.file_picker.get_directory_path(dialog_title="Добавить папку")
        if path:
            self.add_folder(Path(path))

    def add_folder(self, folder: Path) -> None:
        found = scan_folder(folder)
        if not found:
            self.toast("В папке нет поддерживаемых файлов.", error=True)
            return
        self.add_paths(found, root=folder)

    async def pick_out_dir(self) -> None:
        path = await self.file_picker.get_directory_path(dialog_title="Куда сохранять")
        if path:
            self.set_out_dir(Path(path))

    async def pick_subtitle_file(self) -> None:
        files = await self.file_picker.pick_files(
            dialog_title="Файл субтитров", file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["srt", "ass", "ssa", "vtt"],
        )
        if files and files[0].path:
            self.panel.set_subtitle_file(files[0].path)
            self.page.update()

    def set_out_dir(self, path: Optional[Path], save: bool = True) -> None:
        self.out_dir = path
        self.panel.show_out_dir(path)
        if save:
            self._save_pref("out_dir", str(path) if path else None)
        self.page.update()

    def set_notify(self, value: bool) -> None:
        self.notify_enabled = value
        self._save_pref("notify", value)

    def set_jobs(self, value: int) -> None:
        self.jobs = value
        self._save_pref("jobs", value)

    def set_concat(self, value: bool) -> None:
        self.concat = value
        self._refresh_action_bar()
        self.page.update()

    def set_text_focus(self, value: bool) -> None:
        self._text_focus = value

    def has_folder_items(self) -> bool:
        return any(f.root is not None for f in self.files)

    def add_paths(self, paths: list[Path], root: Optional[Path] = None) -> None:
        if self.running:
            return
        known = {f.path for f in self.files}
        added: list[FileItem] = []
        skipped: list[str] = []
        for path in paths:
            if path.is_dir():
                for p in scan_folder(path):
                    if p not in known:
                        item = FileItem(p, root=path)
                        self.files.append(item)
                        known.add(p)
                        added.append(item)
                continue
            if path in known:
                continue
            item = FileItem(path, root=root)
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
        for item in added:
            item.ui["fresh"] = True
        if self.selected is None:
            self._select(added[0], refresh=False)
        self.panel.show_out_dir(self.out_dir)
        self._refresh_all()
        threading.Thread(target=self._slide_in, args=(added,), daemon=True).start()
        threading.Thread(target=self._probe_items, args=(added,), daemon=True).start()

    def _slide_in(self, items: list[FileItem]) -> None:
        """Staggered entrance of freshly added tiles."""
        time.sleep(0.05)
        for item in items:
            tile = item.ui.get("tile")
            item.ui["fresh"] = False
            if tile is None:
                continue
            tile.offset = ft.Offset(0, 0)
            tile.opacity = 1
            try:
                tile.update()
            except Exception:
                pass
            time.sleep(0.04)

    def _on_dropped(self, e) -> None:
        self._drop_highlight(False)
        paths = [Path(f.path) for f in getattr(e, "files", []) if getattr(f, "path", "")]
        if paths:
            self.add_paths(paths)

    def _drop_highlight(self, on: bool) -> None:
        self.queue_pane.bgcolor = ft.Colors.PRIMARY_CONTAINER if on else PANE_BG
        self.queue_pane.update()

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
        self.concat = False
        self.panel.concat_switch.value = False
        self._refresh_all()

    def _remove(self, item: FileItem) -> None:
        if self.running:
            return
        idx = self.files.index(item) if item in self.files else -1
        self.files = [f for f in self.files if f is not item]
        if self.selected is item:
            nxt = self.files[min(idx, len(self.files) - 1)] if self.files and idx >= 0 else None
            self.selected = None
            self._select(nxt, refresh=False)
        self._refresh_all()

    def _on_reorder(self, e) -> None:
        old, new = e.old_index, e.new_index
        if old is None or new is None or self.running:
            return
        item = self.files.pop(old)
        self.files.insert(new, item)
        ctl = self.queue_list.controls.pop(old)
        self.queue_list.controls.insert(new, ctl)
        self.page.update()

    def _select(self, item: Optional[FileItem], refresh: bool = True) -> None:
        if item is not None and item is self.selected:
            item = None  # click again = deselect
        prev, self.selected = self.selected, item
        self.preview.show(item)
        for it in (prev, item):
            if it is not None and it in self.files:
                self._restyle_tile(it)
        self._refresh_sample_btn()
        self.panel.refresh_template_example()
        if refresh:
            self.page.update()

    def _select_neighbour(self, step: int) -> None:
        if not self.files:
            return
        if self.selected not in self.files:
            idx = 0 if step > 0 else len(self.files) - 1
        else:
            idx = max(0, min(len(self.files) - 1, self.files.index(self.selected) + step))
        if self.files[idx] is not self.selected:
            self._select(self.files[idx])

    # ---------------------------------------------------------- tiles
    def _tile(self, item: FileItem, index: int) -> ft.Control:
        subtitle = ft.Text("", theme_style=ft.TextThemeStyle.BODY_SMALL,
                           color=ft.Colors.ON_SURFACE_VARIANT,
                           max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        bar = ft.ProgressBar(value=item.progress, year_2023=False,
                             visible=item.status == "running", border_radius=4)
        pct = ft.Text("", theme_style=ft.TextThemeStyle.LABEL_LARGE, color=ft.Colors.PRIMARY)
        trailing = self._tile_trailing(item, pct)
        handle = ft.ReorderableDragHandle(
            content=ft.Icon(ft.Icons.DRAG_INDICATOR_ROUNDED, color=ft.Colors.OUTLINE),
            mouse_cursor=ft.MouseCursor.GRAB,
            visible=not self.running and len(self.files) > 1,
        )
        fresh = item.ui.get("fresh", False)
        tile = ft.Container(
            padding=ft.Padding.only(left=4, right=12, top=10, bottom=10),
            border_radius=SHAPE_LARGE,
            ink=True,
            animate=ft.Animation(260, ft.AnimationCurve.EASE_OUT),
            offset=ft.Offset(0.15, 0) if fresh else ft.Offset(0, 0),
            opacity=0 if fresh else 1,
            animate_offset=ft.Animation(420, ft.AnimationCurve.EASE_OUT_BACK),
            animate_opacity=ft.Animation(300, ft.AnimationCurve.EASE_OUT),
            tooltip=item.message[:600] if item.status == "error" else None,
            on_click=lambda _e, it=item: self._select(it),
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    handle,
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
                            ft.Text(self._display_name(item), theme_style=ft.TextThemeStyle.BODY_LARGE,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            subtitle,
                            bar,
                        ],
                    ),
                    trailing,
                ],
            ),
        )
        item.ui = {"tile": tile, "subtitle": subtitle, "bar": bar, "pct": pct, "fresh": fresh}
        self._restyle_tile(item)
        self._update_tile_text(item)
        return tile

    @staticmethod
    def _display_name(item: FileItem) -> str:
        if item.root is not None:
            try:
                return item.path.relative_to(item.root).as_posix()
            except ValueError:
                pass
        return item.path.name

    def _tile_trailing(self, item: FileItem, pct: ft.Text) -> ft.Control:
        st = item.status
        if st == "running":
            return pct
        if st == "ok":
            buttons: list[ft.Control] = []
            if item.kind == "image" and item.dst and target_kind(item.dst.suffix) == "image" \
                    and item.dst.suffix.lower() != ".pdf":
                buttons.append(ft.IconButton(
                    icon=ft.Icons.COMPARE_ROUNDED, tooltip="Сравнить до и после",
                    on_click=lambda _e, it=item: dialogs.show_compare(self.page, it.path, it.dst)))
            buttons += [
                ft.IconButton(icon=ft.Icons.FOLDER_OPEN_OUTLINED, tooltip="Показать в папке",
                              on_click=lambda _e, p=item.dst: self.reveal_path(p)),
                ft.IconButton(icon=ft.Icons.OPEN_IN_NEW_ROUNDED, tooltip="Открыть",
                              on_click=lambda _e, p=item.dst: self.open_path(p)),
                ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, color=ft.Colors.PRIMARY),
            ]
            return ft.Row(spacing=0, tight=True, controls=buttons)
        if st == "error":
            return ft.Row(spacing=0, tight=True, controls=[
                ft.IconButton(icon=ft.Icons.TERMINAL_ROUNDED, tooltip="Подробности в журнале",
                              on_click=self._open_log),
                ft.Icon(ft.Icons.ERROR_ROUNDED, color=ft.Colors.ERROR),
            ])
        if st == "skipped":
            return ft.Icon(ft.Icons.REDO_ROUNDED, color=ft.Colors.OUTLINE, tooltip=item.message)
        if st == "cancelled":
            return ft.Icon(ft.Icons.CANCEL_OUTLINED, color=ft.Colors.OUTLINE, tooltip="Отменено")
        return ft.IconButton(icon=ft.Icons.CLOSE_ROUNDED, tooltip="Убрать из очереди (Delete)",
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
            sub.value = humanize_error(item.message) if item.status == "error" else item.message
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
        if item.status == "pending" and item.estimated_size and self.target_ext and not self.concat:
            text += f" · станет ~{human_size(item.estimated_size)}"
        if item.status == "ok" and item.dst is not None:
            try:
                text += f" · готово: {item.dst.name} · {human_size(item.dst.stat().st_size)}"
            except OSError:
                pass
            if item.checksum:
                algo = (self._job_settings.compute_checksum or "").upper()
                text += f" · {algo} {item.checksum[:10]}…"
        sub.value = text

    def _rebuild_tile(self, item: FileItem) -> None:
        old = item.ui.get("tile")
        try:
            idx = self.queue_list.controls.index(old)
        except ValueError:
            return
        self.queue_list.controls[idx] = self._tile(item, idx)

    def _refresh_queue(self) -> None:
        self.queue_list.controls = [self._tile(it, i) for i, it in enumerate(self.files)]
        has = bool(self.files)
        self.queue_empty.visible = not has
        self.queue_list.visible = has
        self.queue_count.visible = has
        self.queue_count.content.value = str(len(self.files))
        self.clear_btn.visible = has
        self.clear_btn.disabled = self.add_btn.disabled = self.add_dir_btn.disabled = self.running

    # ======================================================= settings → UI
    def on_settings_changed(self, estimate: bool = True) -> None:
        """Called by SettingsPanel after the user tweaks anything."""
        if estimate:
            self._refresh_estimates()
            for item in self.files:
                self._update_tile_text(item)
        self._save_settings()
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

    def _refresh_concat(self) -> None:
        possible = can_concat(self._kinds(), self.target_ext)
        self.panel.concat_row.visible = possible
        if not possible:
            self.concat = False
            self.panel.concat_switch.value = False

    def _refresh_sample_btn(self) -> None:
        it = self.selected
        self.sample_btn.visible = bool(
            it and it.kind in ("audio", "video") and self.target_ext
            and target_kind(self.target_ext) in ("audio", "video") and not self.running
            and ffmpeg_available())

    def _refresh_all(self) -> None:
        self._refresh_target()
        self._refresh_presets()
        self._refresh_estimates()
        self._refresh_concat()
        self.panel.update_visibility({k for k in self._kinds() if k}, self.target_ext)
        self._refresh_queue()
        self._refresh_sample_btn()
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
            tooltip="Esc", on_click=lambda _e: self.cancel(),
        )
        self.retry_btn = ft.TextButton(
            "Повторить ошибки", icon=ft.Icons.REPLAY_ROUNDED, visible=False,
            on_click=lambda _e: self.start(only_failed=True),
        )
        self.open_dir_btn = ft.TextButton(
            "Открыть папку", icon=ft.Icons.FOLDER_OPEN_OUTLINED, visible=False,
            on_click=lambda _e: self._last_output_dir and self.reveal_path(self._last_output_dir),
        )
        self.convert_btn = ft.FilledButton(
            "Конвертировать",
            icon=ft.Icons.PLAY_ARROW_ROUNDED,
            height=56,
            tooltip="Ctrl+Enter",
            style=expressive_button_style(
                28, 12,
                padding=ft.Padding.symmetric(horizontal=28),
                text_style=ft.TextStyle(size=16, weight=ft.FontWeight.W_600),
            ),
            on_click=lambda _e: self.start(),
        )
        self.run_loader = ExpressiveLoader(44)
        # "Done" burst: springs in from zero when a batch finishes.
        self.done_badge = ShapeBadge("burst", 44, ft.Icons.CHECK_ROUNDED,
                                     color=ft.Colors.PRIMARY, icon_color=ft.Colors.ON_PRIMARY,
                                     spinner=self.spinner, speed=0.4, icon_size=24)
        self.done_badge.visible = False
        self.done_badge.scale = ft.Scale(0)
        self.done_badge.animate_scale = ft.Animation(700, ft.AnimationCurve.ELASTIC_OUT)
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=20, vertical=14),
            border_radius=SHAPE_EXTRA_LARGE,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    self.run_loader,
                    self.done_badge,
                    ft.Column(expand=True, spacing=6,
                              controls=[self.status_text, self.eta_text, self.progress]),
                    self.retry_btn,
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
        self.convert_btn.content = "Склеить" if self.concat else "Конвертировать"
        self.pause_btn.visible = self.running and psutil is not None
        self.cancel_btn.visible = self.running
        self.progress.visible = self.running
        self.left_pane.disabled = self.running
        if self.running:
            self.retry_btn.visible = False
        if not self.running:
            if not self.files:
                self.status_text.value = "Добавьте файлы и выберите формат"
            elif not self.target_ext:
                self.status_text.value = "Выберите формат"
            elif not any(f.status in ("ok", "error", "skipped", "cancelled") for f in self.files):
                n = len(self.files)
                if self.concat:
                    self.status_text.value = f"Склеить {n} {files_word(n)} в один {self.target_ext.upper()}"
                else:
                    self.status_text.value = (f"Готово к конвертации: {n} {files_word(n)} "
                                              f"в {self.target_ext.upper()}")
                self.eta_text.value = ""

    # ============================================================ convert
    def start(self, only_failed: bool = False) -> None:
        if self.running or not self.files or not self.target_ext:
            return
        if self.target_ext not in allowed_targets(self._kinds()):
            self.toast("Этот формат не подходит для файлов в очереди.", error=True)
            return
        if self.panel.vid_compat.visible:
            self.toast(self.panel.vid_compat.content.controls[1].value, error=True)
            return
        s = self.settings
        if s.subtitle_mode in ("burn_file", "soft_file") and not s.subtitle_file \
                and self.panel.subtitles.visible:
            self.toast("Выберите файл субтитров или отключите их.", error=True)
            return

        batch = [f for f in self.files if f.status == "error"] if only_failed else list(self.files)
        if not batch:
            return
        self.running = True
        self._cancel.clear()
        self._paused = False
        self.preview.stop()
        self.done_badge.visible = False
        self.done_badge.scale = ft.Scale(0)
        self.run_loader.start()
        for item in batch:
            item.status, item.message, item.dst = "pending", "", None
            item.progress, item.checksum = 0.0, None
        self.open_dir_btn.visible = False
        self.retry_btn.visible = False
        self.status_text.value = "Подготовка…"
        self.eta_text.value = ""
        self.progress.value = 0
        self._refresh_queue()
        self._refresh_action_bar()
        self._refresh_sample_btn()
        self.page.update()

        # Snapshot everything: changes in the UI must not leak into a running batch.
        settings = dataclasses.replace(self.settings)
        if not self.panel.subtitles.visible:
            settings.subtitle_mode = None
        job = (batch, self.target_ext, settings, self.out_dir, self.concat and not only_failed)
        threading.Thread(target=self._run, args=job, daemon=True).start()
        threading.Thread(target=self._ticker, daemon=True).start()

    def cancel(self) -> None:
        if not self.running:
            return
        self._cancel.set()
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            if proc.poll() is None:
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
        if psutil is None:
            return
        with self._lock:
            procs = [p for p in self._procs.values() if p.poll() is None]
        if not procs and not self._paused:
            self.toast("Сейчас нечего ставить на паузу — идёт обработка картинок.")
            return
        for proc in procs:
            try:
                p = psutil.Process(proc.pid)
                p.resume() if self._paused else p.suspend()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if self._paused:
            self._run_started += time.time() - self._pause_started
        else:
            self._pause_started = time.time()
        self._paused = not self._paused
        self.pause_btn.icon = ft.Icons.PLAY_ARROW_ROUNDED if self._paused else ft.Icons.PAUSE_ROUNDED
        self.pause_btn.tooltip = "Продолжить" if self._paused else "Пауза"
        self.page.update()

    def _run(self, files: list[FileItem], target: str, settings: ConversionSettings,
             out_dir: Optional[Path], concat: bool) -> None:
        self._job_settings = settings
        self._batch = files
        for item in files:
            if item.duration_s is None:
                item.duration_s = (probe_duration(item.path) or 0.0) if item.kind != "image" else 0.0
        self._total_s = sum(effective_duration(f.duration_s, settings) or 0 for f in files)
        self._done_s = 0.0
        self._run_started = time.time()
        self._procs.clear()
        self._active.clear()

        if target == "pdf" and len(files) > 1 and all(f.kind == "image" for f in files):
            self._run_pdf(files, settings, out_dir)
            return
        if concat:
            self._run_concat(files, target, settings, out_dir)
            return

        taken: set[Path] = set()
        counters = {"ok": 0, "failed": 0, "skipped": 0}
        last: list[Path] = []
        workers = self.jobs or auto_jobs(self._kinds(), target)
        workers = max(1, min(workers, len(files)))

        # Destinations are resolved up front so parallel jobs never collide.
        plan: list[tuple[FileItem, Optional[Path]]] = []
        for idx, item in enumerate(files, start=1):
            dst = output_path(item.path, target, out_dir, item.root, settings, idx, taken)
            if dst is not None:
                taken.add(dst)
            plan.append((item, dst))

        def work(item: FileItem, dst: Optional[Path]) -> None:
            if self._cancel.is_set():
                item.status = "cancelled"
                self._rebuild_tile(item)
                return
            with self._lock:
                self._active.add(item)
            item.status, item.progress = "running", 0.0
            self._rebuild_tile(item)
            self._update_status_line()
            self.page.update()
            if dst is None:
                item.status, item.message = "skipped", "Пропущен: файл уже существует"
                counters["skipped"] += 1
            else:
                file_settings = dataclasses.replace(
                    settings, audio_track_index=item.audio_track,
                    subtitle_track=item.subtitle_track)
                self._log(f"━━ {item.path.name} → {dst.name}")
                key = id(item)
                try:
                    convert(
                        item.path, dst,
                        settings=file_settings,
                        on_log=self._log,
                        on_progress=lambda f, it=item: setattr(it, "progress", f),
                        duration_s=item.duration_s or None,
                        on_proc=lambda p, k=key: self._set_proc(k, p),
                        is_cancelled=self._cancel.is_set,
                    )
                    item.status, item.dst = "ok", dst
                    last.append(dst)
                    counters["ok"] += 1
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
                    counters["failed"] += 1
                    self._log(f"ОШИБКА ({item.path.name}): {e}")
                finally:
                    with self._lock:
                        self._procs.pop(key, None)
            with self._lock:
                self._active.discard(item)
                self._done_s += effective_duration(item.duration_s, settings) or 0
            item.progress = 1.0
            self._rebuild_tile(item)
            self.page.update()

        if workers == 1:
            for item, dst in plan:
                work(item, dst)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for f in [pool.submit(work, item, dst) for item, dst in plan]:
                    f.result()

        self._finish(counters["ok"], counters["failed"], counters["skipped"], len(files),
                     last[-1] if last else None)

    def _update_status_line(self) -> None:
        with self._lock:
            active = [a for a in self._active if a in self._batch]
        total = len(self._batch)
        if not active:
            return
        done = sum(1 for f in self._batch if f.status in ("ok", "error", "skipped", "cancelled"))
        if len(active) == 1:
            self.status_text.value = f"{done + 1} из {total} · {active[0].path.name}"
        elif active:
            self.status_text.value = f"{done} из {total} готово · в работе {len(active)}"

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
        self._finish(ok, len(files) - ok, 0, len(files), dst if ok else None, single=dst.name)

    def _run_concat(self, files: list[FileItem], target: str, settings: ConversionSettings,
                    out_dir: Optional[Path]) -> None:
        first = files[0]
        folder = out_dir or first.path.parent
        stem = render_name(settings.name_template, first.path, target, 1, settings) + "_склейка"
        dst = folder / f"{stem}.{target}"
        i = 1
        while dst.exists():
            dst = folder / f"{stem}_{i}.{target}"
            i += 1
        for item in files:
            item.status = "running"
            self._rebuild_tile(item)
        self.status_text.value = f"Склеиваю {len(files)} {files_word(len(files))}…"
        self.page.update()
        holder = FileItem(dst)
        holder.duration_s = sum(f.duration_s or 0 for f in files)
        with self._lock:
            self._active.add(holder)
        status, message = "ok", ""
        try:
            concat_files(
                [f.path for f in files], dst, settings,
                durations=[f.duration_s or 0 for f in files],
                on_log=self._log,
                on_progress=lambda f: setattr(holder, "progress", f),
                on_proc=lambda p: self._set_proc(id(holder), p),
                is_cancelled=self._cancel.is_set,
            )
            self._log(f"Склеено: {dst}")
        except ConversionCancelled:
            status = "cancelled"
        except Exception as e:  # noqa: BLE001
            status, message = "error", str(e)
            self._log(f"СКЛЕЙКА ОШИБКА: {e}")
        with self._lock:
            self._active.discard(holder)
        for item in files:
            item.status, item.message = status, message
            item.dst = dst if status == "ok" else None
            self._rebuild_tile(item)
        ok = len(files) if status == "ok" else 0
        failed = len(files) if status == "error" else 0
        self._finish(ok, failed, 0, len(files), dst if ok else None, single=dst.name)

    def _finish(self, ok: int, failed: int, skipped: int, total: int,
                last_dst: Optional[Path], single: Optional[str] = None) -> None:
        cancelled = self._cancel.is_set()
        self.running = False
        self._paused = False
        self.run_loader.stop()
        self.cancel_btn.disabled = False
        self.pause_btn.icon = ft.Icons.PAUSE_ROUNDED
        if last_dst is not None:
            self._last_output_dir = last_dst.parent
            self.open_dir_btn.visible = True
        self.retry_btn.visible = failed > 0 and not single

        elapsed = fmt_time(time.time() - self._run_started)
        if single and ok:
            summary = f"Готово: {single}"
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
        self._set_taskbar_progress(None)
        self._refresh_queue()
        self._refresh_action_bar()
        self._refresh_sample_btn()
        self.page.update()

        if ok and not cancelled:
            self._celebrate(error=failed > 0)
        if cancelled:
            self.toast("Конвертация отменена.")
            return
        if failed == 0 and ok:
            msg = f"Готово! {single}" if single else f"Готово! Сконвертировано: {ok} {files_word(ok)}."
            self.toast(msg)
        elif ok == 0 and failed:
            msg = "Не удалось конвертировать ни одного файла."
            self.toast(msg + " Подробности — в журнале.", error=True)
        else:
            msg = f"{summary}."
            self.toast(f"{summary}. Можно повторить только ошибки.", error=True)
        if self.notify_enabled:
            os_utils.beep()
            if not self._window_focused:
                os_utils.notify("Конвертер", msg)

    def _celebrate(self, error: bool = False) -> None:
        """M3 Expressive "done" moment: a burst shape springs in."""
        b = self.done_badge
        b.icon.icon = ft.Icons.PRIORITY_HIGH_ROUNDED if error else ft.Icons.CHECK_ROUNDED
        b.visible = True
        b.scale = ft.Scale(0)
        self.page.update()

        def pop():
            time.sleep(0.05)
            b.scale = ft.Scale(1)
            try:
                b.update()
            except Exception:
                pass
        threading.Thread(target=pop, daemon=True).start()

    def _set_proc(self, key: int, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs[key] = proc
        if self._cancel.is_set():
            proc.kill()
        elif self._paused and psutil is not None:
            try:
                psutil.Process(proc.pid).suspend()
            except Exception:
                pass

    def _set_taskbar_progress(self, value: Optional[float]) -> None:
        try:
            self.page.window.progress_bar = value
        except Exception:
            pass

    def _ticker(self) -> None:
        """Refresh overall progress, ETA, running tiles and the log ~3×/s."""
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
        with self._lock:
            active = list(self._active)
            done_s = self._done_s
        s = self._job_settings
        if self._total_s > 0:
            cur_s = sum((effective_duration(it.duration_s, s) or 0) * it.progress for it in active)
            frac = (done_s + cur_s) / self._total_s
        else:
            n = len(self._batch) or 1
            done = sum(1 for f in self._batch if f.status not in ("pending", "running"))
            frac = (done + sum(it.progress for it in active)) / n
        frac = max(0.0, min(1.0, frac))
        self.progress.value = frac
        self._set_taskbar_progress(frac)
        if frac > 0.02 and elapsed > 1:
            remaining = elapsed / frac - elapsed
            self.eta_text.value = (f"{int(frac * 100)}% · прошло {fmt_time(elapsed)} · "
                                   f"осталось ~{fmt_time(remaining)}")
        else:
            self.eta_text.value = f"Прошло {fmt_time(elapsed)}"
        for it in active:
            if it.ui:
                it.ui["bar"].value = it.progress if it.progress > 0 else None
                it.ui["pct"].value = f"{int(it.progress * 100)}%" if it.progress > 0 else ""
        self._update_status_line()
        self._flush_log()
        self.page.update()

    # ============================================================ sample
    def _run_sample(self) -> None:
        item, target = self.selected, self.target_ext
        if not item or not target or self.running:
            return
        self.sample_btn.disabled = True
        self.sample_btn.content = "Считаю…"
        self.page.update()
        settings = dataclasses.replace(self.settings, audio_track_index=item.audio_track,
                                       subtitle_track=item.subtitle_track)
        if not self.panel.subtitles.visible:
            settings.subtitle_mode = None

        def work():
            try:
                sample, projected = render_sample(item.path, target, settings, seconds=5,
                                                  duration_s=item.duration_s, on_log=self._log)
                dialogs.show_sample_result(self.page, sample, projected, 5, self.open_path)
            except Exception as e:  # noqa: BLE001
                self.toast(f"Проба не удалась: {humanize_error(str(e))}", error=True)
            finally:
                self.sample_btn.disabled = False
                self.sample_btn.content = "Проба 5 с"
                self.page.update()

        threading.Thread(target=work, daemon=True).start()

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

    # ============================================================ hotkeys
    def _on_key(self, e: ft.KeyboardEvent) -> None:
        key = (e.key or "").lower()
        ctrl = e.ctrl or e.meta
        if key == "f1":
            dialogs.show_hotkeys(self.page)
        elif ctrl and key == "o":
            self.page.run_task(self._pick_folder if e.shift else self._pick_files)
        elif ctrl and key == "enter":
            self.start()
        elif ctrl and key == "l":
            self._open_log()
        elif key == "escape":
            if self.running:
                self.cancel()
            elif self.selected is not None:
                self._select(None)
        elif self._text_focus:
            return  # arrows/Delete belong to the focused text field
        elif key == "delete" and self.selected is not None and not self.running:
            self._remove(self.selected)
        elif key in ("arrow down", "arrowdown"):
            self._select_neighbour(1)
        elif key in ("arrow up", "arrowup"):
            self._select_neighbour(-1)

    # ============================================================ compact
    def _toggle_compact(self, _e=None) -> None:
        self._compact = not self._compact
        c = self._compact
        self.left_col.visible = not c
        self.preview.control.visible = not c
        self.queue_pane.visible = not c
        for ctl in (self.app_title_sub, self.theme_toggle, self.palette_menu, self.hotkeys_btn):
            ctl.visible = not c
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
        elif e.type == ft.WindowEventType.FOCUS:
            self._window_focused = True
        elif e.type in (ft.WindowEventType.BLUR, ft.WindowEventType.MINIMIZE):
            self._window_focused = False

    def shutdown(self) -> None:
        """Kill running ffmpeg processes so nothing is left behind after the window closes."""
        self._cancel.set()
        self.spinner.stop()
        self.run_loader.stop()
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            if proc.poll() is None:
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
        err = os_utils.open_path(path) if path is not None else "Файл не найден."
        if err:
            self.toast(err, error=True)

    def reveal_path(self, path: Optional[Path]) -> None:
        if path is None:
            return
        err = os_utils.reveal_path(path)
        if err:
            self.toast(err, error=True)


async def main(page: ft.Page) -> None:
    app = ConverterApp(page)
    await app.load_prefs()


if __name__ == "__main__":
    ft.run(main)
