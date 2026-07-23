#!/usr/bin/env python3
"""Organize AI coding/chat sessions into Markdown, grouped by vendor & account.

Output layout:

    sessions_md/
      claude/<account>/{chat,cowork,claude-code}/*.md
      openai/<account>/{chatgpt,codex}/*.md
      droid/<account>/*.md
      openclaw/<account>/{webchat,weixin,wecom,cron,subagent,...}/*.md
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

Account attribution: chat from users.json; cowork per-session from its metadata;
claude-code/codex from the current CLI login (no per-session account is
recorded); droid from the session owner; openclaw from authProfileOverride
(or agent id).

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
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import uuid

import render_common as rc
from chatgpt_md import conversation_to_md as chatgpt_conv_to_md
from chatgpt_md import load_conversations as load_chatgpt
from codex_md import codex_to_md
from droid_md import droid_to_md
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

    High-volume agent sources (codex, droid, claude-code, openclaw) pass `project`
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
        prefix = local_when.strftime("%Y%m%d") if local_when else "00000000"
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


def chatgpt_tasks(export_path: str | None):
    if not export_path:
        print("  (chatgpt) no chatgpt_export configured; ChatGPT chats are server-side, "
              "skipping. Download a data export and set chatgpt_export.", file=sys.stderr)
        return
    if not os.path.isfile(export_path):
        print(f"  (chatgpt) {export_path} not found; skipping", file=sys.stderr)
        return
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
                          chatgpt_export: str | None = None) -> bool:
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
                or bool(chatgpt_export and os.path.isfile(chatgpt_export)))
    if vendor == "droid":
        return os.path.isdir(DROID_SESSIONS)
    if vendor == "openclaw":
        return os.path.isdir(OPENCLAW_AGENTS)
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
    parser.add_argument("--vendor", choices=["all", "claude", "openai", "droid", "openclaw"],
                        default="all", help="Which vendor to export (default: all)")
    parser.add_argument("-o", "--output", default=None,
                        help="Output directory (overrides config.yml output_dir)")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="Path to config.yml (default: alongside this script)")
    parser.add_argument("--export-dir", default=None,
                        help="Claude chat export directory (conversations.json)")
    parser.add_argument("--chatgpt-export", default=None,
                        help="Path to a ChatGPT data-export conversations.json")
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

    # Prune sessions whose source disappeared. Pruning is scoped to the vendors
    # processed this run, and skipped entirely under a --project filter (which
    # makes the scan partial), so a per-vendor run only ever prunes its own vendor.
    removed = 0
    if args.project is None:
        prunable = {v for v in processed_vendors
                    if vendor_source_present(v, export_dir, chatgpt_export)}
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
