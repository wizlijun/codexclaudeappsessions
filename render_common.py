#!/usr/bin/env python3
"""Shared helpers for rendering sessions to Markdown across vendors.

All exporters (Claude chat, Claude Code/Cowork, Codex, Droid, ChatGPT) produce
the same visual Markdown: a metadata block, then alternating "👤 User" /
"🤖 Claude" sections, with tool calls noted inline. These helpers keep that
output identical regardless of the source format.
"""
from __future__ import annotations

import datetime as dt
import re

# Fields probed (in order) to show a short, useful detail for a tool call.
_TOOL_DETAIL_KEYS = ("command", "query", "url", "file_path", "path", "pattern",
                     "prompt", "description", "explanation")
_SYS_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def tool_note(name: str, tool_input) -> str:
    """One-line note for a tool/function call."""
    detail = ""
    if isinstance(tool_input, dict):
        for key in _TOOL_DETAIL_KEYS:
            if tool_input.get(key):
                val = str(tool_input[key]).replace("\n", " ")
                detail = f": `{val[:120]}`"
                break
    elif isinstance(tool_input, str) and tool_input.strip():
        detail = f": `{tool_input.strip().replace(chr(10), ' ')[:120]}`"
    return f"> 🔧 **{name or 'tool'}**{detail}"


def result_note(is_error: bool = False) -> str:
    """Compact note for a tool result."""
    return f"> 📄 *tool result ({'⚠️ error' if is_error else 'ok'})*"


def thinking_details(text: str) -> str:
    """Collapsed block for assistant thinking/reasoning."""
    return ("<details><summary>💭 thinking</summary>\n\n"
            + text.strip() + "\n\n</details>")


def clean_text(text: str) -> str:
    """Strip harness-injected system reminders from a chunk of text."""
    return _SYS_REMINDER_RE.sub("", text or "").strip()


class Sections:
    """Accumulates conversation turns, merging consecutive assistant output.

    A user prompt always starts a new section; assistant text, tool calls,
    tool results and thinking all flow into the current assistant section.
    """

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.first_prompt: str | None = None

    def add_user(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.items.append({"role": "user", "parts": [text]})
        if self.first_prompt is None:
            self.first_prompt = text

    def add_assistant(self, fragments) -> None:
        if isinstance(fragments, str):
            fragments = [fragments]
        fragments = [f for f in fragments if f and f.strip()]
        if not fragments:
            return
        if self.items and self.items[-1]["role"] == "assistant":
            self.items[-1]["parts"].extend(fragments)
        else:
            self.items.append({"role": "assistant", "parts": list(fragments)})

    @property
    def user_turns(self) -> int:
        return sum(1 for s in self.items if s["role"] == "user")


def build_markdown(title: str, meta_lines: list[str], sections: Sections,
                   assistant_label: str = "Assistant") -> str:
    """Assemble the final Markdown document."""
    lines = [f"# {title}", ""]
    lines.extend(meta_lines)
    lines.append(f"- **User turns:** {sections.user_turns}")
    lines.extend(["", "---", ""])
    for section in sections.items:
        lines.append("## 👤 User" if section["role"] == "user"
                     else f"## 🤖 {assistant_label}")
        lines.append("")
        body = "\n\n".join(p for p in section["parts"] if p.strip())
        lines.append(body if body.strip() else "*(no text content)*")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_local(value: dt.datetime | None) -> dt.datetime | None:
    """Convert a datetime to the OS local timezone.

    All supported transcript formats emit UTC timestamps (ISO strings ending
    in ``Z`` or epoch seconds), so any tz-aware value is converted with
    ``astimezone()`` and any naive value is assumed to be UTC first. This is
    the single choke point that guarantees dates written to Markdown and used
    for month/day bucketing reflect local wall-clock time, not UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone()


def fmt_dt(value: dt.datetime | None) -> str:
    value = to_local(value)
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def parse_iso(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def epoch_to_dt(value) -> dt.datetime | None:
    """Accept epoch seconds or milliseconds."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value > 1e12:  # milliseconds
        value /= 1000
    try:
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
    except (ValueError, OSError):
        return None
