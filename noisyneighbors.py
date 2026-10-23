#!/usr/bin/env python3
"""NoisyNeighbors - Detects neighbor booms and plays them back."""

import io
import csv
import hmac
import json
import math
import os
import re
import sys
import time
import wave
import queue
import random
import logging
import tempfile
import threading
import subprocess
import urllib.request
from datetime import datetime, date, timedelta

import numpy as np
import sounddevice as sd
from flask import Flask, Response, render_template, request, send_from_directory, jsonify
from flask_socketio import SocketIO

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("noisyneighbors")

# Flask app
app = Flask(__name__)
app.config["SECRET_KEY"] = "noisyneighbors"
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024  # custom sound uploads
socketio = SocketIO(app, async_mode="threading")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")

# Shared state
state = {
    "status": "listening",
    "history": [],
    "today_count": 0,
    "today_date": str(date.today()),
    "config": {},
    "enabled": True,
    "cb_state": None,
    "restart_audio": False,
    "calibrating": False,
    "calibration_samples": [],
    "hourly_boom_count": 0,
    "current_hour": -1,
    "pause_until": None,
    "escalation_level": 0,
    "escalation_ts": 0.0,
    "last_notify": 0.0,
    "limit_notified_hour": None,
    "effective_threshold": None,
}

CONFIG_PATH = "config.json"
CONFIG_EXAMPLE_PATH = "config.example.json"
HISTORY_PATH = "history.json"


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_EXAMPLE_PATH) as f:
            defaults = json.load(f)
        save_config(defaults)
        return defaults
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def load_history():
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            return json.load(f)
    return []


def save_history(history):
    with open(HISTORY_PATH, "w") as f:
        json.dump(history[-2000:], f)


def rms(block):
    return np.sqrt(np.mean(block ** 2))


def is_in_time_range(start_str, end_str):
    """Check if current time is within [start, end]. Handles midnight crossing."""
    try:
        now = datetime.now().time()
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()
        if start <= end:
            return start <= now <= end
        else:  # crosses midnight
            return now >= start or now <= end
    except (ValueError, TypeError):
        return False


def set_enabled(enabled):
    """Enable/disable detection and tell every dashboard."""
    state["enabled"] = enabled
    cb = state.get("cb_state")
    if cb is not None:
        cb["paused"] = not enabled
    socketio.emit("enabled_state", {"enabled": enabled})
    socketio.emit("status", {"state": "listening" if enabled else "disabled"})


def emit_pause_state():
    until = state["pause_until"]
    socketio.emit("pause_state", {"until": int(until * 1000) if until else None})


def pause_loop():
    """Background thread: re-enable detection when a timed pause expires."""
    while True:
        time.sleep(5)
        until = state["pause_until"]
        if until and time.time() >= until:
            state["pause_until"] = None
            set_enabled(True)
            emit_pause_state()
            log.info("Pause ended: NoisyNeighbors enabled")


# --- Optional dashboard password (HTTP Basic, password in config.json) ---

def auth_ok(auth):
    pw = state["config"].get("dashboard_password") or ""
    if not pw:
        return True
    given = (auth.password or "") if auth is not None else ""
    return hmac.compare_digest(given.encode(), str(pw).encode())


@app.before_request
def require_auth():
    if auth_ok(request.authorization):
        return None
    return Response("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="NoisyNeighbors"'})


# --- Notifications (ntfy.sh style: POST body to a URL) ---

def notify(title, message, force=False, throttle=True):
    cfg = state["config"]
    url = (cfg.get("notify_url") or "").strip()
    if not force and not cfg.get("notify_enabled", False):
        return
    if not url.startswith(("http://", "https://")):
        return
    if throttle and not force:
        min_gap = float(cfg.get("notify_min_interval", 60))
        if time.time() - state["last_notify"] < min_gap:
            return
        state["last_notify"] = time.time()

    def _send():
        try:
            req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST",
                                         headers={"Title": title})
            urllib.request.urlopen(req, timeout=10).close()
            log.info("Notification sent: %s", title)
        except Exception as e:
            log.warning("Notification failed: %s", e)
    threading.Thread(target=_send, daemon=True).start()


def to_db(level):
    return 20 * math.log10(max(float(level), 1e-6))


# --- Band-limited level (isolates thumps/bass from speech and TV) ---

_band_cache = {}


def band_level(block, sr, low, high):
    """RMS of `block` restricted to [low, high] Hz, via a windowed FFT."""
    x = np.asarray(block, dtype=np.float32).reshape(-1)
    n = len(x)
    key = (n, sr, low, high)
    c = _band_cache.get(key)
    if c is None:
        if len(_band_cache) > 8:
            _band_cache.clear()
        w = np.hanning(n).astype(np.float32)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        c = (w, (freqs >= low) & (freqs <= high), float(np.mean(w ** 2)))
        _band_cache[key] = c
    w, mask, wpow = c
    spec = np.fft.rfft(x * w)
    power = 2.0 * float(np.sum(np.abs(spec[mask]) ** 2)) / (n * n) / wpow
    return math.sqrt(power)


def strike_loop():
    """Background thread: randomly fire sound/vibration when Strike Mode is active."""
    next_strike = 0.0
    while True:
        time.sleep(1)
        cfg = state["config"]
        if not cfg.get("strike_mode_enabled", False) or not state["enabled"]:
            next_strike = 0.0
            continue
        now_ts = time.monotonic()
        if next_strike == 0.0:
            min_i = max(1.0, float(cfg.get("strike_min_interval", 60)))
            max_i = max(min_i + 1, float(cfg.get("strike_max_interval", 300)))
            delay = random.uniform(min_i, max_i)
            next_strike = now_ts + delay
            log.info("Strike Mode: first strike in %.0fs", delay)
            continue
        if now_ts < next_strike:
            continue
        # Fire!
        alsa = cfg.get("alsa_device") or detect_alsa_device()
        mode = resolve_replay_mode(cfg.get("replay_mode", "echo"), advance=True)
        log.info("Strike Mode: firing (mode=%s)", mode)
        socketio.emit("status", {"state": "boom"})
        if cfg.get("ps4_vibration", False):
            intensity = cfg.get("vibration_intensity", 100)
            threading.Thread(target=vibrate_ps4, args=(1.0, intensity), daemon=True).start()
        if mode == "echo":
            sr = 48000
            t_arr = np.linspace(0, 0.5, int(sr * 0.5), dtype=np.float32)
            audio = np.sin(2 * np.pi * 440 * t_arr) * 0.8
            threading.Thread(target=play_audio, args=(audio, sr, alsa, sr), daemon=True).start()
        else:
            threading.Thread(target=play_sound_file, args=(mode, alsa), daemon=True).start()
        time.sleep(0.5)
        socketio.emit("status", {"state": "listening" if state["enabled"] else "disabled"})
        # Schedule next strike
        min_i = max(1.0, float(cfg.get("strike_min_interval", 60)))
        max_i = max(min_i + 1, float(cfg.get("strike_max_interval", 300)))
        interval = random.uniform(min_i, max_i)
        next_strike = time.monotonic() + interval
        log.info("Strike Mode: next strike in %.0fs", interval)


def scheduler_loop():
    """Background thread: check schedule every 30s and auto-enable/disable."""
    while True:
        time.sleep(30)
        cfg = state["config"]
        if not cfg.get("schedule_enabled", False) or state["pause_until"]:
            continue
        start = cfg.get("schedule_start", "22:00")
        end = cfg.get("schedule_end", "08:00")
        should_be_enabled = is_in_time_range(start, end)
        if should_be_enabled != state["enabled"]:
            set_enabled(should_be_enabled)
            log.info("Scheduler: NoisyNeighbors %s", "enabled" if should_be_enabled else "disabled")


