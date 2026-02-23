"""scripts配下のテスト共通設定

scripts/ ディレクトリをインポートパスに追加する
"""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/ ディレクトリをインポートパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
