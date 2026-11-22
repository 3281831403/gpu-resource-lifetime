"""Pytest bootstrap: make the repository root importable as ``lifetime``.

Running ``python -m pytest tests -q`` from the repository root already puts
the current directory on ``sys.path``; the explicit insert keeps plain
``pytest tests -q`` working the same way from any working directory.
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
