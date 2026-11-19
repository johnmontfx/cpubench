#!/usr/bin/env python3
"""
mac_stress_test.py -- how does this Mac hold up under a long video encode?

The script records CPU load, chip temperature and (with sudo) GPU load, clock
speed and power. After a short idle baseline it starts a software H.264 encode
with ffmpeg and keeps it running for the test length. Then it stops recording,
stops ffmpeg and saves graphs plus a summary to a new folder on your Desktop.

  0:00         recording starts (idle baseline)
  0:30         ffmpeg encode starts            (--warmup, default 30 s)
  0:30 + 30m   recording stops, ffmpeg stops   (--duration, default 1800 s)
               graphs, CSV and summary are saved

Press Ctrl-C at any time to end early. You still get graphs for what was
recorded.

REQUIREMENTS
  * A Mac. Apple Silicon gives every graph. On an Intel Mac you get CPU load
    and encode speed only.
  * Python 3 with psutil and matplotlib
  * ffmpeg with libx264 (Homebrew's ffmpeg has it)

SETUP (once)
  1. Install Homebrew from https://brew.sh if you don't have it, then:
         brew install python ffmpeg
  2. Make a Python environment with the two packages:
         python3 -m venv ~/stresstest-venv
         ~/stresstest-venv/bin/pip install psutil matplotlib
  3. Check that everything is in place:
         ~/stresstest-venv/bin/python mac_stress_test.py --check

RUNNING
  Plug the Mac into power, quit other heavy apps and run, from the folder
  holding this file:
         sudo ~/stresstest-venv/bin/python mac_stress_test.py
  sudo asks for your password. It's needed for GPU %, clock speed and power,
  which come from macOS's powermetrics tool. Without sudo you still get CPU
  load, temperature and encode speed.

  Options:
    --duration N   seconds of encoding (default 1800 = 30 minutes)
    --warmup N     idle seconds recorded before the encode starts (default 30)
    --encodes N    identical encodes run side by side (default 3). One 1080p
                   encode can't fill a chip with many cores; 3 fill an M5 Max.
                   Use --encodes 1 for a single-encode test.
    --out DIR      where to save results (default: your Desktop). Each run
                   makes its own dated folder inside DIR. DIR is created if
                   it doesn't exist.
    --check        check the requirements and exit

  For example, a one-hour test saved to ~/StressTests:
         sudo ~/stresstest-venv/bin/python mac_stress_test.py --duration 3600 --out ~/StressTests

RESULTS
  A new folder in the --out folder (your Desktop unless you choose another),
  e.g. ~/Desktop/mac_stress_test_2026-11-19_1430/:
    chart.png        every measurement
    chart_cores.png  the same, with the utilization panel showing only core types
    data.csv         one row per 2-second sample
    summary.txt      Mac model, chip, macOS and ffmpeg versions, settings, results

  The headline number is the average encode speed (fps). Higher is faster.
  Compare it only between runs with the same --duration and --encodes. If the
  fps in the last 5 minutes is well below the first 5, the Mac slowed down as
  it got hot (thermal throttling). The temperature and clock graphs show it.

  The encode is a synthetic 1920x1080 30 fps test pattern made by ffmpeg. It's
  encoded with libx264's medium preset at 6 Mbps and thrown away. Nothing is
  written to disk except the results.
"""

import argparse
import csv
import ctypes
import ctypes.util
import datetime
import os
import platform
import pwd
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

try:
    import psutil
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError as e:
    sys.exit(f"[!] Missing Python package: {e.name}\n"
             "    Follow SETUP in:  python3 mac_stress_test.py --help\n"
             "    (in short: python3 -m venv ~/stresstest-venv && "
             "~/stresstest-venv/bin/pip install psutil matplotlib)")

INTERVAL = 2           # seconds between samples
CPU_SMOOTH_WINDOW = 5  # graph smoothing for CPU % and fps lines, in samples (1 = off)
SHUTDOWN_TIMEOUT = 10  # seconds to let ffmpeg exit cleanly before killing it
COMPARE_WINDOW = 300   # "first 5 min vs last 5 min" encode speed, in seconds

FFMPEG_ARGS = [
    "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
    "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
    "-map", "0:v:0", "-an",
    "-c:v", "libx264", "-preset", "medium",
    "-pix_fmt", "yuv420p", "-profile:v", "high",
    "-b:v", "6000k", "-maxrate", "6600k", "-bufsize", "9000k",
    "-f", "null", "-",
]
ENCODE_DESC = "libx264 medium, 1920x1080 30 fps test pattern, 6 Mbps"

NAN = float("nan")


