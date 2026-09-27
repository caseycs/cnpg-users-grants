"""Unified diffs for the terminal."""

from __future__ import annotations

import difflib
import sys
from typing import TextIO


def print_diff(before: str, after: str, name: str, indent: str = "", out: TextIO | None = None) -> None:
    out = out or sys.stdout
    if before == after:
        print(f"{indent}(no changes)", file=out)
        return
    text = "\n".join(difflib.unified_diff(
        before.splitlines(), after.splitlines(), fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="",
    ))
    if sys.stdout.isatty():
        from pygments import highlight
        from pygments.formatters import TerminalFormatter
        from pygments.lexers.diff import DiffLexer
        text = highlight(text, DiffLexer(), TerminalFormatter()).rstrip("\n")
    print("\n".join(indent + line for line in text.splitlines()), file=out)
