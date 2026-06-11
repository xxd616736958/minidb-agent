"""Long-term memory: persistent storage via LangGraph checkpointing + Store.

Two mechanisms:
1. **Checkpointer** (SqliteSaver / PostgresSaver): Full graph state serialized
   after every node. Enables time-travel, fork, resume. Keyed by thread_id.
2. **Store** (LangGraph Store API): User-managed key-value store with optional
   vector indexing. Used for semantic retrieval of past memories.

The checkpointer is the backbone; the Store is for enriched long-term recall.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger(__name__)


def create_checkpointer(postgres_uri: str = "") -> BaseCheckpointSaver:
    """Create the appropriate checkpointer based on configuration.

    Args:
        postgres_uri: PostgreSQL connection string.
                      If empty, falls back to SQLite.

    Returns:
        A BaseCheckpointSaver instance (SqliteSaver or PostgresSaver).
    """
    if postgres_uri:
        return _create_postgres_checkpointer(postgres_uri)
    else:
        return _create_sqlite_checkpointer()


def _create_sqlite_checkpointer() -> BaseCheckpointSaver:
    """Create SQLite-backed checkpointer for local development."""
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver

    # Ensure data directory exists
    db_dir = os.path.join(os.getcwd(), "data")
    os.makedirs(db_dir, exist_ok=True)

    db_path = os.path.join(db_dir, "agent_checkpoints.sqlite")
    logger.info(f"Using SQLite checkpointer: {db_path}")

    # SqliteSaver.from_conn_string() returns a context manager in v2.x.
    # Use direct sqlite3 connection for persistent checkpointer lifetime.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    checkpointer.setup()
    return checkpointer


def _create_postgres_checkpointer(postgres_uri: str) -> BaseCheckpointSaver:
    """Create PostgreSQL-backed checkpointer for production."""
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg_pool import ConnectionPool

    logger.info(f"Using PostgreSQL checkpointer: {postgres_uri[:50]}...")

    pool = ConnectionPool(
        postgres_uri,
        max_size=10,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
        },
    )
    checkpointer = PostgresSaver(pool)
    checkpointer.setup()
    return checkpointer


# ── Long-term memory Store helpers ───────────────────────────

# These are placeholder functions for when the LangGraph Store API is
# available within the graph runtime. The Store is configured in
# langgraph.json and accessed via config["store"] inside nodes.

async def save_to_long_term(
    store,  # LangGraph Store instance
    session_id: str,
    key: str,
    content: str,
    metadata: Optional[dict] = None,
) -> None:
    """Save a memory entry to the long-term Store.

    Args:
        store: LangGraph BaseStore instance.
        session_id: Thread/session identifier for isolation.
        key: Unique key for this memory entry.
        content: The memory content (embedded for semantic search).
        metadata: Optional metadata dict.
    """
    namespace = ("memories", session_id, "long_term")
    await store.aput(
        namespace,
        key,
        {
            "content": content,
            "metadata": metadata or {},
            "created_at": None,  # Store timestamps server-side if needed
        },
    )


async def search_long_term(
    store,  # LangGraph Store instance
    session_id: str,
    query: str,
    limit: int = 5,
) -> list[dict]:
    """Search long-term memory for semantically relevant entries.

    Args:
        store: LangGraph BaseStore instance.
        session_id: Thread/session identifier.
        query: Natural language query for semantic search.
        limit: Max results to return.

    Returns:
        List of matching memory entries.
    """
    namespace = ("memories", session_id, "long_term")
    results = await store.asearch(
        namespace,
        query=query,
        limit=limit,
    )
    return [
        {
            "key": item.key,
            "content": item.value.get("content", ""),
            "metadata": item.value.get("metadata", {}),
            "score": item.score,
        }
        for item in results
    ]
