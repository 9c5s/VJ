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

import json
import logging
import subprocess
from typing import TYPE_CHECKING, Any

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


_CODEC_MAP: dict[str, str] = {
    "opus": "libopus",
    "vorbis": "libvorbis",
    "mp3": "libmp3lame",
}


def probe_media(filepath: Path) -> dict[str, Any]:
    """ffprobeで入力ファイルの音声メタデータを取得する

    Args:
        filepath: 入力ファイルのパス

    Returns:
        audio_codec, sample_rate, audio_bitrateを含む辞書
        取得できなかった場合は空辞書
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(filepath),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        logger.warning("ffprobeの実行に失敗しました: %s", filepath)
        return {}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("ffprobeの出力を解析できませんでした: %s", filepath)
        return {}

    audio_stream = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "audio":
            audio_stream = stream
            break

    if audio_stream is None:
        logger.warning("音声ストリームが見つかりませんでした: %s", filepath)
        return {}

    defaults: dict[str, Any] = {}

    codec = audio_stream.get("codec_name")
    if codec:
        defaults["audio_codec"] = _CODEC_MAP.get(codec, codec)

    sample_rate = audio_stream.get("sample_rate")
    if sample_rate is not None:
        defaults["sample_rate"] = int(sample_rate)

    bit_rate = audio_stream.get("bit_rate")
    if bit_rate is not None:
        defaults["audio_bitrate"] = f"{int(bit_rate) // 1000}k"

    return defaults
