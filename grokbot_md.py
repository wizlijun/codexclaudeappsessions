#!/usr/bin/env python3
"""Render Grok Bot sessions into Markdown.

Primary source (desktop app cache, read-only):

    ~/Library/Application Support/Grok Bot/sand-client-persistence/<base32>.blob

Each ``transcript.replicas.<agentId>`` blob is a JSON envelope
``{"schemaVersion", "value": {"entries": [...]}}``. The cache is a rolling
window, so callers merge entries into an append-only archive before rendering.

Fallback source: ``agent-data/agents/<id>/store.db`` (``transcript_entries``),
used when ``grokbot_data`` points at a directory or ``.tgz``.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.parse

from render_common import (Sections, build_markdown, clean_text, epoch_to_dt,
                           fmt_dt, result_note, thinking_details, tool_note)

DEFAULT_PERSIST_DIR = os.path.expanduser(
    "~/Library/Application Support/Grok Bot/sand-client-persistence")
REPLICA_MARK = ".transcript.replicas."
ROSTER_SUFFIX = ".roster.last-roster"
KNOWN_REPLICA_SCHEMAS = {1}
# Roster rows kept the same shape (id, name, createdAt, isHiddenFromSidebar)
# when the desktop app moved from schema 1 to schema 4.
KNOWN_ROSTER_SCHEMAS = {1, 4}
# send-message types that are UI chrome, not conversation text.
_SKIP_SEND_TYPES = {"widget", "auto-review-approval", "local-tool-permission",
                    "secret-request"}
_KNOWN_KINDS = {"message", "send-message", "user-attachment", "notice", "event",
                "tool-call", "thinking", "reasoning"}
# Seconds to wait and retry a blob the app may be writing. Tests set this to 0.
_RETRY_DELAY = 0.2


def warn(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr)


def decode_blob_name(filename: str) -> str | None:
    """Lowercase unpadded base32 filename → slice key. None if it is not a blob."""
    if not filename.endswith(".blob"):
        return None
    raw = filename[:-len(".blob")].upper()
    try:
        return base64.b32decode(raw + "=" * (-len(raw) % 8)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _read_envelope(path: str) -> tuple[int | None, dict | None]:
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(obj, dict) or not isinstance(obj.get("value"), dict):
        return None, None
    schema = obj.get("schemaVersion")
    return (schema if isinstance(schema, int) else None), obj["value"]


def _account_and_agent(key: str) -> tuple[str, str] | None:
    """Split an account-scoped replica key into (account_id, agent_id)."""
    if REPLICA_MARK not in key or ".account." not in key:
        return None
    acct_part, agent_id = key.split(REPLICA_MARK, 1)
    if not agent_id:
        return None
    account_id = urllib.parse.unquote(acct_part.split(".account.", 1)[1])
    return account_id, agent_id


def iter_replica_refs(root: str = DEFAULT_PERSIST_DIR):
    """Yield (agent_id, account_id, path) for every replica filename.

    Does not open the blob. Used so a half-written file can still keep its
    manifest entry for the next run.
    """
    if not root or not os.path.isdir(root):
        return
    for fn in sorted(os.listdir(root)):
        key = decode_blob_name(fn)
        if not key:
            continue
        parsed = _account_and_agent(key)
        if not parsed:
            continue
        account_id, agent_id = parsed
        yield agent_id, account_id, os.path.join(root, fn)


def iter_replicas(root: str = DEFAULT_PERSIST_DIR):
    """Yield replica dicts: agent_id, account_id, path, entries, mtime, schema.

    Entries are sorted by (seq, timestampMs). Unreadable blobs are skipped
    after one retry.
    """
    if not root or not os.path.isdir(root):
        return
    for fn in sorted(os.listdir(root)):
        key = decode_blob_name(fn)
        if not key:
            continue
        parsed = _account_and_agent(key)
        if not parsed:
            continue
        account_id, agent_id = parsed
        path = os.path.join(root, fn)
        schema, value = _read_envelope(path)
        if value is None:
            if _RETRY_DELAY:
                time.sleep(_RETRY_DELAY)
            schema, value = _read_envelope(path)
        if value is None:
            warn(f"(grokbot) unreadable replica {fn}")
            continue
        if schema not in KNOWN_REPLICA_SCHEMAS:
            warn(f"(grokbot) replica schemaVersion={schema} untested; parsing best-effort")
        raw_entries = value.get("entries")
        if "entries" in value and not isinstance(raw_entries, list):
            warn(f"(grokbot) replica {agent_id} entries is not a list; skipping")
            continue
        if not isinstance(raw_entries, list):
            raw_entries = []
        entries = [e for e in raw_entries if isinstance(e, dict)]
        entries.sort(key=lambda e: (e.get("seq") or 0, e.get("timestampMs") or 0))
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        yield {
            "agent_id": agent_id,
            "account_id": account_id,
            "path": path,
            "entries": entries,
            "mtime": mtime,
            "schema": schema,
        }


def load_roster(root: str = DEFAULT_PERSIST_DIR) -> dict[str, dict[str, dict]]:
    """Return ``{account_id: {agent_id: row}}`` from roster blobs."""
    out: dict[str, dict[str, dict]] = {}
    if not root or not os.path.isdir(root):
        return out
    for fn in os.listdir(root):
        key = decode_blob_name(fn)
        if not key or not key.endswith(ROSTER_SUFFIX) or ".account." not in key:
            continue
        account_id = urllib.parse.unquote(
            key.split(".account.", 1)[1][: -len(ROSTER_SUFFIX)])
        schema, value = _read_envelope(os.path.join(root, fn))
        if value is None:
            warn(f"(grokbot) unreadable roster {fn}")
            continue
        if schema not in KNOWN_ROSTER_SCHEMAS:
            warn(f"(grokbot) roster schemaVersion={schema} untested; parsing best-effort")
        rows = value.get("rows") or []
        if not isinstance(rows, list):
            continue
        out[account_id] = {
            row["id"]: row for row in rows
            if isinstance(row, dict) and row.get("id")
        }
    return out


def _loads(value) -> dict | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        obj = json.loads(value)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def load_store_entries(path: str) -> list[dict]:
    """Read ``transcript_entries`` from a store.db. Empty list on any error."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT entry FROM transcript_entries ORDER BY seq"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    entries = []
    for (raw,) in rows:
        entry = _loads(raw)
        if entry:
            entries.append(entry)
    entries.sort(key=lambda e: (e.get("seq") or 0, e.get("timestampMs") or 0))
    return entries