def run_cmd(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()
    except OSError:
        return ""


def sysctl(key):
    return run_cmd(["sysctl", "-n", key])


# ---------------------------------------------------------------------------
# Temperature via IOKit HID (Apple Silicon). powermetrics has no temperatures
# on Apple Silicon (only a "pressure level"); the SoC die sensors do.
# ---------------------------------------------------------------------------
class SocTemperature:
    _kIOHIDEventTypeTemperature = 15
    _kCFStringEncodingUTF8 = 0x08000100
    _kCFNumberSInt32Type = 3

    def __init__(self):
        self.sensors = []
        try:
            self._setup()
        except (OSError, AttributeError, ValueError):
            self.sensors = []

    def _setup(self):
        vp = ctypes.c_void_p
        self.iokit = iokit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
        self.cf = cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))

        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFNumberCreate.restype = vp
        cf.CFNumberCreate.argtypes = [vp, ctypes.c_int, vp]
        cf.CFDictionaryCreate.restype = vp
        cf.CFDictionaryCreate.argtypes = [vp, vp, vp, ctypes.c_long, vp, vp]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [vp]
        cf.CFArrayGetValueAtIndex.restype = vp
        cf.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.argtypes = [vp]

        iokit.IOHIDEventSystemClientCreate.restype = vp
        iokit.IOHIDEventSystemClientCreate.argtypes = [vp]
        iokit.IOHIDEventSystemClientSetMatching.argtypes = [vp, vp]
        iokit.IOHIDEventSystemClientCopyServices.restype = vp
        iokit.IOHIDEventSystemClientCopyServices.argtypes = [vp]
        iokit.IOHIDServiceClientCopyProperty.restype = vp
        iokit.IOHIDServiceClientCopyProperty.argtypes = [vp, vp]
        iokit.IOHIDServiceClientCopyEvent.restype = vp
        iokit.IOHIDServiceClientCopyEvent.argtypes = [vp, ctypes.c_int64, ctypes.c_int32, ctypes.c_int64]
        iokit.IOHIDEventGetFloatValue.restype = ctypes.c_double
        iokit.IOHIDEventGetFloatValue.argtypes = [vp, ctypes.c_int32]

        # Apple's vendor temperature sensors: PrimaryUsagePage 0xff00, PrimaryUsage 5
        keys = (vp * 2)(self._cfstr("PrimaryUsagePage"), self._cfstr("PrimaryUsage"))
        vals = (vp * 2)(self._cfnum(0xFF00), self._cfnum(5))
        key_cb = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        val_cb = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        matching = cf.CFDictionaryCreate(None, keys, vals, 2, key_cb, val_cb)

        self.client = iokit.IOHIDEventSystemClientCreate(None)
        iokit.IOHIDEventSystemClientSetMatching(self.client, matching)
        self._product_key = self._cfstr("Product")

        services = iokit.IOHIDEventSystemClientCopyServices(self.client)
        if services:
            for i in range(cf.CFArrayGetCount(services)):
                svc = cf.CFArrayGetValueAtIndex(services, i)
                if self._name(svc).startswith("PMU tdie"):  # SoC die sensors only
                    self.sensors.append(svc)

    def _cfstr(self, s):
        return self.cf.CFStringCreateWithCString(None, s.encode(), self._kCFStringEncodingUTF8)

    def _cfnum(self, i):
        v = ctypes.c_int32(i)
        return self.cf.CFNumberCreate(None, self._kCFNumberSInt32Type, ctypes.byref(v))

    def _name(self, svc):
        ref = self.iokit.IOHIDServiceClientCopyProperty(svc, self._product_key)
        if not ref:
            return ""
        buf = ctypes.create_string_buffer(256)
        ok = self.cf.CFStringGetCString(ref, buf, 256, self._kCFStringEncodingUTF8)
        self.cf.CFRelease(ref)
        return buf.value.decode() if ok else ""

    def read(self):
        """Return (hottest, average) die temperature in °C, or (None, None)."""
        t = self._kIOHIDEventTypeTemperature
        temps = []
        for svc in self.sensors:
            ev = self.iokit.IOHIDServiceClientCopyEvent(svc, t, 0, 0)
            if ev:
                val = self.iokit.IOHIDEventGetFloatValue(ev, t << 16)
                self.cf.CFRelease(ev)
                if 0 < val < 150:  # drop unpopulated/bogus sensors
                    temps.append(val)
        if not temps:
            return None, None
        return max(temps), sum(temps) / len(temps)


# ---------------------------------------------------------------------------
# GPU %, clock speeds and power via powermetrics (needs root)
# ---------------------------------------------------------------------------
running = True
pm = {"gpu": None, "freq": {}, "cpu_w": None, "gpu_w": None, "ane_w": None}

_RE_CLUSTER_FREQ = re.compile(r"^(\S+)-Cluster HW active frequency:\s*([\d.]+) MHz")
_RE_CLUSTER_RES = re.compile(r"^(\S+)-Cluster HW active residency:\s*([\d.]+)%")
_RE_CPU_LINE = re.compile(r"^CPU (\d+) frequency:")
_RE_CPU_POWER = re.compile(r"^CPU Power:\s*([\d.]+) mW")
_RE_GPU_POWER = re.compile(r"^GPU Power:\s*([\d.]+) mW")
_RE_ANE_POWER = re.compile(r"^ANE Power:\s*([\d.]+) mW")
_RE_GPU_RES = re.compile(r"^GPU (?:HW )?active residency:\s*([\d.]+)%", re.I)


