#!/bin/bash
set -e
SDK=${UNITREE_SDK2_PATH:-"$HOME/unitree_sdk2"}
cmake -B build -S . -DUNITREE_SDK2_PATH="$SDK"
cmake --build build
echo "Built: $(pwd)/build/robot_agent"
