# Night Signal

An internet radio and local music player for your terminal with a live waveform and 12-band spectrum.

## Setup

Extract the ZIP and open a terminal in the `night-signal` folder. Night Signal needs Python 3.10 or newer and an FFmpeg build with both `ffmpeg` and `ffplay` on your PATH. If you just installed them, open a new terminal.

| System | Install missing dependencies | Check | Launch |
| --- | --- | --- | --- |
| Windows (PowerShell) | `winget install --id Python.Python.3.13 -e` and `winget install ffmpeg` | `.\radio.cmd --check` | `.\radio.cmd` |
| macOS (Homebrew) | `brew install python ffmpeg` | `python3 radio.py --check` | `python3 radio.py` |
| Debian / Ubuntu | `sudo apt install python3 ffmpeg` | `python3 radio.py --check` | `python3 radio.py` |

On other Linux distributions, install Python and an FFmpeg package that includes `ffplay` using your package manager. Add `--demo` to the launch command to hear a test tone and try the controls offline.

## Local music

Put your music in the `music` folder beside `radio.py`, then press `L`. Night Signal queues supported files from the folder and its subfolders in path order. Press `N` or `P` to skip tracks, or `L` again to return to radio.

You can also launch with one or more files or folders as arguments.

```powershell
.\radio.cmd --play "C:\Music\Album" "C:\Music\Loose Track.flac"
```

```bash
python3 radio.py --play "$HOME/Music/Album" "$HOME/Music/Loose Track.flac"
```

Folder playback supports MP3, FLAC, WAV, M4A, AAC, AIFF, ALAC, Ogg, Opus, and WMA. When a local file has artist and title tags, Night Signal displays them. Otherwise it uses the filename.

## Controls

| Key | Action |
| --- | --- |
| Arrow keys or `J` / `K` | Browse presets or move through the current list |
| `Enter` | Tune or retry the selected station, or play the current file |
| `1`–`9` | Tune the corresponding preset if configured |
| `Space` | Stop or start playback |
| `+` / `-` | Change volume |
| `M` | Mute or unmute |
| `V` | Switch between the waveform and spectrum |
| `X` | Open or close the fullscreen spectrum |
| `H` | Open or close track history |
| `C` in history | Copy the selected title |
| `L` | Switch between local music and radio |
| `N` / `P` | Next or previous local track |
| `F` | Add or remove the highlighted preset from favorites |
| `Tab` | Switch between all presets and favorites |
| `Q` | Quit |

In fullscreen mode, the spectrum runs from 20 Hz on the left to 20 kHz on the right. The `^` markers briefly hold peaks after a loud hit.

History keeps up to 100 entries from local files and streams that provide ICY track titles. Copying uses the system clipboard. On Linux, install `wl-clipboard` for Wayland or `xclip` for X11 if copying is unavailable.

Favorites, the last tuned station, volume, mute, visualizer preference, and track history are saved in `settings.json` beside the app. Night Signal resumes the last radio preset on a normal launch. If a stream drops, it retries up to five times. Press `Space` to cancel a pending retry.

Add `--list` to print the presets. To edit presets, update `stations.json` with up to nine names, genres, and direct HTTP(S) audio stream URLs.

Preset streams are provided by [SomaFM](https://somafm.com/listen/), [Jazz24](https://www.jazz24.org/streaming-links/), and [KEXP](https://www.kexp.org/mobile/kexp-livestreams/).
