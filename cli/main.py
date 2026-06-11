"""CLI entry point — Claude Code-style interactive terminal client.

Usage:
    python -m cli.main                          # Connect to localhost:2024
    python -m cli.main --url http://host:2024   # Connect to remote server
    python -m cli.main --api-key sk-xxx         # With API key auth
    python -m cli.main --resume <thread_id>     # Resume a session
    python -m cli.main --new                    # Force new session
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid

from dotenv import load_dotenv

# Load .env before anything else (for API keys, server URL defaults)
load_dotenv()

from cli.repl import AgentRepl
from rich.console import Console

console = Console()


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="zuixiaoagent",
        description="Terminal-operating programming intelligent agent — CLI client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # Connect to local server
  %(prog)s --url https://agent.example.com   # Connect to remote server
  %(prog)s --api-key sk-secret               # With authentication
  %(prog)s --resume abc123-def456            # Resume a previous session
  %(prog)s --new                             # Start a fresh session
  %(prog)s --log-level DEBUG                 # Enable debug logging
        """,
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("AGENT_SERVER_URL", "http://localhost:2024"),
        help="LangGraph server URL (default: http://localhost:2024)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("AGENT_API_KEY"),
        help="API key for server authentication",
    )
    parser.add_argument(
        "--resume",
        default=None,
        help="Resume a specific session by thread_id",
    )
    parser.add_argument(
        "--new",
        action="store_true",
        help="Force a new session (ignore previous session)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("AGENT_LOG_LEVEL", "WARNING"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: WARNING)",
    )
    return parser.parse_args()


def setup_logging(level: str):
    """Configure Python logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def main():
    """Main entry point."""
    args = parse_args()

    # Setup logging
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    # Determine thread_id
    thread_id = None
    if args.resume:
        thread_id = args.resume
        logger.info(f"Resuming session: {thread_id}")
    elif args.new:
        thread_id = None  # Will auto-generate
        logger.info("Starting new session")
    else:
        # Try to reuse the last session
        session_file = os.path.expanduser("~/.zuixiaoagent_session")
        try:
            if os.path.exists(session_file):
                with open(session_file) as f:
                    thread_id = f.read().strip()
                logger.info(f"Reusing session: {thread_id[:12]}...")
        except Exception:
            pass

    # Validate server connection
    console.print(f"[dim]Connecting to {args.url}...[/dim]")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            api_key_header = {"X-API-Key": args.api_key} if args.api_key else {}
            resp = await client.get(f"{args.url}/health", headers=api_key_header)
            if resp.status_code == 200:
                console.print(f"[green]✓ Server healthy[/green]")
            elif resp.status_code == 401:
                console.print("[red]✗ Authentication failed — check your API key[/red]")
                sys.exit(1)
            else:
                console.print(f"[yellow]⚠ Server returned {resp.status_code}[/yellow]")
    except httpx.ConnectError:
        console.print(
            f"[red]✗ Cannot connect to {args.url}[/red]\n"
            f"[dim]Is the server running? Start it with: make dev[/dim]"
        )
        sys.exit(1)
    except Exception as e:
        console.print(f"[yellow]⚠ Could not verify server: {e}[/yellow]")

    # Launch REPL
    repl = AgentRepl(
        server_url=args.url,
        api_key=args.api_key,
        thread_id=thread_id,
    )

    try:
        await repl.run()
    except Exception as e:
        console.print(f"[red]Fatal error: {e}[/red]")
        sys.exit(1)
    finally:
        # Save session for next time
        session_file = os.path.expanduser("~/.zuixiaoagent_session")
        try:
            os.makedirs(os.path.dirname(session_file), exist_ok=True)
            with open(session_file, "w") as f:
                f.write(repl.thread_id)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
