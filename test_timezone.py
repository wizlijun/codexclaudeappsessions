#!/usr/bin/env python3
"""Regression test: session timestamps must render in OS local time.

Runs under a fixed TZ (Asia/Shanghai, GMT+8) so results are deterministic.
"""
import datetime as dt
import os
import time

os.environ["TZ"] = "Asia/Shanghai"
time.tzset()

import render_common as rc
import export_sessions as es


def test_fmt_dt_converts_utc_to_local():
    # 2026-06-30 20:00 UTC == 2026-07-01 04:00 local (GMT+8).
    utc = dt.datetime(2026, 6, 30, 20, 0, tzinfo=dt.timezone.utc)
    assert rc.fmt_dt(utc) == "2026-07-01 04:00", rc.fmt_dt(utc)


def test_parse_iso_then_fmt_is_local():
    got = rc.fmt_dt(rc.parse_iso("2026-06-30T20:00:00Z"))
    assert got == "2026-07-01 04:00", got


def test_epoch_to_dt_then_fmt_is_local():
    # 2026-06-30 20:00 UTC epoch seconds.
    epoch = 1782849600
    got = rc.fmt_dt(rc.epoch_to_dt(epoch))
    assert got == "2026-07-01 04:00", got


def test_month_key_uses_local_month():
    # UTC month is June, local month is July — must bucket as July.
    utc = dt.datetime(2026, 6, 30, 20, 0, tzinfo=dt.timezone.utc)
    assert es._month_key(utc) == "2026-07", es._month_key(utc)


def test_filename_prefix_uses_local_date():
    utc = dt.datetime(2026, 6, 30, 20, 0, tzinfo=dt.timezone.utc)
    assert rc.to_local(utc).strftime("%Y%m%d") == "20260701"


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
