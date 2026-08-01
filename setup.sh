#!/bin/bash
# Setup NoisyNeighbors on Raspberry Pi

set -e

echo "=== Installing system dependencies ==="
sudo apt update
sudo apt install -y python3-pip python3-venv portaudio19-dev

echo "=== Creating virtual environment ==="
python3 -m venv venv
source venv/bin/activate

echo "=== Installing Python dependencies ==="
pip install -r requirements.txt

echo "=== Setting up config.json ==="
[ -f config.json ] || cp config.example.json config.json

echo "=== Adding user to input group (for PS4 controller) ==="
sudo usermod -aG input "$USER"

echo "=== Installing systemd service ==="
sed -e "s|__USER__|$USER|g" -e "s|__HOME__|$HOME|g" -e "s|__UID__|$(id -u)|g" noisyneighbors.service | sudo tee /etc/systemd/system/noisyneighbors.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable noisyneighbors

echo "=== Allowing passwordless Bluetooth restart (for the dashboard's Restart button) ==="
SYSTEMCTL_PATH=$(command -v systemctl)
SUDOERS_FILE=/etc/sudoers.d/noisyneighbors-bluetooth
echo "$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart bluetooth" | sudo tee "$SUDOERS_FILE" > /dev/null
sudo chmod 0440 "$SUDOERS_FILE"
sudo visudo -c -f "$SUDOERS_FILE" || sudo rm -f "$SUDOERS_FILE"

echo "=== Enabling Bluetooth adapter auto-power-on at boot ==="
# Without this, the adapter stays unpowered after every reboot and pairing/
# connecting fails until someone runs "bluetoothctl power on" manually.
BT_CONF=/etc/bluetooth/main.conf
if grep -q "^\[Policy\]" "$BT_CONF" 2>/dev/null; then
    if grep -q "^AutoEnable" "$BT_CONF"; then
        sudo sed -i 's/^AutoEnable.*/AutoEnable=true/' "$BT_CONF"
    else
        sudo sed -i '/^\[Policy\]/a AutoEnable=true' "$BT_CONF"
    fi
else
    printf '\n[Policy]\nAutoEnable=true\n' | sudo tee -a "$BT_CONF" > /dev/null
fi
sudo systemctl restart bluetooth

echo ""
echo "=== Installation complete ==="
echo "Useful commands:"
echo "  sudo systemctl start noisyneighbors    # Start"
echo "  sudo systemctl stop noisyneighbors     # Stop"
echo "  sudo systemctl status noisyneighbors   # Status"
echo "  journalctl -u noisyneighbors -f        # View logs"
echo ""
echo "To test manually:"
echo "  source venv/bin/activate"
echo "  python3 noisyneighbors.py"
