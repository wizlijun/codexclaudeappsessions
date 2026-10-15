#!/usr/bin/env python3
"""Organize AI coding/chat sessions into Markdown, grouped by vendor & account.

Output layout:

    sessions_md/
      claude/<account>/{chat,cowork,claude-code}/*.md
      openai/<account>/{chatgpt,codex}/*.md
      droid/<account>/*.md
      openclaw/<account>/{webchat,weixin,wecom,cron,subagent,...}/*.md
      cursor/<account>/{agent,subagent}/<project>/*.md
      grokbot/<account>/chat/<bot>/*.md
      index-<device>.md        (per-device landing: totals + month links)
      index-<device>-YYYY-MM.md (one concise line per session that month)

Sources and where they live:
  * claude/chat        - Claude app export (conversations.json + users.json)
  * claude/cowork      - ~/Library/Application Support/Claude/local-agent-mode-sessions
  * claude/claude-code - ~/.claude/projects/<proj>/*.jsonl
  * openai/codex       - ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
  * openai/chatgpt     - a ChatGPT data export conversations.json (chats are
                         server-side; pass one with --chatgpt-export)
  * droid              - ~/.factory/sessions/<proj>/<uuid>.jsonl
  * openclaw           - ~/.openclaw/agents/<agent>/sessions/<uuid>.jsonl
  * cursor             - ~/.cursor/projects/<slug>/agent-transcripts/<id>/*.jsonl
                         and the live composer in Cursor's global state.vscdb
  * grokbot            - ~/Library/Application Support/Grok Bot/sand-client-persistence
                         (transcript.replicas blobs; rolling window, archived
                         under <output>/grokbot/.archive). Optional fallback:
                         grokbot_data → agent-data store.db or a .tgz.
                         ~/git/grokbot is never read.

Account attribution: chat from users.json; cowork per-session from its metadata;
claude-code/codex from the current CLI login (no per-session account is
recorded); droid from the session owner; openclaw from authProfileOverride
(or agent id); cursor from the current Cursor login; grokbot from that same
login (Grok Bot signs in with Cursor) unless grokbot_account is set.

Usage:
    python export_sessions.py                          # everything found
    python export_sessions.py --vendor openai           # just OpenAI
    python export_sessions.py --vendor openclaw
    python export_sessions.py --vendor droid --project hemory
    python export_sessions.py --chatgpt-export ~/Downloads/conversations.json
    python export_sessions.py --thinking                # include reasoning
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import uuid
from urllib.parse import unquote, urlparse

import render_common as rc
from chatgpt_md import conversation_to_md as chatgpt_conv_to_md
from chatgpt_md import load_conversations as load_chatgpt
from codex_md import codex_to_md
from cursor_md import (cursor_jsonl_to_md, cursor_to_md, hashes_from_conversation_state,
                       messages_from_blobs, parse_messages)
from droid_md import droid_to_md
import grokbot_md
from openclaw_md import openclaw_to_md
from transcript_md import transcript_to_md

HOME = os.path.expanduser("~")
COWORK_BASE = os.path.join(
    HOME, "Library", "Application Support", "Claude", "local-agent-mode-sessions")
CLAUDE_CODE_BASE = os.path.join(HOME, ".claude", "projects")
CLAUDE_CONFIG = os.path.join(HOME, ".claude.json")
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")
CODEX_AUTH = os.path.join(HOME, ".codex", "auth.json")
DROID_SESSIONS = os.path.join(HOME, ".factory", "sessions")
OPENCLAW_AGENTS = os.path.join(HOME, ".openclaw", "agents")
OPENCLAW_CONFIG = os.path.join(HOME, ".openclaw", "openclaw.json")
CURSOR_PROJECTS = os.path.join(HOME, ".cursor", "projects")
CURSOR_STATE_DB = os.path.join(
    HOME, "Library", "Application Support", "Cursor", "User",
    "globalStorage", "state.vscdb")
CURSOR_WORKSPACES = os.path.join(
    HOME, "Library", "Application Support", "Cursor", "User", "workspaceStorage")

# Config file lives next to this script.
DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yml")
DEFAULT_OUTPUT = "sessions_md"


def load_config(path: str) -> dict:
    """Load config.yml if present; return {} on any problem (config is optional)."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        import yaml
    except ImportError:
        print(f"  (config) PyYAML not installed; ignoring {path}", file=sys.stderr)
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"  (config) could not read {path}: {exc}", file=sys.stderr)
        return {}

# Per-directory registry so filenames stay unique within each output folder.
_USED: dict[str, set[str]] = {}
# Per-project aggregation (keyed by the project's relative output dir) used to
# write a _project.md meta file with git info for each project bucket.
_PROJECTS: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def slugify(text: str, fallback: str) -> str:
    import re
    text = (text or "").strip() or fallback
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "-", text.strip())
    return text[:60].strip("-") or fallback


def account_slug(label: str | None) -> str:
    import re
    base = (label or "unknown-account").strip()
    return re.sub(r"[^\w@.\-]", "_", base) or "unknown-account"


def reserve(out_dir: str, base: str, disambiguator: str | None = None) -> str:
    """Claim a unique filename stem within `out_dir`.

    The manifest is the primary identity source: a known session overwrites its
    recorded path (see run_tasks/force_path) and never reaches here. So any
    collision on `base` is between DIFFERENT sessions sharing a date+title — we
    keep both. `disambiguator` (the session start time, HHMMSS) makes the second
    name deterministic from content, so it stays stable even if the manifest is
    lost. Only if that also collides (or is absent) do we fall back to -2/-3.
    """
    used = _USED.setdefault(out_dir, set())
    if base not in used:
        used.add(base)
        return base
    stem = f"{base}-{disambiguator}" if disambiguator else base
    name, n = stem, 2
    while name in used:
        name = f"{stem}-{n}"
        n += 1
    used.add(name)
    return name


