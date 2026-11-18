"""Modal M3 dialogs: hotkeys, before/after compare, preset name, test render."""
from __future__ import annotations

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


def _close_btn(page: ft.Page, text: str = "Закрыть") -> ft.Control:
    return ft.TextButton(text, on_click=lambda _e: page.pop_dialog())


def show_hotkeys(page: ft.Page) -> None:
    rows = [
        ft.Row(controls=[
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                border_radius=8, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                content=ft.Text(keys, theme_style=ft.TextThemeStyle.LABEL_LARGE,
                                font_family="monospace"),
                width=170,
            ),
            ft.Text(desc, expand=True),
        ])
        for keys, desc in HOTKEYS
    ]
    page.show_dialog(ft.AlertDialog(
        icon=ft.Icon(ft.Icons.KEYBOARD_OUTLINED),
        title=ft.Text("Горячие клавиши"),
        content=ft.Container(width=440, content=ft.Column(rows, tight=True, spacing=10)),
        actions=[_close_btn(page, "Понятно")],
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
        title=ft.Text("До и после"),
        content=ft.Container(width=W, content=ft.Column(tight=True, spacing=8, controls=[
            ft.Container(stage, border_radius=SHAPE_LARGE, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                         bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST),
            ft.Row([
                label(f"После · {after.suffix.lstrip('.').upper()} · {size_of(after)}"),
                slider,
                label(f"До · {before.suffix.lstrip('.').upper()} · {size_of(before)}"),
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
                ft.Text(f"Не удалось открыть: {exc}"), alignment=ft.Alignment.CENTER,
                width=W, height=H))
        loading.visible = False
        page.update()

    threading.Thread(target=load, daemon=True).start()


def ask_preset_name(page: ft.Page, on_save: Callable[[str], None],
                    existing: list[str]) -> None:
    field = ft.TextField(label="Название пресета", autofocus=True, max_length=40)

    def save(_e=None):
        name = (field.value or "").strip()
        if not name:
            field.error = "Введите название"
            field.update()
            return
        page.pop_dialog()
        on_save(name)

    field.on_submit = save
    page.show_dialog(ft.AlertDialog(
        icon=ft.Icon(ft.Icons.BOOKMARK_ADD_OUTLINED),
        title=ft.Text("Сохранить как пресет"),
        content=ft.Container(width=380, content=ft.Column(tight=True, controls=[
            label("Сохранятся формат и все настройки качества."
                  + (" Пресет с таким именем будет заменён." if existing else "")),
            field,
        ])),
        actions=[_close_btn(page, "Отмена"), ft.FilledButton("Сохранить", on_click=save)],
    ))


def show_sample_result(page: ft.Page, sample: Path, projected: Optional[int],
                       seconds: float, on_open: Callable[[Path], None]) -> None:
    try:
        size = human_size(sample.stat().st_size)
    except OSError:
        size = "?"
    lines = [ft.Text(f"Фрагмент {seconds:g} с: {size}", theme_style=ft.TextThemeStyle.BODY_LARGE)]
    if projected:
        lines.append(ft.Text(f"Весь файл получится примерно {human_size(projected)}",
                             theme_style=ft.TextThemeStyle.TITLE_MEDIUM, color=ft.Colors.PRIMARY))
    lines.append(label("Откройте фрагмент и оцените качество. Если не нравится — поменяйте "
                       "настройки и сделайте пробу ещё раз."))
    page.show_dialog(ft.AlertDialog(
        icon=ft.Icon(ft.Icons.SCIENCE_OUTLINED),
        title=ft.Text("Пробный рендер готов"),
        content=ft.Container(width=420, content=ft.Column(lines, tight=True, spacing=8)),
        actions=[
            _close_btn(page),
            ft.FilledTonalButton("Открыть фрагмент", icon=ft.Icons.PLAY_ARROW_ROUNDED,
                                 on_click=lambda _e: on_open(sample)),
        ],
    ))
