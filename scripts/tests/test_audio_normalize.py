"""audio_normalize.py のテスト"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from audio_normalize import (
    FIXED_DEFAULTS,  # noqa: F401
    build_normalize_kwargs,
    collect_files,
    probe_media,
)


class TestCollectFiles:
    """collect_files: パスリストからファイルを収集する"""

    def test_single_file_returns_list_with_that_file(self, tmp_path: Path) -> None:
        """単一ファイルパスを渡すとそのファイルのみのリストを返す"""
        f = tmp_path / "test.mp3"
        f.touch()
        result = collect_files([f])
        assert result == [f]

    def test_directory_returns_all_files_recursively(self, tmp_path: Path) -> None:
        """ディレクトリを渡すと再帰的に全ファイルを収集する"""
        sub = tmp_path / "sub"
        sub.mkdir()
        f1 = tmp_path / "a.mp3"
        f2 = sub / "b.flac"
        f1.touch()
        f2.touch()
        result = sorted(collect_files([tmp_path]))
        assert result == sorted([f1, f2])

    def test_mixed_files_and_dirs(self, tmp_path: Path) -> None:
        """ファイルとディレクトリが混在する入力を正しく収集する"""
        d = tmp_path / "dir"
        d.mkdir()
        f1 = tmp_path / "standalone.wav"
        f2 = d / "nested.aac"
        f1.touch()
        f2.touch()
        result = sorted(collect_files([f1, d]))
        assert result == sorted([f1, f2])

    def test_nonexistent_path_is_skipped(self, tmp_path: Path) -> None:
        """存在しないパスはスキップされる"""
        f = tmp_path / "exists.mp3"
        f.touch()
        result = collect_files([f, tmp_path / "nonexistent.mp3"])
        assert result == [f]

    def test_empty_input_returns_empty_list(self) -> None:
        """空リストを渡すと空リストを返す"""
        assert collect_files([]) == []

    def test_directory_excludes_subdirectories_from_results(
        self, tmp_path: Path
    ) -> None:
        """ディレクトリ自体は結果に含まれず、ファイルのみ返す"""
        sub = tmp_path / "sub"
        sub.mkdir()
        f = sub / "file.mp3"
        f.touch()
        result = collect_files([tmp_path])
        assert result == [f]


class TestProbeMedia:
    """probe_media: ffprobeでメタデータを取得する"""

    def test_returns_codec_and_sample_rate_and_bitrate(self) -> None:
        """ffprobeの出力からcodec, sample_rate, bitrateを抽出する"""
        ffprobe_output = json.dumps({
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "44100",
                    "bit_rate": "128000",
                }
            ],
            "format": {"format_name": "mp4"},
        })
        with patch("audio_normalize.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=ffprobe_output
            )
            result = probe_media(Path("test.mp4"))
        assert result == {
            "audio_codec": "aac",
            "sample_rate": 44100,
            "audio_bitrate": "128k",
        }

    def test_returns_empty_dict_on_ffprobe_failure(self) -> None:
        """ffprobeが失敗した場合は空辞書を返す"""
        with patch("audio_normalize.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError
            result = probe_media(Path("test.mp4"))
        assert result == {}

    def test_returns_empty_dict_on_no_audio_stream(self) -> None:
        """音声ストリームがない場合は空辞書を返す"""
        ffprobe_output = json.dumps({
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
            "format": {"format_name": "mp4"},
        })
        with patch("audio_normalize.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=ffprobe_output
            )
            result = probe_media(Path("test.mp4"))
        assert result == {}

    def test_codec_map_converts_opus_to_libopus(self) -> None:
        """opusコーデックはlibopusに変換される"""
        ffprobe_output = json.dumps({
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "opus",
                    "sample_rate": "48000",
                }
            ],
            "format": {"format_name": "webm"},
        })
        with patch("audio_normalize.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=ffprobe_output
            )
            result = probe_media(Path("test.webm"))
        assert result["audio_codec"] == "libopus"

    def test_missing_bitrate_excluded_from_result(self) -> None:
        """ビットレート情報がない場合はaudio_bitrateキーを含まない"""
        ffprobe_output = json.dumps({
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "flac",
                    "sample_rate": "96000",
                }
            ],
            "format": {"format_name": "flac"},
        })
        with patch("audio_normalize.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=ffprobe_output
            )
            result = probe_media(Path("test.flac"))
        assert "audio_bitrate" not in result


class TestBuildNormalizeKwargs:
    """build_normalize_kwargs: デフォルト値、probe値、CLI引数をマージする"""

    def test_no_overrides_returns_fixed_defaults(self) -> None:
        """引数なしの場合、固定デフォルト値を返す"""
        result = build_normalize_kwargs([], {})
        assert result["target_level"] == -14.0
        assert result["audio_codec"] == "aac"
        assert result["audio_bitrate"] == "128k"
        assert result["sample_rate"] == 48000
        assert result["metadata_disable"] is True
        assert result["chapters_disable"] is True
        assert result["subtitle_disable"] is True
        assert result["progress"] is True

    def test_probe_defaults_override_fixed_defaults(self) -> None:
        """probe値が固定デフォルトのcodec, bitrate, sample_rateを上書きする"""
        probe = {
            "audio_codec": "libopus",
            "audio_bitrate": "192k",
            "sample_rate": 44100,
        }
        result = build_normalize_kwargs([], probe)
        assert result["audio_codec"] == "libopus"
        assert result["audio_bitrate"] == "192k"
        assert result["sample_rate"] == 44100
        # 固定デフォルトは維持
        assert result["target_level"] == -14.0

    def test_cli_overrides_take_highest_priority(self) -> None:
        """CLI引数がprobe値と固定デフォルトの両方を上書きする"""
        probe = {"audio_codec": "libopus", "sample_rate": 44100}
        result = build_normalize_kwargs(["-c:a", "aac", "-ar", "96000"], probe)
        assert result["audio_codec"] == "aac"
        assert result["sample_rate"] == 96000

    def test_short_flags_are_recognized(self) -> None:
        """短縮フラグ(-t, -c:a, -b:a等)が正しく解析される"""
        result = build_normalize_kwargs(["-t", "-20.0", "-b:a", "256k"], {})
        assert result["target_level"] == -20.0
        assert result["audio_bitrate"] == "256k"

    def test_long_flags_are_recognized(self) -> None:
        """長形式フラグ(--target-level等)が正しく解析される"""
        result = build_normalize_kwargs(["--target-level", "-20.0"], {})
        assert result["target_level"] == -20.0

    def test_bool_flags_without_value(self) -> None:
        """boolフラグは値なしで指定するとTrueになる"""
        result = build_normalize_kwargs(["--dual-mono"], {})
        assert result["dual_mono"] is True
