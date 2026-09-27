"""Run a command for several db files: read in parallel, print in order.

Each db's report is written to its own buffer by a worker thread and printed
as soon as it and every db before it are done, so output never interleaves.
An optional `after` step runs once in the main thread when every report is
printed, with the payloads of the dbs that read fine (sync-users --apply).
Ctrl+C cancels the dbs not started yet and re-raises KeyboardInterrupt;
reports already printed are complete, nothing is printed half."""

from __future__ import annotations

import io
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable

MAX_WORKERS = 4  # default for --parallel
STATUS_SECONDS = 0.5


def _status(message: str = "") -> None:
    """Rewrite the progress line on stderr (terminals only; stdout stays the reports)."""
    if sys.stderr.isatty():
        sys.stderr.write("\r\x1b[K" + message)
        sys.stderr.flush()


def _multiline(text: str) -> bool:
    return text.rstrip("\n").count("\n") > 0


@dataclass
class Report:
    text: str
    drift: bool = False
    error: bool = False
    payload: Any = None


def collect(work: Callable[..., tuple[bool, Any]], *args) -> Report:
    """Call work(*args, out=buffer) -> (drift, payload); errors become part of the report."""
    out = io.StringIO()
    try:
        drift, payload = work(*args, out=out)
        return Report(out.getvalue(), drift, payload=payload)
    except SystemExit as exc:
        print(f"ERROR: {exc.code}", file=out)
    except Exception:  # noqa: BLE001 — one broken db must not hide the others
        print("ERROR:\n" + traceback.format_exc(), file=out)
    return Report(out.getvalue(), error=True)


def run_in_order(
    items: list, work: Callable[..., tuple[bool, Any]],
    after: Callable[[list], bool] | None = None, max_workers: int = MAX_WORKERS,
) -> int:
    """Exit code: 1 if any db failed, else 3 if any drift, else 0. With
    `after`, its result replaces the drift of the dbs it got payloads for."""
    reports: list[Report] = []
    workers = max(1, min(max_workers, len(items)))
    print(f"Reading {len(items)} db file(s), {workers} at a time...", file=sys.stderr, flush=True)
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(collect, work, item) for item in items]
        for i, future in enumerate(futures):
            while not future.done():
                done = sum(f.done() for f in futures)
                _status(f"  {done}/{len(items)} read — waiting for {getattr(items[i], 'name', items[i])}")
                wait([future], timeout=STATUS_SECONDS)
            _status()
            report = future.result()
            # a blank line only around multi-line reports; one-liners stay together
            if i and (_multiline(reports[-1].text) or _multiline(report.text)):
                print()
            print(report.text, end="", flush=True)
            reports.append(report)
    except KeyboardInterrupt:
        # Ctrl+C: drop dbs not started yet; don't wait for reads in flight
        _status()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    error = any(r.error for r in reports)
    handed = [r for r in reports if not r.error and r.payload is not None]
    drift = any(r.drift for r in reports if r not in handed)
    if after is not None and handed:
        try:
            drift |= after([r.payload for r in handed])
        except SystemExit as exc:
            print(f"ERROR: {exc.code}", flush=True)
            error = True
    else:
        drift |= any(r.drift for r in handed)
    sys.stdout.flush()
    return 1 if error else 3 if drift else 0
