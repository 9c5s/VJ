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

NO_OUTPUT_DOT = (
    "digraph G {\n"
    'PublishedInputs [type="vuo.in" '
    'label="PublishedInputs|<a>a\\r" _a_type="VuoImage"];\n'
    'PublishedOutputs [type="vuo.out" label="PublishedOutputs"];\n'
    "}\n"
)

MISSING_PUBLISHED_DOT = 'digraph G {\nNode1 [label="N1|<in>in\\l|<out>out\\r"];\n}\n'


class TestParseLabelPorts:
    """parse_label_ports: label文字列から port_id -> 方向 マップを抽出"""

    def test_empty_label_returns_empty_dict(self) -> None:
        """空文字列を渡すと空dictを返す"""
        assert parse_label_ports("") == {}

    def test_single_left_port_marker(self) -> None:
        r"""``\l`` ポートは direction 'l'"""
        assert parse_label_ports("Display|<image>image\\l") == {"image": "l"}

    def test_single_right_port_marker(self) -> None:
        r"""``\r`` ポートは direction 'r'"""
        assert parse_label_ports("Display|<out>out\\r") == {"out": "r"}

    def test_multiple_port_markers_with_directions(self) -> None:
        """複数の<port>マーカーから各方向を抽出する"""
        assert parse_label_ports("N|<a>a\\l|<b>b\\r|<c>c\\l") == {
            "a": "l",
            "b": "r",
            "c": "l",
        }

    def test_label_without_markers_returns_empty(self) -> None:
        """マーカーが無いlabelは空dictを返す"""
        assert parse_label_ports("DisplayName") == {}

    def test_marker_without_direction_is_ignored(self) -> None:
        r"""``\l`` ``\r`` が付かないマーカーは捨てる"""
        assert parse_label_ports("X|<lonely>lonely") == {}


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

    def test_excludes_quoted_reserved_keys(self) -> None:
        """ダブルクォート付きの予約名("node"等)も除外される"""
        g = pydot.Dot()
        g.add_node(pydot.Node("Real"))
        g.add_node(pydot.Node('"node"'))
        g.add_node(pydot.Node('"edge"'))
        g.add_node(pydot.Node('"graph"'))
        result = _extract_nodes(g)
        assert set(result.keys()) == {"Real"}


class TestBuildNodePorts:
    """_build_node_ports: 各ノードの port_id -> 方向 マップと警告を構築"""

    def test_all_nodes_with_ports_no_warnings(self) -> None:
        """全ノードがポートを持つ場合は警告無し"""
        nodes = {
            "A": pydot.Node("A", label='"N|<p1>p1\\l|<p2>p2\\r"'),
            "B": pydot.Node("B", label='"N|<x>x\\l"'),
        }
        ports, warnings = _build_node_ports(nodes)
        assert ports == {"A": {"p1": "l", "p2": "r"}, "B": {"x": "l"}}
        assert warnings == []

    def test_node_without_label_generates_warning(self) -> None:
        """labelが無いノードは警告を生成しポート dict は空"""
        nodes = {"A": pydot.Node("A")}
        ports, warnings = _build_node_ports(nodes)
        assert ports == {"A": {}}
        assert warnings == ["node 'A': label has no <portId> markers (''...)"]

    def test_node_with_label_but_no_markers_generates_warning(self) -> None:
        """labelはあるがポートマーカーが無いノードも警告"""
        nodes = {"A": pydot.Node("A", label='"PlainLabel"')}
        ports, warnings = _build_node_ports(nodes)
        assert ports == {"A": {}}
        assert warnings == ["node 'A': label has no <portId> markers ('PlainLabel'...)"]


