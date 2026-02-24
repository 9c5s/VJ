# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "ffmpeg-normalize",
#     "yt-dlp-audio-normalize",
# ]
# ///
"""音量正規化スクリプト

ファイルやフォルダに対してffmpeg-normalizeを適用する
CLI引数とドラッグ&ドロップの両方に対応する
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)


def collect_files(paths: Sequence[Path]) -> list[Path]:
    """パスリストからファイルを収集する

    ファイルはそのまま、ディレクトリは再帰的に走査してファイルを収集する
    存在しないパスは警告を出力してスキップする

    Args:
        paths: ファイルまたはディレクトリのパスリスト

    Returns:
        収集されたファイルパスのリスト
    """
    files: list[Path] = []
    for path in paths:
        if not path.exists():
            logger.warning("パスが存在しません: %s", path)
            continue
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(p for p in path.rglob("*") if p.is_file()))
    return files
