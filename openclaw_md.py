#!/usr/bin/env python3
"""Render OpenClaw agent session transcripts into Markdown.

OpenClaw stores each session as:

    ~/.openclaw/agents/<agent>/sessions/<uuid>.jsonl

with an optional sibling index:

    ~/.openclaw/agents/<agent>/sessions/sessions.json

JSONL lines are typed:
  * `session`  – id, cwd, timestamp
  * `message`  – nested message with role user / assistant / toolResult

Assistant content blocks use `text`, `thinking`, and `toolCall` (args may be
under `arguments` or `input`). Channel metadata is often prepended to user
text as "Conversation info (untrusted metadata): …".
"""
from __future__ import annotations

import json
import re

from render_common import (Sections, build_markdown, clean_text, fmt_dt,
                           parse_iso, result_note, thinking_details, tool_note)

# Harness-injected user turns that are not real human prompts.
_SKIP_USER_PREFIXES = (
    "System (untrusted):",
    "An async command completion event",
    "Pre-compaction memory flush.",
    "[OpenClaw heartbeat poll]",
)
_CONV_INFO_RE = re.compile(
    r"^Conversation info \(untrusted metadata\):\s*```json\s*.*?\s*```\s*",
    re.S,
)
_CRON_LABEL_RE = re.compile(r"^\[cron:[^\]]+\]\s*")
_SUBAGENT_CTX_RE = re.compile(
    r"^\[Subagent Context\].*?\[Subagent Task\]\s*", re.S)


def _content_text(content) -> str:
    """Flatten message content (string or list of blocks) into plain text."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("text", "input_text", "output_text"):
            parts.append(block.get("text") or "")
        elif block.get("type") == "thinking":
            # thinking is handled separately for assistant turns
            continue
    return "\n".join(p for p in parts if p)


def _clean_user_text(text: str) -> str | None:
    """Strip channel/harness wrappers; return None for pure system noise."""
    text = (text or "").strip()
    if not text:
        return None
    if any(text.startswith(p) for p in _SKIP_USER_PREFIXES):
        return None
    text = _CONV_INFO_RE.sub("", text).strip()
    text = _SUBAGENT_CTX_RE.sub("", text).strip()
    # Keep cron label short: drop the long job body prefix tag only.
    text = _CRON_LABEL_RE.sub("", text).strip()
    # Timestamp-prefixed subagent wrappers: "[Mon 2026-05-18 21:36 GMT+8] …"
    text = re.sub(r"^\[[A-Za-z]{3} \d{4}-\d{2}-\d{2}[^\]]*\]\s*", "", text).strip()
    text = clean_text(text)
    return text or None


def _tool_args(block: dict):
    args = block.get("arguments")
    if args is None:
        args = block.get("input")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            pass
    return args


def _render_assistant(content, include_thinking: bool) -> list[str]:
    if isinstance(content, str):
        txt = clean_text(content)
        return [txt] if txt else []
    frags: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in ("text", "output_text"):
            txt = clean_text(block.get("text") or "")
            if txt:
                frags.append(txt)
        elif btype == "thinking":
            if include_thinking:
                think = (block.get("thinking") or block.get("text") or "").strip()
                if think:
                    frags.append(thinking_details(think))
        elif btype in ("toolCall", "tool_use", "function_call"):
            frags.append(tool_note(block.get("name"), _tool_args(block)))
    return frags


def parse_openclaw(path: str, include_thinking: bool = False) -> dict:
    sections = Sections()
    cwd = session_id = model = None
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
            ts = parse_iso(obj.get("timestamp"))
            if ts:
                started = started or ts
                ended = ts

            if otype == "session":
                cwd = obj.get("cwd") or cwd
                session_id = obj.get("id") or session_id
                continue
            if otype != "message":
                continue

            message = obj.get("message") or {}
            role = message.get("role")
            content = message.get("content")

            if role == "user":
                text = _clean_user_text(_content_text(content))
                if text:
                    sections.add_user(text)
            elif role == "assistant":
                model = message.get("model") or model
                frags = _render_assistant(content, include_thinking)
                if frags:
                    sections.add_assistant(frags)
            elif role == "toolResult":
                sections.add_assistant(result_note(bool(message.get("isError"))))

    return {"sections": sections, "cwd": cwd, "session_id": session_id,
            "model": model, "started": started, "ended": ended}


def openclaw_to_md(path: str, meta_extra: dict | None = None,
                   include_thinking: bool = False,
                   title: str | None = None) -> dict:
    parsed = parse_openclaw(path, include_thinking=include_thinking)
    sections = parsed["sections"]
    final_title = (title or "").strip() or _derive_title(sections.first_prompt)

    meta = []
    if parsed["started"]:
        meta.append(f"- **Started:** {fmt_dt(parsed['started'])}")
    if parsed["ended"]:
        meta.append(f"- **Ended:** {fmt_dt(parsed['ended'])}")
    if parsed["model"]:
        meta.append(f"- **Model:** {parsed['model']}")
    for key, val in (meta_extra or {}).items():
        if val:
            meta.append(f"- **{key}:** {val}")

    return {
        "markdown": build_markdown(final_title, meta, sections,
                                   assistant_label="OpenClaw"),
        "title": final_title,
        "first_prompt": sections.first_prompt,
        "started": parsed["started"],
        "cwd": parsed["cwd"],
        "session_id": parsed["session_id"],
        "model": parsed["model"],
        "user_turns": sections.user_turns,
    }


def _derive_title(first_prompt: str | None) -> str:
    if not first_prompt:
        return "OpenClaw session"
    title = " ".join(first_prompt.split())
    return title[:60] + ("…" if len(title) > 60 else "")


if __name__ == "__main__":
    import sys
    out = openclaw_to_md(sys.argv[1], meta_extra={"Source": "OpenClaw"},
                         include_thinking="--thinking" in sys.argv)
    sys.stdout.write(out["markdown"])