def parse_powermetrics_line(line, state, cpu_type):
    """
    A core type can span several clusters (e.g. Performance = P0 + P1), so its
    clock is the average of its clusters' active frequency, weighted by how
    much of the time each cluster was running.
    """
    line = line.strip()
    clusters = state.setdefault("clusters", {})

    m = _RE_CLUSTER_FREQ.match(line)
    if m:
        state["cur"] = m.group(1)
        clusters[m.group(1)] = {"freq": float(m.group(2)), "res": 0.0, "type": None}
        return
    m = _RE_CLUSTER_RES.match(line)
    if m and m.group(1) in clusters:
        clusters[m.group(1)]["res"] = float(m.group(2))
        return
    m = _RE_CPU_LINE.match(line)
    if m:
        c = clusters.get(state.get("cur"))
        if c is not None and c["type"] is None:
            c["type"] = cpu_type.get(int(m.group(1)))
        return
    m = _RE_CPU_POWER.match(line)
    if m:
        pm["cpu_w"] = float(m.group(1)) / 1000
        # Last line of the processor section: finish this sample's clusters
        by_type = {}
        for c in clusters.values():
            if c["type"]:
                by_type.setdefault(c["type"], []).append(c)
        freq = {}
        for t, cs in by_type.items():
            busy = sum(c["res"] for c in cs)
            freq[t] = sum(c["freq"] * c["res"] for c in cs) / busy if busy > 0 else 0.0
        if freq:
            pm["freq"] = freq
        state["clusters"] = {}
        state["cur"] = None
        return
    m = _RE_GPU_POWER.match(line)
    if m:
        pm["gpu_w"] = float(m.group(1)) / 1000
        return
    m = _RE_ANE_POWER.match(line)
    if m:
        pm["ane_w"] = float(m.group(1)) / 1000
        return
    m = _RE_GPU_RES.match(line)
    if m:
        pm["gpu"] = float(m.group(1))


def powermetrics_reader(cpu_type):
    cmd = ["powermetrics", "--samplers", "cpu_power,gpu_power", "-i", str(INTERVAL * 1000)]
    try:
        # Own session, so Ctrl-C in Terminal doesn't kill it before we finish
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    except OSError as e:
        print(f"[-] Couldn't start powermetrics: {e}")
        return
    state = {}
    while running and proc.poll() is None:
        line = proc.stdout.readline()
        if not line:
            break
        parse_powermetrics_line(line, state, cpu_type)
    proc.terminate()


def core_groups():
    """
    [(name, [cpu indices])], fastest core type first, e.g.
    [("Super", [12..17]), ("Performance", [0..11])] on an M5 Max, or
    [("Performance", [4..11]), ("Efficiency", [0..3])] on an M1.
    macOS lists core types fastest first but numbers CPUs slowest first.
    Empty on Intel Macs (one core type).
    """
    try:
        levels = []
        for i in range(int(sysctl("hw.nperflevels"))):
            levels.append((sysctl(f"hw.perflevel{i}.name"), int(sysctl(f"hw.perflevel{i}.logicalcpu"))))
    except ValueError:
        return []
    if len(levels) < 2 or sum(n for _, n in levels) != psutil.cpu_count():
        return []
    groups, idx = [], 0
    for name, count in reversed(levels):
        groups.append((name, list(range(idx, idx + count))))
        idx += count
    return list(reversed(groups))


