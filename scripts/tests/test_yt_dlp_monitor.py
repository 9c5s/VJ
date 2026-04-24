# テストから内部APIを直接検証するため抑制
# pyright: reportPrivateUsage=false
"""yt_dlp_monitor.py のテスト

対象:
- _parse_option_list(): CLIオプションリストを辞書に変換する
- _dict_to_option_list(): オプション辞書をリスト形式に変換する
- merge_yt_dlp_options(): デフォルトオプションにCLI引数をマージする
- is_valid_url(): URLの有効性を判定する
- parse_args(): コマンドライン引数の解析
- DownloadQueue: URL用FIFOキューと重複排除
- ClipboardWatcher: クリップボード変更の検出
- VideoDownloader: yt-dlpによるダウンロード実行
- YtDlpMonitorApp: クリップボード監視からダウンロードまでの統合
- setup_logger(): アプリケーションロガーの構成
"""

import logging
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from yt_dlp_monitor import (
    YT_DLP_OPTIONS,
    DownloadQueue,
    _dict_to_option_list,
    _parse_option_list,
    ensure_download_archive,
    is_valid_url,
    merge_yt_dlp_options,
    parse_args,
    setup_logger,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from yt_dlp_monitor import VideoDownloader


def _make_poll_fn(sequence: list[str]) -> MagicMock:
    """テスト用poll関数を作成する

    sequenceの値を順に返し、枯渇後は最後の値を返し続ける
    """
    it = iter(sequence)
    last = sequence[-1]
    return MagicMock(side_effect=lambda: next(it, last))


class FakeDownloader:
    """テスト用のダウンローダー"""

    def __init__(self, *, fail_urls: set[str] | None = None) -> None:
        """失敗URLの集合を受け取り、ダウンロード記録を初期化する"""
        self.downloaded: list[str] = []
        self._fail_urls = fail_urls or set()

    def download(self, url: str) -> None:
        """URLをダウンロードする。fail_urlsに含まれる場合はRuntimeErrorを送出する"""
        if url in self._fail_urls:
            raise RuntimeError("simulated failure")
        self.downloaded.append(url)


class _FiniteWatcher:
    """テスト用の有限クリップボード監視"""

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts

    def poll_changes(self) -> Iterator[str]:
        yield from self._texts


class TestParseOptionList:
    """_parse_option_list: CLIオプションリストを辞書に変換する"""

    # --- 基本動作 ---

    def test_empty_input_returns_empty_dict(self) -> None:
        """空リストを渡すと空辞書を返す"""
        assert _parse_option_list([]) == {}

    def test_option_without_value_stored_as_none(self) -> None:
        """値を持たないフラグ型オプションはNoneとして格納される"""
        result = _parse_option_list(["--ignore-config"])
        assert result == {"--ignore-config": None}

    def test_option_with_value_stored_as_single_element_list(self) -> None:
        """値付きオプションは要素1つのリストとして格納される"""
        result = _parse_option_list(["-f", "bv+ba"])
        assert result == {"-f": ["bv+ba"]}

    def test_distinct_options_each_stored_separately(self) -> None:
        """異なるオプションはそれぞれ独立したエントリとして格納される"""
        result = _parse_option_list(["-f", "bv+ba", "-S", "res:1080"])
        assert result == {"-f": ["bv+ba"], "-S": ["res:1080"]}

    def test_consecutive_flags_all_stored_as_none(self) -> None:
        """連続するフラグは全てNoneとして格納される"""
        result = _parse_option_list(["--verbose", "--debug", "--quiet"])
        assert result == {"--verbose": None, "--debug": None, "--quiet": None}

    def test_flag_and_value_options_coexist(self) -> None:
        """フラグ型と値付きオプションが混在しても正しく解析される"""
        result = _parse_option_list(["--ignore-config", "-f", "bv+ba", "--verbose"])
        assert result == {
            "--ignore-config": None,
            "-f": ["bv+ba"],
            "--verbose": None,
        }

    def test_duplicate_key_accumulates_values_in_order(self) -> None:
        """同一キーが複数回出現すると値が出現順にリストへ蓄積される"""
        result = _parse_option_list(["--ppa", "value1", "--ppa", "value2"])
        assert result == {"--ppa": ["value1", "value2"]}

    def test_duplicate_key_preserves_all_values(self) -> None:
        """3回以上の重複でも全ての値が保持される"""
        result = _parse_option_list(["--ppa", "a", "--ppa", "b", "--ppa", "c"])
        assert result["--ppa"] == ["a", "b", "c"]

    def test_flag_at_end_of_list_stored_as_none(self) -> None:
        """リスト末尾のオプションに値がなければフラグとして扱われる"""
        result = _parse_option_list(["-f", "bv+ba", "--verbose"])
        assert result == {"-f": ["bv+ba"], "--verbose": None}

    def test_flag_followed_by_another_option_stored_as_none(self) -> None:
        """フラグの直後に別のオプションが続く場合、フラグはNoneで格納される"""
        result = _parse_option_list(["--verbose", "-f", "bv+ba"])
        assert result == {"--verbose": None, "-f": ["bv+ba"]}

    def test_non_option_value_at_head_is_skipped(self) -> None:
        """先頭がハイフンでない値はスキップされる"""
        result = _parse_option_list(["stray_value", "-f", "bv+ba"])
        assert result == {"-f": ["bv+ba"]}

    def test_multiple_non_option_values_are_skipped(self) -> None:
        """連続する非オプション値は全てスキップされる"""
        result = _parse_option_list(["stray1", "stray2", "-f", "bv+ba"])
        assert result == {"-f": ["bv+ba"]}

    def test_option_with_empty_string_value(self) -> None:
        """空文字列も値として扱われる"""
        result = _parse_option_list(["-f", ""])
        assert result == {"-f": [""]}

    def test_key_value_pairs_parsed_without_offset_drift(self) -> None:
        """連続する値付きオプションがインデックスずれなく正しくパースされる"""
        result = _parse_option_list(["-f", "bv+ba", "-o", "%(title)s"])

        assert result == {"-f": ["bv+ba"], "-o": ["%(title)s"]}

    def test_value_not_consumed_as_next_option_key(self) -> None:
        """値が次のオプションのキーとして誤消費されない"""
        result = _parse_option_list(["-S", "res:1080", "-f", "bv+ba", "-P", "/tmp"])

        assert len(result) == 3
        assert result["-S"] == ["res:1080"]
        assert result["-f"] == ["bv+ba"]
        assert result["-P"] == ["/tmp"]

    def test_complex_mixed_options_parsed_correctly(self) -> None:
        """実際のYT_DLP_OPTIONSに近い複合入力が正しく解析される"""
        options = [
            "--ignore-config",
            "-S",
            "codec:avc:aac,res:1080",
            "-f",
            "bv+ba",
            "-P",
            "/tmp/downloads",
            "--ppa",
            "Merger+ffmpeg_o1:-map_metadata -1",
            "--ppa",
            "AudioNormalize:-t -14.0",
        ]

        result = _parse_option_list(options)

        assert result == {
            "--ignore-config": None,
            "-S": ["codec:avc:aac,res:1080"],
            "-f": ["bv+ba"],
            "-P": ["/tmp/downloads"],
            "--ppa": [
                "Merger+ffmpeg_o1:-map_metadata -1",
                "AudioNormalize:-t -14.0",
            ],
        }


class TestIsValidUrl:
    """is_valid_url: URLの有効性を判定する"""

    def test_http_url_returns_true(self) -> None:
        """HTTP URLは有効と判定される"""
        assert is_valid_url("http://example.com") is True

    def test_https_url_returns_true(self) -> None:
        """HTTPS URLは有効と判定される"""
        assert is_valid_url("https://example.com/path?q=1") is True

    def test_ftp_url_returns_false(self) -> None:
        """FTPスキームは無効と判定される"""
        assert is_valid_url("ftp://example.com") is False

    def test_no_scheme_returns_false(self) -> None:
        """スキームなしのテキストは無効と判定される"""
        assert is_valid_url("example.com") is False

    def test_scheme_only_without_netloc_returns_false(self) -> None:
        """スキームのみでnetlocがない場合は無効と判定される"""
        assert is_valid_url("https://") is False

    def test_empty_string_returns_false(self) -> None:
        """空文字列は無効と判定される"""
        assert is_valid_url("") is False

    def test_plain_text_returns_false(self) -> None:
        """通常のテキストは無効と判定される"""
        assert is_valid_url("hello world") is False

    def test_value_error_from_urlparse_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """urlparseがValueErrorを送出する場合は無効と判定される"""

        def raise_value_error(_text: str) -> None:
            raise ValueError("invalid URL")

        monkeypatch.setattr("yt_dlp_monitor.urlparse", raise_value_error)
        assert is_valid_url("http://example.com") is False


class TestDictToOptionList:
    """_dict_to_option_list: オプション辞書をリスト形式に変換する"""

    def test_empty_dict_returns_empty_list(self) -> None:
        """空辞書を渡すと空リストを返す"""
        assert _dict_to_option_list({}) == []

    def test_flag_option_emits_key_only(self) -> None:
        """フラグ型オプション(None値)はキーのみ出力される"""
        assert _dict_to_option_list({"--verbose": None}) == ["--verbose"]

    def test_single_value_option_emits_key_and_value(self) -> None:
        """単一値オプションはキーと値のペアとして出力される"""
        assert _dict_to_option_list({"-f": ["bv+ba"]}) == ["-f", "bv+ba"]

    def test_multi_value_option_repeats_key(self) -> None:
        """複数値オプションはキーを繰り返して出力される"""
        result = _dict_to_option_list({"--ppa": ["a", "b"]})
        assert result == ["--ppa", "a", "--ppa", "b"]

    def test_mixed_flag_and_value_options(self) -> None:
        """フラグ型と値付きオプションが混在する辞書を正しく変換する"""
        result = _dict_to_option_list({"--verbose": None, "-f": ["bv+ba"]})
        assert result == ["--verbose", "-f", "bv+ba"]


class TestMergeYtDlpOptions:
    """merge_yt_dlp_options: デフォルトオプションにCLI引数をマージする"""

    def test_no_overrides_returns_default_options(self) -> None:
        """空リストを渡すとデフォルトオプションがそのまま返る"""
        result = merge_yt_dlp_options([])
        parsed = _parse_option_list(result)
        # デフォルトに含まれるキーが存在する
        assert "-f" in parsed
        assert parsed["-f"] == ["bv+ba"]
        assert "-S" in parsed

    def test_value_option_overrides_default(self) -> None:
        """値付きオプションでデフォルト値が上書きされる"""
        result = merge_yt_dlp_options(["-f", "bestvideo"])
        parsed = _parse_option_list(result)
        assert parsed["-f"] == ["bestvideo"]

    def test_flag_only_removes_existing_option(self) -> None:
        """値なしフラグでデフォルトに存在するオプションが削除される"""
        result = merge_yt_dlp_options(["-f"])
        parsed = _parse_option_list(result)
        assert "-f" not in parsed

    def test_new_option_appended(self) -> None:
        """デフォルトにないオプションが追加される"""
        result = merge_yt_dlp_options(["--write-sub", "en"])
        parsed = _parse_option_list(result)
        assert parsed["--write-sub"] == ["en"]

    def test_new_flag_without_value_added_to_options(self) -> None:
        """デフォルトに存在しないフラグはフラグとして追加される"""
        result = merge_yt_dlp_options(["--verbose"])
        parsed = _parse_option_list(result)
        assert parsed["--verbose"] is None


class TestYtDlpOptionsDefaults:
    """YT_DLP_OPTIONS: デフォルトオプションの内容検証"""

    def test_write_thumbnail_enabled_by_default(self) -> None:
        """サムネイル書き出しがデフォルトで有効になっている"""
        parsed = _parse_option_list(YT_DLP_OPTIONS)
        assert "--write-thumbnail" in parsed
        assert parsed["--write-thumbnail"] is None

    def test_convert_thumbnails_to_png(self) -> None:
        """サムネイル変換形式がpngに指定されている"""
        parsed = _parse_option_list(YT_DLP_OPTIONS)
        assert parsed.get("--convert-thumbnails") == ["png"]


class TestParseArgs:
    """parse_args: コマンドライン引数を解析してParsedArgsを返す"""

    def test_default_returns_normalize_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """引数なしの場合、normalize=Trueを返す"""
        monkeypatch.setattr(sys, "argv", ["yt_dlp_monitor.py"])
        result = parse_args()
        assert result.normalize is True

    def test_no_normalize_flag_returns_normalize_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-normalize指定時、normalize=Falseを返す"""
        monkeypatch.setattr(sys, "argv", ["yt_dlp_monitor.py", "--no-normalize"])
        result = parse_args()
        assert result.normalize is False

    def test_no_normalize_with_yt_dlp_args_returns_normalize_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-normalizeとyt-dlp引数併用時、normalize=Falseを返す"""
        monkeypatch.setattr(
            sys, "argv", ["yt_dlp_monitor.py", "--no-normalize", "--", "-f", "bv+ba"]
        )
        result = parse_args()
        assert result.normalize is False

    def test_no_normalize_with_yt_dlp_args_passes_yt_dlp_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-normalizeとyt-dlp引数併用時、yt-dlpオプションが正しく渡される"""
        monkeypatch.setattr(
            sys, "argv", ["yt_dlp_monitor.py", "--no-normalize", "--", "-f", "bv+ba"]
        )
        result = parse_args()
        parsed = _parse_option_list(result.yt_dlp_options)
        assert parsed["-f"] == ["bv+ba"]

    def test_yt_dlp_args_without_no_normalize_keeps_normalize_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-normalizeなしでyt-dlp引数指定時、normalize=Trueを返す"""
        monkeypatch.setattr(sys, "argv", ["yt_dlp_monitor.py", "--", "-f", "bv+ba"])
        result = parse_args()
        assert result.normalize is True

    def test_yt_dlp_args_without_no_normalize_passes_yt_dlp_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-normalizeなしでyt-dlp引数指定時、yt-dlpオプションが正しく渡される"""
        monkeypatch.setattr(sys, "argv", ["yt_dlp_monitor.py", "--", "-f", "bv+ba"])
        result = parse_args()
        parsed = _parse_option_list(result.yt_dlp_options)
        assert parsed["-f"] == ["bv+ba"]


class TestDownloadQueue:
    """DownloadQueue: URL用FIFOキューと重複排除"""

    def test_enqueue_returns_pending_count_for_new_url(self) -> None:
        """新規URLのenqueueは待機数を返す"""
        dq = DownloadQueue()
        assert dq.enqueue("https://example.com/1") == 1

    def test_enqueue_returns_incremental_pending_count(self) -> None:
        """連続enqueueが増加する待機数を返す"""
        dq = DownloadQueue()
        assert dq.enqueue("https://example.com/1") == 1
        assert dq.enqueue("https://example.com/2") == 2

    def test_enqueue_returns_none_for_duplicate_url(self) -> None:
        """既にキューにあるURLのenqueueはNoneを返す"""
        dq = DownloadQueue()
        dq.enqueue("https://example.com/1")
        assert dq.enqueue("https://example.com/1") is None

    def test_empty_queue_has_zero_pending_count(self) -> None:
        """空キューのpending_countは0を返す"""
        dq = DownloadQueue()
        assert dq.pending_count == 0

    def test_pending_count_is_one_after_single_enqueue(self) -> None:
        """1件追加後のpending_countは1を返す"""
        dq = DownloadQueue()
        dq.enqueue("https://example.com/1")
        assert dq.pending_count == 1

    def test_pending_count_is_two_after_two_enqueues(self) -> None:
        """2件追加後のpending_countは2を返す"""
        dq = DownloadQueue()
        dq.enqueue("https://example.com/1")
        dq.enqueue("https://example.com/2")
        assert dq.pending_count == 2

    def test_dequeue_returns_url_in_fifo_order(self) -> None:
        """dequeueはFIFO順でURLを返す"""
        dq = DownloadQueue()
        dq.enqueue("https://example.com/1")
        dq.enqueue("https://example.com/2")
        result = [dq.dequeue() for _ in range(2)]
        assert result == ["https://example.com/1", "https://example.com/2"]

    def test_mark_done_allows_re_enqueue(self) -> None:
        """mark_done後に同じURLを再度enqueueできる"""
        dq = DownloadQueue()
        dq.enqueue("https://example.com/1")
        url = dq.dequeue()
        dq.mark_done(url)
        assert dq.enqueue("https://example.com/1") is not None

    def test_dequeue_blocks_until_url_available(self) -> None:
        """dequeueはURLが投入されるまでブロックする"""
        dq = DownloadQueue()
        result: list[str] = []

        def consumer() -> None:
            result.append(dq.dequeue())

        t = threading.Thread(target=consumer)
        t.start()
        time.sleep(0.05)
        dq.enqueue("https://example.com/1")
        t.join(timeout=1.0)
        assert result == ["https://example.com/1"]

    def test_duplicate_not_enqueued_while_downloading(self) -> None:
        """ダウンロード中(dequeue済みmark_done未済)のURLはenqueueできない"""
        dq = DownloadQueue()
        dq.enqueue("https://example.com/1")
        dq.dequeue()  # ダウンロード中
        assert dq.enqueue("https://example.com/1") is None


class TestClipboardWatcher:
    """ClipboardWatcher: クリップボード変更の検出"""

    def _take(self, gen: Iterator[str], n: int) -> list[str]:
        """ジェネレータからn個の値を取得する"""
        results: list[str] = []
        for _, val in zip(range(n), gen, strict=False):
            results.append(val)
        return results

    def test_yields_changed_text(self) -> None:
        """テキストが変更されるとyieldされる"""
        from yt_dlp_monitor import ClipboardWatcher

        poll_fn = _make_poll_fn(["initial", "changed"])
        watcher = ClipboardWatcher(poll_fn=poll_fn, interval=0)
        gen = watcher.poll_changes()
        assert next(gen) == "changed"

    def test_does_not_yield_unchanged_text(self) -> None:
        """テキストが変更されていない場合はyieldされない"""
        from yt_dlp_monitor import ClipboardWatcher

        poll_fn = _make_poll_fn(["same", "same", "same", "different"])
        watcher = ClipboardWatcher(poll_fn=poll_fn, interval=0)
        gen = watcher.poll_changes()
        assert next(gen) == "different"

    def test_yields_multiple_changes_in_order(self) -> None:
        """複数の変更がある場合、順番にyieldされる"""
        from yt_dlp_monitor import ClipboardWatcher

        poll_fn = _make_poll_fn(["a", "b", "c"])
        watcher = ClipboardWatcher(poll_fn=poll_fn, interval=0)
        gen = watcher.poll_changes()
        assert self._take(gen, 2) == ["b", "c"]

    def test_skips_pyperclip_exception_and_continues(self) -> None:
        """PyperclipExceptionが発生してもスキップして監視を継続する"""
        import pyperclip
        from yt_dlp_monitor import ClipboardWatcher

        call_count = 0

        def flaky_poll() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "initial"
            if call_count == 2:
                raise pyperclip.PyperclipException("test error")
            return "after_error"

        watcher = ClipboardWatcher(poll_fn=flaky_poll, interval=0)
        gen = watcher.poll_changes()
        assert next(gen) == "after_error"


class TestVideoDownloader:
    """VideoDownloader: yt-dlpによるダウンロード実行"""

    def test_resolves_download_dir_from_options(self, tmp_path: Path) -> None:
        """オプションの-Pからダウンロードディレクトリを解決する"""
        from yt_dlp_monitor import VideoDownloader

        dl_dir = tmp_path / "custom"
        logger = logging.getLogger("test")
        downloader = VideoDownloader(
            yt_dlp_options=["-P", str(dl_dir)],
            logger=logger,
        )
        assert downloader.download_dir == dl_dir

    def test_resolves_default_download_dir_when_no_p_option(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """オプションに-Pがない場合、デフォルトディレクトリを使用する"""
        import yt_dlp_monitor

        fake_default = tmp_path / "yt_dlp"
        monkeypatch.setattr(yt_dlp_monitor, "DOWNLOAD_DIR", fake_default)
        logger = logging.getLogger("test")
        downloader = yt_dlp_monitor.VideoDownloader(
            yt_dlp_options=["--ignore-config"],
            logger=logger,
        )
        assert downloader.download_dir == fake_default

    def test_creates_download_directory(self, tmp_path: Path) -> None:
        """コンストラクタでダウンロードディレクトリが作成される"""
        from yt_dlp_monitor import VideoDownloader

        dl_dir = tmp_path / "new_dir" / "nested"
        logger = logging.getLogger("test")
        VideoDownloader(
            yt_dlp_options=["-P", str(dl_dir)],
            logger=logger,
        )
        assert dl_dir.exists(), f"ディレクトリが作成されていない: {dl_dir}"


class TestVideoDownloaderDownload:
    """VideoDownloader.download: yt-dlpダウンロード実行の検証"""

    def _make_downloader(
        self, tmp_path: Path, *, normalize: bool = True
    ) -> VideoDownloader:
        """テスト用のVideoDownloaderインスタンスを生成する"""
        from yt_dlp_monitor import VideoDownloader

        dl_dir = tmp_path / "dl"
        logger = logging.getLogger("test_download")
        return VideoDownloader(
            yt_dlp_options=["-P", str(dl_dir)],
            logger=logger,
            normalize=normalize,
        )

    def test_download_calls_yt_dlp_with_url(self, tmp_path: Path) -> None:
        """指定URLでyt-dlpのdownloadが呼ばれる"""
        with (
            patch("yt_dlp.parse_options") as mock_parse_options,
            patch("yt_dlp.YoutubeDL") as mock_ydl_cls,
        ):
            mock_parse_options.return_value.ydl_opts = {}
            mock_ydl = mock_ydl_cls.return_value.__enter__.return_value
            mock_ydl.download.return_value = 0

            downloader = self._make_downloader(tmp_path)
            downloader.download("https://example.com/video")

            mock_ydl.download.assert_called_once_with(["https://example.com/video"])

    def test_download_adds_audio_normalize_pp_when_normalize_is_true(
        self, tmp_path: Path
    ) -> None:
        """normalize=Trueの場合、AudioNormalizePPがpost_processorに追加される"""
        with (
            patch("yt_dlp.parse_options") as mock_parse_options,
            patch("yt_dlp.YoutubeDL") as mock_ydl_cls,
        ):
            mock_parse_options.return_value.ydl_opts = {}
            mock_ydl = mock_ydl_cls.return_value.__enter__.return_value
            mock_ydl.download.return_value = 0

            downloader = self._make_downloader(tmp_path, normalize=True)
            downloader.download("https://example.com/video")

            mock_ydl.add_post_processor.assert_called_once()

    def test_download_skips_audio_normalize_pp_when_normalize_is_false(
        self, tmp_path: Path
    ) -> None:
        """normalize=Falseの場合、add_post_processorは呼ばれない"""
        with (
            patch("yt_dlp.parse_options") as mock_parse_options,
            patch("yt_dlp.YoutubeDL") as mock_ydl_cls,
        ):
            mock_parse_options.return_value.ydl_opts = {}
            mock_ydl = mock_ydl_cls.return_value.__enter__.return_value
            mock_ydl.download.return_value = 0

            downloader = self._make_downloader(tmp_path, normalize=False)
            downloader.download("https://example.com/video")

            mock_ydl.add_post_processor.assert_not_called()

    def test_download_logs_error_on_download_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DownloadError発生時にログ出力され例外は伝播しない"""
        from yt_dlp.utils import DownloadError

        with (
            patch("yt_dlp.parse_options") as mock_parse_options,
            patch("yt_dlp.YoutubeDL") as mock_ydl_cls,
            caplog.at_level(logging.ERROR),
        ):
            mock_parse_options.return_value.ydl_opts = {}
            mock_ydl = mock_ydl_cls.return_value.__enter__.return_value
            mock_ydl.download.side_effect = DownloadError("test error")

            downloader = self._make_downloader(tmp_path)
            downloader.download("https://example.com/video")

            assert "ダウンロードに失敗しました" in caplog.text


class TestYtDlpMonitorAppWorker:
    """YtDlpMonitorApp._download_worker: キューからURLを処理する"""

    def test_processes_single_url_from_queue(self) -> None:
        """キューのURLに対してdownloader.downloadが呼ばれる"""
        from yt_dlp_monitor import YtDlpMonitorApp

        dq = DownloadQueue()
        dq.enqueue("https://example.com/1")
        fake = FakeDownloader()
        logger = logging.getLogger("test_worker")
        app = YtDlpMonitorApp(
            watcher=None,  # type: ignore[arg-type]
            downloader=fake,  # type: ignore[arg-type]
            download_queue=dq,
            logger=logger,
        )
        t = threading.Thread(target=app._download_worker, daemon=True)
        t.start()
        dq.join()
        assert fake.downloaded == ["https://example.com/1"]

    def test_processes_urls_in_fifo_order(self) -> None:
        """複数URLがFIFO順で処理される"""
        from yt_dlp_monitor import YtDlpMonitorApp

        dq = DownloadQueue()
        dq.enqueue("https://example.com/1")
        dq.enqueue("https://example.com/2")
        dq.enqueue("https://example.com/3")
        fake = FakeDownloader()
        logger = logging.getLogger("test_worker")
        app = YtDlpMonitorApp(
            watcher=None,  # type: ignore[arg-type]
            downloader=fake,  # type: ignore[arg-type]
            download_queue=dq,
            logger=logger,
        )
        t = threading.Thread(target=app._download_worker, daemon=True)
        t.start()
        dq.join()
        assert fake.downloaded == [
            "https://example.com/1",
            "https://example.com/2",
            "https://example.com/3",
        ]

    def test_marks_done_after_download(self) -> None:
        """ダウンロード後にmark_doneが呼ばれ、再enqueueできる"""
        from yt_dlp_monitor import YtDlpMonitorApp

        dq = DownloadQueue()
        dq.enqueue("https://example.com/1")
        fake = FakeDownloader()
        logger = logging.getLogger("test_worker")
        app = YtDlpMonitorApp(
            watcher=None,  # type: ignore[arg-type]
            downloader=fake,  # type: ignore[arg-type]
            download_queue=dq,
            logger=logger,
        )
        t = threading.Thread(target=app._download_worker, daemon=True)
        t.start()
        dq.join()
        assert dq.enqueue("https://example.com/1") is not None

    def test_continues_after_downloader_raises_exception(self) -> None:
        """ダウンローダーが例外を送出してもワーカーは停止せず次のURLを処理する"""
        from yt_dlp_monitor import YtDlpMonitorApp

        dq = DownloadQueue()
        dq.enqueue("https://example.com/fail")
        dq.enqueue("https://example.com/ok")
        fake = FakeDownloader(fail_urls={"https://example.com/fail"})
        logger = logging.getLogger("test_worker")
        app = YtDlpMonitorApp(
            watcher=None,  # type: ignore[arg-type]
            downloader=fake,  # type: ignore[arg-type]
            download_queue=dq,
            logger=logger,
        )
        t = threading.Thread(target=app._download_worker, daemon=True)
        t.start()
        dq.join()
        assert fake.downloaded == ["https://example.com/ok"]
        # 失敗したURLもmark_doneされ再enqueue可能
        assert dq.enqueue("https://example.com/fail") is not None


class TestYtDlpMonitorAppRun:
    """YtDlpMonitorApp.run: クリップボード監視からダウンロードまでの統合テスト"""

    def test_valid_urls_are_enqueued_and_downloaded(self) -> None:
        """有効なURLがキューに追加されダウンロードされる"""
        from yt_dlp_monitor import YtDlpMonitorApp

        dq = DownloadQueue()
        fake = FakeDownloader()
        logger = logging.getLogger("test_run")
        app = YtDlpMonitorApp(
            watcher=_FiniteWatcher(  # type: ignore[arg-type]
                [
                    "https://example.com/video1",
                    "not-a-url",
                    "https://example.com/video2",
                ]
            ),
            downloader=fake,  # type: ignore[arg-type]
            download_queue=dq,
            logger=logger,
        )
        app.run()
        dq.join()
        assert fake.downloaded == [
            "https://example.com/video1",
            "https://example.com/video2",
        ]

    def test_duplicate_urls_are_not_downloaded_twice(self) -> None:
        """重複URLは2回ダウンロードされない

        ワーカーのdownloadをEventでブロックし、run()が全URLを処理した後に
        解放することで、enqueueの重複排除が確実にテストされる
        """
        from yt_dlp_monitor import YtDlpMonitorApp

        dq = DownloadQueue()
        proceed = threading.Event()

        class BlockingDownloader(FakeDownloader):
            def download(self, url: str) -> None:
                proceed.wait()
                super().download(url)

        fake = BlockingDownloader()
        logger = logging.getLogger("test_run")
        app = YtDlpMonitorApp(
            watcher=_FiniteWatcher(  # type: ignore[arg-type]
                [
                    "https://example.com/video1",
                    "https://example.com/video1",
                ]
            ),
            downloader=fake,  # type: ignore[arg-type]
            download_queue=dq,
            logger=logger,
        )
        app.run()
        proceed.set()
        dq.join()
        assert fake.downloaded == ["https://example.com/video1"]

    def test_non_url_text_is_ignored(self) -> None:
        """URL以外のテキストはダウンロードされない"""
        from yt_dlp_monitor import YtDlpMonitorApp

        dq = DownloadQueue()
        fake = FakeDownloader()
        logger = logging.getLogger("test_run")
        app = YtDlpMonitorApp(
            watcher=_FiniteWatcher(  # type: ignore[arg-type]
                ["hello world", "ftp://not-http.com", "just text"]
            ),
            downloader=fake,  # type: ignore[arg-type]
            download_queue=dq,
            logger=logger,
        )
        app.run()
        dq.join()
        assert fake.downloaded == []


class TestSetupLogger:
    """setup_logger: アプリケーションロガーの構成"""

    @pytest.fixture(autouse=True)
    def cleanup_logger(self) -> Iterator[None]:
        """テスト後にロガーのハンドラをクリーンアップする"""
        yield
        # テスト間でロガー状態が汚染されないよう、ハンドラを全て除去する
        logger = logging.getLogger("yt_dlp_monitor")
        logger.handlers.clear()

    def test_returns_logger_instance(self) -> None:
        """初回呼び出しでLogging.Loggerインスタンスが返される"""
        result = setup_logger()
        assert isinstance(result, logging.Logger)

    def test_adds_stream_handler_on_first_call(self) -> None:
        """初回呼び出しでStreamHandlerが追加される"""
        setup_logger()
        logger = logging.getLogger("yt_dlp_monitor")
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.StreamHandler)

    def test_sets_log_level_to_info(self) -> None:
        """初回呼び出しでログレベルがINFOに設定される"""
        setup_logger()
        logger = logging.getLogger("yt_dlp_monitor")
        assert logger.level == logging.INFO

    def test_no_duplicate_handler_on_second_call(self) -> None:
        """既にハンドラがある場合はハンドラを追加せず同じロガーを返す"""
        first = setup_logger()
        second = setup_logger()
        logger = logging.getLogger("yt_dlp_monitor")
        # 二重追加防止: ハンドラは1つのままであること
        assert len(logger.handlers) == 1
        assert first is second


