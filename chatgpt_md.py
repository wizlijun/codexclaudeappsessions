#!/usr/bin/env python3
"""Render a ChatGPT data export (conversations.json) into Markdown.

The ChatGPT desktop/web app keeps conversations server-side; nothing usable is
stored locally. To export them you download your data from ChatGPT
(Settings → Data controls → Export), which yields a conversations.json: a list
of conversations, each with a `mapping` of message nodes forming a tree. We walk
from current_node up to the root to recover the linear conversation.
"""
from __future__ import annotations

import json

from render_common import (Sections, build_markdown, clean_text, epoch_to_dt,
                           fmt_dt, tool_note)


def _node_text(message: dict) -> str | None:
    """Extract displayable text from a ChatGPT message node."""
    if not message:
        return None
    content = message.get("content") or {}
    ctype = content.get("content_type")
    if ctype == "text":
        return "\n".join(content.get("parts") or []).strip()
    if ctype in ("multimodal_text",):
        parts = []
        for p in content.get("parts") or []:
            if isinstance(p, str):
                parts.append(p)
        return "\n".join(parts).strip()
    if ctype == "code":
        code = content.get("text") or ""
        return f"```\n{code}\n```" if code.strip() else ""
    return None


def _linear_path(mapping: dict, current_node: str | None) -> list[dict]:
    """Return message nodes from root to current_node in order."""
    nodes = []
    node_id = current_node
    # Fall back to the deepest node if current_node is missing.
    if node_id not in mapping:
        node_id = None
        for nid, n in mapping.items():
            if not n.get("children"):
                node_id = nid
                break
    while node_id:
        node = mapping.get(node_id)
        if not node:
            break
        nodes.append(node)
        node_id = node.get("parent")
    return list(reversed(nodes))


def conversation_to_md(conv: dict, include_thinking: bool = False) -> dict:
    mapping = conv.get("mapping") or {}
    sections = Sections()
    for node in _linear_path(mapping, conv.get("current_node")):
        message = node.get("message")
        if not message:
            continue
        author = (message.get("author") or {}).get("role")
        text = _node_text(message)
        if author == "user":
            cleaned = clean_text(text or "")
            # Skip hidden/system-ish user payloads.
            meta = message.get("metadata") or {}
            if cleaned and not meta.get("is_visually_hidden_from_conversation"):
                sections.add_user(cleaned)
        elif author == "assistant":
            if text and text.strip():
                sections.add_assistant(text.strip())
        elif author == "tool":
            name = (message.get("author") or {}).get("name") or "tool"
            sections.add_assistant(tool_note(name, None))

    title = conv.get("title") or _derive_title(sections.first_prompt)
    created = epoch_to_dt(conv.get("create_time"))
    updated = epoch_to_dt(conv.get("update_time"))
    meta = []
    if created:
        meta.append(f"- **Created:** {fmt_dt(created)}")
    if updated:
        meta.append(f"- **Updated:** {fmt_dt(updated)}")
    meta.append("- **Source:** ChatGPT (data export)")

    return {
        "markdown": build_markdown(title, meta, sections, assistant_label="ChatGPT"),
        "title": title,
        "started": created,
        "user_turns": sections.user_turns,
        "first_prompt": sections.first_prompt,
    }


def load_conversations(path: str) -> list[dict]:
    """Load a ChatGPT export; accepts either conversations.json or the wrapper."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "conversations" in data:
        data = data["conversations"]
    if not isinstance(data, list):
        return []
    # Only ChatGPT-format items have a `mapping` tree (distinguishes from Claude).
    return [c for c in data if isinstance(c, dict) and "mapping" in c]


def _derive_title(first_prompt: str | None) -> str:
    if not first_prompt:
        return "ChatGPT conversation"
    title = " ".join(first_prompt.split())
    return title[:60] + ("…" if len(title) > 60 else "")


if __name__ == "__main__":
    import sys
    for conv in load_conversations(sys.argv[1]):
        sys.stdout.write(conversation_to_md(conv)["markdown"])
        sys.stdout.write("\n\n")
