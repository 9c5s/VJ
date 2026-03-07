# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "ffmpeg-normalize",
#     "yt-dlp-audio-normalize>=0.3.0",
# ]
# ///
"""音量正規化スクリプト

ファイルやフォルダに対してffmpeg-normalizeを適用する
CLI引数とドラッグ&ドロップの両方に対応する
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from ffmpeg_normalize import FFmpegNormalize
from yt_dlp_plugins.postprocessor.audio_normalize import (  # pyright: ignore[reportMissingTypeStubs]
    AudioNormalizePP,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


class ParsedArgs(NamedTuple):
    """parse_args()の戻り値"""

    paths: list[Path]
    output: Path | None
    normalize_args: list[str]


def parse_args() -> ParsedArgs:
    """コマンドライン引数を解析する

    Returns:
        パース結果を格納したParsedArgs
    """
    # -- でsys.argvを分割し、前半をargparseに渡す
    argv = sys.argv[1:]
    normalize_args: list[str] = []
    if "--" in argv:
        sep_idx = argv.index("--")
        normalize_args = argv[sep_idx + 1 :]
        argv = argv[:sep_idx]

    parser = argparse.ArgumentParser(
        description="音声/動画ファイルの音量をffmpeg-normalizeで正規化する",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="正規化するファイルまたはフォルダのパス",
    )
    parser.add_argument(
        "-o",
        "-O",
        "--output",
        type=Path,
        default=None,
        help="出力先ディレクトリ(省略時は元ファイルを上書き)",
    )
    args = parser.parse_args(argv)

    return ParsedArgs(
        paths=list(args.paths),
        output=args.output,
        normalize_args=normalize_args,
    )


def collect_files(paths: Sequence[Path]) -> list[Path]:
    """パスリストからファイルを収集する

    ファイルはそのまま、ディレクトリは再帰的に走査してファイルを収集する
    存在しないパスは警告を出力してスキップする

    Args:
        paths: ファイルまたはディレクトリのパスリスト

    Returns:
        収集されたファイルパスのリスト
    """
    files: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if not path.exists():
            logger.warning("パスが存在しません: %s", path)
            continue
        if path.is_file():
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(path)
        elif path.is_dir():
            for p in sorted(path.rglob("*")):
                if p.is_file():
                    resolved = p.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        files.append(p)
    return files


_CODEC_MAP: dict[str, str] = {
    "opus": "libopus",
    "vorbis": "libvorbis",
    "mp3": "libmp3lame",
}


def _resolve_extension(
    kwargs: dict[str, Any], filepath: Path
) -> tuple[str, dict[str, Any]]:
    """kwargsからextensionを解決し、設定済みのkwargsコピーを返す"""
    ext = kwargs.get("extension", filepath.suffix.lstrip("."))
    return ext, {**kwargs, "extension": ext}


def _extract_audio_defaults(audio_stream: dict[str, Any]) -> dict[str, Any]:
    """音声ストリームからFFmpegNormalizeのデフォルト値を抽出する

    codec_nameは_CODEC_MAPで変換し、sample_rateとbit_rateは
    数値変換を試みる 変換に失敗した場合はその項目をスキップする

    Args:
        audio_stream: ffprobeが返す音声ストリーム情報の辞書

    Returns:
        audio_codec, sample_rate, audio_bitrateを含む辞書
        各項目は取得/変換できた場合のみ含まれる
    """
    defaults: dict[str, Any] = {}

    codec = audio_stream.get("codec_name")
    if codec:
        defaults["audio_codec"] = _CODEC_MAP.get(codec, codec)

    sample_rate = audio_stream.get("sample_rate")
    if sample_rate is not None:
        try:
            defaults["sample_rate"] = int(sample_rate)
        except ValueError, TypeError:
            logger.warning("sample_rateの変換に失敗しました: %s", sample_rate)

    bit_rate = audio_stream.get("bit_rate")
    if bit_rate is not None:
        try:
            defaults["audio_bitrate"] = f"{int(bit_rate) // 1000}k"
        except ValueError, TypeError:
            logger.warning("bit_rateの変換に失敗しました: %s", bit_rate)

    return defaults


def probe_media(filepath: Path) -> dict[str, Any] | None:
    """ffprobeで入力ファイルの音声メタデータを取得する

    Args:
        filepath: 入力ファイルのパス

    Returns:
        audio_codec, sample_rate, audio_bitrateを含む辞書
        音声ストリームが存在しない場合は空辞書
        ffprobeの実行や解析に失敗した場合はNone
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(filepath),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except OSError, subprocess.TimeoutExpired:
        logger.warning("ffprobeの実行に失敗しました: %s", filepath, exc_info=True)
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("ffprobeの出力を解析できませんでした: %s", filepath)
        return None

    audio_stream = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "audio":
            audio_stream = stream
            break

    if audio_stream is None:
        logger.warning("音声ストリームが見つかりませんでした: %s", filepath)
        return {}

    return _extract_audio_defaults(audio_stream)


