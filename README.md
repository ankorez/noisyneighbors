# NoisyNeighbors

Detects neighbor booms and automatically plays them back. Includes a real-time web dashboard for monitoring and configuration.

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

The web dashboard is available at `http://<hostname>.local:5000`. It shows real-time audio level, detection history, and lets you adjust settings and enable/disable detection.

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

Edit `config.json` via the web dashboard (applied in real-time) or manually (requires a service restart):

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
  "vibration_intensity": 100
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
| `alsa_device` | ALSA device for playback. `null` = auto-detect USB device. |
| `output_sample_rate` | Output sample rate for playback (48000 recommended). |
| `replay_mode` | Sound played after detection: `echo` (replay the boom), `alarm`, `doorbell`, `hammer`, `honk`, `siren`. |
| `ps4_vibration` | Enable PS4 controller vibration on boom detection (triggers alongside the sound). |
| `vibration_intensity` | Vibration intensity (10-100%). |
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
