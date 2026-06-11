"""Example plugin skill — demonstrates how to add new tools.

To add a new tool:
  1. Create a .py file in the plugins/ directory
  2. Define a class inheriting from AgentTool (or langchain_core.tools.BaseTool)
  3. Set name, description, and args_schema
  4. Implement _run() method
  5. The SkillRegistry auto-discovers it on next restart

No graph code changes needed!
"""

from __future__ import annotations

import json
import os
from typing import Type

from pydantic import BaseModel, Field

from tools.base import AgentTool


# ── Input schema with Pydantic strict validation ─────────────

class WeatherInput(BaseModel):
    """Strict input schema — LLM JSON is validated against this."""
    city: str = Field(
        description="City name to get weather for (e.g., 'Beijing', 'San Francisco').",
        min_length=1,
        max_length=100,
    )
    unit: str = Field(
        default="celsius",
        description="Temperature unit: 'celsius' or 'fahrenheit'.",
        pattern="^(celsius|fahrenheit)$",
    )


# ── Tool implementation ──────────────────────────────────────

class WeatherTool(AgentTool):
    """Get current weather for a city (example plugin).

    This is a demonstration of the plugin system.
    Replace with real API integration for production use.
    """

    name: str = "get_weather"
    description: str = (
        "Get the current weather for a specified city. "
        "Returns temperature and conditions. "
        "Use this when the user asks about weather."
    )
    args_schema: Type[BaseModel] = WeatherInput
    register_in_registry: bool = True  # Auto-discovered by SkillRegistry

    def _run(self, city: str, unit: str = "celsius") -> str:
        """Simulate weather lookup. Replace with real API call."""
        # In production, use requests.get(f"https://api.weather.com/...")
        # This is a demo stub.
        return json.dumps({
            "city": city,
            "temperature": 22 if unit == "celsius" else 72,
            "unit": unit,
            "condition": "Sunny",
            "humidity": "45%",
            "note": "This is example data — replace with real API integration.",
        }, indent=2)


# ── Add more tools by defining additional classes ───────────
# class YourNewTool(AgentTool):
#     name: str = "your_tool_name"
#     description: str = "What your tool does"
#     args_schema: Type[BaseModel] = YourInputSchema
#
#     def _run(self, ...) -> str:
#         # Your implementation
#         pass
