# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "pyperclip",
#     "yt-dlp>=2026.2.4",
#     "yt-dlp-audio-normalize",
# ]
# ///
"""yt-dlp用クリップボード監視ツール

クリップボードを監視し、URLが検出された場合特定の引数でyt-dlpを実行する
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import (
    Final,
    NamedTuple,
    NoReturn,
)
from urllib.parse import urlparse

import pyperclip
import yt_dlp
from yt_dlp.utils import DownloadError
from yt_dlp_plugins.postprocessor.audio_normalize import (  # pyright: ignore[reportMissingTypeStubs]
    AudioNormalizePP,
)

type OptionDict = dict[str, list[str] | None]


class ParsedArgs(NamedTuple):
    """parse_args()の戻り値"""

    yt_dlp_options: list[str]
    normalize: bool


# 設定
POLLING_INTERVAL: Final[float] = 0.1
DOWNLOAD_DIR: Final[Path] = Path.home() / "Downloads" / "yt_dlp"
DOWNLOAD_ARCHIVE_FILE: Final[Path] = (
    Path.home() / "projects" / "ytdlp-archive" / "downloaded.txt"
)
YT_DLP_OPTIONS: Final[list[str]] = [
    "--ignore-config",
    "-S",
    "codec:avc:aac,res:1080,fps:60,hdr:sdr",
    "-f",
    "bv+ba",
    "-P",
    str(DOWNLOAD_DIR),
    "-o",
    "%(title)s_%(id)s",
    "--ppa",
    "Merger+ffmpeg_o1:-map_metadata -1",
    "--ppa",
    "AudioNormalize:-t -14.0 -c:a aac -b:a 128k -ar 48000 -mn -cn -sn -pr",
    "--remote-components",
    "ejs:github",
    "--cookies-from-browser",
    "firefox",
    "--download-archive",
    str(DOWNLOAD_ARCHIVE_FILE),
]


def _parse_option_list(options: list[str]) -> OptionDict:
    """オプションリストをキーと値のペアの辞書に変換する

    同一キーが複数回出現する場合(--ppa等)、全ての値をリストとして保持する

    Args:
        options: yt-dlpオプションのリスト

    Returns:
        オプション名をキー、値がある場合はリスト、
        フラグ型オプションの場合はNoneを値とする辞書
    """
    result: OptionDict = {}
    i = 0
    while i < len(options):
        opt = options[i]
        if opt.startswith("-"):
            if i + 1 < len(options) and not options[i + 1].startswith("-"):
                values = result.get(opt)
                if values is None:
                    values = []
                    result[opt] = values
                values.append(options[i + 1])
                i += 2
            else:
                result[opt] = None
                i += 1
        else:
            i += 1
    return result


def _dict_to_option_list(options: OptionDict) -> list[str]:
    """オプション辞書をリスト形式に変換する

    複数値を持つオプションはキーを繰り返して出力する

    Args:
        options: オプション名と値のペアの辞書

    Returns:
        yt-dlpに渡せるフラットなオプションリスト
    """
    result: list[str] = []
    for key, values in options.items():
        if values is None:
            result.append(key)
        else:
            for value in values:
                result.append(key)
                result.append(value)
    return result


def merge_yt_dlp_options(overrides: list[str]) -> list[str]:
    """デフォルトオプションにCLI引数をマージする

    値付きのオプションは上書き(複数値オプションは全置換)し、新規オプションは追加する
    値なしで渡されたオプションがデフォルトに存在する場合は削除する

    Args:
        overrides: CLI引数から取得したyt-dlpオプションのリスト

    Returns:
        マージ済みのオプションリスト
    """
    base = _parse_option_list(YT_DLP_OPTIONS)
    override_dict = _parse_option_list(overrides)

    for key, value in override_dict.items():
        if value is None and key in base:
            # デフォルトに存在するオプションが値なしで渡された場合は削除
            del base[key]
        else:
            base[key] = value

    return _dict_to_option_list(base)


def parse_args() -> ParsedArgs:
    """コマンドライン引数を解析し、yt-dlpオプションと設定を返す

    Returns:
        パース結果を格納したParsedArgs
    """
    parser = argparse.ArgumentParser(
        description="クリップボードを監視し、URLを検知するとyt-dlpでダウンロードする",
    )
    parser.add_argument(
        "--no-normalize",
        action="store_true",
        help="ダウンロード後の音量正規化をスキップする",
    )
    parser.add_argument(
        "yt_dlp_args",
        nargs=argparse.REMAINDER,
        help="yt-dlpオプション (-- の後に指定)",
    )
    args = parser.parse_args()

    # REMAINDER は先頭の '--' を含む場合があるため除去する
    overrides = args.yt_dlp_args
    if overrides and overrides[0] == "--":
        overrides = overrides[1:]

    if not overrides:
        yt_dlp_options = list(YT_DLP_OPTIONS)
    else:
        yt_dlp_options = merge_yt_dlp_options(overrides)

    return ParsedArgs(yt_dlp_options=yt_dlp_options, normalize=not args.no_normalize)


def setup_logger() -> logging.Logger:
    """アプリケーションロガーを構成して返す"""
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def is_valid_url(text: str) -> bool:
    """指定されたテキストが有効なURLかどうかを判定する

    Args:
        text: 検証する文字列

    Returns:
        テキストが有効なHTTP(S) URLの場合はTrue、そうでない場合はFalse
    """
    try:
        parsed = urlparse(text)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def download_video(
    url: str,
    logger: logging.Logger,
    yt_dlp_options: list[str],
    *,
    normalize: bool = True,
) -> None:
    """指定されたURLに対してyt-dlpでダウンロードを実行する

    Args:
        url: ダウンロード対象のURL
        logger: 状態を出力するためのロガーインスタンス
        yt_dlp_options: yt-dlpに渡すオプションリスト
        normalize: Trueの場合、ダウンロード後に音量を正規化する
    """
    # マージ済みオプションからダウンロードディレクトリを取得して作成する
    parsed = _parse_option_list(yt_dlp_options)
    p_values = parsed.get("-P")
    download_dir = Path(p_values[-1] if p_values else str(DOWNLOAD_DIR))
    try:
        download_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("ダウンロードディレクトリの作成に失敗しました")
        return

    opts = yt_dlp.parse_options(yt_dlp_options).ydl_opts

    try:
        logger.info("ダウンロードを開始します: %s", url)
        logger.debug("yt-dlp options: %s", yt_dlp_options)
        with yt_dlp.YoutubeDL(opts) as ydl:
            if normalize:
                ydl.add_post_processor(AudioNormalizePP(), when="after_move")
            ret = ydl.download([url])
        if ret != 0:
            logger.error("ダウンロードがエラーで終了しました (code=%d)", ret)
        else:
            logger.info("ダウンロードが正常に完了しました")
    except DownloadError:
        logger.exception("ダウンロードに失敗しました")


def monitor_clipboard(yt_dlp_options: list[str], *, normalize: bool = True) -> NoReturn:
    """新しいURLがないかクリップボードを継続的に監視する

    定義された間隔でクリップボードの内容を確認する無限ループを実行する
    変更された内容が有効なURLである場合、ダウンロードプロセスをトリガーする

    Args:
        yt_dlp_options: yt-dlpに渡すオプションリスト
        normalize: Trueの場合、ダウンロード後に音量を正規化する
    """
    logger = setup_logger()
    logger.info("クリップボード監視を開始します")
    logger.info("停止するには Ctrl+C を押してください")

    last_text: str = ""

    try:
        # 起動時に既にURLが存在する場合に反応しないよう、
        # 現在のクリップボードの内容で last_text を初期化する
        last_text = pyperclip.paste()

        while True:
            try:
                current_text = pyperclip.paste()
            except pyperclip.PyperclipException:
                # クリップボードへのアクセスが一時的に失敗した場合の処理
                time.sleep(POLLING_INTERVAL)
                continue

            if current_text != last_text:
                last_text = current_text
                if is_valid_url(current_text):
                    logger.info("URLを検知しました: %s", current_text)
                    download_video(
                        current_text, logger, yt_dlp_options, normalize=normalize
                    )
                else:
                    logger.debug(
                        "クリップボードの変更を検知しましたが、URLではありません"
                    )

            time.sleep(POLLING_INTERVAL)

    except KeyboardInterrupt:
        logger.info("クリップボード監視を停止します")
        sys.exit(0)


if __name__ == "__main__":
    parsed = parse_args()
    monitor_clipboard(parsed.yt_dlp_options, normalize=parsed.normalize)
