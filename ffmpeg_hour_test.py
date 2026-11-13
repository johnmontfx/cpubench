#!/usr/bin/env python3
"""
One-hour FFmpeg CPU encoding benchmark for macOS.

Setup
-----
Install Python 3 and FFmpeg with libx264 support. With Homebrew, run
`brew install python ffmpeg`. Check that both tools are on your PATH with
`python3 --version` and `ffmpeg -version`. Run `ffmpeg -encoders` and
look for libx264 if FFmpeg reports an unknown encoder.

Usage
-----
Save this file as ffmpeg_hour_test.py. In Terminal, change to its directory
and run `python3 ffmpeg_hour_test.py`. No Python packages or sudo are needed.
Press Ctrl+C to stop early. For a quick trial, set RUN_SECONDS to 15.

The test generates a 1920x1080, 30 fps pattern and encodes it with software
H.264 using libx264's medium preset and a 6 Mbps target. FFmpeg discards the
encoded stream. The timer measures elapsed wall-clock time, and FFmpeg runs
as fast as it can during that time. This tests one synthetic video encode.

Comparing results
-----------------
Use the same run length and FFmpeg/libx264 builds where possible. Keep each
Mac awake and record its chip, macOS version, power mode and FFmpeg version.
Compare the frame count on the final FFmpeg progress line. For runs of equal
duration, more frames means higher throughput. For a utilization and
temperature chart, run app_monitor_share.py in a separate Terminal window,
start this test, then stop the monitor after FFmpeg exits.
"""

import signal
import subprocess
import sys

RUN_SECONDS = 3600
SHUTDOWN_TIMEOUT = 10

COMMAND = [
    "ffmpeg", "-hide_banner", "-nostdin", "-benchmark", "-stats",
    "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
    "-map", "0:v:0", "-an",
    "-c:v", "libx264", "-preset", "medium",
    "-pix_fmt", "yuv420p", "-profile:v", "high",
    "-b:v", "6000k", "-maxrate", "6600k", "-bufsize", "9000k",
    "-f", "null", "-",
]


def main():
    try:
        # Keep terminal Ctrl+C with Python so it can shut down FFmpeg.
        encoder = subprocess.Popen(COMMAND, start_new_session=True)
    except FileNotFoundError:
        print("FFmpeg was not found. Install it and make sure it is on your PATH.", file=sys.stderr)
        return 1

    try:
        return encoder.wait(timeout=RUN_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"\n{RUN_SECONDS} seconds elapsed. Stopping FFmpeg.", flush=True)
        exit_status = 0
    except KeyboardInterrupt:
        print("\nStopping FFmpeg early.", flush=True)
        exit_status = 130

    # Give FFmpeg time to flush its encoder and print the final frame count.
    encoder.send_signal(signal.SIGINT)
    try:
        encoder.wait(timeout=SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        encoder.kill()
        encoder.wait()
        print("FFmpeg did not finish shutting down in time.", file=sys.stderr)
        return 1
    return exit_status


if __name__ == "__main__":
    sys.exit(main())
