# Converter

**English** · [Русский](README.ru.md)

A desktop app for converting **images, audio and video**, written in Python. The interface follows Material 3 / Material You (Flet 1.x): the whole palette is generated from one seed color, with light, dark and system themes.

> The interface is in English by default; Russian is one click away (palette button → Language).

## Download

Grab the latest build from [**Releases**](https://github.com/alexkonstich-byte/converter/releases/latest):

- **Windows 10/11, installer:** `Converter-<version>-windows-x64-setup.exe`. Installs like a normal app (Start menu, uninstaller), no admin rights needed.
- **Windows 10/11, portable:** `Converter-<version>-windows-x64-portable.zip`. Unzip anywhere and run `Converter.exe`.
- Both Windows builds already include ffmpeg.
- **macOS:** `Converter-macos.zip`. Install ffmpeg with `brew install ffmpeg`.
- **Linux:** `Converter-linux-x64.tar.gz`. Install `ffmpeg`, `libmpv2` and `zenity`.

The builds are not code-signed. On Windows choose *More info → Run anyway* if SmartScreen warns. On macOS right-click the app and choose *Open* the first time.

## Features

**Formats**
- **Images** (Pillow): `png`, `jpg`, `webp`, `avif`, `bmp`, `tiff`, `gif`, `ico`, `tga`, `ppm`, `pcx`, `pdf`. Inputs also include iPhone photos (`heic`) and camera RAW (`dng`, `cr2`, `cr3`, `nef`, `arw`, `orf`, `rw2`, `raf`…).
- **Audio** (ffmpeg): `mp3`, `wav`, `flac`, `ogg`, `m4a`, `aac`, `wma`, `opus`, `aiff`.
- **Video** (ffmpeg): `mp4`, `mkv`, `avi`, `mov`, `webm`, `flv`, `wmv`, `m4v`, `mpeg`, `mpg`, `ts`, plus high-quality GIF. You can also extract the audio or the subtitles (`srt`, `ass`, `vtt`) from a video.

**Processing**
- **Images:** quality, size and EXIF stripping.
- **Audio:** bitrate, sample rate and channels.
- **Video:** H.264/H.265/AV1, CRF or bitrate, resolution, crop to 9:16 / 1:1 / 4:5, fps, and hardware encoding (NVENC, Quick Sync, AMF, VideoToolbox).
- **Fit into N MB:** e.g. a video under 25 MB for Telegram, Discord or e-mail.
- **Rotate and mirror**, remove audio, loudness normalisation (EBU R128).
- **Trim** by time and add a **text watermark** (Cyrillic supported).
- **Subtitles:** burn in from a file or from the video itself, or add them as a switchable track.
- **Join** several audio or video files into one, even with different resolutions.
- Combine several images into one PDF.

**Convenience**
- **Adding files:** add files or a whole folder with sub-folders. In the built app you can also drag files into the window.
- **Queue:** reorder files by dragging.
- **Output names:** templates (`{name}`, `{n}`, `{date}`, `{height}`…), optionally keeping the folder structure.
- **Parallel jobs:** several images or audio files at once.
- **Preview before committing:** a 5-second test render with a size forecast, and a before/after slider for images.
- **Presets:** built-in (YouTube, Stories, Telegram, e-mail, FLAC, MP3 320, web) and your own.
- **Running a batch:** pause, cancel, retry only the failed files. Errors come with plain-language explanations, and the full ffmpeg log is available.
- **Notifications:** progress on the taskbar icon, and a system notification when the window is in the background.
- **Appearance:** 7 Material You themes (Lavender, Ocean, Forest, Sunset, Sakura, Mint, Graphite), each with its own animated shapes. Light, dark or system mode, and a pure-black AMOLED variant.
- **Fonts:** Balsamiq Sans (default), Google Sans or the system font.
- **Languages:** English (default) and Russian.
- **Memory:** theme, font, language, format and all settings are kept between launches.

## Keyboard shortcuts

| Keys | Action |
|---|---|
| `Ctrl+O` / `Ctrl+Shift+O` | Add files / folder |
| `Ctrl+Enter` | Start converting |
| `Esc` | Cancel / clear selection |
| `Delete` | Remove the selected file |
| `↑` / `↓` | Previous / next file |
| `Ctrl+L` | ffmpeg log |
| `F1` | Shortcut list |

## Running from source

Requires Python 3.10+.

```bash
pip install -r requirements.txt
python app.py
```

Audio and video need **ffmpeg** (images work without it). The app looks for it next to the executable first, then in `PATH`:

- Windows: `winget install Gyan.FFmpeg` (the app can start this for you)
- macOS: `brew install ffmpeg`
- Linux: `sudo apt install ffmpeg`

On Linux the built-in video player needs `libmpv` (`sudo apt install libmpv2`) and the file dialogs need `zenity`.

## Building

```bash
flet build windows   # or macos / linux
```

The result goes to `build/<platform>` and runs without Python. Drag & drop from the file manager only works in the built app.

Releases are built by GitHub Actions. Push a tag like `v2.0.0` and the **Release** workflow builds Windows, macOS and Linux packages and attaches them to a GitHub Release. The Windows packages (installer built with Inno Setup, see `installer/converter.iss`, and a portable zip) bundle ffmpeg.

## Project layout

```
converter/
├── app.py             # window, queue, running conversions
├── settings_panel.py  # settings cards
├── preview.py         # preview and media player
├── dialogs.py         # shortcuts, before/after, presets, test render
├── ui_kit.py          # shared M3 components
├── themes.py          # Material You themes, fonts, AMOLED
├── shapes.py          # M3 Expressive shapes, backdrop, morphing loader
├── i18n.py            # English / Russian strings
├── assets/fonts/      # Balsamiq Sans, Google Sans (SIL OFL)
├── queue_logic.py     # UI-free queue logic (formats, names, settings)
├── os_utils.py        # opening files, sounds, system notifications
├── converter.py       # conversion engine (Pillow + ffmpeg)
├── tests/             # pytest suite
└── pyproject.toml     # dependencies and `flet build` settings
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Test media is generated by ffmpeg itself. Audio and video tests are skipped when ffmpeg isn't installed.

## License

MIT. The bundled fonts Balsamiq Sans and Google Sans are licensed under the SIL Open Font License (see `assets/fonts`). Windows releases include an FFmpeg build licensed under GPL (see `FFMPEG-LICENSE.txt` inside the package).
