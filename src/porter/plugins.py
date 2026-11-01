"""Minimal plugin loading: ``module:attr`` names, searched on configured plugin paths.

Lets a private installation keep its own task sources and executors in a separate
directory without changing the core.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def add_plugin_paths(paths: list[Path]) -> None:
    for p in paths:
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


def load_object(spec: str):
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ValueError(f"plugin spec must be 'module:attr', got {spec!r}")
    obj = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj
