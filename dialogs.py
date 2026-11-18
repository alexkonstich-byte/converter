"""Modal M3 dialogs: hotkeys, before/after compare, preset name, test render."""
from __future__ import annotations

from i18n import tr
import threading
from pathlib import Path
from typing import Callable, Optional

import flet as ft

from preview import image_preview_bytes
from ui_kit import SHAPE_LARGE, human_size, label

HOTKEYS = [
    ("Ctrl + O", "Добавить файлы"),
    ("Ctrl + Shift + O", "Добавить папку"),
    ("Ctrl + Enter", "Начать конвертацию"),
    ("Esc", "Отменить конвертацию / снять выделение"),
    ("Delete", "Убрать выбранный файл из очереди"),
    ("Стрелки вверх / вниз", "Выбрать предыдущий / следующий файл"),
    ("Ctrl + L", "Журнал ffmpeg"),
    ("F1", "Эта подсказка"),
]


def _close_btn(page: ft.Page, text: str = "") -> ft.Control:
    return ft.TextButton(text or tr("Закрыть"), on_click=lambda _e: page.pop_dialog())


def show_hotkeys(page: ft.Page) -> None:
    rows = [
        ft.Row(controls=[
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                border_radius=8, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                content=ft.Text(tr(keys), theme_style=ft.TextThemeStyle.LABEL_LARGE,
                                font_family="monospace"),
                width=170,
            ),
            ft.Text(tr(desc), expand=True),
        ])
        for keys, desc in HOTKEYS
    ]
    page.show_dialog(ft.AlertDialog(
        icon=ft.Icon(ft.Icons.KEYBOARD_OUTLINED),
        title=ft.Text(tr("Горячие клавиши")),
        content=ft.Container(width=440, content=ft.Column(rows, tight=True, spacing=10)),
        actions=[_close_btn(page, tr("Понятно"))],
    ))


