#!/usr/bin/env python3
"""CLI wrapper for the repository secret scanner."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lipcolor_pipeline.security import main


if __name__ == "__main__":
    raise SystemExit(main())
