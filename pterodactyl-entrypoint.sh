#!/bin/bash
set -e

cd "${PTERODACTYL_SERVER_DIR:-/home/container}"
: "${STARTUP:=python3 main.py}"
export STARTUP

# Expand Pterodactyl's {{VARIABLE}} placeholders while preserving shell quotes.
PARSED_STARTUP=$(python3 -c '
import os
import re
import sys

startup = os.environ["STARTUP"]
sys.stdout.write(
    re.sub(
        r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        startup,
    )
)
')

printf 'container@pterodactyl~ %s\n' "${PARSED_STARTUP}"
exec /bin/bash -lc "${PARSED_STARTUP}"
