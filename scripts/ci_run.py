"""CI 入口脚本：翻译 stagent CLI 的三态 exit code。

stagent CLI 的 exit code 语义：
  0 = 全 passed
  2 = 有 expected_fail 但无 unexpected（known bug）
  1 = 有 unexpected_fail 或 error（真红线）

GitHub Actions 只认 0=绿，非 0=红。所以我们把 2 翻译成 0，
让 nightly 里有 expected_fail 但没有新回归时 job 依然绿。
同时打印清晰的 CI 友好汇总（|exit code 2 = known-bug pass|）。
"""
from __future__ import annotations

import subprocess
import sys


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("用法：python scripts/ci_run.py <suite_path> [额外参数...]")
        return 2

    print(f"\n{'='*60}")
    print(f"[CI] 执行：python -m stagent run {' '.join(args)}")
    print(f"{'='*60}\n")

    result = subprocess.run(
        [sys.executable, "-m", "stagent", "run", *args],
        env=dict(__import__("os").environ),
    )

    code = result.returncode
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
