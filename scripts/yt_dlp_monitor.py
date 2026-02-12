# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "ffmpeg-normalize",
#     "pyperclip",
#     "yt-dlp",
# ]
# ///
"""yt-dlp用クリップボード監視ツール

クリップボードを監視し、URLが検出された場合特定の引数でyt-dlpを実行する
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Final,
    Literal,
    NoReturn,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)
from urllib.parse import urlparse

import pyperclip
import yt_dlp
from ffmpeg_normalize import FFmpegNormalize, FFmpegNormalizeError

if TYPE_CHECKING:
    from yt_dlp.extractor.common import _InfoDict  # pyright: ignore[reportPrivateUsage]
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import DownloadError

type OptionDict = dict[str, list[str] | None]

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
                result.setdefault(opt, []).append(options[i + 1])  # type: ignore[union-attr]
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


def parse_args() -> list[str]:
    """コマンドライン引数を解析し、yt-dlpオプションを返す

    Returns:
        マージ済みのyt-dlpオプションリスト
    """
    parser = argparse.ArgumentParser(
        description="クリップボードを監視し、URLを検知するとyt-dlpでダウンロードする",
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
        return list(YT_DLP_OPTIONS)
    return merge_yt_dlp_options(overrides)


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


class AudioNormalizePP(PostProcessor):
    """ダウンロード後にffmpeg-normalizeで音量を正規化するPostProcessor

    --ppa "AudioNormalize:ARGS" でffmpeg-normalizeのパラメータを上書きできる
    FFmpegNormalize.__init__の全スカラーパラメータを長形式フラグで自動サポートし、短縮フラグも提供する
    bool型パラメータは値なしフラグとして指定可能(例: --dual-mono)
    """

    # 短縮フラグ→パラメータ名のマッピング(自動導出不可能なもののみ)
    # https://slhck.info/ffmpeg-normalize/usage/cli-options/
    _SHORT_FLAGS: ClassVar[dict[str, str]] = {
        # 正規化
        "-nt": "normalization_type",
        "-t": "target_level",
        "-p": "print_stats",
        # EBU R128
        "-lrt": "loudness_range_target",
        "-tp": "true_peak",
        # 音声エンコード
        "-c:a": "audio_codec",
        "-b:a": "audio_bitrate",
        "-ar": "sample_rate",
        "-ac": "audio_channels",
        "-koa": "keep_original_audio",
        # フィルタ
        "-prf": "pre_filter",
        "-pof": "post_filter",
        # 映像/字幕/メタデータ
        "-vn": "video_disable",
        "-c:v": "video_codec",
        "-sn": "subtitle_disable",
        "-mn": "metadata_disable",
        "-cn": "chapters_disable",
        # 出力形式
        "-ofmt": "output_format",
        "-ext": "extension",
        # 実行制御
        "-d": "debug",
        "-n": "dry_run",
        "-pr": "progress",
    }

    # アノテーションと実際の使用法が乖離しているパラメータの型オーバーライド
    _TYPE_OVERRIDES: ClassVar[dict[str, type]] = {
        "audio_bitrate": str,  # 型注釈はfloatだが実際はstr("192k"等)を受け付ける
    }

    @staticmethod
    def _extract_scalar_type(hint: object) -> type | None:
        """型ヒントからスカラー型を抽出する list型の場合はNoneを返す"""
        if isinstance(hint, type) and hint in (str, float, int, bool):
            return hint
        origin = get_origin(hint)
        if origin is Literal:
            return type(get_args(hint)[0])  # type: ignore[no-any-return]
        if origin is list:
            return None
        if origin is types.UnionType:
            for arg in get_args(hint):
                if arg is not type(None):
                    return AudioNormalizePP._extract_scalar_type(arg)
        return str

    @staticmethod
    @functools.cache
    def _build_param_map() -> dict[str, tuple[str, type]]:
        """PPA引数フラグから(パラメータ名, 型)へのマッピングを自動構築する

        __init__の型アノテーションから長形式フラグと型を自動生成し、
        短縮フラグと型オーバーライドをマージする
        """
        hints = get_type_hints(FFmpegNormalize.__init__)
        param_map: dict[str, tuple[str, type]] = {}
        for param_name, hint in hints.items():
            scalar_type = AudioNormalizePP._extract_scalar_type(hint)
            if scalar_type is None:
                continue
            actual_type = AudioNormalizePP._TYPE_OVERRIDES.get(param_name, scalar_type)
            long_flag = "--" + param_name.replace("_", "-")
            param_map[long_flag] = (param_name, actual_type)
        for flag, param_name in AudioNormalizePP._SHORT_FLAGS.items():
            long_flag = "--" + param_name.replace("_", "-")
            if long_flag in param_map:
                param_map[flag] = param_map[long_flag]
        return param_map

    def run(self, information: _InfoDict) -> tuple[list[str], _InfoDict]:
        """ダウンロード済みファイルの音量を正規化する"""
        filepath = information.get("filepath")
        if filepath:
            self._normalize_file(filepath)
        return [], information

    def _build_normalize_kwargs(self) -> dict[str, Any]:
        """PPAの引数をFFmpegNormalizeのコンストラクタ引数に変換する"""
        kwargs: dict[str, Any] = {}
        args = cast("list[str]", self._configuration_args(self.pp_key()))  # type: ignore[attr-defined]
        param_map = self._build_param_map()

        i = 0
        while i < len(args):
            key = args[i]
            mapping = param_map.get(key)
            if not mapping:
                i += 1
                continue

            param_name, param_type = mapping
            if param_type is bool:
                kwargs[param_name] = True
                i += 1
            elif i + 1 < len(args):
                try:
                    kwargs[param_name] = param_type(args[i + 1])
                except ValueError:
                    self.report_warning(f"無効な引数値をスキップ: {key} {args[i + 1]}")
                i += 2
            else:
                i += 1
        return kwargs

    def _normalize_file(self, filepath: str) -> None:
        """指定されたファイルの音量を正規化する

        一時ファイルに正規化した結果を出力し、成功した場合のみ元ファイルを置換する
        """
        path = Path(filepath)
        if not path.exists():
            self.report_warning(f"ファイルが存在しません。スキップします: {filepath}")
            return

        msg = f"音量の正規化を開始します: {path.name}"
        self.to_screen(msg)  # pyright: ignore[reportCallIssue]

        try:
            fd, tmp_path = tempfile.mkstemp(suffix=path.suffix, dir=path.parent)
            os.close(fd)
        except OSError:
            self.report_warning(
                "一時ファイルの作成に失敗しました。正規化をスキップします"
            )
            return

        try:
            kwargs = self._build_normalize_kwargs()
            norm = FFmpegNormalize(**kwargs)
            norm.add_media_file(str(path), tmp_path)
            norm.run_normalization()
            shutil.move(tmp_path, str(path))
            msg = f"音量の正規化が完了しました: {path.name}"
            self.to_screen(msg)  # pyright: ignore[reportCallIssue]
        except (FFmpegNormalizeError, OSError) as e:
            self.report_warning(
                f"音量の正規化に失敗しました。元ファイルを保持します: {e}"
            )
            Path(tmp_path).unlink(missing_ok=True)


def download_video(url: str, logger: logging.Logger, yt_dlp_options: list[str]) -> None:
    """指定されたURLに対してyt-dlpでダウンロードを実行する

    Args:
        url: ダウンロード対象のURL
        logger: 状態を出力するためのロガーインスタンス
        yt_dlp_options: yt-dlpに渡すオプションリスト
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
            ydl.add_post_processor(AudioNormalizePP(), when="after_move")
            ret = ydl.download([url])
        if ret != 0:
            logger.error("ダウンロードがエラーで終了しました (code=%d)", ret)
        else:
            logger.info("ダウンロードが正常に完了しました")
    except DownloadError:
        logger.exception("ダウンロードに失敗しました")


def monitor_clipboard(yt_dlp_options: list[str]) -> NoReturn:
    """新しいURLがないかクリップボードを継続的に監視する

    定義された間隔でクリップボードの内容を確認する無限ループを実行する
    変更された内容が有効なURLである場合、ダウンロードプロセスをトリガーする

    Args:
        yt_dlp_options: yt-dlpに渡すオプションリスト
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
                    download_video(current_text, logger, yt_dlp_options)
                else:
                    logger.debug(
                        "クリップボードの変更を検知しましたが、URLではありません"
                    )

            time.sleep(POLLING_INTERVAL)

    except KeyboardInterrupt:
        logger.info("クリップボード監視を停止します")
        sys.exit(0)


if __name__ == "__main__":
    monitor_clipboard(parse_args())
