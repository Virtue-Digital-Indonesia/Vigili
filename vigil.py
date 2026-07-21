#!/usr/bin/env python3
"""
Vigil — combined app: proximity lock + motion alarm in ONE process.

Two front ends over one shared engine (VigilCore):
  * a real **window GUI** (default) — a control panel with buttons, live meters,
    and editable numeric fields for every setting;
  * a **menu-bar** app (`--menubar`) — the same thing folded into the status bar.

It reuses the exact, reviewed engines from proximity_lock.py and motion_alarm.py.

PRIVILEGE MODEL (read this)
--------------------------
  proximity lock → CoreBluetooth (needs the app's Bluetooth TCC permission)
  motion alarm   → the SPU accelerometer HID (needs **root**)

One process can't cleanly have both, so Vigil degrades:

    python3 vigil.py            → proximity only  (motion shows "needs sudo")
    sudo -E python3 vigil.py    → both halves

CAVEAT (untested by the author — no root here): under sudo, CoreBluetooth also
runs as root, and Bluetooth-TCC-under-sudo on macOS 27 is unverified. If the
proximity half shows "Bluetooth unavailable" under sudo, run
`python3 proximity_lock.py --menubar` as your normal user for proximity instead.

Config: ~/.config/vigil/vigil.json (one file, written owner-only, chowned to you).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import signal
import subprocess
import sys
import threading
import time

import objc
from Foundation import NSObject, NSTimer, NSRunLoop, NSMakeRect
from PyObjCTools import AppHelper
try:
    from Foundation import NSRunLoopCommonModes
except ImportError:                              # pragma: no cover
    NSRunLoopCommonModes = "kCFRunLoopCommonModes"

import motion_alarm as motion
from proximity_lock import (ProximityMonitor, lock_screen, _make_central,
                            PRESENT, AWAY)
from motion_alarm import (MotionSensor, AlarmPlayer, ensure_siren,
                          screen_is_locked, has_gui_session)


# ---- config -----------------------------------------------------------------

DEFAULTS = {
    # proximity lock
    "device_identifier": None, "device_name": None,
    "away_rssi": -78, "present_rssi": -58, "grace_seconds": 12.0,
    "absence_timeout": 20.0, "smoothing_window": 8.0, "min_samples": 2,
    # motion alarm
    "threshold_g": 0.06, "max_alarm_s": 90.0, "cooldown_s": 8.0,
    "arm_grace_s": 4.0, "sample_rate": 100, "baseline_tau_s": 1.0,
    "silent_mode": False,
    # combined
    "heartbeat_s": 0.5, "link_lock_to_motion": True,
    "theme": "auto",   # auto | light | dark
}

# numeric bounds — corrupt/hand-edited values are coerced so a bad config can't
# brick launch, freeze the timer, or crash the sensor-thread comparison.
_NUM_BOUNDS = {
    "away_rssi": (-127, 0), "present_rssi": (-127, 0),
    "grace_seconds": (0, 3600), "absence_timeout": (0, 3600),
    "smoothing_window": (0.5, 3600), "min_samples": (1, 100),
    "threshold_g": (0.001, 10), "max_alarm_s": (0.001, 86400),
    "cooldown_s": (0, 3600), "arm_grace_s": (0, 3600),
    "sample_rate": (1, 800), "baseline_tau_s": (0.05, 3600),
    "heartbeat_s": (0.1, 5),
}


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        motion.resolve_home(), ".config")
    return os.path.join(base, "vigil", "vigil.json")


def _sanitize(cfg: dict) -> dict:
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
    for key in ("silent_mode", "link_lock_to_motion"):
        cfg[key] = bool(cfg.get(key))
    if cfg.get("theme") not in ("auto", "light", "dark"):
        cfg["theme"] = "auto"
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
    except (ValueError, TypeError, OSError) as exc:
        print(f"warning: could not read config, using defaults: {exc}",
              file=sys.stderr)
    return _sanitize(cfg)


def save_config(cfg: dict) -> None:
    try:
        motion.ensure_config_dir()
        motion._write_file_safely(config_path(),
                                  json.dumps(cfg, indent=2).encode("utf-8"))
    except OSError as exc:
        print(f"warning: could not save config: {exc}", file=sys.stderr)


def clamp_num(key, raw, cfg):
    """Validate a user-entered value for `key`; return (ok, value_or_None)."""
    lo, hi = _NUM_BOUNDS[key]
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return False, None
    if not math.isfinite(v) or not (lo <= v <= hi):
        return False, None
    return True, (int(round(v)) if isinstance(DEFAULTS[key], int) else v)


def export_config(path: str, cfg: dict) -> None:
    """Write the current settings to a user-chosen file (plain JSON)."""
    with open(os.path.expanduser(path), "w") as fh:
        json.dump(cfg, fh, indent=2)


def import_config(path: str) -> dict:
    """Read + validate + sanitize settings from a file. Returns a full cfg dict."""
    with open(os.path.expanduser(path)) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("that file isn't a Vigil settings object")
    merged = dict(DEFAULTS)
    merged.update(data)
    return _sanitize(merged)


# ---- heartbeat timer (fires even while a menu is open) ----------------------

class _Ticker(NSObject):
    """NSTimer target that fires in NSRunLoopCommonModes (incl. menu tracking)."""

    @objc.python_method
    def configure(self, callback):
        self._callback = callback
        return self

    def fire_(self, _timer):
        try:
            self._callback()
        except Exception:
            import traceback
            traceback.print_exc()


def _rssi_bar(rssi):
    if rssi is None:
        return "········"
    n = max(0, min(8, round(((rssi + 100) / 60.0) * 8)))
    return "▇" * n + "·" * (8 - n)


# ---- engine setup -----------------------------------------------------------

def _setup_engines(cfg, want_motion, motion_reason):
    """Build the proximity + (optional) motion engines. Returns a dict."""
    monitor = ProximityMonitor.new().setup_({
        "cfg": cfg, "monitor_only": False,
        "lock_on_signal_loss": False, "verbose": False,
        "locking_enabled": False,
    })
    monitor.central = _make_central(monitor)

    imu = sensor = alarm = None
    if want_motion:
        try:
            from macimu import IMU
            imu = IMU(accel=True, gyro=False, sample_rate=cfg["sample_rate"])
            imu.start()
            sensor = MotionSensor(imu, cfg)
            siren = ensure_siren(os.path.join(
                os.path.dirname(motion.config_path()), "siren.wav"))
            alarm = AlarmPlayer(siren)
        except Exception as exc:
            if imu is not None:
                try:
                    imu.stop()
                except Exception:
                    pass
            imu = sensor = alarm = None
            want_motion = False
            motion_reason = f"init failed: {exc}"
            print(f"motion init failed — running proximity-only: {exc}",
                  file=sys.stderr)
    return {"monitor": monitor, "imu": imu, "sensor": sensor, "alarm": alarm,
            "want_motion": want_motion, "motion_reason": motion_reason}


# ---- privileged motion helper (GUI stays unprivileged) ----------------------

def helper_paths():
    d = os.path.dirname(config_path())
    return os.path.join(d, "motion.ctrl"), os.path.join(d, "motion.dat")


def launch_motion_helper(control, data):
    """Prompt for admin (password / Touch ID) and start motion_helper.py as root,
    detached. Returns (ok, message). The heavy lifting is the one osascript line."""
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "motion_helper.py")
    py = sys.executable
    # start a fresh control file so the helper doesn't immediately see it as stale
    try:
        motion.ensure_config_dir()
        with open(control, "w") as fh:
            json.dump({"armed": False, "threshold_g": 0.06, "arm_grace_s": 4.0,
                       "arm_seq": 0, "stop": False}, fh)
    except OSError as exc:
        return False, f"could not prepare control file: {exc}"
    inner = (f"nohup {shlex.quote(py)} {shlex.quote(helper)} "
             f"--control {shlex.quote(control)} --data {shlex.quote(data)} "
             f">/tmp/vigil_helper.log 2>&1 &")
    # embed inner into an AppleScript double-quoted string
    osa = ('do shell script "' + inner.replace('\\', '\\\\').replace('"', '\\"')
           + '" with administrator privileges')
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", osa],
                           capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return False, f"could not run the password prompt: {exc}"
    if r.returncode != 0:
        msg = (r.stderr or "").strip()
        if "User canceled" in msg or "-128" in msg:
            return False, "cancelled"
        return False, msg or "authorization failed"
    return True, "started"


class RemoteMotionSensor(threading.Thread):
    """Drop-in for MotionSensor that sources data from the root helper's files.

    Same interface VigilCore uses: arm/disarm/stop, armed, latest_disturbance,
    trigger_value, triggered, sample_starved, error, is_alive, is_dead.
    """

    def __init__(self, cfg, control_path, data_path):
        super().__init__(daemon=True)
        self._cfg = cfg
        self._control = control_path
        self._data = data_path
        self._stop = threading.Event()
        self.armed = False
        self.latest_disturbance = 0.0
        self.trigger_value = 0.0
        self.triggered = threading.Event()
        self.sample_starved = False
        self.error = None
        self._dead = False
        self._arm_seq = 0
        self._last_trig = None
        self._last_seq = None
        self._last_change = time.monotonic()

    def _write_control(self):
        payload = {"armed": self.armed, "threshold_g": self._cfg["threshold_g"],
                   "arm_grace_s": self._cfg["arm_grace_s"], "arm_seq": self._arm_seq,
                   "stop": self._stop.is_set()}
        try:
            tmp = self._control + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self._control)   # refreshes mtime = keepalive to helper
        except OSError as exc:
            self.error = f"control write failed: {exc}"

    def arm(self):
        self._arm_seq += 1
        self.triggered.clear()
        self.armed = True
        self._write_control()

    def disarm(self):
        self.armed = False
        self.triggered.clear()
        self._write_control()

    def stop(self):
        self._stop.set()
        self._write_control()                # ask the root helper to exit
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=2)

    def is_dead(self):
        return self._dead

    def run(self):
        self._write_control()
        while not self._stop.is_set():
            self._write_control()            # keepalive
            seq = latest = trig = starved = None
            try:
                with open(self._data) as fh:
                    parts = fh.read().split()
                seq = int(parts[0]); latest = float(parts[1])
                trig = int(parts[2]); starved = int(parts[3])
            except (OSError, ValueError, IndexError):
                pass
            now = time.monotonic()
            if seq is not None:
                self.latest_disturbance = latest
                self.sample_starved = bool(starved)
                if seq != self._last_seq:
                    self._last_seq = seq
                    self._last_change = now
                    self._dead = False
                    self.error = None
                if (self._last_trig is not None and trig > self._last_trig
                        and self.armed):
                    self.trigger_value = latest
                    self.triggered.set()
                self._last_trig = trig
            if now - self._last_change > 3.0:    # helper stopped writing
                self._dead = True
                self.error = "motion helper not responding"
            self._stop.wait(0.4)


# ---- shared logic (UI-agnostic) --------------------------------------------

class VigilCore:
    """All arming / alarm / lock-wiring logic. Front ends only render + call in."""

    def __init__(self, cfg, engines, notify=None):
        self.cfg = cfg
        self.monitor = engines["monitor"]
        self.sensor = engines["sensor"]
        self.alarm = engines["alarm"]
        self.imu = engines["imu"]
        self.want_motion = engines["want_motion"]
        self.motion_reason = engines["motion_reason"]
        self.notify = notify or (lambda *a: None)

        self.alarm_start = 0.0
        self.cooldown_until = 0.0
        self.silent_active = False
        self.motion_peak = 0.0
        self.prev_locked = screen_is_locked()
        self._sensor_fail_alerted = False
        self._test_until = 0.0
        self._torn_down = False
        if self.sensor:
            self.sensor.start()

    def enable_remote_motion(self):
        """Wire up a RemoteMotionSensor + alarm after the root helper is running.
        Lets the (unprivileged) app gain the motion alarm without a restart."""
        if self.sensor is not None:
            return
        control, data = helper_paths()
        self.sensor = RemoteMotionSensor(self.cfg, control, data)
        siren = ensure_siren(os.path.join(
            os.path.dirname(motion.config_path()), "siren.wav"))
        self.alarm = AlarmPlayer(siren)
        self.want_motion = True
        self.motion_reason = ""
        self.sensor.start()

    # -- proximity --
    def has_device(self):
        return bool(self.cfg.get("device_identifier") or self.cfg.get("device_name"))

    def proximity_armed(self):
        return self.monitor.locking_enabled

    def arm_proximity(self):
        if not self.has_device():
            return False
        self.monitor.reset_warmup()
        self.monitor.locking_enabled = True
        return True

    def disarm_proximity(self):
        self.monitor.locking_enabled = False

    def pick_device(self, uid, name):
        self.cfg["device_identifier"] = uid
        self.cfg["device_name"] = name
        save_config(self.cfg)
        self.monitor.samples.clear()
        self.monitor.last_seen = time.monotonic()
        self.monitor.reset_warmup()

    def resolvable_devices(self):
        return sorted(self.monitor.seen_resolvable.items(),
                      key=lambda kv: kv[1]["rssi"], reverse=True)

    # -- motion --
    def motion_armed(self):
        return bool(self.sensor and self.sensor.armed)

    def arm_motion(self):
        if self.sensor:
            self.sensor.arm()

    def disarm_motion(self):
        if self.sensor:
            self.sensor.disarm()
        self._stop_alarm()

    def test_alarm(self):
        if not self.sensor or self.sensor.armed or self._alarm_engaged():
            return
        self._engage_alarm(time.monotonic())
        self._test_until = time.monotonic() + 3.0

    def toggle_silent(self):
        self.cfg["silent_mode"] = not self.cfg.get("silent_mode")
        if self.alarm and self.cfg["silent_mode"] and self.alarm.active:
            self.alarm.stop()
            self.silent_active = True
        elif self.alarm and not self.cfg["silent_mode"] and self.silent_active:
            self.silent_active = False
            self.alarm.start()
        save_config(self.cfg)

    def lock_now(self):
        return lock_screen()

    def set_value(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)

    # -- alarm engine --
    def _alarm_engaged(self):
        return bool((self.alarm and self.alarm.active) or self.silent_active)

    def _engage_alarm(self, now):
        self.alarm_start = now
        if self.cfg.get("silent_mode"):
            self.silent_active = True
            self.notify("Vigil — MOTION DETECTED", "silent mode",
                        f"moved {self.sensor.trigger_value*1000:.0f} mg "
                        f"(threshold {self.cfg['threshold_g']*1000:.0f} mg)")
        elif self.alarm:
            self.alarm.start()

    def _stop_alarm(self):
        if self.alarm and self.alarm.active:
            self.alarm.stop()
        self.silent_active = False

    # -- the heartbeat step (call every tick) --
    def heartbeat_step(self):
        now = time.monotonic()
        self.monitor.evaluate_(None)

        if self._test_until and now >= self._test_until:
            self._test_until = 0
            if self._alarm_engaged():
                self._stop_alarm()

        locked = screen_is_locked()
        if self.sensor:
            if (self.cfg.get("link_lock_to_motion") and locked
                    and not self.prev_locked and not self.sensor.armed):
                self.arm_motion()
            if self.sensor.armed and self.prev_locked and not locked:
                self.disarm_motion()
        self.prev_locked = locked

        if self.sensor:
            if (self.sensor.armed and self.sensor.triggered.is_set()
                    and not self._alarm_engaged() and now >= self.cooldown_until):
                self.sensor.triggered.clear()
                self._engage_alarm(now)
            elif self.sensor.triggered.is_set() and (self._alarm_engaged()
                                                     or now < self.cooldown_until):
                self.sensor.triggered.clear()
            if self._alarm_engaged() and not self._test_until:
                if now - self.alarm_start >= self.cfg["max_alarm_s"]:
                    self._stop_alarm()
                    self.cooldown_until = now + self.cfg["cooldown_s"]

            self.motion_peak = max(self.motion_peak * 0.9,
                                   self.sensor.latest_disturbance)
            if (self.sensor.is_dead() and self.sensor.armed
                    and not self._sensor_fail_alerted):
                self._sensor_fail_alerted = True
                self.notify("Vigil", "Motion sensor stopped",
                            "NOT protected — motion detection failed.")
            elif not self.sensor.is_dead():
                self._sensor_fail_alerted = False
        return now

    # -- display state --
    def proximity_view(self):
        m = self.monitor
        if not m.bt_ready:
            return "⚠︎ Bluetooth unavailable", m.smoothed
        if not m.locking_enabled:
            return "disarmed (monitoring)", m.smoothed
        if m.state == AWAY:
            return "🔒 away — locked", m.smoothed
        if not m.fresh:
            return "🔒 no signal", m.smoothed
        if time.monotonic() < m.warmup_until:
            return "🟢 armed (settling…)", m.smoothed
        return "🟢 present — armed", m.smoothed

    def motion_view(self):
        s = self.sensor
        if not s:
            return self.motion_reason or "unavailable", 0.0
        mg = s.latest_disturbance * 1000
        if s.is_dead():
            return "SENSOR FAILED", mg
        if s.sample_starved and s.armed:
            return "NO SENSOR DATA", mg
        if self.silent_active:
            return "🚨 MOVED (silent)", mg
        if self.alarm and self.alarm.active:
            return "🚨 ALARM", mg
        if s.armed:
            return "🔴 armed", mg
        return "disarmed", mg

    def teardown(self):
        if self._torn_down:
            return
        self._torn_down = True
        self.monitor.stop()
        if self.sensor:
            self.sensor.stop()
        if self.alarm:
            self.alarm.stop()
        if self.imu:
            self.imu.stop()


# ---- window GUI front end ---------------------------------------------------

def run_window(cfg, want_motion, motion_reason):
    """Carbon-styled UI rendered in a WKWebView; logic stays in VigilCore."""
    from AppKit import (NSApplication, NSWindow, NSImage, NSMenu, NSMenuItem,
                        NSColor, NSSavePanel, NSOpenPanel,
                        NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
                        NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                        NSBackingStoreBuffered, NSApplicationActivationPolicyRegular,
                        NSViewWidthSizable, NSViewHeightSizable)
    from Foundation import NSBundle
    try:
        from WebKit import (WKWebView, WKWebViewConfiguration,
                            WKUserContentController)
    except ImportError:
        sys.exit("Vigil's window needs pyobjc-framework-WebKit.\n"
                 "  pip install pyobjc-framework-WebKit\n"
                 "(or run the venv python / re-run 'Install Vigil.command', "
                 "or use --menubar).")

    engines = _setup_engines(cfg, want_motion, motion_reason)
    SCALE = {"threshold_g": 1000.0}
    base = os.path.dirname(os.path.abspath(__file__))

    def _system_dark():
        try:
            ap = NSApplication.sharedApplication().effectiveAppearance()
            name = ap.bestMatchFromAppearancesWithNames_(
                ["NSAppearanceNameAqua", "NSAppearanceNameDarkAqua"])
            return "Dark" in str(name)
        except Exception:
            return True

    def _resolve_theme():
        # env override (dev) wins, else the saved config; "auto" follows macOS.
        mode = os.environ.get("VIGIL_THEME", "").lower() or cfg.get("theme", "auto")
        if mode == "system":
            mode = "auto"
        if mode not in ("auto", "light", "dark"):
            mode = "auto"
        dark = _system_dark() if mode == "auto" else (mode == "dark")
        return mode, dark

    def _load_html():
        with open(os.path.join(base, "assets", "carbon_ui.html")) as fh:
            html = fh.read()
        faces = ""
        try:
            with open(os.path.join(base, "assets", "fonts", "plex_b64.json")) as fh:
                for key, b64 in json.load(fh).items():
                    fam, wt = key.split("|")
                    faces += (f"@font-face{{font-family:'{fam}';font-style:normal;"
                              f"font-weight:{wt};font-display:swap;"
                              f"src:url(data:font/woff2;base64,{b64}) format('woff2');}}\n")
        except (OSError, ValueError):
            pass
        return html.replace("__FONTS__", faces)

    class Bridge(NSObject):
        @objc.python_method
        def setup(self):
            self.core = VigilCore(cfg, engines, notify=self._notify)
            self._banner = None
            self._banner_until = 0.0
            self._nstimer = None
            self._torn = False
            self._last_dark = None
            return self

        @objc.python_method
        def _notify(self, title, subtitle, message):
            self._set_banner(f"{subtitle}: {message}", "warn")

        @objc.python_method
        def _set_banner(self, text, kind="info", secs=5.0):
            self._banner = (text, kind)
            self._banner_until = time.monotonic() + secs

        @objc.python_method
        def _fields_payload(self):
            return {
                "away_rssi": cfg["away_rssi"], "present_rssi": cfg["present_rssi"],
                "grace_seconds": cfg["grace_seconds"],
                "threshold_g": round(cfg["threshold_g"] * 1000),
                "max_alarm_s": cfg["max_alarm_s"], "arm_grace_s": cfg["arm_grace_s"],
                "heartbeat_s": cfg["heartbeat_s"],
            }

        @objc.python_method
        def _js(self, fn, arg):
            try:
                self.web.evaluateJavaScript_completionHandler_(
                    f"{fn}({json.dumps(arg)})", None)
            except Exception:
                pass

        @objc.python_method
        def _push_init(self):
            mode, dark = _resolve_theme()
            self._last_dark = dark
            self._js("vigilInit", {
                "theme": {"mode": mode, "dark": dark},
                "fields": self._fields_payload(),
                "silent": bool(cfg.get("silent_mode")),
                "link": bool(cfg.get("link_lock_to_motion")),
            })

        @objc.python_method
        def _apply_theme(self):
            mode, dark = _resolve_theme()
            self._last_dark = dark
            try:
                self.web.evaluateJavaScript_completionHandler_(
                    f"applyTheme({json.dumps(mode)},{str(dark).lower()})", None)
            except Exception:
                pass

        # WKNavigationDelegate
        def webView_didFinishNavigation_(self, web, nav):
            self._push_init()

        # WKScriptMessageHandler
        def userContentController_didReceiveScriptMessage_(self, ucc, message):
            body = message.body()
            try:
                action = str(body["action"])
            except Exception:
                return
            self._handle(action, body)

        @objc.python_method
        def _handle(self, action, body):
            core = self.core
            if action == "toggleProximity":
                if core.proximity_armed():
                    core.disarm_proximity()
                    self._set_banner("Proximity lock disarmed")
                elif not core.has_device():
                    self._set_banner("Pick a device first", "warn")
                elif core.arm_proximity():
                    self._set_banner("Proximity lock armed")
            elif action == "pickDevice":
                uid = str(body.get("uid") or "")
                for u, d in core.resolvable_devices():
                    if u == uid:
                        core.pick_device(u, d["name"])
                        break
            elif action == "toggleMotion":
                core.disarm_motion() if core.motion_armed() else core.arm_motion()
            elif action == "enableMotion":
                self._enable_motion()
            elif action == "testAlarm":
                core.test_alarm()
            elif action == "toggleSilent":
                core.toggle_silent()
            elif action == "toggleLink":
                cfg["link_lock_to_motion"] = not cfg.get("link_lock_to_motion")
                save_config(cfg)
            elif action == "setTheme":
                mode = str(body.get("mode") or "auto")
                if mode in ("auto", "light", "dark"):
                    cfg["theme"] = mode
                    save_config(cfg)
                    self._apply_theme()
            elif action == "lockNow":
                self._set_banner(f"Locked via {core.lock_now()}", "ok")
            elif action == "setField":
                self._set_field(str(body.get("key")), body.get("value"))
            elif action == "save":
                save_config(cfg)
                self._set_banner("Settings saved", "ok")
            elif action == "export":
                self._export()
            elif action == "import":
                self._import()
            elif action == "quit":
                self._quit()

        @objc.python_method
        def _reset_field(self, key):
            scale = SCALE.get(key, 1.0)
            shown = round(cfg[key] * scale) if scale != 1.0 else cfg[key]
            self._js2("vigilField", json.dumps(key), json.dumps(shown))

        @objc.python_method
        def _js2(self, fn, a, b):
            try:
                self.web.evaluateJavaScript_completionHandler_(f"{fn}({a},{b})", None)
            except Exception:
                pass

        @objc.python_method
        def _set_field(self, key, raw):
            if key not in _NUM_BOUNDS:
                return
            scale = SCALE.get(key, 1.0)
            try:
                entered = float(raw) / scale
            except (TypeError, ValueError):
                self._reset_field(key)
                return
            ok, val = clamp_num(key, entered, cfg)
            if not ok:
                self._reset_field(key)
                lo, hi = _NUM_BOUNDS[key]
                self._set_banner(f"{key} must be {lo*scale:g}…{hi*scale:g}", "warn")
                return
            self.core.set_value(key, val)
            if key == "heartbeat_s":
                self._reschedule(val)

        @objc.python_method
        def _enable_motion(self):
            control, data = helper_paths()
            self._set_banner("Requesting permission…")
            ok, msg = launch_motion_helper(control, data)
            if not ok:
                self._set_banner("Cancelled" if msg == "cancelled"
                                 else f"Failed: {msg}", "warn")
                return
            self.core.enable_remote_motion()
            self._set_banner("Motion alarm enabled — click Arm", "ok")

        @objc.python_method
        def _export(self):
            panel = NSSavePanel.savePanel()
            panel.setNameFieldStringValue_("vigil-settings.json")
            if panel.runModal() == 1:
                try:
                    export_config(panel.URL().path(), cfg)
                    self._set_banner("Exported", "ok")
                except OSError as e:
                    self._set_banner(f"Export failed: {e}", "err")

        @objc.python_method
        def _import(self):
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(True)
            panel.setCanChooseDirectories_(False)
            panel.setAllowsMultipleSelection_(False)
            if panel.runModal() == 1:
                try:
                    merged = import_config(panel.URLs()[0].path())
                except (OSError, ValueError) as e:
                    self._set_banner(f"Import failed: {e}", "err")
                    return
                cfg.clear()
                cfg.update(merged)
                save_config(cfg)
                self.core.monitor.samples.clear()
                self.core.monitor.reset_warmup()
                self._push_init()
                self._set_banner("Imported", "ok")

        # heartbeat
        @objc.python_method
        def start_timer(self):
            hb = min(5.0, max(0.1, float(cfg["heartbeat_s"])))
            self._ticker = _Ticker.alloc().init().configure(self._tick)
            self._nstimer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                hb, self._ticker, b"fire:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self._nstimer, NSRunLoopCommonModes)

        @objc.python_method
        def _reschedule(self, hb):
            if self._nstimer is not None:
                self._nstimer.invalidate()
            self.start_timer()

        @objc.python_method
        def _tick(self):
            core = self.core
            core.heartbeat_step()
            # in Auto, follow a macOS light/dark switch made while we're running
            mode, dark = _resolve_theme()
            if mode == "auto" and dark != self._last_dark:
                self._apply_theme()
            p_txt, rssi = core.proximity_view()
            pct = 0 if rssi is None else max(0.0, min(100.0, (rssi + 100) / 60.0 * 100))
            if not core.monitor.bt_ready:
                p_tag, p_tk = "Bluetooth off", "err"
            elif not core.proximity_armed():
                p_tag, p_tk = "Monitoring", "info"
            elif core.monitor.state == AWAY:
                p_tag, p_tk = "Away — locked", "info"
            elif not core.monitor.fresh:
                p_tag, p_tk = "No signal", "warn"
            else:
                p_tag, p_tk = "Present", "ok"
            devices = [{"uid": u, "name": d["name"], "rssi": d["rssi"]}
                       for u, d in core.resolvable_devices()[:12]]

            if core.want_motion and core.sensor is not None:
                m_mode = "ready"
            elif (engines["motion_reason"] == "needs sudo -E (root)"
                  and core.sensor is None):
                m_mode = "enable"
            else:
                m_mode = "none"
            m_txt, mg = core.motion_view()
            thr_mg = cfg["threshold_g"] * 1000 or 1
            m_pct = min(100.0, mg / thr_mg * 50) if m_mode == "ready" else 0
            if m_mode == "enable":
                m_tag, m_tk = "Off", "info"
            elif m_mode == "none":
                m_tag, m_tk = "Unavailable", ""
            elif core._alarm_engaged():
                m_tag, m_tk = "ALARM", "err"
            elif core.motion_armed():
                m_tag, m_tk = "Armed", "ok"
            else:
                m_tag, m_tk = "Disarmed", "info"

            if core._alarm_engaged():
                header, hk = "Alarm", "alarm"
            elif core.proximity_armed() or core.motion_armed():
                header, hk = "Armed", "armed"
            else:
                header, hk = "Monitoring", ""

            banner, bk = "", "info"
            if self._banner and time.monotonic() < self._banner_until:
                banner, bk = self._banner

            self._js("vigilTick", {
                "p": {"armed": core.proximity_armed(), "tagText": p_tag, "tag": p_tk,
                      "rssi": "—" if rssi is None else f"{rssi:.0f} dBm", "pct": pct,
                      "devices": devices, "device": cfg.get("device_identifier") or ""},
                "m": {"mode": m_mode, "armed": core.motion_armed(),
                      "tagText": m_tag, "tag": m_tk, "reason": engines["motion_reason"],
                      "mg": (f"{mg:.0f} mg"
                             + (f"  peak {core.motion_peak*1000:.0f}" if m_mode == "ready" else "")),
                      "pct": m_pct, "hot": bool(mg >= thr_mg and m_mode == "ready"),
                      "silent": bool(cfg.get("silent_mode"))},
                "header": header, "headerKind": hk,
                "banner": banner, "bannerKind": bk,
            })

        @objc.python_method
        def _teardown(self):
            if self._torn:
                return
            self._torn = True
            if self._nstimer is not None:
                self._nstimer.invalidate()
            self.core.teardown()

        @objc.python_method
        def _quit(self):
            self.win.close()

        # NSWindowDelegate
        def windowWillClose_(self, note):
            self._teardown()
            AppHelper.stopEventLoop()

    class AppDelegate(NSObject):
        def applicationShouldTerminateAfterLastWindowClosed_(self, app):
            return True

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    icns = os.path.join(base, "assets", "Vigil.icns")
    if os.path.exists(icns):
        _img = NSImage.alloc().initWithContentsOfFile_(icns)
        if _img is not None:
            app.setApplicationIconImage_(_img)
    try:
        _info = NSBundle.mainBundle().infoDictionary()
        if _info is not None:
            _info["CFBundleName"] = "Vigil"
    except Exception:
        pass

    mainmenu = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    mainmenu.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItemWithTitle_action_keyEquivalent_("Hide Vigil", b"hide:", "h")
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItemWithTitle_action_keyEquivalent_("Quit Vigil", b"terminate:", "q")
    app_item.setSubmenu_(app_menu)
    edit_item = NSMenuItem.alloc().init()
    mainmenu.addItem_(edit_item)
    edit_menu = NSMenu.alloc().initWithTitle_("Edit")
    for title, sel, key in (("Cut", b"cut:", "x"), ("Copy", b"copy:", "c"),
                            ("Paste", b"paste:", "v"), ("Select All", b"selectAll:", "a")):
        edit_menu.addItemWithTitle_action_keyEquivalent_(title, sel, key)
    edit_item.setSubmenu_(edit_menu)
    app.setMainMenu_(mainmenu)

    bridge = Bridge.alloc().init().setup()
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)

    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
             | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 940, 820), style, NSBackingStoreBuffered, False)
    win.setTitle_("Vigil")
    win.setReleasedWhenClosed_(False)
    win.setDelegate_(bridge)
    win.setMinSize_((520, 560))
    win.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0.086, 0.086, 0.086, 1.0))
    bridge.win = win

    conf = WKWebViewConfiguration.alloc().init()
    ucc = WKUserContentController.alloc().init()
    ucc.addScriptMessageHandler_name_(bridge, "vigil")
    conf.setUserContentController_(ucc)
    web = WKWebView.alloc().initWithFrame_configuration_(
        NSMakeRect(0, 0, 940, 820), conf)
    web.setNavigationDelegate_(bridge)
    web.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    try:
        web.setValue_forKey_(False, "drawsBackground")
    except Exception:
        pass
    bridge.web = web
    win.contentView().addSubview_(web)

    web.loadHTMLString_baseURL_(_load_html(), None)
    bridge.start_timer()

    win.center()
    win.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    signal.signal(signal.SIGTERM, lambda *_: AppHelper.stopEventLoop())
    try:
        AppHelper.runEventLoop()
    finally:
        bridge._teardown()



# ---- menu-bar front end -----------------------------------------------------

def run_menubar(cfg, want_motion, motion_reason):
    import rumps
    engines = _setup_engines(cfg, want_motion, motion_reason)

    class VigilBar(rumps.App):
        def __init__(self):
            super().__init__("🛡︎ Vigil", quit_button=None)
            self.core = VigilCore(cfg, engines,
                                  notify=lambda t, s, m: rumps.notification(t, s, m))
            self.p_arm = rumps.MenuItem("Arm proximity lock", callback=self.p_toggle)
            self.p_status = rumps.MenuItem("Proximity: —")
            self.p_signal = rumps.MenuItem("Signal: —")
            self.p_device = rumps.MenuItem(self._dev_label())
            self.m_arm = rumps.MenuItem("Arm motion alarm", callback=self.m_toggle)
            self.m_status = rumps.MenuItem("Motion: —")
            self.m_motion = rumps.MenuItem("motion: —")
            self.thr_item = rumps.MenuItem(f"Threshold: {cfg['threshold_g']*1000:.0f} mg")
            self.silent = rumps.MenuItem(self._silent_label(), callback=self.silent_toggle)
            self.menu = [
                rumps.MenuItem("— Proximity —"), self.p_arm, self.p_status,
                self.p_signal, self.p_device,
                rumps.MenuItem("Pick device…", callback=self.pick_device),
                rumps.MenuItem("Away threshold…", callback=lambda _: self._prompt("away_rssi")),
                rumps.MenuItem("Present threshold…", callback=lambda _: self._prompt("present_rssi")),
                rumps.MenuItem("Grace…", callback=lambda _: self._prompt("grace_seconds")),
                rumps.MenuItem("Lock now", callback=self.lock_now),
                None, rumps.MenuItem("— Motion —"), self.m_arm, self.m_status,
                self.m_motion, self.thr_item,
                rumps.MenuItem("Set threshold (mg)…", callback=lambda _: self._prompt("threshold_g", 1000.0)),
                rumps.MenuItem("Max alarm…", callback=lambda _: self._prompt("max_alarm_s")),
                rumps.MenuItem("Arm grace…", callback=lambda _: self._prompt("arm_grace_s")),
                self.silent, rumps.MenuItem("Test alarm", callback=self.test_alarm),
                None, rumps.MenuItem("— General —"),
                rumps.MenuItem("Set heartbeat…", callback=lambda _: self._prompt("heartbeat_s", reschedule=True)),
                rumps.MenuItem("Save settings", callback=self.save_settings),
                rumps.MenuItem("Export settings…", callback=self.export_settings),
                rumps.MenuItem("Import settings…", callback=self.import_settings),
                rumps.MenuItem("Quit Vigil", callback=self.quit_app),
            ]
            self.prev_locked = screen_is_locked()
            hb = min(5.0, max(0.1, float(cfg["heartbeat_s"])))
            self._ticker = _Ticker.alloc().init().configure(self._tick)
            self._nstimer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                hb, self._ticker, b"fire:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self._nstimer, NSRunLoopCommonModes)

        def _dev_label(self):
            n = cfg.get("device_name") or cfg.get("device_identifier")
            return f"Device: {n}" if n else "Device: (none)"

        def _silent_label(self):
            return f"Silent mode: {'ON' if cfg.get('silent_mode') else 'off'}"

        def p_toggle(self, _):
            if self.core.proximity_armed():
                self.core.disarm_proximity()
            elif not self.core.arm_proximity():
                rumps.alert("Vigil", "Pick a device first.")

        def m_toggle(self, _):
            if not engines["want_motion"]:
                rumps.alert("Vigil", "Motion unavailable.\n" + (engines["motion_reason"] or ""))
                return
            self.core.disarm_motion() if self.core.motion_armed() else self.core.arm_motion()

        def silent_toggle(self, _):
            self.core.toggle_silent()
            self.silent.title = self._silent_label()

        def test_alarm(self, _):
            if engines["want_motion"]:
                self.core.test_alarm()

        def lock_now(self, _):
            rumps.notification("Vigil", "Test lock", f"locked via {self.core.lock_now()}")

        def save_settings(self, _):
            save_config(cfg)
            rumps.notification("Vigil", "Settings saved", config_path())

        def export_settings(self, _):
            from AppKit import NSSavePanel
            panel = NSSavePanel.savePanel()
            panel.setNameFieldStringValue_("vigil-settings.json")
            if panel.runModal() != 1:
                return
            try:
                export_config(panel.URL().path(), cfg)
                rumps.notification("Vigil", "Exported", panel.URL().path())
            except OSError as exc:
                rumps.alert("Vigil", f"Export failed: {exc}")

        def import_settings(self, _):
            from AppKit import NSOpenPanel
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(True)
            panel.setCanChooseDirectories_(False)
            panel.setAllowsMultipleSelection_(False)
            if panel.runModal() != 1:
                return
            try:
                merged = import_config(panel.URLs()[0].path())
            except (OSError, ValueError) as exc:
                rumps.alert("Vigil", f"Import failed: {exc}")
                return
            cfg.clear()
            cfg.update(merged)
            save_config(cfg)
            self.core.monitor.samples.clear()
            self.core.monitor.reset_warmup()
            self.thr_item.title = f"Threshold: {cfg['threshold_g']*1000:.0f} mg"
            self.silent.title = self._silent_label()
            self.p_device.title = self._dev_label()
            self._reschedule(cfg["heartbeat_s"])
            rumps.notification("Vigil", "Settings imported", "applied")

        def pick_device(self, _):
            devs = self.core.resolvable_devices()
            if not devs:
                rumps.alert("Vigil", "No resolvable devices seen yet.")
                return
            listing = "\n".join(f"{i}: {d['name']} ({d['rssi']} dBm)"
                                for i, (_u, d) in enumerate(devs))
            w = rumps.Window(message=f"Type the number:\n{listing}", title="Vigil",
                             default_text="0", ok="Set", cancel="Cancel", dimensions=(240, 22))
            r = w.run()
            if not r.clicked:
                return
            try:
                idx = int(r.text.strip())
                if idx < 0:
                    raise IndexError
                uid, d = devs[idx]
            except (ValueError, IndexError):
                rumps.alert("Vigil", "Invalid selection.")
                return
            self.core.pick_device(uid, d["name"])
            self.p_device.title = self._dev_label()

        def _prompt(self, key, scale=1.0, reschedule=False):
            w = rumps.Window(message=f"New value for {key}:", title="Vigil",
                             default_text=f"{cfg[key]*scale:g}", ok="Save",
                             cancel="Cancel", dimensions=(200, 22))
            r = w.run()
            if not r.clicked:
                return
            try:
                entered = float(r.text.strip()) / scale
            except ValueError:
                rumps.alert("Vigil", "Please enter a number.")
                return
            ok, val = clamp_num(key, entered, cfg)
            if not ok:
                lo, hi = _NUM_BOUNDS[key]
                rumps.alert("Vigil", f"{key} must be between {lo*scale:g} and {hi*scale:g}.")
                return
            self.core.set_value(key, val)
            if key == "threshold_g":
                self.thr_item.title = f"Threshold: {cfg['threshold_g']*1000:.0f} mg"
            if reschedule:
                self._reschedule(val)

        def _reschedule(self, hb):
            self._nstimer.invalidate()
            hb = min(5.0, max(0.1, float(hb)))
            self._ticker = _Ticker.alloc().init().configure(self._tick)
            self._nstimer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                hb, self._ticker, b"fire:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self._nstimer, NSRunLoopCommonModes)

        def quit_app(self, _):
            self._nstimer.invalidate()
            self.core.teardown()
            rumps.quit_application()

        @objc.python_method
        def _tick(self):
            self.core.heartbeat_step()
            p_txt, rssi = self.core.proximity_view()
            self.p_status.title = f"Proximity: {p_txt}"
            self.p_arm.title = "Disarm proximity lock" if self.core.proximity_armed() else "Arm proximity lock"
            self.p_signal.title = f"Signal: {_rssi_bar(rssi)} {'—' if rssi is None else f'{rssi:.0f} dBm'}"
            m_txt, mg = self.core.motion_view()
            self.m_status.title = f"Motion: {m_txt}"
            if engines["want_motion"]:
                self.m_arm.title = "Disarm motion alarm" if self.core.motion_armed() else "Arm motion alarm"
                self.m_motion.title = f"motion: {mg:.0f} mg (peak {self.core.motion_peak*1000:.0f})"
            bits = []
            if self.core.proximity_armed():
                bits.append("🔒" if self.core.monitor.state == AWAY else "🟢")
            if self.core._alarm_engaged():
                bits.append("🚨")
            elif self.core.motion_armed():
                bits.append("🔴")
            self.title = "🛡︎" + ("" if not bits else " " + "".join(bits))

    app = VigilBar()
    try:
        app.run()
    finally:
        app.core.teardown()


# ---- startup ----------------------------------------------------------------

def decide_motion():
    try:
        from macimu import IMU
    except ImportError:
        return False, "macimu not installed"
    if not IMU.available():
        return False, "no SPU accelerometer on this Mac"
    if os.geteuid() != 0:
        return False, "needs sudo -E (root)"
    return True, ""


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Vigil — combined proximity-lock + motion-alarm app.")
    p.add_argument("--menubar", action="store_true",
                   help="run as a menu-bar app instead of the window GUI")
    p.add_argument("--silent", action="store_true", help="start motion silent mode on")
    p.add_argument("--heartbeat", type=float, default=None,
                   help="check/refresh interval in seconds (0.1–5)")
    args = p.parse_args(argv)

    cfg = load_config()
    if args.silent:
        cfg["silent_mode"] = True
    if args.heartbeat is not None:
        if math.isfinite(args.heartbeat) and 0.1 <= args.heartbeat <= 5.0:
            cfg["heartbeat_s"] = args.heartbeat
        else:
            sys.exit("--heartbeat must be between 0.1 and 5")

    want_motion, reason = decide_motion()
    if not has_gui_session():
        print("WARNING: no GUI (Aqua) session — launch from a Terminal in your "
              "login session, not ssh/LaunchDaemon.", file=sys.stderr)
    print(f"Vigil starting — proximity: on, motion: "
          f"{'on' if want_motion else 'OFF (' + reason + ')'}")
    if not want_motion and reason == "needs sudo -E (root)":
        print("  (re-run with:  sudo -E python3 vigil.py  for the motion alarm)")

    if args.menubar:
        run_menubar(cfg, want_motion, reason)
    else:
        run_window(cfg, want_motion, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
