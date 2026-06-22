"""validate_vuo.py のテスト"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pydot
import pytest
from validate_vuo import (
    _build_node_ports,  # pyright: ignore[reportPrivateUsage]
    _detect_protocol,  # pyright: ignore[reportPrivateUsage]
    _extract_nodes,  # pyright: ignore[reportPrivateUsage]
    _has_typed,  # pyright: ignore[reportPrivateUsage]
    _parse_dot,  # pyright: ignore[reportPrivateUsage]
    _validate_edges,  # pyright: ignore[reportPrivateUsage]
    get_attr,
    logger,
    main,
    parse_label_ports,
    setup_logger,
    validate,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# 最小限の有効なVDMX Plugin形式
VDMX_PLUGIN_DOT = (
    "digraph G {\n"
    'PublishedInputs [type="vuo.in" '
    'label="PublishedInputs|<Image>Image\\r" '
    '_Image_type="VuoImage"];\n'
    'PublishedOutputs [type="vuo.out" '
    'label="PublishedOutputs|<OutputImage>OutputImage\\l" '
    '_OutputImage_type="VuoImage"];\n'
    "PublishedInputs:Image -> PublishedOutputs:OutputImage;\n"
    "}\n"
)

IMAGE_FILTER_DOT = (
    "digraph G {\n"
    'PublishedInputs [type="vuo.in" '
    'label="PublishedInputs|<image>image\\r|<time>time\\r" '
    '_image_type="VuoImage" _time_type="VuoReal"];\n'
    'PublishedOutputs [type="vuo.out" '
    'label="PublishedOutputs|<outputImage>outputImage\\l" '
    '_outputImage_type="VuoImage"];\n'
    "PublishedInputs:image -> PublishedOutputs:outputImage;\n"
    "}\n"
)

IMAGE_GENERATOR_DOT = (
    "digraph G {\n"
    'PublishedInputs [type="vuo.in" '
    'label="PublishedInputs|<width>width\\r|<height>height\\r|<time>time\\r" '
    '_width_type="VuoInteger" _height_type="VuoInteger" _time_type="VuoReal"];\n'
    'PublishedOutputs [type="vuo.out" '
    'label="PublishedOutputs|<outputImage>outputImage\\l" '
    '_outputImage_type="VuoImage"];\n'
    "PublishedInputs:width -> PublishedOutputs:outputImage;\n"
    "}\n"
)

# PublishedOutputsにポートが無い(エラーになるケース)
NO_OUTPUT_DOT = (
    "digraph G {\n"
    'PublishedInputs [type="vuo.in" '
    'label="PublishedInputs|<a>a\\r" _a_type="VuoImage"];\n'
    'PublishedOutputs [type="vuo.out" label="PublishedOutputs"];\n'
    "}\n"
)

MISSING_PUBLISHED_DOT = 'digraph G {\nNode1 [label="N1|<in>in\\l|<out>out\\r"];\n}\n'


class TestParseLabelPorts:
    """parse_label_ports: label文字列からポートID集合を抽出"""

    def test_empty_label_returns_empty_set(self) -> None:
        """空文字列を渡すと空のsetを返す"""
        assert parse_label_ports("") == set()

    def test_single_port_marker(self) -> None:
        """単一の<port>マーカーを含むlabelから1要素のsetを返す"""
        assert parse_label_ports("Display|<image>image\\l") == {"image"}

    def test_multiple_port_markers(self) -> None:
        """複数の<port>マーカーから全要素のsetを返す"""
        result = parse_label_ports("N|<a>a\\l|<b>b\\r|<c>c\\l")
        assert result == {"a", "b", "c"}

    def test_label_without_markers_returns_empty(self) -> None:
        """マーカーが無いlabelは空setを返す"""
        assert parse_label_ports("DisplayName") == set()


class TestGetAttr:
    """get_attr: pydot属性値を取得しダブルクォートを剥がす"""

    def test_returns_none_when_attr_missing(self) -> None:
        """属性が未設定ならNoneを返す"""
        n = pydot.Node("X")
        assert get_attr(n, "nonexistent") is None

    def test_strips_surrounding_quotes(self) -> None:
        """前後のダブルクォートを剥がして返す"""
        n = pydot.Node("X", label='"Hello"')
        assert get_attr(n, "label") == "Hello"

    def test_keeps_value_without_quotes(self) -> None:
        """クォートで囲まれていない値はそのまま返す"""
        n = pydot.Node("X", label="Plain")
        assert get_attr(n, "label") == "Plain"

    def test_handles_empty_quoted_string(self) -> None:
        """空のクォート文字列は空文字列として返す"""
        n = pydot.Node("X", label='""')
        assert get_attr(n, "label") == ""


class TestExtractNodes:
    """_extract_nodes: 予約名以外のノードを名前→オブジェクト辞書として返す"""

    def test_returns_user_nodes(self) -> None:
        """通常のユーザーノードは辞書に含まれる"""
        g = pydot.Dot()
        g.add_node(pydot.Node("A"))
        g.add_node(pydot.Node("B"))
        result = _extract_nodes(g)
        assert set(result.keys()) == {"A", "B"}

    def test_excludes_reserved_keys(self) -> None:
        """予約名(node/edge/graph)は除外される"""
        g = pydot.Dot()
        g.add_node(pydot.Node("Real"))
        g.add_node(pydot.Node("node"))
        g.add_node(pydot.Node("edge"))
        g.add_node(pydot.Node("graph"))
        result = _extract_nodes(g)
        assert set(result.keys()) == {"Real"}

    def test_empty_graph_returns_empty_dict(self) -> None:
        """ノードが無いグラフは空辞書を返す"""
        g = pydot.Dot()
        assert _extract_nodes(g) == {}


class TestBuildNodePorts:
    """_build_node_ports: 各ノードのポートID集合と警告を構築"""

    def test_all_nodes_with_ports_no_warnings(self) -> None:
        """全ノードがポートを持つ場合は警告無し"""
        nodes = {
            "A": pydot.Node("A", label='"N|<p1>p1\\l|<p2>p2\\r"'),
            "B": pydot.Node("B", label='"N|<x>x\\l"'),
        }
        ports, warnings = _build_node_ports(nodes)
        assert ports == {"A": {"p1", "p2"}, "B": {"x"}}
        assert warnings == []

    def test_node_without_label_generates_warning(self) -> None:
        """labelが無いノードは警告を生成しポート集合は空"""
        nodes = {"A": pydot.Node("A")}
        ports, warnings = _build_node_ports(nodes)
        assert ports == {"A": set()}
        assert len(warnings) == 1
        assert "A" in warnings[0]

    def test_node_with_label_but_no_markers_generates_warning(self) -> None:
        """labelはあるがポートマーカーが無いノードも警告"""
        nodes = {"A": pydot.Node("A", label='"PlainLabel"')}
        ports, warnings = _build_node_ports(nodes)
        assert ports == {"A": set()}
        assert len(warnings) == 1


class TestValidateEdges:
    """_validate_edges: エッジが参照するノード/ポートを検証"""

    def test_valid_edges_no_errors(self) -> None:
        """有効なエッジはエラーを生成しない"""
        edges = [pydot.Edge("A:out", "B:in")]
        node_ports = {"A": {"out"}, "B": {"in"}}
        assert _validate_edges(edges, node_ports) == []

    def test_edge_without_port_in_source(self) -> None:
        """sourceにポート指定が無いとエラー"""
        edges = [pydot.Edge("A", "B:in")]
        node_ports = {"A": {"out"}, "B": {"in"}}
        errors = _validate_edges(edges, node_ports)
        assert any("has no port" in e and "source" in e for e in errors)

    def test_edge_with_unknown_source_node(self) -> None:
        """未宣言のノードを参照するエッジはエラー"""
        edges = [pydot.Edge("Unknown:out", "B:in")]
        node_ports = {"B": {"in"}}
        errors = _validate_edges(edges, node_ports)
        assert any("unknown node 'Unknown'" in e for e in errors)

    def test_edge_with_port_not_in_node_label(self) -> None:
        """ノードのlabelに無いポートを参照するエッジはエラー"""
        edges = [pydot.Edge("A:nonexistent", "B:in")]
        node_ports = {"A": {"out"}, "B": {"in"}}
        errors = _validate_edges(edges, node_ports)
        assert any("nonexistent" in e and "not in node label" in e for e in errors)

    def test_multiple_errors_in_one_edge(self) -> None:
        """sourceとdestの両方に問題があれば両方エラー"""
        edges = [pydot.Edge("A", "B")]
        node_ports = {"A": {"out"}, "B": {"in"}}
        errors = _validate_edges(edges, node_ports)
        assert len(errors) == 2


class TestHasTyped:
    """_has_typed: ノードの _{port}_type 属性チェック"""

    def test_matches_expected_type(self) -> None:
        """属性値が期待型と一致するとTrue"""
        n = pydot.Node("X")
        n.set("_image_type", '"VuoImage"')
        assert _has_typed(n, "image", "VuoImage") is True

    def test_differs_from_expected_type(self) -> None:
        """属性値が異なるとFalse"""
        n = pydot.Node("X")
        n.set("_image_type", '"VuoReal"')
        assert _has_typed(n, "image", "VuoImage") is False

    def test_attribute_missing(self) -> None:
        """属性が未設定ならFalse"""
        n = pydot.Node("X")
        assert _has_typed(n, "image", "VuoImage") is False


class TestDetectProtocol:
    """_detect_protocol: PublishedInputs/Outputsからプロトコルを検出"""

    def test_detects_image_filter(self) -> None:
        """ImageFilter署名を持つPI/POからImageFilterを検出"""
        pi = pydot.Node(
            "PublishedInputs",
            label='"PI|<image>image\\r|<time>time\\r"',
        )
        pi.set("_image_type", '"VuoImage"')
        pi.set("_time_type", '"VuoReal"')
        po = pydot.Node(
            "PublishedOutputs",
            label='"PO|<outputImage>outputImage\\l"',
        )
        po.set("_outputImage_type", '"VuoImage"')
        proto, errors = _detect_protocol(pi, po)
        assert proto == "ImageFilter"
        assert errors == []

    def test_detects_image_generator(self) -> None:
        """ImageGenerator署名を持つPI/POからImageGeneratorを検出"""
        pi = pydot.Node(
            "PublishedInputs",
            label='"PI|<width>w\\r|<height>h\\r|<time>t\\r"',
        )
        pi.set("_width_type", '"VuoInteger"')
        pi.set("_height_type", '"VuoInteger"')
        pi.set("_time_type", '"VuoReal"')
        po = pydot.Node(
            "PublishedOutputs",
            label='"PO|<outputImage>outputImage\\l"',
        )
        po.set("_outputImage_type", '"VuoImage"')
        proto, errors = _detect_protocol(pi, po)
        assert proto == "ImageGenerator"
        assert errors == []

    def test_detects_vdmx_plugin_freeform(self) -> None:
        """プロトコル外でも出力ポートがあればVDMXPlugin扱い"""
        pi = pydot.Node("PublishedInputs", label='"PI"')
        po = pydot.Node(
            "PublishedOutputs",
            label='"PO|<custom>custom\\l"',
        )
        proto, errors = _detect_protocol(pi, po)
        assert proto == "VDMXPlugin"
        assert errors == []

    def test_returns_error_when_no_output_port(self) -> None:
        """出力ポートが無いとプロトコル検出失敗でエラー"""
        pi = pydot.Node("PublishedInputs", label='"PI"')
        po = pydot.Node("PublishedOutputs", label='"PO"')
        proto, errors = _detect_protocol(pi, po)
        assert proto is None
        assert len(errors) == 1
        assert "No protocol matched" in errors[0]


class TestParseDot:
    """_parse_dot: DOT文字列をパース"""

    def test_valid_dot_returns_graph(self) -> None:
        """有効なDOTを渡すとGraphが返る"""
        result = _parse_dot("digraph G { A -> B; }")
        assert result is not None

    def test_invalid_dot_returns_none(self) -> None:
        """構文エラーを含むDOTはNoneを返す"""
        result = _parse_dot("digraph G { A -> ; invalid }")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        """空文字列はNoneを返す"""
        result = _parse_dot("")
        assert result is None


class TestValidate:
    """validate: .vuoファイルを統合的に検証"""

    def test_valid_vdmx_plugin_returns_zero(self, tmp_path: Path) -> None:
        """有効なVDMX PluginのDOTはreturn 0"""
        f = tmp_path / "ok.vuo"
        f.write_text(VDMX_PLUGIN_DOT, encoding="utf-8")
        assert validate(f) == 0

    def test_valid_image_filter_returns_zero(self, tmp_path: Path) -> None:
        """有効なImage FilterのDOTはreturn 0"""
        f = tmp_path / "filter.vuo"
        f.write_text(IMAGE_FILTER_DOT, encoding="utf-8")
        assert validate(f) == 0

    def test_valid_image_generator_returns_zero(self, tmp_path: Path) -> None:
        """有効なImage GeneratorのDOTはreturn 0"""
        f = tmp_path / "gen.vuo"
        f.write_text(IMAGE_GENERATOR_DOT, encoding="utf-8")
        assert validate(f) == 0

    def test_invalid_dot_returns_one(self, tmp_path: Path) -> None:
        """DOT構文エラーはreturn 1"""
        f = tmp_path / "bad.vuo"
        f.write_text("digraph G { A -> ; invalid }", encoding="utf-8")
        assert validate(f) == 1

    def test_missing_published_nodes_returns_one(self, tmp_path: Path) -> None:
        """PublishedInputs/Outputsが無いとreturn 1"""
        f = tmp_path / "missing.vuo"
        f.write_text(MISSING_PUBLISHED_DOT, encoding="utf-8")
        assert validate(f) == 1

    def test_no_output_port_returns_one(self, tmp_path: Path) -> None:
        """出力ポート無しでプロトコル検出失敗するとreturn 1"""
        f = tmp_path / "noout.vuo"
        f.write_text(NO_OUTPUT_DOT, encoding="utf-8")
        assert validate(f) == 1


class TestSetupLogger:
    """setup_logger: モジュールロガー初期化"""

    @pytest.fixture(autouse=True)
    def _cleanup_logger(self) -> Iterator[None]:
        """テスト前後でモジュールロガーのハンドラを初期化する"""
        saved = logger.handlers[:]
        saved_level = logger.level
        logger.handlers.clear()
        yield
        logger.handlers.clear()
        for h in saved:
            logger.addHandler(h)
        logger.setLevel(saved_level)

    def test_adds_handler_when_none(self) -> None:
        """ハンドラ未設定時にStreamHandlerが追加される"""
        assert logger.handlers == []
        setup_logger()
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.StreamHandler)
        assert logger.level == logging.INFO

    def test_idempotent_when_handler_exists(self) -> None:
        """既にハンドラがあれば何もしない"""
        existing = logging.NullHandler()
        logger.addHandler(existing)
        setup_logger()
        assert logger.handlers == [existing]


class TestMain:
    """main: CLIエントリポイント"""

    @pytest.fixture(autouse=True)
    def _cleanup_logger(self) -> Iterator[None]:
        """テスト前後でモジュールロガーのハンドラを初期化する"""
        saved = logger.handlers[:]
        saved_level = logger.level
        logger.handlers.clear()
        yield
        logger.handlers.clear()
        for h in saved:
            logger.addHandler(h)
        logger.setLevel(saved_level)

    def test_returns_zero_for_valid_file(self, tmp_path: Path) -> None:
        """有効な.vuoを引数に渡すとreturn 0"""
        f = tmp_path / "ok.vuo"
        f.write_text(VDMX_PLUGIN_DOT, encoding="utf-8")
        assert main(["validate_vuo.py", str(f)]) == 0

    def test_returns_two_for_wrong_argc(self) -> None:
        """引数の数が違うとreturn 2"""
        assert main(["validate_vuo.py"]) == 2
        assert main(["validate_vuo.py", "a", "b"]) == 2

    def test_invalid_dot_file_returns_one(self, tmp_path: Path) -> None:
        """不正なDOTファイルを引数に渡すとreturn 1"""
        f = tmp_path / "bad.vuo"
        f.write_text("not a valid dot", encoding="utf-8")
        assert main(["validate_vuo.py", str(f)]) == 1
