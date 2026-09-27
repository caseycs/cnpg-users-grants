#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["ruamel.yaml", "pygments", "kubernetes", "boto3"]
# ///
"""Run from a checkout without installing: ./cnpg-users.py (see README)."""

from cnpg_users.cli import run

if __name__ == "__main__":
    run()
