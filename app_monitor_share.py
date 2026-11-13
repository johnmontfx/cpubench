"""
Mac performance monitor | setup and usage
=========================================

This script samples system-wide activity while you run an app or workload.
Use the same workload and duration when comparing Macs.

1. SET UP PYTHON
   Save this file to your Desktop as app_monitor_share.py. Open Terminal.
   Install a current Python 3 from python.org if you do not already have one.
   On Apple Silicon, use a native arm64 Python and Terminal (without Rosetta).

   Copy these commands into Terminal. Run them as your normal user:

       python3 -m venv "$HOME/.venvs/mac-monitor"
       "$HOME/.venvs/mac-monitor/bin/python" -m pip install psutil matplotlib

   Keep using that exact Python path below so the script can find the packages.
   The setup commands only need to be run once.

2. RUN A ONE-MINUTE TEST
   Start the workload you want to observe, then run:

       "$HOME/.venvs/mac-monitor/bin/python" "$HOME/Desktop/app_monitor_share.py" --seconds=60

   On Apple Silicon, use sudo for the extra powermetrics readings:

       sudo "$HOME/.venvs/mac-monitor/bin/python" "$HOME/Desktop/app_monitor_share.py" --seconds=60

   Enter your Mac login password in Terminal if prompted. Typing is invisible.
   Use the sudo command only after reviewing the script and if your account
   has administrator access. Ordinary CPU monitoring works without sudo.
   Omit --seconds to run until Ctrl+C. Press Ctrl+C once to stop and save early.
   Allow at least 4 seconds for a recording. --seconds=3600 records one hour.

3. FIND YOUR RESULTS
   By default the Desktop gets mac_monitor_chart.png and mac_monitor_data.csv.
   If core groups were detected, mac_monitor_chart_cores.png shows just those
   groups in the utilization panel. New recordings replace these files, so
   rename or move them to keep a comparison. --out changes the chart path.

   Redraw from a saved recording without sudo:

       "$HOME/.venvs/mac-monitor/bin/python" "$HOME/Desktop/app_monitor_share.py" --from "$HOME/Desktop/mac_monitor_data.csv"

   --from also accepts the script's printed table saved as plain text.
   --hide all,gpu draws a single chart without those utilization lines.
   --help lists the available options.

HARDWARE AND READINGS
   The full sensor path was developed on an Apple M5 Max. Core group names
   and counts are discovered from macOS, so other Apple Silicon chips may
   list Performance and Efficiency cores instead of Super and Performance.
   The per-group mapping assumes logical CPUs are ordered slowest group first.
   Treat group readings as provisional on other models.

   Temperature uses macOS IOKit die sensors named PMU tdie*. The script keeps
   recording if it cannot find these sensors. Intel Macs use CPU utilization
   only. On Apple Silicon, sudo enables GPU utilization, CPU cluster clocks,
   and CPU, GPU, and Neural Engine power where powermetrics provides them.
   Fields can vary across models and macOS releases. A dash or blank CSV
   field is a missing measurement. A numeric zero is a reported value.

TROUBLESHOOTING
   "No module named psutil" or "matplotlib"
       Repeat the pip install above using the same Python path as the run.
   "Operation not permitted" opening or writing Desktop files
       Check System Settings > Privacy & Security > Files and Folders for
       your terminal app's Desktop access.
   Missing temperature or power values
       Check the startup messages. For power readings on Apple Silicon, try
       the sudo command above. Availability still depends on the Mac and OS.
       Include your chip model, macOS version and startup messages when
       sharing results or reporting an issue.
"""

import argparse
import csv
import ctypes
import ctypes.util
import os
import platform
import pwd
import re
import subprocess
import sys
import threading
import time

import psutil
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Configuration
INTERVAL = 2  # seconds between samples
# Smoothing for the CPU % lines on the graph, in samples (5 = 10 s at INTERVAL 2).
# A rolling median takes out short wiggles but keeps big steps sharp; a light
# 3-sample average then rounds off what's left. 1 = no smoothing. The console
# always shows raw numbers.
CPU_SMOOTH_WINDOW = 5

