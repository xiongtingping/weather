#!/bin/bash
# The launchd entry point for the regional lane's admission due check.
#
# A shell wrapper rather than launchd calling python directly, for the reason
# every other sweep here has one: the interpreter, the working directory and
# the exit convention live in one place a person can run by hand and get
# exactly what the daemon gets.
set -euo pipefail
cd /Users/xiong/weather
exec /usr/bin/env python3 scripts/regional_admission_due_check.py "$@"
