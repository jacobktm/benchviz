"""Shared cross-cutting utilities for the benchviz application."""

from __future__ import annotations

import os


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
