"""audio_normalize.py のテスト"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from audio_normalize import (
    build_normalize_kwargs,
    collect_files,
    main,
    normalize_file,
    parse_args,
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

    def test_returns_none_on_ffprobe_failure(self) -> None:
        """ffprobeが失敗した場合はNoneを返す"""
        with patch("audio_normalize.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError
            result = probe_media(Path("test.mp4"))
        assert result is None

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

    def test_returns_none_on_invalid_json(self) -> None:
        """ffprobeの出力が不正なJSONの場合はNoneを返す"""
        with patch("audio_normalize.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="not valid json"
            )
            result = probe_media(Path("test.mp4"))
        assert result is None

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

    def test_non_numeric_bitrate_excluded_from_result(self) -> None:
        """bit_rateが'N/A'など非数値の場合はaudio_bitrateキーを含まない"""
        ffprobe_output = json.dumps({
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "opus",
                    "sample_rate": "48000",
                    "bit_rate": "N/A",
                }
            ],
            "format": {"format_name": "webm"},
        })
        with patch("audio_normalize.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=ffprobe_output
            )
            result = probe_media(Path("test.webm"))
        assert "audio_bitrate" not in result
        assert result["sample_rate"] == 48000


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

    def test_unknown_flag_is_ignored(self) -> None:
        """不明なフラグは無視され、他の引数に影響しない

        --unknown-flagが値なしフラグとしてスキップされ、
        次の-tがunknown-flagの値として消費されないことを確認する
        """
        result = build_normalize_kwargs(["--unknown-flag", "-t", "-20.0"], {})
        assert result["target_level"] == -20.0
        assert "unknown-flag" not in result

    def test_missing_value_after_flag_is_skipped(self) -> None:
        """値を要求するフラグの後に値がない場合、そのフラグはスキップされる"""
        result = build_normalize_kwargs(["-t"], {})
        # -t (target_level) の値がないため、固定デフォルトが維持される
        assert result["target_level"] == -14.0

    def test_invalid_value_type_is_skipped(self) -> None:
        """型変換に失敗する値が渡された場合、そのフラグはスキップされる"""
        result = build_normalize_kwargs(["-t", "not_a_number"], {})
        # 変換失敗のため、固定デフォルトが維持される
        assert result["target_level"] == -14.0


class TestNormalizeFile:
    """normalize_file: ファイルの音量を正規化する"""

    def test_overwrites_original_when_no_output(self, tmp_path: Path) -> None:
        """output_pathなしの場合、元ファイルを上書きする"""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"original")
        with (
            patch("audio_normalize.FFmpegNormalize") as mock_cls,
            patch("audio_normalize.shutil.move") as mock_move,
        ):
            mock_norm = MagicMock()
            mock_cls.return_value = mock_norm
            result = normalize_file(f, {"target_level": -14.0})
        assert result is True
        mock_norm.add_media_file.assert_called_once()
        mock_norm.run_normalization.assert_called_once()
        mock_move.assert_called_once()
        assert mock_move.call_args[0][1] == str(f)

    def test_writes_to_output_dir_when_specified(self, tmp_path: Path) -> None:
        """output_dir指定時、出力ディレクトリにファイルを書き出す"""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"original")
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        with patch("audio_normalize.FFmpegNormalize") as mock_cls:
            mock_norm = MagicMock()
            mock_cls.return_value = mock_norm
            result = normalize_file(f, {"target_level": -14.0}, output_dir=output_dir)
        assert result is True
        call_args = mock_norm.add_media_file.call_args[0]
        expected_output = str(output_dir / "test.mp3")
        assert call_args[1] == expected_output

    def test_returns_false_on_normalization_error(self, tmp_path: Path) -> None:
        """正規化が失敗した場合はFalseを返す"""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"original")
        with patch("audio_normalize.FFmpegNormalize") as mock_cls:
            mock_norm = MagicMock()
            mock_norm.run_normalization.side_effect = Exception("ffmpeg error")
            mock_cls.return_value = mock_norm
            result = normalize_file(f, {"target_level": -14.0})
        assert result is False

    def test_returns_false_for_nonexistent_file(self, tmp_path: Path) -> None:
        """存在しないファイルに対してはFalseを返す"""
        result = normalize_file(tmp_path / "no.mp3", {"target_level": -14.0})
        assert result is False

    def test_cleans_up_temp_file_on_error(self, tmp_path: Path) -> None:
        """上書きモードで失敗時、一時ファイルが削除される"""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"original")
        with patch("audio_normalize.FFmpegNormalize") as mock_cls:
            mock_norm = MagicMock()
            mock_norm.run_normalization.side_effect = Exception("error")
            mock_cls.return_value = mock_norm
            normalize_file(f, {"target_level": -14.0})
        # 元ファイル以外の一時ファイルが残っていないことを確認
        remaining = list(tmp_path.iterdir())
        assert remaining == [f]

    def test_returns_false_when_temp_file_creation_fails(self, tmp_path: Path) -> None:
        """一時ファイルの作成に失敗した場合はFalseを返す"""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"original")
        with patch("audio_normalize.tempfile.mkstemp", side_effect=OSError("no space")):
            result = normalize_file(f, {"target_level": -14.0})
        assert result is False


class TestParseArgs:
    """parse_args: コマンドライン引数を解析する"""

    def test_single_file_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """単一ファイルパスを解析できる"""
        monkeypatch.setattr(sys, "argv", ["audio_normalize.py", "test.mp3"])
        args = parse_args()
        assert args.paths == [Path("test.mp3")]
        assert args.output is None
        assert args.normalize_args == []

    def test_multiple_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """複数パスを解析できる"""
        monkeypatch.setattr(
            sys, "argv", ["audio_normalize.py", "a.mp3", "b/", "c.flac"]
        )
        args = parse_args()
        assert args.paths == [Path("a.mp3"), Path("b/"), Path("c.flac")]

    def test_output_option(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--outputオプションを解析できる"""
        monkeypatch.setattr(
            sys, "argv", ["audio_normalize.py", "--output", "/tmp/out", "test.mp3"]
        )
        args = parse_args()
        assert args.output == Path("/tmp/out")

    @pytest.mark.parametrize("flag", ["-o", "-O"])
    def test_output_short_options(
        self, monkeypatch: pytest.MonkeyPatch, flag: str
    ) -> None:
        """短縮オプション(-o, -O)が--outputと同じ挙動をする"""
        monkeypatch.setattr(
            sys, "argv", ["audio_normalize.py", flag, "/tmp/out", "test.mp3"]
        )
        args = parse_args()
        assert args.output == Path("/tmp/out")

    def test_normalize_args_after_separator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """-- 以降の引数がnormalize_argsに格納される"""
        monkeypatch.setattr(
            sys,
            "argv",
            ["audio_normalize.py", "test.mp3", "--", "-c:a", "aac", "-b:a", "256k"],
        )
        args = parse_args()
        assert args.paths == [Path("test.mp3")]
        assert args.normalize_args == ["-c:a", "aac", "-b:a", "256k"]


