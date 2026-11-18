"""Material 3 building blocks shared by the whole UI.

Everything here follows the Material 3 spec: tonal surfaces
(surface-container-*), the M3 type scale, M3 shape radii and dynamic color
generated from a single seed (Material You).
"""
from __future__ import annotations

from i18n import tr
from typing import Callable, Optional

import flet as ft

# M3 shape scale.
SHAPE_SMALL = 8
SHAPE_MEDIUM = 12
SHAPE_LARGE = 16
SHAPE_EXTRA_LARGE = 28

# Panes are slightly translucent so the M3 Expressive backdrop shapes peek through.
PANE_BG = ft.Colors.with_opacity(0.86, ft.Colors.SURFACE_CONTAINER_LOW)

KIND_ICON = {
    "image": ft.Icons.IMAGE_OUTLINED,
    "audio": ft.Icons.MUSIC_NOTE_OUTLINED,
    "video": ft.Icons.MOVIE_OUTLINED,
}
KIND_LABEL = {"image": "Изображение", "audio": "Аудио", "video": "Видео"}


def human_size(n: float) -> str:
    for unit in (tr("Б"), tr("КБ"), tr("МБ"), tr("ГБ")):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == tr("Б") else f"{n:.1f} {unit}"
        n /= 1024
    return tr("{0:.1f} ТБ", n)


def fmt_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def pane(*controls: ft.Control, expand: bool | int = False, padding: int = 20,
         spacing: int = 12, scroll: Optional[ft.ScrollMode] = None) -> ft.Container:
    """Large M3 surface (extra-large shape, surface-container-low)."""
    return ft.Container(
        expand=expand,
        padding=padding,
        border_radius=SHAPE_EXTRA_LARGE,
        bgcolor=PANE_BG,
        animate=ft.Animation(300, ft.AnimationCurve.EASE_OUT),
        content=ft.Column(list(controls), spacing=spacing, expand=expand, scroll=scroll),
    )


def section(title: str, icon: ft.IconData, *controls: ft.Control,
            subtitle: Optional[str] = None, visible: bool = True) -> ft.Container:
    """Filled M3 card with a leading icon and Title-Medium heading."""
    header: list[ft.Control] = [
        ft.Container(
            width=36, height=36, border_radius=SHAPE_MEDIUM,
            bgcolor=ft.Colors.SECONDARY_CONTAINER,
            alignment=ft.Alignment.CENTER,
            content=ft.Icon(icon, size=20, color=ft.Colors.ON_SECONDARY_CONTAINER),
        ),
        ft.Column(
            spacing=0, expand=True,
            controls=[ft.Text(title, theme_style=ft.TextThemeStyle.TITLE_MEDIUM)]
            + ([ft.Text(subtitle, theme_style=ft.TextThemeStyle.BODY_SMALL,
                        color=ft.Colors.ON_SURFACE_VARIANT)] if subtitle else []),
        ),
    ]
    return ft.Container(
        visible=visible,
        padding=16,
        border_radius=SHAPE_LARGE,
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
        animate_size=ft.Animation(280, ft.AnimationCurve.EASE_OUT_CUBIC),
        content=ft.Column(
            spacing=12,
            controls=[ft.Row(header, spacing=12,
                             vertical_alignment=ft.CrossAxisAlignment.CENTER), *controls],
        ),
    )


def label(text: str, *, color=ft.Colors.ON_SURFACE_VARIANT,
          style: ft.TextThemeStyle = ft.TextThemeStyle.BODY_SMALL, **kw) -> ft.Text:
    return ft.Text(text, theme_style=style, color=color, **kw)


class HelpButton(ft.IconButton):
    """Small "?" icon; opens an M3 dialog with an explanation."""

    def __init__(self, title: str, message: str):
        super().__init__(
            icon=ft.Icons.HELP_OUTLINE_ROUNDED,
            icon_size=18,
            tooltip=tr("Что это?"),
            icon_color=ft.Colors.ON_SURFACE_VARIANT,
            on_click=self._open,
        )
        self._title = title
        self._message = message

    def _open(self, _e) -> None:
        dlg = ft.AlertDialog(
            icon=ft.Icon(ft.Icons.LIGHTBULB_OUTLINE_ROUNDED),
            title=ft.Text(self._title),
            content=ft.Container(width=420, content=ft.Text(self._message, selectable=True)),
            actions=[ft.TextButton(tr("Понятно"), on_click=lambda _e: self.page.pop_dialog())],
        )
        self.page.show_dialog(dlg)


def full_width(control: ft.Control) -> ft.Row:
    """Stretch a control (e.g. a Dropdown) to the full width of a Column."""
    control.expand = True
    return ft.Row([control])


def with_help(control: ft.Control, title: str, message: str) -> ft.Row:
    control.expand = True  # take all the width left of the "?" button
    return ft.Row(
        spacing=4,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[control, HelpButton(title, message)],
    )


def switch_row(text: str, value: bool, on_change: Callable,
               help_title: Optional[str] = None, help_text: Optional[str] = None
               ) -> tuple[ft.Row, ft.Switch]:
    """M3 list-item style row: label on the left, switch on the right."""
    sw = ft.Switch(value=value, on_change=on_change)
    controls: list[ft.Control] = [
        ft.Text(text, theme_style=ft.TextThemeStyle.BODY_LARGE, expand=True),
        sw,
    ]
    # Without help keep the same right gutter so switches line up.
    controls.append(HelpButton(help_title or text, help_text) if help_text
                    else ft.Container(width=40))
    return ft.Row(controls, vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4), sw


def dropdown(label_text: str, value: str, options: list[tuple[str, str]],
             on_select: Callable, **kw) -> ft.Dropdown:
    return ft.Dropdown(
        label=label_text,
        value=value,
        options=[ft.DropdownOption(key=k, text=t) for k, t in options],
        on_select=on_select,
        dense=True,
        border_radius=SHAPE_SMALL,
        **kw,
    )
