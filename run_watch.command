#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  echo "Run the installation steps in README.md first."
  exit 1
fi
exec .venv/bin/python watch.py "$@"
