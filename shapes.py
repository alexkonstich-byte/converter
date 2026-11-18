"""Material 3 Expressive shapes and motion.

* M3 shape library (cookie, clover, sunny, soft burst, pentagon, pill…)
  drawn on a Flet canvas in theme colors.
* ``Backdrop`` — huge, slowly turning shapes behind the whole UI.
* ``ExpressiveLoader`` — the Android 16 style loading indicator: a shape
  that keeps turning and springs from one form into the next.
* ``ShapeBadge`` — an icon sitting on a slowly rotating shape (app logo,
  empty states, the "done" burst).

Animations are driven by Flutter implicit animations (``animate_rotation``,
``animate_scale``, ``AnimatedSwitcher``): Python only nudges the target
value every few hundred milliseconds, the client interpolates at 60 fps.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Optional

import flet as ft
import flet.canvas as cv

# ----------------------------------------------------------------- geometry
# radius(θ) for unit-size shapes; θ in radians. Values ~0.5 = half the box.

def _wavy(lobes: int, depth: float):
    """Cookie / flower: a circle whose edge waves `lobes` times."""
    return lambda t: 0.5 * (1 - depth) + 0.5 * depth * (0.5 + 0.5 * math.cos(lobes * t))


def _polygon(sides: int):
    """Sharp regular polygon (corners get rounded later by smoothing)."""
    seg = 2 * math.pi / sides

    def r(t: float) -> float:
        local = ((t + math.pi / 2) % seg) - seg / 2  # a corner points up
        return 0.5 * math.cos(math.pi / sides) / math.cos(local)
    return r


def _burst(spikes: int, depth: float):
    """Soft burst: sharper waves (|cos|^0.6)."""
    return lambda t: 0.5 * (1 - depth) + 0.5 * depth * abs(math.cos(spikes * t / 2)) ** 0.6


def _pill(t: float) -> float:
    """Superellipse-ish capsule, 1:0.62."""
    a, b, n = 0.5, 0.31, 4.0
    c, s = abs(math.cos(t)), abs(math.sin(t))
    return 1.0 / ((c / a) ** n + (s / b) ** n) ** (1 / n)


# How much to round corners (fraction of the outline averaged per point).
_SMOOTH = {"triangle": 0.07, "square": 0.06, "pentagon": 0.055, "hexagon": 0.05}

SHAPES = {
    "circle": lambda t: 0.5,
    "cookie4": _wavy(4, 0.16),
    "cookie6": _wavy(6, 0.12),
    "cookie7": _wavy(7, 0.11),
    "cookie9": _wavy(9, 0.09),
    "cookie12": _wavy(12, 0.08),
    "clover4": _wavy(4, 0.34),
    "clover8": _wavy(8, 0.22),
    "sunny": _wavy(8, 0.07),
    "flower": _wavy(5, 0.28),
    "burst": _burst(12, 0.22),
    "soft_burst": _burst(10, 0.14),
    "triangle": _polygon(3),
    "square": _polygon(4),
    "pentagon": _polygon(5),
    "hexagon": _polygon(6),
    "pill": _pill,
}

# The morph sequence of the M3 Expressive loading indicator.
LOADER_SEQUENCE = ["soft_burst", "cookie9", "pentagon", "pill", "sunny", "cookie4", "circle"]


_CACHE: dict[tuple[str, int], list[tuple[float, float]]] = {}


def shape_points(kind: str, points: int = 120) -> list[tuple[float, float]]:
    """Unit outline centred at (0.5, 0.5), fitted to the 0..1 box."""
    key = (kind, points)
    if key in _CACHE:
        return _CACHE[key]
    r = SHAPES.get(kind, SHAPES["circle"])
    n = points * 4
    pts = [(r(t) * math.cos(t), r(t) * math.sin(t))
           for t in (2 * math.pi * i / n for i in range(n))]
    k = int(_SMOOTH.get(kind, 0) * n)
    if k:  # moving average along the outline → round corners, straight edges
        sm = []
        for i in range(n):
            xs = ys = 0.0
            for j in range(-k, k + 1):
                x, y = pts[(i + j) % n]
                xs += x
                ys += y
            sm.append((xs / (2 * k + 1), ys / (2 * k + 1)))
        pts = sm
    pts = pts[::4]
    # center + fit into the unit box
    minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
    miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
    scale = 1 / max(maxx - minx, maxy - miny)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    out = [(0.5 + (x - cx) * scale, 0.5 + (y - cy) * scale) for x, y in pts]
    _CACHE[key] = out
    return out


def shape_path(kind: str, size: float, points: int = 120) -> list:
    elems: list = []
    for i, (x, y) in enumerate(shape_points(kind, points)):
        elems.append(cv.Path.MoveTo(x * size, y * size) if i == 0
                     else cv.Path.LineTo(x * size, y * size))
    elems.append(cv.Path.Close())
    return elems


def shape_canvas(kind: str, size: float, color, opacity: float = 1.0) -> cv.Canvas:
    return cv.Canvas(
        width=size, height=size,
        shapes=[cv.Path(shape_path(kind, size),
                        paint=ft.Paint(color=ft.Colors.with_opacity(opacity, color),
                                       style=ft.PaintingStyle.FILL,
                                       anti_alias=True))],
    )


# ------------------------------------------------------------------ motion
def _mounted(ctl: ft.Control) -> bool:
    try:
        return ctl.page is not None
    except RuntimeError:  # Flet 1.x raises for controls not on a page yet
        return False


class _Spinner:
    """One background thread that keeps a set of controls turning."""

    def __init__(self, page: ft.Page):
        self.page = page
        self.items: list[tuple[ft.Control, float]] = []  # (control, radians per second)
        self.tick = 4.0
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def add(self, control: ft.Control, rad_per_s: float) -> None:
        control.rotate = ft.Rotate(angle=0, alignment=ft.Alignment.CENTER)
        control.animate_rotation = ft.Animation(int(self.tick * 1000), ft.AnimationCurve.LINEAR)
        self.items.append((control, rad_per_s))

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        time.sleep(1.0)
        while not self._stop.is_set():
            changed = False
            for ctl, speed in self.items:
                if not ctl.visible or not _mounted(ctl):
                    continue
                ctl.rotate = ft.Rotate(angle=ctl.rotate.angle + speed * self.tick,
                                       alignment=ft.Alignment.CENTER)
                changed = True
            if changed:
                try:
                    self.page.update()
                except Exception:
                    pass
            self._stop.wait(self.tick)


class Backdrop:
    """Big, slow, low-contrast shapes behind the UI (M3 Expressive decoration)."""

    # size, position, color, opacity, rad/s — the shape kinds come from the theme
    SLOTS = [
        (520, dict(left=-170, top=-190), ft.Colors.PRIMARY_CONTAINER, 0.85, 0.035),
        (420, dict(right=-140, bottom=-160), ft.Colors.TERTIARY_CONTAINER, 0.80, -0.045),
        (300, dict(left=380, bottom=-120), ft.Colors.SECONDARY_CONTAINER, 0.70, 0.06),
        (220, dict(right=260, top=70), ft.Colors.PRIMARY_CONTAINER, 0.55, -0.05),
    ]

    def __init__(self, spinner: _Spinner, kinds=("cookie12", "clover4", "soft_burst", "pentagon")):
        self.holders: list[ft.Container] = []
        children = []
        for (size, pos, color, opacity, speed), kind in zip(self.SLOTS, kinds):
            holder = ft.Container(content=shape_canvas(kind, size, color, opacity),
                                  width=size, height=size)
            spinner.add(holder, speed)
            self.holders.append(holder)
            children.append(ft.Container(holder, **pos))
        self.control = ft.Stack(children, expand=True)

    def set_shapes(self, kinds) -> None:
        for holder, (size, _pos, color, opacity, _speed), kind in zip(self.holders, self.SLOTS, kinds):
            holder.content = shape_canvas(kind, size, color, opacity)


class ShapeBadge(ft.Container):
    """An icon on a (slowly rotating) M3 shape. The icon itself stays upright."""

    def __init__(self, kind: str, size: float, icon: ft.IconData, *,
                 color=ft.Colors.PRIMARY_CONTAINER, icon_color=ft.Colors.ON_PRIMARY_CONTAINER,
                 icon_size: Optional[float] = None, spinner: Optional[_Spinner] = None,
                 speed: float = 0.25):
        self.shape_holder = ft.Container(shape_canvas(kind, size, color), width=size, height=size)
        if spinner is not None:
            spinner.add(self.shape_holder, speed)
        self.icon = ft.Icon(icon, color=icon_color, size=icon_size or size * 0.46)
        super().__init__(
            width=size, height=size,
            content=ft.Stack([
                self.shape_holder,
                ft.Container(self.icon, width=size, height=size, alignment=ft.Alignment.CENTER),
            ]),
        )


class ExpressiveLoader(ft.Container):
    """M3 Expressive loading indicator: turning + spring-morphing shapes."""

    STEP = 0.65  # seconds per morph

    def __init__(self, size: float = 40, color=ft.Colors.PRIMARY,
                 container=ft.Colors.SECONDARY_CONTAINER, with_container: bool = True):
        self._ld_size = size
        self._ld_color = color
        self._ld_step = 0
        self._ld_running = False
        self._ld_angle = 0.0
        inner = size * 0.72 if with_container else size
        self._ld_stepnner = inner
        self.switcher = ft.AnimatedSwitcher(
            content=self._shape(0),
            transition=ft.AnimatedSwitcherTransition.SCALE,
            duration=420,
            reverse_duration=260,
            switch_in_curve=ft.AnimationCurve.EASE_OUT_BACK,
            switch_out_curve=ft.AnimationCurve.EASE_IN,
        )
        self.turner = ft.Container(
            self.switcher, width=inner, height=inner,
            rotate=ft.Rotate(angle=0, alignment=ft.Alignment.CENTER),
            animate_rotation=ft.Animation(int(self.STEP * 1000), ft.AnimationCurve.EASE_OUT_BACK),
        )
        super().__init__(
            width=size, height=size,
            border_radius=size / 2,
            bgcolor=container if with_container else None,
            alignment=ft.Alignment.CENTER,
            content=self.turner,
            visible=False,
            animate_opacity=ft.Animation(250, ft.AnimationCurve.EASE_OUT),
        )

    def _shape(self, i: int) -> ft.Control:
        kind = LOADER_SEQUENCE[i % len(LOADER_SEQUENCE)]
        # key makes AnimatedSwitcher treat each shape as a new child
        return ft.Container(shape_canvas(kind, self._ld_stepnner, self._ld_color),
                            width=self._ld_stepnner, height=self._ld_stepnner, key=f"s{i}")

    def start(self) -> None:
        if self._ld_running:
            return
        self._ld_running = True
        self.visible = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._ld_running = False
        self.visible = False

    def _loop(self) -> None:
        while self._ld_running:
            time.sleep(self.STEP)
            if not self._ld_running or not _mounted(self):
                continue
            self._ld_step += 1
            self._ld_angle += math.radians(140)
            self.switcher.content = self._shape(self._ld_step)
            self.turner.rotate = ft.Rotate(angle=self._ld_angle, alignment=ft.Alignment.CENTER)
            try:
                self.update()
            except Exception:
                pass


def expressive_button_style(radius_rest: float = 28, radius_pressed: float = 12,
                            **kw) -> ft.ButtonStyle:
    """M3 Expressive buttons: the shape squishes on hover/press and springs back."""
    return ft.ButtonStyle(
        shape={
            ft.ControlState.PRESSED: ft.RoundedRectangleBorder(radius=radius_pressed),
            ft.ControlState.HOVERED: ft.RoundedRectangleBorder(radius=(radius_rest + radius_pressed) / 2),
            ft.ControlState.DEFAULT: ft.RoundedRectangleBorder(radius=radius_rest),
        },
        animation_duration=220,
        **kw,
    )


def make_spinner(page: ft.Page) -> _Spinner:
    return _Spinner(page)
