#!/usr/bin/env python3
"""Entry point to run the C5 Address Mapping pipeline."""
import sys
from pathlib import Path

# Ensure pipeline package is discoverable
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.c5.run_all import main

if __name__ == "__main__":
    main()
