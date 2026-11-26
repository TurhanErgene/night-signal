"""Play internet radio or local music with a live waveform and spectrum."""

import argparse
import array
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
from math import ceil
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from unicodedata import category, east_asian_width, normalize
from urllib.parse import urlsplit

from spectrum import BAND_EDGES, FRAME_COUNT, Spectrum


PRESETS = Path(__file__).with_name("stations.json")
SETTINGS = Path(__file__).with_name("settings.json")
MUSIC = Path(__file__).absolute().with_name("music")
SAMPLE_RATE = 44100
BLOCK_BYTES = 4096
HISTORY_LIMIT = 100
MUSIC_EXTENSIONS = {".aac", ".aif", ".aiff", ".alac", ".flac", ".m4a", ".mp3",
                    ".oga", ".ogg", ".opus", ".wav", ".wma"}
# The decoder supplies raw PCM. Send it to ffplay with a streaming WAV header
# so older and newer FFmpeg builds can both infer stereo and sample rate.
WAV_HEADER = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 0xFFFFFFFF, b"WAVE",
                         b"fmt ", 16, 1, 2, SAMPLE_RATE, SAMPLE_RATE * 4, 4, 16,
                         b"data", 0xFFFFFFFF)
STREAM_TIMEOUT = 15
RETRY_DELAYS = (2, 4, 8, 16, 30)
STABLE_STREAM_TIME = 10
ESC = "\x1b["
GITHUB_REPO = "github.com/TurhanErgene/night-signal"
HEADER_COLUMNS_PER_SECOND = 8
TONE = (
    "aevalsrc=exprs='0.12*(sin(2*PI*110*t)+0.5*sin(2*PI*165*t))"
    "*pow(0.55+0.45*sin(2*PI*1.2*t),2)':sample_rate=44100"
)
PALETTES = {
    "Classic": {"text": "1;97", "active": "1;92", "accent": "95", "dim": "90", "warning": "93", "star": "1;93"},
    "Amber": {"text": "1;97", "active": "1;93", "accent": "33", "dim": "90", "warning": "91", "star": "1;93"},
    "Chromatic": {"text": "97", "active": "1;96", "accent": "1;95", "dim": "90", "warning": "93", "star": "1;93"},
    "Mono": {},
}
SPECTRUM_COLORS = (
    "255;91;132", "255;113;111", "255;148;91", "255;190;92",
    "243;219;105", "191;232;118", "117;232;153", "83;225;199",
    "80;195;243", "116;164;255", "164;139;255", "222;127;248",
)


@dataclass(frozen=True)
class Station:
    name: str
    genre: str
    url: str = ""
    file: Path | None = None


@dataclass(frozen=True)
class Track:
    title: str
    source: str
    heard_at: str


def clean_text(value):
    return "".join(character for character in " ".join(value.split()) if character.isprintable())


def local_sources(paths):
    files = {}
    for entry in paths:
        path = Path(entry).expanduser()
        if path.is_dir():
            tracks = sorted(
                (item for item in path.rglob("*")
                 if item.suffix.lower() in MUSIC_EXTENSIONS and item.is_file()),
                key=lambda item: (str(item).casefold(), str(item)),
            )
        elif path.is_file():
            tracks = [path]
        else:
            raise ValueError(f"Music path not found: {path}")
        for track in tracks:
            resolved = track.resolve()
            files[resolved] = Station(clean_text(track.stem) or "Local track", "Local file",
                                      file=resolved)
    if not files:
        raise ValueError("No supported audio files found.")
    return list(files.values())


