"""ClipboardWatcher のテスト"""

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pyperclip

if TYPE_CHECKING:
    from collections.abc import Iterator


def _make_poll_fn(sequence: list[str]) -> MagicMock:
    """テスト用poll関数を作成する

    sequenceの値を順に返し、枯渇後は最後の値を返し続ける
    """
    it = iter(sequence)
    last = sequence[-1]
    return MagicMock(side_effect=lambda: next(it, last))


class TestClipboardWatcher:
    """ClipboardWatcher: クリップボード変更の検出"""

    def _take(self, gen: Iterator[str], n: int) -> list[str]:
        """ジェネレータからn個の値を取得する"""
        results = []
        for _, val in zip(range(n), gen):
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
