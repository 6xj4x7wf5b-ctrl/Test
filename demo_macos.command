#!/bin/zsh
set -eu
TASK_DESKTOP="${0:A:h}"
exec "$TASK_DESKTOP/run_macos.command" --demo "$@"