def list_devices():
    print("\n=== Available audio devices ===\n")
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        inp = d["max_input_channels"]
        out = d["max_output_channels"]
        sr = int(d["default_samplerate"])
        flags = []
        if inp > 0:
            flags.append(f"{inp} in")
        if out > 0:
            flags.append(f"{out} out")
        print(f"  [{i}] {d['name']}  ({', '.join(flags)})  {sr} Hz")
    print()
    print("For config.json:")
    print('  "device" : index of the device with input channels (mic)')
    print('  "alsa_device" : "plughw:<card>,0" for output')
    print()


def list_input_devices():
    devices = sd.query_devices()
    result = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            result.append({"id": i, "name": d["name"]})
    return result


def detect_device():
    devices = sd.query_devices()
    # Prefer a device clearly identifiable as known hardware — ALSA/PortAudio
    # enumeration order can shift after system updates (new virtual PCM plugins
    # get inserted), so a generic "usb" substring match alone is too easy to
    # collide with something else that happens to reuse a stale index.
    for i, d in enumerate(devices):
        if "jabra" in d["name"].lower() and d["max_input_channels"] > 0:
            return i, d
    for i, d in enumerate(devices):
        if "usb" in d["name"].lower() and d["max_input_channels"] > 0:
            return i, d
    return None, None


def is_valid_input_device(index, channels):
    """Check a configured device index still points to something usable —
    protects against a stale index after ALSA/PortAudio enumeration changes."""
    try:
        info = sd.query_devices(index)
        return info["max_input_channels"] >= channels
    except Exception:
        return False


def list_alsa_playback():
    import re
    try:
        result = subprocess.run(["aplay", "-l"], capture_output=True, text=True)
        devices = []
        for line in result.stdout.split("\n"):
            m = re.match(r"card (\d+):.*\[(.+?)\], device (\d+):", line)
            if m:
                card, name, dev = m.group(1), m.group(2), m.group(3)
                alsa_id = f"plughw:{card},{dev}"
                devices.append({"id": alsa_id, "name": name})
        return devices
    except Exception:
        return []


def detect_alsa_device():
    devices = list_alsa_playback()
    for d in devices:
        if "usb" in d["name"].lower():
            return d["id"]
    if devices:
        return devices[0]["id"]
    return "plughw:0,0"


def get_bluetooth_sink():
    """Find a connected Bluetooth (A2DP) PulseAudio sink."""
    try:
        result = subprocess.run(["pactl", "list", "sinks", "short"],
                                 capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and "bluez" in parts[1]:
                return parts[1]
    except Exception as e:
        log.error("Error listing pulse sinks: %s", e)
    return None


MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


def list_paired_bt_devices():
    """List Bluetooth devices already known/paired with this adapter."""
    try:
        r = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=5)
        devices = []
        for line in r.stdout.splitlines():
            m = re.match(r"Device ([0-9A-Fa-f:]{17}) (.+)", line.strip())
            if m:
                devices.append({"mac": m.group(1), "name": m.group(2)})
        return devices
    except Exception as e:
        log.error("Error listing paired BT devices: %s", e)
        return []


def is_bt_device_connected(mac):
    try:
        r = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True, timeout=5)
        return "Connected: yes" in r.stdout
    except Exception:
        return False


def get_bt_status():
    """Return the current Bluetooth secondary-output connection status."""
    sink = get_bluetooth_sink()
    mac = None
    if sink:
        m = re.search(r"bluez_sink\.([0-9A-Fa-f_]+)", sink)
        if m:
            mac = m.group(1).replace("_", ":")

    paired = list_paired_bt_devices()
    if mac is None:
        # PulseAudio can take a second or two to create the A2DP sink after BlueZ
        # reports the connection — fall back to asking BlueZ directly.
        for d in paired:
            if is_bt_device_connected(d["mac"]):
                mac = d["mac"]
                break

    name = None
    if mac:
        name = next((d["name"] for d in paired if d["mac"].upper() == mac.upper()), None)

    return {"connected": mac is not None, "mac": mac, "name": name}


