"""YtDlpMonitorApp のテスト"""

import logging
import threading
from typing import TYPE_CHECKING

from yt_dlp_monitor import DownloadQueue

if TYPE_CHECKING:
    from collections.abc import Iterator


class FakeDownloader:
    """テスト用のダウンローダー"""

    def __init__(self, *, fail_urls: set[str] | None = None) -> None:
        self.downloaded: list[str] = []
        self._fail_urls = fail_urls or set()

    def download(self, url: str) -> None:
        if url in self._fail_urls:
            raise RuntimeError("simulated failure")
        self.downloaded.append(url)


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


class _FiniteWatcher:
    """テスト用の有限クリップボード監視"""

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts

    def poll_changes(self) -> Iterator[str]:
        yield from self._texts


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
