"""VideoDownloader のテスト"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from yt_dlp_monitor import VideoDownloader


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

    def test_resolves_default_download_dir_when_no_p_option(self) -> None:
        """オプションに-Pがない場合、デフォルトディレクトリを使用する"""
        from yt_dlp_monitor import DOWNLOAD_DIR, VideoDownloader

        logger = logging.getLogger("test")
        downloader = VideoDownloader(
            yt_dlp_options=["--ignore-config"],
            logger=logger,
        )
        assert downloader.download_dir == DOWNLOAD_DIR

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
