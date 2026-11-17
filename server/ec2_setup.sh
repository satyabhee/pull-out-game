#!/usr/bin/env bash
# ec2_setup.sh: run ONCE on a fresh Ubuntu EC2 server, from the folder holding
# record_hl.py and fetch_blocks.py.   Usage:  bash ec2_setup.sh BTC ETH
#
# Sets up:
#   - Python + websockets in ~/hl/.venv
#   - the recorder as a systemd service: starts on boot, restarts itself if it crashes
#   - a daily check that warns when the disk is over 80% full
set -euo pipefail
COINS="${*:-BTC ETH}"
HL="$HOME/hl"

sudo apt-get update -y
sudo apt-get install -y python3-venv tmux
mkdir -p "$HL"
cp record_hl.py fetch_blocks.py "$HL"/
python3 -m venv "$HL/.venv"
"$HL/.venv/bin/pip" install --quiet websockets

sudo tee /etc/systemd/system/hl-recorder.service >/dev/null <<EOF
[Unit]
Description=Hyperliquid trade/book/block-header recorder
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$HL
ExecStart=$HL/.venv/bin/python -u record_hl.py $COINS
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now hl-recorder
( crontab -l 2>/dev/null; echo '0 12 * * * df -h / | awk '"'"'NR==2 && $5+0>80 {print "DISK OVER 80%: "$5}'"'"' >> $HOME/hl/disk_warnings.log' ) | crontab -

echo
echo "Recorder running. Check it with:   systemctl status hl-recorder"
echo "Watch its log with:                journalctl -u hl-recorder -f"
echo "Fetch blocks inside tmux so it survives disconnects:"
echo "   tmux new -s fetch"
echo "   cd ~/hl && .venv/bin/python fetch_blocks.py START END"
echo "   (detach: Ctrl+B then D   |   reattach later: tmux attach -t fetch)"
