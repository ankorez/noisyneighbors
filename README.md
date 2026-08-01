# NoisyNeighbors

Detects neighbor booms and automatically plays them back. Includes a real-time web dashboard for monitoring and configuration.
<img width="2074" height="1394" alt="image" src="https://github.com/user-attachments/assets/87140861-59c3-4cd9-9f0f-95a070aab987" />

## Hardware

- Raspberry Pi (Zero W/2W, 3, 4, 5) or any Linux device
- USB speakerphone or mic + speaker (tested with Jabra SPEAK 410)
- (Optional) PS4 DualShock 4 controller via USB for vibration feedback

## Installation

### 1. Prepare the device

Install a Linux OS (e.g. Raspberry Pi OS Lite with [Raspberry Pi Imager](https://www.raspberrypi.com/software/)).

For a Raspberry Pi, enable SSH and configure Wi-Fi in the Imager advanced settings.

### 2. Clone and install

```bash
ssh <user>@<hostname>
sudo apt update && sudo apt upgrade -y
sudo apt install -y git
git clone https://github.com/ankorez/noisyneighbors.git ~/noisyneighbors
cd ~/noisyneighbors
chmod +x setup.sh
./setup.sh
```

This installs system dependencies, creates a Python virtual environment, and sets up the systemd service.

### 3. Set output volume

```bash
# List volume controls for the audio card (replace 1 with your card number)
amixer -c 1 contents

# Set volume to max (adjust numid and value based on the output above)
amixer -c 1 cset numid=<id> <max>
```

Volume can also be adjusted from the web dashboard.

## Usage

### Manual test

```bash
cd ~/noisyneighbors
source venv/bin/activate
python3 noisyneighbors.py
```

The web dashboard is available at `http://<hostname>.local:5000`. It's organized into tabs:

- **Live** — real-time audio level, boom/status indicator, enable/disable toggle, and test buttons for sound and vibration.
- **Config** — replay mode, threshold, volume, scheduler, night mode, strike mode, and rate limiting.
- **Devices** — audio input/output device selection (including a paired Bluetooth speaker), PS4 controller vibration settings, and Bluetooth speaker pairing (scan/pair/connect/forget).
- **Stats** — detection history and aggregate stats, plus saved recordings (if enabled).

### systemd service (auto-start)

```bash
sudo systemctl start noisyneighbors      # Start
sudo systemctl stop noisyneighbors       # Stop
sudo systemctl status noisyneighbors     # Status
sudo systemctl restart noisyneighbors    # Restart (after config change)
journalctl -u noisyneighbors -f          # View live logs
```

The service starts automatically on boot.

## Configuration

`config.json` is generated on first run from `config.example.json` (`setup.sh` does this automatically) and is **not tracked by git** — it's local runtime state (the app writes to it whenever you change something in the dashboard), so `git pull` never touches or conflicts with it. Edit it via the web dashboard (applied in real-time) or manually (requires a service restart):

```json
{
  "threshold": 0.15,
  "pre_boom_seconds": 1.0,
  "post_boom_seconds": 1.5,
  "cooldown_seconds": 5,
  "sample_rate": null,
  "channels": 1,
  "device": null,
  "alsa_device": null,
  "output_sample_rate": 48000,
  "replay_mode": "echo",
  "ps4_vibration": false,
  "vibration_intensity": 100,
  "schedule_enabled": false,
  "schedule_start": "22:00",
  "schedule_end": "08:00",
  "night_mode_enabled": false,
  "night_mode_start": "22:00",
  "night_mode_end": "08:00",
  "night_threshold": 0.10,
  "night_replay_mode": "echo",
  "max_booms_per_hour": 0,
  "save_recordings": false,
  "strike_mode_enabled": false,
  "strike_min_interval": 60,
  "strike_max_interval": 300
}
```

| Parameter | Description |
|---|---|
| `threshold` | RMS detection threshold (0.0-1.0). Lower = more sensitive. |
| `pre_boom_seconds` | Seconds of audio kept before the boom. |
| `post_boom_seconds` | Seconds of audio recorded after detection. |
| `cooldown_seconds` | Pause after each replay to avoid loops. |
| `sample_rate` | Sample rate. `null` = auto-detect from device. |
| `channels` | Input channels (1 = mono). |
| `device` | sounddevice device index for capture. `null` = auto-detect first USB device. |
| `alsa_device` | ALSA device for playback, or `"bluetooth"` to use a paired Bluetooth speaker. `null` = auto-detect USB device. |
| `output_sample_rate` | Output sample rate for playback (48000 recommended). |
| `replay_mode` | Sound played after detection: `echo` (replay the boom), `alarm`, `doorbell`, `hammer`, `honk`, `siren`, `sandstorm`, `hammering`. |
| `ps4_vibration` | Enable PS4 controller vibration on boom detection (triggers alongside the sound). |
| `vibration_intensity` | Vibration intensity (10-100%). |
| `schedule_enabled` | Only run detection during the `schedule_start`–`schedule_end` window (auto enable/disable). |
| `schedule_start` / `schedule_end` | Detection active window (HH:MM), wraps past midnight if `start > end`. |
| `night_mode_enabled` | Use a different threshold and replay mode during a night window. |
| `night_mode_start` / `night_mode_end` | Night window (HH:MM), wraps past midnight if `start > end`. |
| `night_threshold` | RMS threshold used during the night window instead of `threshold`. |
| `night_replay_mode` | Replay mode used during the night window instead of `replay_mode`. |
| `max_booms_per_hour` | Cap responses per rolling hour to avoid an escalating cycle. `0` = unlimited. |
| `save_recordings` | Save each detected boom as a WAV file in `recordings/` (viewable/downloadable/deletable from the Stats tab). |
| `strike_mode_enabled` | Randomly fire sound/vibration on an interval, independent of boom detection. |
| `strike_min_interval` / `strike_max_interval` | Random delay range (seconds) between Strike Mode firings. |
| `web_port` | Web dashboard port (default 5000). |

### Finding audio devices

```bash
python3 noisyneighbors.py --list-devices
```

This lists all available devices with their index, channel count, and sample rate.

### Calibrating the threshold

Start NoisyNeighbors and make some noise. The logs show the RMS value for each detection. Adjust `threshold` in `config.json` based on the observed values.

### PS4 controller (optional)

Connect a DualShock 4 controller via USB. The dashboard shows its connection status and lets you enable vibration on boom detection. Vibration triggers alongside the response sound.

The setup script automatically adds the user to the `input` group (required for controller access). A reboot may be needed after the first install.

### Bluetooth speaker output (optional)

Pair a Bluetooth speaker (e.g. Bose) and select it as the output, instead of the main ALSA device — the response sound plays on whichever one is selected in the **Output (speaker)** dropdown, not both at once.

1. One-time system setup:
   ```bash
   sudo apt install -y pulseaudio pulseaudio-module-bluetooth
   sudo loginctl enable-linger "$USER"
   systemctl --user enable --now pulseaudio.service pulseaudio.socket
   sudo rfkill unblock bluetooth   # if the adapter shows as soft-blocked (check with `rfkill list bluetooth`)
   ```
   `noisyneighbors.service` needs `XDG_RUNTIME_DIR` set to reach your PulseAudio session (already included in the provided unit template — the UID is substituted at install time rather than using systemd's `%U` specifier, which doesn't reliably resolve to the service's `User=` on all systemd versions). If upgrading from an older install, re-run the service install step: `./setup.sh`, or manually: `sed -e "s|__USER__|$USER|g" -e "s|__HOME__|$HOME|g" -e "s|__UID__|$(id -u)|g" noisyneighbors.service | sudo tee /etc/systemd/system/noisyneighbors.service && sudo systemctl daemon-reload && sudo systemctl restart noisyneighbors`.
2. Put the speaker in pairing mode, then from the **Devices** tab → **Bluetooth Speaker** card: click **Scan for speakers** and then **Pair** next to it. Once paired, it's remembered — use **Connect** to reconnect after a reboot, or **Forget** to remove it.
3. Once connected, it appears as an extra entry in the **Output (speaker)** dropdown at the top of the Devices tab — select it to route the response sound there instead of the main output.

The app talks to the connected Bluetooth (A2DP) speaker via `paplay`/`pactl` — no need to hardcode a device name. If the speaker disconnects, errors are logged and switching back to the main ALSA output keeps everything working normally.

Note: after a Pi reboot, paired Bluetooth speakers don't reconnect automatically — use the **Connect** button in the dashboard (or `bluetoothctl connect <MAC>` over SSH).

If scanning/pairing gets stuck (e.g. `br-connection-busy`), use the **Restart Bluetooth** button. `setup.sh` grants the app a narrowly-scoped passwordless `sudo systemctl restart bluetooth` (nothing else) for this — see `/etc/sudoers.d/noisyneighbors-bluetooth`. Without it, the button falls back to just power-cycling the adapter (`bluetoothctl power off`/`on`), which is less thorough but needs no extra privileges. If upgrading from an older install, re-run `./setup.sh` to add the sudoers rule.

`setup.sh` also sets `AutoEnable=true` in `/etc/bluetooth/main.conf` so the adapter powers itself on at boot — otherwise it stays off after every reboot (`org.bluez.Error.NotReady`/`br-connection-adapter-not-powered` when trying to connect) until someone runs `bluetoothctl power on`. On some Pi boots the adapter also comes up soft-blocked by rfkill, which silently prevents that auto-power-on; `setup.sh` adds a `bluetooth.service` drop-in (`/etc/systemd/system/bluetooth.service.d/override.conf`) that runs `rfkill unblock bluetooth` right before the daemon starts, so this isn't a per-reboot manual step either. If upgrading from an older install, re-run `./setup.sh` to apply both.

## Tips for best results

- **Place the Raspberry Pi and USB speakerphone high up**, close to the ceiling (on top of a tall cabinet or shelf). This improves both detection sensitivity and sound projection toward the neighbors above.
- **PS4 controller placement**: for maximum impact, place the vibrating controller on top of a glass bowl or jar. The glass amplifies the vibrations and transmits them through the ceiling.

## Troubleshooting

### No sound output

1. Check that the device is not muted
2. Set ALSA volume: `amixer -c <card> contents` to see controls, then `amixer -c <card> cset numid=<id> <max>`
3. Test with: `speaker-test -D plughw:<card>,0 -c 2 -t sine -f 440`

### Finding the right ALSA device

```bash
arecord -l   # Capture devices
aplay -l     # Playback devices
```

The card number corresponds to `<card>` in `plughw:<card>,0`.
