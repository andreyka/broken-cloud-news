#!/usr/bin/env python3
"""Wrapper for running packaged Reddit backfill utility."""

from bcn.ops.backfill_reddit_links import main


if __name__ == "__main__":
    raise SystemExit(main())
