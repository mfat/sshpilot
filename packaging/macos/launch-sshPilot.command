#!/bin/bash

# Get the directory where this command file is located
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Launch the sshPilot app
open "$SCRIPT_DIR/sshPilot.app"