def scan_bt_devices(duration=8):
    """Scan for nearby Bluetooth devices and return newly discovered, named ones."""
    try:
        proc = subprocess.Popen(
            ["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )

        def send(cmd):
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()

        send("power on")
        time.sleep(0.5)
        send("scan on")
        time.sleep(duration)
        send("scan off")
        time.sleep(0.5)
        send("devices")
        time.sleep(0.5)
        send("quit")
        out, _ = proc.communicate(timeout=10)
    except Exception as e:
        log.error("BT scan failed: %s", e)
        return []

    devices = {}
    for line in out.splitlines():
        m = re.match(r"Device ([0-9A-Fa-f:]{17}) (.+)", line.strip())
        if m:
            mac, name = m.group(1), m.group(2)
            # Skip unnamed devices (name defaults to MAC-with-dashes) and LE-only ads
            # (classic BR/EDR audio pairing needs the non-"LE-" advertisement)
            if name.replace(":", "-") == mac.replace(":", "-") or name.startswith("LE-"):
                continue
            devices[mac] = name
    return [{"mac": k, "name": v} for k, v in devices.items()]


def pair_bt_device(mac):
    """Pair, trust and connect a Bluetooth device by MAC address."""
    try:
        proc = subprocess.Popen(
            ["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )

        def send(cmd):
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()

        send("power on")
        time.sleep(0.3)
        send("agent on")
        send("default-agent")
        send(f"pair {mac}")
        time.sleep(8)
        send(f"trust {mac}")
        time.sleep(1)
        send(f"connect {mac}")
        time.sleep(3)
        send("quit")
        out, _ = proc.communicate(timeout=15)
    except Exception as e:
        log.error("BT pairing error: %s", e)
        return {"ok": False, "message": str(e)}

    if "Connection successful" in out:
        return {"ok": True, "message": "Paired and connected"}
    if "Pairing successful" in out:
        return {"ok": True, "message": "Paired (connecting...)"}
    if "Failed to pair" in out or "AuthenticationFailed" in out or "org.bluez.Error" in out:
        return {"ok": False, "message": "Pairing failed — put the speaker in pairing mode and retry"}
    return {"ok": False, "message": "No response — check logs"}


def connect_bt_device(mac):
    """(Re)connect an already-paired Bluetooth device."""
    try:
        r = subprocess.run(["bluetoothctl", "connect", mac], capture_output=True, text=True, timeout=15)
        lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
        ok = "Connection successful" in r.stdout
        return {"ok": ok, "message": lines[-1] if lines else ""}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def forget_bt_device(mac):
    """Remove a paired Bluetooth device."""
    try:
        r = subprocess.run(["bluetoothctl", "remove", mac], capture_output=True, text=True, timeout=10)
        return {"ok": r.returncode == 0, "message": r.stdout.strip()}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def restart_bluetooth_adapter():
    """Restart the Bluetooth daemon (if passwordless sudo is set up) and power-cycle the adapter."""
    # -n: never prompt for a password — if sudo isn't configured for this, fail fast instead of hanging
    r = subprocess.run(["sudo", "-n", "systemctl", "restart", "bluetooth"],
                       capture_output=True, text=True, timeout=15)
    daemon_restarted = r.returncode == 0
    if not daemon_restarted:
        log.warning("Could not restart bluetooth.service (passwordless sudo not configured?): %s",
                   r.stderr.strip())
    time.sleep(1 if daemon_restarted else 0)
    try:
        subprocess.run(["bluetoothctl", "power", "off"], capture_output=True, text=True, timeout=10)
        time.sleep(1)
        subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, text=True, timeout=10)
    except Exception as e:
        log.error("Bluetooth adapter power-cycle failed: %s", e)
        return {"ok": False, "message": str(e)}
    return {
        "ok": True,
        "message": "Bluetooth daemon and adapter restarted" if daemon_restarted else "Adapter power-cycled (daemon restart needs sudo setup)",
    }


BLUETOOTH_OUTPUT_ID = "bluetooth"


def list_output_devices():
    """ALSA playback devices plus the currently connected Bluetooth speaker, if any."""
    devices = list_alsa_playback()
    bt = get_bt_status()
    if bt["connected"]:
        devices.append({"id": BLUETOOTH_OUTPUT_ID, "name": bt["name"] or "Bluetooth speaker"})
    return devices


# Prevent simultaneous paplay calls (Bluetooth A2DP can't handle overlapping streams)
pulse_lock = threading.Lock()


def play_via_bluetooth(path):
    sink = get_bluetooth_sink()
    if not sink:
        log.error("Bluetooth output selected but no connected speaker found")
        return
    with pulse_lock:
        try:
            r = subprocess.run(["paplay", "--device", sink, path],
                               capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                log.error("paplay failed (rc=%d): %s", r.returncode, r.stderr.strip())
        except subprocess.TimeoutExpired:
            log.error("paplay timed out after 15s on %s", sink)
        except FileNotFoundError:
            log.error("paplay not found — install pulseaudio-utils")


def play_audio(audio, sr, alsa_device, out_sr, gain=None):
    if gain is None:
        gain = sound_gain("echo")
    if sr != out_sr:
        n_samples = int(len(audio) * out_sr / sr)
        indices = np.linspace(0, len(audio) - 1, n_samples)
        audio = np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)

    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak
    audio = audio * gain

    audio_int16 = (audio * 32767).astype(np.int16)
    stereo = np.column_stack([audio_int16, audio_int16])

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name
        with wave.open(f, "w") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(out_sr)
            w.writeframes(stereo.tobytes())

    if alsa_device == BLUETOOTH_OUTPUT_ID:
        play_via_bluetooth(tmp_path)
    else:
        with aplay_lock:
            log.debug("aplay start: %s", alsa_device)
            try:
                r = subprocess.run(["aplay", "-D", alsa_device, tmp_path],
                                   capture_output=True, text=True, timeout=15)
                if r.returncode != 0:
                    log.error("aplay failed (rc=%d): %s", r.returncode, r.stderr.strip())
                else:
                    log.debug("aplay done")
            except subprocess.TimeoutExpired:
                log.error("aplay timed out after 15s on %s", alsa_device)

    os.unlink(tmp_path)


SOUNDS_DIR = os.path.join(BASE_DIR, "sounds")
CUSTOM_SOUNDS_DIR = os.path.join(BASE_DIR, "custom_sounds")
BUILTIN_SOUNDS = ["echo", "alarm", "doorbell", "hammer", "honk", "siren", "sandstorm", "hammering"]
META_MODES = ["random", "escalate"]
# "escalate" walks up this list on consecutive booms, restarting after a quiet spell
ESCALATION_ORDER = ["echo", "doorbell", "honk", "alarm", "siren", "hammer", "hammering", "sandstorm"]
ESCALATION_RESET_SECONDS = 600
MAX_CUSTOM_SOUND_SECONDS = 30

# Prevent simultaneous aplay calls (USB device can't handle two streams at once)
aplay_lock = threading.Lock()


def list_custom_sounds():
    if not os.path.isdir(CUSTOM_SOUNDS_DIR):
        return []
    return sorted(f[:-4] for f in os.listdir(CUSTOM_SOUNDS_DIR) if f.endswith(".wav"))


def get_available_modes():
    return BUILTIN_SOUNDS + list_custom_sounds() + META_MODES


def sound_path(name):
    for d in (SOUNDS_DIR, CUSTOM_SOUNDS_DIR):
        path = os.path.join(d, f"{name}.wav")
        if os.path.exists(path):
            return path
    return None


def sound_gain(name):
    try:
        return float(np.clip(float(state["config"].get("sound_gains", {}).get(name, 1.0)), 0.1, 1.0))
    except (TypeError, ValueError):
        return 1.0


def resolve_replay_mode(mode, advance=False):
    """Turn "random"/"escalate" into a concrete sound name; other modes pass through."""
    if mode == "random":
        return random.choice(BUILTIN_SOUNDS + list_custom_sounds())
    if mode == "escalate":
        customs = list_custom_sounds()
        order = ESCALATION_ORDER + [c for c in customs if c not in ESCALATION_ORDER]
        now = time.time()
        if now - state["escalation_ts"] > ESCALATION_RESET_SECONDS:
            state["escalation_level"] = 0
        level = min(state["escalation_level"], len(order) - 1)
        if advance:
            state["escalation_level"] = level + 1
            state["escalation_ts"] = now
        return order[level]
    return mode


def effective_replay_mode():
    """Configured replay mode, honouring the night-mode override."""
    cfg = state["config"]
    mode = cfg.get("replay_mode", "echo")
    if cfg.get("night_mode_enabled", False) and is_in_time_range(
            cfg.get("night_mode_start", "22:00"), cfg.get("night_mode_end", "08:00")):
        mode = cfg.get("night_replay_mode", mode)
    return mode


def _scaled_copy(path, gain):
    """Temp copy of a 16-bit PCM wav scaled by `gain`, or None if it can't be scaled."""
    try:
        with wave.open(path) as w:
            if w.getsampwidth() != 2:
                return None
            nch, fr, frames = w.getnchannels(), w.getframerate(), w.readframes(w.getnframes())
        arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) * gain
        out = np.clip(arr, -32768, 32767).astype(np.int16)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        with wave.open(tmp, "w") as w:
            w.setnchannels(nch)
            w.setsampwidth(2)
            w.setframerate(fr)
            w.writeframes(out.tobytes())
        return tmp
    except (wave.Error, OSError, EOFError) as e:
        log.warning("Could not scale %s: %s", path, e)
        return None


def play_sound_file(name, alsa_device):
    path = sound_path(name)
    if path is None:
        log.error("Sound not found: %s", name)
        return

    gain = sound_gain(name)
    scaled = _scaled_copy(path, gain) if gain < 0.999 else None
    try:
        play_path = scaled or path
        if alsa_device == BLUETOOTH_OUTPUT_ID:
            play_via_bluetooth(play_path)
            return

        with aplay_lock:
            log.debug("aplay sound start: %s on %s", name, alsa_device)
            try:
                r = subprocess.run(["aplay", "-D", alsa_device, play_path],
                                   capture_output=True, text=True, timeout=15)
                if r.returncode != 0:
                    log.error("aplay sound failed (rc=%d): %s", r.returncode, r.stderr.strip())
                else:
                    log.debug("aplay sound done: %s", name)
            except subprocess.TimeoutExpired:
                log.error("aplay timed out after 15s playing %s on %s", name, alsa_device)
    finally:
        if scaled:
            os.unlink(scaled)


def save_recording(audio, sr):
    """Save boom audio as a WAV file in RECORDINGS_DIR."""
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    filename = f"boom_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.wav"
    path = os.path.join(RECORDINGS_DIR, filename)
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak
    audio_int16 = (audio * 32767).astype(np.int16)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(audio_int16.tobytes())
    log.info("Saved recording: %s", filename)
    return filename


def find_ps4_controller():
    try:
        import evdev
        for path in evdev.list_devices():
            dev = evdev.InputDevice(path)
            name = dev.name.lower()
            if any(k in name for k in ["wireless controller", "dualshock", "sony"]):
                caps = dev.capabilities()
                if evdev.ecodes.EV_FF in caps:
                    return dev
            dev.close()
    except Exception as e:
        log.error("Error scanning for PS4 controller: %s", e)
    return None


def vibrate_ps4(duration=2.0, intensity=100):
    import evdev
    from evdev import ecodes, ff

    dev = find_ps4_controller()
    if dev is None:
        log.error("No PS4 controller found")
        return

    try:
        mag = int(0xFFFF * max(0, min(100, intensity)) / 100)
        rumble = ff.Rumble(strong_magnitude=mag, weak_magnitude=mag)
        effect = ff.Effect(
            ecodes.FF_RUMBLE,
            -1, 0,
            ff.Trigger(0, 0),
            ff.Replay(int(duration * 1000), 0),
            ff.EffectType(ff_rumble_effect=rumble),
        )
        effect_id = dev.upload_effect(effect)
        dev.write(ecodes.EV_FF, effect_id, 1)
        time.sleep(duration)
        dev.write(ecodes.EV_FF, effect_id, 0)
        dev.erase_effect(effect_id)
        dev.close()
        log.info("PS4 vibration completed (%.1fs)", duration)
    except Exception as e:
        log.error("PS4 vibration failed: %s", e)
        try:
            dev.close()
        except Exception:
            pass


def get_alsa_card():
    alsa = state["config"].get("alsa_device", "plughw:1,0")
    try:
        return alsa.split(":")[1].split(",")[0]
    except (IndexError, ValueError):
        return "1"


def get_bt_volume():
    """Return (level, max) for the connected Bluetooth speaker's own volume, on a 0-100 scale."""
    sink = get_bluetooth_sink()
    if sink:
        try:
            r = subprocess.run(["pactl", "get-sink-volume", sink],
                               capture_output=True, text=True, timeout=5)
            m = re.search(r"(\d+)%", r.stdout)
            if m:
                return int(m.group(1)), 100
        except Exception as e:
            log.error("Error reading Bluetooth volume: %s", e)
    return 100, 100


def set_bt_volume(level):
    sink = get_bluetooth_sink()
    if sink:
        subprocess.run(["pactl", "set-sink-volume", sink, f"{level}%"], capture_output=True, timeout=5)


def get_alsa_volume_control(card):
    """Find a usable playback volume control name for an ALSA card — varies per device."""
    try:
        r = subprocess.run(["amixer", "-c", card, "scontrols"], capture_output=True, text=True, timeout=5)
        names = re.findall(r"'([^']+)'", r.stdout)
        if not names:
            return None
        for preferred in ["Speaker", "Master", "PCM", "Headphone", "Playback"]:
            for n in names:
                if preferred.lower() in n.lower():
                    return n
        return names[0]
    except Exception as e:
        log.error("Error listing mixer controls for card %s: %s", card, e)
        return None


def get_volume():
    """Return (level, max) on a 0-100 scale for whichever output is currently selected."""
    if state["config"].get("alsa_device") == BLUETOOTH_OUTPUT_ID:
        return get_bt_volume()
    card = get_alsa_card()
    control = get_alsa_volume_control(card)
    if control:
        try:
            r = subprocess.run(["amixer", "-c", card, "sget", control],
                               capture_output=True, text=True, timeout=5)
            m = re.search(r"\[(\d+)%\]", r.stdout)
            if m:
                return int(m.group(1)), 100
        except Exception as e:
            log.error("Error reading volume for card %s: %s", card, e)
    return 100, 100


def set_volume(level):
    if state["config"].get("alsa_device") == BLUETOOTH_OUTPUT_ID:
        set_bt_volume(level)
        return
    card = get_alsa_card()
    control = get_alsa_volume_control(card)
    if control:
        subprocess.run(["amixer", "-c", card, "sset", control, f"{level}%"], capture_output=True, timeout=5)


def compute_stats():
    """Compute boom statistics from history."""
    history = state["history"]
    now = datetime.now()

    # Booms per hour for last 24h (indexed 0-23)
    hourly_counts = [0] * 24
    hourly_labels = []
    for i in range(23, -1, -1):
        dt = now - timedelta(hours=i)
        hourly_labels.append(f"{dt.hour:02d}:00")

    for item in history:
        try:
            dt = datetime.strptime(f"{item['date']} {item['time']}", "%Y-%m-%d %H:%M:%S")
            diff = (now - dt).total_seconds()
            if 0 <= diff < 86400:
                idx = 23 - int(diff // 3600)
                if 0 <= idx < 24:
                    hourly_counts[idx] += 1
        except (ValueError, KeyError):
            pass

    # Booms per day for last 7 days
    daily_counts = []
    daily_labels = []
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        daily_labels.append(d[5:])  # MM-DD
        count = sum(1 for h in history if h.get("date") == d)
        daily_counts.append(count)

    week_total = sum(daily_counts)

    # Weekday x hour heatmap over the last 28 days (rows Mon..Sun)
    heatmap = [[0] * 24 for _ in range(7)]
    for item in history:
        try:
            dt = datetime.strptime(f"{item['date']} {item['time']}", "%Y-%m-%d %H:%M:%S")
        except (ValueError, KeyError):
            continue
        if 0 <= (now - dt).days < 28:
            heatmap[dt.weekday()][dt.hour] += 1

    return {
        "heatmap": heatmap,
        "hourly": {"labels": hourly_labels, "data": hourly_counts},
        "daily": {"labels": daily_labels, "data": daily_counts},
        "total": len(history),
        "today": state["today_count"],
        "week": week_total,
    }


# --- Flask routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/recordings-list")
def recordings_list():
    files = []
    if os.path.exists(RECORDINGS_DIR):
        for f in sorted(os.listdir(RECORDINGS_DIR), reverse=True)[:100]:
            if f.endswith(".wav"):
                size = os.path.getsize(os.path.join(RECORDINGS_DIR, f))
                files.append({"name": f, "size": size})
    return jsonify(files)


@app.route("/recordings/<path:filename>")
def serve_recording(filename):
    return send_from_directory(RECORDINGS_DIR, filename)


def history_since(days):
    """History entries from the last `days` days (all of them if days <= 0)."""
    if days <= 0:
        return list(state["history"])
    cutoff = str(date.today() - timedelta(days=days - 1))
    return [h for h in state["history"] if h.get("date", "") >= cutoff]


def _days_arg(default):
    try:
        return max(0, int(request.args.get("days", default)))
    except ValueError:
        return default


@app.route("/history.csv")
def history_csv():
    items = history_since(_days_arg(0))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "time", "level_rms", "level_db", "duration_s", "sound", "muted", "recording"])
    for h in items:
        rms_v = h.get("rms", 0)
        w.writerow([h.get("date", ""), h.get("time", ""), f"{rms_v:.4f}", f"{to_db(rms_v):.1f}",
                    f"{h.get('duration', 0):.2f}", h.get("mode", ""),
                    "yes" if h.get("muted") else "no", h.get("recording", "")])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=noisyneighbors-history.csv"})


@app.route("/report")
def report():
    """Printable evidence report (use the browser's Print > Save as PDF)."""
    days = _days_arg(30)
    items = history_since(days)
    per_day = {}
    per_hour = [0] * 24
    loudest = None
    for h in items:
        per_day[h.get("date", "?")] = per_day.get(h.get("date", "?"), 0) + 1
        try:
            per_hour[int(h["time"][:2])] += 1
        except (KeyError, ValueError, IndexError):
            pass
        if loudest is None or h.get("rms", 0) > loudest.get("rms", 0):
            loudest = h
    peak_hour = max(range(24), key=lambda i: per_hour[i]) if items else None
    events = [dict(h, db=to_db(h.get("rms", 0))) for h in reversed(items[-500:])]
    return render_template(
        "report.html",
        days=days, total=len(items), generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        per_day=sorted(per_day.items(), reverse=True),
        peak_hour=peak_hour, peak_hour_count=per_hour[peak_hour] if peak_hour is not None else 0,
        loudest_db=to_db(loudest["rms"]) if loudest else None,
        loudest_when=f"{loudest.get('date', '')} {loudest.get('time', '')}" if loudest else "",
        events=events, truncated=len(items) > 500,
    )


@app.route("/sounds/upload", methods=["POST"])
def upload_sound():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"ok": False, "error": "No file received"}), 400
    data = f.read()
    try:
        with wave.open(io.BytesIO(data)) as w:
            ok_fmt = w.getsampwidth() == 2 and w.getnchannels() in (1, 2)
            seconds = w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, EOFError):
        return jsonify({"ok": False, "error": "Not a valid WAV file"}), 400
    if not ok_fmt:
        return jsonify({"ok": False, "error": "WAV must be 16-bit PCM, mono or stereo"}), 400
    if seconds > MAX_CUSTOM_SOUND_SECONDS:
        return jsonify({"ok": False, "error": f"Sound is longer than {MAX_CUSTOM_SOUND_SECONDS}s"}), 400
    name = re.sub(r"[^a-z0-9_-]", "_", os.path.splitext(f.filename)[0].lower())[:32] or "sound"
    if name in BUILTIN_SOUNDS or name in META_MODES:
        name += "_custom"
    os.makedirs(CUSTOM_SOUNDS_DIR, exist_ok=True)
    with open(os.path.join(CUSTOM_SOUNDS_DIR, f"{name}.wav"), "wb") as out:
        out.write(data)
    log.info("Custom sound uploaded: %s (%.1fs)", name, seconds)
    emit_replay_mode()
    return jsonify({"ok": True, "name": name})


