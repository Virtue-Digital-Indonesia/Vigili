#!/usr/bin/env python3
"""
Vigili — Part 1: proximity_lock.py

Locks the Mac's screen when a paired Bluetooth device (your iPhone / AirPods /
Watch) goes out of range, and *re-arms* when it comes back. It NEVER unlocks —
that is intentional. Unlocking is the login window's job.

HOW IT ACTUALLY WORKS (read this — it differs from the naive plan)
------------------------------------------------------------------
The obvious idea — "read the paired device's Bluetooth RSSI" — does NOT work on
modern macOS:

  * Classic-Bluetooth RSSI (IOBluetooth) is unusable: an iPhone/Watch keeps no
    classic connection to the Mac, and even for connected devices macOS returns
    a frozen `0`. Verified on this machine.
  * BLE devices use rotating "resolvable private addresses", so a raw BLE scan
    can't tell which anonymous advertiser is *your* phone.

What *does* work — and what this script uses — is **CoreBluetooth passive
scanning**. Because you paired the device in System Settings, macOS holds its
identity resolving key and, during a scan, hands us back the *resolved name*
(e.g. "Efran's iPhone") plus a **stable per-Mac identifier (UUID)** and a live
**dBm RSSI** for every advertisement. That is the whole reason pairing first is
mandatory: it's what de-anonymizes the rotating address. An unpaired device just
shows up as `None` with a random address and is useless to us.

So: we listen for your device's advertisements, smooth the RSSI, and drive a
PRESENT⇄AWAY state machine with hysteresis + a grace period. On PRESENT→AWAY we
lock the screen once. On AWAY→PRESENT we simply re-arm (we do not unlock).

FRAGILITY / THINGS THAT CAN BREAK (you asked to know)
-----------------------------------------------------
  * Requires pyobjc (CoreBluetooth). No root needed.
  * The Terminal/app you run this from needs the **Bluetooth** privacy
    permission (System Settings ▸ Privacy & Security ▸ Bluetooth). macOS will
    prompt the first time; if you deny it, scanning silently returns nothing.
  * Locking uses the **private** symbol `SACLockScreenImmediate` from
    login.framework. It has been stable for many macOS releases but is
    undocumented and could vanish in an update. Fallbacks (screen saver / display
    sleep) are used if it's gone — but those only *lock* if you have
    "require password immediately" set (yours is currently a 60-second grace,
    see the security note in the README).
  * RSSI is noisy (bodies, walls, multipath). Thresholds MUST be tuned per desk;
    use `--monitor` and do a walk test. The shipped defaults are a starting point.
  * If Bluetooth is turned off, we can't sense the device. By default we do NOT
    lock on sensor loss (fail-open, to avoid nuisance locks). Pass
    `--lock-on-signal-loss` to invert that.

USAGE
-----
  python3 proximity_lock.py --scan            # list nearby resolvable devices
  python3 proximity_lock.py --calibrate       # pick your device, save config
  python3 proximity_lock.py --monitor         # live RSSI/state, NO locking (tune here)
  python3 proximity_lock.py                    # run: lock when away, re-arm when back

Config lives at ~/.config/vigili/proximity.json. Override any threshold on the
command line (see --help).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pwd
import signal
import statistics
import sys
import time
from collections import deque

# ---- dependency guard -------------------------------------------------------
try:
    import objc  # noqa: F401
    import CoreBluetooth
    from Foundation import NSObject, NSRunLoop, NSDate, NSTimer
except ImportError as exc:  # pragma: no cover
    sys.exit(
        "Missing pyobjc CoreBluetooth bindings.\n"
        "  pip install pyobjc-core pyobjc-framework-CoreBluetooth\n"
        f"(import error: {exc})"
    )

import ctypes


# ---- config -----------------------------------------------------------------

APP_DIR_NAME = "vigili"
CONFIG_BASENAME = "proximity.json"

DEFAULT_CONFIG = {
    "device_identifier": None,   # CoreBluetooth peripheral UUID (preferred match key)
    "device_name": None,         # human label / fallback match key
    "away_rssi": -78,            # smoothed RSSI at/below this => "away" candidate (dBm)
    "present_rssi": -58,         # smoothed RSSI at/above this => "present" again (hysteresis)
    "grace_seconds": 12.0,       # away condition must persist this long before locking
    "absence_timeout": 20.0,     # no advertisement for this long => treated as no-signal
    "smoothing_window": 8.0,     # seconds of RSSI history used for the median
    "min_samples": 2,            # need at least this many samples in-window to trust it
    "lock_method": "immediate",  # immediate | keystroke | screensaver
}


def resolve_home() -> str:
    """Real user's home, even if invoked under sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            return pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def config_path(override: str | None = None) -> str:
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(resolve_home(), ".config")
    return os.path.join(base, APP_DIR_NAME, CONFIG_BASENAME)


