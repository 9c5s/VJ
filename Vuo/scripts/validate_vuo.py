#!/usr/bin/env python
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydot>=3.0"]
# ///
"""Vuoコンポジション(.vuo) DOTファイルの静的検証ツール

検証範囲:
  1. DOT構文(pydotでパース成功するか)
  2. 参照整合性(エッジで言及されるノードが宣言済みか)
  3. ポート整合性(エッジで参照されるポートIDがノードlabelで宣言されているか)
  4. プロトコル準拠(Image Filter / Image Generator の必須ポート存在)

検証範囲外:
  - ノードID(`vuo.image.blur` 等)とバージョン番号が実在するか → Vuo本体が必要
  - 型整合性(VuoImage→VuoRealへの接続など) → Vuo本体が必要

usage: uv run validate_vuo.py path/to/composition.vuo
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pydot

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

PROTOCOL_REQUIREMENTS: Final[Mapping[str, Mapping[str, list[tuple[str, str]]]]] = {
    "ImageFilter": {
        "PublishedInputs": [("image", "VuoImage"), ("time", "VuoReal")],
        "PublishedOutputs": [("outputImage", "VuoImage")],
    },
    "ImageGenerator": {
        "PublishedInputs": [
            ("width", "VuoInteger"),
            ("height", "VuoInteger"),
            ("time", "VuoReal"),
        ],
        "PublishedOutputs": [("outputImage", "VuoImage")],
    },
}

EXPECTED_ARGV_LEN: Final[int] = 2
RESERVED_NODE_KEYS: Final[frozenset[str]] = frozenset({"node", "edge", "graph"})


def parse_label_ports(label: str) -> set[str]:
    r"""ノードlabel文字列からポートID集合を抽出する

    label形式: ``DisplayName|<portId>portName\l|<otherId>otherName\r``

    Args:
        label: pydot Nodeのlabel属性値

    Returns:
        labelに含まれる ``<portId>`` マーカーの集合
    """
    return set(re.findall(r"<([^>]+)>", label))


def get_attr(obj: pydot.Common, key: str) -> str | None:
    """pydotオブジェクトの属性値を取得し前後のダブルクォートを剥がす

    Args:
        obj: pydotオブジェクト(Node/Edge/Graph)
        key: 属性名

    Returns:
        属性値の文字列、未設定ならNone
    """
    val = obj.get(key)
    if val is None:
        return None
    s = str(val)
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        return s[1:-1]
    return s


def _extract_nodes(graph: pydot.Dot) -> dict[str, pydot.Node]:
    """グラフから予約名以外のノードを名前→オブジェクトの辞書として返す"""
    extracted: dict[str, pydot.Node] = {}
    for n in graph.get_nodes():
        name = n.get_name().strip('"')
        if name in RESERVED_NODE_KEYS:
            continue
        extracted[name] = n
    return extracted


def _build_node_ports(
    nodes: dict[str, pydot.Node],
) -> tuple[dict[str, set[str]], list[str]]:
    """各ノードのlabelからポートID集合を構築し警告を集約する"""
    node_ports: dict[str, set[str]] = {}
    warnings: list[str] = []
    for name, n in nodes.items():
        label = get_attr(n, "label") or ""
        node_ports[name] = parse_label_ports(label)
        if not node_ports[name]:
            warnings.append(
                f"node {name!r}: label has no <portId> markers ({label[:60]!r}...)"
            )
    return node_ports, warnings


def _validate_edges(
    edges: list[pydot.Edge],
    node_ports: dict[str, set[str]],
) -> list[str]:
    """エッジの参照ノード/ポートが宣言済みかを検証する"""
    errors: list[str] = []
    for e in edges:
        src_full = str(e.get_source()).strip('"')
        dst_full = str(e.get_destination()).strip('"')
        for end, full in (("source", src_full), ("dest", dst_full)):
            if ":" not in full:
                errors.append(f"edge {end} {full!r} has no port (expected Node:port)")
                continue
            node_name, port = full.split(":", 1)
            if node_name not in node_ports:
                errors.append(f"edge {end} references unknown node {node_name!r}")
                continue
            if port not in node_ports[node_name]:
                errors.append(
                    f"edge {end} {node_name}:{port!r} - port not in node label "
                    f"(declared: {sorted(node_ports[node_name])})"
                )
    return errors


def _has_typed(node: pydot.Node, port: str, vtype: str) -> bool:
    """ノードの ``_{port}_type`` 属性が期待型と一致するか判定する"""
    return get_attr(node, f"_{port}_type") == vtype


def _detect_protocol(
    pi: pydot.Node,
    po: pydot.Node,
) -> tuple[str | None, list[str]]:
    """PublishedInputs/Outputsノードからプロトコルを検出する

    VDMX Vuo Pluginはプロトコル制約がなく、出力ポートが1つ以上あれば検出する

    Returns:
        (検出されたプロトコル名 or None, エラーメッセージのリスト)
    """
    pi_ports = parse_label_ports(get_attr(pi, "label") or "")
    po_ports = parse_label_ports(get_attr(po, "label") or "")

    for proto, req in PROTOCOL_REQUIREMENTS.items():
        in_ok = all(
            p in pi_ports and _has_typed(pi, p, t) for p, t in req["PublishedInputs"]
        )
        out_ok = all(
            p in po_ports and _has_typed(po, p, t) for p, t in req["PublishedOutputs"]
        )
        if in_ok and out_ok:
            logger.info("[OK]   Protocol detected: %s", proto)
            return proto, []

    if po_ports:
        logger.info(
            "[OK]   Protocol: VDMX Plugin (free-form, %d output(s))",
            len(po_ports),
        )
        return "VDMXPlugin", []

    return None, [
        "No protocol matched and no PublishedOutputs. "
        "Required: Image Filter / Image Generator / "
        "or at least one published output for plugin"
    ]


def _parse_dot(text: str) -> pydot.Dot | None:
    """DOT文字列をパースしてGraphを返す パース失敗時はNone"""
    try:
        graphs = pydot.graph_from_dot_data(text)
    except Exception:
        logger.exception("[FAIL] DOT parse error")
        return None
    if not graphs:
        logger.error("[FAIL] pydot returned no graphs")
        return None
    last_brace = text.rfind("}")
    if last_brace == -1 or text[last_brace + 1 :].strip():
        logger.error("[FAIL] unexpected trailing content after DOT graph")
        return None
    return graphs[0]


def validate(path: Path) -> int:
    """指定された.vuoファイルを検証する

    Args:
        path: .vuoファイルのパス

    Returns:
        終了コード(0=成功、1=失敗)
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        logger.exception("[FAIL] cannot read file: %s", path)
        return 1
    logger.info("== Validating: %s (%d bytes)", path, len(text))

    g = _parse_dot(text)
    if g is None:
        return 1
    logger.info("[OK]   DOT syntax parsed; graph name=%r", g.get_name())

    nodes = _extract_nodes(g)
    edges: list[pydot.Edge] = list(g.get_edges())
    logger.info("       nodes=%d, edges=%d", len(nodes), len(edges))

    node_ports, warnings = _build_node_ports(nodes)
    errors = _validate_edges(edges, node_ports)

    pi = nodes.get("PublishedInputs")
    po = nodes.get("PublishedOutputs")
    if pi is None or po is None:
        errors.append("PublishedInputs and/or PublishedOutputs node missing")
    else:
        _, proto_errors = _detect_protocol(pi, po)
        errors.extend(proto_errors)

    for w in warnings:
        logger.warning("[WARN] %s", w)
    for err in errors:
        logger.error("[FAIL] %s", err)

    if errors:
        logger.error(
            "== FAILED: %d error(s), %d warning(s)", len(errors), len(warnings)
        )
        return 1
    logger.info("== PASSED: %d warning(s)", len(warnings))
    return 0


def setup_logger() -> None:
    """モジュールロガーにstdoutへのStreamHandlerを設定する"""
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def main(argv: list[str]) -> int:
    """CLIエントリポイント"""
    setup_logger()
    if len(argv) != EXPECTED_ARGV_LEN:
        logger.error("usage: validate_vuo.py path/to/composition.vuo")
        return 2
    return validate(Path(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
