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
    VIDEO_FORMATS,
    DEFAULT_VIDEO_CODEC_FOR_CONTAINER,
    _DEFAULT_AUDIO_FOR_CONTAINER,
    check_video_compat,
    detect_available_hwaccels,
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
        self.trim = self._build_trim()
        self.watermark = self._build_watermark()
        self.output = self._build_output()
        self.sections = [self.image, self.video, self.audio, self.trim,
                         self.watermark, self.output]

    @property
    def s(self):
        return self.app.settings

    def _changed(self, estimate: bool = True) -> None:
        self.app.on_settings_changed(estimate)

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
            visible=False,
        )

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
        self.s.video_bitrate_mode = mode
        self.vid_crf_row.visible = mode == "crf"
        self.vid_bitrate_row.visible = mode == "cbr"
        self._changed()

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
            self.wm_text,
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
            "Звук по завершении", self.app.notify_enabled, self._on_sound)
        return section(
            "Сохранение", ft.Icons.SAVE_OUTLINED,
            folder_tile,
            with_help(self.overwrite, "Если файл уже есть",
                      "Что делать, если в папке уже лежит файл с таким именем. "
                      "По умолчанию добавляется суффикс _1, _2…"),
            with_help(self.checksum, "Контрольная сумма",
                      "Хэш готового файла — чтобы потом проверить, что он не побился при "
                      "копировании. MD5 быстрее, SHA-256 надёжнее."),
            meta_row,
            prio_row,
            sound_row,
        )

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
        self.vid_mode.selected = [s.video_bitrate_mode]
        self.vid_crf.value = max(14, min(35, s.video_crf))
        self.vid_crf_text.value = f"CRF {s.video_crf}"
        self.vid_bitrate.value = f"{s.video_bitrate_kbps / 1000:g}"
        self.vid_crf_row.visible = s.video_bitrate_mode == "crf"
        self.vid_bitrate_row.visible = s.video_bitrate_mode == "cbr"
        self.vid_res.value = str(s.video_height) if s.video_height else "auto"
        self.vid_aspect.value = s.video_aspect or "auto"
        self.vid_fps.value = str(s.video_fps) if s.video_fps else "auto"
        self.vid_hw.value = s.hwaccel or "none"

        self.trim_start.value = format_time(s.trim_start_s)
        self.trim_end.value = format_time(s.trim_end_s)
        self.trim_start.error = self.trim_end.error = None

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
        from converter import media_kind

        target_kind = media_kind(target_ext) if target_ext else None
        to_gif = target_ext == "gif" and "video" in kinds
        to_video = "video" in kinds and (target_kind == "video" or to_gif)
        to_audio = target_kind == "audio"
        has_av = bool(kinds & {"audio", "video"})

        self.image.visible = "image" in kinds and target_kind == "image" and not to_gif
        self.video.visible = to_video
        # GIF has no sound, so audio settings only for real audio/video outputs.
        self.audio.visible = has_av and (to_audio or (to_video and not to_gif))
        self.trim.visible = has_av and (to_audio or to_video)
        self.watermark.visible = ("image" in kinds and target_kind == "image"
                                  and target_ext != "pdf") or to_video
        # Codec choices make no sense for GIF.
        gif_hidden = not to_gif
        for row in (self.vid_codec_row, self.vid_audio_codec_row,
                    self.vid_mode_row, self.vid_hw_row):
            row.visible = gif_hidden
        self.vid_crf_row.visible = gif_hidden and self.s.video_bitrate_mode == "crf"
        self.vid_bitrate_row.visible = gif_hidden and self.s.video_bitrate_mode == "cbr"
        self.refresh_compat()
        if to_gif:
            self.vid_compat.visible = False
