#!/bin/bash

resolve_project_python() {
  local root="${1:-/Users/xiong/weather}"
  local candidate=""

  candidate="$root/.venv/bin/python"
  if [ -x "$candidate" ]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  candidate="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
  if [ -x "$candidate" ]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  return 1
}