def copy_to_clipboard(value, success):
    if os.name == "nt":
        # Write Unicode text without a BOM or trailing newline for Explorer.
        commands = (("powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     "[Console]::InputEncoding = [Text.UTF8Encoding]::new($false); "
                     "Set-Clipboard -Value ([Console]::In.ReadToEnd()) -ErrorAction Stop"),)
        encoding = "utf-8"
    elif sys.platform == "darwin":
        commands, encoding = (("pbcopy",),), "utf-8"
    else:
        commands = (("wl-copy",), ("xclip", "-selection", "clipboard"), ("xsel", "--clipboard", "--input"))
        encoding = "utf-8"
    for program, *arguments in commands:
        executable = shutil.which(program)
        if executable is None:
            continue
        try:
            result = subprocess.run([executable, *arguments], input=value.encode(encoding),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return success
    if sys.platform.startswith("linux"):
        return "Clipboard unavailable. Try wl-copy or xclip."
    return "Clipboard unavailable."


def open_music_folder():
    try:
        MUSIC.mkdir(exist_ok=True)
        if os.name == "nt":
            os.startfile(str(MUSIC))
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(MUSIC)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return "Could not open the folder. Press C to copy its path."
    return "Opening the music folder. Press L to scan again."


def plain(value):
    decomposed = normalize("NFKD", " ".join(value.split()))
    return "".join(
        character if " " <= character <= "~" else "?"
        for character in decomposed if not category(character).startswith("M")
    )


def cell_width(character):
    if category(character).startswith("M"):
        return 0
    return 2 if east_asian_width(character) in ("F", "W") else 1


def fit_cells(value, width):
    if value.isascii():
        return value[:width].ljust(width)
    result = []
    for character in value:
        cells = cell_width(character)
        if cells > width:
            break
        result.append(character)
        width -= cells
    return "".join(result) + " " * width


def path_lines(path, width):
    line = []
    used = 0
    for character in str(path):
        character = character if character.isprintable() else "?"
        cells = cell_width(character)
        if used + cells > width:
            yield "".join(line)
            line = []
            used = 0
        line.append(character)
        used += cells
    if line:
        yield "".join(line)


def read_settings(path):
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(path, data):
    # Replace the whole file after a successful write so a failed save leaves
    # the previous settings readable.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".settings-", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(data, handle, indent=2)
            handle.write("\n")
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_presets():
    entries = json.loads(PRESETS.read_text(encoding="utf-8-sig"))
    if not isinstance(entries, list) or not 1 <= len(entries) <= 9:
        raise ValueError("stations.json must contain between 1 and 9 presets.")

    stations = []
    for number, entry in enumerate(entries, 1):
        fields = ("name", "genre", "url")
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(field), str) and entry[field].strip() for field in fields
        ):
            raise ValueError(f"Preset {number} needs a name, genre and URL.")
        name, genre, url = (entry[field].strip() for field in fields)
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"Preset {number} needs a direct HTTP(S) stream URL.")
        stations.append(Station(plain(name), plain(genre), url))
    return stations


def find_executable(name):
    executable = shutil.which(name)
    if executable is None:
        raise FileNotFoundError(f"{name} is required. Install FFmpeg and add it to PATH.")

    # Run the actual program behind a Scoop shim so stop/quit can terminate it directly.
    path = Path(executable)
    shim = path.with_suffix(".shim")
    if os.name == "nt" and shim.is_file():
        for line in shim.read_text(encoding="utf-8-sig").splitlines():
            key, _, value = line.partition("=")
            target = Path(value.strip().strip('"'))
            if key.strip() == "path" and target.is_absolute() and target.is_file():
                return str(target)
    return executable