# --- SocketIO handlers ---

def emit_replay_mode():
    cfg = state["config"]
    socketio.emit("replay_mode", {
        "mode": cfg.get("replay_mode", "echo"),
        "night_mode": cfg.get("night_replay_mode", "echo"),
        "available": get_available_modes(),
        "custom": list_custom_sounds(),
        "gains": cfg.get("sound_gains", {}),
    })


@socketio.on("connect")
def on_connect():
    if not auth_ok(request.authorization):
        return False
    cfg = state["config"]
    socketio.emit("config", {
        "threshold": cfg.get("threshold", 0.15),
        "cooldown_seconds": cfg.get("cooldown_seconds", 5),
        "pre_boom_seconds": cfg.get("pre_boom_seconds", 1.0),
        "post_boom_seconds": cfg.get("post_boom_seconds", 1.5),
    })
    socketio.emit("enabled_state", {"enabled": state["enabled"]})
    emit_replay_mode()
    emit_pause_state()
    # PS4 controller status
    ps4 = find_ps4_controller()
    ps4_connected = ps4 is not None
    if ps4:
        ps4.close()
    socketio.emit("ps4_status", {
        "connected": ps4_connected,
        "enabled": cfg.get("ps4_vibration", False),
        "intensity": cfg.get("vibration_intensity", 100),
    })
    socketio.emit("input_devices", {
        "devices": list_input_devices(),
        "current": cfg.get("device"),
    })
    socketio.emit("alsa_devices", {
        "devices": list_output_devices(),
        "current": cfg.get("alsa_device", ""),
    })
    level, max_vol = get_volume()
    socketio.emit("volume", {"level": level, "max": max_vol})
    # Extended config (new features)
    socketio.emit("extended_config", {
        "schedule_enabled": cfg.get("schedule_enabled", False),
        "schedule_start": cfg.get("schedule_start", "22:00"),
        "schedule_end": cfg.get("schedule_end", "08:00"),
        "night_mode_enabled": cfg.get("night_mode_enabled", False),
        "night_mode_start": cfg.get("night_mode_start", "22:00"),
        "night_mode_end": cfg.get("night_mode_end", "08:00"),
        "night_threshold": cfg.get("night_threshold", 0.10),
        "night_replay_mode": cfg.get("night_replay_mode", "echo"),
        "max_booms_per_hour": cfg.get("max_booms_per_hour", 0),
        "save_recordings": cfg.get("save_recordings", False),
        "strike_mode_enabled": cfg.get("strike_mode_enabled", False),
        "strike_min_interval": cfg.get("strike_min_interval", 60),
        "strike_max_interval": cfg.get("strike_max_interval", 300),
        "filter_enabled": cfg.get("filter_enabled", False),
        "filter_low_hz": cfg.get("filter_low_hz", 20),
        "filter_high_hz": cfg.get("filter_high_hz", 250),
        "adaptive_threshold": cfg.get("adaptive_threshold", False),
        "notify_enabled": cfg.get("notify_enabled", False),
        "notify_url": cfg.get("notify_url", ""),
        "notify_on_boom": cfg.get("notify_on_boom", True),
        "notify_on_limit": cfg.get("notify_on_limit", True),
        "password_protected": bool(cfg.get("dashboard_password")),
    })
    socketio.emit("bt_status_result", get_bt_status())
    socketio.emit("bt_paired_devices", {"devices": list_paired_bt_devices()})
    # Today's history
    if state["today_date"] != str(date.today()):
        state["today_date"] = str(date.today())
        state["today_count"] = 0
    today = str(date.today())
    today_items = [h for h in state["history"] if h.get("date") == today]
    state["today_count"] = len(today_items)
    socketio.emit("history", {
        "items": list(reversed(today_items[-50:])),
        "today_count": state["today_count"],
    })
    # Stats
    socketio.emit("stats", compute_stats())


