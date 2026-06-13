"""Interactive session picker for resume workflows."""

from __future__ import annotations

import sys
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

_LABEL_WIDTH = 52


def session_label(record: dict[str, Any]) -> str:
    """Return a Codex-style one-line session label."""

    thread_id = str(record.get("thread_id") or "unknown")
    title = str(record.get("title") or "").strip() or f"Session {thread_id[:8]}"
    updated = str(record.get("updated_at") or "").replace("T", " ")[:16]
    if len(title) > _LABEL_WIDTH:
        title = title[: _LABEL_WIDTH - 1] + "…"
    return f"{updated}  {title}"


async def choose_session(records: list[dict[str, Any]], *, title: str = "Resume session") -> str | None:
    """Choose a session with an inline up/down list and Enter confirmation."""

    if not records:
        return None

    values: list[tuple[str, str]] = [
        (str(record.get("thread_id") or ""), session_label(record).strip())
        for record in records
        if record.get("thread_id")
    ]
    if not values:
        return None
    if len(values) == 1 or not sys.stdin.isatty() or not sys.stdout.isatty():
        return values[0][0]

    selected = {"idx": 0}
    kb = KeyBindings()

    def move(delta: int) -> None:
        selected["idx"] = (selected["idx"] + delta) % len(values)

    @kb.add("down")
    @kb.add("c-n")
    @kb.add("j")
    def _down(event) -> None:
        move(1)

    @kb.add("up")
    @kb.add("c-p")
    @kb.add("k")
    def _up(event) -> None:
        move(-1)

    @kb.add("enter")
    def _accept(event) -> None:
        event.app.exit(result=values[selected["idx"]][0])

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event) -> None:
        event.app.exit(result=None)

    def render() -> StyleAndTextTuples:
        fragments: StyleAndTextTuples = [
            ("class:title", f"{title}\n"),
            ("class:hint", "Use Up/Down to choose, Enter to resume, Esc to cancel.\n\n"),
        ]
        for idx, (_thread_id, label) in enumerate(values):
            marker = ">" if idx == selected["idx"] else " "
            style = "class:selected" if idx == selected["idx"] else "class:item"
            fragments.append((style, f"{marker} {label}\n"))
        return fragments

    app = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(render), always_hide_cursor=True)])),
        key_bindings=kb,
        style=Style.from_dict(
            {
                "title": "bold",
                "hint": "ansibrightblack",
                "selected": "reverse",
                "item": "",
            }
        ),
        full_screen=False,
        erase_when_done=True,
    )
    result = await app.run_async()
    return str(result) if result else None
