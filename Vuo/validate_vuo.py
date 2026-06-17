#!/usr/bin/env python
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydot>=3.0"]
# ///
"""Vuoコンポジション(.vuo) DOTファイルの静的検証ツール.

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

import re
import sys
from pathlib import Path

import pydot

PROTOCOL_REQUIREMENTS = {
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

# VDMX Vuo Plugin: プロトコル制約なし。任意のpublished port構成可能。
# 検出条件は「Image Filter/Generatorに当てはまらないが、出力ポートが1つ以上ある」。


def parse_label_ports(label: str) -> set[str]:
    """ノードlabel文字列からポートID集合を抽出する.

    label形式: "DisplayName|<portId>portName\\l|<otherId>otherName\\r"
    """
    return set(re.findall(r"<([^>]+)>", label))


def parse_published_ports(label: str) -> list[tuple[str, str]]:
    """PublishedInputs/Outputsのlabelから (portId, direction) のリストを返す.

    direction: 'r' (出力ポート/Inputsの右端), 'l' (入力ポート/Outputsの左端)
    """
    pattern = re.compile(r"<([^>]+)>[^|]+?\\([lr])")
    return [(pid, d) for pid, d in pattern.findall(label)]


def get_attr(obj, key: str) -> str | None:
    """pydotの属性取得.値の前後ダブルクォートを剥がす."""
    val = obj.get(key)
    if val is None:
        return None
    s = str(val)
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        s = s[1:-1]
    return s


def validate(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    print(f"== Validating: {path} ({len(text)} bytes)")

    try:
        graphs = pydot.graph_from_dot_data(text)
    except Exception as e:
        print(f"[FAIL] DOT parse error: {e}")
        return 1
    if not graphs:
        print("[FAIL] pydot returned no graphs")
        return 1
    g = graphs[0]
    print(f"[OK]   DOT syntax parsed; graph name={g.get_name()!r}")

    nodes = {n.get_name().strip('"'): n for n in g.get_nodes() if n.get_name() not in {"node", "edge", "graph"}}
    edges = g.get_edges()
    print(f"       nodes={len(nodes)}, edges={len(edges)}")

    errors: list[str] = []
    warnings: list[str] = []

    # --- ノード一覧と各ノードのポートID集合 ---
    node_ports: dict[str, set[str]] = {}
    for name, n in nodes.items():
        label = get_attr(n, "label") or ""
        node_ports[name] = parse_label_ports(label)
        if not node_ports[name]:
            warnings.append(f"node {name!r}: label has no <portId> markers ({label[:60]!r}...)")

    # --- エッジが参照するノード/ポートを検証 ---
    for e in edges:
        src_full = e.get_source().strip('"')
        dst_full = e.get_destination().strip('"')
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

    # --- プロトコル準拠 ---
    detected_protocol = None
    pi = nodes.get("PublishedInputs")
    po = nodes.get("PublishedOutputs")
    if pi is None or po is None:
        errors.append("PublishedInputs and/or PublishedOutputs node missing")
    else:
        pi_ports = parse_label_ports(get_attr(pi, "label") or "")
        po_ports = parse_label_ports(get_attr(po, "label") or "")

        def has_typed(node, port, vtype):
            return get_attr(node, f"_{port}_type") == vtype

        for proto, req in PROTOCOL_REQUIREMENTS.items():
            in_ok = all(p in pi_ports and has_typed(pi, p, t) for p, t in req["PublishedInputs"])
            out_ok = all(p in po_ports and has_typed(po, p, t) for p, t in req["PublishedOutputs"])
            if in_ok and out_ok:
                detected_protocol = proto
                break

        if detected_protocol:
            print(f"[OK]   Protocol detected: {detected_protocol}")
        elif po_ports:
            # VDMX Vuo Plugin: プロトコル不要、自由port構成
            detected_protocol = "VDMXPlugin"
            print(f"[OK]   Protocol: VDMX Plugin (free-form, {len(po_ports)} output(s))")
        else:
            errors.append(
                "No protocol matched and no PublishedOutputs. Required: "
                "Image Filter / Image Generator / or at least one published output for plugin"
            )

    # --- 結果 ---
    for w in warnings:
        print(f"[WARN] {w}")
    for e in errors:
        print(f"[FAIL] {e}")

    if errors:
        print(f"\n== FAILED: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"\n== PASSED: {len(warnings)} warning(s)")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_vuo.py path/to/composition.vuo", file=sys.stderr)
        return 2
    return validate(Path(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