FIXED_DEFAULTS: dict[str, Any] = {
    "target_level": -14.0,
    "audio_codec": "aac",
    "audio_bitrate": "128k",
    "sample_rate": 48000,
    "metadata_disable": True,
    "chapters_disable": True,
    "subtitle_disable": True,
    "progress": True,
}


def build_normalize_kwargs(
    cli_args: Sequence[str],
    probe_defaults: dict[str, Any],
) -> dict[str, Any]:
    """固定デフォルト、probe値、CLI引数をマージしてFFmpegNormalize引数を構築する

    優先順位(低->高): 固定デフォルト -> probe推定値 -> CLI引数

    Args:
        cli_args: CLI引数リスト(ffmpeg-normalize形式のフラグ)
        probe_defaults: ffprobeから推定したデフォルト値

    Returns:
        FFmpegNormalizeコンストラクタに渡す引数の辞書
    """
    kwargs: dict[str, Any] = {**FIXED_DEFAULTS, **probe_defaults}

    param_map = AudioNormalizePP._build_param_map()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    args_iter = iter(cli_args)
    for key in args_iter:
        mapping = param_map.get(key)
        if not mapping:
            logger.warning("不明なフラグです: %s", key)
            continue
        param_name, param_type = mapping
        if param_type is bool:
            kwargs[param_name] = True
        else:
            try:
                value = next(args_iter)
                kwargs[param_name] = param_type(value)
            except StopIteration:
                logger.warning("引数の値がありません: %s", key)
            except (ValueError, TypeError) as e:
                logger.warning("無効な引数値です: %s (%s)", key, e)

    return kwargs


def normalize_file(
    filepath: Path,
    kwargs: dict[str, Any],
    *,
    output_dir: Path | None = None,
) -> bool:
    """ファイルの音量を正規化する

    output_dirが指定されていない場合は一時ファイル経由で元ファイルを上書きする
    output_dirが指定されている場合はそのディレクトリに出力する

    Args:
        filepath: 入力ファイルのパス
        kwargs: FFmpegNormalizeコンストラクタに渡す引数
        output_dir: 出力先ディレクトリ(省略時は元ファイルを上書き)

    Returns:
        正規化が成功した場合はTrue、失敗した場合はFalse
    """
    if not filepath.exists():
        logger.warning("ファイルが存在しません: %s", filepath)
        return False

    logger.info("正規化を開始: %s", filepath.name)

    if output_dir is not None:
        return _normalize_to_dir(filepath, kwargs, output_dir)

    return _normalize_overwrite(filepath, kwargs)


def _normalize_to_dir(
    filepath: Path,
    kwargs: dict[str, Any],
    output_dir: Path,
) -> bool:
    """出力ディレクトリに正規化結果を書き出す

    Args:
        filepath: 入力ファイルのパス
        kwargs: FFmpegNormalizeコンストラクタに渡す引数
        output_dir: 出力先ディレクトリ

    Returns:
        正規化が成功した場合はTrue、失敗した場合はFalse
    """
    ext, kwargs = _resolve_extension(kwargs, filepath)
    output_path = str(output_dir / f"{filepath.stem}.{ext}")
    try:
        norm = FFmpegNormalize(**kwargs)
        norm.add_media_file(str(filepath), output_path)
        norm.run_normalization()
        logger.info("正規化が完了しました: %s", filepath.name)
    except Exception:
        logger.exception("正規化に失敗しました: %s", filepath.name)
        return False
    return True


