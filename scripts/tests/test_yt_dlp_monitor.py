"""yt_dlp_monitor.py のテスト

対象:
  - _parse_option_list(): CLIオプションリストを辞書に変換する
  - _dict_to_option_list(): オプション辞書をリスト形式に変換する
  - merge_yt_dlp_options(): デフォルトオプションにCLI引数をマージする
  - is_valid_url(): URLの有効性を判定する
  - parse_args(): コマンドライン引数の解析
  - DownloadQueue: URL用FIFOキューと重複排除
"""

from __future__ import annotations

import sys
import threading
import time

import pytest
from yt_dlp_monitor import (
    DownloadQueue,
    _dict_to_option_list,
    _parse_option_list,
    is_valid_url,
    merge_yt_dlp_options,
    parse_args,
)

# === _parse_option_list ===


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

    # --- 複数オプション ---

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

    # --- 重複キーの蓄積(変更箇所: get+Noneチェック) ---

    def test_duplicate_key_accumulates_values_in_order(self) -> None:
        """同一キーが複数回出現すると値が出現順にリストへ蓄積される"""
        result = _parse_option_list(["--ppa", "value1", "--ppa", "value2"])
        assert result == {"--ppa": ["value1", "value2"]}

    def test_duplicate_key_preserves_all_values(self) -> None:
        """3回以上の重複でも全ての値が保持される"""
        result = _parse_option_list(["--ppa", "a", "--ppa", "b", "--ppa", "c"])
        assert result["--ppa"] == ["a", "b", "c"]

    # --- 境界値・エッジケース ---

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

    # --- i+=2の正確性(M3ミューテーション対策) ---

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

    # --- 統合テスト ---

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


# === is_valid_url ===


class TestIsValidUrl:
    """is_valid_url: URLの有効性を判定する"""

    # --- 正常系 ---

    def test_http_url_returns_true(self) -> None:
        """HTTP URLは有効と判定される"""
        assert is_valid_url("http://example.com") is True

    def test_https_url_returns_true(self) -> None:
        """HTTPS URLは有効と判定される"""
        assert is_valid_url("https://example.com/path?q=1") is True

    # --- 異常系: scheme ---

    def test_ftp_url_returns_false(self) -> None:
        """FTPスキームは無効と判定される"""
        assert is_valid_url("ftp://example.com") is False

    def test_no_scheme_returns_false(self) -> None:
        """スキームなしのテキストは無効と判定される"""
        assert is_valid_url("example.com") is False

    # --- 異常系: netloc ---

    def test_scheme_only_without_netloc_returns_false(self) -> None:
        """スキームのみでnetlocがない場合は無効と判定される"""
        assert is_valid_url("https://") is False

    # --- 境界値 ---

    def test_empty_string_returns_false(self) -> None:
        """空文字列は無効と判定される"""
        assert is_valid_url("") is False

    def test_plain_text_returns_false(self) -> None:
        """通常のテキストは無効と判定される"""
        assert is_valid_url("hello world") is False

    # --- 例外処理 ---

    def test_value_error_from_urlparse_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """urlparseがValueErrorを送出する場合は無効と判定される"""

        def raise_value_error(_text: str) -> None:
            raise ValueError("invalid URL")

        monkeypatch.setattr("yt_dlp_monitor.urlparse", raise_value_error)
        assert is_valid_url("http://example.com") is False


# === _dict_to_option_list ===


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


# === merge_yt_dlp_options ===


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


# === parse_args ===


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


# === DownloadQueue ===


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
