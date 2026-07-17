#!/usr/bin/env python3
"""Render Factory.ai Droid CLI session transcripts into Markdown.

Droid stores each session as ~/.factory/sessions/<encoded-cwd>/<uuid>.jsonl
with a sibling <uuid>.settings.json. Transcript lines are typed: `session_start`
(title, sessionTitle, cwd, owner), `message` (a nested Anthropic-style message),
plus `todo_state` / `compaction_state` bookkeeping.
"""
from __future__ import annotations

import json

from render_common import (Sections, build_markdown, clean_text, fmt_dt,
                           parse_iso, result_note, thinking_details, tool_note)


def _render_blocks(content, include_thinking: bool) -> tuple[str, list[str]]:
    """Split a message's content into (user_text, assistant_fragments)."""
    if isinstance(content, str):
        return clean_text(content), []
    user_text = []
    frags = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            txt = clean_text(block.get("text") or "")
            if txt:
                user_text.append(txt)
                frags.append(txt)
        elif btype == "thinking":
            if include_thinking:
                txt = (block.get("thinking") or block.get("text") or "").strip()
                if txt:
                    frags.append(thinking_details(txt))
        elif btype == "tool_use":
            frags.append(tool_note(block.get("name"), block.get("input")))
        elif btype == "tool_result":
            frags.append(result_note(bool(block.get("is_error"))))
    return "\n\n".join(user_text), frags


def parse_droid(path: str, include_thinking: bool = False) -> dict:
    sections = Sections()
    title = cwd = owner = None
    started = ended = None

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            otype = obj.get("type")

            if otype == "session_start":
                title = (obj.get("sessionTitle") or obj.get("title") or "").strip() or None
                cwd = obj.get("cwd")
                owner = obj.get("owner")
                continue
            if otype != "message":
                continue

            ts = parse_iso(obj.get("timestamp"))
            if ts:
                started = started or ts
                ended = ts

            message = obj.get("message") or {}
            role = message.get("role")
            user_text, frags = _render_blocks(message.get("content"), include_thinking)
            if role == "user":
                if user_text:
                    sections.add_user(user_text)
                else:
                    # A user turn carrying only tool results.
                    sections.add_assistant([f for f in frags if f.startswith(">")])
            elif role == "assistant":
                sections.add_assistant(frags)

    return {"sections": sections, "title": title, "cwd": cwd, "owner": owner,
            "started": started, "ended": ended}


def droid_to_md(path: str, meta_extra: dict, include_thinking: bool = False) -> dict:
    parsed = parse_droid(path, include_thinking=include_thinking)
    sections = parsed["sections"]
    title = parsed["title"] or _derive_title(sections.first_prompt)

    meta = []
    if parsed["started"]:
        meta.append(f"- **Started:** {fmt_dt(parsed['started'])}")
    if parsed["ended"]:
        meta.append(f"- **Ended:** {fmt_dt(parsed['ended'])}")
    for key, val in (meta_extra or {}).items():
        if val:
            meta.append(f"- **{key}:** {val}")

    return {
        "markdown": build_markdown(title, meta, sections, assistant_label="Droid"),
        "title": title,
        "first_prompt": sections.first_prompt,
        "started": parsed["started"],
        "cwd": parsed["cwd"],
        "owner": parsed["owner"],
        "user_turns": sections.user_turns,
    }


def _derive_title(first_prompt: str | None) -> str:
    if not first_prompt:
        return "Droid session"
    title = " ".join(first_prompt.split())
    return title[:60] + ("…" if len(title) > 60 else "")


if __name__ == "__main__":
    import sys
    out = droid_to_md(sys.argv[1], meta_extra={"Source": "Droid"},
                      include_thinking="--thinking" in sys.argv)
    sys.stdout.write(out["markdown"])