@socketio.on("save_config")
def on_save_config(data):
    try:
        t = float(data["threshold"])
        cd = int(data["cooldown_seconds"])
        pre = float(data["pre_boom_seconds"])
        post = float(data["post_boom_seconds"])
    except (TypeError, ValueError) as e:
        log.warning("Invalid config ignored: %s", e)
        return
    cfg = state["config"]
    cfg["threshold"] = t
    cfg["cooldown_seconds"] = cd
    cfg["pre_boom_seconds"] = pre
    cfg["post_boom_seconds"] = post
    save_config(cfg)
    state["config"] = cfg
    log.info("Config updated from dashboard")


@socketio.on("set_volume")
def on_set_volume(data):
    level = int(data["level"])
    set_volume(level)
    log.info("Volume set to %d from dashboard", level)
    if state["config"].get("alsa_device") == BLUETOOTH_OUTPUT_ID:
        return
    # Jabra SPEAK 410 briefly drops off ALSA after amixer, which resets its own volume —
    # restart the InputStream to recapture the reconnected device, then report back
    # whatever volume it actually settled on instead of fighting to force an exact value.
    def _restart_after_volume():
        time.sleep(2)
        state["restart_audio"] = True
        log.info("Audio restarting after device reconnect")
        time.sleep(2)
        _emit_volume()
    threading.Thread(target=_restart_after_volume, daemon=True).start()


@socketio.on("set_replay_mode")
def on_set_replay_mode(data):
    mode = data["mode"]
    if mode in get_available_modes():
        state["config"]["replay_mode"] = mode
        save_config(state["config"])
        emit_replay_mode()
        log.info("Replay mode set to '%s' from dashboard", mode)


@socketio.on("test_sound")
def on_test_sound():
    def _play():
        cfg = state["config"]
        alsa_device = cfg.get("alsa_device") or detect_alsa_device()
        mode = resolve_replay_mode(cfg.get("replay_mode", "echo"))

        # Non-blocking: skip if that output is already playing something
        lock = pulse_lock if alsa_device == BLUETOOTH_OUTPUT_ID else aplay_lock
        if not lock.acquire(blocking=False):
            log.info("Test sound skipped — already playing")
            return
        lock.release()

        cb = state.get("cb_state")
        if cb is not None:
            cb["paused"] = True
        try:
            if mode == "echo":
                sr = 48000
                t = np.linspace(0, 1.0, sr, dtype=np.float32)
                audio = np.sin(2 * np.pi * 440 * t) * 0.8
                play_audio(audio, sr, alsa_device, sr)
            else:
                play_sound_file(mode, alsa_device)
            log.info("Test sound played (mode=%s)", mode)
        finally:
            time.sleep(0.3)
            if cb is not None:
                cb["paused"] = not state["enabled"]
    threading.Thread(target=_play, daemon=True).start()


