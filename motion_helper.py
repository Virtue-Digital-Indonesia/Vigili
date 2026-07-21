#!/usr/bin/env python3
"""
Vigil motion helper — runs as ROOT, reads the accelerometer, reports to the
(unprivileged) Vigil GUI through two small files. The GUI launches this via a
one-time macOS admin prompt (password or Touch ID), so the motion alarm needs
no Terminal.

Why a helper at all: the SPU accelerometer is root-only, but CoreBluetooth (the
proximity lock) works best as your normal user. So the GUI stays unprivileged
and spins up this headless root helper just for the sensor.

File protocol (both small, in ~/.config/vigil/):
  control  (GUI → helper), JSON: {armed, threshold_g, arm_grace_s, arm_seq, stop}
           The GUI rewrites it ~2×/s; its mtime is the GUI's "still alive" signal.
  data     (helper → GUI), one line: "seq latest_g trigger_seq starved mono"
           World-readable so the unprivileged GUI can read it.

The helper exits when the control file goes stale (GUI gone) or stop is set, so
it never lingers as a rogue root process.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time


def _read_control(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_data(path, seq, latest_g, trig, starved, mono):
    line = f"{seq} {latest_g:.6f} {trig} {starved} {mono:.3f}\n".encode()
    # world-readable (root writes; unprivileged GUI reads); write+rename = atomic
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Vigil root motion helper.")
    ap.add_argument("--control", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--sample-rate", type=int, default=100)
    ap.add_argument("--stale", type=float, default=6.0,
                    help="exit if the GUI's control file is older than this (s)")
    ap.add_argument("--baseline-tau", type=float, default=1.0)
    args = ap.parse_args(argv)

    if os.geteuid() != 0:
        print("motion_helper must run as root", file=sys.stderr)
        return 2
    try:
        from macimu import IMU
    except ImportError:
        print("macimu not installed", file=sys.stderr)
        return 2
    if not IMU.available():
        _write_data(args.data, 0, 0.0, 0, 1, time.monotonic())
        print("IMU not available on this Mac", file=sys.stderr)
        return 2

    imu = IMU(accel=True, gyro=False, sample_rate=args.sample_rate)
    imu.start()
    alpha = min(1.0, (1.0 / args.sample_rate) / max(args.baseline_tau, 1e-3))

    baseline = None
    seq = 0
    trig = 0
    latest = 0.0
    last_arm_seq = None
    arm_until = 0.0
    last_data = time.monotonic()
    started = time.monotonic()
    seen_fresh = False       # have we ever seen the GUI keep the control file fresh?
    try:
        while True:
            now = time.monotonic()
            ctrl = _read_control(args.control)
            if ctrl.get("stop"):
                break
            # Exit when the GUI goes away — but tolerate a slow password entry:
            # only treat staleness as "GUI gone" once we've seen it fresh at least
            # once; if it never connects within 30s, give up (no rogue root proc).
            try:
                age = time.time() - os.path.getmtime(args.control)
            except OSError:
                age = 1e9
            if age <= args.stale:
                seen_fresh = True
            if seen_fresh and age > args.stale:
                break
            if not seen_fresh and (now - started) > 30.0:
                break

            armed = bool(ctrl.get("armed"))
            threshold = float(ctrl.get("threshold_g", 0.06))
            arm_grace = float(ctrl.get("arm_grace_s", 4.0))
            arm_seq = ctrl.get("arm_seq")
            if arm_seq != last_arm_seq:          # (re)armed → settle baseline + grace
                last_arm_seq = arm_seq
                baseline = None
                arm_until = now + arm_grace

            samples = imu.read_accel()
            starved = 0
            if not samples:
                if now - last_data > 2.0:
                    starved = 1
            else:
                last_data = now
                for s in samples:
                    v = (s.x, s.y, s.z)
                    if baseline is None:
                        baseline = v
                        continue
                    baseline = (baseline[0] + alpha * (v[0] - baseline[0]),
                                baseline[1] + alpha * (v[1] - baseline[1]),
                                baseline[2] + alpha * (v[2] - baseline[2]))
                    dx = v[0] - baseline[0]
                    dy = v[1] - baseline[1]
                    dz = v[2] - baseline[2]
                    latest = math.sqrt(dx * dx + dy * dy + dz * dz)
                    if armed and now >= arm_until and latest >= threshold:
                        trig += 1

            seq += 1
            _write_data(args.data, seq, latest, trig, starved, now)
            time.sleep(0.005)
    finally:
        try:
            imu.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
