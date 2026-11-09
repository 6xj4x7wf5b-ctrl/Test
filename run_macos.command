#!/bin/zsh
set -eu
TASK_DESKTOP="${0:A:h}"
TASK_PYTHON="$TASK_DESKTOP/../.conda/warehouse-pc/bin/python"
cd "$TASK_DESKTOP"
exec "$TASK_PYTHON" launcher.py "$@"
