import sys
import threading
import time

from cnpg_users.runner import run_in_order


def test_reports_printed_in_order_despite_parallel_completion(capsys):
    started = threading.Barrier(3)  # proves the three reads really overlap

    def work(item, out):
        started.wait(timeout=5)
        time.sleep({"a": 0.2, "b": 0.0, "c": 0.1}[item])
        for i in range(3):
            print(f"{item}{i}", file=out)
        return item == "b", None

    code = run_in_order(["a", "b", "c"], work)
    assert capsys.readouterr().out == "a0\na1\na2\n\nb0\nb1\nb2\n\nc0\nc1\nc2\n"
    assert code == 3


def test_error_stays_in_its_report_and_others_run(capsys):
    def work(item, out):
        print(f"== {item}", file=out)
        if item == "bad":
            sys.exit("boom")
        if item == "worse":
            raise RuntimeError("kaput")
        return False, None

    code = run_in_order(["ok1", "bad", "worse", "ok2"], work)
    text = capsys.readouterr().out
    assert code == 1
    assert "== bad\nERROR: boom\n" in text
    assert "== worse\nERROR:\n" in text and "RuntimeError: kaput" in text
    assert text.index("== ok1") < text.index("== bad") < text.index("== worse") < text.index("== ok2")


def test_after_runs_once_with_every_payload(capsys):
    def work(item, out):
        print(f"read {item}", file=out)
        if item == "bad":
            sys.exit("boom")
        return True, item if item != "offline" else None

    seen = []

    def after(payloads):
        seen.append(payloads)
        print("apply " + ",".join(payloads))
        return False  # applied: no drift left

    code = run_in_order(["x", "offline", "bad", "y"], work, after=after)
    out = capsys.readouterr().out
    assert seen == [["x", "y"]]  # once, after every report, only dbs that read fine
    assert out.endswith("read y\napply x,y\n")
    assert out.index("read offline") < out.index("read bad") < out.index("apply")
    assert code == 1  # "bad" failed


def test_after_drift_decides_exit_code():
    work = lambda item, out: (True, item)  # noqa: E731
    assert run_in_order(["a"], work, after=lambda payloads: False) == 0
    assert run_in_order(["a"], work, after=lambda payloads: True) == 3


def test_all_in_sync():
    assert run_in_order(["a"], lambda item, out: (False, None)) == 0
