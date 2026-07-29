"""`python -m subtext_postmeeting` のエントリ（console_scripts の `subtext-postmeeting` と等価）。

通常の起動は `uv run subtext-postmeeting`（entry point の cli:main）。本ファイルは同じ cli.main へ
委譲し、console-script を経由せずインタプリタを直接指定して起動したい場合の口を残す。
"""

from __future__ import annotations

import sys

from subtext_postmeeting.cli import main

if __name__ == "__main__":
    sys.exit(main())
