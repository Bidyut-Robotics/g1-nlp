#!/bin/bash
# Sets up g1-robot-agent and g1-nlp as systemd services that start on boot.
# Run once on the AGX Thor as a user with sudo privileges.

set -e

CONDA_ENV="/home/unitree/miniconda3/envs/nlp-env"
ROBOT_AGENT_BIN="/home/unitree/nlp/g1-nlp/cpp/build/robot_agent"
ROBOT_AGENT_IFACE="enP2p1s0"
NLP_DIR="/home/unitree/nlp/g1-nlp"
SERVICE_USER="unitree"

echo "[1/4] Creating g1-robot-agent.service..."
sudo tee /etc/systemd/system/g1-robot-agent.service > /dev/null << EOF
[Unit]
Description=G1 Robot Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=$(dirname ${ROBOT_AGENT_BIN})
ExecStart=${ROBOT_AGENT_BIN} ${ROBOT_AGENT_IFACE}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[2/4] Creating g1-nlp.service..."
sudo tee /etc/systemd/system/g1-nlp.service > /dev/null << EOF
[Unit]
Description=G1 NLP Main
After=network-online.target g1-robot-agent.service
Wants=g1-robot-agent.service

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${NLP_DIR}
Environment="PATH=${CONDA_ENV}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="CONDA_PREFIX=${CONDA_ENV}"
ExecStart=${CONDA_ENV}/bin/python3 main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[3/4] Reloading systemd and enabling services..."
sudo systemctl daemon-reload
sudo systemctl enable g1-robot-agent g1-nlp

echo "[4/4] Starting services..."
sudo systemctl start g1-robot-agent g1-nlp

echo ""
echo "Done. Status:"
sudo systemctl status g1-robot-agent g1-nlp --no-pager
echo ""
echo "Useful commands:"
echo "  journalctl -u g1-nlp -f          # live logs for NLP"
echo "  journalctl -u g1-robot-agent -f  # live logs for robot agent"
echo "  sudo systemctl restart g1-nlp    # restart NLP"
echo "  sudo systemctl stop g1-nlp g1-robot-agent  # stop both"
