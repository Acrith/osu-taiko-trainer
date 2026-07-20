"""PyInstaller entry point for the standalone Windows uploader binary.

Delegates immediately to `taiko_trainer.uploader_gui:main` — this file
exists only because PyInstaller's `Analysis` step wants a real script path,
not a module reference. Nothing meaningful lives here.
"""
from __future__ import annotations

import sys

from taiko_trainer.uploader_gui import main


if __name__ == "__main__":
    sys.exit(main())