class TestEnsureDownloadArchive:
    """ensure_download_archive: symlink運用前提のアーカイブファイル検証"""

    _LOGGER_NAME = "test_ensure_download_archive"

    @pytest.fixture(autouse=True)
    def _cleanup_logger(self) -> Iterator[None]:
        """テスト後にテスト専用ロガーのハンドラを除去する"""
        yield
        logging.getLogger(self._LOGGER_NAME).handlers.clear()

    def _logger(self) -> logging.Logger:
        return logging.getLogger(self._LOGGER_NAME)

    def _make_symlink(self, link: Path, target: Path) -> None:
        """symlinkを作成する。作成不能な環境ではテストをスキップする"""
        try:
            link.symlink_to(target)
        except NotImplementedError, OSError:
            pytest.skip("symlinkを作成できない環境")

    def test_returns_silently_when_symlink_target_exists(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """symlinkのリンク先が存在する場合、ログ出力なしで戻る"""
        target = tmp_path / "target.txt"
        target.write_text("")
        link = tmp_path / "downloaded.txt"
        self._make_symlink(link, target)
        with caplog.at_level(logging.DEBUG, logger=self._LOGGER_NAME):
            ensure_download_archive(link, self._logger())
        assert caplog.records == []

    def test_exits_with_code_1_on_broken_symlink(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """symlinkのリンク先が存在しない場合、エラーログを出してSystemExit(1)する"""
        target = tmp_path / "missing_target.txt"
        link = tmp_path / "downloaded.txt"
        self._make_symlink(link, target)
        with (
            caplog.at_level(logging.ERROR, logger=self._LOGGER_NAME),
            pytest.raises(SystemExit) as exc_info,
        ):
            ensure_download_archive(link, self._logger())
        assert exc_info.value.code == 1
        assert "リンク先が存在しません" in caplog.text

    def test_warns_when_file_is_not_symlink(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """通常ファイル(非symlink)の場合、警告ログを出して続行する"""
        archive = tmp_path / "downloaded.txt"
        archive.write_text("")
        with caplog.at_level(logging.WARNING, logger=self._LOGGER_NAME):
            ensure_download_archive(archive, self._logger())
        assert "symlinkではありません" in caplog.text

    def test_warns_when_file_does_not_exist(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """ファイルが存在しない場合、未作成の旨の警告を出して続行する"""
        archive = tmp_path / "does_not_exist.txt"
        with caplog.at_level(logging.WARNING, logger=self._LOGGER_NAME):
            ensure_download_archive(archive, self._logger())
        assert "存在しません" in caplog.text
