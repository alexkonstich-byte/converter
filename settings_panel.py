"""Quality / processing settings — one M3 card per topic.

The panel owns its controls and writes straight into ``app.settings``
(a :class:`converter.ConversionSettings`). Sections show up only when they
are relevant for the current queue and target format, so nothing that is
hidden can silently affect a conversion.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import flet as ft

from converter import (
    NAME_TOKENS,
    VIDEO_FORMATS,
    DEFAULT_VIDEO_CODEC_FOR_CONTAINER,
    _DEFAULT_AUDIO_FOR_CONTAINER,
    check_video_compat,
    detect_available_hwaccels,
    render_name,
    target_kind,
)
from ui_kit import (
    SHAPE_SMALL,
    dropdown,
    full_width,
    label,
    section,
    switch_row,
    with_help,
)

if TYPE_CHECKING:
    from app import ConverterApp

HW_LABELS = {
    "nvenc": "NVIDIA NVENC",
    "qsv": "Intel Quick Sync",
    "amf": "AMD AMF",
    "videotoolbox": "Apple VideoToolbox",
}


def parse_time(s: str) -> Optional[float]:
    """'90', '1:30', '0:01:30', '1:30.5' → seconds. None if empty/invalid."""
    s = (s or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        return None
    if any(p < 0 for p in parts) or len(parts) > 3:
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def format_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    frac = seconds - int(seconds)
    s_txt = f"{s:02d}" + (f"{frac:.2f}"[1:] if frac >= 0.005 else "")
    return f"{h}:{m:02d}:{s_txt}" if h else f"{m}:{s_txt}"


class SettingsPanel:
    def __init__(self, app: "ConverterApp"):
        self.app = app
        self.image = self._build_image()
        self.audio = self._build_audio()
        self.video = self._build_video()
        self.transform = self._build_transform()
        self.subtitles = self._build_subtitles()
        self.trim = self._build_trim()
        self.watermark = self._build_watermark()
        self.output = self._build_output()
        self.sections = [self.image, self.video, self.audio, self.transform,
                         self.subtitles, self.trim, self.watermark, self.output]
        self._track_text_focus()

    @property
    def s(self):
        return self.app.settings

    def _changed(self, estimate: bool = True) -> None:
        self.app.on_settings_changed(estimate)

    def _track_text_focus(self) -> None:
        """Tell the app when a text field has focus (so Delete edits text,
        not the queue)."""
        for value in vars(self).values():
            if isinstance(value, ft.TextField):
                value.on_focus = lambda _e: self.app.set_text_focus(True)
                value.on_blur = lambda _e: self.app.set_text_focus(False)

    @staticmethod
    def _parse_mb(text: str) -> Optional[float]:
        try:
            v = float((text or "").replace(",", "."))
        except ValueError:
            return None
        return v if v > 0 else None

    # ------------------------------------------------------------- image
    def _build_image(self) -> ft.Container:
        self.img_quality = ft.Slider(
            min=1, max=100, divisions=99, value=self.s.image_quality,
            label="{value}", year_2023=False, on_change=self._on_quality, expand=True,
        )
        self.img_quality_text = label(f"{self.s.image_quality}", style=ft.TextThemeStyle.LABEL_LARGE,
                                      color=ft.Colors.PRIMARY, width=32, text_align=ft.TextAlign.END)
        self.img_w = ft.TextField(label="Ширина", suffix="px", dense=True, expand=True,
                                  input_filter=ft.NumbersOnlyInputFilter(),
                                  border_radius=SHAPE_SMALL, on_change=self._on_resize)
        self.img_h = ft.TextField(label="Высота", suffix="px", dense=True, expand=True,
                                  input_filter=ft.NumbersOnlyInputFilter(),
                                  border_radius=SHAPE_SMALL, on_change=self._on_resize)
        aspect_row, self.img_keep_aspect = switch_row(
            "Сохранять пропорции", self.s.image_keep_aspect, self._on_keep_aspect)
        exif_row, self.img_strip_exif = switch_row(
            "Удалить EXIF", self.s.image_strip_exif, self._on_strip_exif,
            "EXIF",
            "Невидимые метаданные камеры: модель, GPS-координаты, дата съёмки. "
            "Удаление защищает приватность при публикации.",
        )
        return section(
            "Изображения", ft.Icons.IMAGE_OUTLINED,
            with_help(
                ft.Column(spacing=0, controls=[
                    label("Качество (JPEG, WebP, AVIF)"),
                    ft.Row([self.img_quality, self.img_quality_text],
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ]),
                "Качество",
                "Шкала 1–100. Для JPEG 80–90 — золотая середина: на глаз почти не "
                "отличить от 100, а файл вдвое легче. PNG сжимается без потерь — "
                "там настройка не используется.",
            ),
            with_help(
                ft.Row([self.img_w, ft.Text("×"), self.img_h],
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                "Размер",
                "Можно указать только ширину или только высоту — вторая сторона "
                "посчитается с сохранением пропорций. Пусто — размер не меняется.",
            ),
            aspect_row,
            exif_row,
            visible=False,
        )

    def _on_quality(self, e) -> None:
        v = int(e.control.value)
        self.s.image_quality = v
        self.img_quality_text.value = str(v)
        self._changed()

    def _on_resize(self, _e) -> None:
        self.s.image_resize_width = int(self.img_w.value) if self.img_w.value else None
        self.s.image_resize_height = int(self.img_h.value) if self.img_h.value else None
        self._changed()

    def _on_keep_aspect(self, e) -> None:
        self.s.image_keep_aspect = bool(e.control.value)

    def _on_strip_exif(self, e) -> None:
        self.s.image_strip_exif = bool(e.control.value)

    # ------------------------------------------------------------- audio
    def _build_audio(self) -> ft.Container:
        self.aud_bitrate = dropdown(
            "Битрейт", "auto",
            [("auto", "Авто"), ("96", "96 kbps"), ("128", "128 kbps"), ("192", "192 kbps"),
             ("256", "256 kbps"), ("320", "320 kbps")],
            self._on_audio_bitrate,
        )
        self.aud_rate = dropdown(
            "Частота", "auto",
            [("auto", "Авто"), ("44100", "44.1 кГц · CD"), ("48000", "48 кГц · видео"),
             ("96000", "96 кГц · студия")],
            self._on_sample_rate,
        )
        self.aud_channels = dropdown(
            "Каналы", "auto",
            [("auto", "Авто"), ("1", "Моно"), ("2", "Стерео"), ("6", "5.1")],
            self._on_channels,
        )
        return section(
            "Звук", ft.Icons.GRAPHIC_EQ_ROUNDED,
            with_help(self.aud_bitrate, "Битрейт",
                      "Сколько данных в секунду. Больше — чище звук и тяжелее файл. "
                      "MP3 — от 192 kbps, AAC хватает 192. FLAC/WAV — без потерь, "
                      "настройка игнорируется."),
            with_help(self.aud_rate, "Частота дискретизации",
                      "44.1 кГц — стандарт музыки и CD, 48 кГц — стандарт видео. "
                      "Выше — только если точно знаете зачем."),
            with_help(self.aud_channels, "Каналы",
                      "Моно — 1, стерео — 2, 5.1 — 6. «Авто» сохраняет как в оригинале."),
            self._build_audio_size(),
            visible=False,
        )

    def _build_audio_size(self) -> ft.Row:
        self.aud_size = ft.TextField(
            label="Уложить в размер", suffix="МБ", hint_text="не ограничивать", dense=True,
            input_filter=ft.InputFilter(regex_string=r"^[0-9]*[.,]?[0-9]*$"),
            border_radius=SHAPE_SMALL, on_change=self._on_size,
            prefix_icon=ft.Icons.SAVE_ALT_ROUNDED,
        )
        self.aud_size_row = with_help(
            self.aud_size, "Уложить в размер",
            "Битрейт подберётся по длительности так, чтобы файл получился не больше "
            "указанного. Удобно для лимитов мессенджеров и почты.")
        return self.aud_size_row

    def _on_size(self, e) -> None:
        self.s.target_size_mb = self._parse_mb(e.control.value)
        # keep both size fields (audio / video) in sync
        for f in (self.aud_size, self.vid_size):
            if f is not e.control:
                f.value = e.control.value
        self._changed()

    def _on_audio_bitrate(self, e) -> None:
        v = e.control.value
        self.s.audio_bitrate_kbps = None if v == "auto" else int(v)
        self._changed()

    def _on_sample_rate(self, e) -> None:
        v = e.control.value
        self.s.audio_sample_rate = None if v == "auto" else int(v)
        self._changed()

    def _on_channels(self, e) -> None:
        v = e.control.value
        self.s.audio_channels = None if v == "auto" else int(v)
        self._changed()

    # ------------------------------------------------------------- video
    def _build_video(self) -> ft.Container:
        self.vid_codec = dropdown(
            "Видеокодек", "auto",
            [("auto", "Авто"), ("h264", "H.264 · везде играет"),
             ("h265", "H.265 / HEVC · компактнее"), ("av1", "AV1 · новейший")],
            self._on_video_codec,
        )
        self.vid_audio_codec = dropdown(
            "Звук в видео", "auto",
            [("auto", "Авто"), ("aac", "AAC"), ("mp3", "MP3"), ("opus", "Opus"),
             ("vorbis", "Vorbis"), ("flac", "FLAC"), ("ac3", "AC-3"),
             ("copy", "Без перекодирования")],
            self._on_video_audio_codec,
        )
        self.vid_mode = ft.SegmentedButton(
            selected=[self.s.video_bitrate_mode],
            segments=[
                ft.Segment(value="crf", label="Качество", icon=ft.Icons.HIGH_QUALITY_OUTLINED),
                ft.Segment(value="cbr", label="Битрейт", icon=ft.Icons.SPEED_ROUNDED),
                ft.Segment(value="size", label="Размер", icon=ft.Icons.SAVE_ALT_ROUNDED),
            ],
            on_change=self._on_video_mode,
        )
        self.vid_crf = ft.Slider(
            min=14, max=35, divisions=21, value=self.s.video_crf, label="{value}",
            year_2023=False, on_change=self._on_crf, expand=True,
        )
        self.vid_crf_text = label(f"CRF {self.s.video_crf}", style=ft.TextThemeStyle.LABEL_LARGE,
                                  color=ft.Colors.PRIMARY)
        self.vid_crf_row = with_help(
            ft.Row([self.vid_crf, self.vid_crf_text], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            "CRF",
            "Constant Rate Factor: меньше число — выше качество и больше файл. "
            "18 — почти без потерь, 23 — стандарт, 28 — заметное сжатие. "
            "−6 пунктов ≈ ×2 к размеру.",
        )
        self.vid_bitrate = ft.TextField(
            label="Битрейт", suffix="Мбит/с", value="8", dense=True,
            input_filter=ft.InputFilter(regex_string=r"^[0-9]*[.,]?[0-9]*$"),
            border_radius=SHAPE_SMALL, on_change=self._on_video_bitrate,
        )
        self.vid_bitrate_row = with_help(
            self.vid_bitrate, "Битрейт видео",
            "Для Full HD: 5–8 Мбит/с — веб, 12–20 — высокое качество, 35+ — мастер-копия. "
            "Для 4K умножьте на 4.",
        )
        self.vid_bitrate_row.visible = False
        self.vid_size = ft.TextField(
            label="Итоговый размер", suffix="МБ", hint_text="например, 25", dense=True,
            input_filter=ft.InputFilter(regex_string=r"^[0-9]*[.,]?[0-9]*$"),
            border_radius=SHAPE_SMALL, on_change=self._on_size,
        )
        self.vid_size_row = with_help(
            self.vid_size, "Итоговый размер",
            "Видеобитрейт рассчитается из длительности, чтобы файл влез в лимит: "
            "Telegram, Discord (25 МБ), почта. Чем меньше размер и длиннее видео, "
            "тем ниже качество.")
        self.vid_size_row.visible = False
        self.vid_res = dropdown(
            "Разрешение", "auto",
            [("auto", "Как в оригинале"), ("480", "480p"), ("720", "720p · HD"),
             ("1080", "1080p · Full HD"), ("1440", "1440p · 2K"), ("2160", "2160p · 4K")],
            self._on_resolution,
        )
        self.vid_aspect = dropdown(
            "Кадрирование", "auto",
            [("auto", "Без обрезки"), ("16:9", "16:9 · горизонталь"),
             ("9:16", "9:16 · вертикаль"), ("1:1", "1:1 · квадрат"), ("4:5", "4:5 · пост")],
            self._on_aspect,
        )
        self.vid_fps = dropdown(
            "Частота кадров", "auto",
            [("auto", "Как в оригинале"), ("24", "24 · кино"), ("30", "30 · стандарт"),
             ("60", "60 · плавно")],
            self._on_fps,
        )
        hw_opts = [("none", "Процессор (CPU)")]
        for codec in ("h264", "h265", "av1"):
            for hw in detect_available_hwaccels(codec):
                if all(k != hw for k, _ in hw_opts):
                    hw_opts.append((hw, HW_LABELS.get(hw, hw)))
        self.vid_hw = dropdown("Кодирование", "none", hw_opts, self._on_hwaccel,
                               disabled=len(hw_opts) == 1)
        self.vid_compat = ft.Container(
            visible=False,
            padding=12,
            border_radius=SHAPE_SMALL,
            bgcolor=ft.Colors.ERROR_CONTAINER,
            content=ft.Row(spacing=8, controls=[
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=ft.Colors.ON_ERROR_CONTAINER, size=20),
                ft.Text("", color=ft.Colors.ON_ERROR_CONTAINER, expand=True,
                        theme_style=ft.TextThemeStyle.BODY_SMALL),
            ]),
        )
        self.vid_codec_row = with_help(
            self.vid_codec, "Видеокодек",
            "H.264 играет на любом устройстве. H.265 даёт ту же картинку при "
            "меньшем размере. AV1 — ещё компактнее, но кодируется дольше всех. "
            "Для .webm подходит только AV1.")
        self.vid_audio_codec_row = with_help(
            self.vid_audio_codec, "Звук в видео",
            "«Авто» подбирает кодек под контейнер. Если выбрать несовместимый, "
            "приложение предупредит заранее.")
        self.vid_mode_row = ft.Row([self.vid_mode])
        self.vid_hw_row = with_help(
            self.vid_hw, "Аппаратное кодирование",
            "Видеокарта кодирует в 5–10 раз быстрее процессора, качество при "
            "том же битрейте чуть ниже. В списке только то, что поддерживает "
            "ваш ffmpeg.")
        return section(
            "Видео", ft.Icons.MOVIE_OUTLINED,
            self.vid_codec_row,
            self.vid_audio_codec_row,
            self.vid_compat,
            self.vid_mode_row,
            self.vid_crf_row,
            self.vid_bitrate_row,
            self.vid_size_row,
            with_help(self.vid_res, "Разрешение",
                      "Высота кадра; ширина считается автоматически. Уменьшение сильно "
                      "экономит место, увеличение бессмысленно."),
            with_help(self.vid_aspect, "Кадрирование",
                      "Обрезает кадр по центру до нужных пропорций — например, делает "
                      "вертикальное видео 9:16 для сторис из горизонтального."),
            with_help(self.vid_fps, "Частота кадров",
                      "24 — киношно, 30 — стандарт, 60 — плавно. Повышение fps размер "
                      "не уменьшает: кадры просто дублируются."),
            self.vid_hw_row,
            visible=False,
        )

    def _on_video_codec(self, e) -> None:
        v = e.control.value
        self.s.video_codec = None if v == "auto" else v
        self.refresh_compat()
        self._changed(estimate=False)

    def _on_video_audio_codec(self, e) -> None:
        v = e.control.value
        self.s.audio_codec_for_video = None if v == "auto" else v
        self.refresh_compat()
        self._changed()

    def _on_video_mode(self, e) -> None:
        mode = e.control.selected[0] if e.control.selected else "crf"
        if mode == "size":
            self.s.target_size_mb = self._parse_mb(self.vid_size.value)
        else:
            self.s.video_bitrate_mode = mode
            self.s.target_size_mb = None
        self._show_mode(mode)
        self._changed()

    def _ui_mode(self) -> str:
        return "size" if self.s.target_size_mb else self.s.video_bitrate_mode

    def _show_mode(self, mode: str, codec_visible: bool = True) -> None:
        self.vid_crf_row.visible = codec_visible and mode == "crf"
        self.vid_bitrate_row.visible = codec_visible and mode == "cbr"
        self.vid_size_row.visible = codec_visible and mode == "size"

    def _on_crf(self, e) -> None:
        v = int(e.control.value)
        self.s.video_crf = v
        self.vid_crf_text.value = f"CRF {v}"
        self._changed()

    def _on_video_bitrate(self, e) -> None:
        try:
            mbps = float((e.control.value or "0").replace(",", "."))
        except ValueError:
            mbps = 0
        if mbps > 0:
            self.s.video_bitrate_kbps = int(mbps * 1000)
            self._changed()

    def _on_resolution(self, e) -> None:
        v = e.control.value
        self.s.video_height = None if v == "auto" else int(v)
        self._changed()

    def _on_aspect(self, e) -> None:
        v = e.control.value
        self.s.video_aspect = None if v == "auto" else v
        self._changed(estimate=False)

    def _on_fps(self, e) -> None:
        v = e.control.value
        self.s.video_fps = None if v == "auto" else int(v)
        self._changed(estimate=False)

    def _on_hwaccel(self, e) -> None:
        v = e.control.value
        self.s.hwaccel = None if v == "none" else v

    def refresh_compat(self) -> None:
        """Warn up-front when the codec can't go into the chosen container."""
        target = self.app.target_ext
        msg = None
        if target in VIDEO_FORMATS:
            vc = self.s.video_codec or DEFAULT_VIDEO_CODEC_FOR_CONTAINER.get(target, "h264")
            ac = self.s.audio_codec_for_video or _DEFAULT_AUDIO_FOR_CONTAINER.get(target, "aac")
            msg = check_video_compat(target, vc, ac)
        self.vid_compat.visible = bool(msg)
        self.vid_compat.content.controls[1].value = msg or ""

    # --------------------------------------------------------- transform
    def _build_transform(self) -> ft.Container:
        self.rotate = ft.SegmentedButton(
            selected=["0"],
            show_selected_icon=False,
            segments=[
                ft.Segment(value="0", label="0°"),
                ft.Segment(value="90", label="90°", icon=ft.Icons.ROTATE_RIGHT_ROUNDED),
                ft.Segment(value="180", label="180°"),
                ft.Segment(value="270", label="270°", icon=ft.Icons.ROTATE_LEFT_ROUNDED),
            ],
            on_change=self._on_rotate,
        )
        self.rotate_row = with_help(
            ft.Row([self.rotate]), "Поворот",
            "Поворот по часовой стрелке. Пригодится для видео, снятого боком на телефон.")
        self.flip_h_row, self.flip_h = switch_row(
            "Отразить по горизонтали", self.s.flip_horizontal,
            lambda e: self._set("flip_horizontal", bool(e.control.value), False))
        self.flip_v_row, self.flip_v = switch_row(
            "Отразить по вертикали", self.s.flip_vertical,
            lambda e: self._set("flip_vertical", bool(e.control.value), False))
        self.mute_row, self.mute = switch_row(
            "Убрать звук", self.s.video_mute,
            lambda e: self._set("video_mute", bool(e.control.value)),
            "Убрать звук", "В результате останется только картинка.")
        self.norm_row, self.normalize = switch_row(
            "Выровнять громкость", self.s.audio_normalize,
            lambda e: self._set("audio_normalize", bool(e.control.value), False),
            "Нормализация громкости",
            "Приводит громкость к стандарту −16 LUFS (как у стримингов): тихие записи "
            "станут громче, громкие — тише, без искажений.")
        return section(
            "Поворот и звук", ft.Icons.TUNE_ROUNDED,
            self.rotate_row, self.flip_h_row, self.flip_v_row, self.mute_row, self.norm_row,
            visible=False,
        )

    def _set(self, name: str, value, estimate: bool = True) -> None:
        setattr(self.s, name, value)
        self._changed(estimate)

    def _on_rotate(self, e) -> None:
        self.s.video_rotate = int(e.control.selected[0]) if e.control.selected else 0
        self._changed(estimate=False)

    # --------------------------------------------------------- subtitles
    def _build_subtitles(self) -> ft.Container:
        self.sub_mode = dropdown(
            "Режим", "none",
            [("none", "Не трогать"), ("burn_file", "Вшить из файла в картинку"),
             ("soft_file", "Добавить из файла отдельной дорожкой"),
             ("burn_track", "Вшить встроенные субтитры видео")],
            self._on_sub_mode,
        )
        self.sub_file_text = label("Файл не выбран", style=ft.TextThemeStyle.BODY_MEDIUM,
                                   max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True)
        self.sub_file_row = ft.Container(
            visible=False,
            padding=ft.Padding.symmetric(horizontal=12, vertical=4),
            border_radius=SHAPE_SMALL,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Row(
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.SUBTITLES_OUTLINED, color=ft.Colors.PRIMARY),
                    self.sub_file_text,
                    ft.TextButton("Выбрать", on_click=lambda _e: self.app.page.run_task(
                        self.app.pick_subtitle_file)),
                ],
            ),
        )
        self.sub_track_hint = label(
            "Какую дорожку вшить, можно выбрать в предпросмотре видео.", visible=False)
        return section(
            "Субтитры", ft.Icons.SUBTITLES_OUTLINED,
            with_help(self.sub_mode, "Субтитры",
                      "«Вшить» — текст становится частью картинки и виден везде. "
                      "«Отдельной дорожкой» — субтитры можно включать и выключать в плеере "
                      "(MKV, MP4, MOV, WebM). Чтобы просто вытащить субтитры из видео, "
                      "выберите формат SRT, ASS или VTT."),
            self.sub_file_row,
            self.sub_track_hint,
            visible=False,
        )

    def _on_sub_mode(self, e) -> None:
        v = e.control.value
        self.s.subtitle_mode = None if v == "none" else v
        self._refresh_sub_rows()
        self._changed(estimate=False)

    def _refresh_sub_rows(self) -> None:
        mode = self.s.subtitle_mode
        self.sub_file_row.visible = mode in ("burn_file", "soft_file")
        self.sub_track_hint.visible = mode == "burn_track"
        f = self.s.subtitle_file
        self.sub_file_text.value = f.replace("\\", "/").rsplit("/", 1)[-1] if f else "Файл не выбран"

    def set_subtitle_file(self, path: str) -> None:
        self.s.subtitle_file = path
        self._refresh_sub_rows()

    # -------------------------------------------------------------- trim
    def _build_trim(self) -> ft.Container:
        self.trim_start = ft.TextField(
            label="Начало", hint_text="0:00", dense=True, expand=True,
            border_radius=SHAPE_SMALL, on_change=self._on_trim,
            prefix_icon=ft.Icons.START_ROUNDED,
        )
        self.trim_end = ft.TextField(
            label="Конец", hint_text="до конца", dense=True, expand=True,
            border_radius=SHAPE_SMALL, on_change=self._on_trim,
            prefix_icon=ft.Icons.KEYBOARD_TAB_ROUNDED,
        )
        return section(
            "Обрезка", ft.Icons.CONTENT_CUT_ROUNDED,
            with_help(
                ft.Row([self.trim_start, self.trim_end], spacing=8),
                "Обрезка",
                "Время в формате 90, 1:30 или 1:02:30. Пустое поле — от начала / до "
                "конца. Удобно вырезать нужный кусок без видеоредактора.",
            ),
            subtitle="Сохранить только фрагмент",
            visible=False,
        )

    def _on_trim(self, _e) -> None:
        start = parse_time(self.trim_start.value)
        end = parse_time(self.trim_end.value)
        bad_start = bool(self.trim_start.value.strip()) and start is None
        bad_end = bool(self.trim_end.value.strip()) and end is None
        self.trim_start.error = "Формат 1:30" if bad_start else None
        if bad_end:
            self.trim_end.error = "Формат 1:30"
        elif start is not None and end is not None and end <= start:
            self.trim_end.error = "Должен быть позже начала"
        else:
            self.trim_end.error = None
        self.s.trim_start_s = start
        self.s.trim_end_s = end if (end is None or start is None or end > start) else None
        self._changed()

    # --------------------------------------------------------- watermark
    def _build_watermark(self) -> ft.Container:
        self.wm_text = ft.TextField(
            label="Текст", hint_text="например, @username", dense=True,
            border_radius=SHAPE_SMALL, on_change=self._on_wm_text,
            prefix_icon=ft.Icons.TEXT_FIELDS_ROUNDED,
        )
        self.wm_position = dropdown(
            "Положение", self.s.watermark_position,
            [("top-left", "Сверху слева"), ("top-right", "Сверху справа"),
             ("bottom-left", "Снизу слева"), ("bottom-right", "Снизу справа"),
             ("center", "По центру")],
            self._on_wm_position,
        )
        self.wm_opacity = ft.Slider(
            min=0.1, max=1.0, divisions=9, value=self.s.watermark_opacity,
            label="{value}", year_2023=False, round=1, expand=True,
            on_change=self._on_wm_opacity,
        )
        return section(
            "Водяной знак", ft.Icons.BRANDING_WATERMARK_OUTLINED,
            full_width(self.wm_text),
            full_width(self.wm_position),
            ft.Column(spacing=0, controls=[
                label("Непрозрачность"),
                ft.Row([self.wm_opacity]),
            ]),
            subtitle="Для картинок и видео",
            visible=False,
        )

    def _on_wm_text(self, e) -> None:
        self.s.watermark_text = (e.control.value or "").strip() or None

    def _on_wm_position(self, e) -> None:
        self.s.watermark_position = e.control.value

    def _on_wm_opacity(self, e) -> None:
        self.s.watermark_opacity = round(float(e.control.value), 2)

    # ------------------------------------------------------------ output
    def _build_output(self) -> ft.Container:
        self.out_path = ft.Text(
            "Рядом с исходными файлами", theme_style=ft.TextThemeStyle.BODY_MEDIUM,
            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
        )
        self.out_reset = ft.IconButton(
            icon=ft.Icons.CLOSE_ROUNDED, tooltip="Сохранять рядом с исходными",
            visible=False, on_click=lambda _e: self.app.set_out_dir(None),
        )
        folder_tile = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            border_radius=SHAPE_SMALL,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            ink=True,
            on_click=lambda _e: self.app.page.run_task(self.app.pick_out_dir),
            content=ft.Row(
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.FOLDER_OUTLINED, color=ft.Colors.PRIMARY),
                    ft.Column(spacing=0, expand=True, controls=[
                        label("Папка"), self.out_path,
                    ]),
                    self.out_reset,
                ],
            ),
        )
        self.overwrite = dropdown(
            "Если файл уже есть", self.s.overwrite_mode,
            [("rename", "Сохранить под новым именем"), ("overwrite", "Перезаписать"),
             ("skip", "Пропустить")],
            self._on_overwrite,
        )
        self.checksum = dropdown(
            "Контрольная сумма", "off",
            [("off", "Не считать"), ("md5", "MD5"), ("sha256", "SHA-256")],
            self._on_checksum,
        )
        meta_row, self.meta_switch = switch_row(
            "Копировать метаданные", self.s.copy_metadata, self._on_metadata,
            "Метаданные",
            "Теги, обложки, даты, GPS и прочая служебная информация из оригинала. "
            "Выключите, если публикуете файл и не хотите делиться лишним.",
        )
        prio_row, self.prio_switch = switch_row(
            "Фоновый режим", self.s.low_priority, self._on_priority,
            "Фоновый режим",
            "ffmpeg работает с пониженным приоритетом: конвертация чуть дольше, "
            "зато компьютер не тормозит — можно работать или играть параллельно.",
        )
        sound_row, self.sound_switch = switch_row(
            "Звук и уведомление в конце", self.app.notify_enabled, self._on_sound,
            "Уведомление",
            "Когда очередь готова — короткий звук, а если окно свёрнуто или в фоне, "
            "ещё и системное уведомление.")
        self.template = ft.TextField(
            label="Имя файла", value=self.s.name_template, dense=True,
            border_radius=SHAPE_SMALL, on_change=self._on_template,
            prefix_icon=ft.Icons.DRIVE_FILE_RENAME_OUTLINE_ROUNDED,
        )
        self.template_example = label("")
        tokens = ft.Row(wrap=True, spacing=6, run_spacing=6, controls=[
            ft.Chip(label=tok, tooltip=desc, on_click=lambda _e, t=tok: self._add_token(t))
            for tok, desc in NAME_TOKENS.items()
        ])
        self.structure_row, self.structure_switch = switch_row(
            "Сохранять структуру папок", self.s.keep_structure, self._on_structure,
            "Структура папок",
            "Если вы добавили целую папку, в папке вывода повторятся её подпапки. "
            "Работает, когда выбрана отдельная папка для сохранения.")
        self.structure_row.visible = False
        self.jobs = dropdown(
            "Одновременно", "0",
            [("0", "Авто"), ("1", "По одному"), ("2", "2 файла"), ("4", "4 файла"),
             ("8", "8 файлов")],
            lambda e: self.app.set_jobs(int(e.control.value)),
        )
        self.concat_row, self.concat_switch = switch_row(
            "Склеить в один файл", False, lambda e: self.app.set_concat(bool(e.control.value)),
            "Склейка",
            "Все файлы очереди соединятся по порядку в один. Порядок меняется "
            "перетаскиванием в очереди. Разные разрешения и частоты приводятся к первому файлу.")
        self.concat_row.visible = False
        return section(
            "Сохранение", ft.Icons.SAVE_OUTLINED,
            folder_tile,
            self.structure_row,
            with_help(self.template, "Шаблон имени",
                      "Из чего собрать имя готового файла. Нажмите на метку ниже, чтобы "
                      "вставить её. Расширение добавится само."),
            tokens,
            self.template_example,
            with_help(self.overwrite, "Если файл уже есть",
                      "Что делать, если в папке уже лежит файл с таким именем. "
                      "По умолчанию добавляется суффикс _1, _2…"),
            with_help(self.checksum, "Контрольная сумма",
                      "Хэш готового файла — чтобы потом проверить, что он не побился при "
                      "копировании. MD5 быстрее, SHA-256 надёжнее."),
            with_help(self.jobs, "Одновременная конвертация",
                      "Сколько файлов обрабатывать параллельно. «Авто»: картинки — по числу "
                      "ядер, аудио — несколько сразу, видео — по одному (ffmpeg и так "
                      "загружает все ядра)."),
            self.concat_row,
            meta_row,
            prio_row,
            sound_row,
        )

    def _on_template(self, e) -> None:
        self.s.name_template = e.control.value or "{name}"
        self.refresh_template_example()
        self._changed(estimate=False)

    def _add_token(self, token: str) -> None:
        cur = self.template.value or ""
        self.template.value = (cur + ("_" if cur and not cur.endswith(("_", "-", " ")) else "")
                               + token)
        self.s.name_template = self.template.value
        self.refresh_template_example()
        self._changed(estimate=False)

    def refresh_template_example(self) -> None:
        from pathlib import Path
        item = self.app.selected or (self.app.files[0] if self.app.files else None)
        src = item.path if item else Path("Отпуск.mov")
        target = self.app.target_ext or "mp4"
        self.template_example.value = (
            f"Пример: {render_name(self.s.name_template, src, target, 1, self.s)}.{target}")

    def _on_structure(self, e) -> None:
        self.s.keep_structure = bool(e.control.value)

    def _on_overwrite(self, e) -> None:
        self.s.overwrite_mode = e.control.value

    def _on_checksum(self, e) -> None:
        v = e.control.value
        self.s.compute_checksum = None if v == "off" else v

    def _on_metadata(self, e) -> None:
        self.s.copy_metadata = bool(e.control.value)

    def _on_priority(self, e) -> None:
        self.s.low_priority = bool(e.control.value)

    def _on_sound(self, e) -> None:
        self.app.set_notify(bool(e.control.value))

    def show_out_dir(self, path) -> None:
        self.out_path.value = str(path) if path else "Рядом с исходными файлами"
        self.out_reset.visible = path is not None
        self.structure_row.visible = path is not None and self.app.has_folder_items()

    # ------------------------------------------------------------ sync
    def sync(self) -> None:
        """Push every value from ``app.settings`` into the controls."""
        s = self.s
        self.img_quality.value = s.image_quality
        self.img_quality_text.value = str(s.image_quality)
        self.img_w.value = str(s.image_resize_width or "")
        self.img_h.value = str(s.image_resize_height or "")
        self.img_keep_aspect.value = s.image_keep_aspect
        self.img_strip_exif.value = s.image_strip_exif

        self.aud_bitrate.value = str(s.audio_bitrate_kbps) if s.audio_bitrate_kbps else "auto"
        self.aud_rate.value = str(s.audio_sample_rate) if s.audio_sample_rate else "auto"
        self.aud_channels.value = str(s.audio_channels) if s.audio_channels else "auto"

        self.vid_codec.value = s.video_codec or "auto"
        self.vid_audio_codec.value = s.audio_codec_for_video or "auto"
        self.vid_mode.selected = [self._ui_mode()]
        self.vid_crf.value = max(14, min(35, s.video_crf))
        self.vid_crf_text.value = f"CRF {s.video_crf}"
        self.vid_bitrate.value = f"{s.video_bitrate_kbps / 1000:g}"
        size_txt = f"{s.target_size_mb:g}" if s.target_size_mb else ""
        self.vid_size.value = self.aud_size.value = size_txt
        self._show_mode(self._ui_mode())
        self.vid_res.value = str(s.video_height) if s.video_height else "auto"
        self.vid_aspect.value = s.video_aspect or "auto"
        self.vid_fps.value = str(s.video_fps) if s.video_fps else "auto"
        self.vid_hw.value = s.hwaccel or "none"

        self.trim_start.value = format_time(s.trim_start_s)
        self.trim_end.value = format_time(s.trim_end_s)
        self.trim_start.error = self.trim_end.error = None

        self.rotate.selected = [str(s.video_rotate % 360)]
        self.flip_h.value = s.flip_horizontal
        self.flip_v.value = s.flip_vertical
        self.mute.value = s.video_mute
        self.normalize.value = s.audio_normalize
        self.sub_mode.value = s.subtitle_mode or "none"
        self._refresh_sub_rows()
        self.template.value = s.name_template
        self.structure_switch.value = s.keep_structure
        self.refresh_template_example()

        self.wm_text.value = s.watermark_text or ""
        self.wm_position.value = s.watermark_position
        self.wm_opacity.value = s.watermark_opacity

        self.overwrite.value = s.overwrite_mode
        self.checksum.value = s.compute_checksum or "off"
        self.meta_switch.value = s.copy_metadata
        self.prio_switch.value = s.low_priority
        self.refresh_compat()

    def update_visibility(self, kinds: set[str], target_ext: Optional[str]) -> None:
        """Show only the sections that will actually affect this conversion."""
        tk = target_kind(target_ext) if target_ext else None
        to_gif = target_ext == "gif" and "video" in kinds
        to_subs = tk == "subtitle"
        to_video = "video" in kinds and (tk == "video" or to_gif)
        to_audio = tk == "audio"
        to_image = "image" in kinds and tk == "image" and not to_gif
        has_av = bool(kinds & {"audio", "video"})

        self.image.visible = to_image
        self.video.visible = to_video
        # GIF has no sound, so audio settings only for real audio/video outputs.
        self.audio.visible = has_av and (to_audio or (to_video and not to_gif))
        self.aud_size_row.visible = to_audio
        self.trim.visible = has_av and (to_audio or to_video or to_subs)
        self.watermark.visible = (to_image and target_ext != "pdf") or to_video
        self.subtitles.visible = to_video and not to_gif

        self.transform.visible = to_image or to_video or (has_av and to_audio)
        geometric = to_image or to_video
        self.rotate_row.visible = self.flip_h_row.visible = self.flip_v_row.visible = geometric
        self.mute_row.visible = to_video and not to_gif
        self.norm_row.visible = has_av and (to_audio or (to_video and not to_gif))

        # Codec choices make no sense for GIF.
        codec_visible = not to_gif
        for row in (self.vid_codec_row, self.vid_audio_codec_row,
                    self.vid_mode_row, self.vid_hw_row):
            row.visible = codec_visible
        self._show_mode(self._ui_mode(), codec_visible)
        self.refresh_compat()
        if to_gif:
            self.vid_compat.visible = False
        self.refresh_template_example()