# ---------------------------------------------------------------------------
# Temperature via IOKit HID (Apple Silicon)
# ---------------------------------------------------------------------------
class SocTemperature:
	_kIOHIDEventTypeTemperature = 15
	_kCFStringEncodingUTF8 = 0x08000100
	_kCFNumberSInt32Type = 3

	def __init__(self):
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

		# Match Apple's vendor temperature sensors: PrimaryUsagePage 0xff00, PrimaryUsage 5
		keys = (vp * 2)(self._cfstr("PrimaryUsagePage"), self._cfstr("PrimaryUsage"))
		vals = (vp * 2)(self._cfnum(0xFF00), self._cfnum(5))
		key_cb = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
		val_cb = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
		matching = cf.CFDictionaryCreate(None, keys, vals, 2, key_cb, val_cb)

		self.client = iokit.IOHIDEventSystemClientCreate(None)
		iokit.IOHIDEventSystemClientSetMatching(self.client, matching)
		self._product_key = self._cfstr("Product")

		# Find the die sensors once
		self.sensors = []
		services = iokit.IOHIDEventSystemClientCopyServices(self.client)
		if services:
			for i in range(cf.CFArrayGetCount(services)):
				svc = cf.CFArrayGetValueAtIndex(services, i)
				if self._name(svc).startswith("PMU tdie"):
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
		"""Return (max, average) die temperature in °C, or (None, None)."""
		t = self._kIOHIDEventTypeTemperature
		temps = []
		for svc in self.sensors:
			ev = self.iokit.IOHIDServiceClientCopyEvent(svc, t, 0, 0)
			if ev:
				val = self.iokit.IOHIDEventGetFloatValue(ev, t << 16)
				self.cf.CFRelease(ev)
				if 0 < val < 150:  # drop bogus/unpopulated sensors
					temps.append(val)
		if not temps:
			return None, None
		return max(temps), sum(temps) / len(temps)


# ---------------------------------------------------------------------------
# GPU %, clock speeds and power via powermetrics (needs root)
# ---------------------------------------------------------------------------
running = True
# Latest values from powermetrics; read by the main loop
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
	Feed one line of powermetrics text. `state` holds the clusters seen in the
	current sample; `cpu_type` maps cpu index -> core type name.

	A core type can span several clusters (on the M5 Max, Performance = P0 + P1,
	Super = S), so its clock is the average of its clusters' active frequency,
	weighted by how much of the time each cluster was actually running.
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
	"""Keep `pm` updated from a powermetrics stream."""
	cmd = ["powermetrics", "--samplers", "cpu_power,gpu_power", "-i", str(INTERVAL * 1000)]
	try:
		proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
								stdin=subprocess.DEVNULL, text=True)
	except Exception as e:
		print(f"[-] Couldn't start powermetrics: {e}")
		return
	state = {}
	while running and proc.poll() is None:
		line = proc.stdout.readline()
		if not line:
			break
		parse_powermetrics_line(line, state, cpu_type)
	proc.terminate()


# ---------------------------------------------------------------------------
# Core clusters (Super / Performance / Efficiency)
# ---------------------------------------------------------------------------
def _sysctl(key):
	return subprocess.run(["sysctl", "-n", key], capture_output=True, text=True,
						  stdin=subprocess.DEVNULL).stdout.strip()


def core_groups():
	"""
	Return [(name, [cpu indices])], fastest core type first, e.g.
	[("Super", [12..17]), ("Performance", [0..11])] on an M5 Max.

	macOS lists core types fastest-first (hw.perflevel0 = fastest), but numbers
	the CPUs slowest-first (cpu0 is in the slowest cluster), so the slowest
	type gets the lowest CPU numbers.
	"""
	try:
		levels = []
		for i in range(int(_sysctl("hw.nperflevels"))):
			levels.append((_sysctl(f"hw.perflevel{i}.name"),
						   int(_sysctl(f"hw.perflevel{i}.logicalcpu"))))
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
# Graph (dark theme)
# ---------------------------------------------------------------------------
THEME = {
	"page": "#0d0d0d",      # figure background
	"surface": "#1a1a19",   # plot area
	"ink": "#f0efec",       # titles, values
	"ink2": "#c3c2b7",      # legend, subtitle
	"muted": "#898781",     # tick labels
	"grid": "#2c2c2a",
	"axis": "#383835",
}
# One colour per thing measured, used in every panel it appears in
COLOR_ALL = "#3987e5"                              # all CPU cores / CPU power
COLOR_GROUPS = ["#d95926", "#199e70", "#9085e9"]   # core types, fastest first
COLOR_GPU = "#c98500"
COLOR_ANE = "#d55181"                              # Neural Engine
COLOR_TEMP = "#e66767"