@socketio.on("test_vibration")
def on_test_vibration():
    intensity = state["config"].get("vibration_intensity", 100)
    cb = state.get("cb_state")
    def _vibrate():
        if cb is not None:
            cb["paused"] = True
        try:
            vibrate_ps4(1.0, intensity)
        finally:
            time.sleep(0.2)
            if cb is not None:
                cb["paused"] = not state["enabled"]
    threading.Thread(target=_vibrate, daemon=True).start()


@socketio.on("toggle_ps4_vibration")
def on_toggle_ps4_vibration(data):
    enabled = bool(data["enabled"])
    state["config"]["ps4_vibration"] = enabled
    save_config(state["config"])
    log.info("PS4 vibration %s from dashboard", "enabled" if enabled else "disabled")


@socketio.on("set_vibration_intensity")
def on_set_vibration_intensity(data):
    intensity = int(data["intensity"])
    state["config"]["vibration_intensity"] = intensity
    save_config(state["config"])
    log.info("Vibration intensity set to %d%% from dashboard", intensity)


@socketio.on("set_input_device")
def on_set_input_device(data):
    device = int(data["device"])
    state["config"]["device"] = device
    save_config(state["config"])
    state["restart_audio"] = True
    socketio.emit("input_devices", {
        "devices": list_input_devices(),
        "current": device,
    })
    log.info("Input device set to %d from dashboard, restarting audio...", device)


@socketio.on("set_alsa_device")
def on_set_alsa_device(data):
    device = data["device"]
    state["config"]["alsa_device"] = device
    save_config(state["config"])
    socketio.emit("alsa_devices", {
        "devices": list_output_devices(),
        "current": device,
    })
    level, max_vol = get_volume()
    socketio.emit("volume", {"level": level, "max": max_vol})
    log.info("Output device set to '%s' from dashboard", device)


@socketio.on("toggle_enabled")
def on_toggle_enabled():
    enabled = not state["enabled"]
    state["pause_until"] = None
    set_enabled(enabled)
    emit_pause_state()
    log.info("NoisyNeighbors %s from dashboard", "enabled" if enabled else "disabled")