class TestValidateEdges:
    """_validate_edges: エッジが参照するノード/ポート/向きを検証"""

    def test_valid_edges_no_errors(self) -> None:
        r"""``\r`` ソース -> ``\l`` デストの正方向ケーブルはエラーなし"""
        edges = [pydot.Edge("A:out", "B:in")]
        node_ports = {"A": {"out": "r"}, "B": {"in": "l"}}
        assert _validate_edges(edges, node_ports) == []

    def test_edge_without_port_in_source(self) -> None:
        """sourceにポート指定が無いとエラー"""
        edges = [pydot.Edge("A", "B:in")]
        node_ports = {"A": {"out": "r"}, "B": {"in": "l"}}
        assert _validate_edges(edges, node_ports) == [
            "edge source 'A' has no port (expected Node:port)",
        ]

    def test_edge_with_unknown_source_node(self) -> None:
        """未宣言のノードを参照するエッジはエラー"""
        edges = [pydot.Edge("Unknown:out", "B:in")]
        node_ports = {"B": {"in": "l"}}
        assert _validate_edges(edges, node_ports) == [
            "edge source references unknown node 'Unknown'",
        ]

    def test_edge_with_port_not_in_node_label(self) -> None:
        """ノードのlabelに無いポートを参照するエッジはエラー"""
        edges = [pydot.Edge("A:nonexistent", "B:in")]
        node_ports = {"A": {"out": "r"}, "B": {"in": "l"}}
        assert _validate_edges(edges, node_ports) == [
            "edge source A:'nonexistent' - port not in node label (declared: ['out'])",
        ]

    def test_multiple_errors_in_one_edge(self) -> None:
        """sourceとdestの両方に問題があれば両方エラー"""
        edges = [pydot.Edge("A", "B")]
        node_ports = {"A": {"out": "r"}, "B": {"in": "l"}}
        assert _validate_edges(edges, node_ports) == [
            "edge source 'A' has no port (expected Node:port)",
            "edge dest 'B' has no port (expected Node:port)",
        ]

    def test_reversed_cable_is_rejected(self) -> None:
        r"""``\l`` ソースは出力でないため拒否(逆向きケーブル検出)"""
        edges = [pydot.Edge("A:in", "B:out")]
        node_ports = {"A": {"in": "l", "out": "r"}, "B": {"in": "l", "out": "r"}}
        assert _validate_edges(edges, node_ports) == [
            "edge source A:'in' - expected '\\r' port but found '\\l'",
            "edge dest B:'out' - expected '\\l' port but found '\\r'",
        ]

    def test_source_using_input_port_is_rejected(self) -> None:
        r"""Source が ``\l``(入力)ポートのみエラー"""
        edges = [pydot.Edge("A:in", "B:in")]
        node_ports = {"A": {"in": "l"}, "B": {"in": "l"}}
        assert _validate_edges(edges, node_ports) == [
            "edge source A:'in' - expected '\\r' port but found '\\l'",
        ]

    def test_dest_using_output_port_is_rejected(self) -> None:
        r"""Dest が ``\r``(出力)ポートのみエラー"""
        edges = [pydot.Edge("A:out", "B:out")]
        node_ports = {"A": {"out": "r"}, "B": {"out": "r"}}
        assert _validate_edges(edges, node_ports) == [
            "edge dest B:'out' - expected '\\l' port but found '\\r'",
        ]

    def test_quoted_node_endpoint_is_normalized(self) -> None:
        """Quote 付きの ``"NodeName":port`` を分割後に正規化して照合する"""
        edges = [pydot.Edge('"Quoted Name":out', '"Quoted Name":in')]
        node_ports = {"Quoted Name": {"in": "l", "out": "r"}}
        assert _validate_edges(edges, node_ports) == []


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
        assert errors == [
            "No protocol matched and no PublishedOutputs. "
            "Required: Image Filter / Image Generator / "
            "or at least one published output for plugin",
        ]


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

    def test_rejects_trailing_text_after_closing_brace(self) -> None:
        """グラフ末尾の } 以降に非空白があれば拒否する"""
        result = _parse_dot("digraph G { A -> B; }\n<<<<<<< HEAD\nleftover")
        assert result is None

    def test_allows_only_whitespace_after_closing_brace(self) -> None:
        """末尾に空白/改行のみあれば受け入れる"""
        result = _parse_dot("digraph G { A -> B; }\n\n   \n")
        assert result is not None

    def test_rejects_trailing_brace_after_graph(self) -> None:
        """グラフの後にブレースを含むコンフリクトマーカー残骸があっても検出する"""
        result = _parse_dot("digraph G { A -> B; }\n<<<<<<< HEAD\n{ junk }")
        assert result is None

    def test_rejects_multiple_top_level_graphs(self) -> None:
        """2つ以上の top-level graph を含む入力は拒否する"""
        result = _parse_dot("digraph G { A; }\ndigraph H { B; }")
        assert result is None

    def test_rejects_undirected_graph(self) -> None:
        """無向グラフ (``graph``) は Vuo composition でないため拒否する"""
        result = _parse_dot("graph G { A -- B; }")
        assert result is None

    def test_accepts_block_comment_with_braces_in_string(self) -> None:
        r"""ブロックコメント中の ``{`` ``}`` は brace counter で無視される"""
        text = "/* { not a graph } */\ndigraph G { A -> B; }\n"
        result = _parse_dot(text)
        assert result is not None

    def test_accepts_line_comment_with_braces(self) -> None:
        r"""行コメント中の ``{`` ``}`` も brace counter で無視される"""
        text = "// { not a graph }\ndigraph G { A -> B; }\n"
        result = _parse_dot(text)
        assert result is not None

    def test_returns_none_when_pydot_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pydot.graph_from_dot_data が例外を投げた場合 None を返す"""

        def _raise(_: str) -> None:
            msg = "simulated pydot failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(pydot, "graph_from_dot_data", _raise)
        assert _parse_dot("anything") is None


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

    def test_missing_file_returns_one(self, tmp_path: Path) -> None:
        """存在しないファイルを渡すと未捕捉例外でクラッシュせず return 1"""
        nonexistent = tmp_path / "does_not_exist.vuo"
        assert validate(nonexistent) == 1

    def test_invalid_utf8_returns_one(self, tmp_path: Path) -> None:
        """UTF-8として不正なファイルは return 1(クラッシュしない)"""
        f = tmp_path / "binary.vuo"
        f.write_bytes(b"\xff\xfe\x00\x01invalid")
        assert validate(f) == 1


class _LoggerCleanup:
    """ロガー副作用のあるテストクラス用ミックスイン"""

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


class TestSetupLogger(_LoggerCleanup):
    """setup_logger: モジュールロガー初期化"""

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


class TestMain(_LoggerCleanup):
    """main: CLIエントリポイント"""

    def test_returns_zero_for_valid_file(self, tmp_path: Path) -> None:
        """有効な.vuoを引数に渡すとreturn 0"""
        f = tmp_path / "ok.vuo"
        f.write_text(VDMX_PLUGIN_DOT, encoding="utf-8")
        assert main(["validate_vuo.py", str(f)]) == 0

    @pytest.mark.parametrize(
        "argv",
        [
            ["validate_vuo.py"],
            ["validate_vuo.py", "a", "b"],
        ],
    )
    def test_returns_two_for_wrong_argc(self, argv: list[str]) -> None:
        """引数の数が違うとreturn 2"""
        assert main(argv) == 2

    def test_invalid_dot_file_returns_one(self, tmp_path: Path) -> None:
        """不正なDOTファイルを引数に渡すとreturn 1"""
        f = tmp_path / "bad.vuo"
        f.write_text("not a valid dot", encoding="utf-8")
        assert main(["validate_vuo.py", str(f)]) == 1