class Playback:
    def __init__(self, decoder, player, station, volume, muted):
        self.volume = volume
        self.muted = muted
        self.trace = ()
        self.bands = ()
        self.peaks = ()
        self.local = station.file is not None
        self.title = station.name if self.local else "" if station.url else "Night Signal test tone"
        self.end_of_input = False
        self.last_audio = None
        self.started = time.monotonic()
        self.stop_requested = threading.Event()
        self.decoder_errors = deque(maxlen=8)
        self.player_log = tempfile.TemporaryFile()
        self.decoder = self.player = self.worker = self.metadata_reader = None

        if self.local:
            source = ["-re", "-protocol_whitelist", "file", "-i", str(station.file)]
        elif station.url:
            source = ["-rw_timeout", "8000000", "-icy", "1"]
            if urlsplit(station.url).scheme == "https":
                source += ["-tls_verify", "1"]
            source += ["-i", station.url]
        else:
            source = ["-re", "-f", "lavfi", "-i", TONE]
        output = ["-map", "0:a:0", "-vn", "-ac", "2", "-ar", str(SAMPLE_RATE),
                  "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"]
        player_input = ["-f", "wav", "-ignore_length", "1", "-probesize", "32", "-i", "pipe:0"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        try:
            self.decoder = subprocess.Popen(
                [decoder, "-hide_banner", "-nostdin", "-loglevel", "level+verbose",
                 *source, *output],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=flags,
            )
            self.player = subprocess.Popen(
                [player, "-hide_banner", "-loglevel", "error", "-nostats",
                 "-nodisp", "-autoexit", *player_input],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.player_log,
                creationflags=flags,
            )
        except OSError:
            self.close()
            raise

        self.metadata_reader = threading.Thread(target=self.read_metadata, daemon=True)
        self.metadata_reader.start()
        self.worker = threading.Thread(target=self.pump, daemon=True)
        self.worker.start()

    def read_metadata(self):
        marker = "Metadata update for StreamTitle: "
        tags = {}
        reading_local_tags = self.local
        for raw in self.decoder.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if "\ufffd" in line:
                line = raw.decode("latin-1").rstrip("\r\n")
            prefix, found, title = line.partition(marker)
            if found and "[verbose]" in prefix:
                self.title = clean_text(title)
            elif "[error]" in line or "[fatal]" in line:
                self.decoder_errors.append(line)
            elif reading_local_tags and line.startswith("[info]"):
                key, _, value = line[len("[info]"):].strip().partition(":")
                key = key.strip().lower()
                if key in ("title", "artist") and key not in tags:
                    tags[key] = clean_text(value)
                if key == "stream mapping":
                    title = tags.get("title") or self.title
                    self.title = " - ".join(filter(None, (tags.get("artist"), title)))
                    reading_local_tags = False

    def pump(self):
        spectrum = Spectrum(SAMPLE_RATE)
        spectrum_samples = deque(maxlen=FRAME_COUNT * 2)
        frames_since_spectrum = 0
        try:
            self.player.stdin.write(WAV_HEADER)
            while not self.stop_requested.is_set():
                block = self.decoder.stdout.read(BLOCK_BYTES)
                if not block:
                    self.end_of_input = True
                    return

                samples = array.array("h", block)
                if sys.byteorder == "big":
                    samples.byteswap()

                # Use the mean of the left and right channels for the scope.
                stride = max(2, (len(samples) // 128) * 2)
                trace = tuple(
                    (samples[index] + samples[index + 1]) / 65536
                    for index in range(0, len(samples) - 1, stride)
                )
                volume = self.volume
                if self.muted or volume == 0:
                    block = bytes(len(block))
                elif volume != 100:
                    scaled = array.array("h", (sample * volume // 100 for sample in samples))
                    if sys.byteorder == "big":
                        scaled.byteswap()
                    block = scaled.tobytes()

                self.player.stdin.write(block)
                self.player.stdin.flush()
                self.trace = trace
                self.last_audio = time.monotonic()

                spectrum_samples.extend(samples)
                frames_since_spectrum += len(samples) // 2
                if frames_since_spectrum >= FRAME_COUNT:
                    self.bands = spectrum.measure(list(spectrum_samples))
                    self.peaks = spectrum.peaks
                    frames_since_spectrum = 0
        except OSError:
            # The UI reads the subprocess diagnostics when the pipe closes.
            return
        finally:
            if self.local:
                try:
                    self.player.stdin.close()
                except OSError:
                    pass

    def finished_file(self):
        return (self.local and self.end_of_input and self.last_audio is not None
                and self.decoder.poll() == 0 and self.player.poll() == 0)

    def problem(self, now):
        draining_file = self.local and self.end_of_input and self.last_audio is not None
        if self.player.poll() is not None or (not self.worker.is_alive() and not draining_file):
            return "Audio stream ended."
        if now - (self.last_audio or self.started) > STREAM_TIMEOUT:
            return "Stream timed out."
        return ""

    def close(self):
        self.stop_requested.set()
        for process in (self.player, self.decoder):
            if process is not None and process.poll() is None:
                process.kill()
        for process in (self.player, self.decoder):
            if process is not None:
                process.wait()
        if self.worker is not None:
            self.worker.join()
        if self.metadata_reader is not None:
            self.metadata_reader.join()
        if self.decoder is not None:
            self.decoder.stdout.close()
            self.decoder.stderr.close()
        if self.player is not None:
            try:
                self.player.stdin.close()
            except OSError:
                pass

        log_size = self.player_log.seek(0, os.SEEK_END)
        self.player_log.seek(max(0, log_size - 2048))
        player_errors = self.player_log.read().decode("utf-8", errors="replace").splitlines()
        self.player_log.close()
        return player_errors + list(self.decoder_errors)


class Radio:
    def __init__(self, stations, decoder, player, settings_path=None):
        self.stations = stations
        self.decoder = decoder
        self.player = player
        self.settings_path = settings_path
        settings = read_settings(settings_path)
        self.tuned = next((index for index, station in enumerate(stations)
                           if station.url and station.url == settings.get("station")), None)
        self.selected = self.tuned if self.tuned is not None else 0
        self.playback = None
        volume = settings.get("volume")
        self.volume = volume if type(volume) is int and 0 <= volume <= 100 else 65
        self.muted = settings.get("muted") is True
        self.show_spectrum = settings.get("spectrum") is True
        theme = settings.get("theme")
        self.theme = theme if isinstance(theme, str) and theme in PALETTES else "Classic"
        self.header_motion = settings.get("header_motion") is not False
        self.header_started = time.monotonic()
        stored_favorites = settings.get("favorites")
        self.favorites = (
            {url for url in stored_favorites if isinstance(url, str)}
            if isinstance(stored_favorites, list) else set()
        )
        self.favorites_only = False
        self.settings_warning = ""
        self.fullscreen = False
        self.history_open = False
        self.history_index = 0
        self.history_note = ""
        self.history = deque(maxlen=HISTORY_LIMIT)
        history_data = settings.get("history")
        if isinstance(history_data, list):
            for entry in history_data[:HISTORY_LIMIT]:
                if isinstance(entry, dict) and all(
                    isinstance(entry.get(field), str) for field in ("title", "source", "heard_at")
                ):
                    self.history.append(Track(*(clean_text(entry[field])
                                               for field in ("title", "source", "heard_at"))))
        self.local_tracks = []
        self.local_index = None
        self.music_help = ""
        self.music_note = ""
        self.retry_at = None
        self.retry_count = 0
        self.on_air_since = None
        self.failure = ""
        self.status = "STANDBY"
        self.message = "Press Enter to tune."

    def save(self):
        if self.settings_path is None:
            return
        station = self.stations[self.tuned].url if self.tuned is not None else None
        try:
            write_settings(self.settings_path, {
                "station": station,
                "volume": self.volume,
                "muted": self.muted,
                "spectrum": self.show_spectrum,
                "theme": self.theme,
                "header_motion": self.header_motion,
                "favorites": sorted(self.favorites),
                "history": [{"title": track.title, "source": track.source, "heard_at": track.heard_at}
                            for track in self.history],
            })
        except OSError:
            self.settings_warning = "Could not save settings."
        else:
            self.settings_warning = ""

    def preset_indices(self):
        return [
            index for index, station in enumerate(self.stations)
            if not self.favorites_only or station.url in self.favorites
        ]

    def source(self):
        if self.local_index is not None:
            return self.local_tracks[self.local_index]
        index = self.tuned if self.tuned is not None else self.selected
        return self.stations[index]

    def log_track(self, title, source):
        if self.history and (self.history[0].title, self.history[0].source) == (title, source):
            return
        if self.history_open and self.history:
            self.history_index = min(self.history_index + 1, HISTORY_LIMIT - 1)
        self.history.appendleft(Track(title, source, datetime.now().strftime("%Y-%m-%d %H:%M")))
        self.save()

    def open_local(self, tracks):
        self.stop()
        self.local_tracks = tracks
        self.local_index = 0
        self.start_playback()

    def toggle_local(self):
        if self.local_index is not None:
            self.local_index = None
            self.stop()
            return
        try:
            MUSIC.mkdir(exist_ok=True)
            tracks = local_sources([MUSIC])
        except ValueError:
            self.music_help = "Add MP3, FLAC, WAV, or other audio to:"
            self.music_note = "Then press L to scan again."
        except OSError:
            self.music_help = "Could not open the local music folder:"
            self.music_note = "Check that the folder is accessible, then press L."
        else:
            self.music_help = ""
            self.music_note = ""
            self.open_local(tracks)

    def select_local(self, index):
        self.stop()
        self.local_index = index
        self.start_playback()

    def stop(self):
        if self.playback is not None:
            self.playback.close()
            self.playback = None
        self.retry_at = None
        self.retry_count = 0
        self.on_air_since = None
        self.failure = ""
        self.status = "STANDBY"
        self.message = "Press Enter to play." if self.local_index is not None else "Press Enter to tune."

    def tune(self):
        self.local_index = None
        self.stop()
        self.tuned = self.selected
        self.save()
        self.start_playback()

    def start_playback(self):
        try:
            self.playback = Playback(self.decoder, self.player, self.source(),
                                     self.volume, self.muted)
        except OSError as error:
            self.status = "OFFLINE"
            self.message = plain(error.strerror or str(error))
            return
        self.status = "RECONNECTING" if self.retry_count else "OPENING" if self.local_index is not None else "TUNING"
        self.message = (
            f"Reconnect attempt {self.retry_count}/{len(RETRY_DELAYS)}..."
            if self.retry_count else "Opening audio stream..."
        )

    def update(self, now):
        if self.retry_at is not None:
            if now < self.retry_at:
                seconds = ceil(self.retry_at - now)
                self.message = f"Retry {self.retry_count}/{len(RETRY_DELAYS)} in {seconds}s | {self.failure}"
                return
            self.retry_at = None
            self.start_playback()
            return
        if self.playback is None:
            return

        source = self.source()
        if self.playback.last_audio is not None and self.playback.title and (source.url or source.file):
            self.log_track(self.playback.title, "Local file" if source.file else source.name)
        if self.playback.finished_file():
            next_index = self.local_index + 1
            if next_index < len(self.local_tracks):
                self.select_local(next_index)
            else:
                self.stop()
                self.status = "FINISHED"
                self.message = "End of queue | Enter replays this track"
            return

        issue = self.playback.problem(now)
        if issue:
            can_retry = source.url and self.playback.player.poll() is None
            diagnostics = self.playback.close()
            self.playback = None
            self.on_air_since = None
            detail = next((line for line in diagnostics if line.strip()), "")
            while detail.startswith("[") and "] " in detail:
                detail = detail.split("] ", 1)[1]
            self.failure = plain(detail) or issue
            if can_retry and self.retry_count < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[self.retry_count]
                self.retry_count += 1
                self.retry_at = time.monotonic() + delay
                self.status = "WAITING TO RETRY"
                self.message = f"Retry {self.retry_count}/{len(RETRY_DELAYS)} in {delay}s | {self.failure}"
            else:
                self.status = "FILE ERROR" if source.file else "OFFLINE"
                self.message = f"Enter to retry | {self.failure}"
        elif self.playback.last_audio is not None:
            if self.on_air_since is None:
                self.on_air_since = now
            elif now - self.on_air_since >= STABLE_STREAM_TIME:
                self.retry_count = 0
            self.status = "PLAYING" if source.file else "ON AIR"
            self.message = ""

    def handle_key(self, key):
        if key == "t":
            themes = list(PALETTES)
            self.theme = themes[(themes.index(self.theme) + 1) % len(themes)]
            self.save()
        elif key == "a":
            self.header_motion = not self.header_motion
            self.header_started = time.monotonic()
            self.save()
        elif self.music_help:
            if key == "\x1b":
                self.music_help = ""
            elif key == "c":
                self.music_note = copy_to_clipboard(str(MUSIC), "Music folder path copied.")
            elif key == "o":
                self.music_note = open_music_folder()
            elif key == "l":
                self.toggle_local()
        elif key == "h":
            self.history_open = not self.history_open
            self.history_index = 0
            self.history_note = ""
        elif self.history_open:
            if key == "\x1b":
                self.history_open = False
            elif key in ("up", "k"):
                self.history_index = max(0, self.history_index - 1)
            elif key in ("down", "j") and self.history:
                self.history_index = min(len(self.history) - 1, self.history_index + 1)
            elif key == "c" and self.history:
                self.history_note = copy_to_clipboard(self.history[self.history_index].title, "Title copied.")
        elif key == "x":
            self.fullscreen = not self.fullscreen
        elif key == "\x1b":
            self.fullscreen = False
        elif key == "l":
            self.toggle_local()
        elif key == "o":
            self.music_help = "Music folder:"
            self.music_note = open_music_folder()
        elif self.local_index is not None and key in ("up", "left", "k", "p", "down", "right", "j", "n"):
            direction = -1 if key in ("up", "left", "k", "p") else 1
            self.select_local((self.local_index + direction) % len(self.local_tracks))
        elif key in ("up", "left", "k", "down", "right", "j"):
            presets = self.preset_indices()
            if presets:
                direction = -1 if key in ("up", "left", "k") else 1
                self.selected = presets[(presets.index(self.selected) + direction) % len(presets)]
        elif len(key) == 1 and key in "123456789" and int(key) <= len(self.stations):
            self.selected = int(key) - 1
            if self.selected not in self.preset_indices():
                self.favorites_only = False
            self.tune()
        elif key in ("\r", "\n"):
            if self.local_index is None:
                if self.preset_indices():
                    self.tune()
            else:
                self.select_local(self.local_index)
        elif key == " ":
            if self.playback is None and self.retry_at is None:
                if self.local_index is None:
                    if self.preset_indices():
                        self.tune()
                else:
                    self.start_playback()
            else:
                self.stop()
        elif key == "v":
            self.show_spectrum = not self.show_spectrum
            self.save()
        elif key == "f" and self.local_index is None and self.preset_indices():
            url = self.stations[self.selected].url
            if url in self.favorites:
                self.favorites.remove(url)
            else:
                self.favorites.add(url)
            self.keep_preset_visible()
            self.save()
        elif key == "\t" and self.local_index is None:
            self.fullscreen = False
            self.favorites_only = not self.favorites_only
            self.keep_preset_visible()
        elif key in ("+", "=", "-", "m"):
            if key == "m":
                self.muted = not self.muted
            else:
                change = -5 if key == "-" else 5
                self.volume = max(0, min(100, self.volume + change))
            if self.playback is not None:
                self.playback.volume = self.volume
                self.playback.muted = self.muted
            self.save()

    def keep_preset_visible(self):
        presets = self.preset_indices()
        if presets and self.selected not in presets:
            self.selected = presets[0]


def waveform(trace, width, height):
    middle = height // 2
    grid = [["-" if row == middle and column % 2 == 0 else " "
             for column in range(width)] for row in range(height)]
    if trace:
        scale = max(0.10, max(abs(sample) for sample in trace) * 1.1)
        previous = middle
        for column in range(width):
            sample = trace[column * len(trace) // width]
            row = middle - round(sample / scale * middle)
            for between in range(min(previous, row) + 1, max(previous, row)):
                grid[between][column] = ":"
            grid[row][column] = "*" if row != middle else "."
            previous = row
    return ["".join(row) for row in grid]


def spectrum_bars(levels, width, height, peaks=()):
    count = len(BAND_EDGES) - 1
    levels = levels or (0.0,) * count
    edges = [width * index // count for index in range(count + 1)]
    peak_rows = [ceil(peak * height) - 1 for peak in peaks]
    rows = []
    for row in range(height - 1, -1, -1):
        bars = []
        for index, (level, start, end) in enumerate(zip(levels, edges, edges[1:])):
            fill = level * height - row
            mark = "#" if fill >= 0.75 else "-" if fill >= 0.25 else " "
            bar = mark * (end - start - 1)
            if peak_rows and peak_rows[index] == row:
                bar = "^".center(len(bar), mark)
            bars.append(bar + " ")
        rows.append("".join(bars))
    return rows


def dial(count, active, width):
    if count == 1:
        return "01", "^"
    slots = [round(index * (width - 2) / (count - 1)) for index in range(count)]
    labels = [" "] * width
    ticks = ["-"] * (width - 1)
    for index, slot in enumerate(slots):
        labels[slot:slot + 2] = f"{index + 1:02}"
        ticks[slot] = "^" if index == active else "+"
    return "".join(labels), "".join(ticks)


def header_offset(elapsed, space):
    if space <= 0:
        return 0
    step = int(max(0, elapsed) * HEADER_COLUMNS_PER_SECOND) % (2 * space)
    return min(step, 2 * space - step)


def screen(radio, columns, rows, now=None):
    columns -= 1
    if not radio.fullscreen:
        columns = min(columns, 96)
    if columns < 49 or rows < 19:
        return "Resize terminal to 50 x 19, or press Q."[:max(1, columns)]

    inner = columns - 4
    palette = PALETTES[radio.theme] if not os.environ.get("NO_COLOR") else PALETTES["Mono"]

    def paint(value, tone):
        color = palette.get(tone)
        return f"{ESC}{color}m{value}{ESC}0m" if color else value

    def framed(value):
        return paint("| ", "dim") + value + paint(" |", "dim")

    def row(value="", tone="text"):
        return framed(paint(fit_cells(value, inner), tone))

    def scope_row(value, live):
        if not live or radio.theme != "Chromatic" or not palette:
            return row(value, "active" if live else "dim")
        slices = []
        count = len(SPECTRUM_COLORS)
        for index, rgb in enumerate(SPECTRUM_COLORS):
            segment = value[inner * index // count:inner * (index + 1) // count]
            slices.append(f"{ESC}38;2;{rgb}m{segment}{ESC}0m" if segment.strip() else segment)
        return framed("".join(slices))

    def split(left, right):
        right = right[:inner // 2]
        return fit_cells(left, inner - len(right) - 1) + " " + right

    rule = paint("+" + "-" * (columns - 2) + "+", "dim")
    if radio.music_help:
        parts = list(path_lines(MUSIC, inner))
        visible = rows - 9
        return "\r\n".join([
            rule,
            row("L O C A L   M U S I C", "accent"),
            rule,
            row(radio.music_help, "warning"),
            *(row(part) for part in parts[:visible]),
            *([row("...  Press C for the full path.")] if len(parts) > visible else []),
            row(radio.music_note, "accent"),
            row("O Open | C Copy | L Scan | ESC Back", "dim"),
            row("Paste into the file manager's address bar.", "dim"),
            rule,
        ])

    if radio.history_open:
        top = [
            rule,
            row("T R A C K   H I S T O R Y", "accent"),
            rule,
        ]
        if radio.history:
            tracks = list(radio.history)
            visible = rows - 9
            start = max(0, radio.history_index - visible + 1)
            for index, track in enumerate(tracks[start:start + visible], start):
                cursor = ">" if index == radio.history_index else " "
                top.append(row(f"{cursor} {track.heard_at[5:]}  {track.title}",
                               "active" if cursor == ">" else "text"))
            top.append(rule)
            top.append(row(f"Source: {tracks[radio.history_index].source}"))
        else:
            top.append(row("Play a local file or a station with track titles."))
        top.extend([
            row(radio.history_note, "warning"),
            row("UP/DOWN Browse | C Copy | T Theme | H Back", "dim"),
            rule,
        ])
        return "\r\n".join(top)

    live = radio.status in ("ON AIR", "PLAYING")
    tuning = radio.playback is not None or radio.retry_at is not None
    local = radio.local_index is not None
    active = radio.tuned if tuning and not local else radio.selected
    station = radio.source() if local else radio.stations[active]
    status_color = "active" if live else "warning"
    height = 5 if rows >= 28 else 3
    if local:
        labels = "LOCAL MUSIC"
        ticks = f"Track {radio.local_index + 1} of {len(radio.local_tracks)}"
    else:
        labels, ticks = dial(len(radio.stations), active, inner)
    filled = round(radio.volume / 10)
    volume = "MUTED" if radio.muted else f"[{'#' * filled}{'.' * (10 - filled)}] {radio.volume:3}%"
    if radio.show_spectrum:
        view = "SPECTRUM 20Hz-20kHz"
        plot = spectrum_bars(radio.playback.bands if live else (), inner, height)
    else:
        view = "AUDIO SCOPE"
        plot = waveform(radio.playback.trace if live else (), inner, height)
    message = radio.message
    if live:
        title = radio.playback.title
        message = f"Now playing: {title}" if title else "Track title unavailable"
    if radio.settings_warning:
        message = radio.settings_warning
    if radio.fullscreen:
        bands = radio.playback.bands if live else ()
        peaks = radio.playback.peaks if live else ()
        third = inner // 3
        axis = ("BASS".center(third) + "MIDS".center(third) + "TREBLE".center(inner - 2 * third))
        return "\r\n".join([
            rule,
            row(split("SPECTRUM 20Hz-20kHz", f"[ {radio.status} ]"), status_color),
            row(split(station.name, f"VOL {volume}"), "accent"),
            row(message),
            rule,
            *(scope_row(line, live) for line in spectrum_bars(bands, inner, rows - 10, peaks)),
            row(axis, "accent"),
            rule,
            row(f"X Back | SPACE Play/stop | T {radio.theme}", "dim"),
            rule,
        ])

    title = "N I G H T   S I G N A L"
    status = f"[ {radio.status} ]"
    if inner < 54 and radio.status == "WAITING TO RETRY":
        status = "[ RETRY WAIT ]"
    star_text = f"{' ' if inner < 54 else ' Star: '}{GITHUB_REPO}"
    star_label = paint("*", "star") + paint(star_text, "accent")
    star_link = f"\x1b]8;;https://{GITHUB_REPO}\x1b\\{star_label}\x1b]8;;\x1b\\"
    now = time.monotonic() if now is None else now
    elapsed = now - radio.header_started if radio.header_motion else 0
    title_left = header_offset(elapsed, inner - len(status) - 1 - len(title))
    star_space = inner - 1 - len(star_text)
    star_left = header_offset(elapsed, star_space)

    lines = [
        rule,
        row(split(" " * title_left + title, status), status_color),
        framed(" " * star_left + star_link + " " * (star_space - star_left)),
        row(labels, "dim"),
        row(ticks, "accent"),
        row(split(station.name, station.genre)),
        row(split(view, f"VOL {volume}"), "accent"),
        *(scope_row(line, live) for line in plot),
        row(message, "text" if live else "warning"),
        rule,
    ]

    presets = list(range(len(radio.local_tracks))) if local else radio.preset_indices()
    visible = min(len(presets), rows - height - 15)
    selected = radio.local_index if local else radio.selected
    focus = presets.index(selected) if presets else 0
    start = min(max(0, focus - visible + 1), len(presets) - visible)
    for index in presets[start:start + visible]:
        preset = radio.local_tracks[index] if local else radio.stations[index]
        cursor = ">" if selected == index else " "
        on_air = "~" if tuning and (selected if local else radio.tuned) == index else " "
        favorite = "*" if not local and preset.url in radio.favorites else " "
        text = f"{cursor}{on_air}{favorite} {index + 1:02}  {preset.name}"
        if not local:
            text = split(text, preset.genre)
        lines.append(row(text, "active" if cursor == ">" else "text"))
    if not presets:
        lines.append(row("No favorites. Press Tab, then F to add one.", "warning"))

    shortcut = f"1-{len(radio.stations)} Tune" if len(radio.stations) > 1 else "1 Tune"
    tab_action = "TAB All" if radio.favorites_only else "TAB Favorites"
    lines.extend([
        row("N/P Next/prev | ENTER Play | SPACE Play/stop" if local
            else "UP/DOWN Browse | ENTER Tune | SPACE Play/stop", "dim"),
        row(f"{shortcut} | +/- Volume | M Mute | Q Quit", "dim"),
        row("V View | X Full | H History | L Radio | O Dir" if local
            else "V View | X Full | H History | L Local | O Dir", "dim"),
    ])
    appearance = f"T {radio.theme} | A {'On' if radio.header_motion else 'Off'}"
    lines.append(row(appearance if local else f"F Fav | {tab_action} | {appearance}", "dim"))
    lines.append(rule)
    return "\r\n".join(lines)


def frame_output(frame):
    # Clear every row past its right edge when returning from a wider view.
    return f"{ESC}H" + f"{ESC}K\r\n".join(frame.split("\r\n")) + f"{ESC}K{ESC}J"


@contextmanager
def terminal():
    arrows = {"A": "up", "B": "down", "C": "right", "D": "left"}
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(sys.stdout.fileno()))
        mode = wintypes.DWORD()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            raise OSError("Could not read terminal mode.")
        if not kernel.SetConsoleMode(handle, mode.value | 0x0004):
            raise OSError("Could not enable ANSI output. Try Windows Terminal.")

        def read_key():
            if not msvcrt.kbhit():
                time.sleep(0.06)
                return ""
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(msvcrt.getwch(), "")
            return key.lower()

        def restore():
            kernel.SetConsoleMode(handle, mode.value)
    else:
        import select
        import termios
        import tty

        descriptor = sys.stdin.fileno()
        mode = termios.tcgetattr(descriptor)
        tty.setcbreak(descriptor)

        def read_key():
            if not select.select([descriptor], [], [], 0.06)[0]:
                return ""
            key = os.read(descriptor, 1).decode("ascii", errors="ignore")
            if key == "\x1b" and select.select([descriptor], [], [], 0.02)[0]:
                prefix = os.read(descriptor, 1)
                if prefix in (b"[", b"O") and select.select([descriptor], [], [], 0.02)[0]:
                    return arrows.get(os.read(descriptor, 1).decode("ascii", errors="ignore"), "")
                return ""
            return key.lower()

        def restore():
            termios.tcsetattr(descriptor, termios.TCSADRAIN, mode)

    try:
        print(f"{ESC}?1049h{ESC}?25l{ESC}2J", end="", flush=True)
        yield read_key
    finally:
        try:
            print(f"{ESC}0m{ESC}?25h{ESC}?1049l", end="", flush=True)
        finally:
            restore()


def check_setup(stations):
    print(f"Python {sys.version.split()[0]}")
    print(f"Station presets: {len(stations)}")
    issues = 0
    for name in ("ffmpeg", "ffplay"):
        try:
            command = find_executable(name)
            result = subprocess.run([command, "-version"], capture_output=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"{name}: {error}", file=sys.stderr)
            issues += 1
        else:
            output = result.stdout.decode("utf-8", errors="replace").splitlines()
            print(output[0] if output else f"{name}: available")
    if issues == 0:
        print("Ready. Run with --demo to check audio and controls.")
    return int(issues > 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="print the station presets and exit")
    mode.add_argument("--demo", action="store_true", help="play an offline test signal")
    mode.add_argument("--check", action="store_true", help="check the playback dependencies and station presets")
    mode.add_argument("--play", nargs="+", metavar="PATH", help="play audio files or folders")
    args = parser.parse_args()
    try:
        stations = [Station("Test Signal", "Offline audio check")] if args.demo else load_presets()
        if args.check:
            return check_setup(stations)
        if args.list:
            for index, station in enumerate(stations, 1):
                print(f"{index:02}  {station.name:<16}  {station.genre}")
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise OSError("Open an interactive terminal to play Night Signal.")

        tracks = local_sources(args.play) if args.play else []
        radio = Radio(stations, find_executable("ffmpeg"), find_executable("ffplay"),
                      settings_path=None if args.demo else SETTINGS)
        try:
            with terminal() as read_key:
                if args.demo:
                    radio.tune()
                elif tracks:
                    radio.open_local(tracks)
                elif radio.tuned is not None:
                    radio.start_playback()
                last_frame = ""
                while True:
                    now = time.monotonic()
                    radio.update(now)
                    size = shutil.get_terminal_size(fallback=(80, 28))
                    frame = screen(radio, size.columns, size.lines, now)
                    if frame != last_frame:
                        print(frame_output(frame), end="", flush=True)
                        last_frame = frame
                    key = read_key()
                    if key in ("q", "\x03"):
                        break
                    if key:
                        radio.handle_key(key)
        finally:
            radio.stop()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        print(f"Night Signal: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
