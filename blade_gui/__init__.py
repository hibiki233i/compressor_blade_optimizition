"""Desktop GUI for the blade-shape active-learning pipeline.

The package is a read/write *front end* over the existing command-line entry
points.  It never re-implements the optimizer: data is read with the same
helpers the CLI uses (``blade_shape_active_learning``) and every action is
executed by spawning the original CLI as a child process.
"""
from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