@socketio.on("pause_for")
def on_pause_for(data):
    """Disable detection for N minutes, or until 08:00 tomorrow ({"until": "tomorrow"})."""
    now = datetime.now()
    if data.get("until") == "tomorrow":
        wake = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        until = wake.timestamp()
    else:
        try:
            minutes = int(data.get("minutes", 30))
        except (TypeError, ValueError):
            return
        until = time.time() + max(1, min(minutes, 24 * 60)) * 60
    state["pause_until"] = until
    set_enabled(False)
    emit_pause_state()
    log.info("Paused until %s from dashboard", datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M"))


@socketio.on("save_detection")
def on_save_detection(data):
    cfg = state["config"]
    try:
        low = float(data.get("filter_low_hz", 20))
        high = float(data.get("filter_high_hz", 250))
    except (TypeError, ValueError):
        return
    low = max(10.0, min(low, 7900.0))
    high = max(low + 10.0, min(high, 8000.0))
    cfg["filter_enabled"] = bool(data.get("filter_enabled", False))
    cfg["filter_low_hz"] = low
    cfg["filter_high_hz"] = high
    cfg["adaptive_threshold"] = bool(data.get("adaptive_threshold", False))
    save_config(cfg)
    log.info("Detection saved: filter=%s %.0f-%.0f Hz adaptive=%s",
             cfg["filter_enabled"], low, high, cfg["adaptive_threshold"])


@socketio.on("save_notify")
def on_save_notify(data):
    cfg = state["config"]
    url = str(data.get("url", "")).strip()
    if url and not url.startswith(("http://", "https://")):
        return
    cfg["notify_enabled"] = bool(data.get("enabled", False))
    cfg["notify_url"] = url
    cfg["notify_on_boom"] = bool(data.get("on_boom", True))
    cfg["notify_on_limit"] = bool(data.get("on_limit", True))
    save_config(cfg)
    log.info("Notifications saved: enabled=%s", cfg["notify_enabled"])


@socketio.on("test_notify")
def on_test_notify():
    notify("NoisyNeighbors test", "Test notification from the dashboard", force=True)


@socketio.on("set_sound_gain")
def on_set_sound_gain(data):
    name = data.get("name")
    if name not in BUILTIN_SOUNDS + list_custom_sounds():
        return
    try:
        gain = float(np.clip(float(data.get("gain", 1.0)), 0.1, 1.0))
    except (TypeError, ValueError):
        return
    state["config"].setdefault("sound_gains", {})[name] = round(gain, 2)
    save_config(state["config"])
    emit_replay_mode()


@socketio.on("delete_custom_sound")
def on_delete_custom_sound(data):
    name = data.get("name", "")
    if name not in list_custom_sounds():
        return
    os.unlink(os.path.join(CUSTOM_SOUNDS_DIR, f"{name}.wav"))
    cfg = state["config"]
    for key in ("replay_mode", "night_replay_mode"):
        if cfg.get(key) == name:
            cfg[key] = "echo"
    cfg.get("sound_gains", {}).pop(name, None)
    save_config(cfg)
    log.info("Custom sound deleted: %s", name)
    emit_replay_mode()


@socketio.on("save_schedule")
def on_save_schedule(data):
    cfg = state["config"]
    cfg["schedule_enabled"] = bool(data.get("enabled", False))
    cfg["schedule_start"] = data.get("start", "22:00")
    cfg["schedule_end"] = data.get("end", "08:00")
    save_config(cfg)
    log.info("Schedule saved: enabled=%s %s-%s",
             cfg["schedule_enabled"], cfg["schedule_start"], cfg["schedule_end"])


@socketio.on("save_night_mode")
def on_save_night_mode(data):
    cfg = state["config"]
    cfg["night_mode_enabled"] = bool(data.get("enabled", False))
    cfg["night_mode_start"] = data.get("start", "22:00")
    cfg["night_mode_end"] = data.get("end", "08:00")
    try:
        cfg["night_threshold"] = float(data.get("threshold", 0.10))
    except (TypeError, ValueError):
        pass
    mode = data.get("replay_mode", "echo")
    if mode in get_available_modes():
        cfg["night_replay_mode"] = mode
    save_config(cfg)
    log.info("Night mode saved: enabled=%s %s-%s", cfg["night_mode_enabled"],
             cfg["night_mode_start"], cfg["night_mode_end"])


@socketio.on("save_limits")
def on_save_limits(data):
    cfg = state["config"]
    try:
        cfg["max_booms_per_hour"] = int(data.get("max_booms_per_hour", 0))
    except (TypeError, ValueError):
        pass
    save_config(cfg)
    log.info("Limits saved: max_booms_per_hour=%d", cfg["max_booms_per_hour"])


@socketio.on("set_save_recordings")
def on_set_save_recordings(data):
    cfg = state["config"]
    cfg["save_recordings"] = bool(data.get("enabled", False))
    save_config(cfg)
    log.info("Save recordings: %s", cfg["save_recordings"])


@socketio.on("calibrate_threshold")
def on_calibrate_threshold():
    if state["calibrating"]:
        return
    state["calibration_samples"] = []
    state["calibrating"] = True
    socketio.emit("calibration_started", {"duration": 5})
    log.info("Threshold calibration started (5s)")

    def _finish():
        time.sleep(5)
        state["calibrating"] = False
        samples = state["calibration_samples"]
        if len(samples) < 10:
            socketio.emit("calibration_done", {"error": "Not enough audio samples"})
            return
        mean = float(np.mean(samples))
        std = float(np.std(samples))
        new_threshold = round(float(np.clip(mean + 3 * std, 0.01, 1.0)), 4)
        state["config"]["threshold"] = new_threshold
        save_config(state["config"])
        socketio.emit("calibration_done", {"threshold": new_threshold})
        log.info("Calibrated threshold: %.4f (mean=%.4f, std=%.4f)", new_threshold, mean, std)

    threading.Thread(target=_finish, daemon=True).start()


@socketio.on("get_stats")
def on_get_stats():
    socketio.emit("stats", compute_stats())


@socketio.on("save_strike_mode")
def on_save_strike_mode(data):
    cfg = state["config"]
    cfg["strike_mode_enabled"] = bool(data.get("enabled", False))
    try:
        cfg["strike_min_interval"] = float(data.get("min_interval", 60))
        cfg["strike_max_interval"] = float(data.get("max_interval", 300))
    except (TypeError, ValueError):
        pass
    save_config(cfg)
    log.info("Strike Mode: enabled=%s min=%.0fs max=%.0fs",
             cfg["strike_mode_enabled"], cfg["strike_min_interval"], cfg["strike_max_interval"])


def _emit_output_devices():
    socketio.emit("alsa_devices", {
        "devices": list_output_devices(),
        "current": state["config"].get("alsa_device", ""),
    })


def _emit_volume():
    level, max_vol = get_volume()
    socketio.emit("volume", {"level": level, "max": max_vol})


@socketio.on("bt_scan")
def on_bt_scan():
    def _scan():
        socketio.emit("bt_scan_started", {})
        devices = scan_bt_devices(duration=8)
        socketio.emit("bt_scan_result", {"devices": devices})
    threading.Thread(target=_scan, daemon=True).start()


@socketio.on("bt_pair")
def on_bt_pair(data):
    mac = data.get("mac", "")
    if not MAC_RE.match(mac):
        return
    def _pair():
        result = pair_bt_device(mac)
        result["mac"] = mac
        socketio.emit("bt_pair_result", result)
        socketio.emit("bt_status_result", get_bt_status())
        socketio.emit("bt_paired_devices", {"devices": list_paired_bt_devices()})
        _emit_output_devices()
        if result["ok"]:
            time.sleep(1.5)  # give PulseAudio a moment to create the A2DP sink
            _emit_volume()
    threading.Thread(target=_pair, daemon=True).start()


@socketio.on("bt_connect")
def on_bt_connect(data):
    mac = data.get("mac", "")
    if not MAC_RE.match(mac):
        return
    def _connect():
        result = connect_bt_device(mac)
        result["mac"] = mac
        socketio.emit("bt_connect_result", result)
        socketio.emit("bt_status_result", get_bt_status())
        _emit_output_devices()
        if result["ok"]:
            time.sleep(1.5)  # give PulseAudio a moment to create the A2DP sink
            _emit_volume()
    threading.Thread(target=_connect, daemon=True).start()


@socketio.on("bt_restart_adapter")
def on_bt_restart_adapter():
    def _restart():
        result = restart_bluetooth_adapter()
        socketio.emit("bt_restart_result", result)
        time.sleep(1)
        socketio.emit("bt_status_result", get_bt_status())
        socketio.emit("bt_paired_devices", {"devices": list_paired_bt_devices()})
        _emit_output_devices()
        _emit_volume()
    threading.Thread(target=_restart, daemon=True).start()


@socketio.on("bt_forget")
def on_bt_forget(data):
    mac = data.get("mac", "")
    if not MAC_RE.match(mac):
        return
    def _forget():
        result = forget_bt_device(mac)
        result["mac"] = mac
        socketio.emit("bt_forget_result", result)
        socketio.emit("bt_status_result", get_bt_status())
        socketio.emit("bt_paired_devices", {"devices": list_paired_bt_devices()})
        _emit_output_devices()
    threading.Thread(target=_forget, daemon=True).start()


@socketio.on("delete_recording")
def on_delete_recording(data):
    filename = data.get("name", "")
    if not filename.endswith(".wav") or "/" in filename or "\\" in filename:
        return
    path = os.path.join(RECORDINGS_DIR, filename)
    if os.path.exists(path):
        os.unlink(path)
        log.info("Deleted recording: %s", filename)
        changed = False
        for h in state["history"]:
            if h.get("recording") == filename:
                h["recording"] = None
                changed = True
        if changed:
            save_history(state["history"])
        socketio.emit("recording_deleted", {"name": filename})


# --- Audio detection thread ---

def audio_loop():
    cfg = state["config"]
    channels = cfg["channels"]
    device = cfg["device"]
    alsa_device = cfg.get("alsa_device") or None
    out_sr = cfg.get("output_sample_rate", 48000)

    if alsa_device is None:
        alsa_device = detect_alsa_device()
        cfg["alsa_device"] = alsa_device
        save_config(cfg)
        log.info("Auto-detected ALSA output: %s", alsa_device)

    if device is not None and not is_valid_input_device(device, channels):
        log.warning("Configured input device %d is no longer valid (ALSA enumeration "
                    "likely changed) — re-detecting", device)
        device = None

    if device is None:
        idx, dev_info = detect_device()
        if idx is not None:
            device = idx
            cfg["device"] = idx
            save_config(cfg)
            log.info("Auto-detected device: [%d] %s", idx, dev_info["name"])
        else:
            log.error("No USB device detected.")
            return

    dev_info = sd.query_devices(device)
    sr = cfg.get("sample_rate")
    if sr is None or sr == 0:
        sr = int(dev_info["default_samplerate"])
        log.info("Auto-detected sample rate: %d Hz", sr)

    block_size = 1024
    boom_queue = queue.Queue()

    cb_state = {
        "write_pos": 0,
        "boom_detected": False,
        "post_recorded": 0,
        "paused": not state["enabled"],
        "post_recording": None,
    }
    state["cb_state"] = cb_state

    def get_cfg_values():
        c = state["config"]
        try:
            t = float(c.get("threshold") or 0.15)
            # Night mode: use night threshold if in night window
            if c.get("night_mode_enabled", False):
                nt = c.get("night_threshold")
                if nt and is_in_time_range(
                    c.get("night_mode_start", "22:00"),
                    c.get("night_mode_end", "08:00")
                ):
                    t = float(nt)
            pre = int(sr * float(c.get("pre_boom_seconds") or 1.0))
            post = int(sr * float(c.get("post_boom_seconds") or 1.5))
            cd = c.get("cooldown_seconds")
            cd = 5.0 if cd is None else float(cd)  # 0 is a valid "no cooldown"
        except (TypeError, ValueError):
            t, pre, post, cd = 0.15, int(sr * 1.0), int(sr * 1.5), 5
        return t, pre, post, cd

    buffer_len = int(sr * 3)
    ring = np.zeros((buffer_len, channels), dtype=np.float32)
    rms_counter = 0
    # Ambient-noise tracker for the adaptive threshold (running mean/variance)
    ambient = {"mean": 0.0, "var": 0.0, "n": 0}

    def measure(block):
        """Detection level: broadband RMS, or RMS inside the configured band."""
        c = state["config"]
        if c.get("filter_enabled", False):
            try:
                return band_level(block, sr,
                                  float(c.get("filter_low_hz", 20)),
                                  float(c.get("filter_high_hz", 250)))
            except (TypeError, ValueError):
                pass
        return float(rms(block))

    def adapt(level, threshold):
        """Raise the threshold above steady background noise (never lowers it)."""
        a = ambient
        if a["n"] == 0:
            a["mean"], a["var"] = level, 0.0
        if level <= threshold:
            alpha = 0.002
            d = level - a["mean"]
            a["mean"] += alpha * d
            a["var"] = (1 - alpha) * (a["var"] + alpha * d * d)
            a["n"] += 1
        if a["n"] < 100:  # ~6s warm-up
            return threshold
        return min(1.0, max(threshold, a["mean"] + 6 * math.sqrt(a["var"])))

    def callback(indata, frames, time_info, status):
        nonlocal rms_counter
        s = cb_state

        try:
            if status:
                log.warning("Audio status: %s", status)

            # Calibration mode: collect ambient RMS samples
            if state["calibrating"]:
                state["calibration_samples"].append(measure(indata))
                return

            if s["paused"]:
                return

            threshold, pre_samples, post_samples, _ = get_cfg_values()

            if s["boom_detected"]:
                remaining = post_samples - s["post_recorded"]
                to_copy = min(frames, remaining)
                s["post_recording"][s["post_recorded"]:s["post_recorded"] + to_copy] = indata[:to_copy]
                s["post_recorded"] += to_copy

                if s["post_recorded"] >= post_samples:
                    s["boom_detected"] = False
                    pre_start = (s["write_pos"] - pre_samples) % buffer_len
                    if pre_start < s["write_pos"]:
                        pre_audio = ring[pre_start:s["write_pos"]].copy()
                    else:
                        pre_audio = np.concatenate([ring[pre_start:], ring[:s["write_pos"]]])
                    boom_audio = np.concatenate([pre_audio, s["post_recording"]])
                    s["paused"] = True
                    boom_queue.put(boom_audio)
                return

            for i in range(frames):
                ring[s["write_pos"]] = indata[i]
                s["write_pos"] = (s["write_pos"] + 1) % buffer_len

            level = measure(indata)
            if state["config"].get("adaptive_threshold", False):
                threshold = adapt(level, threshold)
            state["effective_threshold"] = threshold

            rms_counter += 1
            if rms_counter % 5 == 0:
                socketio.emit("rms", {"level": level, "threshold": threshold})

            if level > threshold:
                log.info("BOOM detected! level=%.4f (threshold=%.4f)", level, threshold)
                socketio.emit("status", {"state": "boom"})
                s["boom_detected"] = True
                s["post_recording"] = np.zeros((post_samples, channels), dtype=np.float32)
                s["post_recorded"] = 0
        except Exception as e:
            log.error("Error in audio callback: %s", e)

    log.info("NoisyNeighbors started")
    log.info("  device=[%s] %s", device, dev_info["name"])
    log.info("  alsa_device=%s  sr=%d  out_sr=%d  channels=%d",
             alsa_device, sr, out_sr, channels)

    try:
        with sd.InputStream(
            samplerate=sr,
            channels=channels,
            dtype="float32",
            blocksize=block_size,
            device=device,
            callback=callback,
        ):
            log.info("Listening... (Ctrl+C to stop)")
            while True:
                if state["restart_audio"]:
                    log.info("Audio restart requested")
                    return
                try:
                    boom_audio = boom_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if boom_audio.ndim == 2 and boom_audio.shape[1] == 1:
                    boom_audio = boom_audio.flatten()

                duration = len(boom_audio) / sr
                boom_rms = float(rms(boom_audio))
                now = datetime.now()

                # Hourly rate limit
                if now.hour != state["current_hour"]:
                    state["current_hour"] = now.hour
                    state["hourly_boom_count"] = 0

                max_per_hour = state["config"].get("max_booms_per_hour", 0)
                limit_reached = max_per_hour > 0 and state["hourly_boom_count"] >= max_per_hour
                if limit_reached:
                    log.info("Hourly limit reached (%d/%d), skipping response", state["hourly_boom_count"], max_per_hour)
                else:
                    state["hourly_boom_count"] += 1

                # Determine effective replay mode (night mode override), then resolve
                # "random"/"escalate" into a concrete sound
                cur_alsa = state["config"].get("alsa_device") or alsa_device
                configured_mode = effective_replay_mode()
                replay_mode = resolve_replay_mode(configured_mode, advance=not limit_reached)
                if configured_mode != state["config"].get("replay_mode", "echo"):
                    log.info("Night mode active, using replay_mode=%s", configured_mode)

                if not limit_reached:
                    log.info("Playing boom (%.2fs, mode=%s)...", duration, replay_mode)
                    socketio.emit("status", {"state": "boom"})

                    # PS4 vibration in parallel
                    if state["config"].get("ps4_vibration", False):
                        intensity = state["config"].get("vibration_intensity", 100)
                        threading.Thread(
                            target=vibrate_ps4,
                            args=(duration, intensity),
                            daemon=True,
                        ).start()

                    if replay_mode == "echo":
                        play_audio(boom_audio, sr, cur_alsa, out_sr)
                    else:
                        play_sound_file(replay_mode, cur_alsa)
                    log.info("Playback finished")

                # Save recording if enabled
                recording = None
                if state["config"].get("save_recordings", False):
                    try:
                        recording = save_recording(boom_audio.copy(), sr)
                    except OSError as e:
                        log.error("Could not save recording: %s", e)

                # Log detection
                detection = {
                    "date": str(now.date()),
                    "time": now.strftime("%H:%M:%S"),
                    "rms": boom_rms,
                    "duration": duration,
                    "mode": None if limit_reached else replay_mode,
                    "muted": limit_reached,
                    "recording": recording,
                }
                state["history"].append(detection)
                save_history(state["history"])

                if state["today_date"] != str(now.date()):
                    state["today_date"] = str(now.date())
                    state["today_count"] = 0
                state["today_count"] += 1

                socketio.emit("boom", dict(
                    detection,
                    today_count=state["today_count"],
                    limit_reached=limit_reached,
                    hourly_count=state["hourly_boom_count"],
                    max_per_hour=max_per_hour,
                ))

                cfg_now = state["config"]
                if limit_reached:
                    if cfg_now.get("notify_on_limit", True) and state["limit_notified_hour"] != (str(now.date()), now.hour):
                        state["limit_notified_hour"] = (str(now.date()), now.hour)
                        notify("NoisyNeighbors: hourly limit reached",
                               f"{max_per_hour} responses this hour; further booms are logged but not answered.")
                elif cfg_now.get("notify_on_boom", True):
                    notify("NoisyNeighbors: boom detected",
                           f"{detection['time']} - {to_db(boom_rms):.0f} dB, played {replay_mode}")

                if not limit_reached:
                    _, _, _, cooldown = get_cfg_values()
                    if cooldown > 0:
                        log.info("Cooldown %ds...", cooldown)
                        socketio.emit("status", {"state": "cooldown"})
                        time.sleep(cooldown)

                cb_state["paused"] = not state["enabled"]
                status_str = "listening" if state["enabled"] else "disabled"
                socketio.emit("status", {"state": status_str})
                log.info("Listening resumed (enabled=%s)", state["enabled"])

    except KeyboardInterrupt:
        log.info("Shutdown requested")
    except Exception as e:
        log.error("Error: %s", e)
        raise


def main():
    if "--list-devices" in sys.argv:
        list_devices()
        return

    os.makedirs(RECORDINGS_DIR, exist_ok=True)

    cfg = load_config()
    state["config"] = cfg
    state["history"] = load_history()

    today = str(date.today())
    state["today_date"] = today
    state["today_count"] = len([h for h in state["history"] if h.get("date") == today])

    # Start scheduler thread
    threading.Thread(target=scheduler_loop, daemon=True).start()
    # Start strike mode thread
    threading.Thread(target=strike_loop, daemon=True).start()
    # Start timed-pause expiry thread
    threading.Thread(target=pause_loop, daemon=True).start()

    # Start audio detection thread (auto-restarts)
    def audio_loop_wrapper():
        while True:
            try:
                audio_loop()
            except Exception as e:
                log.error("Audio loop crashed, restarting: %s", e)
            state["restart_audio"] = False
            time.sleep(0.5)

    threading.Thread(target=audio_loop_wrapper, daemon=True).start()

    port = cfg.get("web_port", 5000)
    log.info("Web dashboard on http://0.0.0.0:%d", port)
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
