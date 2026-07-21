#!/usr/bin/env python3
"""
Vigil — Part 2: motion_alarm.py

A menu-bar (rumps) tripwire. While ARMED, it watches the Mac's built-in
accelerometer; if the machine is moved past a tunable threshold it blasts a
looping siren until you disarm. It auto-disarms when you unlock the screen, and
has a hard cap on how long the siren can run (safety valve).

THE SENSOR (undocumented, Apple-silicon-only)
---------------------------------------------
M1 *Pro/Max/Ultra* (and later) expose a Bosch IMU (accel + gyro) over IOKit HID
as `AppleSPUHIDDevice` — the same sensor macOS uses for lid-angle etc. The base
M1 does NOT have it. We read it through the `macimu` package
(https://pypi.org/project/macimu/), a thin ctypes/IOKit wrapper. Before doing
anything this script runs the go/no-go check you asked for:

    from macimu import IMU;  IMU.available()   # must be True

On THIS machine (M1 Pro, MacBookPro18,3) that returned True during setup.

FRAGILITY / THINGS THAT CAN BREAK (you asked to know)
-----------------------------------------------------
  * 100% private-API dependent. macimu matches undocumented IOKit classes
    (`AppleSPUHIDDevice`, `AppleSPUHIDDriver`), the Apple vendor HID usage page
    0xFF00, and assumes a fixed 22-byte report layout (Q16 xyz at offset 6). A
    macOS point update that renumbers usages or changes the report descriptor
    would make reads silently return nothing (no samples => no alarm). There is
    no fallback and we will NOT fake one — if the sensor stops delivering data
    this app is inert, by design.
  * Needs ROOT to open the HID device (`IMU.start()` refuses without it).
    `IMU.available()` does NOT need root.
  * Running a GUI (menu bar) app as root is the awkward part — see README
    "Running Part 2 as root". Short version: launch it with `sudo` from a
    Terminal inside your normal login session (NOT a LaunchDaemon), or the menu
    bar item won't appear.

USAGE
-----
    python3 motion_alarm.py --check           # run IMU.available() and exit
    sudo -E python3 motion_alarm.py           # arm/disarm from the menu bar
    sudo -E python3 motion_alarm.py --arm-on-lock   # also auto-arm when screen locks

Tunables: --threshold (g), --max-alarm (s), --sound PATH. Config persists to
~/.config/vigil/motion.json (stored under your real home even under sudo).
"""

from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import pwd
import signal
import struct
import subprocess
import sys
import threading
import time
import wave


# ---- config / paths ---------------------------------------------------------

APP_DIR_NAME = "vigil"
CONFIG_BASENAME = "motion.json"

DEFAULTS = {
    "threshold_g": 0.06,     # ||accel - baseline|| that counts as "moved" (g)
    "max_alarm_s": 90.0,     # hard cap on a single siren episode (safety valve)
    "cooldown_s": 8.0,       # quiet period after a capped alarm before re-trigger
    "arm_grace_s": 4.0,      # ignore motion for this long after arming (setting it down)
    "sample_rate": 100,      # Hz requested from the IMU (native ~800, decimated)
    "baseline_tau_s": 1.0,   # time constant of the slow gravity-tracking baseline
    "silent_mode": False,    # test mode: show an on-screen alert instead of a siren
}


def resolve_home() -> str:
    """Real user's home even under sudo (so config isn't stranded in /var/root)."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            return pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(resolve_home(), ".config")
    return os.path.join(base, APP_DIR_NAME, CONFIG_BASENAME)


_NUM_BOUNDS = {
    "threshold_g": (0.001, 10), "max_alarm_s": (0.001, 86400),
    "cooldown_s": (0, 3600), "arm_grace_s": (0, 3600),
    "sample_rate": (1, 800), "baseline_tau_s": (0.05, 3600),
}


def _sanitize(cfg: dict) -> dict:
    """Coerce corrupt/hand-edited numbers to sane values so a bad config can't
    crash menu construction or the sensor-thread comparison."""
    for key, (lo, hi) in _NUM_BOUNDS.items():
        v = cfg.get(key)
        try:
            v = float(v)
            if not math.isfinite(v):
                raise ValueError
        except (TypeError, ValueError):
            v = float(DEFAULTS[key])
        v = min(hi, max(lo, v))
        cfg[key] = int(round(v)) if isinstance(DEFAULTS[key], int) else v
    cfg["silent_mode"] = bool(cfg.get("silent_mode"))
    return cfg


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(config_path()) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")
        cfg.update(data)
    except FileNotFoundError:
        pass
    except (ValueError, TypeError, OSError) as exc:  # ValueError covers JSONDecodeError
        print(f"warning: could not read config, using defaults: {exc}", file=sys.stderr)
    return _sanitize(cfg)


def _sudo_ids() -> tuple[int, int] | None:
    """(uid, gid) of the real user under sudo, else None."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:
        return int(uid), int(gid)
    return None