def _agent_label(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("id") or "agent")
    return str(value)


def _tool_args(entry: dict):
    args = entry.get("arguments")
    if args is None:
        args = entry.get("args")
    if args is None:
        args = entry.get("input")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            pass
    return args


def _touch_time(started, ended, entry):
    ts = epoch_to_dt(entry.get("timestampMs"))
    if not ts:
        return started, ended
    return (started or ts), ts


def parse_entries(entries, include_thinking: bool = False,
                  include_agent_mail: bool = True,
                  verbose: bool = False) -> dict:
    """Turn entry dicts into sections plus timing and a skipped-kind count."""
    sections = Sections()
    started = ended = None
    skipped = 0
    seqs = []

    def skip_unknown(kind, detail: str = "") -> None:
        nonlocal skipped
        skipped += 1
        if verbose:
            extra = f" {detail}" if detail else ""
            warn(f"(grokbot) skipped unknown {kind}{extra}")

    for entry in entries or []:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        if isinstance(entry.get("seq"), int):
            seqs.append(entry["seq"])
        if entry.get("isStreaming") is True:
            continue
        kind = entry.get("kind")
        rendered = False
        if kind == "message":
            if entry.get("suppressed") is not None:
                continue
            role = entry.get("role")
            from_agent = entry.get("fromAgent")
            to_agent = entry.get("toAgent")
            content = entry.get("content")
            text = clean_text(content if isinstance(content, str) else "")
            if role == "user" and not from_agent:
                if text:
                    sections.add_user(text)
                    rendered = True
            elif role == "user" and from_agent:
                if include_agent_mail and text:
                    sections.add_assistant(
                        f"From {_agent_label(from_agent)}: {text}")
                    rendered = True
            elif role == "assistant":
                if to_agent and not include_agent_mail:
                    pass
                else:
                    frags = []
                    if include_thinking:
                        think = entry.get("thinking") or entry.get("reasoning")
                        if isinstance(think, str) and think.strip():
                            frags.append(thinking_details(think.strip()))
                    if text:
                        if to_agent:
                            text = f"To {_agent_label(to_agent)}: {text}"
                        frags.append(text)
                    if frags:
                        sections.add_assistant(frags)
                        rendered = True
            elif role == "tool":
                sections.add_assistant(result_note(bool(entry.get("isError"))))
                rendered = True
            else:
                skip_unknown("message", f"role={role}")
        elif kind == "send-message":
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            mtype = message.get("type")
            if mtype == "text":
                content = message.get("content")
                text = clean_text(content if isinstance(content, str) else "")
                if text:
                    sections.add_assistant(text)
                    rendered = True
            elif mtype == "attachment":
                name = message.get("file_name") or message.get("url") or "attachment"
                sections.add_assistant(f"Attached: `{name}`")
                rendered = True
            elif mtype == "cursor-agent":
                title = message.get("title") or message.get("bcId") or "agent"
                sections.add_assistant(f"[Cloud agent: {title}]")
                rendered = True
            elif mtype in _SKIP_SEND_TYPES:
                pass
            else:
                skip_unknown("send-message", f"type={mtype}")
        elif kind == "user-attachment":
            name = entry.get("file_name") or entry.get("file_path") or "file"
            sections.add_user(f"Attached: `{name}`")
            rendered = True
        elif kind == "notice":
            text = clean_text(entry.get("text") or "")
            if text:
                sections.add_assistant(text)
                rendered = True
        elif kind == "tool-call":
            sections.add_assistant(tool_note(entry.get("name"), _tool_args(entry)))
            rendered = True
        elif kind in ("thinking", "reasoning"):
            if include_thinking:
                text = (entry.get("text") or entry.get("thinking") or "").strip()
                if text:
                    sections.add_assistant(thinking_details(text))
                    rendered = True
        elif kind == "event":
            pass
        elif kind not in _KNOWN_KINDS:
            skip_unknown(kind or "entry")
        if rendered:
            started, ended = _touch_time(started, ended, entry)

    first_seq = min(seqs) if seqs else None
    return {
        "sections": sections,
        "started": started,
        "ended": ended,
        "first_seq": first_seq,
        "skipped": skipped,
    }


