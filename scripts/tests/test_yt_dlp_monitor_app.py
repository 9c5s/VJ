"""YtDlpMonitorApp のテスト"""

from __future__ import annotations

import logging
import threading

from yt_dlp_monitor import DownloadQueue


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
        assert dq.enqueue("https://example.com/1") is True

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
        assert dq.enqueue("https://example.com/fail") is True
