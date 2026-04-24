# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "pyperclip",
#     "yt-dlp",
#     "yt-dlp-audio-normalize",
# ]
# ///
"""yt-dlp用クリップボード監視ツール

クリップボードを監視し、URLが検出された場合特定の引数でyt-dlpを実行する
"""

import argparse
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Final,
    NamedTuple,
)
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

import pyperclip
import yt_dlp
from yt_dlp.utils import DownloadError, expand_path
from yt_dlp_plugins.postprocessor.audio_normalize import (  # pyright: ignore[reportMissingImports,reportMissingTypeStubs]
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
DOWNLOAD_ARCHIVE_FILE: Final[Path] = DOWNLOAD_DIR / "downloaded.txt"
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
    "--write-thumbnail",
    "--convert-thumbnails",
    "png",
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
    `--key=value` 形式も `--key value` と等価に扱う

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
        if opt.startswith("--") and "=" in opt:
            key, _, value = opt.partition("=")
            values = result.get(key)
            if values is None:
                values = []
                result[key] = values
            values.append(value)
            i += 1
        elif opt.startswith("-"):
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
            # 既存キーは一度削除して末尾に再挿入し、overrideのトークン順を保持する
            if key in base:
                del base[key]
            base[key] = value

    return _dict_to_option_list(base)


def _resolve_archive_path(
    yt_dlp_options: list[str],
    *,
    parsed: Mapping[str, list[str] | None] | None = None,
) -> Path | None:
    """yt-dlpオプションから実効のダウンロードアーカイブパスを抽出する

    --no-download-archiveと--download-archiveが両方含まれる場合は
    yt-dlp本体のlast-wins仕様に従い最後に出現した方を採用する
    空文字列または空白のみのパスは無効として扱う

    Args:
        yt_dlp_options: yt-dlpに渡すオプションリスト
        parsed: 事前にパース済みのOptionDict 省略時は内部で再パースする

    Returns:
        --download-archiveのパス 無効化されている場合はNone
    """
    if parsed is None:
        parsed = _parse_option_list(yt_dlp_options)
    values = parsed.get("--download-archive")
    has_disable = "--no-download-archive" in parsed

    if has_disable and values:
        disable_idx = max(
            (i for i, v in enumerate(yt_dlp_options) if v == "--no-download-archive"),
            default=-1,
        )
        archive_idx = max(
            (
                i
                for i, v in enumerate(yt_dlp_options)
                if v == "--download-archive" or v.startswith("--download-archive=")
            ),
            default=-1,
        )
        if disable_idx > archive_idx:
            return None
    elif has_disable:
        return None

    if not values:
        return None
    path_str = values[-1]
    if not path_str.strip():
        return None
    # yt-dlp本体と同じ展開ロジックを適用し、~や環境変数を正規化する
    return Path(expand_path(path_str))


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


class DownloadQueue:
    """URL用FIFOキューと重複排除を提供する

    スレッドセーフなキューでURLの追加・取得を管理し、
    重複URLのキュー投入を防止する
    """

    def __init__(self) -> None:
        """インスタンスを初期化する"""
        self._queue: queue.Queue[str] = queue.Queue()
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    @property
    def pending_count(self) -> int:
        """キュー内のURL数を返す"""
        return self._queue.qsize()

    def enqueue(self, url: str) -> int | None:
        """URLをキューに追加する

        既にキュー内またはダウンロード中のURLは追加しない

        Args:
            url: 追加するURL

        Returns:
            追加できた場合はキュー内の待機数、重複の場合はNone
        """
        with self._lock:
            if url in self._seen:
                return None
            self._seen.add(url)
            self._queue.put(url)
            return self._queue.qsize()

    def dequeue(self) -> str:
        """キューからURLを取得する(ブロッキング)

        Returns:
            キューの先頭のURL
        """
        return self._queue.get()

    def mark_done(self, url: str) -> None:
        """URLのダウンロード完了を記録し、重複排除セットから除去する

        Args:
            url: ダウンロードが完了したURL
        """
        with self._lock:
            self._seen.discard(url)
        self._queue.task_done()

    def join(self) -> None:
        """全てのキュー内タスクの完了を待機する"""
        self._queue.join()


class ClipboardWatcher:
    """クリップボードを監視し、変更されたテキストを検出する

    poll_fnを定期的に呼び出し、前回と異なるテキストが検出された場合にyieldする
    """

    def __init__(
        self,
        poll_fn: Callable[[], str] = pyperclip.paste,
        interval: float = POLLING_INTERVAL,
    ) -> None:
        """インスタンスを初期化する

        Args:
            poll_fn: クリップボードの内容を返す関数
            interval: ポーリング間隔(秒)
        """
        self._poll_fn = poll_fn
        self._interval = interval

    def poll_changes(self) -> Iterator[str]:
        """クリップボードの変更を検出してyieldするジェネレータ

        起動時のクリップボード内容は無視し、変更が検出された場合のみyieldする
        PyperclipExceptionが発生した場合はスキップして監視を継続する

        Yields:
            変更後のクリップボードテキスト
        """
        try:
            last_text = self._poll_fn()
        except pyperclip.PyperclipException:
            last_text = ""

        while True:
            try:
                current_text = self._poll_fn()
            except pyperclip.PyperclipException:
                time.sleep(self._interval)
                continue

            if current_text != last_text:
                last_text = current_text
                yield current_text

            time.sleep(self._interval)


class VideoDownloader:
    """yt-dlpを使用して動画をダウンロードする

    ダウンロードディレクトリの解決・作成とyt-dlpによるダウンロード実行を担う
    """

    def __init__(
        self,
        yt_dlp_options: list[str],
        logger: logging.Logger,
        *,
        normalize: bool = True,
        parsed_options: Mapping[str, list[str] | None] | None = None,
    ) -> None:
        """インスタンスを初期化する

        yt_dlp.parse_options()はコンストラクタで一度だけ実行され、
        結果は全てのdownload()呼び出しで再利用される
        オプションの動的変更が必要な場合は新しいインスタンスを作成すること

        Args:
            yt_dlp_options: yt-dlpに渡すオプションリスト
            logger: ロガーインスタンス
            normalize: Trueの場合、ダウンロード後に音量を正規化する
            parsed_options: 事前にパース済みのOptionDict 省略時は内部で再パースする

        Raises:
            OSError: ダウンロードディレクトリの作成に失敗した場合
        """
        self._yt_dlp_options = yt_dlp_options
        self._parsed_options = (
            parsed_options
            if parsed_options is not None
            else _parse_option_list(yt_dlp_options)
        )
        self._ydl_opts = yt_dlp.parse_options(yt_dlp_options).ydl_opts
        self._logger = logger
        self._normalize = normalize
        self._download_dir = self._resolve_download_dir()
        self._download_dir.mkdir(parents=True, exist_ok=True)

    @property
    def download_dir(self) -> Path:
        """ダウンロードディレクトリを返す"""
        return self._download_dir

    def _resolve_download_dir(self) -> Path:
        """オプションからダウンロードディレクトリを解決する"""
        p_values = self._parsed_options.get("-P")
        return Path(p_values[-1] if p_values else str(DOWNLOAD_DIR))

    def download(self, url: str) -> None:
        """指定されたURLに対してyt-dlpでダウンロードを実行する

        DownloadErrorをキャッチしてログ出力する
        それ以外の例外は呼び出し元に伝播する

        Args:
            url: ダウンロード対象のURL
        """
        try:
            self._logger.info("ダウンロードを開始します: %s", url)
            self._logger.debug("yt-dlp options: %s", self._yt_dlp_options)
            with yt_dlp.YoutubeDL(self._ydl_opts) as ydl:
                if self._normalize:
                    ydl.add_post_processor(AudioNormalizePP(), when="after_move")
                ret = ydl.download([url])
            if ret != 0:  # pyright: ignore[reportUnnecessaryComparison]
                self._logger.error("ダウンロードがエラーで終了しました (code=%d)", ret)
            else:
                self._logger.info("ダウンロードが正常に完了しました")
        except DownloadError:
            self._logger.exception("ダウンロードに失敗しました")


class YtDlpMonitorApp:
    """クリップボード監視、ダウンロードキュー、ワーカーの統合管理

    ClipboardWatcher、VideoDownloader、DownloadQueueを組み合わせて
    クリップボード監視からダウンロードまでの一連の処理を実行する
    """

    def __init__(
        self,
        watcher: ClipboardWatcher,
        downloader: VideoDownloader,
        download_queue: DownloadQueue,
        logger: logging.Logger,
    ) -> None:
        """インスタンスを初期化する

        Args:
            watcher: クリップボード監視インスタンス
            downloader: ダウンロード実行インスタンス
            download_queue: URL用FIFOキュー
            logger: ロガーインスタンス
        """
        self._watcher = watcher
        self._downloader = downloader
        self._queue = download_queue
        self._logger = logger

    def _download_worker(self) -> None:
        """ワーカースレッド: キューからURLを取得してダウンロードを実行する

        daemonスレッドとして実行され、キューからURLを取り出し
        downloaderでダウンロードを実行する メインスレッド終了時に自動終了する
        """
        while True:
            url = self._queue.dequeue()
            try:
                self._downloader.download(url)
            except Exception:
                self._logger.exception("ダウンロード中に予期しないエラーが発生しました")
            finally:
                self._queue.mark_done(url)

    def _start_worker(self) -> None:
        """ワーカースレッドを起動する"""
        worker = threading.Thread(target=self._download_worker, daemon=True)
        worker.start()

    def run(self) -> None:
        """メインループを開始する

        クリップボードの変更を監視し、有効なURLを検出した場合は
        ダウンロードキューに追加する
        """
        self._logger.info("クリップボード監視を開始します")
        self._logger.info("停止するには Ctrl+C を押してください")
        self._start_worker()

        try:
            for text in self._watcher.poll_changes():
                if is_valid_url(text):
                    pending = self._queue.enqueue(text)
                    if pending is not None:
                        self._logger.info(
                            "キューに追加しました: %s (待機中: %d件)",
                            text,
                            pending,
                        )
                    else:
                        self._logger.info("既にキューに存在します: %s", text)
                else:
                    self._logger.debug(
                        "クリップボードの変更を検知しましたが、URLではありません"
                    )

        except KeyboardInterrupt:
            self._logger.info("クリップボード監視を停止します")
            sys.exit(0)


def setup_logger() -> logging.Logger:
    """アプリケーションロガーを構成して返す"""
    logger = logging.getLogger(__name__)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def _validate_symlink_archive(archive_file: Path, logger: logging.Logger) -> None:
    """symlinkアーカイブの状態を検証する

    異常時は`sys.exit(1)`を送出し、正常時は呼び出し元へ戻る

    Args:
        archive_file: symlinkとして存在するアーカイブファイルのパス
        logger: ロガーインスタンス
    """
    if archive_file.is_file():
        if not os.access(archive_file, os.W_OK):
            logger.error(
                "アーカイブsymlinkのリンク先に書き込み権限がありません: %s",
                archive_file,
            )
            sys.exit(1)
        return
    if archive_file.exists():
        logger.error(
            "アーカイブsymlinkのリンク先がファイルではありません: %s",
            archive_file,
        )
        sys.exit(1)
    # リンク先が未作成でも親ディレクトリが書き込み可能ならyt-dlp実行時に作成できる
    target = archive_file.readlink()
    if not target.is_absolute():
        target = archive_file.parent / target
    target_parent = target.parent
    if not target_parent.is_dir():
        logger.error(
            "アーカイブsymlinkのリンク先の親ディレクトリが存在しません: %s -> %s",
            archive_file,
            target,
        )
        sys.exit(1)
    if not os.access(target_parent, os.W_OK):
        logger.error(
            "アーカイブsymlinkのリンク先の親ディレクトリに書き込み権限がありません:"
            " %s -> %s",
            archive_file,
            target,
        )
        sys.exit(1)
    logger.warning(
        "アーカイブsymlinkのリンク先が未作成です: %s -> %s (実行時に作成されます)",
        archive_file,
        target,
    )


def ensure_download_archive(archive_file: Path, logger: logging.Logger) -> None:
    """ダウンロードアーカイブファイル(symlink運用前提)の状態を確認する

    symlinkとしてリンク先が存在する場合のみ正常と見なす
    リンク切れはyt-dlp書き込み時に必ず失敗するため、起動時点で中止する
    symlinkでない実ファイルやファイル未作成の場合は警告のみ出力して続行する
    親ディレクトリが存在しない、または書き込み権限がない場合は中止する

    Args:
        archive_file: ダウンロードアーカイブファイルのパス
        logger: ロガーインスタンス
    """
    if archive_file.is_symlink():
        _validate_symlink_archive(archive_file, logger)
        return

    parent = archive_file.parent
    if not parent.is_dir():
        logger.error(
            "アーカイブファイルの親ディレクトリが存在しません: %s",
            parent,
        )
        sys.exit(1)

    if archive_file.exists():
        if not archive_file.is_file():
            logger.error("アーカイブパスがファイルではありません: %s", archive_file)
            sys.exit(1)
        if not os.access(archive_file, os.W_OK):
            logger.error(
                "アーカイブファイルに書き込み権限がありません: %s",
                archive_file,
            )
            sys.exit(1)
        logger.warning("アーカイブファイルがsymlinkではありません: %s", archive_file)
        return

    # ファイル未作成時のみ新規作成のためのparent書き込み権限を要求する
    if not os.access(parent, os.W_OK):
        logger.error(
            "アーカイブファイルの親ディレクトリに書き込み権限がありません: %s",
            parent,
        )
        sys.exit(1)

    logger.warning(
        "アーカイブファイルが存在しません: %s (実行時に通常ファイルとして作成されます)",
        archive_file,
    )


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


if __name__ == "__main__":
    parsed = parse_args()
    logger = setup_logger()
    parsed_options = _parse_option_list(parsed.yt_dlp_options)
    archive_path = _resolve_archive_path(parsed.yt_dlp_options, parsed=parsed_options)
    watcher = ClipboardWatcher()
    downloader = VideoDownloader(
        parsed.yt_dlp_options,
        logger,
        normalize=parsed.normalize,
        parsed_options=parsed_options,
    )
    if archive_path is not None:
        if archive_path == DOWNLOAD_ARCHIVE_FILE:
            # -P変更時にVideoDownloaderがDOWNLOAD_DIRを作らない場合に備え事前作成
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        ensure_download_archive(archive_path, logger)
    app = YtDlpMonitorApp(
        watcher=watcher,
        downloader=downloader,
        download_queue=DownloadQueue(),
        logger=logger,
    )
    app.run()