def parse_store(path: str, include_thinking: bool = False,
                include_agent_mail: bool = True, verbose: bool = False) -> dict:
    """Fallback: store.db rows through the same entry renderer."""
    return parse_entries(
        load_store_entries(path),
        include_thinking=include_thinking,
        include_agent_mail=include_agent_mail,
        verbose=verbose,
    )


def _derive_title(first_prompt: str | None, fallback: str) -> str:
    if not first_prompt:
        return fallback
    title = " ".join(first_prompt.split())
    return title[:60] + ("…" if len(title) > 60 else "")


def grokbot_entries_to_md(entries, meta_extra: dict | None = None,
                          include_thinking: bool = False,
                          title: str | None = None,
                          truncated_before_seq: int | None = None,
                          include_agent_mail: bool = True,
                          verbose: bool = False) -> dict:
    """Render an entry list. ``truncated_before_seq`` adds the cache-window note."""
    parsed = parse_entries(
        entries,
        include_thinking=include_thinking,
        include_agent_mail=include_agent_mail,
        verbose=verbose,
    )
    if truncated_before_seq is None:
        seqs = [e.get("seq") for e in entries or []
                if isinstance(e, dict) and isinstance(e.get("seq"), int)]
        first = min(seqs) if seqs else None
        if first and first > 1:
            truncated_before_seq = first
    sections = parsed["sections"]
    final = (title or "").strip() or _derive_title(sections.first_prompt, "Grok Bot")
    meta = []
    if parsed["started"]:
        meta.append(f"- Started: {fmt_dt(parsed['started'])}")
    if parsed["ended"] and parsed["ended"] != parsed["started"]:
        meta.append(f"- Ended: {fmt_dt(parsed['ended'])}")
    if truncated_before_seq:
        meta.append(
            f"- Note: history before seq {truncated_before_seq} not available "
            "locally (desktop cache window)")
    for key, val in (meta_extra or {}).items():
        if val:
            meta.append(f"- {key}: {val}")
    return {
        "markdown": build_markdown(final, meta, sections, assistant_label="Grok Bot"),
        "title": final,
        "first_prompt": sections.first_prompt,
        "started": parsed["started"],
        "ended": parsed["ended"],
        "user_turns": sections.user_turns,
        "skipped": parsed["skipped"],
        "first_seq": parsed["first_seq"],
    }


