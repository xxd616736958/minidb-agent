"""Session history and checkpoint navigation for the CLI.

Provides:
  - List past sessions (threads)
  - Show checkpoint history for a session
  - View state at a specific checkpoint
  - Fork/resume from a checkpoint
"""

from __future__ import annotations

from typing import Any, Optional

from rich.console import Console
from rich.table import Table
from rich.text import Text

console = Console()


class SessionHistory:
    """Browse and navigate LangGraph session history."""

    def __init__(self, client):
        self.client = client

    async def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent sessions (threads)."""
        try:
            threads = await self.client.threads.search(limit=limit)
            return [
                {
                    "thread_id": t.get("thread_id", str(t)),
                    "created_at": t.get("created_at"),
                    "updated_at": t.get("updated_at"),
                }
                for t in threads
            ]
        except Exception as e:
            console.print(f"[red]Failed to list sessions: {e}[/red]")
            return []

    async def show_history(self, thread_id: str, limit: int = 10):
        """Display checkpoint history for a session."""
        try:
            states = await self.client.threads.get_history(
                thread_id,
                limit=limit,
            )
            if not states:
                console.print("[dim]No history for this session.[/dim]")
                return

            table = Table(title=f"Session History: {thread_id[:12]}...")
            table.add_column("Step", style="cyan")
            table.add_column("Checkpoint", style="dim")
            table.add_column("Messages", style="green")

            for state in states:
                cfg = state.get("config", {}) or {}
                cid = (cfg.get("checkpoint_id", "?") or "?")[:8]
                meta = state.get("metadata", {}) or {}
                step = meta.get("step", "?")
                vals = state.get("values", {}) or {}
                msg_count = len(vals.get("messages", []))

                table.add_row(str(step), cid, str(msg_count))

            console.print(table)
        except Exception as e:
            console.print(f"[red]Failed to get history: {e}[/red]")

    async def get_checkpoint_state(
        self, thread_id: str, checkpoint_id: Optional[str] = None
    ) -> Optional[dict]:
        """Get the state at a specific checkpoint."""
        try:
            state = await self.client.threads.get_state(
                thread_id,
                checkpoint_id=checkpoint_id,
            )
            vals = state.get("values", {}) if state else {}
            return {
                "messages": len(vals.get("messages", [])),
                "working_memory": vals.get("working_memory", {}),
                "plan": vals.get("plan"),
                "error": vals.get("error"),
            }
        except Exception as e:
            console.print(f"[red]Failed to get state: {e}[/red]")
            return None

    async def resume_from_checkpoint(
        self, thread_id: str, checkpoint_id: str
    ) -> bool:
        """Fork a session from a specific checkpoint."""
        try:
            await self.client.threads.update_state(
                thread_id,
                checkpoint_id=checkpoint_id,
            )
            console.print(f"[green]Resumed from: {checkpoint_id[:12]}...[/green]")
            return True
        except Exception as e:
            console.print(f"[red]Failed to resume: {e}[/red]")
            return False
