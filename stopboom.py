#!/usr/bin/env python3
"""StopBoom - Détecte les booms des voisins et les rejoue."""

import json
import os
import sys
import time
import wave
import queue
import logging
import tempfile
import threading
import subprocess
from datetime import datetime, date

import numpy as np
import sounddevice as sd
from flask import Flask, render_template
from flask_socketio import SocketIO

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("stopboom")

# Flask app
app = Flask(__name__)
app.config["SECRET_KEY"] = "stopboom"
socketio = SocketIO(app, async_mode="threading")

# State partagé
state = {
    "status": "listening",  # listening, boom, cooldown
    "history": [],
    "today_count": 0,
    "today_date": str(date.today()),
    "config": {},
    "enabled": True,
    "cb_state": None,
}

CONFIG_PATH = "config.json"
HISTORY_PATH = "history.json"


def load_config():
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
        json.dump(history[-200:], f)


def rms(block):
    return np.sqrt(np.mean(block ** 2))


def list_devices():
    print("\n=== Devices audio disponibles ===\n")
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
    print("Pour config.json :")
    print('  "device" : index du device avec des canaux input (micro)')
    print('  "alsa_device" : "plughw:<card>,0" pour la sortie')
    print()


def detect_device():
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        name = d["name"].lower()
        if "usb" in name and d["max_input_channels"] > 0:
            return i, d
    return None, None


def play_audio(audio, sr, alsa_device, out_sr):
    if sr != out_sr:
        n_samples = int(len(audio) * out_sr / sr)
        indices = np.linspace(0, len(audio) - 1, n_samples)
        audio = np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)

    audio_int16 = (audio * 32767).astype(np.int16)
    stereo = np.column_stack([audio_int16, audio_int16])

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name
        with wave.open(f, "w") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(out_sr)
            w.writeframes(stereo.tobytes())

    subprocess.run(["aplay", "-D", alsa_device, tmp_path], capture_output=True)
    os.unlink(tmp_path)


SOUNDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")

AVAILABLE_SOUNDS = ["echo", "alarm", "doorbell", "hammer", "honk", "siren"]


def play_sound_file(name, alsa_device):
    """Joue un fichier wav prédéfini."""
    path = os.path.join(SOUNDS_DIR, f"{name}.wav")
    if not os.path.exists(path):
        log.error("Son introuvable: %s", path)
        return
    subprocess.run(["aplay", "-D", alsa_device, path], capture_output=True)


def get_alsa_card():
    """Extrait le numéro de carte depuis alsa_device (ex: plughw:1,0 -> 1)."""
    alsa = state["config"].get("alsa_device", "plughw:1,0")
    try:
        return alsa.split(":")[1].split(",")[0]
    except (IndexError, ValueError):
        return "1"


def get_volume():
    """Lit le volume actuel et max via amixer."""
    card = get_alsa_card()
    try:
        result = subprocess.run(
            ["amixer", "-c", card, "cget", "numid=3"],
            capture_output=True, text=True
        )
        output = result.stdout
        # Parse max
        max_vol = 11
        for line in output.split("\n"):
            if "max=" in line:
                for part in line.split(","):
                    if part.strip().startswith("max="):
                        max_vol = int(part.strip().split("=")[1])
            if ": values=" in line:
                level = int(line.strip().split("=")[1])
                return level, max_vol
    except Exception:
        pass
    return 11, 11


def set_volume(level):
    """Change le volume via amixer."""
    card = get_alsa_card()
    subprocess.run(
        ["amixer", "-c", card, "cset", "numid=3", str(level)],
        capture_output=True
    )


# --- Flask routes ---

@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("connect")
def on_connect():
    socketio.emit("config", {
        "threshold": state["config"].get("threshold", 0.15),
        "cooldown_seconds": state["config"].get("cooldown_seconds", 5),
        "pre_boom_seconds": state["config"].get("pre_boom_seconds", 1.0),
        "post_boom_seconds": state["config"].get("post_boom_seconds", 1.5),
    })
    socketio.emit("enabled_state", {"enabled": state["enabled"]})
    socketio.emit("replay_mode", {
        "mode": state["config"].get("replay_mode", "echo"),
        "available": AVAILABLE_SOUNDS,
    })
    # Send current volume
    level, max_vol = get_volume()
    socketio.emit("volume", {"level": level, "max": max_vol})
    # Reset today count if new day
    if state["today_date"] != str(date.today()):
        state["today_date"] = str(date.today())
        state["today_count"] = 0
    # Send recent history
    today = str(date.today())
    today_items = [h for h in state["history"] if h.get("date") == today]
    state["today_count"] = len(today_items)
    socketio.emit("history", {
        "items": list(reversed(today_items[-50:])),
        "today_count": state["today_count"],
    })


@socketio.on("save_config")
def on_save_config(data):
    try:
        t = float(data["threshold"])
        cd = int(data["cooldown_seconds"])
        pre = float(data["pre_boom_seconds"])
        post = float(data["post_boom_seconds"])
    except (TypeError, ValueError) as e:
        log.warning("Config invalide ignorée: %s", e)
        return
    cfg = state["config"]
    cfg["threshold"] = t
    cfg["cooldown_seconds"] = cd
    cfg["pre_boom_seconds"] = pre
    cfg["post_boom_seconds"] = post
    save_config(cfg)
    state["config"] = cfg
    log.info("Config mise à jour depuis le dashboard")