def _normalize_overwrite(
    filepath: Path,
    kwargs: dict[str, Any],
) -> bool:
    """一時ファイル経由で元ファイルを上書きする

    一時ファイルに正規化結果を書き出した後、元ファイルを置換する
    失敗した場合は一時ファイルを削除する

    Args:
        filepath: 入力ファイルのパス
        kwargs: FFmpegNormalizeコンストラクタに渡す引数

    Returns:
        正規化が成功した場合はTrue、失敗した場合はFalse
    """
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=filepath.suffix, dir=filepath.parent)
        os.close(fd)
    except OSError:
        logger.exception("一時ファイルの作成に失敗しました")
        return False

    ext, kwargs = _resolve_extension(kwargs, filepath)
    if ext != filepath.suffix.lstrip("."):
        logger.warning(
            "上書きモードでは拡張子の変更は反映されません: %s -> .%s",
            filepath.suffix,
            ext,
        )
    try:
        norm = FFmpegNormalize(**kwargs)
        norm.add_media_file(str(filepath), tmp_path)
        norm.run_normalization()
        shutil.move(tmp_path, str(filepath))
    except Exception:
        logger.exception("正規化に失敗しました: %s", filepath.name)
        Path(tmp_path).unlink(missing_ok=True)
        return False
    logger.info("正規化が完了しました: %s", filepath.name)
    return True


def setup_logger() -> None:
    """モジュールロガーにStreamHandlerを設定する

    ハンドラが未設定の場合のみstdoutへのStreamHandlerを追加し、
    INFOレベルで出力する 既にハンドラが設定済みの場合は何もしない
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _validate_output_dir(output: Path | None) -> None:
    """出力ディレクトリのバリデーションと作成を行う

    Args:
        output: 出力ディレクトリのパス (Noneの場合は何もしない)
    """
    if output is None:
        return
    if output.exists() and not output.is_dir():
        logger.error("--output はディレクトリを指定してください: %s", output)
        sys.exit(1)
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("出力ディレクトリを作成できませんでした: %s", output)
        sys.exit(1)


def _process_single_file(
    filepath: Path,
    normalize_args: list[str],
    output_dir: Path | None,
    seen_outputs: dict[Path, Path],
) -> bool | None:
    """単一ファイルの正規化処理を実行する

    Args:
        filepath: 入力ファイルのパス
        normalize_args: ffmpeg-normalizeに渡す追加引数
        output_dir: 出力ディレクトリ (Noneの場合は上書き)
        seen_outputs: 出力先パスの衝突検出用辞書 (副作用で更新される)

    Returns:
        True: 成功, False: 失敗, None: スキップ
    """
    probe = probe_media(filepath)
    if probe is None:
        return False
    if not probe:
        logger.info("音声ストリームが存在しません スキップします: %s", filepath.name)
        return None
    kwargs = build_normalize_kwargs(normalize_args, probe)

    if output_dir is not None:
        ext, _ = _resolve_extension(kwargs, filepath)
        output_path = (output_dir / f"{filepath.stem}.{ext}").resolve()
        if output_path in seen_outputs:
            logger.error(
                "出力先が重複しています: %s と %s -> %s",
                seen_outputs[output_path],
                filepath,
                output_path,
            )
            return False
        seen_outputs[output_path] = filepath

    return normalize_file(filepath, kwargs, output_dir=output_dir)


def main() -> None:
    """メインエントリポイント

    CLI引数を解析し、対象ファイルを収集して順次正規化を実行する
    処理対象がない場合や失敗がある場合はsys.exit(1)で終了する
    """
    setup_logger()
    args = parse_args()

    files = collect_files(args.paths)
    if not files:
        logger.error("処理対象のファイルが見つかりませんでした")
        sys.exit(1)

    _validate_output_dir(args.output)
    logger.info("処理対象: %d件", len(files))

    success = 0
    fail = 0
    # 出力先パス -> 入力元パスの対応を記録し、衝突を検出する
    seen_outputs: dict[Path, Path] = {}

    for filepath in files:
        result = _process_single_file(
            filepath, args.normalize_args, args.output, seen_outputs
        )
        if result is True:
            success += 1
        elif result is False:
            fail += 1

    logger.info("完了: 成功 %d件, 失敗 %d件", success, fail)

    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