plt.rcParams.update({
	"font.family": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
	"font.size": 10,
	"axes.unicode_minus": False,
})


def _last_valid(values):
	for v in reversed(values):
		if v == v:
			return v
	return None


def _fmt_elapsed(seconds):
	seconds = int(round(seconds))
	return f"{seconds // 60}:{seconds % 60:02d}"


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
	"""Panel title on the left and its legend on the right, both above the plot."""
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
	min_gap = 12 / height_pt  # 12 pt in axes-fraction units
	labels = []
	for values, color in items:
		v = _last_valid(values)
		if v is not None:
			labels.append([(v - ymin) / (ymax - ymin), v, color])
	labels.sort(key=lambda l: l[0])
	for _ in range(20):  # spread out any collisions
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


def plot_chart(out, t, cpu, groups, group_data, freq_data, gpu, cpu_w, gpu_w, tmax, tavg, is_root,
			   hide=(), ane_w=None):
	from matplotlib.lines import Line2D
	from matplotlib.ticker import FuncFormatter, MaxNLocator, MultipleLocator

	def handle(label, color, style="-"):
		return Line2D([], [], color=color, linewidth=2, linestyle=style, label=label)

	# Panel layout, in inches: (key, plot height)
	panels = [("util", 2.6)]
	valid = [v for v in tmax + tavg if v == v]
	if valid:
		panels.append(("temp", 1.7))
	if is_root:
		panels += [("clock", 1.7), ("power", 1.5)]
	width, header, gap, bottom = 12.0, 1.45, 0.8, 0.65
	fig_h = header + sum(h for _, h in panels) + gap * (len(panels) - 1) + bottom
	fig = plt.figure(figsize=(width, fig_h), facecolor=THEME["page"])
	left_in, right_in = 0.75, 0.95  # right margin holds the end-of-line values

	# Header: title + one-line summary
	chip = _sysctl("machdep.cpu.brand_string") or "Apple Silicon"
	duration = _fmt_elapsed(t[-1]) if t else "0:00"
	avg_cpu = sum(cpu) / len(cpu)
	peak = max((v for v in tmax if v == v), default=None)
	summary = f"{chip}   ·   {duration} recorded   ·   avg CPU {avg_cpu:.0f}%"
	if peak is not None:
		summary += f"   ·   peak {peak:.1f} °C"
	fig.text(left_in / width, 1 - 0.32 / fig_h, "Mac performance", color=THEME["ink"],
			 fontsize=16, fontweight="bold", ha="left", va="top")
	fig.text(left_in / width, 1 - 0.68 / fig_h, summary, color=THEME["ink2"],
			 fontsize=10, ha="left", va="top")

	axes = {}
	y_top = fig_h - header
	for i, (key, h) in enumerate(panels):
		y_top -= 0.0 if i == 0 else gap
		rect = [left_in / width, (y_top - h) / fig_h, 1 - (left_in + right_in) / width, h / fig_h]
		ax = fig.add_axes(rect, sharex=axes.get("util"))
		_style_axes(ax)
		axes[key] = ax
		y_top -= h

	def pct(v): return f"{v:.0f}%"

	# --- Utilization ---
	ax = axes["util"]
	items, handles = [], []
	if "all" not in hide:
		ax.plot(t, cpu, color=COLOR_ALL, linewidth=0.8, alpha=0.3)  # raw, faint
		items.append((smooth(cpu), COLOR_ALL))
		handles.append(handle("All cores", COLOR_ALL))
	for (name, cores), color in zip(groups, COLOR_GROUPS):
		if name.lower() in hide:
			continue
		items.append((smooth(group_data[name]), color))
		handles.append(handle(f"{name} cores ({len(cores)})", color))
	if is_root and "gpu" not in hide:
		items.append((gpu, COLOR_GPU))
		handles.append(handle("GPU", COLOR_GPU))
	for values, color in items:
		ax.plot(t, values, color=color, linewidth=2, solid_capstyle="round")
	ax.set_ylim(0, 102)
	ax.yaxis.set_major_locator(MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]))
	ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: pct(v)))
	_panel_header(ax, "Utilization", handles)

	if valid:
		# --- Temperature ---
		ax = axes["temp"]
		ax.plot(t, tmax, color=COLOR_TEMP, linewidth=2)
		ax.plot(t, tavg, color=COLOR_TEMP, linewidth=1.5, linestyle=(0, (3, 2)), alpha=0.7)
		lo, hi = min(valid), max(valid)
		pad = max(2.0, (hi - lo) * 0.15)
		ax.set_ylim(lo - pad, hi + pad)
		ax.yaxis.set_major_locator(MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]))
		ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}°"))
		_panel_header(ax, "Chip temperature (°C)",
					  [handle("Hottest sensor", COLOR_TEMP), handle("Average of sensors", COLOR_TEMP, (0, (3, 2)))])
		temp_items = [(tmax, COLOR_TEMP), (tavg, COLOR_TEMP)]

	# --- Clock speed and power (sudo only) ---
	if is_root:
		ax = axes["clock"]
		clock_items, handles = [], []
		for (name, _), color in zip(groups, COLOR_GROUPS):
			ghz = [v / 1000 for v in freq_data[name]]
			ax.plot(t, ghz, color=color, linewidth=2)
			clock_items.append((ghz, color))
			handles.append(handle(f"{name} cores", color))
		ax.set_ylim(bottom=0)
		ax.yaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 5, 10]))
		ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.1f}"))
		_panel_header(ax, "Clock speed while active (GHz)", handles)

		ax = axes["power"]
		ax.plot(t, cpu_w, color=COLOR_ALL, linewidth=2)
		ax.plot(t, gpu_w, color=COLOR_GPU, linewidth=2)
		power_items = [(cpu_w, COLOR_ALL), (gpu_w, COLOR_GPU)]
		power_handles = [handle("CPU (all cores)", COLOR_ALL), handle("GPU", COLOR_GPU)]
		if ane_w and any(v == v for v in ane_w):  # older CSVs have no Neural Engine column
			ax.plot(t, ane_w, color=COLOR_ANE, linewidth=2)
			power_items.append((ane_w, COLOR_ANE))
			power_handles.append(handle("Neural Engine", COLOR_ANE))
		ax.set_ylim(bottom=0)
		ax.yaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 5, 10]))
		ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
		_panel_header(ax, "Power (W)", power_handles)

	# Shared time axis: labels only on the bottom panel
	last = axes[panels[-1][0]]
	for key, _ in panels[:-1]:
		axes[key].tick_params(labelbottom=False)
	last.xaxis.set_major_formatter(FuncFormatter(lambda v, _: _fmt_elapsed(v)))
	last.set_xlabel("Elapsed time (m:ss)", color=THEME["muted"], fontsize=9, labelpad=8)
	axes["util"].set_xlim(0, t[-1])
	step = next((s for s in (10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
				 if t[-1] / s <= 9), 7200)
	last.xaxis.set_major_locator(MultipleLocator(step))

	# End-of-line values (after limits are final)
	_end_labels(axes["util"], items, pct)
	if valid:
		_end_labels(axes["temp"], temp_items, lambda v: f"{v:.1f}°")
	if is_root:
		_end_labels(axes["clock"], clock_items, lambda v: f"{v:.2f}")
		_end_labels(axes["power"], power_items, lambda v: f"{v:.1f} W")

	fig.savefig(out, dpi=200, facecolor=THEME["page"])
	plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def desktop_path(filename):
	# Under sudo, "~" is root's home; save to the real user's Desktop instead.
	user = os.environ.get("SUDO_USER")
	home = pwd.getpwnam(user).pw_dir if user else os.path.expanduser("~")
	return os.path.join(home, "Desktop", filename)


NAN = float("nan")


def smooth(values, window=CPU_SMOOTH_WINDOW):
	"""Rolling median, then a 3-point average. Keeps steep drops, removes jitter."""
	if window <= 1 or len(values) < 3:
		return list(values)
	half = window // 2

	def rolling(vals, h, fn):
		out = []
		for i in range(len(vals)):
			chunk = [v for v in vals[max(0, i - h):i + h + 1] if v == v]  # skip NaN
			out.append(fn(chunk) if chunk else NAN)
		return out

	def median(c):
		c = sorted(c)
		n = len(c)
		return c[n // 2] if n % 2 else (c[n // 2 - 1] + c[n // 2]) / 2

	return rolling(rolling(values, half, median), 1, lambda c: sum(c) / len(c))


def save_csv(path, run):
	"""Write every sample to a CSV so the graph can be redrawn later with --from."""
	names = [n for n, _ in run["groups"]]
	header = ["time_s", "cpu_all_pct"] + [f"{n.lower()}_pct" for n in names] + ["gpu_pct"]
	header += [f"{n.lower()}_mhz" for n in names] + ["cpu_w", "gpu_w", "ane_w", "temp_max_c", "temp_avg_c"]
	cols = [run["t"], run["cpu"]] + [run["group_data"][n] for n in names] + [run["gpu"]]
	cols += [run["freq_data"][n] for n in names] + [run["cpu_w"], run["gpu_w"], run["ane_w"], run["tmax"], run["tavg"]]
	with open(path, "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(header)
		for row in zip(*cols):
			w.writerow(["" if v != v else round(v, 3) for v in row])


def _num(token):
	try:
		return float(token.rstrip("°C"))
	except ValueError:
		return NAN


def load_run(path):
	"""Read a CSV from save_csv(), or the text the script printed while running."""
	with open(os.path.expanduser(path), errors="replace") as f:
		text = f.read()
	lines = text.splitlines()
	known = core_groups()

	def group_for(label):
		for name, cores in known:
			if name.lower().startswith(label.lower()):
				return name, cores
		return label.capitalize(), []

	run = {"t": [], "cpu": [], "gpu": [], "cpu_w": [], "gpu_w": [], "ane_w": [], "tmax": [], "tavg": []}

	if lines and lines[0].startswith("time_s,"):
		rows = list(csv.DictReader(lines))
		names = [h[:-4] for h in rows[0].keys() if h.endswith("_pct") and h not in ("cpu_all_pct", "gpu_pct")]
		groups = [group_for(n) for n in names]
		run["groups"] = groups
		run["group_data"] = {g: [_num(r[f"{n}_pct"]) for r in rows] for (g, _), n in zip(groups, names)}
		run["freq_data"] = {g: [_num(r[f"{n}_mhz"]) for r in rows] for (g, _), n in zip(groups, names)}
		for key, col in (("t", "time_s"), ("cpu", "cpu_all_pct"), ("gpu", "gpu_pct"), ("cpu_w", "cpu_w"),
						 ("gpu_w", "gpu_w"), ("tmax", "temp_max_c"), ("tavg", "temp_avg_c")):
			run[key] = [_num(r[col]) for r in rows]
		run["ane_w"] = [_num(r.get("ane_w") or "") for r in rows]  # missing in older CSVs
		run["is_root"] = any(v == v for v in run["cpu_w"])
		return run

	# Console output: find the column header, then read the rows under it
	header = next((l for l in lines if l.startswith("Time") and "CPU%" in l), None)
	if header is None:
		sys.exit(f"[!] {path}: no data found (expected the CSV, or the printed table starting with 'Time  CPU%').")
	# Work out the columns from the header, so any column order reads correctly
	cols, words = [], header.split()
	i = 0
	while i < len(words):
		w, nxt = words[i], (words[i + 1] if i + 1 < len(words) else "")
		pair = {("CPU", "W"): "cpu_w", ("GPU", "W"): "gpu_w", ("ANE", "W"): "ane_w", ("Temp", "max"): "tmax", ("Temp", "avg"): "tavg"}
		if (w, nxt) in pair:
			cols.append((pair[(w, nxt)], None)); i += 2; continue
		if nxt == "MHz":
			cols.append(("mhz", w)); i += 2; continue
		if w == "Time":
			cols.append(("t", None))
		elif w == "CPU%":
			cols.append(("cpu", None))
		elif w == "GPU%":
			cols.append(("gpu", None))
		elif w.endswith("%"):
			cols.append(("pct", w[:-1]))
		i += 1

	labels = []
	for kind, label in cols:
		if label and label not in labels:
			labels.append(label)
	groups = [group_for(l) for l in labels]
	name_of = {l: g[0] for l, g in zip(labels, groups)}
	is_root = any(kind == "mhz" for kind, _ in cols)
	run["groups"], run["is_root"] = groups, is_root
	run["group_data"] = {name: [] for name, _ in groups}
	run["freq_data"] = {name: [] for name, _ in groups}
	for line in lines:
		tok = line.split()
		if len(tok) != len(cols) or not re.fullmatch(r"\d+s", tok[0]):
			continue
		row = {key: NAN for key in ("cpu", "gpu", "cpu_w", "gpu_w", "ane_w", "tmax", "tavg")}
		for (kind, label), x in zip(cols, tok):
			if kind == "t":
				run["t"].append(float(x[:-1]))
			elif kind == "pct":
				run["group_data"][name_of[label]].append(_num(x))
			elif kind == "mhz":
				run["freq_data"][name_of[label]].append(_num(x))
			else:
				row[kind] = _num(x)
		if not is_root:
			for name, _ in groups:
				run["freq_data"][name].append(NAN)
		for key, v in row.items():
			run[key].append(v)
	return run


def draw(out, run, hide=()):
	plot_chart(out, run["t"], run["cpu"], run["groups"], run["group_data"], run["freq_data"],
			   run["gpu"], run["cpu_w"], run["gpu_w"], run["tmax"], run["tavg"], run["is_root"], hide,
			   ane_w=run.get("ane_w"))


def give_to_user(path):
	user = os.environ.get("SUDO_USER")
	if user:  # don't leave root-owned files on the user's Desktop
		pw = pwd.getpwnam(user)
		os.chown(path, pw.pw_uid, pw.pw_gid)


CORES_ONLY = {"all", "gpu"}


def draw_charts(out, run, hide):
	"""
	With --hide: one graph, with those lines left out.
	Without: the full graph plus a cores-only one (<name>_cores.png).
	Returns the paths written.
	"""
	if hide:
		draw(out, run, hide)
		return [out]
	draw(out, run, ())
	if not run["groups"]:
		return [out]
	root, ext = os.path.splitext(out)
	cores_out = f"{root}_cores{ext or '.png'}"
	draw(cores_out, run, CORES_ONLY)
	return [out, cores_out]


def parse_args():
	ap = argparse.ArgumentParser(description="Record CPU/GPU load, clocks, power and chip temperature, then graph it.")
	ap.add_argument("--from", dest="source", metavar="FILE",
					help="redraw a graph from a saved CSV or saved console output instead of recording")
	ap.add_argument("--hide", default="", metavar="LIST",
					help="make one graph, leaving these out of the Utilization panel: all, gpu, super, performance")
	ap.add_argument("--out", metavar="PNG",
					help="where to save the graph (the cores-only graph gets _cores added to the name)")
	ap.add_argument("--seconds", type=int, metavar="N",
					help=f"stop recording after N seconds and save automatically (minimum {2 * INTERVAL})")
	args = ap.parse_args()
	if args.seconds is not None:
		if args.source is not None:
			ap.error("--seconds applies to live recording only")
		if args.seconds < 2 * INTERVAL:
			ap.error(f"--seconds must be at least {2 * INTERVAL} to collect two samples")
	return args


def main():
	global running
	args = parse_args()
	hide = {h.strip().lower() for h in args.hide.split(",") if h.strip()}

	if args.source:
		run = load_run(args.source)
		if len(run["t"]) < 2:
			sys.exit(f"[!] Found only {len(run['t'])} samples in {args.source}.")
		out = args.out or os.path.splitext(os.path.expanduser(args.source))[0] + "_chart.png"
		print(f"[#] {len(run['t'])} samples ({_fmt_elapsed(run['t'][-1])})")
		for path in draw_charts(out, run, hide):
			print(f"[#] Saved graph to {path}")
		return

	apple_silicon = platform.system() == "Darwin" and platform.machine() == "arm64"
	sensor = None
	if apple_silicon:
		try:
			sensor = SocTemperature()
		except (AttributeError, OSError) as error:
			print(f"[!] Temperature sensor initialization failed: {error}")
		if sensor is None or not sensor.sensors:
			print("[i] Die temperature sensors unavailable. Continuing with the available metrics.")
	else:
		print("[i] CPU-only mode. Hardware sensors require native Apple Silicon macOS.")

	groups = core_groups() if apple_silicon else []
	if groups:
		print("[i] Core types: " + ", ".join(f"{n} = cpu{c[0]}-{c[-1]} ({len(c)})" for n, c in groups))
	cpu_type = {i: name for name, cores in groups for i in cores}

	is_root = apple_silicon and os.geteuid() == 0
	if is_root:
		threading.Thread(target=powermetrics_reader, args=(cpu_type,), daemon=True).start()
	elif apple_silicon:
		print("[i] Run with sudo to include GPU %, clock speeds and power where available.")

	time_axis, cpu_data, gpu_data, temp_max, temp_avg = [], [], [], [], []
	cpu_w_data, gpu_w_data, ane_w_data = [], [], []
	group_data = {name: [] for name, _ in groups}
	freq_data = {name: [] for name, _ in groups}

	psutil.cpu_percent(interval=None, percpu=True)  # prime: first call always returns 0.0
	start = time.monotonic()
	deadline = start + args.seconds if args.seconds is not None else None

	# Columns per core type, side by side: Super% Super MHz Perfo% Perfo MHz ...
	hdr = f"{'Time':<7}{'CPU%':<7}"
	for name, _ in groups:
		hdr += f"{name[:5] + '%':<8}"
		if is_root:
			hdr += f"{name[:5] + ' MHz':<11}"
	if is_root:
		hdr += f"{'CPU W':<8}{'GPU%':<7}{'GPU W':<8}{'ANE W':<8}"
	hdr += f"{'Temp max':<10}{'Temp avg':<9}"
	if args.seconds is not None:
		print(f"[+] Monitoring for {args.seconds} seconds. Press Ctrl-C to stop early.\n")
	else:
		print("[+] Monitoring. Press CTRL+C to stop and generate the graph.\n")
	print(hdr)
	print("-" * len(hdr))

	def fmt(v, spec, width):
		return f"{(format(v, spec) if v is not None else '-'):<{width}}"

	try:
		while True:
			remaining = deadline - time.monotonic() if deadline is not None else INTERVAL
			if remaining <= 0:
				break
			time.sleep(min(INTERVAL, remaining))
			now = time.monotonic()
			elapsed = round(now - start)
			per_core = psutil.cpu_percent(interval=None, percpu=True)
			cpu = sum(per_core) / len(per_core)
			group_vals = [sum(per_core[i] for i in cores) / len(cores) for _, cores in groups]
			tmax, tavg = sensor.read() if sensor is not None else (None, None)
			gpu = pm["gpu"] if is_root else None
			freqs = [pm["freq"].get(name) if is_root else None for name, _ in groups]
			cpu_w = pm["cpu_w"] if is_root else None
			gpu_w = pm["gpu_w"] if is_root else None
			ane_w = pm["ane_w"] if is_root else None

			time_axis.append(elapsed)
			cpu_data.append(cpu)
			for (name, _), v, f in zip(groups, group_vals, freqs):
				group_data[name].append(v)
				freq_data[name].append(f if f is not None else NAN)
			gpu_data.append(gpu if gpu is not None else NAN)
			cpu_w_data.append(cpu_w if cpu_w is not None else NAN)
			gpu_w_data.append(gpu_w if gpu_w is not None else NAN)
			ane_w_data.append(ane_w if ane_w is not None else NAN)
			temp_max.append(tmax if tmax is not None else NAN)
			temp_avg.append(tavg if tavg is not None else NAN)

			row = f"{str(elapsed) + 's':<7}{cpu:<7.1f}"
			for v, f in zip(group_vals, freqs):
				row += f"{v:<8.1f}"
				if is_root:
					row += fmt(f, ".0f", 11)
			if is_root:
				row += fmt(cpu_w, ".1f", 8) + fmt(gpu, ".1f", 7) + fmt(gpu_w, ".1f", 8) + fmt(ane_w, ".1f", 8)
			row += fmt(tmax, ".1f", 10) + fmt(tavg, ".1f", 9)
			print(row)
			if deadline is not None and now >= deadline:
				break
		print("\n[-] Time limit reached. Building graphs...")
	except KeyboardInterrupt:
		print("\n[-] Stopped. Building graph...")
	finally:
		running = False

	if len(time_axis) < 2:
		print("[-] Not enough samples for a graph. Let it run a bit longer.")
		return

	run = {"t": time_axis, "cpu": cpu_data, "groups": groups, "group_data": group_data,
		   "freq_data": freq_data, "gpu": gpu_data, "cpu_w": cpu_w_data, "gpu_w": gpu_w_data,
		   "ane_w": ane_w_data, "tmax": temp_max, "tavg": temp_avg, "is_root": is_root}
	data_out = desktop_path("mac_monitor_data.csv")
	save_csv(data_out, run)  # saved first, so the numbers survive even if drawing fails
	give_to_user(data_out)
	print(f"[#] Saved data to {data_out}")
	out = args.out or desktop_path("mac_monitor_chart.png")
	for path in draw_charts(out, run, hide):
		give_to_user(path)
		print(f"[#] Saved graph to {path}")


if __name__ == "__main__":
	main()
