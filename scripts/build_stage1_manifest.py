#!/usr/bin/env python3
"""CLI wrapper for the Stage 1 manifest builder."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lipcolor_pipeline.stage1_manifest import main


if __name__ == "__main__":
    raise SystemExit(main())