def migrate_legacy_config_dir() -> None:
    """One-time: rename the old ~/.config/vigil directory to ~/.config/vigili so
    the app rename doesn't strand saved settings. Idempotent, best-effort."""
    new = os.path.dirname(config_path())                   # ~/.config/vigili
    old = os.path.join(os.path.dirname(new), "vigil")      # ~/.config/vigil
    if old == new:
        return
    try:
        if os.path.isdir(old) and not os.path.exists(new):
            os.rename(old, new)
    except OSError:
        pass


_NUM_BOUNDS = {
    "away_rssi": (-127, 0), "present_rssi": (-127, 0),
    "grace_seconds": (0, 3600), "absence_timeout": (0, 3600),
    "smoothing_window": (0.5, 3600), "min_samples": (1, 100),
}


def _sanitize(cfg: dict) -> dict:
    """Coerce corrupt numeric config to sane values (a bad away_rssi='inf' would
    otherwise nuisance-lock; a bad smoothing_window would crash the deque prune)."""
    for key, (lo, hi) in _NUM_BOUNDS.items():
        v = cfg.get(key)
        try:
            v = float(v)
            if not math.isfinite(v):
                raise ValueError
        except (TypeError, ValueError):
            v = float(DEFAULT_CONFIG[key])
        v = min(hi, max(lo, v))
        cfg[key] = int(round(v)) if isinstance(DEFAULT_CONFIG[key], int) else v
    return cfg


def load_config(path: str) -> dict:
    migrate_legacy_config_dir()
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")
        cfg.update(data)
    except FileNotFoundError:
        pass
    except (ValueError, TypeError, OSError) as exc:  # ValueError covers JSONDecodeError
        print(f"warning: could not read config {path}, using defaults: {exc}",
              file=sys.stderr)
    return _sanitize(cfg)


def save_config(path: str, cfg: dict) -> None:
    # 0o600 file / 0o700 dir: this holds the identifier that de-anonymizes your
    # phone's advertisements — keep it out of other local accounts' reach.
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        data = json.dumps(cfg, indent=2).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, data)
            os.fchmod(fd, 0o600)     # enforce mode even if the file pre-existed
        finally:
            os.close(fd)
        print(f"saved config -> {path}")
    except OSError as exc:
        d = os.path.dirname(path)
        print(f"error: could not save config to {path}: {exc}\n"
              f"(if {d} is root-owned from a sudo run of motion_alarm, fix with:\n"
              f'  sudo chown -R "$USER" {d} )', file=sys.stderr)


# ---- screen lock ------------------------------------------------------------

_LOGIN_FRAMEWORK = "/System/Library/PrivateFrameworks/login.framework/login"


def _accessibility_trusted() -> bool:
    """True if this process may synthesize system keystrokes (Accessibility)."""
    try:
        ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework"
                         "/ApplicationServices")
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(ax.AXIsProcessTrusted())
    except Exception:
        return False


def _accessibility_prompt() -> bool:
    """Ask macOS for Accessibility — pops the system 'allow to control your
    computer' prompt (adds this app to the list). Returns current trust state."""
    try:
        ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework"
                         "/ApplicationServices")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework"
                         "/CoreFoundation")
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
            ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        key = ctypes.c_void_p.in_dll(ax, "kAXTrustedCheckOptionPrompt")
        val = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        keys = (ctypes.c_void_p * 1)(key)
        vals = (ctypes.c_void_p * 1)(val)
        # NULL callbacks are fine — key/value are constant globals, never freed.
        opts = cf.CFDictionaryCreate(None, keys, vals, 1, None, None)
        trusted = bool(ax.AXIsProcessTrustedWithOptions(opts))
        if opts:
            cf.CFRelease(opts)
        return trusted
    except Exception:
        return _accessibility_trusted()


