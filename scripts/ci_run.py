"""CI 入口脚本：翻译 stagent CLI 的三态 exit code。

stagent CLI 的 exit code 语义：
  0 = 全 passed
  2 = 有 expected_fail 但无 unexpected（known bug）
  1 = 有 unexpected_fail 或 error（真红线）

GitHub Actions 只认 0=绿，非 0=红。所以我们把 2 翻译成 0，
让 nightly 里有 expected_fail 但没有新回归时 job 依然绿。

直接 import 调用 cli.main，不走 subprocess——彻底绕过 CI 上
sys.path 混乱导致的 "No module named stagent.__main__" 问题。
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("用法：python scripts/ci_run.py <suite_path> [额外参数...]")
        return 2

    # 确保项目根在 sys.path 里（CI 上 checkout 可能嵌多层目录）
    ROOT = Path(__file__).resolve().parents[1]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    print(f"\n{'='*60}")
    print(f"[CI] 直接调用 stagent.cli.main  suite: {args}")
    print(f"[CI] ROOT: {ROOT}")
    print(f"{'='*60}\n")

    from stagent.cli import main as cli_main

    # cli.main 会自己解析 sys.argv，我们先临时替换
    orig_argv = sys.argv
    try:
        sys.argv = ["stagent", "run", *args]
        code = cli_main()
    finally:
        sys.argv = orig_argv

    print(f"\n[CI] raw exit code: {code}")

    if code == 0:
        print("[CI] ✅ 全 passed")
        return 0
    elif code == 2:
        print("[CI] ⚠️ 有 expected_fail（known bug），无 unexpected → 翻译为 exit 0（CI 通过）")
        return 0
    else:
        print("[CI] ❌ unexpected_fail 或 error → exit 1（CI 阻断）")
        return 1


if __name__ == "__main__":
    sys.exit(main())