# ---------------------------------------------------------------------------
# ffmpeg load
# ---------------------------------------------------------------------------
def find_ffmpeg():
    """PATH first, then the usual install spots (sudo may trim PATH)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"):
        p = os.path.join(d, "ffmpeg")
        if os.access(p, os.X_OK):
            return p
    return None


class Encoders:
    """N identical ffmpeg encodes; tracks the frame count of each."""

    def __init__(self, ffmpeg, count):
        self.cmd = [ffmpeg] + FFMPEG_ARGS
        self.count = count
        self.procs, self.readers = [], []
        self.frames = [0] * count

    def _read(self, stream, index):
        for line in stream:
            if line.startswith("frame="):
                try:
                    self.frames[index] = int(line.split("=", 1)[1])
                except ValueError:
                    pass

    def start(self):
        for i in range(self.count):
            # Own session: Ctrl-C goes to this script, which then stops ffmpeg cleanly
            p = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                 text=True, start_new_session=True)
            self.procs.append(p)
            r = threading.Thread(target=self._read, args=(p.stdout, i), daemon=True)
            r.start()
            self.readers.append(r)

    def total(self):
        return sum(self.frames)

    def all_exited(self):
        return all(p.poll() is not None for p in self.procs)

    def stop(self):
        for p in self.procs:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        for p in self.procs:
            try:
                p.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for r in self.readers:
            r.join(timeout=2)


# ---------------------------------------------------------------------------
# Graph (dark theme)
# ---------------------------------------------------------------------------
THEME = {
    "page": "#0d0d0d", "surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7",
    "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835",
}
COLOR_ALL = "#3987e5"                             # all CPU cores / CPU power
COLOR_GROUPS = ["#d95926", "#199e70", "#9085e9"]  # core types, fastest first
COLOR_GPU = "#c98500"
COLOR_ANE = "#d55181"
COLOR_TEMP = "#e66767"
COLOR_FPS = "#3987e5"

plt.rcParams.update({
    "font.family": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 10,
    "axes.unicode_minus": False,
})


def fmt_elapsed(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def valid(values):
    return [v for v in values if v == v]


def mean(values):
    v = valid(values)
    return sum(v) / len(v) if v else None


def smooth(values, window=CPU_SMOOTH_WINDOW):
    """Rolling median, then a 3-point average. Keeps steep steps, removes jitter."""
    if window <= 1 or len(values) < 3:
        return list(values)
    half = window // 2

    def rolling(vals, h, fn):
        out = []
        for i in range(len(vals)):
            chunk = [v for v in vals[max(0, i - h):i + h + 1] if v == v]
            out.append(fn(chunk) if chunk else NAN)
        return out

    def median(c):
        c = sorted(c)
        n = len(c)
        return c[n // 2] if n % 2 else (c[n // 2 - 1] + c[n // 2]) / 2

    return rolling(rolling(values, half, median), 1, lambda c: sum(c) / len(c))


def _style_axes(ax):
    ax.set_facecolor(THEME["surface"])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(THEME["axis"])
    ax.grid(True, axis="y", color=THEME["grid"], linewidth=0.8)
    ax.grid(True, axis="x", color=THEME["grid"], linewidth=0.5, alpha=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=THEME["muted"], labelsize=9, length=0, pad=6)


def _panel_header(ax, title, handles):
    """Panel title on the left, legend on the right, both above the plot."""
    ax.annotate(title, xy=(0, 1), xycoords="axes fraction", xytext=(0, 9),
                textcoords="offset points", ha="left", va="bottom",
                color=THEME["ink"], fontsize=11, fontweight="bold")
    if handles:
        leg = ax.legend(handles=handles, loc="lower right", bbox_to_anchor=(1.0, 1.0),
                        ncol=len(handles), frameon=False, fontsize=9, borderaxespad=0.55,
                        handlelength=1.6, columnspacing=1.4, handletextpad=0.5)
        for text in leg.get_texts():
            text.set_color(THEME["ink2"])


def _end_labels(ax, items, fmt):
    """Value at the right end of each line, nudged apart so labels never overlap."""
    ymin, ymax = ax.get_ylim()
    height_pt = ax.get_window_extent().height * 72 / ax.figure.dpi
    min_gap = 12 / height_pt
    labels = []
    for values, color in items:
        v = next((x for x in reversed(values) if x == x), None)
        if v is not None:
            labels.append([(v - ymin) / (ymax - ymin), v, color])
    labels.sort(key=lambda l: l[0])
    for _ in range(20):
        moved = False
        for a, b in zip(labels, labels[1:]):
            if b[0] - a[0] < min_gap:
                shift = (min_gap - (b[0] - a[0])) / 2
                a[0] -= shift
                b[0] += shift
                moved = True
        if not moved:
            break
    for frac, v, color in labels:
        ax.plot([1.006], [frac], marker="o", markersize=5, color=color,
                transform=ax.transAxes, clip_on=False)
        ax.text(1.016, frac, fmt(v), transform=ax.transAxes, va="center", ha="left",
                color=THEME["ink"], fontsize=9, clip_on=False)


def plot_chart(out, run, info, hide=()):
    from matplotlib.lines import Line2D
    from matplotlib.ticker import FuncFormatter, MaxNLocator, MultipleLocator

    def handle(label, color, style="-"):
        return Line2D([], [], color=color, linewidth=2, linestyle=style, label=label)

    t, groups, is_root = run["t"], run["groups"], run["is_root"]
    has_fps = bool(valid(run["fps"]))
    has_temp = bool(valid(run["tmax"]))

    panels = [("util", 2.6)]
    if has_fps:
        panels.append(("fps", 1.5))
    if has_temp:
        panels.append(("temp", 1.7))
    if is_root:
        panels += [("clock", 1.7), ("power", 1.5)]
    width, header, gap, bottom = 12.0, 1.7, 0.8, 0.65
    fig_h = header + sum(h for _, h in panels) + gap * (len(panels) - 1) + bottom
    fig = plt.figure(figsize=(width, fig_h), facecolor=THEME["page"])
    left_in, right_in = 0.75, 0.95

    fig.text(left_in / width, 1 - 0.32 / fig_h, f"Stress test: {info['title']}", color=THEME["ink"],
             fontsize=16, fontweight="bold", ha="left", va="top")
    fig.text(left_in / width, 1 - 0.72 / fig_h, info["line1"], color=THEME["ink2"],
             fontsize=10, ha="left", va="top")
    fig.text(left_in / width, 1 - 1.0 / fig_h, info["line2"], color=THEME["ink2"],
             fontsize=10, ha="left", va="top")

    axes = {}
    y_top = fig_h - header
    for i, (key, h) in enumerate(panels):
        y_top -= 0.0 if i == 0 else gap
        rect = [left_in / width, (y_top - h) / fig_h, 1 - (left_in + right_in) / width, h / fig_h]
        ax = fig.add_axes(rect, sharex=axes.get("util"))
        _style_axes(ax)
        if run["load_t"] is not None:  # mark when ffmpeg started
            ax.axvline(run["load_t"], color=THEME["muted"], linewidth=1, linestyle=(0, (2, 2)))
        axes[key] = ax
        y_top -= h

    def pct(v):
        return f"{v:.0f}%"

    # --- Utilization ---
    ax = axes["util"]
    items, handles = [], []
    if "all" not in hide:
        ax.plot(t, run["cpu"], color=COLOR_ALL, linewidth=0.8, alpha=0.3)
        items.append((smooth(run["cpu"]), COLOR_ALL))
        handles.append(handle("All cores", COLOR_ALL))
    for (name, cores), color in zip(groups, COLOR_GROUPS):
        items.append((smooth(run["group_data"][name]), color))
        handles.append(handle(f"{name} cores ({len(cores)})", color))
    if is_root and "gpu" not in hide:
        items.append((run["gpu"], COLOR_GPU))
        handles.append(handle("GPU", COLOR_GPU))
    for values, color in items:
        ax.plot(t, values, color=color, linewidth=2, solid_capstyle="round")
    ax.set_ylim(0, 102)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: pct(v)))
    _panel_header(ax, "Utilization", handles)
    if run["load_t"] is not None:
        ax.annotate("encode starts", xy=(run["load_t"], 0), xycoords=("data", "axes fraction"),
                    xytext=(5, 5), textcoords="offset points", ha="left", va="bottom",
                    color=THEME["muted"], fontsize=8)

    # --- Encode speed ---
    if has_fps:
        ax = axes["fps"]
        # Smoothing fills gaps from neighbours; keep the idle period blank
        fps_s = [s if r == r else NAN for s, r in zip(smooth(run["fps"]), run["fps"])]
        ax.plot(t, run["fps"], color=COLOR_FPS, linewidth=0.8, alpha=0.3)
        ax.plot(t, fps_s, color=COLOR_FPS, linewidth=2)
        ax.set_ylim(0, max(valid(run["fps"])) * 1.1)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 2.5, 5, 10]))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}"))
        enc = run["encodes"]
        _panel_header(ax, "Encode speed (frames per second" + (f", {enc} encodes combined)" if enc > 1 else ")"), [])
        _fps_items = [(fps_s, COLOR_FPS)]

    # --- Temperature ---
    if has_temp:
        ax = axes["temp"]
        ax.plot(t, run["tmax"], color=COLOR_TEMP, linewidth=2)
        ax.plot(t, run["tavg"], color=COLOR_TEMP, linewidth=1.5, linestyle=(0, (3, 2)), alpha=0.7)
        vals = valid(run["tmax"] + run["tavg"])
        lo, hi = min(vals), max(vals)
        pad = max(2.0, (hi - lo) * 0.15)
        ax.set_ylim(lo - pad, hi + pad)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}°"))
        _panel_header(ax, "Chip temperature (°C)",
                      [handle("Hottest sensor", COLOR_TEMP), handle("Average of sensors", COLOR_TEMP, (0, (3, 2)))])

    # --- Clock speed and power (sudo only) ---
    if is_root:
        ax = axes["clock"]
        clock_items, handles = [], []
        for (name, _), color in zip(groups, COLOR_GROUPS):
            ghz = [v / 1000 for v in run["freq_data"][name]]
            ax.plot(t, ghz, color=color, linewidth=2)
            clock_items.append((ghz, color))
            handles.append(handle(f"{name} cores", color))
        ax.set_ylim(bottom=0)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 5, 10]))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.1f}"))
        _panel_header(ax, "Clock speed while active (GHz)", handles)

        ax = axes["power"]
        power_items = [(run["cpu_w"], COLOR_ALL), (run["gpu_w"], COLOR_GPU)]
        power_handles = [handle("CPU (all cores)", COLOR_ALL), handle("GPU", COLOR_GPU)]
        if valid(run["ane_w"]) and max(valid(run["ane_w"])) > 0:
            power_items.append((run["ane_w"], COLOR_ANE))
            power_handles.append(handle("Neural Engine", COLOR_ANE))
        for values, color in power_items:
            ax.plot(t, values, color=color, linewidth=2)
        ax.set_ylim(bottom=0)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 5, 10]))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        _panel_header(ax, "Power (W)", power_handles)

    # Shared time axis, labels on the bottom panel only
    last = axes[panels[-1][0]]
    for key, _ in panels[:-1]:
        axes[key].tick_params(labelbottom=False)
    last.xaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_elapsed(v)))
    last.set_xlabel("Elapsed time (m:ss)", color=THEME["muted"], fontsize=9, labelpad=8)
    axes["util"].set_xlim(0, t[-1])
    step = next((s for s in (10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600) if t[-1] / s <= 9), 7200)
    last.xaxis.set_major_locator(MultipleLocator(step))

    _end_labels(axes["util"], items, pct)
    if has_fps:
        _end_labels(axes["fps"], _fps_items, lambda v: f"{v:.0f}")
    if has_temp:
        _end_labels(axes["temp"], [(run["tmax"], COLOR_TEMP), (run["tavg"], COLOR_TEMP)], lambda v: f"{v:.1f}°")
    if is_root:
        _end_labels(axes["clock"], clock_items, lambda v: f"{v:.2f}")
        _end_labels(axes["power"], power_items, lambda v: f"{v:.1f} W")

    fig.savefig(out, dpi=200, facecolor=THEME["page"])
    plt.close(fig)


# ---------------------------------------------------------------------------
# Machine info, output, checks
# ---------------------------------------------------------------------------
def real_home():
    user = os.environ.get("SUDO_USER")  # under sudo, "~" is root's home
    return pwd.getpwnam(user).pw_dir if user else os.path.expanduser("~")


def resolve_out_base(out):
    """
    The folder that will hold this run's results folder. Checked before the
    test starts, so a bad path doesn't waste a 30-minute run.
    """
    if not out:
        base = os.path.join(real_home(), "Desktop")
    else:
        # A quoted "~" reaches us unexpanded; under sudo, expanduser would give root's home
        if out == "~" or out.startswith("~/"):
            out = real_home() + out[1:]
        base = os.path.abspath(os.path.expanduser(out))
    if os.path.exists(base) and not os.path.isdir(base):
        sys.exit(f"[!] --out: {base} exists and isn't a folder.")
    if not os.path.isdir(base):
        try:
            os.makedirs(base)
        except OSError as e:
            sys.exit(f"[!] --out: couldn't create {base}: {e.strerror}")
        give_to_user(base)
        print(f"[i] Created {base}")
    if not os.access(base, os.W_OK):
        sys.exit(f"[!] --out: can't write to {base}.")
    return base


def give_to_user(path):
    user = os.environ.get("SUDO_USER")
    if user:  # don't leave root-owned files on the user's Desktop
        pw = pwd.getpwnam(user)
        os.chown(path, pw.pw_uid, pw.pw_gid)


def machine_info(ffmpeg):
    batt = run_cmd(["pmset", "-g", "batt"])
    m = re.search(r"drawing from '([^']+)'", batt)
    # Older macOS: "lowpowermode 1". Newer: "powermode 1" (0 automatic, 1 low, 2 high)
    low = re.search(r"\b(?:lowpowermode|powermode)\s+(\d)", run_cmd(["pmset", "-g"]))
    ff = run_cmd([ffmpeg, "-hide_banner", "-version"]).splitlines() if ffmpeg else []
    ff = [ff[0].split(" Copyright")[0]] if ff else []
    return {
        "chip": sysctl("machdep.cpu.brand_string") or platform.processor() or "unknown",
        "model": sysctl("hw.model") or "unknown",
        "cores": psutil.cpu_count(),
        "memory_gb": round(psutil.virtual_memory().total / 2**30),
        "macos": platform.mac_ver()[0] or "unknown",
        "power": m.group(1) if m else "unknown",
        "low_power_mode": ("on" if low.group(1) == "1" else "off") if low else "unknown",
        "ffmpeg": ff[0] if ff else "not found",
    }


def check(ffmpeg, verbose=True):
    """Return a list of problems that stop the test; print notes along the way."""
    problems = []

    def note(ok, msg):
        if verbose:
            print(f"  [{'ok' if ok else '!!'}] {msg}")

    if verbose:
        print("Checking requirements:")
    note(True, f"Python {platform.python_version()}, psutil {psutil.__version__}, matplotlib {matplotlib.__version__}")
    if sys.platform != "darwin":
        problems.append("This test only runs on macOS.")
        note(False, problems[-1])
    if not ffmpeg:
        problems.append("ffmpeg not found. Install it with: brew install ffmpeg")
        note(False, problems[-1])
    else:
        encoders = run_cmd([ffmpeg, "-hide_banner", "-encoders"])
        if "libx264" not in encoders:
            problems.append(f"{ffmpeg} has no libx264 encoder. Homebrew's ffmpeg has it: brew install ffmpeg")
            note(False, problems[-1])
        else:
            note(True, f"ffmpeg with libx264: {ffmpeg}")
    if verbose:
        info = machine_info(ffmpeg)
        temp_ok = bool(SocTemperature().sensors)
        note(temp_ok, "chip temperature sensors found" if temp_ok
             else "no chip temperature sensors (Intel Mac?): temperature won't be graphed")
        root = os.geteuid() == 0
        note(root, "running with sudo: GPU, clock speed and power included" if root
             else "not running with sudo: GPU, clock speed and power will be left out")
        note(info["power"] in ("AC Power", "unknown"), f"power source: {info['power']}")
        note(info["low_power_mode"] != "on", f"Low Power Mode: {info['low_power_mode']}")
        print(f"\nMac: {info['model']}, {info['chip']}, {info['cores']} cores, "
              f"{info['memory_gb']} GB, macOS {info['macos']}")
        print("Ready to run." if not problems else "Fix the [!!] items above before running.")
    return problems


def fps_window(t, fps, t0, t1):
    return mean([f for x, f in zip(t, fps) if t0 < x <= t1])


def write_csv(path, run):
    names = [n for n, _ in run["groups"]]
    header = (["time_s", "cpu_all_pct"] + [f"{n.lower()}_pct" for n in names] + ["gpu_pct"]
              + [f"{n.lower()}_mhz" for n in names]
              + ["cpu_w", "gpu_w", "ane_w", "temp_max_c", "temp_avg_c", "encode_fps", "frames_total"])
    cols = ([run["t"], run["cpu"]] + [run["group_data"][n] for n in names] + [run["gpu"]]
            + [run["freq_data"][n] for n in names]
            + [run["cpu_w"], run["gpu_w"], run["ane_w"], run["tmax"], run["tavg"], run["fps"], run["frames"]])
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in zip(*cols):
            w.writerow(["" if v != v else round(v, 3) for v in row])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 usage="%(prog)s [--duration N] [--warmup N] [--encodes N] [--out DIR] [--check]")
    ap.add_argument("--duration", type=int, default=1800, metavar="N",
                    help="seconds of encoding (default 1800)")
    ap.add_argument("--warmup", type=int, default=30, metavar="N",
                    help="idle seconds recorded before the encode starts (default 30)")
    ap.add_argument("--encodes", type=int, default=3, metavar="N",
                    help="identical encodes run side by side (default 3)")
    ap.add_argument("--out", metavar="DIR",
                    help="folder to save results in; each run gets its own dated folder inside it "
                         "(default: your Desktop)")
    ap.add_argument("--check", action="store_true", help="check the requirements and exit")
    args = ap.parse_args()
    if args.duration < 2 * INTERVAL:
        ap.error(f"--duration must be at least {2 * INTERVAL}")
    if args.warmup < 0:
        ap.error("--warmup can't be negative")
    if args.encodes < 1:
        ap.error("--encodes must be at least 1")
    return args


def main():
    global running
    args = parse_args()
    ffmpeg = find_ffmpeg()

    if args.check:
        sys.exit(1 if check(ffmpeg) else 0)
    problems = check(ffmpeg, verbose=False)
    if problems:
        sys.exit("[!] " + "\n[!] ".join(problems) + "\n    Run with --check for details.")
    out_base = resolve_out_base(args.out)

    info = machine_info(ffmpeg)
    if info["power"] not in ("AC Power", "unknown"):
        print(f"[!] Running on {info['power']}. Plug in for results you can compare.")
    if info["low_power_mode"] == "on":
        print("[!] Low Power Mode is on, which slows the Mac down. Results won't compare with other runs.")

    sensor = SocTemperature()
    groups = core_groups()
    cpu_type = {i: name for name, cores in groups for i in cores}
    is_root = os.geteuid() == 0
    print(f"[i] {info['model']}, {info['chip']}, macOS {info['macos']}")
    print(f"[i] Results will be saved in {out_base}")
    if groups:
        print("[i] Core types: " + ", ".join(f"{n} = cpu{c[0]}-{c[-1]} ({len(c)})" for n, c in groups))
    if not sensor.sensors:
        print("[i] No chip temperature sensors found: temperature won't be recorded.")
    if is_root:
        threading.Thread(target=powermetrics_reader, args=(cpu_type,), daemon=True).start()
    else:
        print("[i] Not running with sudo: GPU %, clock speed and power are left out.")

    # Keep the Mac awake for as long as this script runs
    try:
        subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass

    run = {"t": [], "cpu": [], "gpu": [], "cpu_w": [], "gpu_w": [], "ane_w": [], "tmax": [], "tavg": [],
           "fps": [], "frames": [], "groups": groups, "is_root": is_root, "encodes": args.encodes,
           "group_data": {n: [] for n, _ in groups}, "freq_data": {n: [] for n, _ in groups},
           "load_t": None}

    total_min = (args.warmup + args.duration) / 60
    print(f"[+] Recording for {total_min:.1f} min: {args.warmup} s idle, then {args.duration} s of "
          f"{args.encodes} ffmpeg encode{'s' if args.encodes > 1 else ''}. Ctrl-C ends early.\n")

    hdr = f"{'Time':<8}{'CPU%':<7}"
    for name, _ in groups:
        hdr += f"{name[:5] + '%':<8}"
        if is_root:
            hdr += f"{name[:5] + ' MHz':<11}"
    if is_root:
        hdr += f"{'CPU W':<8}{'GPU%':<7}{'GPU W':<8}"
    hdr += f"{'Temp max':<10}{'fps':<7}"
    print(hdr)
    print("-" * len(hdr))

    def fmt(v, spec, width):
        return f"{(format(v, spec) if v is not None and v == v else '-'):<{width}}"

    psutil.cpu_percent(interval=None, percpu=True)  # first call always returns 0.0
    start = time.monotonic()
    load_at = start + args.warmup
    end_at = None
    next_tick = start + INTERVAL
    enc = None
    prev_frames, prev_time = 0, None
    stop_reason = "time limit reached"
    load_frames = load_secs = None

    try:
        while True:
            wake = next_tick if enc else min(next_tick, load_at)
            delay = wake - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            now = time.monotonic()

            if enc is None and now >= load_at:
                enc = Encoders(ffmpeg, args.encodes)
                enc.start()
                end_at = now + args.duration
                prev_time = now
                run["load_t"] = now - start
                print(f"--- {fmt_elapsed(now - start)}  ffmpeg started "
                      f"({args.encodes} encode{'s' if args.encodes > 1 else ''}) ---")
            if now < next_tick:
                continue
            next_tick += INTERVAL
            if end_at is not None and next_tick > end_at:
                next_tick = max(end_at, now + 0.5)  # last sample lands on the end time

            per_core = psutil.cpu_percent(interval=None, percpu=True)
            cpu = sum(per_core) / len(per_core)
            group_vals = [sum(per_core[i] for i in cores) / len(cores) for _, cores in groups]
            tmax, tavg = sensor.read() if sensor.sensors else (None, None)
            fps, frames = NAN, NAN
            if enc:
                frames = enc.total()
                fps = (frames - prev_frames) / (now - prev_time) if now > prev_time else NAN
                prev_frames, prev_time = frames, now

            def num(v):
                return v if v is not None else NAN

            run["t"].append(round(now - start, 1))
            run["cpu"].append(cpu)
            for (name, _), v in zip(groups, group_vals):
                run["group_data"][name].append(v)
                run["freq_data"][name].append(num(pm["freq"].get(name)) if is_root else NAN)
            run["gpu"].append(num(pm["gpu"]) if is_root else NAN)
            run["cpu_w"].append(num(pm["cpu_w"]) if is_root else NAN)
            run["gpu_w"].append(num(pm["gpu_w"]) if is_root else NAN)
            run["ane_w"].append(num(pm["ane_w"]) if is_root else NAN)
            run["tmax"].append(num(tmax))
            run["tavg"].append(num(tavg))
            run["fps"].append(fps)
            run["frames"].append(frames)

            row = f"{fmt_elapsed(now - start):<8}{cpu:<7.1f}"
            for (name, _), v in zip(groups, group_vals):
                row += f"{v:<8.1f}"
                if is_root:
                    row += fmt(pm["freq"].get(name), ".0f", 11)
            if is_root:
                row += fmt(pm["cpu_w"], ".1f", 8) + fmt(pm["gpu"], ".1f", 7) + fmt(pm["gpu_w"], ".1f", 8)
            row += fmt(tmax, ".1f", 10) + fmt(fps, ".0f", 7)
            print(row)

            if enc and enc.all_exited():
                stop_reason = "ffmpeg exited early (see any error above)"
                break
            if end_at is not None and now >= end_at - 0.05:
                break
    except KeyboardInterrupt:
        stop_reason = "stopped with Ctrl-C"
    finally:
        running = False
        if enc:
            load_frames = enc.total()
            load_secs = time.monotonic() - (start + run["load_t"])

    # 1. Recording is over. 2. Stop ffmpeg. 3. Save.
    print(f"\n[-] Recording stopped: {stop_reason}.")
    if enc:
        print("[-] Stopping ffmpeg...")
        enc.stop()
    if len(run["t"]) < 2:
        print("[-] Not enough samples for a graph.")
        return 130 if stop_reason.startswith("stopped") else 1

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    out_dir = os.path.join(out_base, f"mac_stress_test_{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    give_to_user(out_dir)

    csv_path = os.path.join(out_dir, "data.csv")
    write_csv(csv_path, run)  # first, so the numbers survive a plotting failure
    give_to_user(csv_path)

    # Results
    t, lt = run["t"], run["load_t"]
    loaded = [i for i, x in enumerate(t) if lt is not None and x > lt]
    avg_fps = load_frames / load_secs if load_frames and load_secs else None
    first = last = None
    if lt is not None and load_secs and load_secs >= 2 * COMPARE_WINDOW:
        first = fps_window(t, run["fps"], lt, lt + COMPARE_WINDOW)
        last = fps_window(t, run["fps"], t[-1] - COMPARE_WINDOW, t[-1])
    avg_cpu = mean([run["cpu"][i] for i in loaded]) if loaded else None
    peak_temp = max(valid(run["tmax"]), default=None)
    avg_cpu_w = mean([run["cpu_w"][i] for i in loaded]) if loaded and is_root else None

    results = [
        ("Encode time", fmt_elapsed(load_secs) if load_secs else "none (stopped before the encode started)"),
        ("Frames encoded", f"{load_frames:,}" if load_frames is not None else "-"),
        ("Average encode speed", f"{avg_fps:.1f} fps" if avg_fps else "-"),
    ]
    if first and last:
        change = (last - first) / first * 100
        results.append(("Speed, first 5 min", f"{first:.1f} fps"))
        results.append(("Speed, last 5 min", f"{last:.1f} fps ({change:+.0f}%)"))
    results += [
        ("Average CPU under load", f"{avg_cpu:.0f}%" if avg_cpu is not None else "-"),
        ("Peak chip temperature", f"{peak_temp:.1f} °C" if peak_temp is not None else "-"),
    ]
    if avg_cpu_w is not None:
        results.append(("Average CPU power under load", f"{avg_cpu_w:.1f} W"))

    setup = [
        ("Mac", f"{info['model']}, {info['chip']}, {info['cores']} cores, {info['memory_gb']} GB"),
        ("macOS", info["macos"]),
        ("Power source", info["power"]),
        ("Low Power Mode", info["low_power_mode"]),
        ("ffmpeg", info["ffmpeg"]),
        ("Encode", f"{args.encodes} x {ENCODE_DESC}"),
        ("Planned", f"{args.warmup} s idle + {args.duration} s encoding"),
        ("Ended", stop_reason),
        ("sudo", "yes" if is_root else "no (no GPU, clock or power data)"),
        ("Date", datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
    ]
    width = max(len(k) for k, _ in setup + results) + 2
    text = "Mac stress test\n\n" + "\n".join(f"{k + ':':<{width}}{v}" for k, v in setup)
    text += "\n\nResults\n" + "\n".join(f"{k + ':':<{width}}{v}" for k, v in results) + "\n"
    summary_path = os.path.join(out_dir, "summary.txt")
    with open(summary_path, "w") as f:
        f.write(text)
    give_to_user(summary_path)

    print("\n" + text)
    print("[+] Drawing graphs...")
    enc_label = f"{args.encodes} x ffmpeg libx264 1080p" if args.encodes > 1 else "ffmpeg libx264 1080p"
    line2 = [f"{fmt_elapsed(load_secs) if load_secs else '0:00'} under load"]
    if avg_fps:
        line2.append(f"{avg_fps:.1f} fps average")
    if first and last:
        line2.append(f"{(last - first) / first * 100:+.0f}% fps first→last 5 min")
    if avg_cpu is not None:
        line2.append(f"avg CPU {avg_cpu:.0f}%")
    if peak_temp is not None:
        line2.append(f"peak {peak_temp:.1f} °C")
    chart_info = {
        "title": enc_label,
        "line1": f"{info['chip']}   ·   {info['model']}   ·   macOS {info['macos']}   ·   {info['power']}",
        "line2": "   ·   ".join(line2),
    }
    for name, hide in (("chart.png", ()), ("chart_cores.png", ("all", "gpu"))):
        path = os.path.join(out_dir, name)
        plot_chart(path, run, chart_info, hide)
        give_to_user(path)
    print(f"[#] Saved to {out_dir}")
    return 130 if stop_reason.startswith("stopped") else 0


if __name__ == "__main__":
    sys.exit(main())