def _lock_immediate() -> str | None:
    """Private login.framework lock — instant, no permission. May also sleep the
    display on some macOS versions (that's what the keystroke method avoids)."""
    try:
        login = ctypes.CDLL(_LOGIN_FRAMEWORK)
        if hasattr(login, "SACLockScreenImmediate"):
            login.SACLockScreenImmediate()
            return "immediate"
    except OSError:
        pass
    return None


def _lock_keystroke() -> bool:
    """Post ⌃⌘Q — the native 'Lock Screen' — which keeps the display ON.
    Requires Accessibility permission for this app."""
    try:
        import Quartz
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        flags = Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskCommand
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(src, 12, down)  # 12 = 'q'
            Quartz.CGEventSetFlags(ev, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        return True
    except Exception:
        return False


def lock_screen(method: str = "immediate") -> str:
    """Lock the screen. `method`: 'immediate' (private call, may sleep display),
    'keystroke' (native ⌃⌘Q, keeps display on, needs Accessibility), or
    'screensaver' (starts the screen saver, display stays on). Never unlocks."""
    method = (method or "immediate").lower()
    import subprocess

    if method == "keystroke":
        if _accessibility_trusted() and _lock_keystroke():
            return "keystroke"
        # not permitted → still lock (so we never leave it unlocked), but flag it
        return (_lock_immediate() or "displaysleep") + " · grant Accessibility for ⌃⌘Q"

    if method == "screensaver":
        try:
            subprocess.run(["/usr/bin/open", "-a", "ScreenSaverEngine"],
                           check=True, timeout=5)
            return "screensaver"
        except Exception:
            pass
        m = _lock_immediate()
        if m:
            return m

    # default: immediate, with fallbacks
    m = _lock_immediate()
    if m:
        return m
    try:
        subprocess.run(["/usr/bin/open", "-a", "ScreenSaverEngine"],
                       check=True, timeout=5)
        return "screensaver (fallback)"
    except Exception:
        pass
    subprocess.run(["/usr/bin/pmset", "displaysleepnow"], timeout=5)
    return "displaysleep (last-resort)"


# ---- BLE monitor ------------------------------------------------------------

CBManagerStatePoweredOn = 5
INVALID_RSSI = 127  # CoreBluetooth sentinel for "unknown"

PRESENT = "PRESENT"
AWAY = "AWAY"


def _rssi_ok(value: int) -> bool:
    # Valid RSSI is negative dBm; 0/127/positive are junk sentinels.
    return value < 0 and value != INVALID_RSSI


def _signal_bar(rssi) -> str:
    """8-block signal bar from an RSSI (dBm) for at-a-glance monitoring."""
    if rssi is None:
        return "········"
    n = max(0, min(8, round(((rssi + 100) / 60.0) * 8)))  # -100->0, -40->full
    return "▇" * n + "·" * (8 - n)


class ProximityMonitor(NSObject):
    """CBCentralManager delegate + presence state machine.

    Constructed via ProximityMonitor.new(); configure with .setup_().
    """

    # -- construction --
    @objc.python_method
    def setup_(self, params: dict):
        self.cfg = params["cfg"]
        self.monitor_only = params["monitor_only"]
        self.lock_on_signal_loss = params["lock_on_signal_loss"]
        self.verbose = params["verbose"]
        # Menu-bar arm/disarm: when False, we track + display but never lock.
        self.locking_enabled = params.get("locking_enabled", True)

        self.samples: deque = deque()          # (monotonic_t, rssi)
        self.last_seen: float | None = None
        self.state = PRESENT
        self.away_since: float | None = None
        self.locked_latch = False              # true once we've locked this away-episode
        self.start_time = time.monotonic()
        # Warm-up: don't lock during the first moments before we've had a chance
        # to hear the device at all.
        self.warmup_until = self.start_time + max(self.cfg["grace_seconds"], 8.0)
        self.bt_ready = False
        self.central = None
        self._stop = False

        # exposed for the menu bar / display
        self.smoothed: float | None = None
        self.fresh = False
        # Only lock once we've actually detected the device since arming — arming
        # with no signal (device absent/off) must NOT immediately lock.
        self.present_established = False
        self.last_lock_method: str | None = None
        self.seen_resolvable: dict[str, dict] = {}   # uid -> {name, rssi, t}
        return self

    @objc.python_method
    def reset_warmup(self):
        """Restart the warm-up window (used when arming from the menu bar)."""
        now = time.monotonic()
        self.warmup_until = now + max(self.cfg["grace_seconds"], 8.0)
        self.away_since = None
        self.locked_latch = False
        self.state = PRESENT
        self.present_established = False

    # -- CBCentralManagerDelegate --
    def centralManagerDidUpdateState_(self, central):
        state = central.state()
        self.bt_ready = (state == CBManagerStatePoweredOn)
        if self.bt_ready:
            self._start_scan()
            if self.verbose:
                print("bluetooth ready — scanning")
        else:
            print(f"bluetooth not ready (state={state}); waiting", file=sys.stderr)

    @objc.python_method
    def _start_scan(self):
        opts = {CoreBluetooth.CBCentralManagerScanOptionAllowDuplicatesKey: True}
        # services=None => all advertisers; AllowDuplicates => continuous RSSI.
        self.central.scanForPeripheralsWithServices_options_(None, opts)

    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(
            self, central, peripheral, adv, rssi):
        value = int(rssi)
        if not _rssi_ok(value):
            return
        uid = str(peripheral.identifier().UUIDString())
        name = peripheral.name() or adv.get("kCBAdvDataLocalName")
        now = time.monotonic()

        # Track resolvable (named) devices so the menu bar can offer a picker.
        if name:
            self.seen_resolvable[uid] = {"name": str(name), "rssi": value, "t": now}

        if self.monitor_only and getattr(self, "_calibration_sink", None) is not None:
            # calibration/scan path records everything it sees
            self._calibration_sink(uid, name, value, adv)
            return

        if not self._matches(uid, name):
            return
        self.samples.append((now, value))
        self.last_seen = now

    @objc.python_method
    def _matches(self, uid: str, name) -> bool:
        want_id = self.cfg.get("device_identifier")
        want_name = self.cfg.get("device_name")
        if want_id:
            return uid == want_id
        if want_name and name:
            return str(name) == want_name
        return False

    # -- periodic evaluation (NSTimer target) --
    def evaluate_(self, _timer):
        now = time.monotonic()
        window = self.cfg["smoothing_window"]
        # prune old samples
        while self.samples and (now - self.samples[0][0]) > window:
            self.samples.popleft()

        # Evict resolvable devices not heard in a while — bounds memory and keeps
        # the menu-bar device picker from growing unboundedly over a long session.
        stale = [u for u, dd in self.seen_resolvable.items() if now - dd["t"] > 300.0]
        for u in stale:
            del self.seen_resolvable[u]

        recent = [r for _, r in self.samples]
        smoothed = statistics.median(recent) if len(recent) >= self.cfg["min_samples"] else None
        fresh = (self.last_seen is not None
                 and (now - self.last_seen) <= self.cfg["absence_timeout"])
        self.smoothed, self.fresh = smoothed, fresh
        if fresh:
            self.present_established = True   # we've actually detected the device

        if not self.bt_ready:
            # Can't sense. Default: do nothing (fail-open).
            self._print_status(now, smoothed, "bt-off")
            if not self.lock_on_signal_loss:
                return
            away_condition = True
        elif not fresh:
            # No advertisement within absence_timeout => genuinely out of range.
            away_condition = True
        elif smoothed is None:
            # Seen very recently but too few samples to judge RSSI yet — we DO
            # have contact, so treat as present (don't lock while the phone's here).
            away_condition = False
        else:
            away_condition = smoothed <= self.cfg["away_rssi"]

        # --- state machine ---
        if away_condition:
            if self.away_since is None:
                self.away_since = now
            elapsed = now - self.away_since
            can_lock = now >= self.warmup_until
            # Never lock unless we actually saw the device present since arming —
            # arming with no signal must not fire.
            if (self.state == PRESENT and not self.locked_latch
                    and self.present_established
                    and can_lock and elapsed >= self.cfg["grace_seconds"]):
                self._go_away(now)
        else:
            self.away_since = None
            if self.state == AWAY and smoothed is not None and smoothed >= self.cfg["present_rssi"]:
                self._go_present(now)

        self._print_status(now, smoothed, self.state.lower())

    @objc.python_method
    def _go_away(self, now: float):
        self.state = AWAY
        self.locked_latch = True
        if self.monitor_only or not self.locking_enabled:
            if self.verbose:
                print(">>> would LOCK now (monitor/disarmed; no action taken)")
            self.last_lock_method = None
            return
        method = lock_screen(self.cfg.get("lock_method", "immediate"))
        self.last_lock_method = method
        if self.verbose:
            print(f"\n>>> AWAY: locked screen via {method}")

    @objc.python_method
    def _go_present(self, now: float):
        self.state = PRESENT
        self.locked_latch = False
        if self.verbose:
            print("\n>>> PRESENT: re-armed (screen left as-is; we never unlock)")

    @objc.python_method
    def _print_status(self, now: float, smoothed, tag: str):
        if not self.verbose:
            return
        age = "  -  " if self.last_seen is None else f"{now - self.last_seen:4.1f}s"
        sval = "  --  " if smoothed is None else f"{smoothed:6.1f}"
        bar = _signal_bar(smoothed)
        line = (f"[{tag:8}] {bar} {sval} dBm  seen {age}  "
                f"n={len(self.samples):2d}  {'LOCKED' if self.locked_latch else '     '}")
        if sys.stdout.isatty():
            # rewrite one line in place — much easier to watch than a firehose
            print("\r" + line + "  ", end="", flush=True)
        else:
            print(line)

    @objc.python_method
    def stop(self):
        self._stop = True


# ---- run loop helpers -------------------------------------------------------

def _make_central(delegate):
    return CoreBluetooth.CBCentralManager.alloc().initWithDelegate_queue_options_(
        delegate, None, None)


def _pump(monitor, seconds: float | None):
    """Drive the run loop in slices so Ctrl-C is responsive."""
    rl = NSRunLoop.currentRunLoop()
    deadline = None if seconds is None else time.monotonic() + seconds
    while not monitor._stop:
        rl.runMode_beforeDate_(
            "kCFRunLoopDefaultMode",
            NSDate.dateWithTimeIntervalSinceNow_(0.4))
        if deadline is not None and time.monotonic() >= deadline:
            break


# ---- commands ---------------------------------------------------------------

def cmd_scan(args, cfg):
    """Passively scan and print every resolvable device we hear."""
    seen: dict[str, dict] = {}

    monitor = ProximityMonitor.new().setup_({
        "cfg": cfg, "monitor_only": True,
        "lock_on_signal_loss": False, "verbose": False})

    def sink(uid, name, rssi, adv):
        mfg = adv.get("kCBAdvDataManufacturerData")
        apple = bool(mfg) and bytes(mfg)[:2].hex() == "4c00"
        d = seen.setdefault(uid, {"name": None, "rssi": [], "apple": apple})
        d["rssi"].append(rssi)
        if name:
            d["name"] = str(name)
        if apple:
            d["apple"] = True

    monitor._calibration_sink = sink
    monitor.central = _make_central(monitor)

    dur = args.duration
    print(f"scanning {dur:.0f}s … (walk your device around to see RSSI change)\n")
    _pump(monitor, dur)

    rows = sorted(seen.items(),
                  key=lambda kv: max(kv[1]["rssi"]) if kv[1]["rssi"] else -999,
                  reverse=True)
    print(f"\n{'#':>2}  {'name':30} {'max':>4} {'min':>4} {'seen':>4}  identifier")
    named = []
    for i, (uid, d) in enumerate(rows):
        label = d["name"] or ("<Apple, unresolved>" if d["apple"] else "<unknown>")
        rs = d["rssi"]
        star = "*" if d["name"] else " "
        print(f"{i:>2}{star} {label[:30]:30} {max(rs):>4} {min(rs):>4} {len(rs):>4}  {uid}")
        named.append((uid, d))
    print("\n* = resolvable (paired) — only these are usable as a proximity token.")
    return named


def cmd_calibrate(args, cfg, path):
    named = cmd_scan(args, cfg)
    resolvable = [(uid, d) for uid, d in named if d["name"]]
    if not resolvable:
        print("\nNo resolvable (paired) devices found. Pair your device in "
              "System Settings ▸ Bluetooth first, make sure it's nearby and "
              "advertising, then retry.", file=sys.stderr)
        return
    print("\nPick the device to use as your proximity token:")
    for i, (uid, d) in enumerate(resolvable):
        print(f"  [{i}] {d['name']}  (max RSSI {max(d['rssi'])} dBm)")
    try:
        choice = int(input("number> ").strip())
        uid, d = resolvable[choice]
    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
        print("cancelled.", file=sys.stderr)
        return
    cfg["device_identifier"] = uid
    cfg["device_name"] = d["name"]
    save_config(path, cfg)
    print(f"\nToken set to {d['name']}. Now tune thresholds with:\n"
          f"  python3 {os.path.basename(__file__)} --monitor\n"
          f"Walk to where you consider 'away' and note the RSSI; set away_rssi a "
          f"little above the noise floor there, and present_rssi to your at-desk "
          f"value minus a few dB.")


def cmd_run(args, cfg, monitor_only: bool):
    if not cfg.get("device_identifier") and not cfg.get("device_name"):
        sys.exit("No device configured. Run:  python3 %s --calibrate"
                 % os.path.basename(__file__))

    monitor = ProximityMonitor.new().setup_({
        "cfg": cfg,
        "monitor_only": monitor_only,
        "lock_on_signal_loss": args.lock_on_signal_loss,
        "verbose": True})
    monitor.central = _make_central(monitor)

    def handle_sigint(signum, frame):
        print("\nstopping.")
        monitor.stop()
    signal.signal(signal.SIGINT, handle_sigint)

    # Evaluate the state machine once per second on the run loop.
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0, monitor, b"evaluate:", None, True)

    token = cfg.get("device_name") or cfg.get("device_identifier")
    mode = "MONITOR (no locking)" if monitor_only else "ARMED (will lock when away)"
    print(f"tracking {token!r} — {mode}\n"
          f"  away<= {cfg['away_rssi']} dBm, present>= {cfg['present_rssi']} dBm, "
          f"grace {cfg['grace_seconds']}s, absence {cfg['absence_timeout']}s\n"
          f"Ctrl-C to quit.\n")
    _pump(monitor, None)


