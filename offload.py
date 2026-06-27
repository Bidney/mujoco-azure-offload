#!/usr/bin/env python3
"""Thin launcher so you can run `python offload.py ...` without installing.
Equivalent to `python -m azoffload`."""
from azoffload.cli import main

if __name__ == "__main__":
    main()
