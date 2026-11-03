# Bordders Clipper

Instant replay for Windows. It quietly keeps the last 30 seconds of your screen and audio, and when something cool happens you hit **Alt+F10** and it's saved as an .mp4.

No account, no overlay, no upload. It's a single Python file.

## Why I made it

I wanted something like ShadowPlay that works on any GPU, doesn't eat frames in game, and doesn't need an account. So this records on the GPU, keeps a small rolling buffer on disk, and only does real work when you actually save a clip.

## Features

- Always-on replay buffer, 5 seconds to 5 minutes
- One hotkey to save, works in fullscreen games
- GPU capture and encoding (NVIDIA NVENC, AMD AMF, Intel Quick Sync), CPU fallback
- Desktop audio and mic, optional separate tracks, mic noise suppression
- Sorts clips into a folder per game automatically
- Clip library with thumbnails, filter by game, built-in player
- Rename, delete (to Recycle Bin), open in your normal player
- Lives in the tray, can start with Windows

## Install

You need Windows 10/11, Python 3.10+ and [FFmpeg](https://ffmpeg.org/download.html) on your PATH.

The quickest way to get FFmpeg:

```
winget install Gyan.FFmpeg
```

Then:

```
git clone https://github.com/Bordder/clipper.git
cd clipper
pip install -r requirements.txt
pythonw clipper.pyw
```

Or just double-click `clipper.pyw`.

## Usage

| Key | What it does |
| --- | --- |
| `Alt+F10` | Save the last 30 s (change both in Settings) |
| `Space` | Play / pause in the player |
| `Left` / `Right` | Skip 5 s |
| `Esc` | Back to the library |
| `F5` | Refresh the library |

Clips go to `Videos\Clips` by default, with a subfolder per game, e.g. `Clips\Rocket League\`.

## Settings

- **General:** clip length, hotkey, save folder, sort by game, sound on save, start with Windows, close to tray
- **Video:** monitor, FPS, resolution, encoder, H.264 / HEVC, preset, quality, cursor
- **Audio:** desktop audio, mic and device, volumes, noise suppression, separate tracks, sync offset, bitrate

Settings are stored in `%LOCALAPPDATA%\Clipper\settings.json`.

## How it stays light

- The screen is grabbed on the GPU (`ddagrab`) and encoded there too. At native resolution with NVENC, frames never touch the CPU.
- The buffer is 2 second chunks in a ring on disk, so it never grows.
- Saving a clip just copies the video stream, no re-encode. Usually takes about a second.
- Everything runs at below-normal priority, so your game always wins.

## Troubleshooting

**Nothing records / "Capture stopped"**: click the status pill to open the log, or look at `%LOCALAPPDATA%\Clipper\ffmpeg.log`. Usually FFmpeg isn't on PATH or the encoder isn't supported, try setting Encoder to CPU.

**Hotkey doesn't work**: another app probably has it. Pick a different one in Settings.

**Black clips**: some games block desktop capture in exclusive fullscreen. Borderless windowed fixes it.

## Privacy

Nothing leaves your PC. No telemetry, no network calls. The buffer, thumbnails and log live in `%LOCALAPPDATA%\Clipper`.

## License

[MIT](LICENSE) © Bordder
