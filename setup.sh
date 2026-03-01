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

echo "=== Adding user to input group (for PS4 controller) ==="
sudo usermod -aG input "$USER"

echo "=== Installing systemd service ==="
sudo cp noisyneighbors.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable noisyneighbors

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
