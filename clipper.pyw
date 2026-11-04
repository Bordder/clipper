"""Bordders Clipper - instant replay for Windows.

Keeps a rolling buffer of your screen + audio and saves the last N seconds
to an .mp4 when you press a hotkey.

Speed path: ddagrab (GPU desktop duplication) -> NVENC/AMF/QSV hardware
encoder -> short .ts segments on disk in a ring. Audio is held in RAM.
Nothing is re-encoded when saving a clip (video is stream-copied).
"""

import ctypes
import functools
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import wave
import warnings
from collections import deque
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox
import winreg

import customtkinter as ctk

import numpy as np

try:
    import pystray
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    pystray = None

warnings.filterwarnings("ignore")  # soundcard "data discontinuity" spam

APP = "Clipper"
VERSION = "1.1.0"
FROZEN = getattr(sys, "frozen", False)  # running from the installed .exe
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).parent
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP
BUFFER_DIR = DATA_DIR / "buffer"
CONFIG_PATH = DATA_DIR / "settings.json"
LOG_PATH = DATA_DIR / "ffmpeg.log"
NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000  # below normal priority, the game gets the CPU first
LOW_PRIORITY = NO_WINDOW | BELOW_NORMAL
SEG_SECONDS = 2
SAMPLE_RATE = 48000

DEFAULTS = {
    "clip_seconds": 30,
    "hotkey": "alt+F10",
    "output_dir": str(Path.home() / "Videos" / "Clips"),
    "sound_on_save": True,
    "start_minimized": False,
    "start_with_windows": True,
    "close_to_tray": True,
    "sort_by_game": True,
    "monitor": 0,
    "fps": 60,
    "resolution": "native",
    "encoder": "auto",
    "codec": "h264",
    "quality": 23,
    "preset": "balanced",
    "show_cursor": True,
    "desktop_audio": True,
    "desktop_volume": 100,
    "mic_audio": False,
    "mic_device": "",
    "mic_volume": 100,
    "mic_noise_suppression": True,
    "separate_tracks": False,
    "audio_offset_ms": 0,
    "audio_bitrate": 160,
    "check_updates": True,
    "auto_update": True,
}

RESOLUTIONS = {"native": None, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480}
PRESETS = {
    "nvenc": {"speed": "p1", "balanced": "p4", "quality": "p6"},
    "amf": {"speed": "speed", "balanced": "balanced", "quality": "quality"},
    "qsv": {"speed": "veryfast", "balanced": "medium", "quality": "slower"},
    "cpu": {"speed": "ultrafast", "balanced": "veryfast", "quality": "faster"},
}


# ---------------------------------------------------------------- helpers

@functools.lru_cache(maxsize=None)
def ffmpeg_bin(name="ffmpeg"):
    bundled = APP_DIR / "ffmpeg" / f"{name}.exe"  # shipped with the installer
    if bundled.exists():
        return str(bundled)
    return shutil.which(name) or name


def run_quiet(args, timeout=30):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          creationflags=LOW_PRIORITY)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def list_monitors():
    """Monitor rects (x, y, w, h, primary) in Windows enumeration order."""
    user32 = ctypes.windll.user32
    mons = []

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(hmon, hdc, rect, lp):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        mons.append((r.left, r.top, r.right - r.left, r.bottom - r.top, bool(mi.dwFlags & 1)))
        return 1

    user32.SetProcessDPIAware()
    user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    return mons


NOT_GAMES = {"explorer", "searchhost", "shellexperiencehost", "startmenuexperiencehost",
             "applicationframehost", "lockapp", "python", "pythonw", "textinputhost",
             "shellhost", "widgets", "systemsettings"}


def file_description(path):
    """FileDescription from an exe's version info, e.g. 'Counter-Strike 2'."""
    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return None
    ptr, n = ctypes.c_void_p(), wintypes.UINT()
    if not ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(n)) or not n.value:
        return None
    lang, cp = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD * 2)).contents
    key = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription"
    if not ver.VerQueryValueW(buf, key, ctypes.byref(ptr), ctypes.byref(n)) or not n.value:
        return None
    return ctypes.wstring_at(ptr, n.value).strip("\0 ").strip() or None


def foreground_app():
    """Friendly name of the focused app (the game you are playing), or None."""
    user32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        exe = buf.value
    finally:
        k32.CloseHandle(h)
    if Path(exe).stem.lower() in NOT_GAMES:
        return None
    try:
        name = file_description(exe)
    except Exception:
        name = None
    return name or Path(exe).stem


def safe_folder_name(name):
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in bad or ord(ch) < 32 else ch for ch in name).strip(" .")
    return cleaned[:60] or "Desktop"


_encoder_cache = {}


def detect_encoder(codec):
    """First hardware encoder that actually works on this PC, else CPU."""
    if codec in _encoder_cache:
        return _encoder_cache[codec]
    for vendor in ("nvenc", "amf", "qsv"):
        name = f"{codec}_{vendor}"
        try:
            r = run_quiet([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                           "-i", "color=black:s=640x360", "-frames:v", "3", "-c:v", name,
                           "-f", "null", "-"], timeout=15)
            if r.returncode == 0:
                _encoder_cache[codec] = vendor
                return vendor
        except Exception:
            pass
    _encoder_cache[codec] = "cpu"
    return "cpu"


# ---------------------------------------------------------------- hotkey

MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
NAMED_KEYS = {"space": 0x20, "prior": 0x21, "next": 0x22, "end": 0x23, "home": 0x24,
              "insert": 0x2D, "delete": 0x2E, "pause": 0x13, "scroll_lock": 0x91,
              "print": 0x2C, "snapshot": 0x2C}


def parse_hotkey(text):
    mods, vk = 0, None
    for part in text.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in NAMED_KEYS:
            vk = NAMED_KEYS[part]
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x6F + int(part[1:])
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
    if vk is None:
        raise ValueError(f"Bad hotkey: {text}")
    return mods, vk


class HotkeyThread(threading.Thread):
    def __init__(self, combo, callback):
        super().__init__(daemon=True)
        self.combo, self.callback = combo, callback
        self.ready = threading.Event()
        self.ok = False
        self.tid = None

    def run(self):
        user32 = ctypes.windll.user32
        self.tid = ctypes.windll.kernel32.GetCurrentThreadId()
        try:
            mods, vk = parse_hotkey(self.combo)
            self.ok = bool(user32.RegisterHotKey(None, 1, mods | 0x4000, vk))  # 0x4000 = no repeat
        except ValueError:
            self.ok = False
        self.ready.set()
        if not self.ok:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                self.callback()
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self.tid and self.ok:
            ctypes.windll.user32.PostThreadMessageW(self.tid, 0x0012, 0, 0)  # WM_QUIT


# ---------------------------------------------------------------- audio

class AudioSource(threading.Thread):
    """Records one audio device into RAM. Chunks are placed by sample count, not arrival time, so joins don't click."""

    def __init__(self, device, keep_seconds):
        super().__init__(daemon=True)
        self.device = device
        self.keep = keep_seconds + 5
        self.chunks = deque()
        self.run_start = None  # perf_counter time of sample 0 of the current run
        self.run_pos = 0       # samples written in the current run
        self.stop_ev = threading.Event()
        self.error = None

    def run(self):
        ctypes.windll.ole32.CoInitializeEx(None, 0)
        try:
            # 200 ms buffer read every 50 ms (soundcard's record() busy-polls)
            with self.device.recorder(samplerate=SAMPLE_RATE, blocksize=SAMPLE_RATE // 5) as rec:
                while not self.stop_ev.wait(0.05):
                    parts = []
                    while rec._capture_available_frames() > 0:
                        parts.append(rec.record(numframes=None))
                    if not parts:
                        continue  # silent device: render() fills the gap with zeros
                    now = time.perf_counter()
                    data = np.concatenate(parts)
                    if data.ndim == 1:
                        data = data[:, None]
                    if data.shape[1] == 1:
                        data = np.repeat(data, 2, axis=1)
                    elif data.shape[1] > 2:
                        data = data[:, :2]
                    arrived_start = now - len(data) / SAMPLE_RATE
                    expected = (self.run_start + self.run_pos / SAMPLE_RATE) if self.run_start else None
                    # same stream: append. a big jump means a gap or drift, start a new run
                    if expected is None or abs(arrived_start - expected) > 0.1:
                        self.run_start, self.run_pos = arrived_start, 0
                    pcm = data.astype(np.float32)
                    self.chunks.append((self.run_start, self.run_pos, pcm))
                    self.run_pos += len(pcm)
                    while self.chunks and now - self.chunk_end(self.chunks[0]) > self.keep:
                        self.chunks.popleft()
        except Exception as e:
            self.error = str(e)

    def render(self, t_end, duration):
        """Float32 stereo covering [t_end - duration, t_end]; gaps = silence."""
        n = max(1, int(duration * SAMPLE_RATE))
        out = np.zeros((n, 2), np.float32)
        t0 = t_end - duration
        for run_start, pos, chunk in list(self.chunks):
            start = int(round((run_start - t0) * SAMPLE_RATE)) + pos
            a, b = max(0, start), min(n, start + len(chunk))
            if a < b:
                out[a:b] = chunk[a - start:b - start]
        return out

    @staticmethod
    def chunk_end(c):
        run_start, pos, chunk = c
        return run_start + (pos + len(chunk)) / SAMPLE_RATE

    def stop(self):
        self.stop_ev.set()


def soft_limit(x, knee=0.8, ceiling=0.97):
    """Soft limiter: leaves audio under `knee` alone and rounds peaks off below `ceiling`."""
    a = np.abs(x)
    over = a > knee
    if not over.any():
        return x
    room = ceiling - knee
    out = x.copy()
    out[over] = np.sign(x[over]) * (knee + room * np.tanh((a[over] - knee) / room))
    return out


def denoise(x, strength=2.0, floor=0.08, n=1024, hop=256):
    """Spectral noise gate for the mic. Learns the noise floor from the quietest 15% and cuts rumble."""
    mono = x.mean(axis=1) if x.ndim == 2 else x
    if len(mono) < n * 4:
        return x
    win = np.sqrt(np.hanning(n + 1)[:-1]).astype(np.float32)
    pad = np.concatenate([np.zeros(n, np.float32), mono.astype(np.float32), np.zeros(n, np.float32)])
    frames = np.lib.stride_tricks.sliding_window_view(pad, n)[::hop] * win
    spec = np.fft.rfft(frames, axis=1)
    mag = np.abs(spec) + 1e-9
    noise = np.percentile(mag, 15, axis=0)
    gain = np.clip(1 - strength * noise / mag, 0, 1)
    k = np.ones(5, np.float32) / 5
    gain = np.apply_along_axis(lambda g: np.convolve(g, k, "same"), 0, gain)  # over time
    gain = np.apply_along_axis(lambda g: np.convolve(g, k[:3] * 5 / 3, "same"), 1, gain)  # over freq
    gain = floor + (1 - floor) * gain
    freqs = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
    gain *= np.clip((freqs - 60) / 60, 0, 1)  # rumble cut: 0 at 60 Hz, full from 120 Hz
    out_frames = np.fft.irfft(spec * gain, n, axis=1).astype(np.float32) * win
    out = np.zeros(len(pad), np.float32)
    norm = np.zeros(len(pad), np.float32)
    for i, f in enumerate(out_frames):  # overlap-add
        out[i * hop:i * hop + n] += f
        norm[i * hop:i * hop + n] += win ** 2
    out = (out / np.maximum(norm, 1e-6))[n:n + len(mono)]
    return np.repeat(out[:, None], x.shape[1], axis=1) if x.ndim == 2 else out


def write_wav(path, samples):
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


# ---------------------------------------------------------------- recorder

class Recorder:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.proc = None
        self.audio = {}
        self.save_lock = threading.Lock()
        self.capture = "ddagrab"
        self.vendor = None
        self.started_at = 0

    # ---- ffmpeg command
    def build_cmd(self, capture):
        c = self.cfg
        fps = int(c["fps"])
        gop = fps  # keyframe every second
        height = RESOLUTIONS.get(c["resolution"])
        vendor = self.vendor
        cursor = "1" if c["show_cursor"] else "0"
        wrap = math.ceil(int(c["clip_seconds"]) / SEG_SECONDS) + 4
        cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "warning", "-y",
               "-filter_threads", "1", "-filter_complex_threads", "1"]

        sw_filters = []
        if height:
            sw_filters.append(f"scale=-2:{height}:flags=fast_bilinear")
        if vendor == "cpu":
            sw_filters.append("format=yuv420p")
        elif vendor in ("amf", "qsv"):
            sw_filters.append("format=nv12")

        if capture == "ddagrab":
            chain = f"ddagrab=output_idx={int(c['monitor'])}:framerate={fps}:draw_mouse={cursor}"
            if vendor != "nvenc" or sw_filters:
                # NVENC takes GPU frames directly; everything else needs a download
                chain += ",hwdownload,format=bgra"
            chain += "".join("," + f for f in sw_filters)
            cmd += ["-init_hw_device", "d3d11va", "-filter_complex", chain]
        else:
            mons = list_monitors()
            x, y, w, h, _ = mons[min(int(c["monitor"]), len(mons) - 1)] if mons else (0, 0, 1920, 1080, True)
            cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", cursor,
                    "-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}",
                    "-i", "desktop"]
            vf = sw_filters or (["format=nv12"] if vendor == "nvenc" else [])
            if vf:
                cmd += ["-vf", ",".join(vf)]

        q = str(int(c["quality"]))
        preset = PRESETS[vendor][c["preset"]]
        codec = c["codec"]
        if vendor == "nvenc":
            cmd += ["-c:v", f"{codec}_nvenc", "-preset", preset, "-tune", "hq", "-rc", "vbr",
                    "-cq", q, "-b:v", "0", "-bf", "0"]
        elif vendor == "amf":
            cmd += ["-c:v", f"{codec}_amf", "-quality", preset, "-rc", "cqp",
                    "-qp_i", q, "-qp_p", q]
        elif vendor == "qsv":
            cmd += ["-c:v", f"{codec}_qsv", "-preset", preset, "-global_quality", q]
        else:
            cmd += ["-c:v", "libx264" if codec == "h264" else "libx265",
                    "-preset", preset, "-crf", q]
        cmd += ["-g", str(gop), "-threads", "1" if vendor != "cpu" else "0", "-f", "segment", "-segment_time", str(SEG_SECONDS),
                "-segment_wrap", str(wrap), "-segment_format", "mpegts",
                "-flush_packets", "1", str(BUFFER_DIR / "seg%03d.ts")]
        return cmd

    # ---- lifecycle
    def start(self):
        self.stop()
        BUFFER_DIR.mkdir(parents=True, exist_ok=True)
        for f in BUFFER_DIR.glob("*.ts"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            if LOG_PATH.stat().st_size > 1_000_000:
                LOG_PATH.unlink()
        except OSError:
            pass
        enc = self.cfg["encoder"]
        self.vendor = detect_encoder(self.cfg["codec"]) if enc == "auto" else enc

        for capture in ("ddagrab", "gdigrab"):
            self.capture = capture
            cmd = self.build_cmd(capture)
            logf = open(LOG_PATH, "a", encoding="utf-8")
            logf.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] " + " ".join(cmd) + "\n\n")
            logf.flush()
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=logf, creationflags=LOW_PRIORITY)
            time.sleep(1.5)
            if self.proc.poll() is None:
                break
            self.log(f"{capture} capture failed, trying fallback...")
        else:
            self.proc = None
            raise RuntimeError(f"ffmpeg could not start. See log:\n{LOG_PATH}")

        self.started_at = time.perf_counter()
        self.start_audio()

    def start_audio(self):
        # worker thread: COM has to be initialised here for WASAPI
        ctypes.windll.ole32.CoInitializeEx(None, 0)
        import soundcard as sc
        keep = int(self.cfg["clip_seconds"])
        if self.cfg["desktop_audio"]:
            spk = sc.default_speaker()
            dev = sc.get_microphone(id=str(spk.name), include_loopback=True)
            self.audio["desktop"] = AudioSource(dev, keep)
        if self.cfg["mic_audio"]:
            name = self.cfg["mic_device"]
            dev = sc.get_microphone(name) if name else sc.default_microphone()
            self.audio["mic"] = AudioSource(dev, keep)
        for a in self.audio.values():
            a.start()

    def stop(self):
        for a in self.audio.values():
            a.stop()
        self.audio = {}
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
        self.proc = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    # ---- save
    def save_clip(self):
        if not self.running():
            raise RuntimeError("Recording was interrupted (sleep, lock screen or display change) "
                               "and is restarting. Try again in a few seconds.")
        game = foreground_app() if self.cfg["sort_by_game"] else None
        with self.save_lock:
            t_end = time.perf_counter() + self.cfg["audio_offset_ms"] / 1000.0
            want = int(self.cfg["clip_seconds"])
            segs = sorted(BUFFER_DIR.glob("seg*.ts"), key=lambda p: p.stat().st_mtime)
            segs = [s for s in segs if s.stat().st_size > 0]
            segs = segs[-(math.ceil(want / SEG_SECONDS) + 2):]
            if not segs:
                raise RuntimeError("Buffer is empty, wait a second.")

            out_dir = Path(self.cfg["output_dir"])
            prefix = "Clip"
            if self.cfg["sort_by_game"]:
                folder = safe_folder_name(game) if game else "Desktop"
                out_dir = out_dir / folder
                prefix = folder.replace(" ", "_")
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"{prefix}_{datetime.now():%Y-%m-%d_%H-%M-%S}.mp4"
            tmp = DATA_DIR / "tmp"
            tmp.mkdir(exist_ok=True)
            cut = tmp / "cut.mp4"
            if cut.exists():
                cut.unlink()

            ff = ffmpeg_bin()
            base = [ff, "-hide_banner", "-loglevel", "error", "-y"]
            # one stream-copy pass straight from the ring (no joined temp file)
            run_quiet(base + ["-sseof", f"-{want}", "-i", "concat:" + "|".join(str(s) for s in segs),
                              "-c", "copy", "-avoid_negative_ts", "make_zero", str(cut)])
            if not cut.exists():
                raise RuntimeError(f"Cut failed. See {LOG_PATH}")
            dur = float(run_quiet([ffmpeg_bin("ffprobe"), "-v", "error", "-show_entries",
                                   "format=duration", "-of", "csv=p=0", str(cut)]).stdout.strip() or want)

            tracks = self.render_audio(t_end, dur)
            if not tracks:
                shutil.move(str(cut), out)
                return out
            cmd = base + ["-i", str(cut)]
            wavs = []
            for i, samples in enumerate(tracks):
                w = tmp / f"a{i}.wav"
                write_wav(w, samples)
                wavs.append(w)
                cmd += ["-i", str(w)]
            cmd += ["-map", "0:v"] + sum((["-map", f"{i + 1}:a"] for i in range(len(wavs))), [])
            cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", f"{int(self.cfg['audio_bitrate'])}k",
                    "-shortest", "-movflags", "+faststart", str(out)]
            r = run_quiet(cmd, timeout=60)
            if r.returncode != 0:
                raise RuntimeError(r.stderr[-400:])
            return out

    def render_audio(self, t_end, dur):
        parts = {}
        for key, src in self.audio.items():
            vol = self.cfg[f"{key}_volume"] / 100.0
            samples = src.render(t_end, dur)
            if key == "mic" and self.cfg["mic_noise_suppression"]:
                samples = denoise(samples)
            parts[key] = samples * vol
        if not parts:
            return []
        mix = soft_limit(sum(parts.values()))
        if self.cfg["separate_tracks"] and len(parts) > 1:
            return [mix] + [soft_limit(p) for p in parts.values()]  # track 1 = mix, then each source
        return [mix]


