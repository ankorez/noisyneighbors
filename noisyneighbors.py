#!/usr/bin/env python3
"""NoisyNeighbors - Detects neighbor booms and plays them back."""

import json
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
from datetime import datetime, date, timedelta

import numpy as np
import sounddevice as sd
from flask import Flask, render_template, send_from_directory, jsonify
from flask_socketio import SocketIO

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("noisyneighbors")

# Flask app
app = Flask(__name__)
app.config["SECRET_KEY"] = "noisyneighbors"
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
        mode = cfg.get("replay_mode", "echo")
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
        if not cfg.get("schedule_enabled", False):
            continue
        start = cfg.get("schedule_start", "22:00")
        end = cfg.get("schedule_end", "08:00")
        should_be_enabled = is_in_time_range(start, end)
        if should_be_enabled != state["enabled"]:
            state["enabled"] = should_be_enabled
            cb = state.get("cb_state")
            if cb is not None:
                cb["paused"] = not should_be_enabled
            status = "listening" if should_be_enabled else "disabled"
            socketio.emit("enabled_state", {"enabled": should_be_enabled})
            socketio.emit("status", {"state": status})
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
    for i, d in enumerate(devices):
        name = d["name"].lower()
        if "usb" in name and d["max_input_channels"] > 0:
            return i, d
    return None, None


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


def play_audio(audio, sr, alsa_device, out_sr):
    if sr != out_sr:
        n_samples = int(len(audio) * out_sr / sr)
        indices = np.linspace(0, len(audio) - 1, n_samples)
        audio = np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)

    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak

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
AVAILABLE_SOUNDS = ["echo", "alarm", "doorbell", "hammer", "honk", "siren", "sandstorm", "hammering"]

# Prevent simultaneous aplay calls (USB device can't handle two streams at once)
aplay_lock = threading.Lock()


def play_sound_file(name, alsa_device):
    path = os.path.join(SOUNDS_DIR, f"{name}.wav")
    if not os.path.exists(path):
        log.error("Sound not found: %s", path)
        return

    if alsa_device == BLUETOOTH_OUTPUT_ID:
        play_via_bluetooth(path)
        return

    with aplay_lock:
        log.debug("aplay sound start: %s on %s", name, alsa_device)
        try:
            r = subprocess.run(["aplay", "-D", alsa_device, path],
                               capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                log.error("aplay sound failed (rc=%d): %s", r.returncode, r.stderr.strip())
            else:
                log.debug("aplay sound done: %s", name)
        except subprocess.TimeoutExpired:
            log.error("aplay timed out after 15s playing %s on %s", name, alsa_device)


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

    return {
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


# --- SocketIO handlers ---

@socketio.on("connect")
def on_connect():
    cfg = state["config"]
    socketio.emit("config", {
        "threshold": cfg.get("threshold", 0.15),
        "cooldown_seconds": cfg.get("cooldown_seconds", 5),
        "pre_boom_seconds": cfg.get("pre_boom_seconds", 1.0),
        "post_boom_seconds": cfg.get("post_boom_seconds", 1.5),
    })
    socketio.emit("enabled_state", {"enabled": state["enabled"]})
    socketio.emit("replay_mode", {
        "mode": cfg.get("replay_mode", "echo"),
        "available": AVAILABLE_SOUNDS,
    })
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
    if mode in AVAILABLE_SOUNDS:
        state["config"]["replay_mode"] = mode
        save_config(state["config"])
        socketio.emit("replay_mode", {
            "mode": mode,
            "available": AVAILABLE_SOUNDS,
        })
        log.info("Replay mode set to '%s' from dashboard", mode)


@socketio.on("test_sound")
def on_test_sound():
    def _play():
        cfg = state["config"]
        alsa_device = cfg.get("alsa_device") or detect_alsa_device()
        mode = cfg.get("replay_mode", "echo")

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
    state["enabled"] = not state["enabled"]
    enabled = state["enabled"]
    cb = state.get("cb_state")
    if cb is not None:
        cb["paused"] = not enabled
    status = "listening" if enabled else "disabled"
    socketio.emit("enabled_state", {"enabled": enabled})
    socketio.emit("status", {"state": status})
    log.info("NoisyNeighbors %s from dashboard", "enabled" if enabled else "disabled")


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
    if mode in AVAILABLE_SOUNDS:
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

    if device is None:
        idx, dev_info = detect_device()
        if idx is not None:
            device = idx
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
            cd = float(c.get("cooldown_seconds") or 5)
        except (TypeError, ValueError):
            t, pre, post, cd = 0.15, int(sr * 1.0), int(sr * 1.5), 5
        return t, pre, post, cd

    buffer_len = int(sr * 3)
    ring = np.zeros((buffer_len, channels), dtype=np.float32)
    rms_counter = 0

    def callback(indata, frames, time_info, status):
        nonlocal rms_counter
        s = cb_state

        try:
            if status:
                log.warning("Audio status: %s", status)

            # Calibration mode: collect ambient RMS samples
            if state["calibrating"]:
                state["calibration_samples"].append(float(rms(indata)))
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

            level = rms(indata)

            rms_counter += 1
            if rms_counter % 5 == 0:
                socketio.emit("rms", {"level": float(level)})

            if level > threshold:
                log.info("BOOM detected! RMS=%.4f (threshold=%.4f)", level, threshold)
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

                # Determine effective replay mode (night mode override)
                cur_alsa = state["config"].get("alsa_device") or alsa_device
                replay_mode = state["config"].get("replay_mode", "echo")
                if state["config"].get("night_mode_enabled", False):
                    nm_start = state["config"].get("night_mode_start", "22:00")
                    nm_end = state["config"].get("night_mode_end", "08:00")
                    if is_in_time_range(nm_start, nm_end):
                        replay_mode = state["config"].get("night_replay_mode", replay_mode)
                        log.info("Night mode active, using replay_mode=%s", replay_mode)

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
                if state["config"].get("save_recordings", False):
                    threading.Thread(
                        target=save_recording,
                        args=(boom_audio.copy(), sr),
                        daemon=True,
                    ).start()

                # Log detection
                detection = {
                    "date": str(now.date()),
                    "time": now.strftime("%H:%M:%S"),
                    "rms": boom_rms,
                    "duration": duration,
                }
                state["history"].append(detection)
                save_history(state["history"])

                if state["today_date"] != str(now.date()):
                    state["today_date"] = str(now.date())
                    state["today_count"] = 0
                state["today_count"] += 1

                socketio.emit("boom", {
                    "time": detection["time"],
                    "rms": boom_rms,
                    "duration": duration,
                    "today_count": state["today_count"],
                    "limit_reached": limit_reached,
                    "hourly_count": state["hourly_boom_count"],
                    "max_per_hour": max_per_hour,
                })

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
