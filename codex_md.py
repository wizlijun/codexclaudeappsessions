#!/usr/bin/env python3
"""Render OpenAI Codex CLI rollout transcripts into Markdown.

Codex stores each session as ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.
Lines are typed: `session_meta` (cwd, model provider, git), `turn_context`
(model), and `response_item` whose payload is one of message / reasoning /
function_call / function_call_output.
"""
from __future__ import annotations

import json

from render_common import (Sections, build_markdown, fmt_dt, parse_iso,
                           result_note, thinking_details, tool_note)

# User turns Codex injects (AGENTS.md, environment context) are not real prompts.
_INJECTED_MARKERS = ("<environment_context>", "<user_instructions>",
                     "# AGENTS.md instructions", "<INSTRUCTIONS>")


def _message_text(content) -> str:
    """Join the text parts of a Codex message content list."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") in (
                "input_text", "output_text", "text", "summary_text"):
            parts.append(block.get("text") or "")
    return "\n".join(p for p in parts if p)


def parse_codex(path: str, include_thinking: bool = False) -> dict:
    sections = Sections()
    cwd = model = git = None
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

            ts = parse_iso(obj.get("timestamp"))
            if ts:
                started = started or ts
                ended = ts

            otype = obj.get("type")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

            if otype == "session_meta":
                cwd = payload.get("cwd") or cwd
                model = payload.get("model") or model
                git = payload.get("git") or git
                continue
            if otype == "turn_context":
                model = payload.get("model") or model
                continue
            if otype != "response_item":
                continue

            ptype = payload.get("type")
            if ptype == "message":
                role = payload.get("role")
                text = _message_text(payload.get("content")).strip()
                if not text:
                    continue
                if role == "user":
                    if any(m in text for m in _INJECTED_MARKERS):
                        continue
                    sections.add_user(text)
                elif role == "assistant":
                    sections.add_assistant(text)
                # developer/system roles are skipped.
            elif ptype == "reasoning":
                if include_thinking:
                    text = _message_text(payload.get("summary")).strip()
                    if text:
                        sections.add_assistant(thinking_details(text))
            elif ptype == "function_call":
                args = payload.get("arguments")
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                except json.JSONDecodeError:
                    pass
                sections.add_assistant(tool_note(payload.get("name"), args))
            elif ptype == "function_call_output":
                out = payload.get("output")
                is_err = isinstance(out, dict) and (
                    out.get("success") is False or out.get("error"))
                sections.add_assistant(result_note(bool(is_err)))

    return {"sections": sections, "cwd": cwd, "model": model, "git": git,
            "started": started, "ended": ended}


def codex_to_md(path: str, meta_extra: dict, include_thinking: bool = False) -> dict:
    parsed = parse_codex(path, include_thinking=include_thinking)
    sections = parsed["sections"]
    title = _derive_title(sections.first_prompt)

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
        "markdown": build_markdown(title, meta, sections, assistant_label="Codex"),
        "title": title,
        "first_prompt": sections.first_prompt,
        "started": parsed["started"],
        "cwd": parsed["cwd"],
        "git": parsed["git"],
        "user_turns": sections.user_turns,
    }


def _derive_title(first_prompt: str | None) -> str:
    if not first_prompt:
        return "Codex session"
    title = " ".join(first_prompt.split())
    return title[:60] + ("…" if len(title) > 60 else "")


if __name__ == "__main__":
    import sys
    out = codex_to_md(sys.argv[1], meta_extra={"Source": "Codex"},
                      include_thinking="--thinking" in sys.argv)
    sys.stdout.write(out["markdown"])