# ---------------------------------------------------------------- playback

def probe_clip(path):
    """{duration, width, height, fps, audio (stream count)} for the in-app player."""
    r = run_quiet([ffmpeg_bin("ffprobe"), "-v", "error", "-show_entries",
                   "stream=codec_type,width,height,avg_frame_rate:format=duration", "-of", "json", str(path)])
    data = json.loads(r.stdout or "{}")
    streams = data.get("streams", [])
    video = next((st for st in streams if st.get("codec_type") == "video"), {})
    try:
        num, den = video.get("avg_frame_rate", "30/1").split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    except ValueError:
        fps = 30.0
    return {"duration": float(data.get("format", {}).get("duration") or 0),
            "width": int(video.get("width") or 1920), "height": int(video.get("height") or 1080),
            "fps": fps or 30.0, "audio": sum(st.get("codec_type") == "audio" for st in streams)}


class Playback:
    """One playback run from `start`: ffmpeg pipes raw RGB frames and PCM audio. Pause and seek make a new one."""

    def __init__(self, path, info, size, start, track=None, volume=1.0):
        self.w, self.h = size
        self.start = start
        self.track = track
        self.volume = volume
        self.fps = min(info["fps"], 60)
        self.frames = queue.Queue(maxsize=8)
        self.stop_ev = threading.Event()
        self.t0 = None
        self.video_done = False
        self.audio_live = track is not None
        self.procs = []
        seek = ["-ss", f"{start:.3f}", "-i", str(path)]
        vf = f"fps={self.fps},scale={self.w}:{self.h}:flags=bilinear"
        self.video_cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", *seek, "-map", "0:v:0",
                          "-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        self.audio_cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", *seek, "-map",
                          f"0:a:{track}", "-vn", "-f", "f32le", "-ac", "2", "-ar", str(SAMPLE_RATE), "-"]
        threading.Thread(target=self.read_video, daemon=True).start()
        if self.audio_live:
            threading.Thread(target=self.play_audio, daemon=True).start()

    def spawn(self, cmd):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self.procs.append(proc)
        return proc

    def read_video(self):
        try:
            proc = self.spawn(self.video_cmd)
            size = self.w * self.h * 3
            i = 0
            while not self.stop_ev.is_set():
                buf = proc.stdout.read(size)
                if len(buf) < size:
                    break
                item = (self.start + i / self.fps, buf)
                i += 1
                while not self.stop_ev.is_set():
                    try:
                        self.frames.put(item, timeout=0.1)
                        break
                    except queue.Full:
                        pass
        finally:
            self.video_done = True

    def play_audio(self):
        try:
            # import before CoInitializeEx, soundcard inits COM on first import
            import soundcard as sc
            ctypes.windll.ole32.CoInitializeEx(None, 0)
            proc = self.spawn(self.audio_cmd)
            block = 1024
            # bigger buffer so UI work doesn't cause crackles
            with sc.default_speaker().player(samplerate=SAMPLE_RATE, channels=2, blocksize=block * 4) as sp:
                while not self.stop_ev.is_set():
                    buf = proc.stdout.read(block * 8)
                    if len(buf) < 8:
                        break
                    pcm = np.frombuffer(buf[:len(buf) // 8 * 8], np.float32).reshape(-1, 2) * self.volume
                    if self.t0 is None:
                        # start together with the video: wait until the first frame is decoded
                        while self.frames.empty() and not self.video_done and not self.stop_ev.is_set():
                            time.sleep(0.005)
                        self.t0 = time.perf_counter()
                    sp.play(pcm)
        except Exception:
            pass  # no output device etc.: the picture still plays on the wall clock
        finally:
            self.audio_live = False

    def position(self):
        return self.start + (time.perf_counter() - self.t0 if self.t0 else 0.0)

    def stop(self):
        self.stop_ev.set()
        for proc in self.procs:
            try:
                proc.kill()
            except Exception:
                pass


# ---------------------------------------------------------------- updates

REPO = "Bordder/clipper"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
UPDATE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
UPDATE_DIR = DATA_DIR / "updates"
SETUP_ASSET = "BorddersClipper-Setup.exe"
CHECK_EVERY = 6 * 3600


def parse_version(text):
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def latest_release():
    """Newest release on GitHub as (version, asset dict or None, page url), or None."""
    import urllib.request
    req = urllib.request.Request(UPDATE_API, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"BorddersClipper/{VERSION}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name", "")
    asset = next((a for a in data.get("assets", []) if a.get("name") == SETUP_ASSET), None)
    return tag.lstrip("v"), asset, data.get("html_url") or RELEASES_URL


def download_update(version, asset):
    """Download the installer and check it against the size and sha256 GitHub reports."""
    import urllib.request
    url = asset.get("browser_download_url", "")
    if not url.startswith(f"https://github.com/{REPO}/releases/download/"):
        raise ValueError("unexpected download url")
    digest = (asset.get("digest") or "").lower()
    want = digest[7:] if digest.startswith("sha256:") else None
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    out = UPDATE_DIR / f"BorddersClipper-Setup-{version}.exe"
    part = out.with_suffix(".part")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": f"BorddersClipper/{VERSION}"})
    with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
        while chunk := r.read(1 << 20):
            h.update(chunk)
            f.write(chunk)
    if part.stat().st_size != asset.get("size") or (want and h.hexdigest() != want):
        part.unlink(missing_ok=True)
        raise ValueError("download did not match the release")
    part.replace(out)
    return out


def run_installer(setup, relaunch_args):
    """Wait for this process to exit, install silently, then start the new version."""
    import base64
    exe = APP_DIR / "Clipper.exe"
    q = lambda p: "'" + str(p).replace("'", "''") + "'"
    args = f" -ArgumentList {', '.join(q(a) for a in relaunch_args)}" if relaunch_args else ""
    script = (f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; "
              f"Start-Process -Wait -FilePath {q(setup)} -ArgumentList "
              f"'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART'; "
              f"Remove-Item -LiteralPath {q(setup)} -ErrorAction SilentlyContinue; "
              f"Start-Process -FilePath {q(exe)}{args}")
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    cmd = ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", enc]
    # not DETACHED_PROCESS: powershell quits at once without a console
    flags = NO_WINDOW | 0x00000200  # CREATE_NEW_PROCESS_GROUP
    try:  # outlive us even if whatever launched us put us in a kill-on-close job
        subprocess.Popen(cmd, creationflags=flags | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        subprocess.Popen(cmd, creationflags=flags)


# ---------------------------------------------------------------- GUI

import hashlib
import re
import traceback
import tkinter.font as tkfont

try:
    from PIL import ImageFont
except ImportError:
    ImageFont = None

NAME = "Bordders Clipper"

# colours
BG = "#111214"
RAIL = "#0c0d0f"
SURFACE = "#1a1c20"
SURFACE_2 = "#23262b"
SURFACE_3 = "#2d3036"
INPUT_BG = "#0e0f11"
LINE = "#26292e"
LINE_HI = "#363a41"
KEY_LIP = "#0a0b0d"
TEXT = "#f1f2f4"
TEXT_2 = "#b4b9c2"
MUTED = "#7d838d"
FAINT = "#4b5058"
ACCENT = "#e5484d"
ACCENT_HI = "#d33a40"
ACCENT_DIM = "#4a1f22"
GREEN = "#3fb950"
ORANGE = "#f0a020"
GRAY = "#5b616b"

SHOW_FLAG = DATA_DIR / "show.flag"
ICON_PATH = Path(getattr(sys, "_MEIPASS", APP_DIR)) / "clipper.ico"
APP_ID = "Clipper.InstantReplay"
START_MENU = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
SHORTCUT = START_MENU / f"{NAME}.lnk"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
THUMB_DIR = DATA_DIR / "thumbs"
THUMB_INDEX = THUMB_DIR / "index.json"
THUMB_W, THUMB_H = 256, 144          # logical; images are made at 2x (512x288)
MAX_CARDS = 60
CARD_CACHE = 100  # card widgets kept around for the game filter
THUMB_V = 3                          # bump when the card image design changes: old ones regenerate once
ALL_GAMES = "All games"              # game filter sentinel
AUTO_NAME = re.compile(r".+_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")

GAP, MIN_CARD, MAX_CARD = 16, 248, 400
STATUS_SLOT = 320  # fixed slot so the hotkey hint doesn't move
SETTINGS_MAX = 880                   # widest the settings column gets (logical px)

G_CLIPS, G_SETTINGS, G_REFRESH, G_FOLDER = "\uE714", "\uE713", "\uE72C", "\uE838"
G_PAUSE, G_PLAY, G_RENAME, G_DELETE, G_FILM = "\uE769", "\uE768", "\uE70F", "\uE74D", "\uE8B2"
G_BACK, G_VOLUME, G_MUTE, G_OPEN = "\uE72B", "\uE767", "\uE74F", "\uE8A7"

FONT_DIR = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
KEY_NAMES = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win",
             "prior": "PgUp", "next": "PgDn", "space": "Space", "return": "Enter", "escape": "Esc"}


def ensure_shortcut():
    """Start Menu shortcut with our AppUserModelID, so a pinned app gets our icon."""
    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
        from win32com.shell import shell
    except ImportError:
        return
    old = START_MENU / "Clipper.lnk"
    if old.exists():
        old.unlink()
    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None,
                                      pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    script = Path(__file__).resolve()
    link.SetPath(str(Path(sys.executable).with_name("pythonw.exe")))
    link.SetArguments(f'"{script}"')
    link.SetWorkingDirectory(str(script.parent))
    link.SetIconLocation(str(ICON_PATH), 0)
    link.SetDescription("Instant replay clipper")
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID, pythoncom.VT_LPWSTR))
    store.Commit()
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(SHORTCUT), 0)


def scan_clips(root):
    """All .mp4 in the clips folder and its game subfolders, newest first."""
    root = Path(root)
    if not root.exists():
        return []
    clips = []
    for d in [root] + [p for p in root.iterdir() if p.is_dir()]:
        clips += d.glob("*.mp4")
    return sorted(clips, key=lambda f: f.stat().st_mtime, reverse=True)


def folder_signature(root):
    """Cheap change detector: folder mtimes change on add/delete/rename."""
    root = Path(root)
    try:
        return tuple((d.name, d.stat().st_mtime) for d in [root] + [p for p in root.iterdir() if p.is_dir()])
    except OSError:
        return ()


def game_of(clip, root):
    return clip.parent.name if clip.parent != Path(root) else "Clip"


def fmt_duration(sec):
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


def recycle(path):
    from win32com.shell import shell, shellcon
    flags = shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT
    shell.SHFileOperation((0, shellcon.FO_DELETE, str(path), None, flags, None, None))


def elide(text, font, max_px):
    """Trim text with an ellipsis so it fits max_px (logical) in font."""
    if font.measure(text) <= max_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid].rstrip() + "\u2026") <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "\u2026"


def mid_elide(text, font, max_px):
    """For paths: keep the first 12 characters + ... + as much of the tail as fits."""
    if font.measure(text) <= max_px or len(text) <= 14:
        return text
    head = text[:12] + "\u2026"
    tail = text[12:]
    while tail and font.measure(head + tail) > max_px:
        tail = tail[1:]
    return head + tail


