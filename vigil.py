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
import signal
import sys
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
            if (not self.sensor.is_alive() and self.sensor.armed
                    and not self._sensor_fail_alerted):
                self._sensor_fail_alerted = True
                self.notify("Vigil", "Motion sensor stopped",
                            "NOT protected — motion detection failed.")
            elif self.sensor.is_alive() and not self.sensor.error:
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
        if s.error or not s.is_alive():
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
    from AppKit import (NSApplication, NSWindow, NSView, NSTextField, NSButton,
                        NSPopUpButton, NSLevelIndicator, NSFont, NSColor, NSBox,
                        NSSavePanel, NSOpenPanel,
                        NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
                        NSWindowStyleMaskMiniaturizable, NSBackingStoreBuffered,
                        NSApplicationActivationPolicyRegular, NSButtonTypeSwitch,
                        NSBezelStyleRounded, NSLevelIndicatorStyleContinuousCapacity,
                        NSTextAlignmentRight)

    engines = _setup_engines(cfg, want_motion, motion_reason)

    class _Flipped(NSView):
        def isFlipped(self):
            return True

    class VigilWindow(NSObject):
        # -- build --
        @objc.python_method
        def build(self):
            def banner(title, sub, msg):   # non-blocking notification
                self._set_banner(f"⚠️ {sub}: {msg}")
            self.core = VigilCore(cfg, engines, notify=banner)
            self.fields = {}               # NSTextField -> (key,)
            self._dev_names = None
            self._dev_uids = {}

            W, H = 470, 726
            style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                     | NSWindowStyleMaskMiniaturizable)
            self.win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
            self.win.setTitle_("Vigil")
            self.win.setReleasedWhenClosed_(False)
            self.win.setDelegate_(self)
            content = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
            self.win.setContentView_(content)
            self._content = content
            self._W = W

            TIP = {
                "arm_p": "Start/stop locking the screen when your phone leaves. "
                         "Armed = it will lock when you walk away. It never unlocks.",
                "signal": "Live Bluetooth signal from your phone. Fuller bar = closer. "
                          "Watch this while you do a walk test.",
                "device": "The paired Bluetooth device Vigil tracks. Pair your phone in "
                          "System Settings ▸ Bluetooth first, then pick it here.",
                "away": "Lock when the signal drops to this level or WEAKER (more "
                        "negative = farther). Walk to your 'lock here' spot, read the "
                        "signal there, set this a few dB above it.",
                "present": "After a lock, count you as 'back' when the signal recovers "
                           "to this or STRONGER. Must be less negative than Away — the "
                           "gap between the two prevents flicker.",
                "grace": "Seconds the signal must stay 'away' before locking. Ignores "
                         "brief Bluetooth dips so it won't lock while you're sitting there.",
                "arm_m": "Start/stop the motion tripwire. Needs the app run with a "
                         "password (see Read Me).",
                "motion": "Live movement in mg (thousandths of g). Spikes when the Mac "
                          "is moved.",
                "thresh": "How hard a movement triggers the alarm. Lower = more "
                          "sensitive. ~60 mg = a firm nudge; ~200 mg = a real shove.",
                "maxalarm": "Safety cap — the siren stops after this many seconds even "
                            "if still armed.",
                "armgrace": "Ignore movement for this many seconds right after arming, "
                            "so setting the Mac down doesn't set it off.",
                "silent": "Test mode: show an on-screen alert instead of playing the "
                          "loud siren.",
                "test": "Fire a short 3-second alarm to check it works.",
                "link": "When the screen locks (e.g. the proximity lock fires), "
                        "automatically arm the motion alarm; disarm when you unlock.",
                "heartbeat": "How often Vigil re-checks and refreshes the display. "
                             "Lower = snappier, uses a little more power.",
                "save": "Save current settings now (also commits the field you're "
                        "editing). Settings also save automatically on every change.",
                "export": "Save your settings to a file you can back up or copy to "
                          "another Mac.",
                "import": "Load settings from a file you exported earlier.",
                "locknow": "Lock the screen right now (a quick test).",
                "quit": "Quit Vigil.",
            }

            y = 14
            self.header = self._label("Vigil", 16, y, W - 32, bold=True, size=20)
            y += 28
            self._label("Auto-locks when you leave · alarms if the Mac is moved · "
                        "hover any control for help", 16, y, W - 32, size=11)
            y += 20
            self.banner = self._label("", 16, y, W - 32, size=11)
            y += 20

            # ---- proximity ----
            y = self._section("PROXIMITY LOCK", y)
            self.p_btn = self._button("Arm", 16, y, 90, b"toggleProximity:", TIP["arm_p"])
            self.p_state = self._label("disarmed", 116, y + 3, W - 132)
            y += 34
            self._label("Signal", 16, y + 2, 60)
            self.p_meter = self._meter(80, y, 240, TIP["signal"])
            self.p_rssi = self._label("--", 330, y + 2, W - 346)
            y += 30
            self._label("Device", 16, y + 3, 60)
            self.dev_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(80, y, W - 96, 26), False)
            self.dev_popup.setTarget_(self)
            self.dev_popup.setAction_(b"deviceChanged:")
            self.dev_popup.setToolTip_(TIP["device"])
            self._content.addSubview_(self.dev_popup)
            y += 34
            self._field_row("Away (dBm)", "away_rssi", 16, y, W, tip=TIP["away"])
            self._field_row("Present (dBm)", "present_rssi", 245, y, W, x2=True, tip=TIP["present"])
            y += 30
            self._field_row("Grace (s)", "grace_seconds", 16, y, W, tip=TIP["grace"])
            y += 36

            # ---- motion ----
            y = self._section("MOTION ALARM", y)
            self.m_btn = self._button("Arm", 16, y, 90, b"toggleMotion:", TIP["arm_m"])
            if not engines["want_motion"]:
                self.m_btn.setEnabled_(False)
            self.m_state = self._label(
                "disarmed" if engines["want_motion"] else engines["motion_reason"],
                116, y + 3, W - 132)
            y += 34
            self._label("Motion", 16, y + 2, 60)
            self.m_meter = self._meter(80, y, 240, TIP["motion"])
            self.m_mg = self._label("--", 330, y + 2, W - 346)
            y += 30
            # THE requested input — motion threshold, visible + editable
            self._field_row("Threshold (mg)", "threshold_g", 16, y, W, scale=1000.0, tip=TIP["thresh"])
            y += 30
            self._field_row("Max alarm (s)", "max_alarm_s", 16, y, W, tip=TIP["maxalarm"])
            self._field_row("Arm grace (s)", "arm_grace_s", 245, y, W, x2=True, tip=TIP["armgrace"])
            y += 32
            self.silent_chk = self._check("Silent mode (no siren)", 16, y,
                                          200, b"toggleSilent:", cfg["silent_mode"], TIP["silent"])
            self.test_btn = self._button("Test alarm", 250, y - 4, 110, b"testAlarm:", TIP["test"])
            if not engines["want_motion"]:
                self.silent_chk.setEnabled_(False)
                self.test_btn.setEnabled_(False)
            y += 36

            # ---- general ----
            y = self._section("GENERAL", y)
            self.link_chk = self._check("Lock ⇒ arm motion", 16, y, 200,
                                        b"toggleLink:", cfg["link_lock_to_motion"], TIP["link"])
            y += 28
            self._field_row("Heartbeat (s)", "heartbeat_s", 16, y, W, tip=TIP["heartbeat"])
            y += 36
            y = self._section("SETTINGS", y)
            self._button("Save settings", 16, y, 130, b"saveSettings:", TIP["save"])
            self._button("Export…", 152, y, 100, b"exportSettings:", TIP["export"])
            self._button("Import…", 258, y, 100, b"importSettings:", TIP["import"])
            y += 40
            self._button("Lock screen now", 16, y, 150, b"lockNow:", TIP["locknow"])
            self._button("Quit", W - 96, y, 80, b"quitApp:", TIP["quit"])

            self.win.center()
            self.win.makeKeyAndOrderFront_(None)
            self.win.makeFirstResponder_(None)   # don't let a field grab focus on open
            self._refresh_device_popup()
            self._refresh()

            hb = min(5.0, max(0.1, float(cfg["heartbeat_s"])))
            self._ticker = _Ticker.alloc().init().configure(self._tick)
            self._nstimer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                hb, self._ticker, b"fire:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(
                self._nstimer, NSRunLoopCommonModes)
            return self

        # -- widget helpers --
        @objc.python_method
        def _label(self, text, x, y, w, bold=False, size=13, right=False):
            f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, 20))
            f.setStringValue_(text)
            f.setBezeled_(False)
            f.setDrawsBackground_(False)
            f.setEditable_(False)
            f.setSelectable_(False)
            f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                       else NSFont.systemFontOfSize_(size))
            self._content.addSubview_(f)
            return f

        @objc.python_method
        def _section(self, title, y):
            self._label(title, 16, y, self._W - 32, bold=True, size=11)
            return y + 22

        @objc.python_method
        def _button(self, title, x, y, w, action, tip=None):
            b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, 26))
            b.setTitle_(title)
            b.setBezelStyle_(NSBezelStyleRounded)
            b.setTarget_(self)
            b.setAction_(action)
            if tip:
                b.setToolTip_(tip)
            self._content.addSubview_(b)
            return b

        @objc.python_method
        def _check(self, title, x, y, w, action, state, tip=None):
            b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, 22))
            b.setButtonType_(NSButtonTypeSwitch)
            b.setTitle_(title)
            b.setState_(1 if state else 0)
            b.setTarget_(self)
            b.setAction_(action)
            if tip:
                b.setToolTip_(tip)
            self._content.addSubview_(b)
            return b

        @objc.python_method
        def _meter(self, x, y, w, tip=None):
            lv = NSLevelIndicator.alloc().initWithFrame_(NSMakeRect(x, y, w, 20))
            lv.setLevelIndicatorStyle_(NSLevelIndicatorStyleContinuousCapacity)
            lv.setMinValue_(0.0)
            lv.setMaxValue_(100.0)
            if tip:
                lv.setToolTip_(tip)
            self._content.addSubview_(lv)
            return lv

        @objc.python_method
        def _field_row(self, label, key, x, y, W, x2=False, scale=1.0, tip=None):
            lbl = self._label(label, x, y + 3, 120)
            fx = x + 120
            fw = 80
            tf = NSTextField.alloc().initWithFrame_(NSMakeRect(fx, y, fw, 22))
            shown = cfg[key] * scale
            tf.setStringValue_(f"{shown:g}")
            tf.setDelegate_(self)
            if tip:
                tf.setToolTip_(tip)
                lbl.setToolTip_(tip)
            self._content.addSubview_(tf)
            self.fields[tf] = (key, scale)
            return tf

        @objc.python_method
        def _set_banner(self, text):
            if hasattr(self, "banner"):
                self.banner.setStringValue_(text)

        # -- NSTextField delegate: commit on Enter / focus-loss --
        def controlTextDidEndEditing_(self, note):
            tf = note.object()
            spec = self.fields.get(tf)
            if not spec:
                return
            key, scale = spec
            raw = tf.stringValue().strip()
            try:
                entered = float(raw) / scale
            except ValueError:
                tf.setStringValue_(f"{cfg[key] * scale:g}")
                self._set_banner("⚠️ not a number — kept previous value")
                return
            ok, val = clamp_num(key, entered, cfg)
            if not ok:
                lo, hi = _NUM_BOUNDS[key]
                tf.setStringValue_(f"{cfg[key] * scale:g}")
                self._set_banner(f"⚠️ {key} must be {lo*scale:g}…{hi*scale:g} — kept previous")
                return
            self.core.set_value(key, val)
            tf.setStringValue_(f"{val * scale:g}")
            self._set_banner("")
            if key == "heartbeat_s":
                self._reschedule(val)

        @objc.python_method
        def _reschedule(self, hb):
            if getattr(self, "_nstimer", None) is not None:
                self._nstimer.invalidate()
            hb = min(5.0, max(0.1, float(hb)))
            self._ticker = _Ticker.alloc().init().configure(self._tick)
            self._nstimer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                hb, self._ticker, b"fire:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(
                self._nstimer, NSRunLoopCommonModes)

        # -- actions --
        def toggleProximity_(self, _):
            if self.core.proximity_armed():
                self.core.disarm_proximity()
                self._set_banner("")
                return
            # Adopt whatever device the dropdown is showing, so arming "just works"
            # instead of silently refusing when the popup auto-picked a device the
            # user never explicitly selected.
            if not self.core.has_device():
                title = self.dev_popup.titleOfSelectedItem()
                uid = self._dev_uids.get(title)
                if uid:
                    self.core.pick_device(uid, title)
            if self.core.arm_proximity():
                self._set_banner("")
            else:
                self._set_banner("⚠️ no device yet — wait for the dropdown to list "
                                 "your phone, then Arm")

        def toggleMotion_(self, _):
            if self.core.motion_armed():
                self.core.disarm_motion()
            else:
                self.core.arm_motion()

        def toggleSilent_(self, sender):
            self.core.toggle_silent()
            sender.setState_(1 if cfg.get("silent_mode") else 0)

        def toggleLink_(self, sender):
            cfg["link_lock_to_motion"] = bool(sender.state())
            save_config(cfg)

        def testAlarm_(self, _):
            self.core.test_alarm()

        def lockNow_(self, _):
            method = self.core.lock_now()
            self._set_banner(f"locked via {method}")

        def saveSettings_(self, _):
            self.win.makeFirstResponder_(None)   # commit any pending field edit
            save_config(cfg)
            self._set_banner(f"✓ settings saved to {config_path()}")

        def exportSettings_(self, _):
            self.win.makeFirstResponder_(None)
            panel = NSSavePanel.savePanel()
            panel.setNameFieldStringValue_("vigil-settings.json")
            if panel.runModal() != 1:            # 1 == NSModalResponseOK
                return
            path = panel.URL().path()
            try:
                export_config(path, cfg)
                self._set_banner(f"✓ exported to {os.path.basename(path)}")
            except OSError as exc:
                self._set_banner(f"⚠️ export failed: {exc}")

        def importSettings_(self, _):
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(True)
            panel.setCanChooseDirectories_(False)
            panel.setAllowsMultipleSelection_(False)
            if panel.runModal() != 1:
                return
            path = panel.URLs()[0].path()
            try:
                merged = import_config(path)
            except (OSError, ValueError) as exc:
                self._set_banner(f"⚠️ import failed: {exc}")
                return
            cfg.clear()
            cfg.update(merged)          # same dict object the engines share
            save_config(cfg)
            self._reload_ui_from_cfg()
            self._set_banner(f"✓ imported {os.path.basename(path)}")

        @objc.python_method
        def _reload_ui_from_cfg(self):
            for tf, (key, scale) in self.fields.items():
                tf.setStringValue_(f"{cfg[key] * scale:g}")
            self.silent_chk.setState_(1 if cfg.get("silent_mode") else 0)
            self.link_chk.setState_(1 if cfg.get("link_lock_to_motion") else 0)
            # let the (possibly new) device re-acquire cleanly
            self.core.monitor.samples.clear()
            self.core.monitor.last_seen = time.monotonic()
            self.core.monitor.reset_warmup()
            self._dev_names = None       # force popup rebuild + reselect
            self._refresh_device_popup()
            self._reschedule(cfg["heartbeat_s"])
            self._refresh()

        def deviceChanged_(self, popup):
            title = popup.titleOfSelectedItem()
            uid = self._dev_uids.get(title)
            if uid:
                self.core.pick_device(uid, title)

        def quitApp_(self, _):
            self.win.close()

        # -- window/app lifecycle --
        def windowWillClose_(self, _):
            self.win.makeFirstResponder_(None)   # commit any in-progress field edit
            self._teardown()
            AppHelper.stopEventLoop()

        @objc.python_method
        def _teardown(self):
            if getattr(self, "_nstimer", None) is not None:
                self._nstimer.invalidate()
                self._nstimer = None
            self.core.teardown()

        # -- periodic refresh --
        @objc.python_method
        def _tick(self):
            self.core.heartbeat_step()
            self._refresh()

        @objc.python_method
        def _refresh_device_popup(self):
            devs = self.core.resolvable_devices()
            names = [d["name"] for _u, d in devs]
            cur = cfg.get("device_name")
            if cur and cur not in names:
                names = [cur] + names
            # Rebuild only when the SET of names changes, not on RSSI reorder —
            # otherwise a scan tick tears down the dropdown while it's open.
            if self._dev_names is None or set(names) != set(self._dev_names):
                self._dev_names = names
                self.dev_popup.removeAllItems()
                self.dev_popup.addItemsWithTitles_(names or ["(scanning…)"])
                if cur in names:
                    self.dev_popup.selectItemWithTitle_(cur)
            # strongest-RSSI wins for a duplicated name (devs is sorted strongest-first)
            self._dev_uids = {}
            for uid, d in devs:
                self._dev_uids.setdefault(d["name"], uid)

        @objc.python_method
        def _refresh(self):
            p_txt, rssi = self.core.proximity_view()
            self.p_state.setStringValue_(p_txt)
            self.p_btn.setTitle_("Disarm" if self.core.proximity_armed() else "Arm")
            self.p_rssi.setStringValue_("--" if rssi is None else f"{rssi:.0f} dBm")
            self.p_meter.setDoubleValue_(
                0.0 if rssi is None else min(100.0, max(0.0, (rssi + 100) / 60.0 * 100)))
            self._refresh_device_popup()

            m_txt, mg = self.core.motion_view()
            self.m_state.setStringValue_(m_txt)
            if engines["want_motion"]:
                self.m_btn.setTitle_("Disarm" if self.core.motion_armed() else "Arm")
                self.m_mg.setStringValue_(f"{mg:.0f} mg  (peak {self.core.motion_peak*1000:.0f})")
                thr_mg = cfg["threshold_g"] * 1000
                self.m_meter.setDoubleValue_(min(100.0, mg / thr_mg * 50.0) if thr_mg else 0)

            armed = self.core.proximity_armed() or self.core.motion_armed()
            self.header.setStringValue_(
                "Vigil — 🚨 ALARM" if self.core._alarm_engaged()
                else ("Vigil — armed" if armed else "Vigil"))

    class AppDelegate(NSObject):
        def applicationShouldTerminateAfterLastWindowClosed_(self, app):
            return True

    from AppKit import NSImage, NSMenu, NSMenuItem
    from Foundation import NSBundle

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    # App identity so the Dock/menu read "Vigil" with our icon, not generic Python.
    icns = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "Vigil.icns")
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

    # Minimal main menu: app menu (Quit) + Edit menu (so field copy/paste works).
    main = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    main.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItemWithTitle_action_keyEquivalent_("Hide Vigil", b"hide:", "h")
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItemWithTitle_action_keyEquivalent_("Quit Vigil", b"terminate:", "q")
    app_item.setSubmenu_(app_menu)
    edit_item = NSMenuItem.alloc().init()
    main.addItem_(edit_item)
    edit_menu = NSMenu.alloc().initWithTitle_("Edit")
    for title, sel, key in (("Cut", b"cut:", "x"), ("Copy", b"copy:", "c"),
                            ("Paste", b"paste:", "v"), ("Select All", b"selectAll:", "a")):
        edit_menu.addItemWithTitle_action_keyEquivalent_(title, sel, key)
    edit_item.setSubmenu_(edit_menu)
    app.setMainMenu_(main)

    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    controller = VigilWindow.alloc().init().build()
    app.activateIgnoringOtherApps_(True)
    signal.signal(signal.SIGTERM, lambda *_: AppHelper.stopEventLoop())
    try:
        AppHelper.runEventLoop()
    finally:
        controller._teardown()


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
