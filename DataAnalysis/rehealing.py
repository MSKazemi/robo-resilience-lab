#!/usr/bin/env python3
"""Compatibility entry point: same CLI as ``RH.py`` (dry run by default).

The old ``rehealing.py`` looked for ``Killing`` and ``Started`` events on the SAME pod name,
which does not measure a Deployment replacement (the replacement is a new pod with a new name
and UID), and it used a generic ``ros-`` name prefix. That method is removed. This script now
runs the same selector/UID-based measurement as ``RH.py``; see ``DataAnalysis/README.md``.
"""
import sys

from RH import main

if __name__ == "__main__":
    sys.exit(main(prog="rehealing.py"))