def show_compare(page: ft.Page, before: Path, after: Path) -> None:
    """Before/after split view with a slider (images)."""
    W, H = 760, 440
    before_img = ft.Image(src=b"", width=W, height=H, fit=ft.BoxFit.CONTAIN)
    after_img = ft.Image(src=b"", width=W, height=H, fit=ft.BoxFit.CONTAIN, left=0, top=0)
    clip = ft.Container(
        width=W / 2, height=H, clip_behavior=ft.ClipBehavior.HARD_EDGE,
        content=ft.Stack([after_img], width=W, height=H),
    )
    divider = ft.Container(width=2, height=H, bgcolor=ft.Colors.PRIMARY, left=W / 2 - 1, top=0)
    loading = ft.ProgressRing(year_2023=False)
    stage = ft.Stack(
        width=W, height=H,
        controls=[
            before_img,
            ft.Container(clip, left=0, top=0),
            divider,
            ft.Container(loading, alignment=ft.Alignment.CENTER, width=W, height=H),
        ],
    )

    def on_slide(e):
        frac = float(e.control.value)
        clip.width = max(1, W * frac)
        divider.left = W * frac - 1
        stage.update()

    def size_of(p: Path) -> str:
        try:
            return human_size(p.stat().st_size)
        except OSError:
            return "?"

    slider = ft.Slider(min=0, max=1, value=0.5, year_2023=False, on_change=on_slide, expand=True)
    page.show_dialog(ft.AlertDialog(
        title=ft.Text(tr("До и после")),
        content=ft.Container(width=W, content=ft.Column(tight=True, spacing=8, controls=[
            ft.Container(stage, border_radius=SHAPE_LARGE, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                         bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST),
            ft.Row([
                label(tr("После · {0} · {1}", after.suffix.lstrip('.').upper(), size_of(after))),
                slider,
                label(tr("До · {0} · {1}", before.suffix.lstrip('.').upper(), size_of(before))),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ])),
        actions=[_close_btn(page)],
    ))

    def load():
        try:
            before_img.src = image_preview_bytes(before, 1400)
            after_img.src = image_preview_bytes(after, 1400)
        except Exception as exc:  # noqa: BLE001
            loading.visible = False
            stage.controls.append(ft.Container(
                ft.Text(tr("Не удалось открыть: {0}", exc)), alignment=ft.Alignment.CENTER,
                width=W, height=H))
        loading.visible = False
        page.update()

    threading.Thread(target=load, daemon=True).start()


def ask_preset_name(page: ft.Page, on_save: Callable[[str], None],
                    existing: list[str]) -> None:
    field = ft.TextField(label=tr("Название пресета"), autofocus=True, max_length=40)

    def save(_e=None):
        name = (field.value or "").strip()
        if not name:
            field.error = tr("Введите название")
            field.update()
            return
        page.pop_dialog()
        on_save(name)

    field.on_submit = save
    page.show_dialog(ft.AlertDialog(
        icon=ft.Icon(ft.Icons.BOOKMARK_ADD_OUTLINED),
        title=ft.Text(tr("Сохранить как пресет")),
        content=ft.Container(width=380, content=ft.Column(tight=True, controls=[
            label(tr("Сохранятся формат и все настройки качества.")
                  + (tr(" Пресет с таким именем будет заменён.") if existing else "")),
            field,
        ])),
        actions=[_close_btn(page, tr("Отмена")), ft.FilledButton(tr("Сохранить"), on_click=save)],
    ))


def show_sample_result(page: ft.Page, sample: Path, projected: Optional[int],
                       seconds: float, on_open: Callable[[Path], None]) -> None:
    try:
        size = human_size(sample.stat().st_size)
    except OSError:
        size = "?"
    lines = [ft.Text(tr("Фрагмент {0:g} с: {1}", seconds, size), theme_style=ft.TextThemeStyle.BODY_LARGE)]
    if projected:
        lines.append(ft.Text(tr("Весь файл получится примерно {0}", human_size(projected)),
                             theme_style=ft.TextThemeStyle.TITLE_MEDIUM, color=ft.Colors.PRIMARY))
    lines.append(label(tr("Откройте фрагмент и оцените качество. Если не нравится — поменяйте "
                       "настройки и сделайте пробу ещё раз.")))
    page.show_dialog(ft.AlertDialog(
        icon=ft.Icon(ft.Icons.SCIENCE_OUTLINED),
        title=ft.Text(tr("Пробный рендер готов")),
        content=ft.Container(width=420, content=ft.Column(lines, tight=True, spacing=8)),
        actions=[
            _close_btn(page),
            ft.FilledTonalButton(tr("Открыть фрагмент"), icon=ft.Icons.PLAY_ARROW_ROUNDED,
                                 on_click=lambda _e: on_open(sample)),
        ],
    ))


def show_appearance(app) -> None:
    """Theme gallery, light/dark mode, AMOLED, font and language."""
    from i18n import LANGUAGES, get_language
    from shapes import shape_canvas
    from themes import FONTS, THEMES

    page = app.page

    def theme_card(t) -> ft.Control:
        selected = t.key == app.theme.key
        art = ft.Stack(width=96, height=56, controls=[
            ft.Container(shape_canvas(t.shapes[0], 50, t.seed, 0.95), left=4, top=3),
            ft.Container(shape_canvas(t.shapes[1], 34, t.seed, 0.55), left=48, top=0),
            ft.Container(shape_canvas(t.shapes[2], 26, t.seed, 0.35), left=66, top=30),
        ])
        return ft.Container(
            width=124, padding=10, border_radius=20, ink=True,
            bgcolor=ft.Colors.with_opacity(0.14, t.seed),
            border=ft.Border.all(2, ft.Colors.PRIMARY if selected else ft.Colors.TRANSPARENT),
            animate=ft.Animation(220, ft.AnimationCurve.EASE_OUT),
            on_click=lambda _e, k=t.key: (app.set_theme(k), refresh()),
            content=ft.Column(spacing=6, horizontal_alignment=ft.CrossAxisAlignment.CENTER, controls=[
                art,
                ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=4, controls=[
                    ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, size=16, color=ft.Colors.PRIMARY,
                            visible=selected),
                    ft.Text(tr(t.name), theme_style=ft.TextThemeStyle.LABEL_LARGE),
                ]),
            ]),
        )

    grid = ft.Row(wrap=True, spacing=10, run_spacing=10)

    mode = ft.SegmentedButton(
        selected=[app.theme_mode],
        segments=[
            ft.Segment(value="system", label=tr("Как в системе"), icon=ft.Icons.BRIGHTNESS_AUTO_OUTLINED),
            ft.Segment(value="light", label=tr("Светлая"), icon=ft.Icons.LIGHT_MODE_OUTLINED),
            ft.Segment(value="dark", label=tr("Тёмная"), icon=ft.Icons.DARK_MODE_OUTLINED),
        ],
        on_change=lambda e: app._apply_theme_mode(e.control.selected[0]),
    )
    amoled = ft.Switch(value=app.amoled, on_change=lambda e: app.set_amoled(bool(e.control.value)))
    backdrop = ft.Switch(value=app.backdrop_on,
                         on_change=lambda e: app.set_backdrop(bool(e.control.value)))
    font = ft.SegmentedButton(
        selected=[app.font],
        show_selected_icon=False,
        segments=[
            ft.Segment(
                value=name,
                label=ft.Text(tr("Системный") if name == "System" else name,
                              font_family=None if name == "System" else name),
            )
            for name in FONTS
        ],
        on_change=lambda e: app.set_font(e.control.selected[0]),
    )
    lang = ft.SegmentedButton(
        selected=[get_language()],
        show_selected_icon=False,
        segments=[ft.Segment(value=k, label=v) for k, v in LANGUAGES.items()],
        on_change=lambda e: app.set_language(e.control.selected[0]),
    )

    def heading(text: str) -> ft.Text:
        return ft.Text(text, theme_style=ft.TextThemeStyle.TITLE_SMALL, color=ft.Colors.PRIMARY)

    def refresh():
        grid.controls = [theme_card(t) for t in THEMES]
        try:
            grid.update()
        except Exception:
            pass

    refresh()
    page.show_dialog(ft.AlertDialog(
        icon=ft.Icon(ft.Icons.PALETTE_OUTLINED),
        title=ft.Text(tr("Оформление")),
        content=ft.Container(width=560, content=ft.Column(tight=True, spacing=12, scroll=ft.ScrollMode.AUTO, controls=[
            heading(tr("Тема")),
            grid,
            ft.Row([mode]),
            ft.Row([ft.Text(tr("Чёрный фон в тёмной теме (AMOLED)"), expand=True), amoled]),
            ft.Row([ft.Text(tr("Анимированный фон"), expand=True), backdrop]),
            ft.Divider(),
            heading(tr("Шрифт")),
            ft.Row([font]),
            heading(tr("Язык")),
            ft.Row([lang]),
        ])),
        actions=[ft.FilledTonalButton(tr("Готово"), on_click=lambda _e: page.pop_dialog())],
    ))