def key_label(k):
    low = k.lower()
    if low in KEY_NAMES:
        return KEY_NAMES[low]
    if len(k) == 1:
        return k.upper()
    return k[:1].upper() + k[1:]


_badge_font = None


def badge_font():
    global _badge_font
    if _badge_font is None:
        try:
            _badge_font = ImageFont.truetype(str(FONT_DIR / "seguisb.ttf"), 22)
        except Exception:
            try:
                _badge_font = ImageFont.load_default(size=22)
            except Exception:
                _badge_font = ImageFont.load_default()
    return _badge_font


def compose_card(jpg, duration, out):
    """512x288 RGBA card image: frame + duration badge + rounded alpha corners."""
    size = (THUMB_W * 2, THUMB_H * 2)
    img = Image.open(jpg).convert("RGBA").resize(size, Image.LANCZOS)
    if duration > 0:
        text = fmt_duration(duration)
        f = badge_font()
        overlay = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        l, t, r, b = d.textbbox((0, 0), text, font=f)
        x1, y1 = size[0] - 16, size[1] - 16
        x0, y0 = x1 - (r - l) - 24, y1 - 40
        # dark pill with a faint edge, readable on any frame
        d.rounded_rectangle((x0, y0, x1, y1), radius=8, fill=(24, 26, 30, 220),
                            outline=(255, 255, 255, 40), width=2)
        d.text((x0 + 12 - l, y0 + (40 - (b - t)) / 2 - t), text, font=f, fill="#ffffff")
        img = Image.alpha_composite(img, overlay)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=16, fill=255)
    img.putalpha(mask)
    img.save(out)


class ThumbCache:
    """Thumbnail + duration per clip, cached on disk, made in the background."""

    def __init__(self, on_ready):
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self.index = json.loads(THUMB_INDEX.read_text(encoding="utf-8"))
        except Exception:
            self.index = {}
        self.lock = threading.Lock()
        self.jobs = queue.Queue()
        self.pending = set()
        self.failed = {}                 # str(clip) -> mtime of a file make() could not read
        self.on_ready = on_ready
        threading.Thread(target=self.worker, daemon=True).start()

    def get(self, clip):
        e = self.index.get(str(clip))
        try:
            mtime = clip.stat().st_mtime
            if self.failed.get(str(clip)) == mtime:
                return None  # ffmpeg could not read it; retry only when the file changes
            if (e and e.get("v") == THUMB_V and e["mtime"] == mtime
                    and Path(e["thumb"]).exists() and Path(e["card"]).exists()):
                return e
        except OSError:
            pass
        if str(clip) not in self.pending:
            self.pending.add(str(clip))
            self.jobs.put(clip)
        return None

    def worker(self):
        while True:
            clip = self.jobs.get()
            try:
                self.make(clip)
                self.failed.pop(str(clip), None)
                self.on_ready(str(clip))
            except Exception:
                try:
                    self.failed[str(clip)] = clip.stat().st_mtime
                except OSError:
                    pass
            self.pending.discard(str(clip))

    def make(self, clip):
        mtime = clip.stat().st_mtime
        stem = hashlib.md5(f"{clip}{mtime}".encode()).hexdigest()
        thumb = THUMB_DIR / (stem + ".jpg")
        card = THUMB_DIR / (stem + "-card.png")
        vf = (f"scale={THUMB_W * 2}:{THUMB_H * 2}:force_original_aspect_ratio=decrease,"
              f"pad={THUMB_W * 2}:{THUMB_H * 2}:(ow-iw)/2:(oh-ih)/2")
        for seek in ("1", "0"):
            run_quiet([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-ss", seek,
                       "-i", str(clip), "-frames:v", "1", "-vf", vf, "-q:v", "4", str(thumb)])
            if thumb.exists():
                break
        try:
            info = probe_clip(clip)
        except Exception:
            info = None
        duration = info["duration"] if info else 0.0
        compose_card(thumb, duration, card)
        with self.lock:
            self.index[str(clip)] = {"mtime": mtime, "thumb": str(thumb), "card": str(card),
                                     "duration": duration, "v": THUMB_V, "info": info}
            self.save()

    def info(self, clip):
        """Cached probe_clip() result, or None."""
        e = self.index.get(str(clip))
        try:
            if e and e.get("info") and e["mtime"] == clip.stat().st_mtime:
                return e["info"]
        except OSError:
            pass
        return None

    def remember_info(self, clip, info):
        with self.lock:
            e = self.index.get(str(clip))
            try:
                if e and e["mtime"] == clip.stat().st_mtime:
                    e["info"] = info
                    self.save()
            except OSError:
                pass

    def prune(self, alive):
        alive = {str(c) for c in alive}
        with self.lock:
            gone = [k for k in self.index if k not in alive]
            for key in gone:
                e = self.index.pop(key)
                for f in (e.get("thumb"), e.get("card")):
                    if f and Path(f).parent == THUMB_DIR:  # only ever delete our own files
                        Path(f).unlink(missing_ok=True)
            if gone:
                self.save()

    def save(self):
        THUMB_INDEX.write_text(json.dumps(self.index), encoding="utf-8")


