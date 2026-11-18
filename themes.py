"""Named Material You themes, fonts and the AMOLED dark variant.

Each theme is a seed color (the whole M3 tonal palette is generated from it)
plus its own set of Expressive backdrop shapes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import flet as ft


@dataclass(frozen=True)
class AppTheme:
    key: str
    name: str            # Russian source string, translated with tr()
    seed: str
    shapes: tuple[str, str, str, str]   # backdrop shapes, see shapes.Backdrop


THEMES: list[AppTheme] = [
    AppTheme("lavender", "Лаванда", "#6750A4", ("cookie12", "clover4", "soft_burst", "pentagon")),
    AppTheme("ocean", "Океан", "#0B57D0", ("cookie6", "pill", "sunny", "hexagon")),
    AppTheme("forest", "Лес", "#386A20", ("flower", "clover4", "clover8", "triangle")),
    AppTheme("sunset", "Закат", "#B3541E", ("sunny", "burst", "soft_burst", "circle")),
    AppTheme("sakura", "Сакура", "#984061", ("flower", "cookie9", "cookie4", "soft_burst")),
    AppTheme("mint", "Мята", "#006A6A", ("hexagon", "cookie7", "pill", "square")),
    AppTheme("graphite", "Графит", "#5F6368", ("square", "pentagon", "hexagon", "triangle")),
]
DEFAULT_THEME = THEMES[0]

# Family name → bundled file (relative to the assets dir). None = platform font.
FONTS: dict[str, Optional[str]] = {
    "Balsamiq Sans": "fonts/BalsamiqSans-Regular.ttf",
    "Google Sans": "fonts/GoogleSans-Regular.ttf",
    "System": None,
}
DEFAULT_FONT = "Balsamiq Sans"


def get_theme(key: Optional[str]) -> AppTheme:
    return next((t for t in THEMES if t.key == key), DEFAULT_THEME)


def theme_for_seed(seed: Optional[str]) -> AppTheme:
    """Map the seed stored by older versions to the closest named theme."""
    if not seed:
        return DEFAULT_THEME
    return next((t for t in THEMES if t.seed.lower() == seed.lower()), DEFAULT_THEME)


def page_fonts() -> dict[str, str]:
    return {name: path for name, path in FONTS.items() if path}


# Pure-black surfaces for OLED screens; accents still come from the seed.
_AMOLED = ft.ColorScheme(
    surface="#000000",
    surface_dim="#000000",
    surface_bright="#202020",
    surface_container_lowest="#000000",
    surface_container_low="#080808",
    surface_container="#0F0F0F",
    surface_container_high="#161616",
    surface_container_highest="#1E1E1E",
)


def build_theme(theme: AppTheme, font: Optional[str], *, dark: bool = False,
                amoled: bool = False) -> ft.Theme:
    family = font if font in FONTS and FONTS[font] else None
    return ft.Theme(
        color_scheme_seed=theme.seed,
        color_scheme=_AMOLED if (dark and amoled) else None,
        font_family=family,
        use_material3=True,
        visual_density=ft.VisualDensity.STANDARD,
    )