# ---- menu bar app -----------------------------------------------------------

def run_menubar(args, cfg, path):
    """Menu-bar UI: Arm/Disarm, live RSSI, pick device, edit thresholds."""
    import rumps

    monitor = ProximityMonitor.new().setup_({
        "cfg": cfg,
        "monitor_only": False,
        "lock_on_signal_loss": args.lock_on_signal_loss,
        "verbose": False,
        "locking_enabled": False,       # start DISARMED
    })
    monitor.central = _make_central(monitor)

    class ProximityBar(rumps.App):
        def __init__(self):
            super().__init__("📡 Vigili", quit_button=None)
            self.arm_item = rumps.MenuItem("Arm", callback=self.toggle_arm)
            self.status_item = rumps.MenuItem("Status: monitoring (disarmed)")
            self.rssi_item = rumps.MenuItem("RSSI: --")
            self.device_item = rumps.MenuItem(self._device_label())
            self.menu = [
                self.arm_item,
                self.status_item,
                self.rssi_item,
                None,
                self.device_item,
                rumps.MenuItem("Pick device…", callback=self.pick_device),
                None,
                rumps.MenuItem("Away threshold…", callback=self.set_away),
                rumps.MenuItem("Present threshold…", callback=self.set_present),
                rumps.MenuItem("Grace period…", callback=self.set_grace),
                None,
                rumps.MenuItem("Lock now (test)", callback=self.lock_now),
                rumps.MenuItem("Quit Vigili", callback=self.quit_app),
            ]
            self._timer = rumps.Timer(self.tick, 1.0)
            self._timer.start()

        # -- helpers --
        def _device_label(self):
            name = cfg.get("device_name") or cfg.get("device_identifier")
            return f"Device: {name}" if name else "Device: (none — pick one)"

        def _has_device(self):
            return bool(cfg.get("device_identifier") or cfg.get("device_name"))

        def _prompt(self, message, default, title="Vigili"):
            w = rumps.Window(message=message, title=title,
                             default_text=str(default), ok="Save", cancel="Cancel",
                             dimensions=(220, 22))
            resp = w.run()
            return resp.text.strip() if resp.clicked else None

        # -- menu callbacks --
        def toggle_arm(self, _):
            if monitor.locking_enabled:
                monitor.locking_enabled = False
                self.arm_item.title = "Arm"
            else:
                if not self._has_device():
                    rumps.alert("Vigili", "Pick a device first (Pick device…).")
                    return
                monitor.reset_warmup()
                monitor.locking_enabled = True
                self.arm_item.title = "Disarm"

        def pick_device(self, _):
            devs = sorted(monitor.seen_resolvable.items(),
                          key=lambda kv: kv[1]["rssi"], reverse=True)
            if not devs:
                rumps.alert("Vigili", "No resolvable devices seen yet. Make sure "
                            "your device is paired, nearby, and Bluetooth is on.")
                return
            listing = "\n".join(f"{i}: {d['name']}  ({d['rssi']} dBm)"
                                for i, (_uid, d) in enumerate(devs))
            choice = self._prompt(f"Type the number of your device:\n{listing}",
                                  default="0", title="Pick device")
            if choice is None:
                return
            try:
                idx = int(choice)
                if idx < 0:                 # negative indexes silently pick the wrong end
                    raise IndexError
                uid, d = devs[idx]
            except (ValueError, IndexError):
                rumps.alert("Vigili", "Invalid selection.")
                return
            cfg["device_identifier"] = uid
            cfg["device_name"] = d["name"]
            save_config(path, cfg)
            # Fresh start for the new device, but treat the switch as "just heard"
            # so we don't nuisance-lock during its first advertising gap.
            monitor.samples.clear()
            monitor.last_seen = time.monotonic()
            monitor.reset_warmup()
            self.device_item.title = self._device_label()

        def _set_threshold(self, key, prompt):
            val = self._prompt(prompt, default=cfg[key])
            if val is None:
                return
            try:
                num = float(val)
            except ValueError:
                rumps.alert("Vigili", "Please enter a number.")
                return
            if not math.isfinite(num):
                rumps.alert("Vigili", "Please enter a finite number.")
                return
            cfg[key] = num
            save_config(path, cfg)

        def set_away(self, _):
            self._set_threshold("away_rssi",
                "Lock when smoothed RSSI drops to/below this (dBm, e.g. -78):")

        def set_present(self, _):
            self._set_threshold("present_rssi",
                "Re-arm when RSSI recovers to/above this (dBm, e.g. -60):")

        def set_grace(self, _):
            self._set_threshold("grace_seconds",
                "Seconds the signal must stay 'away' before locking (e.g. 12):")

        def lock_now(self, _):
            method = lock_screen()
            rumps.notification("Vigili", "Test lock", f"locked via {method}")

        def quit_app(self, _):
            monitor.stop()
            rumps.quit_application()

        # -- periodic poll --
        def tick(self, _):
            monitor.evaluate_(None)
            armed = monitor.locking_enabled
            if not monitor.bt_ready:
                state = "⚠︎ Bluetooth off"
            elif not armed:
                state = "monitoring (disarmed)"
            elif monitor.state == AWAY:
                state = "🔒 away — locked"
            elif not monitor.fresh:
                state = "🔒 no signal"
            else:
                state = "🟢 present — armed"
            self.title = "📡 " + ("🔴 " if armed else "") + state.split(" ")[0]
            self.status_item.title = f"Status: {state}"
            rssi = monitor.smoothed
            self.rssi_item.title = ("RSSI: --" if rssi is None
                                    else f"RSSI: {rssi:.0f} dBm")

    ProximityBar().run()


