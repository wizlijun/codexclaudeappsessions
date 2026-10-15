#!/usr/bin/env python3
"""Render Cursor agent transcripts into Markdown.

Cursor keeps two copies of a chat:

* Finished sessions are JSONL files under
  ``~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl``.
  Each line is ``{"role", "message": {"content": [blocks]}}``.
* The live session is in ``state.vscdb`` (``cursorDiskKV``). ``composerData``
  holds a ``conversationState`` blob: a ``~`` plus base64 protobuf whose
  length-delimited 32-byte fields are ``agentKv:blob:<hash>`` keys, in order.
  Those blobs are JSON messages (``role`` + ``content``).

User prompts are wrapped in ``<user_query>``. A preceding ``<user_info>``
turn is harness context and is dropped. Tool calls are noted inline; tool
results are not inlined.
"""
from __future__ import annotations

import base64
import json
import os
import re

from render_common import (Sections, build_markdown, clean_text, epoch_to_dt,
                           fmt_dt, result_note, thinking_details, tool_note)

_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
_ATTACH_PATH_RE = re.compile(r'<code_selection\s+path="([^"]+)"')
_TIMESTAMP_RE = re.compile(r"<timestamp>.*?</timestamp>", re.S)
_HARNESS_BLOCK_RE = re.compile(
    r"<(user_info|rules|agent_skills|agent_transcripts|open_and_recently_viewed_files|"
    r"system-reminder|communication|mcp_file_system|available_skills|mcp_instructions)>"
    r".*?</\1>",
    re.S,
)


def clean_user_text(text: str) -> str | None:
    """Keep the human prompt and a short attachment note.

    Returns None for harness-only turns (the injected user_info / rules block).
    """
    text = text or ""
    if "<user_info>" in text and "<user_query>" not in text:
        return None
    queries = [q.strip() for q in _USER_QUERY_RE.findall(text) if q.strip()]
    paths = list(dict.fromkeys(_ATTACH_PATH_RE.findall(text)))
    parts: list[str] = []
    if queries:
        parts.append("\n\n".join(queries))
    if paths:
        parts.append("Attached: " + ", ".join(f"`{p}`" for p in paths))
    if parts:
        return "\n\n".join(parts)
    stripped = _HARNESS_BLOCK_RE.sub("", text)
    stripped = _TIMESTAMP_RE.sub("", stripped).strip()
    if not stripped or stripped.startswith("<"):
        return None
    return stripped


def _read_varint(buf: bytes, i: int) -> tuple[int, int] | None:
    n = shift = 0
    while i < len(buf) and shift <= 63:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7
    return None


def hashes_from_conversation_state(state: str | None) -> list[str]:
    """Pull ordered agentKv blob hashes out of a composer conversationState.

    The value is ``~`` plus standard base64 of a protobuf. Every
    length-delimited field of exactly 32 bytes is a blob id.
    """
    if not state or state == "~":
        return []
    raw = state[1:] if state.startswith("~") else state
    try:
        blob = base64.b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError):
        return []
    hashes: list[str] = []
    i = 0
    while i < len(blob):
        parsed = _read_varint(blob, i)
        if not parsed:
            break
        key, i = parsed
        wire = key & 7
        if wire == 0:
            parsed = _read_varint(blob, i)
            if not parsed:
                break
            _, i = parsed
        elif wire == 2:
            parsed = _read_varint(blob, i)
            if not parsed:
                break
            length, i = parsed
            if length < 0 or i + length > len(blob):
                break
            val = blob[i:i + length]
            i += length
            if length == 32:
                hashes.append(val.hex())
        elif wire == 1:
            if i + 8 > len(blob):
                break
            i += 8
        elif wire == 5:
            if i + 4 > len(blob):
                break
            i += 4
        else:
            break
    return hashes


def _blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, str):
                out.append({"type": "text", "text": block})
            elif isinstance(block, dict):
                out.append(block)
        return out
    return []


def _message_content(obj: dict):
    if "content" in obj and obj.get("content") is not None:
        return obj.get("content")
    message = obj.get("message")
    if isinstance(message, dict):
        return message.get("content")
    return None


def _tool_args(block: dict):
    args = block.get("args")
    if args is None:
        args = block.get("input")
    if args is None:
        args = block.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            pass
    if isinstance(args, dict) and args.get("glob_pattern") and not args.get("pattern"):
        args = dict(args)
        args["pattern"] = args["glob_pattern"]
    return args


def _tool_frag(block: dict) -> str:
    name = block.get("toolName") or block.get("name") or "tool"
    return tool_note(name, _tool_args(block))


