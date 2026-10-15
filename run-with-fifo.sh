#!/usr/bin/env bash
# Startet bedrock_server so, dass Befehle über eine FIFO (Named Pipe)
# eingespeist werden können, z.B.: echo "say Hallo" > /opt/bedrock/command_input
set -euo pipefail

SERVER_DIR="/opt/bedrock"
FIFO="$SERVER_DIR/command_input"

[ -p "$FIFO" ] || mkfifo "$FIFO"

cd "$SERVER_DIR"
export LD_LIBRARY_PATH=.

# fd 3 hält die FIFO offen, damit sie nie auf EOF läuft
exec 3<>"$FIFO"
exec ./bedrock_server <&3