def ensure_config_dir() -> str:
    """Create ~/.config/vigil owner-only and owned by the real user (not root)."""
    d = os.path.dirname(config_path())
    os.makedirs(d, mode=0o700, exist_ok=True)
    ids = _sudo_ids()
    if ids:
        try:
            # chown the dir fd (not the path) to avoid a symlink swap under us
            dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fchown(dfd, *ids)
                os.fchmod(dfd, 0o700)
            finally:
                os.close(dfd)
        except OSError:
            pass
    return d


def _write_file_safely(path: str, data: bytes, mode: int = 0o600) -> None:
    """Write bytes to a user-space path while running as root, safely.

    O_NOFOLLOW refuses to open a symlink planted at `path` (blocks a root
    overwrite of an arbitrary target); we fchown the fd — never a path — so the
    target can't be swapped between write and chown. mode 0o600 keeps the
    de-anonymizing config out of other local accounts' reach.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        os.write(fd, data)
        os.fchmod(fd, mode)              # enforce mode even if the file pre-existed
        ids = _sudo_ids()
        if ids:
            os.fchown(fd, *ids)
    finally:
        os.close(fd)


def save_config(cfg: dict) -> None:
    try:
        ensure_config_dir()
        _write_file_safely(config_path(),
                           json.dumps(cfg, indent=2).encode("utf-8"))
    except OSError as exc:
        print(f"warning: could not save config: {exc}", file=sys.stderr)


# ---- siren synthesis (stdlib only) -----------------------------------------

def _siren_is_valid(path: str) -> bool:
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() > 0
    except (wave.Error, EOFError, OSError):
        return False


def ensure_siren(path: str) -> str:
    """Create a loud two-tone 'nee-naw' siren WAV, regenerating if missing/corrupt.

    Writes to a temp file then os.replace()s it into place so an interrupted run
    can never leave a 0-byte siren that afplay would fast-fail on.
    """
    if os.path.exists(path) and _siren_is_valid(path):
        return path
    ensure_config_dir()
    rate = 44100
    seg = 0.35                      # seconds per tone
    tones = (740.0, 988.0)          # F#5 / B5 — piercing
    amp = int(0.92 * 32767)
    frames = bytearray()
    for _cycle in range(3):         # ~2.1s clip; afplay loops it
        for freq in tones:
            n = int(rate * seg)
            for i in range(n):
                # tiny linear fade at segment edges to soften clicks
                env = min(1.0, i / 400.0, (n - i) / 400.0)
                s = int(amp * env * math.sin(2 * math.pi * freq * (i / rate)))
                frames += struct.pack("<h", s)

    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))

    tmp = path + ".tmp"
    _write_file_safely(tmp, buf.getvalue(), mode=0o644)
    os.replace(tmp, path)           # atomic; replaces the name, never follows a symlink
    return path


# ---- system volume helpers --------------------------------------------------

def _osa(script: str) -> str | None:
    try:
        out = subprocess.run(["/usr/bin/osascript", "-e", script],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return None


def get_output_volume() -> int | None:
    val = _osa("output volume of (get volume settings)")
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def set_output_volume(vol: int, unmute: bool = True) -> None:
    _osa(f"set volume output volume {int(vol)}")
    if unmute:
        _osa("set volume without output muted")


def get_output_muted() -> bool | None:
    val = _osa("output muted of (get volume settings)")
    if val is None:
        return None
    return val.strip().lower() == "true"


# ---- alarm player -----------------------------------------------------------

class AlarmPlayer:
    """Loops a siren via afplay at max system volume; restores volume on stop."""

    def __init__(self, siren_path: str):
        self._siren = siren_path
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._prev_volume: int | None = None
        self._prev_muted: bool | None = None
        self.active = False
        # restore audio even on Ctrl-C / normal exit paths that never hit stop()
        atexit.register(self._restore_audio)

    def start(self):
        with self._lock:
            if self.active:
                return
            self.active = True
            self._stop.clear()
            # Capture prior state; if the read fails, fall back to a sane level so
            # the forced 100 is ALWAYS brought back down later (never stranded).
            prev = get_output_volume()
            self._prev_volume = prev if prev is not None else 50
            self._prev_muted = get_output_muted()
            set_output_volume(100, unmute=True)
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self):
        fast_fail = 0
        while not self._stop.is_set():
            # Re-assert max volume every cycle so a thief can't silence the siren
            # with the lock-screen volume/mute keys.
            set_output_volume(100, unmute=True)
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(
                    ["/usr/bin/afplay", "-v", "1.0", self._siren])
            except Exception as exc:
                print(f"alarm: afplay could not launch: {exc}", file=sys.stderr)
                break
            while self._proc.poll() is None:
                if self._stop.wait(0.1):
                    self._proc.terminate()
                    break
            if self._stop.is_set():
                break
            # afplay exiting near-instantly means a broken siren file — back off
            # instead of spawning processes in a tight loop.
            if time.monotonic() - started < 0.5:
                fast_fail += 1
                if fast_fail >= 5:
                    print(f"alarm: afplay keeps exiting immediately (bad siren "
                          f"file {self._siren!r}?) — giving up", file=sys.stderr)
                    break
                self._stop.wait(0.5)
            else:
                fast_fail = 0

    def stop(self):
        with self._lock:
            if not self.active:
                return
            self._stop.set()
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
            if self._thread:
                self._thread.join(timeout=2)
            self._restore_audio()
            self.active = False

    def _restore_audio(self):
        """Restore pre-alarm volume + mute exactly once. Safe from atexit/stop()."""
        if self._prev_volume is not None:
            set_output_volume(self._prev_volume, unmute=False)
            if self._prev_muted:
                _osa("set volume with output muted")
        self._prev_volume = None
        self._prev_muted = None


# ---- accelerometer sensor thread -------------------------------------------

class MotionSensor(threading.Thread):
    """Reads accel, tracks a slow baseline, exposes the current disturbance.

    All comms with the GUI thread are via plain attributes / an Event — the
    sensor thread never touches the UI.
    """

    def __init__(self, imu, cfg: dict):
        super().__init__(daemon=True)
        self._imu = imu
        self._cfg = cfg
        self._stop = threading.Event()
        self._reset_baseline = threading.Event()

        # published to the main thread:
        self.armed = False
        self.arm_grace_until = 0.0
        self.latest_disturbance = 0.0
        self.trigger_value = 0.0
        self.triggered = threading.Event()     # set when motion crosses threshold
        self.sample_starved = False            # True if no samples are arriving
        self.error: str | None = None

    def arm(self):
        self.arm_grace_until = time.monotonic() + self._cfg["arm_grace_s"]
        self._reset_baseline.set()
        self.triggered.clear()
        self.armed = True

    def disarm(self):
        self.armed = False
        self.triggered.clear()

    def stop(self):
        self._stop.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=2)

    def run(self):
        rate = self._cfg["sample_rate"]
        # EMA smoothing factor from time constant: alpha = dt/tau, dt=1/rate
        alpha = min(1.0, (1.0 / rate) / max(self._cfg["baseline_tau_s"], 1e-3))
        baseline = None
        last_data = time.monotonic()
        consec_errors = 0
        while not self._stop.is_set():
            # A read raise (transient IOKit hiccup, or a macOS update that breaks
            # the private HID layout) must NOT silently kill the tripwire. Catch it
            # per-read: recover if transient, give up + stay surfaced if persistent.
            try:
                samples = self._imu.read_accel()
            except Exception as exc:
                consec_errors += 1
                self.error = str(exc)
                self.sample_starved = True
                if consec_errors >= 40:      # ~persistent: stop; UI shows SENSOR FAILED
                    return
                time.sleep(0.05)
                continue
            consec_errors = 0
            self.error = None
            now = time.monotonic()
            if not samples:
                if now - last_data > 2.0:
                    self.sample_starved = True
                time.sleep(0.01)
                continue
            last_data = now
            self.sample_starved = False

            if self._reset_baseline.is_set():
                baseline = None
                self._reset_baseline.clear()

            for s in samples:
                v = (s.x, s.y, s.z)
                if baseline is None:
                    baseline = v
                    continue
                baseline = (baseline[0] + alpha * (v[0] - baseline[0]),
                            baseline[1] + alpha * (v[1] - baseline[1]),
                            baseline[2] + alpha * (v[2] - baseline[2]))
                dx, dy, dz = v[0] - baseline[0], v[1] - baseline[1], v[2] - baseline[2]
                dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                self.latest_disturbance = dist
                if (self.armed and now >= self.arm_grace_until
                        and dist >= self._cfg["threshold_g"]):
                    self.trigger_value = dist
                    self.triggered.set()
            time.sleep(0.005)


# ---- menu bar app -----------------------------------------------------------

def screen_is_locked() -> bool:
    """True if the login window/lock screen is up. Uses CGSession dictionary."""
    import Quartz
    d = Quartz.CGSessionCopyCurrentDictionary()
    if not d:
        return False
    return bool(d.get("CGSSessionScreenIsLocked", False))


def has_gui_session() -> bool:
    import Quartz
    return Quartz.CGSessionCopyCurrentDictionary() is not None


def build_app(imu, cfg: dict, arm_on_lock: bool, siren_path: str | None = None):
    import rumps

    if siren_path and os.path.exists(siren_path):
        siren = siren_path
    else:
        siren = ensure_siren(os.path.join(os.path.dirname(config_path()), "siren.wav"))
    sensor = MotionSensor(imu, cfg)
    alarm = AlarmPlayer(siren)

    class VigilMotion(rumps.App):
        def __init__(self):
            super().__init__("🛡︎ Vigil", quit_button=None)
            self.arm_item = rumps.MenuItem("Arm", callback=self.toggle_arm)
            self.status_item = rumps.MenuItem("Status: idle")
            self.motion_item = rumps.MenuItem("motion: --")
            self.thresh_item = rumps.MenuItem(
                f"threshold: {cfg['threshold_g']*1000:.0f} mg")
            self.silent_item = rumps.MenuItem(
                self._silent_label(), callback=self.toggle_silent)
            self.menu = [
                self.arm_item,
                self.status_item,
                self.motion_item,
                None,
                self.thresh_item,
                rumps.MenuItem("More sensitive", callback=self.more_sensitive),
                rumps.MenuItem("Less sensitive", callback=self.less_sensitive),
                rumps.MenuItem("Set max alarm…", callback=self.set_max_alarm),
                rumps.MenuItem("Set arm grace…", callback=self.set_arm_grace),
                None,
                self.silent_item,
                rumps.MenuItem("Test alarm (3s)", callback=self.test_siren),
                rumps.MenuItem("Quit Vigil", callback=self.quit_app),
            ]

            # alarm/session bookkeeping (all touched only on the main thread)
            self.alarm_start = 0.0
            self.cooldown_until = 0.0
            self.silent_active = False           # visual "alarm" engaged (silent mode)
            self.prev_locked = screen_is_locked()

            sensor.start()
            self._timer = rumps.Timer(self.tick, 0.5)
            self._timer.start()

            if sensor.error:
                rumps.alert("Vigil", f"sensor error: {sensor.error}")

        # -- menu callbacks (main thread) --
        def toggle_arm(self, _):
            if sensor.armed:
                self._disarm()
            else:
                self._arm()

        def _arm(self):
            sensor.arm()
            self.arm_item.title = "Disarm"

        def _disarm(self):
            sensor.disarm()
            self.arm_item.title = "Arm"
            self._stop_alarm()

        # -- unified alarm engage/stop (audible OR silent) --
        def _alarm_engaged(self):
            return alarm.active or self.silent_active

        def _engage_alarm(self, now):
            self.alarm_start = now
            if cfg.get("silent_mode"):
                self.silent_active = True
                rumps.notification(
                    "Vigil — MOTION DETECTED", "silent mode",
                    f"moved {sensor.trigger_value*1000:.0f} mg "
                    f"(threshold {cfg['threshold_g']*1000:.0f} mg)")
            else:
                alarm.start()

        def _stop_alarm(self):
            if alarm.active:
                alarm.stop()
            self.silent_active = False

        def more_sensitive(self, _):
            cfg["threshold_g"] = max(0.01, round(cfg["threshold_g"] - 0.01, 3))
            self._save_threshold()

        def less_sensitive(self, _):
            cfg["threshold_g"] = round(cfg["threshold_g"] + 0.01, 3)
            self._save_threshold()

        def _save_threshold(self):
            self.thresh_item.title = f"threshold: {cfg['threshold_g']*1000:.0f} mg"
            save_config(cfg)

        def _silent_label(self):
            return f"Silent mode: {'ON' if cfg.get('silent_mode') else 'off'}"

        def toggle_silent(self, _):
            cfg["silent_mode"] = not cfg.get("silent_mode")
            self.silent_item.title = self._silent_label()
            # Honor a mid-alarm flip in BOTH directions:
            if cfg["silent_mode"] and alarm.active:
                alarm.stop()                     # ON: silence a running siren
                self.silent_active = True
            elif not cfg["silent_mode"] and self.silent_active:
                self.silent_active = False       # OFF: escalate a silent alarm to sound
                alarm.start()
            save_config(cfg)

        def _prompt_number(self, key, message):
            w = rumps.Window(message=message, title="Vigil",
                             default_text=str(cfg[key]), ok="Save", cancel="Cancel",
                             dimensions=(200, 22))
            resp = w.run()
            if not resp.clicked:
                return
            try:
                val = float(resp.text.strip())
            except ValueError:
                rumps.alert("Vigil", "Please enter a number.")
                return
            # arm_grace may be 0 (no grace); other durations must be positive. Reject
            # inf/NaN, which would silently disable the safety cap / motion trigger.
            floor = 0.0 if key == "arm_grace_s" else 0.001
            if not math.isfinite(val) or val < floor:
                rumps.alert("Vigil", "Please enter a valid positive number.")
                return
            cfg[key] = val
            save_config(cfg)

        def set_max_alarm(self, _):
            self._prompt_number("max_alarm_s",
                                "Max seconds a single alarm can run (safety cap):")

        def set_arm_grace(self, _):
            self._prompt_number("arm_grace_s",
                                "Seconds to ignore motion right after arming:")

        def test_siren(self, _):
            # ignore while armed/alarming so it can't tangle with a real episode
            if sensor.armed or self._alarm_engaged():
                return
            self._engage_alarm(time.monotonic())
            self._test_until = time.monotonic() + 3.0

        def quit_app(self, _):
            self._disarm()
            sensor.stop()
            alarm.stop()
            rumps.quit_application()

        # -- periodic main-thread poll --
        def tick(self, _):
            now = time.monotonic()

            # end a short test-alarm unconditionally, so a "3s test" can't run to
            # the max_alarm cap if arm-on-lock armed the sensor mid-test
            if getattr(self, "_test_until", 0) and now >= self._test_until:
                self._test_until = 0
                if self._alarm_engaged():
                    self._stop_alarm()

            locked = screen_is_locked()
            # ---- wiring via the OS lock state ----
            if arm_on_lock and locked and not self.prev_locked and not sensor.armed:
                self._arm()
            # auto-disarm when the owner returns and unlocks
            if sensor.armed and self.prev_locked and not locked:
                self._disarm()
            self.prev_locked = locked

            # ---- alarm trigger / cap ----
            if (sensor.armed and sensor.triggered.is_set()
                    and not self._alarm_engaged() and now >= self.cooldown_until):
                sensor.triggered.clear()
                self._engage_alarm(now)
            elif sensor.triggered.is_set() and (self._alarm_engaged()
                                                or now < self.cooldown_until):
                sensor.triggered.clear()  # consume; already alarming or cooling down

            if self._alarm_engaged() and not getattr(self, "_test_until", 0):
                if now - self.alarm_start >= cfg["max_alarm_s"]:
                    self._stop_alarm()
                    self.cooldown_until = now + cfg["cooldown_s"]

            # ---- health / status display ----
            # A dead/erroring sensor thread must never keep showing "armed" — that
            # would be a silent loss of protection. Check liveness FIRST.
            if sensor.error or not sensor.is_alive():
                state = "SENSOR FAILED"
                # Warn once on a truly dead thread (not a recoverable transient
                # error). Use a non-blocking notification, not a modal — a modal in
                # the tick would freeze the tick loop until dismissed.
                if (not sensor.is_alive() and sensor.armed
                        and not getattr(self, "_sensor_fail_alerted", False)):
                    self._sensor_fail_alerted = True
                    rumps.notification("Vigil", "Motion sensor stopped",
                                       f"NOT protected — {sensor.error or 'thread died'}")
            elif sensor.sample_starved and sensor.armed:
                state = "NO SENSOR DATA"
            elif self.silent_active:
                # flash to draw the eye since there's no sound
                state = "🚨 MOVED (silent)" if int(now * 2) % 2 else "⚠︎ MOVED (silent)"
                self._sensor_fail_alerted = False
            elif alarm.active:
                state = "🚨 ALARM"
                self._sensor_fail_alerted = False
            elif sensor.armed:
                state = "🔴 armed"
                self._sensor_fail_alerted = False
            else:
                state = "idle"
            self.title = "🛡︎ " + state
            self.status_item.title = f"Status: {state}"
            self.motion_item.title = f"motion: {sensor.latest_disturbance*1000:5.0f} mg"

    app = VigilMotion()
    app._sensor = sensor   # exposed so main() can clean up on any exit path
    app._alarm = alarm
    return app


# ---- startup checks ---------------------------------------------------------

def run_check() -> int:
    try:
        from macimu import IMU
    except ImportError:
        print("macimu is not installed.  pip install macimu", file=sys.stderr)
        return 2
    ok = IMU.available()
    print(f"IMU.available() -> {ok}")
    if ok:
        try:
            print("device_info:", IMU.device_info())
        except Exception as exc:
            print(f"(device_info failed: {exc})", file=sys.stderr)
        return 0
    print("This machine does not expose the SPU IMU. STOP — Part 2 cannot run "
          "here (no fake fallback, as requested).", file=sys.stderr)
    return 2


def main(argv=None):
    p = argparse.ArgumentParser(description="Vigil motion alarm (menu bar).")
    p.add_argument("--check", action="store_true",
                   help="run IMU.available() and exit (no root needed)")
    p.add_argument("--arm-on-lock", action="store_true",
                   help="auto-arm when the screen locks (wiring with Part 1)")
    p.add_argument("--threshold", type=float, default=None,
                   help="motion threshold in g (default %(default)s)")
    p.add_argument("--max-alarm", type=float, default=None,
                   help="max siren seconds before the safety cap (default 90)")
    p.add_argument("--sample-rate", type=int, default=None)
    p.add_argument("--sound", default=None, help="custom siren WAV path")
    p.add_argument("--silent", action="store_true",
                   help="silent mode: show an on-screen alert instead of a siren")
    args = p.parse_args(argv)

    if args.check:
        return run_check()

    # go/no-go, exactly as asked
    try:
        from macimu import IMU
    except ImportError:
        sys.exit("macimu is not installed.  pip install macimu")

    if not IMU.available():
        sys.exit("IMU.available() is False — the SPU accelerometer is not present "
                 "on this machine. STOP (no fake fallback).")

    if os.geteuid() != 0:
        sys.exit("motion_alarm needs root to open the HID device.\n"
                 "  sudo -E python3 %s\n"
                 "(-E keeps your env so config lands in your home. See README for "
                 "why the menu bar needs a real login session.)"
                 % os.path.basename(sys.argv[0]))

    if not has_gui_session():
        print("WARNING: no GUI (Aqua) session detected — the menu bar item may not "
              "appear. Launch this with `sudo` from a Terminal inside your normal "
              "login session, not from a LaunchDaemon/ssh.", file=sys.stderr)

    cfg = load_config()
    if args.threshold is not None:
        cfg["threshold_g"] = args.threshold
    if args.max_alarm is not None:
        cfg["max_alarm_s"] = args.max_alarm
    if args.sample_rate is not None:
        cfg["sample_rate"] = args.sample_rate
    if args.silent:
        cfg["silent_mode"] = True

    imu = IMU(accel=True, gyro=False, sample_rate=cfg["sample_rate"])
    imu.start()

    siren_override = os.path.expanduser(args.sound) if args.sound else None

    # SIGTERM (logout/`sudo kill`): stop the Cocoa run loop cleanly so app.run()
    # returns and the finally-cleanup + atexit volume-restore run. sys.exit()
    # inside a timer callback would NOT unwind NSApp.run().
    from PyObjCTools import AppHelper
    signal.signal(signal.SIGTERM, lambda *_: AppHelper.stopEventLoop())

    app = None
    try:
        app = build_app(imu, cfg, arm_on_lock=args.arm_on_lock,
                        siren_path=siren_override)
        print("Vigil motion alarm running in the menu bar. Arm from there. "
              "Ctrl-C or Quit to exit.")
        app.run()
    finally:
        # Order matters: stop+join the sensor BEFORE freeing the IMU shm it reads,
        # then stop the alarm (restores volume), then release the sensor.
        if app is not None:
            try:
                app._sensor.stop()
            except Exception:
                pass
            try:
                app._alarm.stop()
            except Exception:
                pass
        imu.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
