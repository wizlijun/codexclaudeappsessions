#!/usr/bin/env python3
"""Regression test: filename disambiguation for distinct sessions.

The manifest is the primary identity source: a known session key overwrites its
recorded path in place (handled in run_tasks via force_path). Only sessions the
manifest does NOT know reach reserve(). Any collision there is therefore between
DIFFERENT sessions sharing the same date+title, so we must keep both files. We
disambiguate with the session start time (deterministic from content) instead of
a processing-order ordinal (-2/-3), which is unstable if the manifest is lost.
"""
import export_sessions as es


def setup():
    es._USED.clear()


def test_first_session_keeps_bare_name():
    setup()
    assert es.reserve("d", "20260701-ping", "125742") == "20260701-ping"


def test_collision_disambiguates_by_start_time():
    setup()
    a = es.reserve("d", "20260207-ping", "125742")
    b = es.reserve("d", "20260207-ping", "125801")
    assert a == "20260207-ping", a
    assert b == "20260207-ping-125801", b


def test_same_second_collision_falls_back_to_ordinal():
    setup()
    a = es.reserve("d", "20260207-ping", "125742")
    b = es.reserve("d", "20260207-ping", "125742")
    c = es.reserve("d", "20260207-ping", "125742")
    assert a == "20260207-ping", a
    assert b == "20260207-ping-125742", b  # bare taken -> time tag
    assert c == "20260207-ping-125742-2", c  # time tag taken too -> ordinal


def test_no_time_available_uses_ordinal():
    setup()
    a = es.reserve("d", "20260207-ping", None)
    b = es.reserve("d", "20260207-ping", None)
    assert a == "20260207-ping", a
    assert b == "20260207-ping-2", b


def test_scoped_per_directory():
    setup()
    a = es.reserve("d1", "20260207-ping", "125742")
    b = es.reserve("d2", "20260207-ping", "125801")
    assert a == "20260207-ping", a
    assert b == "20260207-ping", b  # different dir, no collision


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