@socketio.on("set_volume")
def on_set_volume(data):
    level = int(data["level"])
    set_volume(level)
    log.info("Volume changé à %d depuis le dashboard", level)


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
        log.info("Mode replay changé à '%s' depuis le dashboard", mode)


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
    log.info("StopBoom %s depuis le dashboard", "activé" if enabled else "désactivé")


# --- Audio detection thread ---

def audio_loop():
    cfg = state["config"]
    channels = cfg["channels"]
    device = cfg["device"]
    alsa_device = cfg.get("alsa_device", "plughw:1,0")
    out_sr = cfg.get("output_sample_rate", 48000)

    if device is None:
        idx, dev_info = detect_device()
        if idx is not None:
            device = idx
            log.info("Device auto-détecté: [%d] %s", idx, dev_info["name"])
        else:
            log.error("Aucun device USB détecté.")
            return

    dev_info = sd.query_devices(device)
    sr = cfg.get("sample_rate")
    if sr is None or sr == 0:
        sr = int(dev_info["default_samplerate"])
        log.info("Sample rate auto-détecté: %d Hz", sr)

    block_size = 1024
    boom_queue = queue.Queue()

    # Mutable state for callback
    cb_state = {
        "write_pos": 0,
        "boom_detected": False,
        "post_recorded": 0,
        "paused": False,
        "post_recording": None,
    }
    state["cb_state"] = cb_state

    def get_cfg_values():
        """Read live config values (with safe fallbacks)."""
        c = state["config"]
        try:
            t = float(c.get("threshold") or 0.15)
            pre = int(sr * float(c.get("pre_boom_seconds") or 1.0))
            post = int(sr * float(c.get("post_boom_seconds") or 1.5))
            cd = float(c.get("cooldown_seconds") or 5)
        except (TypeError, ValueError):
            t, pre, post, cd = 0.15, int(sr * 1.0), int(sr * 1.5), 5
        return t, pre, post, cd

    threshold, pre_samples, post_samples, cooldown = get_cfg_values()
    buffer_len = int(sr * 3)  # 3 seconds buffer
    ring = np.zeros((buffer_len, channels), dtype=np.float32)

    rms_counter = 0

    def callback(indata, frames, time_info, status):
        nonlocal rms_counter
        s = cb_state

        try:
            if status:
                log.warning("Audio status: %s", status)

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
                        pre_audio = np.concatenate([
                            ring[pre_start:],
                            ring[:s["write_pos"]]
                        ])
                    boom_audio = np.concatenate([pre_audio, s["post_recording"]])
                    s["paused"] = True
                    boom_queue.put(boom_audio)
                return

            for i in range(frames):
                ring[s["write_pos"]] = indata[i]
                s["write_pos"] = (s["write_pos"] + 1) % buffer_len

            level = rms(indata)

            # Send RMS to dashboard every ~5 blocks
            rms_counter += 1
            if rms_counter % 5 == 0:
                socketio.emit("rms", {"level": float(level)})

            if level > threshold:
                log.info("BOOM détecté! RMS=%.4f (seuil=%.4f)", level, threshold)
                socketio.emit("status", {"state": "boom"})
                s["boom_detected"] = True
                s["post_recording"] = np.zeros((post_samples, channels), dtype=np.float32)
                s["post_recorded"] = 0
        except Exception as e:
            log.error("Erreur dans le callback audio: %s", e)

    log.info("StopBoom démarré")
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
            log.info("En écoute... (Ctrl+C pour arrêter)")
            while True:
                try:
                    boom_audio = boom_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if boom_audio.ndim == 2 and boom_audio.shape[1] == 1:
                    boom_audio = boom_audio.flatten()

                duration = len(boom_audio) / sr
                boom_rms = float(rms(boom_audio))

                replay_mode = state["config"].get("replay_mode", "echo")
                log.info("Lecture du boom (%.2fs, mode=%s)...", duration, replay_mode)
                socketio.emit("status", {"state": "boom"})
                if replay_mode == "echo":
                    play_audio(boom_audio, sr, alsa_device, out_sr)
                else:
                    play_sound_file(replay_mode, alsa_device)
                log.info("Lecture terminée")

                # Log detection
                now = datetime.now()
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
                })

                _, _, _, cooldown = get_cfg_values()
                if cooldown > 0:
                    log.info("Cooldown %ds...", cooldown)
                    socketio.emit("status", {"state": "cooldown"})
                    time.sleep(cooldown)

                cb_state["paused"] = False
                socketio.emit("status", {"state": "listening"})
                log.info("Écoute reprise")

    except KeyboardInterrupt:
        log.info("Arrêt demandé")
    except Exception as e:
        log.error("Erreur: %s", e)
        raise


def main():
    if "--list-devices" in sys.argv:
        list_devices()
        return

    cfg = load_config()
    state["config"] = cfg
    state["history"] = load_history()

    # Count today's booms
    today = str(date.today())
    state["today_date"] = today
    state["today_count"] = len([h for h in state["history"] if h.get("date") == today])

    # Start audio detection in background thread
    audio_thread = threading.Thread(target=audio_loop, daemon=True)
    audio_thread.start()

    # Start web server
    port = cfg.get("web_port", 5000)
    log.info("Dashboard web sur http://0.0.0.0:%d", port)
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
