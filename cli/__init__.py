"""CLI client package."""

from __future__ import annotations

import warnings

warnings.filterwarnings("ignore", module=r"langgraph\.cache\.base\..*")
warnings.filterwarnings("ignore", message=r".*allowed_objects.*")