def grokbot_to_md(path: str, meta_extra: dict | None = None,
                  include_thinking: bool = False,
                  title: str | None = None,
                  include_agent_mail: bool = True,
                  verbose: bool = False) -> dict:
    """Render a store.db. Same path as ``grokbot_entries_to_md``."""
    return grokbot_entries_to_md(
        load_store_entries(path),
        meta_extra=meta_extra,
        include_thinking=include_thinking,
        title=title,
        include_agent_mail=include_agent_mail,
        verbose=verbose,
    )


def archive_path(out_root: str, agent_id: str) -> str:
    """Append-only archive lives under the export root, not in the app dir."""
    safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in agent_id) or "agent"
    return os.path.join(out_root, "grokbot", ".archive", f"{safe}.jsonl")


def _read_jsonl(path: str) -> tuple[list[dict], int]:
    """Return (entries, corrupt_line_count). Missing file is an empty archive."""
    if not path or not os.path.isfile(path):
        return [], 0
    good: list[dict] = []
    bad = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if isinstance(obj, dict):
                    good.append(obj)
                else:
                    bad += 1
    except (OSError, UnicodeDecodeError):
        warn(f"(grokbot) could not read archive {path}")
        return [], 0
    return good, bad


def _atomic_write_jsonl(path: str, entries: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _sort_entries(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: (e.get("seq") or 0, e.get("timestampMs") or 0))


def merge_into_archive(archive: str, window: list[dict] | None) -> list[dict]:
    """Merge a cache window into the append-only archive and return every entry.

    Streaming placeholders are not stored. Entries without ``id`` are not
    stored (the caller can still render them). A failed write leaves the
    previous file in place; the returned list is still the merged set.
    """
    old, bad = _read_jsonl(archive)
    by_id: dict[str, dict] = {}
    for entry in old:
        eid = entry.get("id")
        if eid:
            by_id[eid] = entry
    changed = False
    for entry in window or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("isStreaming") is True:
            continue
        eid = entry.get("id")
        if not eid:
            continue
        prev = by_id.get(eid)
        if prev is None or prev != entry:
            by_id[eid] = entry
            changed = True
    merged = _sort_entries(list(by_id.values()))
    by_seq: dict[int, set] = {}
    for entry in merged:
        seq = entry.get("seq")
        if isinstance(seq, int):
            by_seq.setdefault(seq, set()).add(entry.get("id"))
    conflicts = [seq for seq, ids in by_seq.items() if len(ids) > 1]
    if conflicts:
        warn(f"(grokbot) {len(conflicts)} seq value(s) map to more than one id")
    if not archive:
        return merged
    if changed or bad:
        try:
            if bad and archive and os.path.isfile(archive):
                warn(f"(grokbot) skipped {bad} corrupt archive line(s) in {archive}")
                shutil.copy2(archive, archive + ".bak")
            if changed or bad:
                _atomic_write_jsonl(archive, merged)
        except OSError as exc:
            warn(f"(grokbot) could not write archive {archive}: {exc}")
    return merged


def entries_for_render(merged: list[dict], window: list[dict] | None) -> list[dict]:
    """Archived entries plus this window's id-less, non-streaming entries."""
    extra = []
    for entry in window or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("isStreaming") is True or entry.get("id"):
            continue
        extra.append(entry)
    return _sort_entries(list(merged) + extra)


if __name__ == "__main__":
    out = grokbot_to_md(sys.argv[1], meta_extra={"Source": "Grok Bot"},
                        include_thinking="--thinking" in sys.argv)
    sys.stdout.write(out["markdown"])
