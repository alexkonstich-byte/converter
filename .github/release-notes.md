## Downloads

| System | File | How to run |
|---|---|---|
| **Windows 10/11** | `Converter-windows-x64.zip` | Unzip anywhere and run `Converter.exe`. ffmpeg is included. |
| macOS | `Converter-macos.zip` | Unzip, move `Converter.app` to Applications. Install ffmpeg: `brew install ffmpeg`. On first launch right-click → Open (the app is not notarized). |
| Linux x64 | `Converter-linux-x64.tar.gz` | Extract and run `linux/Converter`. Needs `ffmpeg`, `libmpv2` and `zenity` from your package manager. |

Windows SmartScreen may warn about an unknown publisher because the build is not code-signed: click **More info → Run anyway**.

## What's inside

Image, audio and video converter with a Material You (Material 3) interface: batch queue, presets, trim, watermark, subtitles, joining files, "fit into N MB", parallel jobs, hardware encoding, drag & drop and more. See the [README](https://github.com/alexkonstich-byte/converter#readme) for the full list.

The Windows package bundles a GPL build of [FFmpeg](https://ffmpeg.org) from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds); its license is in `FFMPEG-LICENSE.txt`.