def _result_frag(block: dict) -> str:
    is_err = bool(block.get("is_error") or block.get("isError"))
    return result_note(is_err)


def _consume(sections: Sections, obj: dict, include_thinking: bool) -> None:
    role = obj.get("role")
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    role = role or message.get("role")
    if role in ("system", "developer"):
        return
    blocks = _blocks(_message_content(obj))
    if role == "tool":
        notes = [_result_frag(b) for b in blocks] or [result_note(False)]
        sections.add_assistant(notes)
        return
    if role == "user":
        user_parts: list[str] = []
        notes: list[str] = []
        for block in blocks:
            btype = block.get("type")
            if btype in ("tool_result", "tool-result"):
                notes.append(_result_frag(block))
            elif btype in ("tool_use", "tool-call", "toolCall", "function_call"):
                notes.append(_tool_frag(block))
            else:
                cleaned = clean_user_text(block.get("text") or "")
                if cleaned:
                    user_parts.append(cleaned)
        if user_parts:
            sections.add_user("\n\n".join(user_parts))
        elif notes:
            sections.add_assistant(notes)
        return
    if role != "assistant":
        return
    frags: list[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype in ("text", "output_text", None):
            txt = clean_text(block.get("text") or "")
            if txt:
                frags.append(txt)
        elif btype in ("thinking", "reasoning"):
            if include_thinking:
                think = (block.get("thinking") or block.get("text") or "").strip()
                if think:
                    frags.append(thinking_details(think))
        elif btype in ("tool_use", "tool-call", "toolCall", "function_call"):
            frags.append(_tool_frag(block))
        elif btype in ("tool_result", "tool-result"):
            frags.append(_result_frag(block))
    sections.add_assistant(frags)


def parse_messages(messages: list[dict], include_thinking: bool = False) -> dict:
    sections = Sections()
    for obj in messages:
        if isinstance(obj, dict):
            _consume(sections, obj, include_thinking)
    return {"sections": sections}


def parse_jsonl(path: str, include_thinking: bool = False) -> dict:
    sections = Sections()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                _consume(sections, obj, include_thinking)
    return {"sections": sections}


def file_time_range(path: str) -> tuple:
    """(started, ended) from birth time and mtime. Both are absolute epochs."""
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    birth = getattr(st, "st_birthtime", None) or st.st_mtime
    return epoch_to_dt(birth), epoch_to_dt(st.st_mtime)


def _derive_title(first_prompt: str | None) -> str:
    if not first_prompt:
        return "Cursor session"
    # Attachment notes sit in a later paragraph; the title is the prompt itself.
    head = first_prompt.split("\n", 1)[0]
    title = " ".join(head.split())
    return title[:60] + ("…" if len(title) > 60 else "")


def cursor_to_md(sections: Sections, title: str | None, meta_extra: dict | None,
                 started=None, ended=None, model: str | None = None) -> dict:
    """Assemble Markdown from an already-parsed Sections accumulator."""
    final = (title or "").strip() or _derive_title(sections.first_prompt)
    meta = []
    if started:
        meta.append(f"- Started: {fmt_dt(started)}")
    if ended and ended != started:
        meta.append(f"- Ended: {fmt_dt(ended)}")
    if model:
        meta.append(f"- Model: {model}")
    for key, val in (meta_extra or {}).items():
        if val:
            meta.append(f"- {key}: {val}")
    return {
        "markdown": build_markdown(final, meta, sections, assistant_label="Cursor"),
        "title": final,
        "first_prompt": sections.first_prompt,
        "started": started,
        "ended": ended,
        "model": model,
        "user_turns": sections.user_turns,
    }


def cursor_jsonl_to_md(path: str, meta_extra: dict | None = None,
                       include_thinking: bool = False,
                       title: str | None = None) -> dict:
    parsed = parse_jsonl(path, include_thinking=include_thinking)
    started, ended = file_time_range(path)
    return cursor_to_md(parsed["sections"], title, meta_extra,
                        started=started, ended=ended)


def messages_from_blobs(hashes: list[str], blobs: dict[str, bytes]) -> list[dict]:
    """Resolve conversationState hashes to JSON messages, skipping protobuf blobs."""
    messages = []
    for digest in hashes:
        val = blobs.get(digest)
        if not val:
            continue
        if isinstance(val, str):
            val = val.encode()
        if not val.startswith(b"{"):
            continue
        try:
            obj = json.loads(val.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict):
            messages.append(obj)
    return messages


if __name__ == "__main__":
    import sys
    out = cursor_jsonl_to_md(sys.argv[1], meta_extra={"Source": "Cursor"},
                             include_thinking="--thinking" in sys.argv)
    sys.stdout.write(out["markdown"])
