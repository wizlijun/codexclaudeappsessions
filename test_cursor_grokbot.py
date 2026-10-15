#!/usr/bin/env python3
"""Parsers for Cursor transcripts and Grok Bot sessions."""
import base64
import json
import os
import sqlite3
import tempfile
import urllib.parse

import cursor_md
import export_sessions as es
import grokbot_md


def test_conversation_state_hashes_roundtrip():
    digest = bytes(range(32))
    blob = b"\x0a\x20" + digest  # field 1, length-delimited, 32 bytes
    state = "~" + base64.b64encode(blob).decode()
    assert cursor_md.hashes_from_conversation_state(state) == [digest.hex()]
    assert cursor_md.hashes_from_conversation_state("~") == []
    assert cursor_md.hashes_from_conversation_state("") == []


def test_cursor_jsonl_drops_harness_and_notes_tools():
    lines = [
        {"role": "user", "message": {"content": [
            {"type": "text", "text": "<user_info>\nOS Version: darwin\n</user_info>"}]}},
        {"role": "user", "message": {"content": [
            {"type": "text", "text":
             "<attached_files>\n<code_selection path=\"/tmp/a.py\" lines=\"1-2\">\n"
             "x = 1\n</code_selection>\n</attached_files>\n"
             "<user_query>\nfix the bug\n</user_query>"}]}},
        {"role": "assistant", "message": {"content": [
            {"type": "reasoning", "text": "secret chain of thought"},
            {"type": "text", "text": "Looking now."},
            {"type": "tool_use", "name": "Read", "input": {"path": "/tmp/a.py"}}]}}
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sess.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for obj in lines:
                fh.write(json.dumps(obj) + "\n")
        out = cursor_md.cursor_jsonl_to_md(path, meta_extra={"Source": "Cursor"})
    assert out["user_turns"] == 1, out["user_turns"]
    md = out["markdown"]
    assert "fix the bug" in md
    assert "OS Version" not in md
    assert "secret chain of thought" not in md
    assert "Looking now." in md
    assert "**Read**" in md
    assert "`/tmp/a.py`" in md
    assert out["title"].startswith("fix the bug")


def test_cursor_jsonl_includes_thinking_when_asked():
    lines = [
        {"role": "user", "message": {"content": [
            {"type": "text", "text": "<user_query>\nping\n</user_query>"}]}},
        {"role": "assistant", "message": {"content": [
            {"type": "reasoning", "text": "chain of thought"},
            {"type": "text", "text": "pong"}]}},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sess.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for obj in lines:
                fh.write(json.dumps(obj) + "\n")
        out = cursor_md.cursor_jsonl_to_md(path, include_thinking=True)
    assert "chain of thought" in out["markdown"]
    assert "pong" in out["markdown"]


def test_cursor_kv_messages_skip_system_and_user_info():
    messages = [
        {"role": "system", "content": "You are an AI coding assistant."},
        {"role": "user", "content": "<user_info>\nWorkspace Path: /tmp/proj\n</user_info>"},
        {"role": "user", "content": [
            {"type": "text", "text": "<timestamp>Thu</timestamp>\n<user_query>\nhello\n</user_query>"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "hi"},
            {"type": "tool-call", "toolName": "Glob", "args": {"glob_pattern": "*.py"}}]},
        {"role": "tool", "content": [
            {"type": "tool-result", "toolName": "Glob", "result": "a.py"}]},
    ]
    parsed = cursor_md.parse_messages(messages)
    out = cursor_md.cursor_to_md(parsed["sections"], None, {"Source": "Cursor"})
    md = out["markdown"]
    assert out["user_turns"] == 1
    assert "hello" in md
    assert "You are an AI coding assistant" not in md
    assert "Workspace Path" not in md
    assert "a.py" not in md  # tool results stay a one-line note
    assert "tool result" in md
    assert "**Glob**" in md and "*.py" in md


def test_git_grokbot_tree_is_ignored():
    root = "/Users/bruce/git/grokbot"
    assert es._grokbot_path_ignored(root)
    assert es._grokbot_path_ignored(root + "/bots/bots-agent-data.tgz")
    assert es._grokbot_search_roots(root) == []
    assert es._grokbot_search_roots(root + "/bots/bots-agent-data.tgz") == []
    assert not es._grokbot_path_ignored(os.path.expanduser("~/.grokbot"))
    assert not es._grokbot_path_ignored(None)


def test_grokbot_store_renders_messages_and_skips_events():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "store.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE transcript_entries (seq INTEGER, id TEXT, entry TEXT)")
        rows = [
            (1, "e1", {"kind": "event", "event": {"type": "name-changed", "from": "A", "to": "B"},
                       "timestampMs": 1782849600000}),
            (2, "m1", {"kind": "message", "role": "user", "content": "今天的安排",
                       "timestampMs": 1782849600000}),
            (3, "m2", {"kind": "message", "role": "assistant", "content": "先看日历。",
                       "fromAgent": None, "timestampMs": 1782849660000}),
            (4, "t1", {"kind": "tool-call", "name": "Calendar",
                       "arguments": {"query": "today"}, "timestampMs": 1782849661000}),
            (5, "a1", {"kind": "user-attachment", "file_name": "notes.txt",
                       "file_path": "/tmp/notes.txt", "timestampMs": 1782849700000}),
        ]
        con.executemany("INSERT INTO transcript_entries VALUES (?, ?, ?)",
                        [(seq, eid, json.dumps(entry)) for seq, eid, entry in rows])
        con.commit()
        con.close()
        out = grokbot_md.grokbot_to_md(path, meta_extra={"Agent": "小布"})
    md = out["markdown"]
    assert out["user_turns"] == 2, out["user_turns"]
    assert "今天的安排" in md
    assert "先看日历。" in md
    assert "**Calendar**" in md
    assert "notes.txt" in md
    assert "name-changed" not in md
    assert "小布" in md


def _blob_name(key: str) -> str:
    raw = base64.b32encode(key.encode()).decode("ascii").lower().rstrip("=")
    return raw + ".blob"


def _replica_key(account: str, agent_id: str) -> str:
    enc = urllib.parse.quote(account, safe="")
    return f"sand.client.slice.account.{enc}.transcript.replicas.{agent_id}"


def _write_blob(root: str, key: str, value: dict, schema: int = 1) -> str:
    path = os.path.join(root, _blob_name(key))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schemaVersion": schema, "value": value}, fh)
    return path


def test_decode_blob_name_roundtrip():
    key = "sand.client.slice.account.google-oauth2%7Cuser_x.transcript.replicas.abc"
    assert grokbot_md.decode_blob_name(_blob_name(key)) == key
    assert grokbot_md.decode_blob_name("notes.txt") is None
    assert grokbot_md.decode_blob_name("not*valid.blob") is None


def test_iter_replicas_and_roster():
    with tempfile.TemporaryDirectory() as tmp:
        a1 = "11111111-1111-1111-1111-111111111111"
        a2 = "22222222-2222-2222-2222-222222222222"
        a3 = "33333333-3333-3333-3333-333333333333"
        acct_a = "google-oauth2|user_a"
        acct_b = "google-oauth2|user_b"
        _write_blob(tmp, _replica_key(acct_a, a1), {"entries": [
            {"id": "b", "seq": 2, "timestampMs": 2, "kind": "message"},
            {"id": "a", "seq": 1, "timestampMs": 1, "kind": "message"},
        ]})
        _write_blob(tmp, _replica_key(acct_a, a2), {"entries": [
            {"id": "c", "seq": 1, "timestampMs": 1, "kind": "message"},
        ]})
        _write_blob(tmp, _replica_key(acct_b, a3), {"entries": []})
        enc_a = urllib.parse.quote(acct_a, safe="")
        _write_blob(tmp, f"sand.client.slice.account.{enc_a}.ui-layout", {"theme": "dark"})
        _write_blob(tmp, f"sand.client.slice.account.{enc_a}.roster.last-roster", {"rows": [
            {"id": a1, "name": "小布"},
            {"id": a2, "name": "New Bot"},
        ]})
        bad = _blob_name(_replica_key(acct_b, "44444444-4444-4444-4444-444444444444"))
        with open(os.path.join(tmp, bad), "w", encoding="utf-8") as fh:
            fh.write("{")
        prev = grokbot_md._RETRY_DELAY
        grokbot_md._RETRY_DELAY = 0
        try:
            reps = list(grokbot_md.iter_replicas(tmp))
        finally:
            grokbot_md._RETRY_DELAY = prev
        assert len(reps) == 3
        by_id = {r["agent_id"]: r for r in reps}
        assert by_id[a1]["account_id"] == acct_a
        assert [e["id"] for e in by_id[a1]["entries"]] == ["a", "b"]
        assert by_id[a3]["account_id"] == acct_b
        roster = grokbot_md.load_roster(tmp)
        assert roster[acct_a][a1]["name"] == "小布"
        assert a3 not in roster.get(acct_b, {})


def test_parse_entries_covers_spec_table():
    entries = [
        {"kind": "message", "role": "user", "content": "hello", "seq": 1, "id": "u"},
        {"kind": "message", "role": "user", "content": "partial", "isStreaming": True,
         "seq": 2, "id": "s"},
        {"kind": "message", "role": "user", "content": "from bot",
         "fromAgent": {"id": "x", "name": "GTMaster"}, "seq": 3, "id": "f"},
        {"kind": "message", "role": "assistant", "content": "to bot",
         "toAgent": {"id": "y", "name": "小布"}, "seq": 4, "id": "t"},
        {"kind": "send-message", "message": {"type": "text", "content": "reply"},
         "seq": 5, "id": "sm"},
        {"kind": "send-message", "message": {"type": "attachment", "file_name": "pic.png"},
         "seq": 6, "id": "at"},
        {"kind": "send-message", "message": {"type": "cursor-agent", "title": "Fix tests",
                                            "bcId": "bc1"}, "seq": 7, "id": "ca"},
        {"kind": "send-message", "message": {"type": "widget", "widget": {"title": "SECRETWIDGET"}},
         "seq": 8, "id": "w"},
        {"kind": "user-attachment", "file_name": "notes.txt", "seq": 9, "id": "ua"},
        {"kind": "notice", "text": "renamed ok", "seq": 10, "id": "n"},
        {"kind": "event", "event": {"type": "name-changed"}, "seq": 11, "id": "ev"},
        {"kind": "mystery", "seq": 12, "id": "x"},
    ]
    parsed = grokbot_md.parse_entries(entries)
    md = grokbot_md.grokbot_entries_to_md(entries)["markdown"]
    assert parsed["skipped"] == 1
    assert "hello" in md and "partial" not in md
    assert "From GTMaster: from bot" in md
    assert "To 小布: to bot" in md
    assert "reply" in md
    assert "Attached: `pic.png`" in md
    assert "[Cloud agent: Fix tests]" in md
    assert "SECRETWIDGET" not in md
    assert "Attached: `notes.txt`" in md
    assert "renamed ok" in md
    assert "name-changed" not in md
    quiet = grokbot_md.grokbot_entries_to_md(entries, include_agent_mail=False)["markdown"]
    assert "from bot" not in quiet and "to bot" not in quiet
    assert "hello" in quiet and "reply" in quiet


def test_parse_store_matches_parse_entries_markdown():
    entries = [
        {"kind": "message", "role": "user", "content": "今天的安排", "seq": 1, "id": "m1",
         "timestampMs": 1782849600000},
        {"kind": "message", "role": "assistant", "content": "先看日历。", "seq": 2, "id": "m2",
         "timestampMs": 1782849660000},
        {"kind": "event", "event": {"type": "name-changed"}, "seq": 3, "id": "e1"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "store.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE transcript_entries (seq INTEGER, id TEXT, entry TEXT)")
        con.executemany(
            "INSERT INTO transcript_entries VALUES (?, ?, ?)",
            [(e["seq"], e["id"], json.dumps(e)) for e in entries])
        con.commit()
        con.close()
        from_store = grokbot_md.grokbot_to_md(path, meta_extra={"Agent": "小布"})
        from_entries = grokbot_md.grokbot_entries_to_md(entries, meta_extra={"Agent": "小布"})
    assert from_store["markdown"] == from_entries["markdown"]
    assert from_store["user_turns"] == 1


def test_merge_into_archive():
    with tempfile.TemporaryDirectory() as tmp:
        archive = grokbot_md.archive_path(tmp, "agent-1")
        first = [{"id": f"e{i}", "seq": i, "kind": "message"} for i in range(1, 101)]
        first.append({"id": "live", "seq": 101, "kind": "message", "isStreaming": True,
                      "content": "nope"})
        first.append({"seq": 102, "kind": "message", "role": "user", "content": "noid"})
        replaced = []
        real_replace = grokbot_md.os.replace

        def wrapped(src, dst):
            assert src.endswith(".tmp")
            replaced.append(dst)
            return real_replace(src, dst)

        grokbot_md.os.replace = wrapped
        try:
            merged = grokbot_md.merge_into_archive(archive, first)
        finally:
            grokbot_md.os.replace = real_replace
        assert replaced == [archive]
        assert not os.path.exists(archive + ".tmp")
        assert len(merged) == 100
        assert all(not e.get("isStreaming") for e in merged)
        rendered = grokbot_md.entries_for_render(merged, first)
        assert any(e.get("content") == "noid" for e in rendered)
        second = [{"id": f"e{i}", "seq": i, "kind": "message"} for i in range(51, 151)]
        second[0] = {"id": "e51", "seq": 51, "kind": "message", "content": "updated"}
        merged = grokbot_md.merge_into_archive(archive, second)
        by_id = {e["id"]: e for e in merged}
        assert len(merged) == 150
        assert by_id["e1"]["seq"] == 1
        assert by_id["e51"]["content"] == "updated"
        assert by_id["e150"]["seq"] == 150
        # Same seq, two ids: both kept.
        clash = grokbot_md.merge_into_archive(archive, [
            {"id": "other", "seq": 1, "kind": "notice", "text": "x"}])
        assert sum(1 for e in clash if e.get("seq") == 1) == 2


def test_corrupt_archive_line_is_backed_up():
    with tempfile.TemporaryDirectory() as tmp:
        archive = grokbot_md.archive_path(tmp, "agent-2")
        os.makedirs(os.path.dirname(archive), exist_ok=True)
        with open(archive, "w", encoding="utf-8") as fh:
            fh.write('{"id":"a","seq":1,"kind":"message"}\n')
            fh.write("{bad\n")
        merged = grokbot_md.merge_into_archive(
            archive, [{"id": "b", "seq": 2, "kind": "message"}])
        assert os.path.isfile(archive + ".bak")
        assert {e["id"] for e in merged} == {"a", "b"}
        text = open(archive, encoding="utf-8").read()
        assert "{bad" not in text


def test_gap_note_only_when_history_starts_after_one():
    late = grokbot_md.grokbot_entries_to_md([
        {"kind": "message", "role": "user", "content": "hi", "seq": 145, "id": "a"},
        {"kind": "message", "role": "assistant", "content": "ok", "seq": 200, "id": "b"},
    ])["markdown"]
    assert "history before seq 145" in late
    assert "desktop cache window" in late
    whole = grokbot_md.grokbot_entries_to_md([
        {"kind": "message", "role": "user", "content": "hi", "seq": 1, "id": "a"},
        {"kind": "message", "role": "assistant", "content": "ok", "seq": 5, "id": "b"},
    ])["markdown"]
    assert "history before seq" not in whole


def test_grokbot_source_present_and_ignored_tree():
    missing = os.path.join(tempfile.gettempdir(), "grokbot-persist-does-not-exist")
    assert es.grokbot_source_present(None, missing) is False
    assert es._grokbot_search_roots(None) == []
    ignored = "/Users/bruce/git/grokbot"
    real_ignored = os.path.realpath(ignored)
    listed = []
    real_listdir = os.listdir

    def wrapped(path):
        target = os.path.realpath(path)
        if target == real_ignored or target.startswith(real_ignored + os.sep):
            raise AssertionError("listed ignored tree")
        listed.append(target)
        return real_listdir(path)

    os.listdir = wrapped
    try:
        assert es.grokbot_source_present(ignored, ignored) is False
    finally:
        os.listdir = real_listdir
    with tempfile.TemporaryDirectory() as tmp:
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        assert es.grokbot_source_present(None, empty) is False
        _write_blob(empty, _replica_key("google-oauth2|user_a", "agent-1"),
                    {"entries": []})
        assert es.grokbot_source_present(None, empty) is True
        persist = os.path.join(tmp, "no-replicas")
        os.makedirs(persist)
        agent = os.path.join(tmp, "agent-data", "agents", "bot-1")
        os.makedirs(agent)
        open(os.path.join(agent, "store.db"), "wb").close()
        assert es.grokbot_source_present(os.path.join(tmp, "agent-data"), persist) is True


def test_unreadable_replica_is_seen_but_not_rendered():
    with tempfile.TemporaryDirectory() as tmp:
        good = "11111111-1111-1111-1111-111111111111"
        bad_id = "22222222-2222-2222-2222-222222222222"
        acct = "google-oauth2|user_a"
        _write_blob(tmp, _replica_key(acct, good), {"entries": [
            {"id": "u", "seq": 1, "kind": "message", "role": "user", "content": "hi",
             "timestampMs": 1700000000000},
        ]})
        with open(os.path.join(tmp, _blob_name(_replica_key(acct, bad_id))), "w",
                  encoding="utf-8") as fh:
            fh.write("{")
        out = os.path.join(tmp, "out")
        prev = grokbot_md._RETRY_DELAY
        grokbot_md._RETRY_DELAY = 0
        try:
            tasks = list(es.grokbot_tasks(
                False, None, None, "acct@example.com", persist_dir=tmp, out_root=out))
        finally:
            grokbot_md._RETRY_DELAY = prev
        by_key = {key: (version, render) for key, version, _vendor, render in tasks}
        assert f"grokbot:{good}" in by_key
        assert by_key[f"grokbot:{bad_id}"][1]() is None
        rendered = by_key[f"grokbot:{good}"][1]()
        assert rendered["turns"] == 1
        assert rendered["source"] == "chat"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    raise SystemExit(1 if failed else 0)
