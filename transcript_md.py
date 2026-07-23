#!/usr/bin/env python3
"""Render Claude Code / Cowork JSONL session transcripts into Markdown.

Both Claude Code (the CLI) and Cowork (the Claude desktop "local agent mode")
store each session as a JSONL transcript in the standard Anthropic Messages
format — one JSON object per line. This module turns such a transcript into a
readable Q&A Markdown document, grouping each user prompt with the assistant's
reply and noting tool calls inline.

It is imported by export_sessions.py but can also be run standalone:

    python transcript_md.py path/to/session.jsonl
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys

from render_common import fmt_dt

# Content/text we never want to surface as a "user question": these are
# synthetic turns the harness injects (command stdout, system reminders, etc.).
_CMD_NAME_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
_CMD_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)
_TAG_STRIP_RE = re.compile(
    r"<(system-reminder|local-command-stdout|command-message|command-contents)>.*?"
    r"</\1>", re.S)
_CARET_TAG_RE = re.compile(r"</?(command-name|command-args)>")


def _to_blocks(content) -> list[dict]:
    """Normalize a message's content into a list of typed blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _clean_prompt(text: str) -> str | None:
    """Clean a human prompt, returning None if it is purely synthetic."""
    # Slash command invocation: surface it as `/command args`.
    name_match = _CMD_NAME_RE.search(text)
    if name_match:
        name = name_match.group(1).strip()
        args_match = _CMD_ARGS_RE.search(text)
        args = args_match.group(1).strip() if args_match else ""
        return f"`{name}` {args}".strip()

    # Drop harness-injected wrapped blocks, then see what's left.
    stripped = _TAG_STRIP_RE.sub("", text)
    stripped = _CARET_TAG_RE.sub("", stripped).strip()
    # The Claude Code "Caveat:" preamble is pure noise.
    if stripped.startswith("Caveat: The messages below were generated"):
        return None
    return stripped or None


def _render_assistant_blocks(blocks: list[dict], include_thinking: bool) -> list[str]:
    """Render assistant content blocks into Markdown fragments."""
    parts: list[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text)
        elif btype == "thinking" and include_thinking:
            think = (block.get("thinking") or "").strip()
            if think:
                parts.append(
                    "<details><summary>💭 thinking</summary>\n\n"
                    + think + "\n\n</details>")
        elif btype == "tool_use":
            name = block.get("name") or "tool"
            inp = block.get("input")
            detail = ""
            if isinstance(inp, dict):
                for key in ("command", "query", "url", "file_path", "path",
                            "pattern", "prompt", "description"):
                    if inp.get(key):
                        val = str(inp[key]).replace("\n", " ")
                        detail = f": `{val[:120]}`"
                        break
            parts.append(f"> 🔧 **{name}**{detail}")
    return parts


def _render_tool_results(blocks: list[dict]) -> list[str]:
    """Render tool_result blocks from a user turn as compact notes."""
    parts: list[str] = []
    for block in blocks:
        if block.get("type") == "tool_result":
            is_err = block.get("is_error")
            mark = "⚠️ error" if is_err else "ok"
            parts.append(f"> 📄 *tool result ({mark})*")
    return parts


def parse_transcript(jsonl_path: str, include_thinking: bool = False) -> dict:
    """Parse a JSONL transcript into sections + metadata.

    Returns a dict with keys: sections (list of {role, body}), first_prompt,
    started (datetime|None), ended (datetime|None), model (str|None).
    """
    sections: list[dict] = []
    first_prompt: str | None = None
    started = ended = None
    model = None
    cwd = git_branch = None

    def append_assistant(fragments: list[str]) -> None:
        if not fragments:
            return
        if sections and sections[-1]["role"] == "assistant":
            sections[-1]["parts"].extend(fragments)
        else:
            sections.append({"role": "assistant", "parts": list(fragments)})

    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            mtype = obj.get("type")
            if mtype not in ("user", "assistant"):
                continue

            ts = obj.get("timestamp")
            if ts:
                parsed = _parse_iso(ts)
                if parsed:
                    started = started or parsed
                    ended = parsed
            cwd = obj.get("cwd") or cwd
            git_branch = obj.get("gitBranch") or git_branch

            message = obj.get("message") or {}
            blocks = _to_blocks(message.get("content"))

            if mtype == "user":
                # A user turn is either a real prompt or fed-back tool results.
                tool_notes = _render_tool_results(blocks)
                texts = [b for b in blocks if b.get("type") == "text"
                         or isinstance(message.get("content"), str)]
                prompt_text = None
                if isinstance(message.get("content"), str):
                    prompt_text = _clean_prompt(message["content"])
                else:
                    joined = "\n\n".join((b.get("text") or "") for b in blocks
                                         if b.get("type") == "text").strip()
                    if joined:
                        prompt_text = _clean_prompt(joined)

                if prompt_text:
                    sections.append({"role": "user", "parts": [prompt_text]})
                    if first_prompt is None:
                        first_prompt = prompt_text
                elif tool_notes:
                    append_assistant(tool_notes)
            else:  # assistant
                model = model or message.get("model")
                append_assistant(_render_assistant_blocks(blocks, include_thinking))

    return {
        "sections": sections,
        "first_prompt": first_prompt,
        "started": started,
        "ended": ended,
        "model": model,
        "cwd": cwd,
        "git_branch": git_branch,
    }


def _parse_iso(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def transcript_to_md(jsonl_path: str, title: str, meta_extra: dict | None = None,
                     include_thinking: bool = False) -> dict:
    """Build a Markdown document for a JSONL transcript.

    Returns {"markdown": str, "first_prompt", "started", "ended", "model"} so the
    caller can also use the parsed metadata for indexing.
    """
    parsed = parse_transcript(jsonl_path, include_thinking=include_thinking)
    lines: list[str] = [f"# {title}", ""]

    meta = []
    if parsed["started"]:
        meta.append(f"- Started: {fmt_dt(parsed['started'])}")
    if parsed["ended"]:
        meta.append(f"- Ended: {fmt_dt(parsed['ended'])}")
    if parsed["model"]:
        meta.append(f"- Model: {parsed['model']}")
    for key, val in (meta_extra or {}).items():
        if val:
            meta.append(f"- {key}: {val}")
    turns = sum(1 for s in parsed["sections"] if s["role"] == "user")
    meta.append(f"- User turns: {turns}")
    lines.extend(meta)
    lines.extend(["", "---", ""])

    for section in parsed["sections"]:
        heading = "## 👤 User" if section["role"] == "user" else "## 🤖 Claude"
        lines.append(heading)
        lines.append("")
        body = "\n\n".join(p for p in section["parts"] if p.strip())
        lines.append(body if body.strip() else "*(no text content)*")
        lines.append("")

    markdown = "\n".join(lines).rstrip() + "\n"
    return {
        "markdown": markdown,
        "first_prompt": parsed["first_prompt"],
        "started": parsed["started"],
        "ended": parsed["ended"],
        "model": parsed["model"],
        "cwd": parsed["cwd"],
        "git_branch": parsed["git_branch"],
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python transcript_md.py SESSION.jsonl", file=sys.stderr)
        raise SystemExit(1)
    out = transcript_to_md(sys.argv[1], title="Session",
                           include_thinking="--thinking" in sys.argv)
    sys.stdout.write(out["markdown"])