class App:
    def __init__(self, hidden=False):
        self.cfg = load_config()
        ctk.set_appearance_mode("dark")
        self.root = ctk.CTk(fg_color=BG)
        self.root.title(NAME)
        # ctk sets its own icon shortly after start, override it after
        self.root.after(300, self.window_chrome)
        self.root.geometry("1100x700")
        self.root.minsize(960, 620)
        self.events = queue.Queue()
        self.rec = Recorder(self.cfg, lambda m: self.events.put(("status", m)))
        self.thumbs = ThumbCache(lambda p: self.events.put(("thumb", p)))
        self.hotkey = None
        self.fields = {}
        self.cards = {}
        self.images = {}
        self.order = []
        self.cols = 0
        self.card_w = 312
        self.thumb_size = (296, 166)
        self._canvas_w = 0
        self._reflow_job = None
        self._dirty = True
        self.sig = None
        self.pulse = False
        self.pulse_on = True
        self.tray = None
        self.ticks = 0
        # watchdog: capture dies on sleep/lock/display changes, restart it
        self.paused = False      # user paused: don't auto-restart
        self.starting = False    # rec.start() in flight on a worker thread
        self.retry_at = 0        # perf_counter time of the next restart attempt (0 = none)
        self.retry_delay = 2
        self.current = None
        self.count_text = ""
        self.games = []
        self.chip_key = None
        self.chips = {}
        self.more = None
        self.note = None
        self.empty_keys_host = None
        self.menu_clip = None
        self._narrow = False
        self._detail_empty = True
        self._detail_text = ""
        self._log_link = False
        self._capturing = False
        self._icons = {}
        self._measure = {}
        self._ph = {}
        self._cancel_capture = None
        self._quitting = False
        self._chips_used = 0
        self._settings_colw = None
        self._settings_pw = 0
        self._settings_job = None
        # in-app player
        self.pb = None              # current Playback, or None while paused/stopped
        self.play_clip = None
        self.play_info = None
        self.play_pos = 0.0         # position while paused
        self.playing = False
        self.play_track = 0
        self.play_volume = 1.0
        self.play_pending = None    # next decoded frame, not due yet
        self.play_photo = None
        self._play_job = None
        self._play_resize_job = None
        self._dragging = False
        # updates
        self.update_info = None     # newer release found: {"version", "setup", "url"}
        self.update_text = f"Version {VERSION}"
        self._checking = False
        self._checked_once = False
        self._notified = None

        self.build_fonts()
        self.placeholder = self.make_placeholder()
        self.build()
        self.build_menu()
        self.root.bind("<Configure>", self.on_root_configure, add="+")
        self.root.bind("<Button-1>", lambda e: self.hide_menu(), add="+")
        self.root.bind("<Unmap>", lambda e: self.hide_menu() if e.widget is self.root else None, add="+")
        self.root.bind("<Map>", self.on_map, add="+")
        self.bind_f5()
        # started in the tray: skip the library until the window is shown
        self.show_page("Clips", populate=not hidden and not self.cfg["start_minimized"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_tray()
        self.set_startup(self.cfg["start_with_windows"])
        if hidden or self.cfg["start_minimized"]:
            self.hide_to_tray() if self.tray else self.root.iconify()
        self.root.after(100, self.pump)
        self.root.after(200, lambda: self.start_buffer(quiet=True))
        self.root.after(700, self.animate)
        self.root.after(15000, self.auto_check)
        if not (hidden or self.cfg["start_minimized"]):
            self.root.after(600, self.ensure_settings)

    def window_chrome(self):
        if ICON_PATH.exists():
            try:
                self.root.iconbitmap(str(ICON_PATH))
            except Exception:
                pass
        try:  # dark caption, coloured like the window background
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(ctypes.c_int(0x00141211)), 4)
        except Exception:
            pass

    # ================================================================ design system
    def build_fonts(self):
        fams = set(tkfont.families(self.root))
        semi = ("Segoe UI Semibold", "normal") if "Segoe UI Semibold" in fams else ("Segoe UI", "bold")

        def f(fam, weight, size):
            return ctk.CTkFont(family=fam, size=size, weight=weight)

        self.F = {
            "display": f("Segoe UI", "bold", 22),
            "h2": f(*semi, 17),
            "title": f(*semi, 15),
            "card": f(*semi, 14),
            "body": f("Segoe UI", "normal", 13),
            "body_sb": f(*semi, 13),
            "small": f("Segoe UI", "normal", 12),
            "small_sb": f(*semi, 12),
            "micro": f(*semi, 11),
        }

    def measure(self, text, role):
        k = (text, role)
        if k not in self._measure:
            self._measure[k] = self.F[role].measure(text)
        return self._measure[k]

    def icon(self, glyph, px, color):
        key = (glyph, px, color)
        if key in self._icons:
            return self._icons[key]
        img = None
        if ImageFont is not None:
            for name in ("SegoeIcons.ttf", "segmdl2.ttf"):
                try:
                    fnt = ImageFont.truetype(str(FONT_DIR / name), px * 2)
                except Exception:
                    continue
                size = px * 2
                img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                l, t, r, b = d.textbbox((0, 0), glyph, font=fnt)
                d.text(((size - (r - l)) / 2 - l, (size - (b - t)) / 2 - t), glyph, font=fnt, fill=color)
                break
        self._icons[key] = ctk.CTkImage(img, img, size=(px, px)) if img else None
        return self._icons[key]

    def make_placeholder(self):
        w, h = THUMB_W * 2, THUMB_H * 2
        top, bot = (0x1d, 0x20, 0x25), (0x16, 0x18, 0x1b)
        grad = Image.new("RGBA", (1, h))
        for y in range(h):
            k = y / (h - 1)
            grad.putpixel((0, y), tuple(round(a + (b - a) * k) for a, b in zip(top, bot)) + (255,))
        img = grad.resize((w, h))
        film = self.icon(G_FILM, 48, FAINT)
        if film is not None:
            g = film.cget("dark_image")
            img.alpha_composite(g, ((w - g.width) // 2, (h - g.height) // 2))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=16, fill=255)
        img.putalpha(mask)
        self._ph_img = img
        return ctk.CTkImage(img, img, size=self.thumb_size)

    def label(self, parent, text, role, color, height, anchor="w", **kw):
        return ctk.CTkLabel(parent, text=text, font=self.F[role], text_color=color, height=height,
                            anchor=anchor, corner_radius=0, fg_color="transparent", **kw)

    def px(self, v):
        """Logical -> physical px, for the few plain tk widgets (1px lines)."""
        return round(v * self.scale(self.root))

    def pad(self, a, b):
        """Scaled (a, b) padding for pack_configure, which CTk does not scale (pack() does)."""
        return (self.px(a), self.px(b))

    def line(self, parent, vertical=False, length=None, color=LINE):
        # plain tk frames: CTkFrame does not draw at 1px
        if vertical:
            return tk.Frame(parent, bg=color, width=1, height=self.px(length) if length else 1, bd=0,
                            highlightthickness=0)
        return tk.Frame(parent, bg=color, height=1, bd=0, highlightthickness=0)

    @staticmethod
    def resize_image(img, size, scaling=None):
        """Resize a CTkImage without keeping every old PhotoImage. With scaling, the old size stays cached too."""
        if tuple(img.cget("size")) == tuple(size):
            return
        keep = ()
        if scaling is not None:
            keep = (img._get_scaled_size(scaling), (round(size[0] * scaling), round(size[1] * scaling)))
        img._scaled_light_photo_images = {k: v for k, v in img._scaled_light_photo_images.items() if k in keep}
        img._scaled_dark_photo_images = {k: v for k, v in img._scaled_dark_photo_images.items() if k in keep}
        img.configure(size=size)

    def base_shift(self, role, ref):
        """px to move a `role` label down so its baseline matches a same-height `ref` label."""
        a, b = self.F[role].metrics(), self.F[ref].metrics()
        return round(((b["ascent"] - b["descent"]) - (a["ascent"] - a["descent"])) / 2)

    def btn(self, parent, kind, text, icon=None, width=None, height=32, border_color=LINE, **kw):
        if kind == "primary":
            fg, hover, tc, role, bw = ACCENT, ACCENT_HI, "#ffffff", "body_sb", 0
        elif kind == "secondary":
            fg, hover, tc, role, bw = SURFACE_2, SURFACE_3, TEXT, "body", 0
        else:  # ghost
            fg, hover, tc, role, bw = "transparent", SURFACE_2, TEXT_2, "body", 1
        img = self.icon(icon, 16, tc) if icon else None
        if width is None:
            width = -(-(self.measure(text, role) + (24 if img else 0) + 32) // 4) * 4
        return ctk.CTkButton(parent, text=text, image=img, compound="left", width=width, height=height,
                             corner_radius=8, fg_color=fg, hover_color=hover, text_color=tc,
                             font=self.F[role], border_width=bw, border_color=border_color,
                             border_spacing=6 if img else 2, **kw)

    def keycaps(self, parent, hotkey, bg=BG):
        """Row of keycaps for a hotkey."""
        box = ctk.CTkFrame(parent, fg_color=bg, corner_radius=0, height=28)
        keys = [k for k in hotkey.split("+") if k][:5]
        for i, k in enumerate(keys):
            text = key_label(k)
            w = max(24, -(-(self.measure(text, "small_sb") + 16) // 2) * 2)
            lip = ctk.CTkFrame(box, width=w, height=24, corner_radius=6, fg_color=KEY_LIP, bg_color=bg)
            lip.pack(side="left", padx=(0, 4 if i < len(keys) - 1 else 0), pady=(3, 1))
            lip.pack_propagate(False)
            face = ctk.CTkFrame(lip, width=w, height=22, corner_radius=6, fg_color=SURFACE_2,
                                border_width=1, border_color=LINE_HI, bg_color=bg)
            face.place(x=0, y=0)
            face.pack_propagate(False)
            for f in (lip, face):  # avoid CTk rounding at 125% scaling
                self.exact_draw(f)
            self.label(face, text, "small_sb", TEXT_2, 16, anchor="center").place(
                relx=0.5, rely=0.5, anchor="center")
        return box

    def scale(self, widget):
        return ctk.ScalingTracker.get_widget_scaling(widget)

    def scrollable(self, parent):
        sf = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0, border_width=0,
                                    scrollbar_button_color=SURFACE_2, scrollbar_button_hover_color=SURFACE_3)
        try:
            sf._scrollbar._border_spacing = 2
            sf._scrollbar.configure(width=8)
        except Exception:
            pass
        return sf

    # ================================================================ layout
    def build(self):
        r = self.root
        r.grid_columnconfigure(0, minsize=72)
        r.grid_columnconfigure(2, weight=1)
        r.grid_rowconfigure(0, weight=1)
        self.build_rail()
        self.line(r, vertical=True).grid(row=0, column=1, sticky="ns")
        self.main = ctk.CTkFrame(r, fg_color=BG, corner_radius=0)
        self.main.grid(row=0, column=2, sticky="nsew")
        self.build_topbar()
        self.line(self.main).pack(fill="x")
        self.page_host = ctk.CTkFrame(self.main, fg_color=BG, corner_radius=0)
        self.page_host.pack(fill="both", expand=True)
        self.pages = {"Clips": ctk.CTkFrame(self.page_host, fg_color=BG, corner_radius=0),
                      "Settings": ctk.CTkFrame(self.page_host, fg_color=BG, corner_radius=0),
                      "Player": ctk.CTkFrame(self.page_host, fg_color=BG, corner_radius=0)}
        # pages are stacked and raised, re-packing is slow with lots of cards
        for p in self.pages.values():
            p.place(x=0, y=0, relwidth=1, relheight=1)
        tk.Misc.bind(self.pages["Settings"], "<Configure>", self.on_settings_page, add="+")
        self.build_clips(self.pages["Clips"])
        self.build_player(self.pages["Player"])
        self._settings_built = False  # built shortly after startup, see ensure_settings

    def build_rail(self):
        rail = ctk.CTkFrame(self.root, fg_color=RAIL, width=72, corner_radius=0)
        rail.grid(row=0, column=0, sticky="ns")
        rail.pack_propagate(False)
        self.logo = None
        if ICON_PATH.exists():
            try:
                logo = Image.open(ICON_PATH)
                self.logo = ctk.CTkImage(logo, logo, size=(32, 32))
            except Exception:
                self.logo = None
        ctk.CTkLabel(rail, text="", image=self.logo, width=32, height=32, corner_radius=0,
                     fg_color="transparent").pack(pady=(16, 24))
        self.nav = {}
        self.nav_glyph = {"Clips": G_CLIPS, "Settings": G_SETTINGS}
        for name in self.nav_glyph:
            self.nav[name] = self.nav_item(rail, name)
        # drawn 4 wide, the rail clips 1px
        self.indicator = ctk.CTkFrame(rail, width=4, height=24, corner_radius=2, fg_color=ACCENT)

    def nav_item(self, rail, name):
        """Rail item: icon over a small label."""
        item = ctk.CTkFrame(rail, width=56, height=56, corner_radius=12, fg_color=RAIL)
        item.pack(pady=(0, 8))
        item.pack_propagate(False)
        item.icon = ctk.CTkLabel(item, text="", image=self.icon(self.nav_glyph[name], 20, MUTED), width=20,
                                 height=20, corner_radius=0, fg_color="transparent")
        item.icon.pack(pady=(8, 4))
        item.text = self.label(item, name, "micro", MUTED, 16, anchor="center", width=48)
        item.text.pack()
        item.selected = False
        item.hover = False
        tk.Frame.configure(item, cursor="hand2")

        def enter(_):
            if not item.hover:
                item.hover = True
                if not item.selected:
                    item.configure(fg_color=SURFACE_2)

        def check():
            try:
                w = self.root.winfo_containing(*self.root.winfo_pointerxy())
            except Exception:
                w = None
            p = str(item)
            if w is None or not (str(w) == p or str(w).startswith(p + ".")):
                item.hover = False
                if not item.selected:
                    item.configure(fg_color=RAIL)

        for w in (item, item.icon, item.text):
            w.bind("<Button-1>", lambda e, n=name: self.show_page(n), add="+")
            w.bind("<Enter>", enter, add="+")
            w.bind("<Leave>", lambda e: self.root.after_idle(check), add="+")
        return item

    def build_topbar(self):
        bar = ctk.CTkFrame(self.main, fg_color=BG, corner_radius=0, height=64)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        inner = ctk.CTkFrame(bar, fg_color=BG, corner_radius=0)
        inner.pack(fill="both", expand=True, padx=24)

        self.page_title = self.label(inner, "Clips", "display", TEXT, 28, anchor="sw")
        self.page_title.pack(side="left")
        d = max(0, self.F["display"].metrics("descent") - self.F["small"].metrics("descent"))
        self.count_lbl = self.label(inner, "", "small", MUTED, 28, anchor="sw")
        self.count_lbl.pack(side="left", padx=(12, 0), pady=(0, d))

        # right side, from the right edge inwards
        self.toggle_btn = self.btn(inner, "ghost", "Pause", icon=G_PAUSE, width=96, command=self.toggle)
        self.toggle_btn.pack(side="right")

        # status pill sits right-aligned in a fixed slot
        slot = ctk.CTkFrame(inner, fg_color=BG, corner_radius=0, height=32)
        slot.pack(side="right", padx=(0, 8))
        slot.grid_columnconfigure(0, minsize=self.px(STATUS_SLOT))
        self.status_slot = slot
        slot.grid_rowconfigure(0, minsize=self.px(32))
        pill = ctk.CTkFrame(slot, fg_color=SURFACE, corner_radius=16, height=32, border_width=1,
                            border_color=LINE)
        pill.grid(row=0, column=0, sticky="e")
        self.exact_draw(pill)
        self.pill = pill
        self.dot = ctk.CTkFrame(pill, width=8, height=8, corner_radius=4, fg_color=GRAY)
        self.dot.pack(side="left", padx=(12, 8), pady=12)
        self.status_title = self.label(pill, "Starting...", "body_sb", TEXT, 20)
        self.status_title.pack(side="left", padx=(0, 16))
        self.status_detail = self.label(pill, "", "small", MUTED, 20)
        # the 12px detail shares the 13px title's baseline
        self.detail_pady = (2 * self.base_shift("small", "body_sb"), 0)
        self.pill_widgets = (pill, self.dot, self.status_title, self.status_detail)
        for w in self.pill_widgets:
            w.bind("<Button-1>", self.on_pill_click, add="+")

        # hotkey hint
        self.hint_host = ctk.CTkFrame(inner, fg_color=BG, corner_radius=0, height=28)
        self.hint_host.pack(side="right", padx=(0, 16), pady=(2, 0))
        self.hint_keys = self.keycaps(self.hint_host, self.cfg["hotkey"])
        self.hint_keys.pack()
        self.hint_lbl = self.label(inner, f"Save last {self.cfg['clip_seconds']} s", "small", MUTED, 24,
                                   anchor="e")
        self.hint_lbl.pack(side="right", padx=(0, 8), pady=(2, 0))
        self._labels_key = (self.cfg["hotkey"], self.cfg["clip_seconds"])

    def on_root_configure(self, e):
        if e.widget is not self.root:
            return
        try:
            w = e.width / ctk.ScalingTracker.get_window_scaling(self.root)
        except Exception:
            w = e.width
        narrow = w < 1080
        if narrow != self._narrow:
            self._narrow = narrow
            # narrow window: no detail text
            self.status_slot.grid_columnconfigure(0, minsize=self.px(160 if narrow else STATUS_SLOT))
            self.sync_detail()

    def sync_detail(self):
        if self._log_link:  # stopped: the pill opens the log
            self.status_detail.configure(text="Open log" if self._narrow else self._detail_text)
        show = (not self._narrow or self._log_link) and not self._detail_empty
        if show and not self.status_detail.winfo_manager():
            self.status_title.pack_configure(padx=0)
            self.status_detail.pack(side="left", padx=(8, 16), pady=self.detail_pady)
        elif not show and self.status_detail.winfo_manager():
            self.status_detail.pack_forget()
            self.status_title.pack_configure(padx=self.pad(0, 16))  # pack_configure is not CTk-scaled

    def show_page(self, name, populate=True):
        self.hide_menu()
        if name != "Settings":
            self.cancel_capture()
        if name != "Player":
            self.stop_player()
        self.current = name
        nav_name = "Clips" if name == "Player" else name  # the player is part of the library
        for n, item in self.nav.items():
            on = n == nav_name
            item.selected = on
            item.configure(fg_color=SURFACE_2 if on or item.hover else RAIL)
            item.icon.configure(image=self.icon(self.nav_glyph[n], 20, TEXT if on else MUTED))
            item.text.configure(text_color=TEXT if on else MUTED)
        self.indicator.place(in_=self.nav[nav_name], x=-9, rely=0.5, anchor="w")
        self.indicator.lift()
        if name == "Settings":
            self.ensure_settings()
        tk.Misc.tkraise(self.pages[name])
        if name != "Player":  # open_player sets the clip's own title
            self.page_title.configure(text=name)
            self.count_lbl.configure(text=self.count_text if name == "Clips" else "")
        if name == "Clips" and populate:
            self.refresh_clips()
            self.catch_up_thumbs()
            self.schedule_reflow_idle()

    # ================================================================ clips page
    def build_clips(self, page):
        bar = ctk.CTkFrame(page, fg_color=BG, corner_radius=0, height=32)
        bar.pack(fill="x", padx=24, pady=(24, 16))
        bar.pack_propagate(False)
        self.toolbar = bar
        self.btn(bar, "secondary", "Open folder", icon=G_FOLDER, width=128,
                 command=self.open_folder).pack(side="right")
        self.btn(bar, "secondary", "Refresh", icon=G_REFRESH, width=104,
                 command=lambda: self.refresh_clips(force=True)).pack(side="right", padx=(0, 8))
        self.chip_bar = ctk.CTkFrame(bar, fg_color=BG, corner_radius=0, height=32)
        self.chip_bar.pack(side="left", fill="x", expand=True)
        self.game_var = tk.StringVar(value=ALL_GAMES)
        self.more_var = tk.StringVar(value="More")

        host = ctk.CTkFrame(page, fg_color=BG, corner_radius=0)
        host.pack(fill="both", expand=True)
        self.clips_host = host
        self.grid = self.scrollable(host)
        self.grid.pack(fill="both", expand=True, padx=(24, 8))
        self.canvas = self.grid._parent_canvas
        self.canvas.bind("<Configure>", self.on_canvas, add="+")
        self.empty = ctk.CTkFrame(host, fg_color=BG, corner_radius=0)
        self.empty_body = None
        self.showing_empty = False

    # ---- filter chips
    def chip_width(self, label):
        return self.measure(label, "body") + 32

    def chip_budget(self):
        if not self._canvas_w:
            return 600
        s = self.scale(self.grid)
        return int(self._canvas_w / s) - 8 - 128 - 8 - 104 - 16

    def fit_count(self):
        budget = self.chip_budget()
        used = self.chip_width("All") + 8
        widths = [self.chip_width(elide(g, self.F["body"], 120)) + 8 for g in self.games]
        if used + sum(widths) <= budget:
            return len(self.games)
        n = 0
        for w in widths:
            if used + w + 96 > budget:
                break
            used += w
            n += 1
        return n

    def update_chips(self):
        n = self.fit_count()
        key = (tuple(self.games), n)
        if key != self.chip_key:
            self.chip_key = key
            for w in self.chip_bar.winfo_children():
                w.destroy()
            self.chips = {}
            self.more = None
            self._chips_used = 0
            for g in [ALL_GAMES] + self.games[:n]:
                text = "All" if g == ALL_GAMES else elide(g, self.F["body"], 120)
                self._chips_used += self.chip_width(text) + 8
                b = ctk.CTkButton(self.chip_bar, text=text, height=32, corner_radius=16, border_width=0,
                                  width=self.chip_width(text), font=self.F["body"],
                                  command=lambda v=g: self.pick_game(v))
                b.pack(side="left", padx=(0, 8))
                self.chips[g] = b
            rest = self.games[n:]
            if rest:
                self.more = ctk.CTkOptionMenu(
                    self.chip_bar, values=rest, variable=self.more_var, height=32, width=96,
                    corner_radius=16, fg_color=SURFACE_2, button_color=SURFACE_2,
                    button_hover_color=SURFACE_3, text_color=TEXT_2, font=self.F["body"],
                    dropdown_font=self.F["body"], dropdown_fg_color=SURFACE_2,
                    dropdown_hover_color=SURFACE_3, dropdown_text_color=TEXT, dynamic_resizing=False,
                    command=self.pick_game)
                self.more.pack(side="left")
        self.color_chips()

    def color_chips(self):
        pick = self.game_var.get()
        for g, b in self.chips.items():
            on = g == pick
            b.configure(fg_color=TEXT if on else SURFACE_2, hover_color=TEXT if on else SURFACE_3,
                        text_color=BG if on else TEXT_2)
        if self.more is not None:
            if pick != ALL_GAMES and pick not in self.chips:
                # the selected overflow game gets the room it needs (within what is left)
                room = max(96, self.chip_budget() - self._chips_used)
                text = elide(pick, self.F["body"], min(120, room - 52))
                width = min(room, -(-(self.measure(text, "body") + 52) // 4) * 4)
                self.more_var.set(text)
                self.more.configure(width=max(96, width), fg_color=TEXT, button_color=TEXT,
                                    button_hover_color=TEXT, text_color=BG)
            else:
                self.more_var.set("More")
                self.more.configure(width=96, fg_color=SURFACE_2, button_color=SURFACE_2,
                                    button_hover_color=SURFACE_3, text_color=TEXT_2)

    def pick_game(self, game):
        self.game_var.set(game)
        self.refresh_clips(force=True)
        self.canvas.yview_moveto(0)

    # ---- library
    def refresh_clips(self, force=False):
        root = self.cfg["output_dir"]
        sig = (folder_signature(root), self.game_var.get())
        if not force and sig == self.sig:
            return
        self.sig = sig
        all_clips = scan_clips(root)
        self.thumbs.prune(all_clips)
        games, seen = [], set()
        for c in all_clips:  # newest clip first
            g = game_of(c, root)
            if g not in seen:
                seen.add(g)
                games.append(g)
        self.games = games
        if self.game_var.get() not in [ALL_GAMES] + games:
            self.game_var.set(ALL_GAMES)
        pick = self.game_var.get()
        clips = [c for c in all_clips if pick == ALL_GAMES or game_of(c, root) == pick]
        n = len(clips)
        self.count_text = f"{n} clip{'s' if n != 1 else ''}" if n else ""
        # empty library: hide the toolbar
        if games and not self.toolbar.winfo_manager():
            self.toolbar.pack(fill="x", padx=24, pady=(24, 16), before=self.clips_host)
        elif not games and self.toolbar.winfo_manager():
            self.toolbar.pack_forget()
        if self.current == "Clips":
            self.count_lbl.configure(text=self.count_text)
        self.update_chips()

        shown = clips[:MAX_CARDS]
        keys = [str(c) for c in shown]
        alive = set(keys)
        # filtered-out cards are hidden and reused, gone or extra ones destroyed
        on_disk = {str(c) for c in all_clips}
        hidden = [k for k in self.cards if k not in alive]
        drop = [k for k in hidden if k not in on_disk]
        keep = [k for k in hidden if k in on_disk]
        drop += keep[:max(0, len(self.cards) - len(drop) - CARD_CACHE)]
        for k in hidden:
            if k in drop:
                self.cards.pop(k)["frame"].destroy()
                self.images.pop(k, None)
            else:
                e = self.cards[k]
                tk.Grid.grid_remove(e["frame"])
                if e["hover"]:
                    e["hover"] = False
                    e["paint"](False)
        for c in shown:
            if str(c) not in self.cards:
                self.cards[str(c)] = self.make_card(c)
        if keys != self.order:
            self.order = keys
            self._dirty = True

        over = n > MAX_CARDS
        if over and self.note is None:
            self.note = self.label(self.grid, f"Showing the newest {MAX_CARDS}. Open folder to see the rest.",
                                   "small", MUTED, 16)
            self._dirty = True
        elif not over and self.note is not None:
            self.note.destroy()
            self.note = None

        if not clips:
            self.show_empty(pick)
        else:
            self.hide_empty()
            self.schedule_reflow_idle()

    def show_empty(self, pick):
        if self.empty_body is not None:
            self.empty_body.destroy()
        self.empty_keys_host = None
        body = ctk.CTkFrame(self.empty, fg_color=BG, corner_radius=0)
        body.place(relx=0.5, rely=0.42, anchor="center")
        self.empty_body = body
        mark = ctk.CTkFrame(body, width=64, height=64, corner_radius=32, fg_color=ACCENT_DIM)
        mark.pack()
        ctk.CTkFrame(mark, width=20, height=20, corner_radius=10, fg_color=ACCENT).place(
            relx=0.5, rely=0.5, anchor="center")
        title = "No clips yet" if pick == ALL_GAMES else f"No clips for {elide(pick, self.F['h2'], 240)}"
        self.label(body, title, "h2", TEXT, 24, anchor="center").pack(pady=(16, 8))
        if pick == ALL_GAMES:
            row = ctk.CTkFrame(body, fg_color=BG, corner_radius=0, height=28)
            row.pack()
            self.label(row, "Press", "body", MUTED, 24).pack(side="left")
            self.empty_keys_host = ctk.CTkFrame(row, fg_color=BG, corner_radius=0, height=28)
            self.empty_keys_host.pack(side="left", padx=8)
            self.keycaps(self.empty_keys_host, self.cfg["hotkey"]).pack()
            self.empty_hint = self.label(
                row, f"in a game to save the last {self.cfg['clip_seconds']} seconds.", "body", MUTED, 24)
            self.empty_hint.pack(side="left")
        self.btn(body, "secondary", "Open folder", icon=G_FOLDER, width=128,
                 command=self.open_folder).pack(pady=(24, 0))
        if not self.showing_empty:
            self.grid.pack_forget()
            self.empty.pack(fill="both", expand=True)
            self.showing_empty = True

    def hide_empty(self):
        if self.showing_empty:
            self.empty.pack_forget()
            self.grid.pack(fill="both", expand=True, padx=(24, 8))
            self.showing_empty = False
            self._dirty = True
        if self.empty_body is not None:
            self.empty_body.destroy()
            self.empty_body = None
            self.empty_keys_host = None

    # ---- grid layout
    def on_canvas(self, e):
        self._canvas_w = e.width
        if not self.visible():
            self._dirty = True
            return
        if self._reflow_job:
            self.root.after_cancel(self._reflow_job)
        self._reflow_job = self.root.after(250, self.reflow)

    def schedule_reflow_idle(self):
        if self.canvas.winfo_width() > 1:
            self.root.after_idle(self.reflow)

    def reflow(self):
        self._reflow_job = None
        if not self.visible():
            self._dirty = True
            return
        cw = self.canvas.winfo_width() if self.canvas.winfo_ismapped() else self._canvas_w
        if cw <= 1:
            return
        self._canvas_w = cw
        s = self.scale(self.grid)
        # layout in physical px, spare px go into the cards from the right
        avail_px = cw + 1 - round(8 * s)  # cards end 8 left of the scrollbar (+1: its own 1px pad)
        try:  # exact at any scaling: cards end where the toolbar (Open folder) ends
            if self.toolbar.winfo_ismapped() and self.canvas.winfo_ismapped():
                avail_px = (self.toolbar.winfo_rootx() + self.toolbar.winfo_width()
                            - self.canvas.winfo_rootx())
        except Exception:
            pass
        gap_px = round(GAP * s)
        inset = round(8 * s)
        cols = max(1, int((avail_px / s + GAP) // (MIN_CARD + GAP)))
        room = avail_px - gap_px * (cols - 1)
        max_px = round(MAX_CARD * s)
        base = min(room // cols, max_px)
        widths = [base] * cols
        if room // cols < max_px:
            for c in range(room - base * cols):
                widths[cols - 1 - c] += 1
        th = round((base - 2 * inset) * 9 / 16)  # one thumb height for every card: rows line up
        self.card_w = base / s
        self.thumb_size = ((base - 2 * inset + 0.25) / s, (th + 0.25) / s)
        self.resize_image(self.placeholder, self.thumb_size)
        sizes = [(w, w - 2 * inset, th) for w in widths]
        if self.chip_key is not None and self.fit_count() != self.chip_key[1]:
            self.update_chips()
        if not self.order:
            return
        for i, k in enumerate(self.order):
            self.size_card(self.cards[k], sizes[i % cols], s)
        layout = (cols, gap_px)
        if layout != getattr(self, "_layout", None) or self._dirty:
            self._layout, self.cols, self._dirty = layout, cols, False
            for i, k in enumerate(self.order):
                col = i % cols
                tk.Grid.grid_configure(self.cards[k]["frame"], row=i // cols, column=col, sticky="nw",
                                       padx=(0, gap_px if col < cols - 1 else 0), pady=(0, gap_px))
            if self.note is not None:
                self.note.grid(row=(len(self.order) - 1) // cols + 1, column=0, columnspan=cols,
                               sticky="w", pady=(8, 24))
            self.grid.grid_columnconfigure(0, weight=0)
        self.root.after_idle(lambda: self.sync_scrollbar(self.grid))

    def sync_scrollbar(self, sf):
        """Hide the scrollbar thumb (keep its slot, so nothing shifts) when nothing scrolls."""
        try:
            fits = sf.winfo_reqheight() <= sf._parent_canvas.winfo_height()
        except Exception:
            return
        if getattr(sf, "_sb_fits", None) != fits:
            sf._sb_fits = fits
            sf._scrollbar.configure(button_color=BG if fits else SURFACE_2,
                                    button_hover_color=BG if fits else SURFACE_3)

    # ---- cards
    def make_card(self, clip):
        root = self.cfg["output_dir"]
        tw, th = self.thumb_size
        frame = ctk.CTkFrame(self.grid, fg_color=SURFACE, corner_radius=12, border_width=0,
                             width=self.card_w, height=th + 68)
        frame.pack_propagate(False)
        self.exact_draw(frame)
        thumb = ctk.CTkLabel(frame, text="", fg_color="transparent", width=tw, height=th,
                             corner_radius=0, image=self.placeholder)
        thumb.pack(padx=8, pady=(8, 8), anchor="w")
        game = game_of(clip, root)
        renamed = not AUTO_NAME.match(clip.stem)
        full = clip.stem if renamed else game
        title = self.label(frame, elide(full, self.F["card"], self.card_w - 24), "card", TEXT, 20)
        title.pack(fill="x", padx=12)
        try:
            st = clip.stat()
            d = datetime.fromtimestamp(st.st_mtime)
            mb = st.st_size / 1e6
        except OSError:
            d, mb = datetime.now(), 0
        when = f"{d:%d %b}" + (f" {d:%Y}" if d.year != datetime.now().year else "")
        size = f"{mb:.1f}" if mb < 10 else f"{mb:.0f}"
        text = f"{when} \u00b7 {d:%H:%M} \u00b7 {size} MB"
        if renamed:
            text = f"{game} \u00b7 {text}"
        meta = self.label(frame, elide(text, self.F["small"], self.card_w - 24), "small", MUTED, 16)
        meta.pack(fill="x", padx=12, pady=(4, 12))
        entry = {"frame": frame, "thumb": thumb, "title": title, "meta": meta, "full": full,
                 "meta_full": text, "ready": False, "hover": False, "size": None, "key": str(clip)}

        def paint(on):
            frame.configure(fg_color=SURFACE_2 if on else SURFACE)

        def enter(_):
            if not entry["hover"]:
                entry["hover"] = True
                paint(True)

        def check():
            if not entry["hover"] or not frame.winfo_exists():
                return
            try:
                w = self.root.winfo_containing(*self.root.winfo_pointerxy())
            except Exception:
                w = None
            fp = str(frame)
            if w is None or not (str(w) == fp or str(w).startswith(fp + ".")):
                entry["hover"] = False
                paint(False)

        entry["paint"] = paint
        tk.Frame.configure(frame, cursor="hand2")
        for w in (frame, thumb, title, meta):
            w.bind("<Button-1>", lambda e, c=clip: self.open_player(c), add="+")
            w.bind("<Button-3>", lambda e, c=clip: self.clip_menu(e, c), add="+")
            w.bind("<Enter>", enter, add="+")
            w.bind("<Leave>", lambda e: self.root.after_idle(check), add="+")
        self.apply_thumb(entry, clip)
        return entry

    def size_card(self, e, size, s):
        """size = (widget px, thumb px, thumb height px). Only touches what changed."""
        if e["size"] == size:
            return
        old = e["size"]
        e["size"] = size
        w, tw, th = size

        def lg(v):  # logical value that lands on exactly v physical px (tk rounds, CTkImage truncates)
            return (v + 0.25) / s

        h = th + round(68 * s)
        e["frame"].configure(width=lg(w), height=lg(h))
        e["thumb"].configure(width=lg(tw), height=lg(th))
        img = self.images.get(e["key"])
        if img is not None:
            if old is None or old[1:] != size[1:]:
                self.resize_image(img, (lg(tw), lg(th)), s)
        else:
            e["thumb"].configure(image=self.placeholder_for(tw, th, s))
        if old is None or old[0] != w:
            text_w = w / s - 24
            e["title"].configure(text=elide(e["full"], self.F["card"], text_w))
            e["meta"].configure(text=elide(e["meta_full"], self.F["small"], text_w))

    def exact_draw(self, frame):
        """Make a CTkFrame draw at its exact widget size (CTk rounds to even px)."""
        try:
            frame._draw_engine.set_round_to_even_numbers(False, False)
        except Exception:
            return

        def sync(e):
            s = self.scale(frame)
            w, h = e.width / s, e.height / s
            if (w, h) != (frame._current_width, frame._current_height):
                frame._current_width, frame._current_height = w, h
                frame._draw(no_color_updates=True)

        tk.Misc.bind(frame, "<Configure>", sync, add="+")

    def placeholder_for(self, tw, th, s):
        """Shared placeholder image per thumb size."""
        key = (tw, th)
        if key not in self._ph:
            if len(self._ph) > 4:
                self._ph.clear()
            self._ph[key] = ctk.CTkImage(self._ph_img, self._ph_img, size=((tw + 0.25) / s, (th + 0.25) / s))
        return self._ph[key]

    def apply_thumb(self, entry, clip):
        info = self.thumbs.get(clip)
        if not info:
            entry["ready"] = False
            return
        try:
            img = Image.open(info["card"])
            img.load()
        except Exception:
            entry["ready"] = False
            return
        if entry["size"] is not None:
            s = self.scale(self.grid)
            size = ((entry["size"][1] + 0.25) / s, (entry["size"][2] + 0.25) / s)
        else:
            size = self.thumb_size
        cimg = ctk.CTkImage(img, img, size=size)
        self.images[entry["key"]] = cimg
        entry["thumb"].configure(image=cimg)
        entry["ready"] = True

    def catch_up_thumbs(self):
        for k, e in list(self.cards.items()):
            if not e["ready"]:
                self.apply_thumb(e, Path(k))

    # ---- context menu (one themed popup, built once)
    def build_menu(self):
        pop = tk.Toplevel(self.root)
        pop.withdraw()
        pop.overrideredirect(True)
        pop.transient(self.root)
        pop.configure(bg=LINE_HI)
        box = ctk.CTkFrame(pop, fg_color=SURFACE_2, corner_radius=0)
        box.pack(padx=1, pady=1)
        inner = ctk.CTkFrame(box, fg_color=SURFACE_2, corner_radius=0)
        inner.pack(padx=4, pady=4)
        items = (("Play", G_PLAY, self.open_player),
                 ("Open in default player", G_OPEN, lambda c: os.startfile(c)),
                 ("Show in folder", G_FOLDER, lambda c: subprocess.Popen(["explorer", "/select,", str(c)])),
                 ("Rename\u2026", G_RENAME, self.rename_clip),
                 (None, None, None),
                 ("Delete", G_DELETE, self.delete_clip))
        for text, g, action in items:
            if text is None:
                self.line(inner, color=LINE_HI).pack(fill="x", padx=self.px(8), pady=self.px(4))
                continue
            color = ACCENT if text == "Delete" else TEXT
            ctk.CTkButton(inner, text=text, image=self.icon(g, 16, ACCENT if text == "Delete" else TEXT_2),
                          compound="left", height=32, width=184, anchor="w", corner_radius=6,
                          fg_color="transparent", hover_color=SURFACE_3, font=self.F["body"],
                          text_color=color, border_spacing=6,
                          command=lambda a=action: self.menu_action(a)).pack()
        pop.bind("<Escape>", lambda e: self.hide_menu())
        pop.bind("<FocusOut>", lambda e: self.root.after(50, self.menu_focus_check))
        self.popup = pop

    def clip_menu(self, event, clip):
        self.menu_clip = clip
        pop = self.popup
        pop.update_idletasks()
        w, h = pop.winfo_reqwidth(), pop.winfo_reqheight()
        x = min(event.x_root, pop.winfo_screenwidth() - w)
        y = min(event.y_root, pop.winfo_screenheight() - h)
        pop.geometry(f"+{max(0, x)}+{max(0, y)}")
        pop.deiconify()
        pop.lift()
        pop.focus_force()

    def menu_action(self, action):
        clip = self.menu_clip
        self.hide_menu()
        if clip is not None:
            action(clip)

    def menu_focus_check(self):
        try:
            f = self.popup.focus_get()
        except Exception:
            f = None
        if f is None or not str(f).startswith(str(self.popup)):
            self.hide_menu()

    def hide_menu(self):
        pop = getattr(self, "popup", None)
        if pop is not None and pop.winfo_ismapped():
            pop.withdraw()

    def rename_clip(self, clip):
        d = ctk.CTkInputDialog(title="Rename clip", text="New name:", fg_color=BG,
                               button_fg_color=ACCENT, button_hover_color=ACCENT_HI)
        name = d.get_input()
        if not name:
            return
        name = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch for ch in name).strip(" .")
        if name.lower().endswith(".mp4"):
            name = name[:-4].rstrip(" .")
        if not name:
            messagebox.showerror(NAME, "That name is not valid.")
            return
        target = clip.with_name(name + ".mp4")
        if target.exists():
            messagebox.showerror(NAME, "A clip with that name already exists.")
            return
        clip.rename(target)
        self.refresh_clips(force=True)

    def delete_clip(self, clip):
        if not messagebox.askyesno(NAME, f"Move this clip to the Recycle Bin?\n\n{clip.name}"):
            return
        try:
            recycle(clip)
        except Exception as e:
            messagebox.showerror(NAME, f"Could not delete: {e}")
        self.refresh_clips(force=True)

    # ================================================================ player page
    def build_player(self, page):
        bar = ctk.CTkFrame(page, fg_color=BG, corner_radius=0, height=48)
        bar.pack(fill="x", padx=24, pady=(16, 12))
        bar.pack_propagate(False)
        self.btn(bar, "ghost", "Clips", icon=G_BACK, command=lambda: self.show_page("Clips")).pack(side="left")
        self.btn(bar, "ghost", "Open in default player", icon=G_OPEN,
                 command=lambda: self.play_clip and os.startfile(self.play_clip)).pack(side="right")

        # plain tk: frames are drawn by us at physical size, CTk scaling would only blur them
        self.video_host = tk.Frame(page, bg="#000000", bd=0, highlightthickness=0, cursor="hand2")
        self.video_host.pack(fill="both", expand=True, padx=self.px(24))
        self.video = tk.Label(self.video_host, bg="#000000", bd=0, highlightthickness=0, cursor="hand2")
        self.video.place(relx=0.5, rely=0.5, anchor="center")
        for w in (self.video_host, self.video):
            w.bind("<Button-1>", lambda e: self.toggle_play(), add="+")
        self.video_host.bind("<Configure>", self.on_video_resize, add="+")

        ctl = ctk.CTkFrame(page, fg_color=BG, corner_radius=0, height=64)
        ctl.pack(fill="x", padx=24)
        ctl.pack_propagate(False)
        self.play_btn = ctk.CTkButton(ctl, text="", image=self.icon(G_PLAY, 18, TEXT), width=40, height=40,
                                      corner_radius=8, fg_color=SURFACE_2, hover_color=SURFACE_3,
                                      command=self.toggle_play)
        self.play_btn.pack(side="left")
        self.time_lbl = self.label(ctl, "0:00 / 0:00", "small_sb", TEXT_2, 20, anchor="w", width=92)
        self.time_lbl.pack(side="left", padx=(12, 12))

        # right side, from the right edge inwards
        self.vol_slider = ctk.CTkSlider(ctl, from_=0, to=1, width=96, height=16, button_length=0,
                                        progress_color=TEXT_2, fg_color=SURFACE_3, button_color=TEXT,
                                        button_hover_color=TEXT, command=self.on_volume)
        self.vol_slider.set(self.play_volume)
        self.vol_slider.pack(side="right")
        self.vol_icon = ctk.CTkLabel(ctl, text="", image=self.icon(G_VOLUME, 16, TEXT_2), width=20,
                                     height=20, corner_radius=0, fg_color="transparent", cursor="hand2")
        self.vol_icon.pack(side="right", padx=(0, 8))
        self.vol_icon.bind("<Button-1>", lambda e: self.toggle_mute(), add="+")
        self.track_btn = ctk.CTkSegmentedButton(ctl, values=["Mix"], height=32, font=self.F["small_sb"],
                                                fg_color=SURFACE_2, selected_color=SURFACE_3,
                                                selected_hover_color=SURFACE_3, unselected_color=SURFACE_2,
                                                unselected_hover_color=SURFACE_3, text_color=TEXT,
                                                command=self.on_track)
        self.seek = ctk.CTkSlider(ctl, from_=0, to=1, height=16, button_length=0, progress_color=ACCENT,
                                  fg_color=SURFACE_3, button_color=TEXT, button_hover_color=TEXT,
                                  command=self.on_seek_drag)
        self.seek.pack(side="left", fill="x", expand=True, padx=(0, 16))
        self.seek.bind("<ButtonRelease-1>", self.on_seek_release, add="+")

        r = self.root
        for key, fn in (("<space>", self.toggle_play), ("<Left>", lambda: self.seek_by(-5)),
                        ("<Right>", lambda: self.seek_by(5)), ("<Escape>", lambda: self.show_page("Clips"))):
            r.bind(key, lambda e, f=fn: f() if self.current == "Player" else None, add="+")

    def open_player(self, clip):
        clip = Path(clip)
        info = self.thumbs.info(clip)  # probed when the thumbnail was made
        if info is None:
            try:
                info = probe_clip(clip)
            except Exception as e:
                messagebox.showerror(NAME, f"Could not open clip: {e}")
                return
            self.thumbs.remember_info(clip, info)
        self.show_page("Player")
        if self.play_photo is not None:  # clear the previous clip's frame
            self.video.configure(image="")
            self.play_photo = None
        self.play_clip, self.play_info = clip, info
        self.play_pos, self.play_track = 0.0, 0
        self.page_title.configure(text=elide(clip.stem, self.F["display"], 520))
        game = clip.parent.name if clip.parent != Path(self.cfg["output_dir"]) else ""
        self.count_lbl.configure(text=game)
        self.seek.configure(to=max(info["duration"], 0.1))
        self.seek.set(0)
        names = {1: ["Mix"], 3: ["Mix", "Game", "Mic"]}.get(info["audio"],
                                                            [f"Track {i + 1}" for i in range(info["audio"])])
        self.track_btn.configure(values=names)
        self.track_btn.set(names[0] if names else "")
        if info["audio"] > 1:
            self.track_btn.pack(side="right", padx=(0, 16))
        else:
            self.track_btn.pack_forget()
        self.video.focus_set()  # space must not press whichever button had focus
        self.root.update_idletasks()
        self.start_playback(0.0, play=True)

    def video_size(self):
        """Fit the clip into the video area (physical px), never upscaled, even sides for ffmpeg."""
        info = self.play_info
        aw, ah = max(self.video_host.winfo_width(), 64), max(self.video_host.winfo_height(), 36)
        k = min(aw / info["width"], ah / info["height"], 1.0)
        return max(2, int(info["width"] * k) // 2 * 2), max(2, int(info["height"] * k) // 2 * 2)

    def start_playback(self, pos, play):
        """(Re)start at pos. play=False decodes just one frame, to show where a paused seek landed."""
        if self.pb:
            self.pb.stop()
        info = self.play_info
        pos = max(0.0, min(pos, max(0.0, info["duration"] - 0.05)))
        self.play_pos = pos
        self.playing = play
        self.play_pending = None
        track = self.play_track if (play and info["audio"]) else None
        self.pb = Playback(self.play_clip, info, self.video_size(), pos, track, self.play_volume)
        self.play_btn.configure(image=self.icon(G_PAUSE if play else G_PLAY, 18, TEXT))
        self.update_time(pos)
        if self._play_job is None:
            self._play_job = self.root.after(5, self.player_tick)

    def stop_player(self):
        if self.pb:
            self.play_pos = self.pb.position() if self.playing else self.play_pos
            self.pb.stop()
            self.pb = None
        self.playing = False
        if self._play_job is not None:
            self.root.after_cancel(self._play_job)
            self._play_job = None
        if hasattr(self, "play_btn"):
            self.play_btn.configure(image=self.icon(G_PLAY, 18, TEXT))

    def player_tick(self):
        self._play_job = None
        pb = self.pb
        if pb is None:
            return
        if pb.t0 is None and not pb.audio_live and not pb.frames.empty():
            pb.t0 = time.perf_counter()  # no audio to follow: run on the wall clock
        if pb.t0 is not None or not self.playing:
            now = pb.position() if self.playing else float("inf")
            frame = None
            while True:
                if self.play_pending is None:
                    try:
                        self.play_pending = pb.frames.get_nowait()
                    except queue.Empty:
                        break
                if self.play_pending[0] > now:
                    break
                frame, self.play_pending = self.play_pending, None  # late frames are skipped
                if not self.playing:
                    break
            if frame is not None:
                self.show_frame(frame[1], pb)
                if not self.playing:  # paused seek: one frame is all we wanted
                    pb.stop()
                    self.pb = None
                    return
            if self.playing:
                pos = min(pb.position(), self.play_info["duration"])
                self.update_time(pos)
                if pb.video_done and pb.frames.empty() and self.play_pending is None:
                    self.stop_player()  # reached the end
                    self.play_pos = self.play_info["duration"]
                    self.update_time(self.play_pos)
                    return
        elif pb.video_done and pb.frames.empty():
            self.pb = None  # nothing decoded (seek past the last frame)
            return
        # wake up when the next frame is due
        delay = 5
        if self.playing and pb.t0 is not None and self.play_pending is not None:
            delay = math.ceil((self.play_pending[0] - pb.position()) * 1000)
            delay = max(1, min(delay, 20))
        self._play_job = self.root.after(delay, self.player_tick)

    def show_frame(self, buf, pb):
        img = Image.frombuffer("RGB", (pb.w, pb.h), buf, "raw", "RGB", 0, 1)
        if self.play_photo is not None and (self.play_photo.width(), self.play_photo.height()) == (pb.w, pb.h):
            self.play_photo.paste(img)
        else:
            self.play_photo = ImageTk.PhotoImage(img)
            self.video.configure(image=self.play_photo)

    def update_time(self, pos):
        dur = self.play_info["duration"] if self.play_info else 0
        # only redraw when something changed
        text = f"{fmt_duration(pos)} / {fmt_duration(dur)}"
        if text != self.time_lbl.cget("text"):
            self.time_lbl.configure(text=text)
        if not self._dragging and abs(pos - self.seek.get()) >= max(0.05, dur / 2000):
            self.seek.set(pos)

    def toggle_play(self):
        if self.current != "Player" or self.play_info is None:
            return
        if self.playing:
            self.stop_player()
            self.update_time(self.play_pos)
        else:
            at_end = self.play_pos >= self.play_info["duration"] - 0.1
            self.start_playback(0.0 if at_end else self.play_pos, play=True)

    def seek_by(self, delta):
        if self.play_info is None:
            return
        pos = (self.pb.position() if self.pb and self.playing else self.play_pos) + delta
        self.start_playback(pos, play=self.playing)

    def on_seek_drag(self, value):
        self._dragging = True
        self.time_lbl.configure(text=f"{fmt_duration(value)} / {fmt_duration(self.play_info['duration'])}")

    def on_seek_release(self, _):
        if not self._dragging or self.play_info is None:
            return
        self._dragging = False
        self.start_playback(float(self.seek.get()), play=self.playing)

    def on_track(self, name):
        values = self.track_btn.cget("values")
        self.play_track = values.index(name) if name in values else 0
        if self.playing:
            self.start_playback(self.pb.position(), play=True)

    def on_volume(self, value):
        self.play_volume = float(value)
        if self.pb:
            self.pb.volume = self.play_volume  # read for every audio block: no restart
        self.vol_icon.configure(image=self.icon(G_MUTE if value <= 0.001 else G_VOLUME, 16, TEXT_2))

    def toggle_mute(self):
        if self.play_volume > 0.001:
            self._unmute_to = self.play_volume
            self.vol_slider.set(0)
            self.on_volume(0)
        else:
            v = getattr(self, "_unmute_to", 1.0)
            self.vol_slider.set(v)
            self.on_volume(v)

    def on_video_resize(self, _):
        if self._play_resize_job:
            self.root.after_cancel(self._play_resize_job)
        self._play_resize_job = self.root.after(250, self.resize_video)

    def resize_video(self):
        self._play_resize_job = None
        if self.current != "Player" or self.play_info is None:
            return
        w, h = self.video_size()
        cur = (self.play_photo.width(), self.play_photo.height()) if self.play_photo else (0, 0)
        if abs(w - cur[0]) > 8 or abs(h - cur[1]) > 8:
            pos = self.pb.position() if self.pb and self.playing else self.play_pos
            self.start_playback(pos, play=self.playing)

    # ================================================================ settings page
    def ensure_settings(self):
        if self._settings_built or self._quitting:
            return
        self.build_settings(self.pages["Settings"])
        if self._settings_colw is None:
            self.layout_settings()

    def build_settings(self, page, tab="General"):
        self.fields = {}
        page.grid_columnconfigure(0, minsize=176 + 24 + 32)
        page.grid_columnconfigure(1, weight=1)
        page.grid_rowconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=0)

        sub = ctk.CTkFrame(page, fg_color=BG, corner_radius=0)
        sub.grid(row=0, column=0, sticky="n", padx=(24, 32), pady=(24, 0))
        self.sub_btns, self.tabs, self.tab_cols = {}, {}, {}
        col_w = self._settings_colw or 680
        for name in ("General", "Video", "Audio"):
            self.sub_btns[name] = self.sub_item(sub, name)
            sf = self.scrollable(page)
            sf.configure(width=col_w + 8)
            col = ctk.CTkFrame(sf, fg_color=BG, corner_radius=0)
            col.pack(anchor="nw")
            col.strut = ctk.CTkFrame(col, width=col_w, height=0, fg_color=BG, corner_radius=0)
            col.strut.pack(anchor="w")
            self.tabs[name], self.tab_cols[name] = sf, col

        gen, vid, aud = (self.tab_cols[n] for n in ("General", "Video", "Audio"))

        g = self.section(gen, "Clipping", first=True)
        self.f_slider(g, "clip_seconds", "Clip length", 5, 300, unit="s",
                      desc="How much is kept before you press the hotkey")
        self.f_hotkey(g, "hotkey", "Save hotkey", desc="Works in games, even fullscreen")
        self.f_folder(g, "output_dir", "Save clips to")
        self.f_switch(g, "sort_by_game", "Sort clips into a folder per game")
        self.f_switch(g, "sound_on_save", "Play a sound when a clip is saved")
        self.end_group(g)
        g = self.section(gen, "App")
        self.f_switch(g, "start_with_windows", "Start with Windows", desc="Starts hidden in the tray")
        self.f_switch(g, "close_to_tray", "Close button hides to tray", desc="Keeps recording in the background")
        self.end_group(g)
        g = self.section(gen, "Updates")
        self.f_switch(g, "check_updates", "Check for updates", desc="Asks GitHub every few hours")
        if FROZEN:
            self.f_switch(g, "auto_update", "Install updates automatically",
                          desc="Installs when Clipper starts in the tray")
        r = self.row(g, NAME, desc=self.update_text)
        self.upd_desc = r.desc
        self.upd_btn = self.btn(r, "ghost", "Check now", width=128, height=32, border_color=LINE_HI,
                                command=self.update_button)
        self.upd_btn.pack(side="right", padx=(0, 4))
        self.paint_update()
        self.end_group(g)

        mons = list_monitors()
        labels = [f"{i}: {w}x{h}{' primary' if p else ''}" for i, (_, _, w, h, p) in enumerate(mons)] or ["0"]
        g = self.section(vid, "Capture", first=True)
        self.f_option(g, "monitor", "Monitor", labels,
                      shown=lambda v: labels[min(int(v), len(labels) - 1)],
                      parse=lambda s: int(s.split(":")[0]))
        self.f_option(g, "fps", "Frame rate", ["30", "60", "120", "144"], parse=int)
        self.f_named(g, "resolution", "Resolution",
                     [(k, "Native" if k == "native" else k) for k in RESOLUTIONS],
                     desc="Native + NVENC is lightest; frames stay on the GPU")
        self.f_switch(g, "show_cursor", "Show mouse cursor")
        self.end_group(g)
        g = self.section(vid, "Encoding")
        self.f_named(g, "encoder", "Encoder", [("auto", "Auto"), ("nvenc", "NVIDIA NVENC"),
                                                ("amf", "AMD AMF"), ("qsv", "Intel Quick Sync"),
                                                ("cpu", "CPU (x264)")])
        self.f_named(g, "codec", "Codec", [("h264", "H.264"), ("hevc", "HEVC")])
        self.f_named(g, "preset", "Preset", [("speed", "Speed"), ("balanced", "Balanced"),
                                              ("quality", "Quality")])
        self.f_slider(g, "quality", "Quality", 15, 40, desc="Lower = sharper and larger files")
        self.end_group(g)

        try:
            import soundcard as sc
            mics = ["Default"] + [m.name for m in sc.all_microphones()]
        except Exception:
            mics = ["Default"]
        g = self.section(aud, "Sources", first=True)
        desk = self.f_switch(g, "desktop_audio", "Desktop audio")
        desk_rows = [self.f_slider(g, "desktop_volume", "Desktop volume", 0, 200, unit="%")]
        mic = self.f_switch(g, "mic_audio", "Microphone")
        mic_rows = [self.f_option(g, "mic_device", "Mic device", mics, width=280,
                                  shown=lambda v: v or "Default", parse=lambda s: "" if s == "Default" else s),
                    self.f_slider(g, "mic_volume", "Mic volume", 0, 200, unit="%")]
        self.f_switch(g, "mic_noise_suppression", "Noise suppression",
                      desc="Removes fan, hiss and desk rumble from the mic")
        self.end_group(g)
        self.link_rows(desk, desk_rows)
        self.link_rows(mic, mic_rows)
        g = self.section(aud, "Output")
        self.f_switch(g, "separate_tracks", "Separate tracks", desc="Mix plus one track per source")
        self.f_slider(g, "audio_offset_ms", "Sync offset", -1000, 1000, unit="ms",
                      desc="Shift audio if it drifts from video")
        rates = ["96", "128", "160", "192", "256", "320"]
        self.f_option(g, "audio_bitrate", "Bitrate", [f"{v} kbps" for v in rates],
                      shown=lambda v: f"{v} kbps", parse=lambda s: int(s.split()[0]))
        self.end_group(g)
        for col in self.tab_cols.values():
            ctk.CTkFrame(col, height=32, width=0, fg_color=BG, corner_radius=0).pack()

        # footer
        foot = ctk.CTkFrame(page, fg_color=BG, corner_radius=0)
        foot.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.line(foot).pack(fill="x")
        band = ctk.CTkFrame(foot, fg_color=BG, corner_radius=0, height=64)
        band.pack(fill="x")
        band.pack_propagate(False)
        inner = ctk.CTkFrame(band, fg_color=BG, corner_radius=0)
        inner.pack(fill="both", expand=True, padx=24)
        self.foot_inner = inner
        self.label(inner, "Changes take effect when you save. Recording restarts.", "small", MUTED,
                   16).pack(side="left")
        self.btn(inner, "primary", "Save & apply", width=128, height=36,
                 command=self.apply).pack(side="right")
        self.btn(inner, "ghost", "Reset defaults", width=128, height=36, border_color=LINE_HI,
                 command=self.reset).pack(side="right", padx=(0, 8))
        self.settings_cur = None
        self.settings_tab(tab)
        self._settings_built = True  # set last, layout_settings checks it
        if self._settings_colw is not None:  # rebuilt by reset(): same widths as before
            colw, self._settings_colw = self._settings_colw, None
            self.layout_settings(colw)

    def sub_item(self, parent, name):
        """Settings tab button."""
        item = ctk.CTkFrame(parent, width=176, height=36, corner_radius=8, fg_color=BG)
        item.pack(pady=(0, 4))
        item.pack_propagate(False)
        item.text = self.label(item, name, "body", MUTED, 20)
        item.text.pack(side="left", fill="x", expand=True, padx=12)
        item.selected = False

        def paint(on):
            if not item.selected:
                item.configure(fg_color=SURFACE_2 if on else BG)

        self.hoverable(item, (item, item.text), paint, lambda e, n=name: self.settings_tab(n))
        return item

    def hoverable(self, frame, widgets, paint, click):
        """Hover + click for a frame made of several widgets."""
        frame.hover = False
        tk.Frame.configure(frame, cursor="hand2")

        def enter(_):
            if not frame.hover:
                frame.hover = True
                paint(True)

        def check():
            if not frame.hover or not frame.winfo_exists():
                return
            try:
                w = self.root.winfo_containing(*self.root.winfo_pointerxy())
            except Exception:
                w = None
            p = str(frame)
            if w is None or not (str(w) == p or str(w).startswith(p + ".")):
                frame.hover = False
                paint(False)

        for w in widgets:
            w.bind("<Button-1>", click, add="+")
            w.bind("<Enter>", enter, add="+")
            w.bind("<Leave>", lambda e: self.root.after_idle(check), add="+")

    def settings_tab(self, name):
        if name == self.settings_cur:
            return
        for n, b in self.sub_btns.items():
            on = n == name
            b.selected = on
            b.configure(fg_color=SURFACE_2 if on or b.hover else BG)
            b.text.configure(text_color=TEXT if on else MUTED)
        for n, sf in self.tabs.items():
            if n != name:
                sf.grid_remove()
        # sticky nsw keeps the scrollbar next to the content
        self.tabs[name].grid(row=0, column=1, sticky="nsw", padx=(0, 8), pady=(24, 0))
        self.settings_cur = name
        self.root.after_idle(lambda sf=self.tabs[name]: sf.winfo_exists() and self.sync_scrollbar(sf))

    def on_settings_page(self, e):
        self._settings_pw = e.width
        if not self.visible() or not self._settings_built:
            return
        if self._settings_job:
            self.root.after_cancel(self._settings_job)
            self._settings_job = None
        if self._settings_colw is None:
            self.layout_settings()  # first layout: no 250ms flash at the default width
        else:
            self._settings_job = self.root.after(250, self.layout_settings)

    def layout_settings(self, colw=None):
        """Settings column follows the page width, capped at SETTINGS_MAX."""
        self._settings_job = None
        page = self.pages["Settings"]
        s = self.scale(page)
        page_w = (self._settings_pw or page.winfo_width()) / s
        if colw is None:
            if page_w <= 1:
                return
            colw = max(480, min(SETTINGS_MAX, int(page_w) - 256))  # 232 subnav + 24 gutter
        if colw != self._settings_colw:
            self._settings_colw = colw
            for name, sf in self.tabs.items():
                sf.configure(width=colw + 8)
                self.tab_cols[name].strut.configure(width=colw)
        if page_w > 1:
            self.foot_inner.pack_configure(padx=self.pad(24, max(24, int(page_w) - 232 - colw)))
        sf = self.tabs.get(self.settings_cur)
        if sf is not None and sf.winfo_exists():
            self.root.after_idle(lambda: self.sync_scrollbar(sf))

    # ---- field builders
    def section(self, col, title, first=False):
        head = ctk.CTkFrame(col, fg_color=BG, corner_radius=0, height=36)
        head.pack(fill="x", pady=(0 if first else 24, 4))
        head.pack_propagate(False)
        self.label(head, title, "title", TEXT, 20).pack(side="left")
        group = ctk.CTkFrame(col, fg_color=SURFACE, corner_radius=12, border_width=1, border_color=LINE)
        group.pack(fill="x")
        self.exact_draw(group)
        group.rows = []
        return group

    def end_group(self, group):
        rows = group.rows
        if not rows:
            return
        b = max(1, self.px(1))  # the group's border, in physical px (pack_configure is not scaled)
        if len(rows) == 1:
            rows[0].pack_configure(pady=(b, b))
        else:
            rows[0].pack_configure(pady=(b, 0))
            rows[-1].pack_configure(pady=(0, b))

    def row(self, group, label, desc=None):
        """Fixed-height settings row."""
        if group.rows:
            self.line(group).pack(fill="x", padx=self.px(16))
        r = ctk.CTkFrame(group, fg_color="transparent", corner_radius=0, height=64 if desc else 56)
        r.pack(fill="x", padx=12)
        r.pack_propagate(False)
        group.rows.append(r)
        if desc:
            left = ctk.CTkFrame(r, fg_color="transparent", corner_radius=0)
            left.pack(side="left", padx=(4, 16))
            r.label = self.label(left, label, "body", TEXT, 20)
            r.label.pack(fill="x")
            r.desc = self.label(left, elide(desc, self.F["small"], 320), "small", MUTED, 16)
            r.desc.pack(fill="x")
        else:
            r.label = self.label(r, label, "body", TEXT, 20)
            r.label.pack(side="left", padx=(4, 16))
        return r

    def f_switch(self, parent, key, label, desc=None):
        """Toggle switch drawn from cached images."""
        var = tk.BooleanVar(value=self.cfg[key])
        r = self.row(parent, label, desc)
        sw = ctk.CTkLabel(r, text="", width=40, height=22, corner_radius=0, fg_color="transparent")
        sw.pack(side="right", padx=(0, 4))
        state = {"hover": False}

        def paint(*_):
            if sw.winfo_exists():
                sw.configure(image=self.switch_image(bool(var.get()), state["hover"]))

        def hover(on):
            state["hover"] = on
            paint()

        sw.bind("<Button-1>", lambda e: var.set(not var.get()))
        sw.bind("<Enter>", lambda e: hover(True))
        sw.bind("<Leave>", lambda e: hover(False))
        tk.Frame.configure(sw, cursor="hand2")
        var.trace_add("write", paint)
        paint()
        self.fields[key] = var.get
        return var

    def link_rows(self, var, rows):
        """Grey out rows that depend on a switch while it is off."""
        def paint(*_):
            on = bool(var.get())
            for r in rows:
                if not r.winfo_exists():
                    continue
                r.label.configure(text_color=TEXT if on else MUTED)
                c = r.control
                if isinstance(c, ctk.CTkSlider):
                    c.configure(state="normal" if on else "disabled",
                                progress_color=ACCENT if on else LINE_HI,
                                button_color=TEXT if on else MUTED,
                                button_hover_color="#ffffff" if on else MUTED)
                    r.value.configure(text_color=TEXT if on else MUTED)
                else:
                    c.configure(state="normal" if on else "disabled")
        var.trace_add("write", paint)
        paint()

    def switch_image(self, on, hover):
        key = ("switch", on, hover)
        if key not in self._icons:
            k = 8  # drawn at 8x, downsampled to 2x: smooth edges
            w, h, d, inset = 40 * k, 22 * k, 16 * k, 3 * k
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            dr = ImageDraw.Draw(img)
            track = (ACCENT_HI if hover else ACCENT) if on else (LINE_HI if hover else SURFACE_3)
            dr.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=track)
            x = w - inset - d if on else inset
            dr.ellipse((x, inset, x + d - 1, inset + d - 1), fill="#ffffff" if on or hover else TEXT)
            img = img.resize((80, 44), Image.LANCZOS)
            self._icons[key] = ctk.CTkImage(img, size=(40, 22))
        return self._icons[key]

    def f_option(self, parent, key, label, values, shown=str, parse=str, width=200, desc=None):
        var = tk.StringVar(value=shown(self.cfg[key]))
        r = self.row(parent, label, desc)
        r.control = ctk.CTkOptionMenu(r, values=values, variable=var, width=width, height=32,
                                      corner_radius=8, fg_color=SURFACE_2, button_color=SURFACE_2,
                                      button_hover_color=SURFACE_3, text_color=TEXT,
                                      text_color_disabled=MUTED, font=self.F["body"],
                                      dropdown_font=self.F["body"], dropdown_fg_color=SURFACE_2,
                                      dropdown_hover_color=SURFACE_3, dropdown_text_color=TEXT,
                                      dynamic_resizing=False, anchor="w")
        r.control.pack(side="right", padx=(0, 4))
        r.value = None
        self.fields[key] = lambda: parse(var.get())
        return r

    def f_named(self, parent, key, label, pairs, desc=None):
        """Option menu showing friendly names; the stored values stay the internal ones."""
        to_name = dict(pairs)
        to_value = {n: v for v, n in pairs}
        self.f_option(parent, key, label, [n for _, n in pairs], desc=desc,
                      shown=lambda v: to_name.get(v, str(v)), parse=lambda n: to_value.get(n, n))

    def f_slider(self, parent, key, label, lo, hi, unit="", desc=None):
        var = tk.IntVar(value=self.cfg[key])
        r = self.row(parent, label, desc)

        def fmt(v):
            return f"{v}%" if unit == "%" else (f"{v} {unit}" if unit else str(v))

        # value label as wide as the widest value, so the sliders line up
        slot = -(-max(self.measure(fmt(v), "small_sb") for v in (lo, hi)) // 4) * 4
        slot = max(24, min(56, slot))
        val = self.label(r, fmt(var.get()), "small_sb", TEXT, 20, anchor="e", width=slot)
        val.pack(side="right", padx=(0, 4))

        def changed(v):
            var.set(int(round(float(v))))
            val.configure(text=fmt(var.get()))

        sl = ctk.CTkSlider(r, from_=lo, to=hi, number_of_steps=hi - lo, variable=var, width=256 - slot,
                           height=16, fg_color=SURFACE_3, progress_color=ACCENT, button_color=TEXT,
                           button_hover_color="#ffffff", button_corner_radius=8, button_length=0,
                           command=changed)
        sl.pack(side="right", padx=(0, 12))
        r.control, r.value = sl, val
        self.fields[key] = var.get
        return r

    def f_hotkey(self, parent, key="hotkey", label="Save hotkey", desc=None):
        var = tk.StringVar(value=self.cfg[key])
        r = self.row(parent, label, desc)
        btn = self.btn(r, "secondary", "Change", width=96)
        btn.pack(side="right", padx=(0, 4))
        holder = ctk.CTkFrame(r, fg_color="transparent", corner_radius=0, height=28)
        holder.pack(side="right", padx=(0, 12))
        state = {"prev": var.get()}

        def show_keys(*_):
            for w in holder.winfo_children():
                w.destroy()
            if self._capturing:
                self.label(holder, "Press a key combination\u2026", "body", ACCENT, 20).pack()
            else:
                self.keycaps(holder, var.get(), bg=SURFACE).pack()

        def finish(value=None):
            self.root.unbind("<KeyPress>")
            self._capturing = False
            self._cancel_capture = None
            self.bind_f5()
            if btn.winfo_exists():
                btn.configure(text="Change", fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT)
                var.set(value or state["prev"])  # trace redraws the keycaps

        def on_key(e):
            if e.keysym == "Escape":
                finish()
                return
            if e.keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R"):
                return
            parts = [m for bit, m in ((0x4, "ctrl"), (0x20000, "alt"), (0x1, "shift")) if e.state & bit]
            finish("+".join(parts + [e.keysym]))

        def capture():
            if self._capturing:
                finish()
                return
            state["prev"] = var.get()
            self._capturing = True
            self._cancel_capture = finish
            self.root.unbind("<F5>")
            btn.configure(text="Cancel", fg_color=ACCENT_DIM, hover_color=SURFACE_3, text_color=TEXT)
            show_keys()
            self.root.bind("<KeyPress>", on_key)

        btn.configure(command=capture)
        var.trace_add("write", show_keys)
        show_keys()
        self.fields[key] = var.get

    def f_folder(self, parent, key="output_dir", label="Save clips to", desc=None):
        var = tk.StringVar(value=self.cfg[key])
        r = self.row(parent, label, desc)

        def pick():
            d = filedialog.askdirectory(initialdir=var.get())
            if d:
                var.set(d)

        self.btn(r, "secondary", "Browse", width=96, command=pick).pack(side="right", padx=(0, 4))
        entry = ctk.CTkEntry(r, width=320, height=32, corner_radius=8, fg_color=INPUT_BG,
                             border_width=1, border_color=LINE_HI, text_color=TEXT, font=self.F["body"])
        entry.pack(side="right", padx=(0, 8))
        inner = entry._entry
        state = {"editing": False}

        def put(text):
            inner.delete(0, "end")
            inner.insert(0, text)

        def show_short(*_):  # show the drive and last folders, elide the middle
            if not state["editing"]:
                put(mid_elide(var.get(), self.F["body"], 320 - 24))
                inner.xview_moveto(0)

        def focus_in(_):
            state["editing"] = True
            put(var.get())
            inner.icursor("end")
            inner.xview_moveto(1.0)

        def focus_out(_):
            if state["editing"]:
                state["editing"] = False
                var.set(inner.get().strip() or var.get())  # trace re-elides
                show_short()

        def get():
            if state["editing"]:
                var.set(inner.get().strip() or var.get())
            return var.get()

        inner.bind("<FocusIn>", focus_in, add="+")
        inner.bind("<FocusOut>", focus_out, add="+")
        inner.bind("<Return>", lambda e: self.root.focus_set(), add="+")
        var.trace_add("write", show_short)
        show_short()
        self.fields[key] = get

    # ---- settings actions
    def read_fields(self):
        new = dict(self.cfg)
        for key, get in self.fields.items():
            new[key] = get()
        parse_hotkey(new["hotkey"])
        return new

    def apply(self):
        self.cancel_capture()
        try:
            new = self.read_fields()
        except Exception as e:
            messagebox.showerror(NAME, f"Invalid setting: {e}")
            return
        self.cfg.clear()
        self.cfg.update(new)
        save_config(self.cfg)
        self.set_startup(self.cfg["start_with_windows"])
        self.update_labels()
        self.show_page("Clips")
        self.start_buffer()

    def reset(self):
        self.cancel_capture()
        if not messagebox.askyesno(NAME, "Reset all settings to defaults?"):
            return
        self.cfg.clear()
        self.cfg.update(DEFAULTS)
        tab = self.settings_cur or "General"
        for w in self.pages["Settings"].winfo_children():
            w.destroy()
        self.build_settings(self.pages["Settings"], tab)
        self.apply()

    def set_startup(self, enabled):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                pyw = Path(sys.executable).with_name("pythonw.exe")
                cmd = (f'"{sys.executable}" --tray' if FROZEN
                       else f'"{pyw}" "{Path(__file__).resolve()}" --tray')
                winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, APP)
                except FileNotFoundError:
                    pass

    def open_folder(self):
        p = Path(self.cfg["output_dir"])
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(p)

    def cancel_capture(self):
        """End a hotkey capture left open (page switch, save, reset, hide): restores the old value."""
        if self._cancel_capture is not None:
            self._cancel_capture()

    def bind_f5(self):
        self.root.bind("<F5>", lambda e: self.refresh_clips(force=True))

    # ================================================================ status
    def update_labels(self):
        c = self.cfg
        key = (c["hotkey"], c["clip_seconds"])
        if key == getattr(self, "_labels_key", None):
            return  # nothing changed
        self._labels_key = key
        self.hint_lbl.configure(text=f"Save last {c['clip_seconds']} s")
        self.hint_keys.destroy()
        self.hint_keys = self.keycaps(self.hint_host, c["hotkey"])
        self.hint_keys.pack()
        if self.empty_keys_host is not None and self.empty_keys_host.winfo_exists():
            for w in self.empty_keys_host.winfo_children():
                w.destroy()
            self.keycaps(self.empty_keys_host, c["hotkey"]).pack()
            self.empty_hint.configure(text=f"in a game to save the last {c['clip_seconds']} seconds.")

    def set_status(self, title, color, detail=None):
        self.status_title.configure(text=elide(title, self.F["body_sb"], 280))
        if detail is not None:
            d = " \u00b7 ".join(l for l in detail.splitlines() if l)
            # title + detail fit the slot (STATUS_SLOT - 2 border - 12 - 8 dot - 8 - 16)
            room = STATUS_SLOT - 46 - self.F["body_sb"].measure(self.status_title.cget("text"))
            self._detail_text = elide(d, self.F["small"], max(96, min(280, room)))
            self.status_detail.configure(text=self._detail_text)
            self._detail_empty = d == ""
        self.dot.configure(fg_color=color)
        self.pulse_on = True
        self.pulse = color == ACCENT
        link = title == "Capture stopped"
        if link != self._log_link:
            self._log_link = link
            for w in self.pill_widgets:
                try:
                    tk.Frame.configure(w, cursor="hand2" if link else "")
                except Exception:
                    pass
        self.sync_detail()
        if self.tray:
            self.tray.title = f"{NAME} - {title}"[:127]
            self.tray.update_menu()

    def on_pill_click(self, _):
        if self._log_link:
            try:
                os.startfile(LOG_PATH)
            except Exception:
                pass

    def set_toggle(self, running):
        self.toggle_btn.configure(text="Pause" if running else "Resume",
                                  image=self.icon(G_PAUSE if running else G_PLAY, 16, TEXT_2))

    def set_buffering(self):
        c = self.cfg
        res = "Native" if c["resolution"] == "native" else c["resolution"]
        enc = (self.rec.vendor or c["encoder"]).upper()
        audio = {(True, True): "Desktop + mic", (True, False): "Desktop",
                 (False, True): "Mic", (False, False): "No audio"}[(bool(c["desktop_audio"]), bool(c["mic_audio"]))]
        self.set_status("Recording", ACCENT, f"{res} \u00b7 {c['fps']} fps \u00b7 {enc} \u00b7 {audio}")
        self.set_toggle(True)
        self.update_labels()

    def animate(self):
        # slow pulse on the dot; skipped entirely while hidden in tray
        if self.pulse and self.visible():
            self.pulse_on = not self.pulse_on
            self.dot.configure(fg_color=ACCENT if self.pulse_on else ACCENT_DIM)
        self.root.after(700, self.animate)

    # ================================================================ buffer actions
    def start_buffer(self, quiet=False, hotkey=True):
        """quiet: on failure retry in the background instead of showing an error dialog."""
        self.paused = False
        self.starting = True
        self.retry_at = 0
        self.set_status("Starting...", ORANGE, "")

        def work():
            try:
                self.rec.start()
                errs = [f"{k}: {a.error}" for k, a in self.rec.audio.items() if a.error]
                self.events.put(("started", errs))
            except Exception as e:
                self.events.put(("retry" if quiet else "error", str(e)))

        threading.Thread(target=work, daemon=True).start()
        if hotkey:
            self.register_hotkey()

    def schedule_restart(self, detail):
        self.retry_at = time.perf_counter() + self.retry_delay
        self.set_status("Restarting capture...", ORANGE, detail)
        self.retry_delay = min(self.retry_delay * 2, 30)

    def register_hotkey(self):
        if self.hotkey:
            self.hotkey.stop()
        self.hotkey = HotkeyThread(self.cfg["hotkey"], self.trigger_save)
        self.hotkey.start()
        self.hotkey.ready.wait(2)
        if not self.hotkey.ok:
            self.alert("showwarning", f"Hotkey '{self.cfg['hotkey']}' is taken or invalid. Pick another.")

    def toggle(self):
        if self.rec.running():
            self.paused = True
            self.retry_at = 0
            self.rec.stop()
            self.set_status("Paused", GRAY, "Nothing is being recorded")
            self.set_toggle(False)
        else:
            self.start_buffer()

    def trigger_save(self):
        def work():
            try:
                self.events.put(("saved", self.rec.save_clip()))
            except Exception as e:
                self.events.put(("error", f"Save failed: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def visible(self):
        return self.root.state() in ("normal", "zoomed")

    def saved_detail(self, path):
        """Pill detail, e.g. 'Counter-Strike 2' and the time."""
        path = Path(path)
        root = Path(self.cfg["output_dir"])
        game = path.parent.name if path.parent != root else f"Last {self.cfg['clip_seconds']} s"
        return f"{game}\n{datetime.now():%H:%M}"

    def pump(self):
        # always reschedule, even if a handler throws
        try:
            self.pump_once()
        except Exception:
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write("UI pump error:\n" + traceback.format_exc())
            except Exception:
                pass
        finally:
            if not self._quitting:
                self.root.after(250, self.pump)

    def pump_once(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "started":
                    self.starting = False
                    self.retry_delay = 2
                    self.set_buffering()
                    if data:
                        self.alert("showwarning", "Audio problem:\n" + "\n".join(data))
                elif kind == "saved":
                    self.set_status("Clip saved", GREEN, self.saved_detail(data))
                    if self.cfg["sound_on_save"]:
                        import winsound
                        winsound.MessageBeep(winsound.MB_OK)
                    # rebuild the card in case auto-refresh caught the half-written file
                    for k in [k for k in self.cards if Path(k) == Path(data)]:
                        self.cards.pop(k)["frame"].destroy()
                        self.images.pop(k, None)
                        self.order = [o for o in self.order if o != k]
                        self._dirty = True
                    self.sig = None
                    if self.visible():
                        self.refresh_clips(force=True)
                    self.root.after(3000, lambda: self.rec.running() and self.set_buffering())
                elif kind == "thumb":
                    if data in self.cards and self.visible():
                        self.apply_thumb(self.cards[data], Path(data))
                elif kind == "status":
                    self.set_status(data, ORANGE)
                elif kind == "retry":
                    self.starting = False
                    if not self.paused:
                        self.schedule_restart((data.splitlines() or [""])[0][:120])
                elif kind == "error":
                    if not data.startswith("Save failed"):
                        self.starting = False
                    self.set_status("Error", GRAY, (data.splitlines() or [""])[0][:120])
                    self.alert("showerror", data)
                elif kind == "update":
                    self.on_update(data)
                elif kind == "install_update":
                    self.install_update()
                    return
                elif kind == "show":
                    self.show_window()
                elif kind == "toggle":
                    self.toggle()
                elif kind == "quit":
                    self.quit()
                    return
        except queue.Empty:
            pass
        if not self.starting and self.rec.proc is not None and self.rec.proc.poll() is not None:
            self.rec.stop()  # also stops the audio threads
            if self.paused:
                self.set_status("Capture stopped", GRAY, "Click to open the log")
                self.set_toggle(False)
            else:
                self.schedule_restart("Capture was interrupted")
        if self.retry_at and not self.starting and not self.paused and time.perf_counter() >= self.retry_at:
            self.start_buffer(quiet=True, hotkey=False)
        if SHOW_FLAG.exists():  # second launch asked us to come to front
            SHOW_FLAG.unlink(missing_ok=True)
            self.show_window()
        self.ticks += 1
        if self.ticks % 12 == 0 and self.visible() and self.current == "Clips":
            self.refresh_clips()  # pick up clips added outside the app

    # ================================================================ updates
    def auto_check(self):
        if self._quitting:
            return
        if self.cfg["check_updates"]:
            self.check_updates()
        self.root.after(CHECK_EVERY * 1000, self.auto_check)

    def check_updates(self, manual=False):
        if self._checking:
            return
        self._checking = True
        first, self._checked_once = not self._checked_once, True
        if manual:
            self.update_text = "Checking..."
            self.paint_update()

        def work():
            d = {"state": "none", "manual": manual, "first": first}
            try:
                for f in UPDATE_DIR.glob("BorddersClipper-Setup-*"):  # old or half-done downloads
                    if f.suffix != ".exe" or parse_version(f.stem) <= parse_version(VERSION):
                        f.unlink(missing_ok=True)
                rel = latest_release()
                if rel and parse_version(rel[0]) > parse_version(VERSION):
                    version, asset, url = rel
                    d.update(state="available", version=version, url=url, setup=None)
                    if FROZEN and asset:
                        done = UPDATE_DIR / f"BorddersClipper-Setup-{version}.exe"
                        d["setup"] = done if done.exists() else download_update(version, asset)
            except Exception as e:
                d.update(state="error", error=str(e))
            self.events.put(("update", d))

        threading.Thread(target=work, daemon=True).start()

    def on_update(self, d):
        self._checking = False
        if d["state"] == "error":
            self.update_text = "Couldn't reach GitHub" if d["manual"] else self.update_text
        elif d["state"] == "none":
            self.update_info = None
            self.update_text = f"Version {VERSION}, up to date"
        else:
            self.update_info = d
            v = d["version"]
            self.update_text = f"Version {v} is ready to install" if d["setup"] else f"Version {v} is out"
            # started in the tray (boot, or after an update): safe to swap now, the buffer is fresh
            if (d["setup"] and d["first"] and self.cfg["auto_update"] and not d["manual"]
                    and not self.visible()):
                self.install_update()
                return
            if self.tray and self._notified != v and not d["manual"]:
                self._notified = v
                try:
                    self.tray.notify(f"{NAME} {v} is available. Install it from the tray menu "
                                     "or Settings.", NAME)
                except Exception:
                    pass
        self.paint_update()
        if self.tray:
            self.tray.update_menu()

    def paint_update(self):
        if not getattr(self, "upd_desc", None) or not self.upd_desc.winfo_exists():
            return
        self.upd_desc.configure(text=self.update_text)
        if self.update_info:
            self.upd_btn.configure(text="Install update" if self.update_info["setup"] else "Download")
        else:
            self.upd_btn.configure(text="Check now")

    def update_button(self):
        if self.update_info:
            self.install_update()
        else:
            self.check_updates(manual=True)

    def install_update(self):
        info = self.update_info
        if not info:
            return
        if not info["setup"]:  # running from source, or no installer on the release
            import webbrowser
            webbrowser.open(info["url"])
            return
        try:
            run_installer(info["setup"], [] if self.visible() else ["--tray"])
        except Exception as e:
            self.alert("showerror", f"Couldn't start the update:\n{e}")
            return
        self.quit()

    # ================================================================ tray
    def start_tray(self):
        if pystray is None:
            return
        img = Image.open(ICON_PATH) if ICON_PATH.exists() else Image.new("RGBA", (64, 64), ACCENT)
        put = self.events.put
        menu = pystray.Menu(
            pystray.MenuItem(f"Open {NAME}", lambda: put(("show", None)), default=True),
            pystray.MenuItem("Save clip now", lambda: self.trigger_save()),
            pystray.MenuItem(lambda item: "Pause recording" if self.rec.running() else "Resume recording",
                             lambda: put(("toggle", None))),
            pystray.MenuItem(lambda item: f"Install update {self.update_info['version']}"
                             if self.update_info else "Install update",
                             lambda: put(("install_update", None)),
                             visible=lambda item: self.update_info is not None),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: put(("quit", None))),
        )
        self.tray = pystray.Icon(APP, img, NAME, menu)
        self.tray.run_detached()

    def alert(self, kind, text):
        # show the window first, a dialog on a hidden window blocks everything
        def go():
            self.show_window()
            getattr(messagebox, kind)(NAME, text, parent=self.root)
        self.root.after(0, go)

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if self.current == "Clips":
            self.refresh_clips()
            self.catch_up_thumbs()
            self.schedule_reflow_idle()
        if not self._settings_built:
            self.root.after(600, self.ensure_settings)

    def on_map(self, e):
        # restored from the taskbar (not the tray): thumbs that finished while minimised
        if e.widget is self.root and self.current == "Clips" and self.cards:
            self.catch_up_thumbs()
            self.schedule_reflow_idle()

    def hide_to_tray(self):
        self.stop_player()
        self.hide_menu()
        self.cancel_capture()
        self.root.withdraw()

    def on_close(self):
        if self.tray and self.cfg["close_to_tray"]:
            self.hide_to_tray()
        else:
            self.quit()

    def quit(self):
        self._quitting = True
        self.stop_player()
        if self.hotkey:
            self.hotkey.stop()
        self.rec.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    hidden = "--tray" in sys.argv
    # own taskbar identity, so Windows doesn't group/show it as Python
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _mutex = k32.CreateMutexW(None, False, "Local\\ClipperSingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        if not hidden:
            SHOW_FLAG.touch()
        sys.exit(0)
    if not FROZEN:  # the installer makes the shortcut for the .exe
        try:
            ensure_shortcut()
        except Exception:
            pass
    App(hidden=hidden).run()