class TestMain:
    """main: メインエントリポイント"""

    def test_exits_with_error_when_no_files_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """処理対象のファイルが見つからない場合、exit code 1で終了する"""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.setattr(sys, "argv", ["audio_normalize.py", str(empty_dir)])
        with pytest.raises(SystemExit, match="1"):
            main()

    def test_exits_with_error_on_partial_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """一部ファイルが失敗した場合、exit code 1で終了する"""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"data")
        monkeypatch.setattr(sys, "argv", ["audio_normalize.py", str(f)])
        probe = {"audio_codec": "aac", "sample_rate": 48000}
        with (
            patch("audio_normalize.probe_media", return_value=probe),
            patch("audio_normalize.normalize_file", return_value=False),
            pytest.raises(SystemExit, match="1"),
        ):
            main()

    def test_succeeds_when_all_files_normalized(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """全ファイルが成功した場合、SystemExitを送出せず正常に終了する"""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"data")
        monkeypatch.setattr(sys, "argv", ["audio_normalize.py", str(f)])
        probe = {"audio_codec": "aac", "sample_rate": 48000}
        with (
            patch("audio_normalize.probe_media", return_value=probe),
            patch("audio_normalize.normalize_file", return_value=True),
        ):
            result = main()
        assert result is None

    def test_creates_output_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--output指定時、ディレクトリが自動作成される"""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"data")
        output_dir = tmp_path / "new_output"
        monkeypatch.setattr(
            sys,
            "argv",
            ["audio_normalize.py", "--output", str(output_dir), str(f)],
        )
        probe = {"audio_codec": "aac", "sample_rate": 48000}
        with (
            patch("audio_normalize.probe_media", return_value=probe),
            patch("audio_normalize.normalize_file", return_value=True),
        ):
            main()
        assert output_dir.exists()

    def test_skips_files_without_audio_stream(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """音声ストリームのないファイルはスキップされ、正常終了する"""
        f = tmp_path / "video_only.mp4"
        f.write_bytes(b"data")
        monkeypatch.setattr(sys, "argv", ["audio_normalize.py", str(f)])
        with patch("audio_normalize.probe_media", return_value={}):
            main()

    def test_probe_failure_counts_as_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """probe_mediaがNoneを返した場合、失敗としてカウントされる"""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"data")
        monkeypatch.setattr(sys, "argv", ["audio_normalize.py", str(f)])
        with (
            patch("audio_normalize.probe_media", return_value=None),
            pytest.raises(SystemExit, match="1"),
        ):
            main()
