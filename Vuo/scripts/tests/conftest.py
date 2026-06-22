"""Vuo/scripts配下のテスト共通設定

Vuo/scripts/ ディレクトリをインポートパスに追加する
"""

import sys
from pathlib import Path

# Vuo/scripts/ ディレクトリをインポートパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
