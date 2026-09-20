#!/usr/bin/env python3
"""Source-checkout alias; the managed Agent ships the canonical module."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from agent.setup_integrations import *
if __name__=="__main__":raise SystemExit(main())