def write_session(out_dir: str, filename: str, markdown: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    # Some transcripts contain lone surrogate code points (broken emoji from the
    # source JSON) that are not encodable as UTF-8; drop them before writing.
    markdown = re.sub(r"[\ud800-\udfff]", "", markdown)
    with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as fh:
        fh.write(markdown)


def emit(out_root: str, vendor: str, account: str, source: str | None,
         when, title: str, markdown: str, turns: int,
         project: str | None = None, cwd: str | None = None,
         git: dict | None = None, force_path: str | None = None) -> dict:
    """Write one session file and return its index row.

    High-volume agent sources (codex, droid, claude-code, openclaw, cursor,
    grokbot) pass `project`
    to bucket sessions into per-project subfolders, plus `cwd`/`git` so a
    per-project _project.md can describe the repository. `force_path` (relative
    to out_root) overwrites an existing session's file in place during
    incremental re-export.
    """
    acct = account_slug(account)
    rel_parts = [vendor, acct] + ([source] if source else [])
    proj_rel = "/".join(rel_parts + [slugify(project, "project")]) if project else None
    base_rel = proj_rel or "/".join(rel_parts)

    if force_path:
        rel_path = force_path
    else:
        out_dir = os.path.join(out_root, *base_rel.split("/"))
        local_when = rc.to_local(when)
        prefix = local_when.strftime("%Y-%m-%d") if local_when else "0000-00-00"
        start_time = local_when.strftime("%H%M%S") if local_when else None
        stem = reserve(out_dir, f"{prefix}-{slugify(title, 'session')}", start_time)
        rel_path = f"{base_rel}/{stem}.md"

    write_session(os.path.join(out_root, os.path.dirname(rel_path)),
                  os.path.basename(rel_path), markdown)

    return {
        "vendor": vendor, "account": account, "source": source or vendor,
        "project": project, "proj_rel": proj_rel,
        "project_path": f"{proj_rel}/_project.md" if project else None,
        "cwd": cwd, "git": git, "device": device_id(),
        "when": when, "title": title, "turns": turns, "path": rel_path,
    }


def record_project(proj_rel: str, vendor: str, account: str, project: str,
                   cwd: str | None, git: dict | None, when) -> None:
    """Aggregate per-project metadata across that project's sessions."""
    entry = _PROJECTS.get(proj_rel)
    if entry is None:
        entry = {"vendor": vendor, "account": account, "project": project,
                 "cwds": set(), "branches": set(), "repo_url": None,
                 "commit": None, "count": 0, "first": None, "last": None}
        _PROJECTS[proj_rel] = entry
    entry["count"] += 1
    if cwd:
        entry["cwds"].add(cwd)
    if git:
        if git.get("repository_url"):
            entry["repo_url"] = git["repository_url"]
        if git.get("branch"):
            entry["branches"].add(git["branch"])
        if git.get("commit_hash"):
            entry["commit"] = git["commit_hash"]
    if when:
        entry["first"] = min(entry["first"], when) if entry["first"] else when
        entry["last"] = max(entry["last"], when) if entry["last"] else when


def aggregate_projects(rows: list[dict]) -> None:
    """Rebuild the per-project registry from all index rows (incremental-safe)."""
    _PROJECTS.clear()
    for r in rows:
        if r.get("project") and r.get("proj_rel"):
            record_project(r["proj_rel"], r["vendor"], r["account"], r["project"],
                           r.get("cwd"), r.get("git"), r.get("when"))


def live_git_info(cwd: str | None, cache: dict) -> dict:
    """Query a live repo at cwd for remote/branch/commit (cached per cwd)."""
    if not cwd:
        return {}
    if cwd in cache:
        return cache[cwd]
    info: dict = {}
    if os.path.isdir(cwd):
        def g(args):
            try:
                r = subprocess.run(["git", "-C", cwd, *args],
                                   capture_output=True, text=True, timeout=5)
                return r.stdout.strip() if r.returncode == 0 else None
            except (subprocess.SubprocessError, OSError):
                return None
        if g(["rev-parse", "--is-inside-work-tree"]) == "true":
            info["remote"] = g(["remote", "get-url", "origin"])
            info["branch"] = g(["rev-parse", "--abbrev-ref", "HEAD"])
            info["root"] = g(["rev-parse", "--show-toplevel"])
            info["last_commit"] = g(["log", "-1", "--format=%h %s"])
    cache[cwd] = info
    return info


def write_project_metas(out_root: str) -> int:
    """Write a _project.md into every project bucket, enriched with git info."""
    cache: dict = {}
    for proj_rel, e in _PROJECTS.items():
        cwd = sorted(e["cwds"])[0] if e["cwds"] else None
        live = live_git_info(cwd, cache)
        remote = e["repo_url"] or live.get("remote")

        lines = [f"# Project: {e['project']}", ""]
        lines.append(f"- **Vendor / Account:** {e['vendor']} / {e['account']}")
        if cwd:
            lines.append(f"- **Working dir:** {cwd}")
        if remote:
            lines.append(f"- **Git remote:** {remote}")
        if live.get("branch"):
            lines.append(f"- **Current branch:** {live['branch']}")
        branches = sorted(b for b in e["branches"] if b)
        if branches:
            lines.append(f"- **Branches seen in sessions:** {', '.join(branches)}")
        if e["commit"]:
            lines.append(f"- **Commit at session time:** `{e['commit'][:12]}`")
        if live.get("last_commit"):
            lines.append(f"- **Latest commit (now):** {live['last_commit']}")
        if not remote and not cwd:
            lines.append("- *(no git information available)*")
        elif not remote:
            lines.append("- *(working dir is not a git repo or no origin remote)*")
        span = ""
        if e["first"]:
            span = rc.fmt_dt(e["first"]) + (f" → {rc.fmt_dt(e['last'])}" if e["last"] else "")
        if span:
            lines.append(f"- **Sessions:** {e['count']}  ({span})")
        else:
            lines.append(f"- **Sessions:** {e['count']}")

        out_dir = os.path.join(out_root, *proj_rel.split("/"))
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "_project.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines).rstrip() + "\n")
    return len(_PROJECTS)


def current_claude_email() -> str | None:
    try:
        with open(CLAUDE_CONFIG, encoding="utf-8") as fh:
            return (json.load(fh).get("oauthAccount") or {}).get("emailAddress")
    except (OSError, json.JSONDecodeError):
        return None


def current_cursor_email() -> str | None:
    """Account email from the Cursor login. Tokens are not read."""
    if not os.path.isfile(CURSOR_STATE_DB):
        return None
    try:
        con = sqlite3.connect(f"file:{CURSOR_STATE_DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                ("cursorAuth/cachedEmail",),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    email = row[0]
    if isinstance(email, bytes):
        email = email.decode("utf-8", "replace")
    email = str(email).strip().strip('"')
    return email or None


def current_codex_email() -> str | None:
    """Best-effort: decode the email claim from the Codex id_token JWT."""
    try:
        with open(CODEX_AUTH, encoding="utf-8") as fh:
            auth = json.load(fh)
        token = (auth.get("tokens") or {}).get("id_token")
        if token and token.count(".") >= 2:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            email = claims.get("email") or (
                claims.get("https://api.openai.com/profile") or {}).get("email")
            if email:
                return email
        acct = (auth.get("tokens") or {}).get("account_id")
        return f"codex-{acct}" if acct else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def derive_title(first_prompt: str | None, fallback: str) -> str:
    if not first_prompt:
        return fallback
    title = " ".join(first_prompt.split())
    return title[:60] + ("…" if len(title) > 60 else "")


def peek_cwd(jsonl_path: str) -> str | None:
    try:
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    cwd = json.loads(line).get("cwd")
                    if cwd:
                        return cwd
    except (OSError, json.JSONDecodeError):
        pass
    return None


# --------------------------------------------------------------------------- #
# Claude: chat
# --------------------------------------------------------------------------- #
def find_export_dir() -> str | None:
    if os.path.isfile("conversations.json"):
        return "."
    cands = [d for d in glob.glob("data-*")
             if os.path.isfile(os.path.join(d, "conversations.json"))]
    return cands[0] if len(cands) == 1 else None


def chat_account(export_dir: str) -> str:
    try:
        with open(os.path.join(export_dir, "users.json"), encoding="utf-8") as fh:
            users = json.load(fh)
        if isinstance(users, list) and users:
            return users[0].get("email_address") or users[0].get("full_name") or "unknown-account"
    except (OSError, json.JSONDecodeError):
        pass
    return "unknown-account"


def render_chat_message(msg: dict) -> str:
    parts = []
    for block in msg.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            txt = (block.get("text") or "").strip()
            if txt:
                parts.append(txt)
        elif btype == "tool_use":
            parts.append(rc.tool_note(block.get("name"), block.get("input")))
        elif btype == "tool_result":
            parts.append(rc.result_note())
    if not parts:
        txt = (msg.get("text") or "").strip()
        if txt:
            parts.append(txt)
    return "\n\n".join(parts)


def chat_to_md(conv: dict) -> str:
    name = conv.get("name") or "(untitled)"
    lines = [f"# {name}", ""]
    if conv.get("created_at"):
        lines.append(f"- Created: {rc.fmt_dt(rc.parse_iso(conv['created_at']))}")
    if conv.get("updated_at"):
        lines.append(f"- Updated: {rc.fmt_dt(rc.parse_iso(conv['updated_at']))}")
    messages = conv.get("chat_messages") or []
    lines.append(f"- Messages: {len(messages)}")
    if conv.get("uuid"):
        lines.append(f"- UUID: `{conv['uuid']}`")
    summary = (conv.get("summary") or "").strip()
    if summary:
        lines.extend(["", f"> {summary}"])
    lines.extend(["", "---", ""])
    for msg in messages:
        heading = "## 👤 User" if msg.get("sender") == "human" else "## 🤖 Claude"
        ts = rc.fmt_dt(rc.parse_iso(msg.get("created_at", "")))
        if ts:
            heading += f"  <sub>{ts}</sub>"
        lines.extend([heading, ""])
        body = render_chat_message(msg).strip()
        lines.append(body if body else "*(no text content)*")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def chat_tasks(export_dir: str | None):
    """Yield (key, version, vendor, render) for each chat conversation."""
    export_dir = export_dir or find_export_dir()
    if not export_dir or not os.path.isfile(os.path.join(export_dir, "conversations.json")):
        print("  (chat) no Claude export found; skipping", file=sys.stderr)
        return
    with open(os.path.join(export_dir, "conversations.json"), encoding="utf-8") as fh:
        conversations = json.load(fh)
    if conversations and isinstance(conversations[0], dict) and "mapping" in conversations[0]:
        print("  (chat) conversations.json looks like a ChatGPT export; skipping", file=sys.stderr)
        return
    account = chat_account(export_dir)
    for conv in conversations:
        key = f"chat:{conv.get('uuid')}"
        version = conv.get("updated_at") or conv.get("created_at") or ""

        def render(conv=conv):
            return {
                "account": account, "source": "chat", "project": None,
                "when": rc.parse_iso(conv.get("created_at", "")),
                "title": conv.get("name") or "(untitled)",
                "turns": len(conv.get("chat_messages") or []),
                "markdown": chat_to_md(conv), "cwd": None, "git": None,
            }
        yield key, version, "claude", render


# --------------------------------------------------------------------------- #
# Claude: cowork + claude-code (JSONL transcripts)
# --------------------------------------------------------------------------- #
def cowork_tasks(thinking: bool):
    if not os.path.isdir(COWORK_BASE):
        print("  (cowork) none found; skipping", file=sys.stderr)
        return
    for meta_path in glob.glob(os.path.join(COWORK_BASE, "*", "*", "local_*.json")):
        session_dir = meta_path[:-len(".json")]
        if not os.path.isdir(session_dir):
            continue
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        cli_id = meta.get("cliSessionId")
        if not cli_id:
            continue
        matches = glob.glob(os.path.join(session_dir, ".claude", "projects", "*", f"{cli_id}.jsonl"))
        if not matches:
            continue
        jsonl = matches[0]
        key = jsonl
        version = _mtime(jsonl)

        def render(jsonl=jsonl, meta=meta):
            account = meta.get("emailAddress") or meta.get("accountName") or \
                os.path.basename(os.path.dirname(os.path.dirname(meta_path)))
            title = (meta.get("title") or "").strip() or None
            extra = {"Account": account, "Source": "Cowork (local agent mode)",
                     "Working dir": meta.get("cwd")}
            result = transcript_to_md(jsonl, title=title or "Session",
                                      meta_extra=extra, include_thinking=thinking)
            if result["first_prompt"] is None and "## 👤 User" not in result["markdown"]:
                return None
            final_title = title or derive_title(result["first_prompt"], "Session")
            if title is None:
                result = transcript_to_md(jsonl, title=final_title, meta_extra=extra,
                                          include_thinking=thinking)
            when = rc.epoch_to_dt(meta.get("createdAt")) or result["started"]
            return {"account": account, "source": "cowork", "project": None,
                    "when": when, "title": final_title, "markdown": result["markdown"],
                    "turns": result["markdown"].count("## 👤 User"),
                    "cwd": None, "git": None}
        yield key, version, "claude", render


def claude_code_tasks(thinking: bool, project: str | None, account_override: str | None):
    if not os.path.isdir(CLAUDE_CODE_BASE):
        print("  (claude-code) none found; skipping", file=sys.stderr)
        return
    account = account_override or current_claude_email() or "unknown-account"
    for proj_dir in sorted(glob.glob(os.path.join(CLAUDE_CODE_BASE, "*"))):
        if not os.path.isdir(proj_dir):
            continue
        for jsonl in sorted(glob.glob(os.path.join(proj_dir, "*.jsonl"))):
            key = jsonl
            version = _mtime(jsonl)

            def render(jsonl=jsonl, proj_dir=proj_dir):
                cwd = peek_cwd(jsonl)
                proj = os.path.basename(cwd) if cwd else os.path.basename(proj_dir)
                if project and project.lower() not in (cwd or proj_dir).lower():
                    return None
                extra = {"Account": f"{account} (current login)", "Source": "Claude Code",
                         "Project": proj, "Working dir": cwd}
                result = transcript_to_md(jsonl, title="Session", meta_extra=extra,
                                          include_thinking=thinking)
                if result["first_prompt"] is None and "## 👤 User" not in result["markdown"]:
                    return None
                title = derive_title(result["first_prompt"], "Session")
                result = transcript_to_md(jsonl, title=title, meta_extra=extra,
                                          include_thinking=thinking)
                return {"account": account, "source": "claude-code", "project": proj,
                        "when": result["started"], "title": title,
                        "markdown": result["markdown"],
                        "turns": result["markdown"].count("## 👤 User"),
                        "cwd": cwd, "git": {"branch": result.get("git_branch")}}
            yield key, version, "claude", render


# --------------------------------------------------------------------------- #
# OpenAI: codex + chatgpt
# --------------------------------------------------------------------------- #
def codex_tasks(thinking: bool, project: str | None, account_override: str | None):
    if not os.path.isdir(CODEX_SESSIONS):
        print("  (codex) none found; skipping", file=sys.stderr)
        return
    account = account_override or current_codex_email() or "unknown-account"
    for jsonl in sorted(glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", "rollout-*.jsonl"))):
        key = jsonl
        version = _mtime(jsonl)

        def render(jsonl=jsonl):
            result = codex_to_md(jsonl, meta_extra={"Account": f"{account} (current login)",
                                                   "Source": "Codex"}, include_thinking=thinking)
            if result["user_turns"] == 0:
                return None
            proj = os.path.basename(result["cwd"]) if result.get("cwd") else None
            if project and project.lower() not in (result.get("cwd") or "").lower():
                return None
            if proj:
                result = codex_to_md(jsonl, meta_extra={
                    "Account": f"{account} (current login)", "Source": "Codex",
                    "Project": proj}, include_thinking=thinking)
            return {"account": account, "source": "codex", "project": proj,
                    "when": result["started"], "title": result["title"],
                    "markdown": result["markdown"], "turns": result["user_turns"],
                    "cwd": result.get("cwd"), "git": result.get("git")}
        yield key, version, "openai", render


def _resolve_chatgpt_export(export_path: str) -> str | None:
    """Accept either conversations.json or the unzipped export folder.

    A ChatGPT data export (Settings -> Data Controls -> Export) arrives as a zip
    that unpacks to a folder containing conversations.json (+ user.json), so
    pointing chatgpt_export at that folder should just work.
    """
    if os.path.isdir(export_path):
        cand = os.path.join(export_path, "conversations.json")
        return cand if os.path.isfile(cand) else None
    return export_path if os.path.isfile(export_path) else None


def chatgpt_tasks(export_path: str | None):
    if not export_path:
        print("  (chatgpt) no chatgpt_export configured; ChatGPT chats are server-side "
              "and the macOS app stores them encrypted locally, so they cannot be read "
              "directly. Download a data export (Settings -> Data Controls -> Export) and "
              "set chatgpt_export to the unzipped folder or its conversations.json.",
              file=sys.stderr)
        return
    resolved = _resolve_chatgpt_export(export_path)
    if not resolved:
        print(f"  (chatgpt) no conversations.json found at {export_path}; skipping",
              file=sys.stderr)
        return
    export_path = resolved
    account = _chatgpt_account(export_path)
    for conv in load_chatgpt(export_path):
        key = f"chatgpt:{conv.get('id') or conv.get('conversation_id')}"
        version = conv.get("update_time") or conv.get("create_time") or ""

        def render(conv=conv):
            result = chatgpt_conv_to_md(conv)
            if result["user_turns"] == 0:
                return None
            return {"account": account, "source": "chatgpt", "project": None,
                    "when": result["started"], "title": result["title"],
                    "markdown": result["markdown"], "turns": result["user_turns"],
                    "cwd": None, "git": None}
        yield key, version, "openai", render


def _chatgpt_account(export_path: str) -> str:
    """Read account email from a sibling user.json in the export, if present."""
    user_json = os.path.join(os.path.dirname(export_path), "user.json")
    try:
        with open(user_json, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("email") or "chatgpt-account"
    except (OSError, json.JSONDecodeError):
        return "chatgpt-account"


# --------------------------------------------------------------------------- #
# Droid
# --------------------------------------------------------------------------- #
def droid_tasks(thinking: bool, project: str | None):
    if not os.path.isdir(DROID_SESSIONS):
        print("  (droid) none found; skipping", file=sys.stderr)
        return
    for jsonl in sorted(glob.glob(os.path.join(DROID_SESSIONS, "*", "*.jsonl"))):
        # Cheap path pre-filter (dir name encodes the cwd) for filtered runs.
        if project and project.lower() not in jsonl.lower():
            continue
        key = jsonl
        version = _mtime(jsonl)

        def render(jsonl=jsonl):
            result = droid_to_md(jsonl, meta_extra={"Source": "Droid"},
                                 include_thinking=thinking)
            if result["user_turns"] == 0:
                return None
            cwd = result.get("cwd")
            proj = os.path.basename(cwd) if cwd else os.path.basename(os.path.dirname(jsonl))
            account = result.get("owner") or "droid"
            result = droid_to_md(jsonl, meta_extra={"Source": "Droid", "Project": proj,
                                                   "Working dir": cwd, "Owner": account},
                                include_thinking=thinking)
            return {"account": account, "source": None, "project": proj,
                    "when": result["started"], "title": result["title"],
                    "markdown": result["markdown"], "turns": result["user_turns"],
                    "cwd": cwd, "git": None}
        yield key, version, "droid", render


# --------------------------------------------------------------------------- #
# OpenClaw
# --------------------------------------------------------------------------- #
def _openclaw_account_slug(profile: str | None, agent: str) -> str:
    """Normalize authProfileOverride (e.g. openai:user@x.com) to a short label."""
    if not profile:
        return agent or "openclaw"
    # "openai:projecthemory@gmail.com" / "openai-codex:foo@bar.com" → email
    if ":" in profile and "@" in profile:
        return profile.split(":", 1)[1]
    return profile


def _openclaw_source_from_key(session_key: str | None) -> str:
    """Map sessions.json key like agent:main:cron:… / openclaw-weixin:… → source."""
    if not session_key:
        return "session"
    parts = session_key.split(":")
    # agent:<id>:<channel>[:…]
    if len(parts) >= 3 and parts[0] == "agent":
        channel = parts[2]
        if channel == "openclaw-weixin":
            return "weixin"
        if channel in ("main", "explicit"):
            return "webchat"
        if channel in ("cron", "subagent", "dashboard", "wecom", "webchat"):
            return channel
        return channel
    return "session"


def _load_openclaw_index(agent_dir: str) -> dict[str, dict]:
    """sessionId → index entry from sessions.json (best-effort)."""
    path = os.path.join(agent_dir, "sessions", "sessions.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    by_id: dict[str, dict] = {}
    if not isinstance(data, dict):
        return by_id
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        sid = entry.get("sessionId")
        if not sid:
            continue
        # Prefer the richest / latest entry when multiple keys share a sessionId.
        prev = by_id.get(sid)
        if prev is None or (entry.get("updatedAt") or 0) >= (prev.get("updatedAt") or 0):
            enriched = dict(entry)
            enriched["_sessionKey"] = key
            by_id[sid] = enriched
    # Also index by sessionFile basename for files not currently in the live map
    # (historical sessions whose index slot was rotated away).
    return by_id


def _openclaw_index_by_file(agent_dir: str) -> dict[str, dict]:
    """Absolute sessionFile path → index entry."""
    path = os.path.join(agent_dir, "sessions", "sessions.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    by_file: dict[str, dict] = {}
    if not isinstance(data, dict):
        return by_file
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        sf = entry.get("sessionFile")
        if not sf:
            continue
        enriched = dict(entry)
        enriched["_sessionKey"] = key
        by_file[os.path.abspath(sf)] = enriched
    return by_file


def openclaw_tasks(thinking: bool, project: str | None):
    if not os.path.isdir(OPENCLAW_AGENTS):
        print("  (openclaw) none found; skipping", file=sys.stderr)
        return
    for agent_dir in sorted(glob.glob(os.path.join(OPENCLAW_AGENTS, "*"))):
        if not os.path.isdir(agent_dir):
            continue
        agent = os.path.basename(agent_dir)
        if agent.startswith("."):
            continue
        sessions_dir = os.path.join(agent_dir, "sessions")
        if not os.path.isdir(sessions_dir):
            continue
        by_file = _openclaw_index_by_file(agent_dir)
        by_id = _load_openclaw_index(agent_dir)

        for jsonl in sorted(glob.glob(os.path.join(sessions_dir, "*.jsonl"))):
            base = os.path.basename(jsonl)
            # Skip trajectory traces, checkpoints, resets, and other non-session logs.
            # Primary sessions are plain <uuid>.jsonl only.
            if (base.endswith(".trajectory.jsonl")
                    or ".checkpoint." in base
                    or ".reset." in base
                    or base.endswith(".bak")
                    or ".migrated" in base
                    or ".pre-doctor" in base
                    or base.count(".") != 1):
                continue
            key = jsonl
            version = _mtime(jsonl)

            def render(jsonl=jsonl, agent=agent, by_file=by_file, by_id=by_id):
                sid = os.path.splitext(os.path.basename(jsonl))[0]
                meta = by_file.get(os.path.abspath(jsonl)) or by_id.get(sid) or {}
                session_key = meta.get("_sessionKey")
                source = _openclaw_source_from_key(session_key)
                account = _openclaw_account_slug(
                    meta.get("authProfileOverride"), agent)
                label = (meta.get("label") or "").strip() or None
                origin = meta.get("origin") or {}
                surface = origin.get("surface") or origin.get("provider")
                model = meta.get("model")
                cwd_hint = None
                # systemPromptReport carries workspaceDir when present.
                spr = meta.get("systemPromptReport") or {}
                if isinstance(spr, dict):
                    cwd_hint = spr.get("workspaceDir")

                result = openclaw_to_md(
                    jsonl,
                    meta_extra={"Source": f"OpenClaw ({source})",
                                "Agent": agent},
                    include_thinking=thinking,
                    title=label,
                )
                if result["user_turns"] == 0:
                    return None
                cwd = result.get("cwd") or cwd_hint
                proj = os.path.basename(cwd) if cwd else None
                if project and project.lower() not in (
                        (cwd or "") + " " + (label or "") + " " + agent).lower():
                    return None

                extra = {
                    "Source": f"OpenClaw ({source})",
                    "Agent": agent,
                    "Project": proj,
                    "Working dir": cwd,
                    "Channel": surface,
                    "Session": sid,
                }
                # Prefer index model when the transcript itself has none.
                if model and not result.get("model"):
                    extra["Model"] = model
                result = openclaw_to_md(
                    jsonl, meta_extra=extra, include_thinking=thinking,
                    title=label or result["title"],
                )
                when = result["started"]
                if not when and meta.get("sessionStartedAt"):
                    when = rc.epoch_to_dt(meta["sessionStartedAt"])
                return {
                    "account": account,
                    "source": source,
                    "project": proj,
                    "when": when,
                    "title": result["title"],
                    "markdown": result["markdown"],
                    "turns": result["user_turns"],
                    "cwd": cwd,
                    "git": None,
                }
            yield key, version, "openclaw", render


# --------------------------------------------------------------------------- #
# Cursor
# --------------------------------------------------------------------------- #
def _file_uri_path(uri: str | None) -> str | None:
    if not uri or not isinstance(uri, str):
        return None
    if uri.startswith("file:"):
        path = unquote(urlparse(uri).path)
    else:
        path = uri
    return path or None


def cursor_project_slug(path: str) -> str:
    """Cursor's project-folder name: absolute path, leading slash dropped, `/` → `-`."""
    path = os.path.abspath(path).replace("\\", "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if path.startswith("/"):
        path = path[1:]
    return path.replace("/", "-")


def load_cursor_workspaces() -> dict[str, str]:
    """Map workspaceStorage id and project slug → filesystem path."""
    by_id: dict[str, str] = {}
    if not os.path.isdir(CURSOR_WORKSPACES):
        return by_id
    for ws_json in glob.glob(os.path.join(CURSOR_WORKSPACES, "*", "workspace.json")):
        try:
            with open(ws_json, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        raw = data.get("folder") or data.get("workspace") or data.get("configuration")
        path = _file_uri_path(raw)
        if not path:
            continue
        by_id[os.path.basename(os.path.dirname(ws_json))] = path
    return by_id


def _resolve_cursor_cwd(slug: str | None, workspace_id: str | None,
                        by_id: dict[str, str]) -> str | None:
    if workspace_id and by_id.get(workspace_id):
        return by_id[workspace_id]
    if not slug:
        return None
    for path in by_id.values():
        if cursor_project_slug(path) == slug:
            return path
    candidate = "/" + slug.replace("-", "/")
    if os.path.isdir(candidate):
        return candidate
    return None


def _cursor_project_name(cwd: str | None, slug: str | None) -> str | None:
    if cwd:
        if cwd.endswith(".code-workspace"):
            return os.path.splitext(os.path.basename(cwd))[0]
        return os.path.basename(cwd.rstrip("/")) or None
    return slug or None


def _load_cursor_db() -> tuple[dict[str, bytes], list[dict]]:
    """Return (blob-by-hash, composer records) from the global state DB."""
    blobs: dict[str, bytes] = {}
    composers: list[dict] = []
    if not os.path.isfile(CURSOR_STATE_DB):
        return blobs, composers
    try:
        con = sqlite3.connect(f"file:{CURSOR_STATE_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return blobs, composers
    try:
        for key, val in con.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'agentKv:blob:%'"):
            digest = key.split("agentKv:blob:", 1)[-1]
            if isinstance(val, str):
                val = val.encode()
            if isinstance(val, bytes):
                blobs[digest] = val
        headers: dict[str, dict] = {}
        try:
            for row in con.execute(
                    "SELECT composerId, workspaceId, createdAt, lastUpdatedAt, "
                    "isSubagent, subagentTypeName FROM composerHeaders"):
                headers[row[0]] = {
                    "workspaceId": row[1],
                    "createdAt": row[2],
                    "lastUpdatedAt": row[3],
                    "isSubagent": bool(row[4]),
                    "subagentTypeName": row[5],
                }
        except sqlite3.Error:
            headers = {}
        for key, val in con.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
            cid = key.split("composerData:", 1)[-1]
            if isinstance(val, bytes):
                val = val.decode("utf-8", "replace")
            try:
                data = json.loads(val)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            state = data.get("conversationState") or ""
            if not hashes_from_conversation_state(state):
                continue
            header = headers.get(cid) or {}
            model = None
            model_cfg = data.get("modelConfig")
            if isinstance(model_cfg, dict):
                model = model_cfg.get("modelName")
            composers.append({
                "id": cid,
                "name": (data.get("name") or "").strip() or None,
                "state": state,
                "model": model,
                "workspaceId": header.get("workspaceId"),
                "createdAt": header.get("createdAt") or data.get("createdAt"),
                "lastUpdatedAt": header.get("lastUpdatedAt"),
                "isSubagent": bool(header.get("isSubagent")),
                "subagentTypeName": header.get("subagentTypeName"),
                "headers": data.get("fullConversationHeadersOnly") or [],
            })
    except sqlite3.Error:
        return {}, []
    finally:
        con.close()
    return blobs, composers


def _composer_span(comp: dict):
    times = []
    for header in comp.get("headers") or []:
        if isinstance(header, dict):
            ts = rc.parse_iso(header.get("createdAt") or "")
            if ts:
                times.append(ts)
    started = min(times) if times else rc.epoch_to_dt(comp.get("createdAt"))
    ended = max(times) if times else (
        rc.epoch_to_dt(comp.get("lastUpdatedAt")) or started)
    return started, ended


def _iter_cursor_jsonl():
    if not os.path.isdir(CURSOR_PROJECTS):
        return
    for jsonl in glob.glob(os.path.join(
            CURSOR_PROJECTS, "*", "agent-transcripts", "**", "*.jsonl"), recursive=True):
        base = os.path.basename(jsonl)
        # Session files are <uuid>/<uuid>.jsonl (subagents nest the same way).
        if os.path.splitext(base)[0] != os.path.basename(os.path.dirname(jsonl)):
            continue
        yield jsonl


def cursor_tasks(thinking: bool, project: str | None, account_override: str | None):
    if not os.path.isdir(CURSOR_PROJECTS) and not os.path.isfile(CURSOR_STATE_DB):
        print("  (cursor) none found; skipping", file=sys.stderr)
        return
    account = account_override or current_cursor_email() or "unknown-account"
    by_id = load_cursor_workspaces()
    blobs, composers = _load_cursor_db()

    # One manifest key per session id. Prefer the live composer when it is at
    # least as new as the JSONL archive (the archive is written when a chat ends).
    # Tuple: kind, payload, activity_epoch, slug, is_sub, version.
    chosen: dict[str, tuple] = {}
    marker = f"{os.sep}agent-transcripts{os.sep}"
    for jsonl in _iter_cursor_jsonl():
        sid = os.path.splitext(os.path.basename(jsonl))[0]
        slug = os.path.basename(jsonl.split(marker, 1)[0])
        is_sub = f"{os.sep}subagents{os.sep}" in jsonl
        mtime = _mtime(jsonl)
        chosen[sid] = ("jsonl", jsonl, mtime, slug, is_sub, mtime)

    for comp in composers:
        sid = comp["id"]
        _started, ended = _composer_span(comp)
        db_epoch = ended.timestamp() if ended else 0
        state_ver = hashlib.sha256((comp.get("state") or "").encode("utf-8")).hexdigest()[:20]
        prev = chosen.get(sid)
        if prev is None or db_epoch >= prev[2]:
            chosen[sid] = ("db", comp, db_epoch, None, comp["isSubagent"], state_ver)

    for sid, spec in chosen.items():
        kind, payload, _activity, slug, is_sub, version = spec
        key = f"cursor:{sid}"
        source = "subagent" if is_sub else "agent"

        def render(kind=kind, payload=payload, slug=slug, source=source, sid=sid):
            if kind == "jsonl":
                cwd = _resolve_cursor_cwd(slug, None, by_id)
            else:
                cwd = _resolve_cursor_cwd(None, payload.get("workspaceId"), by_id)
            proj = _cursor_project_name(cwd, slug)
            haystack = " ".join(p for p in (cwd, proj, slug) if p)
            if project and project.lower() not in haystack.lower():
                return None
            extra = {
                "Account": f"{account} (current login)",
                "Source": "Cursor" if source == "agent" else "Cursor subagent",
                "Project": proj,
                "Working dir": cwd,
                "Session": sid,
            }
            if kind == "jsonl":
                result = cursor_jsonl_to_md(payload, meta_extra=extra,
                                            include_thinking=thinking)
            else:
                messages = messages_from_blobs(
                    hashes_from_conversation_state(payload.get("state")), blobs)
                parsed = parse_messages(messages, include_thinking=thinking)
                started, ended = _composer_span(payload)
                if payload.get("subagentTypeName"):
                    extra["Subagent"] = payload["subagentTypeName"]
                result = cursor_to_md(
                    parsed["sections"], payload.get("name"), extra,
                    started=started, ended=ended, model=payload.get("model"))
            if result["user_turns"] == 0:
                return None
            return {
                "account": account,
                "source": source,
                "project": proj,
                "when": result["started"],
                "title": result["title"],
                "markdown": result["markdown"],
                "turns": result["user_turns"],
                "cwd": cwd,
                "git": None,
            }
        yield key, version, "cursor", render


# --------------------------------------------------------------------------- #
# Grok Bot
# --------------------------------------------------------------------------- #
def _resolve_agents_dir(root: str) -> str | None:
    """Accept an agent-data dir, its agents/ child, or a parent of agent-data."""
    if not root or not os.path.isdir(root):
        return None
    candidates = [
        os.path.join(root, "agents"),
        os.path.join(root, "agent-data", "agents"),
        root,
    ]
    for cand in candidates:
        if not os.path.isdir(cand):
            continue
        for child in os.listdir(cand):
            if os.path.isfile(os.path.join(cand, child, "store.db")):
                return cand
    return None


# ~/git/grokbot is a local archive and is never a session source.
GROKBOT_IGNORED_ROOT = os.path.join(HOME, "git", "grokbot")


def _grokbot_path_ignored(path: str | None) -> bool:
    if not path:
        return False
    real = os.path.realpath(os.path.expanduser(path))
    ignored = os.path.realpath(GROKBOT_IGNORED_ROOT)
    return real == ignored or real.startswith(ignored + os.sep)


def _grokbot_search_roots(explicit: str | None) -> list[str]:
    """store.db fallback roots. Only an explicit grokbot_data path is searched.

    The desktop cache is read separately. ``~/git/grokbot`` is never a root.
    """
    if not explicit:
        return []
    path = os.path.expanduser(explicit)
    if _grokbot_path_ignored(path):
        return []
    return [path]


def _grokbot_persist_dir(config_value: str | None) -> str:
    return os.path.expanduser(config_value or grokbot_md.DEFAULT_PERSIST_DIR)


def grokbot_source_present(explicit: str | None, persist_dir: str | None = None) -> bool:
    """True when a desktop replica cache or a store.db fallback is readable."""
    if not _grokbot_path_ignored(persist_dir):
        directory = _grokbot_persist_dir(persist_dir)
        if os.path.isdir(directory):
            try:
                names = os.listdir(directory)
            except OSError:
                names = []
            for fn in names:
                key = grokbot_md.decode_blob_name(fn) or ""
                if ".transcript.replicas." in key:
                    return True
    if explicit and not _grokbot_path_ignored(explicit):
        root = os.path.expanduser(explicit)
        if os.path.isfile(root) and root.endswith((".tgz", ".tar.gz", ".tar")):
            return True
        if _resolve_agents_dir(root):
            return True
    return False


def _agent_profile(path: str | None) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_grokbot_stores(explicit: str | None) -> tuple[list[tuple], str | None]:
    """Return (stores, tempdir).

    Each store is (agent_id, store_path, profile, version). A .tgz/.tar is
    unpacked only for ``store.db`` and ``profile.json``. The caller deletes
    ``tempdir`` after the stores have been rendered.
    """
    found: list[tuple] = []
    tmp: str | None = None
    for root in _grokbot_search_roots(explicit):
        if _grokbot_path_ignored(root):
            continue
        if os.path.isfile(root) and root.endswith((".tgz", ".tar.gz", ".tar")):
            tmp = tempfile.mkdtemp(prefix="grokbot-export-")
            try:
                tf = tarfile.open(root, "r:*")
            except (OSError, tarfile.TarError) as exc:
                print(f"  (grokbot) could not read {root}: {exc}", file=sys.stderr)
                shutil.rmtree(tmp, ignore_errors=True)
                tmp = None
                continue
            with tf:
                members = {m.name.replace("\\", "/"): m for m in tf.getmembers() if m.isfile()}
                for name, member in members.items():
                    if not name.endswith("/store.db"):
                        continue
                    agent_id = os.path.basename(os.path.dirname(name))
                    if not agent_id or agent_id in (".", ".."):
                        continue
                    dest_dir = os.path.join(tmp, agent_id)
                    os.makedirs(dest_dir, exist_ok=True)
                    store_dest = os.path.join(dest_dir, "store.db")
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    with src, open(store_dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    profile_dest = None
                    for candidate in (name[: -len("store.db")] + "profile.json",):
                        pmember = members.get(candidate)
                        if pmember is None and candidate.startswith("./"):
                            pmember = members.get(candidate[2:])
                        if pmember is None:
                            pmember = members.get("./" + candidate)
                        if pmember is None:
                            continue
                        profile_dest = os.path.join(dest_dir, "profile.json")
                        psrc = tf.extractfile(pmember)
                        if psrc is not None:
                            with psrc, open(profile_dest, "wb") as out:
                                shutil.copyfileobj(psrc, out)
                        break
                    found.append((agent_id, store_dest, _agent_profile(profile_dest),
                                  member.mtime or _mtime(store_dest)))
            return found, tmp
        agents = _resolve_agents_dir(root)
        if not agents:
            continue
        for child in sorted(os.listdir(agents)):
            store = os.path.join(agents, child, "store.db")
            if not os.path.isfile(store):
                continue
            profile = _agent_profile(os.path.join(agents, child, "profile.json"))
            found.append((child, store, profile, _mtime(store)))
        return found, None
    return found, tmp


def _grokbot_project_name(name: str, agent_id: str, counts: dict[str, int]) -> str:
    """Disambiguate bots that share a roster name (two 'New Bot's)."""
    if counts.get(name, 0) > 1 and name != agent_id:
        return f"{name}-{agent_id[:8]}"
    return name


def grokbot_tasks(thinking: bool, project: str | None,
                  data_path: str | None, account_override: str | None,
                  persist_dir: str | None = None, out_root: str | None = None,
                  include_agent_mail: bool = True, verbose: bool = False):
    """Yield one task per bot from the desktop cache, then store.db fallback.

    Replica entries are merged into ``<out_root>/grokbot/.archive/<id>.jsonl``
    before render, so a later run still has lines that rolled out of the
    ~200-entry desktop window. ``~/git/grokbot`` is never opened.
    """
    if data_path and _grokbot_path_ignored(data_path):
        print(f"  (grokbot) ignoring {os.path.expanduser(data_path)}", file=sys.stderr)
        data_path = None
    persist = _grokbot_persist_dir(persist_dir)
    if _grokbot_path_ignored(persist):
        print(f"  (grokbot) ignoring {persist}", file=sys.stderr)
        persist = ""

    replicas = list(grokbot_md.iter_replicas(persist)) if persist else []
    readable = {rep["agent_id"] for rep in replicas}
    roster = grokbot_md.load_roster(persist) if persist else {}

    store_rows: dict[str, tuple] = {}
    tmp = None
    if data_path:
        found, tmp = _load_grokbot_stores(data_path)
        try:
            for agent_id, store, profile, mtime in found:
                store_rows[agent_id] = (
                    grokbot_md.load_store_entries(store), profile, mtime)
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
                tmp = None

    if not replicas and not store_rows and not (
            persist and os.path.isdir(persist)):
        print("  (grokbot) no sand-client-persistence replicas found", file=sys.stderr)
    elif persist and os.path.isdir(persist) and not replicas and not store_rows:
        print("  (grokbot) persistence dir has no transcript replicas", file=sys.stderr)

    account_ids = {rep["account_id"] for rep in replicas}
    multi_account = len(account_ids) > 1 and not account_override
    if multi_account:
        grokbot_md.warn(
            "(grokbot) more than one account in the desktop cache; "
            "using each account id because grokbot_account is unset")
    default_account = account_override or current_cursor_email() or "grokbot"

    # Names across replica + store-only bots, for duplicate disambiguation.
    labels: list[tuple[str, str]] = []
    for rep in replicas:
        row = roster.get(rep["account_id"], {}).get(rep["agent_id"], {})
        profile = (store_rows.get(rep["agent_id"]) or (None, {}, None))[1]
        bot = (row.get("name") or (profile or {}).get("name") or "").strip() or rep["agent_id"]
        labels.append((rep["agent_id"], bot))
    for agent_id, (entries, profile, _mtime_store) in store_rows.items():
        if agent_id in readable:
            continue
        if not entries:
            continue
        bot = ((profile or {}).get("name") or "").strip() or agent_id
        labels.append((agent_id, bot))
    name_counts: dict[str, int] = {}
    for _aid, bot in labels:
        name_counts[bot] = name_counts.get(bot, 0) + 1

    def account_for(account_id: str | None) -> str:
        if multi_account and account_id:
            return account_id
        return default_account

    def emit_bot(agent_id: str, bot: str, account_id: str | None, row: dict,
                 windows: list[list[dict]], source_label: str, version_bits: list):
        if project and project.lower() not in f"{bot} {agent_id}".lower():
            # Still archive, so the window is not lost, but do not emit a task
            # (a --project run does not prune).
            if out_root:
                archive = grokbot_md.archive_path(out_root, agent_id)
                merged: list[dict] = []
                for window in windows:
                    merged = grokbot_md.merge_into_archive(archive, window)
            return
        merged: list[dict] = []
        flat = [entry for window in windows for entry in window]
        if out_root:
            archive = grokbot_md.archive_path(out_root, agent_id)
            for window in windows:
                merged = grokbot_md.merge_into_archive(archive, window)
            version = max([0.0, *version_bits, _mtime(archive)])
        else:
            for window in windows:
                merged = grokbot_md.merge_into_archive("", window)
            version = max([0.0, *version_bits])
        render_entries = grokbot_md.entries_for_render(merged, flat)
        seqs = [e.get("seq") for e in merged if isinstance(e.get("seq"), int)]
        first_seq = min(seqs) if seqs else None
        truncated = first_seq if first_seq and first_seq > 1 else None
        proj = _grokbot_project_name(bot, agent_id, name_counts)
        key = f"grokbot:{agent_id}"
        account = account_for(account_id)

        def render(render_entries=render_entries, bot=bot, agent_id=agent_id,
                   row=row, account=account, proj=proj, source_label=source_label,
                   truncated=truncated):
            extra = {
                "Account": account,
                "Source": source_label,
                "Agent": bot,
                "Session": agent_id,
            }
            if row.get("isHiddenFromSidebar"):
                extra["Hidden"] = "yes"
            result = grokbot_md.grokbot_entries_to_md(
                render_entries, meta_extra=extra, include_thinking=thinking,
                truncated_before_seq=truncated,
                include_agent_mail=include_agent_mail, verbose=verbose)
            if result["user_turns"] == 0:
                return None
            when = result["started"] or rc.epoch_to_dt(row.get("createdAt"))
            return {
                "account": account,
                "source": "chat",
                "project": proj,
                "when": when,
                "title": result["title"],
                "markdown": result["markdown"],
                "turns": result["user_turns"],
                "cwd": None,
                "git": None,
            }
        yield key, version, "grokbot", render

    yielded: set[str] = set()
    for rep in replicas:
        agent_id = rep["agent_id"]
        row = roster.get(rep["account_id"], {}).get(agent_id, {})
        profile = (store_rows.get(agent_id) or (None, {}, None))[1]
        bot = (row.get("name") or (profile or {}).get("name") or "").strip() or agent_id
        windows = [rep["entries"]]
        bits = [rep["mtime"] or 0.0]
        store = store_rows.get(agent_id)
        if store and store[0]:
            windows.append(store[0])
            bits.append(store[2] or 0.0)
        yield from emit_bot(
            agent_id, bot, rep["account_id"], row, windows,
            "Grok Bot (desktop cache)", bits)
        yielded.add(agent_id)

    if persist and os.path.isdir(persist):
        for agent_id, _account_id, _path in grokbot_md.iter_replica_refs(persist):
            if agent_id in readable or agent_id in yielded:
                continue
            # Unreadable this run: keep the manifest row by being "seen".
            key = f"grokbot:{agent_id}"

            def render():
                return None
            yield key, "unreadable", "grokbot", render
            yielded.add(agent_id)

    for agent_id, (entries, profile, mtime) in store_rows.items():
        if agent_id in yielded and not entries:
            continue
        if agent_id in yielded:
            continue  # already merged above
        if not entries:
            continue
        bot = ((profile or {}).get("name") or "").strip() or agent_id
        yield from emit_bot(
            agent_id, bot, None, {}, [entries],
            "Grok Bot (store.db)", [mtime or 0.0])


# --------------------------------------------------------------------------- #
# Incremental export (manifest-driven)
# --------------------------------------------------------------------------- #
MANIFEST_NAME = ".export_manifest.json"
DEVICE_ID_NAME = ".device_id"
_DEVICE_ID: str | None = None


def device_id() -> str:
    """Stable label for this device, so index rows show which machine wrote them.

    Persisted in the run directory (next to the manifest, not synced into the
    shared vault). First run seeds it from the hostname, falling back to a short
    random id; the file is plain text and can be hand-edited to rename a device.
    """
    global _DEVICE_ID
    if _DEVICE_ID is not None:
        return _DEVICE_ID
    path = os.path.join(os.getcwd(), DEVICE_ID_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            val = fh.read().strip()
    except OSError:
        val = ""
    if not val:
        try:
            val = socket.gethostname().strip()
        except OSError:
            val = ""
        val = val or f"device-{uuid.uuid4().hex[:8]}"
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(val + "\n")
        except OSError:
            pass
    _DEVICE_ID = val
    return val


def device_slug() -> str:
    """Filesystem-safe form of device_id(), used in index-<device>-*.md names."""
    return re.sub(r"[^\w.\-]", "_", device_id()) or "device"


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def manifest_path() -> str:
    # The manifest lives in the current run directory (where runapp.sh / the
    # tool is invoked), not in the output vault — it's tool-local state, not
    # part of the exported, syncable session set.
    return os.path.join(os.getcwd(), MANIFEST_NAME)


def load_manifest() -> dict:
    try:
        with open(manifest_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "sessions": {}}


def save_manifest(manifest: dict) -> None:
    with open(manifest_path(), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=0)


def _serialize_row(row: dict) -> dict:
    out = dict(row)
    out["when"] = row["when"].isoformat() if row.get("when") else None
    return out


def _deserialize_row(row: dict) -> dict:
    out = dict(row)
    out["when"] = rc.parse_iso(row["when"]) if row.get("when") else None
    return out


def seed_used_from_manifest(out_root: str, manifest: dict) -> None:
    """Reserve every known output filename so new sessions never collide."""
    for entry in manifest["sessions"].values():
        path = entry.get("row", {}).get("path")
        if path:
            out_dir = os.path.join(out_root, os.path.dirname(path))
            stem = os.path.splitext(os.path.basename(path))[0]
            _USED.setdefault(out_dir, set()).add(stem)


def _safe_delete(out_root: str, rel_path: str | None) -> None:
    if not rel_path:
        return
    full = os.path.join(out_root, rel_path)
    try:
        if os.path.isfile(full):
            os.remove(full)
    except OSError:
        pass


def run_tasks(out_root: str, manifest: dict, tasks, seen: set,
              counters: dict) -> None:
    """Drive a source's tasks incrementally: skip unchanged, (re)render changed."""
    for key, version, vendor, render in tasks:
        seen.add(key)
        prev = manifest["sessions"].get(key)
        prev_path = prev and prev.get("row", {}).get("path")
        if (prev and str(prev.get("version")) == str(version)
                and prev_path and os.path.isfile(os.path.join(out_root, prev_path))):
            counters["unchanged"] += 1
            continue  # up to date; row stays in the manifest

        res = render()
        if not res or res.get("turns", 0) == 0:
            counters["empty"] += 1
            continue

        row = emit(out_root, vendor, res["account"], res["source"], res["when"],
                   res["title"], res["markdown"], res["turns"],
                   project=res.get("project"), cwd=res.get("cwd"), git=res.get("git"),
                   force_path=prev_path if prev else None)
        # If a rename happened (title changed without a stored path), clean the old file.
        if prev_path and prev_path != row["path"]:
            _safe_delete(out_root, prev_path)
        manifest["sessions"][key] = {"version": version, "vendor": vendor,
                                     "row": _serialize_row(row)}
        counters["rendered" if not prev else "updated"] += 1


def vendor_source_present(vendor: str, export_dir: str | None = None,
                          chatgpt_export: str | None = None,
                          grokbot_data: str | None = None,
                          grokbot_persist: str | None = None) -> bool:
    """Whether the vendor's source exists on this machine.

    Used to gate pruning: a source that is *entirely absent* (the tool runs on a
    machine that never had that vendor) must NOT prune previously-exported
    sessions — they may have been produced on another machine and synced into a
    shared output vault. This only guards total absence; a source dir that exists
    but is empty still prunes normally (its sessions really were deleted).
    """
    if vendor == "claude":
        return (os.path.isdir(COWORK_BASE) or os.path.isdir(CLAUDE_CODE_BASE)
                or bool(export_dir and os.path.isfile(
                    os.path.join(export_dir, "conversations.json"))))
    if vendor == "openai":
        return (os.path.isdir(CODEX_SESSIONS)
                or bool(chatgpt_export and _resolve_chatgpt_export(chatgpt_export)))
    if vendor == "droid":
        return os.path.isdir(DROID_SESSIONS)
    if vendor == "openclaw":
        return os.path.isdir(OPENCLAW_AGENTS)
    if vendor == "cursor":
        return os.path.isdir(CURSOR_PROJECTS) or os.path.isfile(CURSOR_STATE_DB)
    if vendor == "grokbot":
        return grokbot_source_present(grokbot_data, grokbot_persist)
    return True


def prune_manifest(out_root: str, manifest: dict, seen: set,
                   vendors: set) -> int:
    """Drop manifest entries (and their files) whose source vanished."""
    removed = 0
    for key in list(manifest["sessions"]):
        entry = manifest["sessions"][key]
        if entry.get("vendor") in vendors and key not in seen:
            _safe_delete(out_root, entry.get("row", {}).get("path"))
            del manifest["sessions"][key]
            removed += 1
    return removed


def manifest_rows(out_root: str, manifest: dict) -> list[dict]:
    """All index rows whose output file still exists on disk."""
    rows = []
    for entry in manifest["sessions"].values():
        row = entry.get("row", {})
        if row.get("path") and os.path.isfile(os.path.join(out_root, row["path"])):
            rows.append(_deserialize_row(row))
    return rows


# --------------------------------------------------------------------------- #
# Index + main
# --------------------------------------------------------------------------- #
def _month_key(when) -> str:
    """Bucket key for a session row: 'YYYY-MM', or 'undated' when no timestamp.

    Bucketed by local wall-clock month (via to_local) so a session run near
    midnight lands in the day/month the user actually ran it, not its UTC day.
    """
    when = rc.to_local(when)
    return when.strftime("%Y-%m") if when else "undated"


def _render_rows(lines: list[dict], rows: list[dict], link_prefix: str) -> None:
    """Append one concise, self-contained line per session (no tables).

    Each line carries its own context — date, vendor/account, source, project,
    turns, and a link to the session file — so an agent can grep a single line
    and understand it. Rows are sorted oldest-first, so new sessions land at the
    bottom (an append-style log). `link_prefix` is prepended to every link so
    they resolve from wherever the file lives.
    """
    earliest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    for r in sorted(rows, key=lambda r: (r["when"] or earliest)):
        date_str = rc.fmt_dt(r["when"]) or "—"
        title = " ".join((r["title"] or "untitled").split()).replace("]", "\\]")
        proj = r.get("project") or "-"
        if r.get("project_path"):
            proj = f"[{proj}]({link_prefix}{r['project_path']})"
        acct = r["account"] or "unknown"
        lines.append(
            f"- {date_str} · {r['vendor']}/{acct} · {r['source']} · "
            f"{proj} · {r['turns']}t · [{title}]({link_prefix}{r['path']})")


def _vendor_counts(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["vendor"]] = counts.get(r["vendor"], 0) + 1
    return ", ".join(f"{v} {n}" for v, n in sorted(counts.items()))


def write_index(out_root: str, rows: list[dict]) -> None:
    """Write this device's index files: one concise month file per month
    (index-<device>-YYYY-MM.md) plus a per-device landing (index-<device>.md)
    linking to them. Every file is device-scoped so devices sharing one vault
    never overwrite each other's index. No tables: each session is a single
    self-contained line so an agent can read/grep it directly. Only this
    device's stale files are pruned; legacy shared index files are removed."""
    dev = device_slug()
    months: dict[str, list[dict]] = {}
    for r in rows:
        months.setdefault(_month_key(r["when"]), []).append(r)

    landing = f"index-{dev}.md"

    # One flat, line-per-session file per month, at the output root.
    written: set = set()
    for key in sorted(months, reverse=True):
        mrows = months[key]
        lines = [f"# Sessions — {dev} — {key}", "",
                 f"{len(mrows)} sessions · {_vendor_counts(mrows)}", "",
                 f"[← index]({landing})", ""]
        _render_rows(lines, mrows, "")
        name = f"index-{dev}-{key}.md"
        with open(os.path.join(out_root, name), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines).rstrip() + "\n")
        written.add(name)

    # Prune only *this device's* month files for months that no longer exist.
    for stale in glob.glob(os.path.join(out_root, f"index-{dev}-*.md")):
        if os.path.basename(stale) not in written:
            os.remove(stale)
    # Remove legacy shared index files from before the per-device split, and the
    # old index/ shard directory. These are never re-created, so this is a
    # one-time cleanup; other devices' index-<device>*.md files are untouched.
    for legacy in [os.path.join(out_root, "index.md"),
                   os.path.join(out_root, "index")]:
        if os.path.isdir(legacy):
            shutil.rmtree(legacy, ignore_errors=True)
        elif os.path.isfile(legacy):
            os.remove(legacy)
    for legacy in glob.glob(os.path.join(out_root, "index-*.md")):
        if re.match(r"index-(\d{4}-\d{2}|undated)\.md$", os.path.basename(legacy)):
            os.remove(legacy)

    # Per-device landing: totals + a concise month directory (a list, not a table).
    vendors: dict[str, list[dict]] = {}
    for r in rows:
        vendors.setdefault(r["vendor"], []).append(r)
    lines = [f"# Sessions Index — {dev}", "",
             f"Total: {len(rows)} · "
             + ", ".join(f"{v} {len(rs)}" for v, rs in sorted(vendors.items())),
             "", "## Months", ""]
    for key in sorted(months, reverse=True):
        mrows = months[key]
        lines.append(f"- [{key}](index-{dev}-{key}.md) — {len(mrows)} · {_vendor_counts(mrows)}")

    with open(os.path.join(out_root, landing), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vendor",
                        choices=["all", "claude", "openai", "droid", "openclaw",
                                 "cursor", "grokbot"],
                        default="all", help="Which vendor to export (default: all)")
    parser.add_argument("-o", "--output", default=None,
                        help="Output directory (overrides config.yml output_dir)")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="Path to config.yml (default: alongside this script)")
    parser.add_argument("--export-dir", default=None,
                        help="Claude chat export directory (conversations.json)")
    parser.add_argument("--chatgpt-export", default=None,
                        help="Path to a ChatGPT data-export conversations.json")
    parser.add_argument("--grokbot-data", default=None,
                        help="Grok Bot agent-data directory, or a .tgz that contains "
                             "agent-data/agents/<id>/store.db")
    parser.add_argument("--grokbot-persist-dir", default=None,
                        help="Grok Bot sand-client-persistence directory "
                             "(default: ~/Library/Application Support/Grok Bot/sand-client-persistence)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print skipped unknown Grok Bot entry kinds")
    parser.add_argument("--project", default=None,
                        help="Only export agent sessions whose working dir matches this substring")
    parser.add_argument("--code-account", default=None,
                        help="Override the account email for Claude Code / Codex sessions")
    parser.add_argument("--thinking", action="store_true",
                        help="Include assistant thinking / reasoning (collapsed)")
    args = parser.parse_args()

    # Precedence for each setting: CLI flag > config.yml > built-in default.
    config = load_config(args.config)
    output = args.output or config.get("output_dir") or DEFAULT_OUTPUT
    output = os.path.expanduser(output)
    export_dir = args.export_dir or config.get("export_dir")
    chatgpt_export = args.chatgpt_export or config.get("chatgpt_export")
    code_account = args.code_account or config.get("code_account")
    cursor_account = config.get("cursor_account")
    grokbot_account = config.get("grokbot_account")
    grokbot_data = args.grokbot_data or config.get("grokbot_data")
    grokbot_persist = args.grokbot_persist_dir or config.get("grokbot_persist_dir")
    include_agent_mail = config.get("grokbot_include_agent_mail", True)
    if include_agent_mail is None:
        include_agent_mail = True
    thinking = args.thinking or bool(config.get("thinking"))

    print(f"Output root: {output}")
    os.makedirs(output, exist_ok=True)

    # Incremental: a manifest in the run directory tracks each source's version
    # (mtime / updated_at) and its index row, so unchanged sessions are skipped.
    manifest = load_manifest()
    seed_used_from_manifest(output, manifest)
    seen: set = set()
    processed_vendors: set = set()
    counters = {"unchanged": 0, "rendered": 0, "updated": 0, "empty": 0}

    if args.vendor in ("all", "claude"):
        print("Exporting claude…")
        processed_vendors.add("claude")
        run_tasks(output, manifest, chat_tasks(export_dir), seen, counters)
        run_tasks(output, manifest, cowork_tasks(thinking), seen, counters)
        run_tasks(output, manifest, claude_code_tasks(thinking, args.project, code_account), seen, counters)
    if args.vendor in ("all", "openai"):
        print("Exporting openai…")
        processed_vendors.add("openai")
        run_tasks(output, manifest, codex_tasks(thinking, args.project, code_account), seen, counters)
        run_tasks(output, manifest, chatgpt_tasks(chatgpt_export), seen, counters)
    if args.vendor in ("all", "droid"):
        print("Exporting droid…")
        processed_vendors.add("droid")
        run_tasks(output, manifest, droid_tasks(thinking, args.project), seen, counters)
    if args.vendor in ("all", "openclaw"):
        print("Exporting openclaw…")
        processed_vendors.add("openclaw")
        run_tasks(output, manifest, openclaw_tasks(thinking, args.project), seen, counters)
    if args.vendor in ("all", "cursor"):
        print("Exporting cursor…")
        processed_vendors.add("cursor")
        run_tasks(output, manifest, cursor_tasks(thinking, args.project, cursor_account), seen, counters)
    if args.vendor in ("all", "grokbot"):
        print("Exporting grokbot…")
        processed_vendors.add("grokbot")
        run_tasks(output, manifest,
                  grokbot_tasks(thinking, args.project, grokbot_data, grokbot_account,
                                persist_dir=grokbot_persist, out_root=output,
                                include_agent_mail=bool(include_agent_mail),
                                verbose=args.verbose),
                  seen, counters)

    # Prune sessions whose source disappeared. Pruning is scoped to the vendors
    # processed this run, and skipped entirely under a --project filter (which
    # makes the scan partial), so a per-vendor run only ever prunes its own vendor.
    removed = 0
    if args.project is None:
        prunable = {v for v in processed_vendors
                    if vendor_source_present(v, export_dir, chatgpt_export, grokbot_data,
                                             grokbot_persist)}
        for v in sorted(processed_vendors - prunable):
            print(f"  (prune) {v} source absent on this machine; "
                  f"keeping existing sessions", file=sys.stderr)
        removed = prune_manifest(output, manifest, seen, prunable)

    # Rebuild index + per-project metas from the full manifest (incremental-safe).
    rows = manifest_rows(output, manifest)
    aggregate_projects(rows)
    n_projects = write_project_metas(output)
    write_index(output, rows)
    save_manifest(manifest)

    print(f"  changed: {counters['rendered']} new, {counters['updated']} updated; "
          f"{counters['unchanged']} unchanged, {removed} removed; "
          f"{n_projects} _project.md")
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["vendor"]] = summary.get(r["vendor"], 0) + 1
    line = ", ".join(f"{v}: {n}" for v, n in sorted(summary.items())) or "nothing"
    print(f"\nDone. {len(rows)} session file(s) + index-{device_slug()}.md "
          f"in '{output}/'\n  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