# ---- cli --------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="Vigili proximity lock (CoreBluetooth). Locks when your "
                    "paired device goes out of range; never unlocks.")
    p.add_argument("--scan", action="store_true",
                   help="list nearby resolvable devices and exit")
    p.add_argument("--calibrate", action="store_true",
                   help="scan, pick your device, and save it to config")
    p.add_argument("--monitor", action="store_true",
                   help="run the tracker but print state only, never lock")
    p.add_argument("--menubar", action="store_true",
                   help="run as a menu-bar app (Arm/Disarm, live RSSI, config)")
    p.add_argument("--duration", type=float, default=12.0,
                   help="scan duration for --scan/--calibrate (default 12s)")
    p.add_argument("--config", default=None, help="path to config json")
    p.add_argument("--lock-on-signal-loss", action="store_true",
                   help="lock if Bluetooth/sensing is lost (default: do not)")
    # threshold overrides
    for key in ("away_rssi", "present_rssi", "grace_seconds",
                "absence_timeout", "smoothing_window"):
        p.add_argument(f"--{key.replace('_','-')}", type=float, default=None,
                       help=f"override {key} (default {DEFAULT_CONFIG[key]})")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    path = config_path(args.config)
    cfg = load_config(path)
    for key in ("away_rssi", "present_rssi", "grace_seconds",
                "absence_timeout", "smoothing_window"):
        val = getattr(args, key)
        if val is not None:
            cfg[key] = val

    if args.scan:
        cmd_scan(args, cfg)
    elif args.calibrate:
        cmd_calibrate(args, cfg, path)
    elif args.menubar:
        run_menubar(args, cfg, path)
    else:
        cmd_run(args, cfg, monitor_only=args.monitor)


if __name__ == "__main__":
    main()
