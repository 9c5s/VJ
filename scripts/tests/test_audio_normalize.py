"""audio_normalize.py のテスト"""

from __future__ import annotations

from pathlib import Path

from audio_normalize import collect_files


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
