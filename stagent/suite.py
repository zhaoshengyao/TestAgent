"""用例集定义与加载（支持数据参数化）。

用例文件是 YAML，最小结构：

    name: 冒烟
    base_url: https://example.com
    data:                    # 顶层变量，可被 {{var}} 或 {{data.var}} 引用
      username: standard_user
      password: secret_sauce
    cases:
      - name: 登录
        instruction: |
          用 {{username}} / {{password}} 登录
        assertions:
          - "URL 包含 /inventory.html"

变量解析规则：
- Case 级 data 覆盖 Suite 同名 data（就近优先）
- 支持点号引用：{{credentials.username}}
- 支持顶层 data 缩写：{{username}}（等价于 {{data.username}}）
- 缺失变量渲染为 ``（空字符串），不在加载阶段报错——模型会看到"空变量"指令，但不会崩溃
- 引用外部数据文件：`data_from: saucedemo_users.yaml`（路径相对用例文件），其顶层整体合并进 data
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---- 数据参数化核心 ---------------------------------------------------

_VAR_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """override 合并到 base，同名 dict 递归，其它类型 override 覆盖。"""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _lookup(data: dict[str, Any], key: str) -> Any:
    """支持 key 或 data.key 或 a.b.c 点号路径查找。"""
    segments = key.strip().split(".")
    # 允许用户写 {{data.username}} 或 {{username}}
    if segments and segments[0] == "data":
        segments = segments[1:]
    cur: Any = data
    for seg in segments:
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
    return cur


def _render(value: Any, data: dict[str, Any]) -> Any:
    """递归渲染字符串里的 {{var}} 模板。非字符串原样返回。"""
    if isinstance(value, str):
        # 允许 {{ var }} 或 {{var}}；支持管道里的默认值 {{ var | default("x") }}
        def _sub(match: re.Match[str]) -> str:
            raw = match.group(1).strip()
            # 管道默认值
            default_val = ""
            if "|" in raw:
                key, tail = raw.split("|", 1)
                key = key.strip()
                tail = tail.strip()
                m = re.match(r'default\(\s*(?:"([^"]*)"|\'([^\']*)\')?\s*\)', tail)
                if m:
                    default_val = m.group(1) if m.group(1) is not None else (m.group(2) or "")
            else:
                key = raw
            resolved = _lookup(data, key)
            if resolved is None:
                return str(default_val)
            if isinstance(resolved, (dict, list)):
                # 嵌套结构只做字符串拼接时转 YAML 紧凑形式
                return yaml.safe_dump(resolved, allow_unicode=True, default_flow_style=True).strip()
            return str(resolved)

        return _VAR_RE.sub(_sub, value)
    if isinstance(value, list):
        return [_render(v, data) for v in value]
    if isinstance(value, dict):
        return {k: _render(v, data) for k, v in value.items()}
    return value


def _load_external_data(suite_path: Path, filename: str) -> dict[str, Any]:
    """从用例文件同目录或 data/ 子目录加载外部 YAML 数据。"""
    try:
        p = (suite_path.parent / filename).resolve()
        if not p.exists():
            # 再找同级 data/ 子目录
            alt = (suite_path.parent / "data" / filename).resolve()
            if alt.exists():
                p = alt
            else:
                return {}
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            return {}
        return loaded
    except Exception:
        return {}


# ---- 数据类 -----------------------------------------------------------

@dataclass
class Case:
    name: str
    instruction: str
    url: str = ""
    assertions: list[str] = field(default_factory=list)
    timeout_s: float = 0.0
    max_steps: int = 0          # 0 表示用 config 默认值（40）；>0 时覆盖
    expected_fail: bool = False # 预期失败：该 case fail 不算红线
    expected_fail_reason: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Suite:
    name: str
    cases: list[Case]
    path: Path
    base_url: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


# ---- 加载 -------------------------------------------------------------

def load_suite(path: str | Path, only: str | None = None) -> Suite:
    suite_path = Path(path).expanduser().resolve()
    if not suite_path.exists():
        raise SystemExit(f"用例文件不存在：{suite_path}")

    raw = yaml.safe_load(suite_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"用例文件格式错误（顶层必须是映射）：{suite_path}")

    suite_name = str(raw.get("name") or suite_path.stem)
    base_url = str(raw.get("base_url") or "").strip()

    # ---- 顶层 data：先合并外部 data_from，再合并内联 data ----
    suite_data: dict[str, Any] = {}
    data_from = raw.get("data_from")
    if data_from:
        if isinstance(data_from, str):
            suite_data = _deep_merge(suite_data, _load_external_data(suite_path, data_from))
        elif isinstance(data_from, list):
            for f in data_from:
                if isinstance(f, str):
                    suite_data = _deep_merge(suite_data, _load_external_data(suite_path, f))
    inline_data = raw.get("data")
    if isinstance(inline_data, dict):
        suite_data = _deep_merge(suite_data, inline_data)

    raw_cases = raw.get("cases")
    if raw_cases is None:
        raw_cases = [raw]

    cases: list[Case] = []
    for index, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, dict):
            continue

        name = str(raw_case.get("name") or f"用例{index}")

        # ---- Case 级 data：继承 + 外部 + 内联覆盖 ----
        case_data: dict[str, Any] = dict(suite_data)
        case_data_from = raw_case.get("data_from")
        if case_data_from:
            if isinstance(case_data_from, str):
                case_data = _deep_merge(case_data, _load_external_data(suite_path, case_data_from))
            elif isinstance(case_data_from, list):
                for f in case_data_from:
                    if isinstance(f, str):
                        case_data = _deep_merge(case_data, _load_external_data(suite_path, f))
        case_inline_data = raw_case.get("data")
        if isinstance(case_inline_data, dict):
            case_data = _deep_merge(case_data, case_inline_data)

        # ---- 渲染字符串字段 ----
        # case_data 里可能含 {{xxx}} 模板（如 users.locked_out.username），
        # 渲染后存入 Case.data（selftest 和 agent 运行时都会读取）
        case_data_rendered = _render(case_data, case_data)
        base_url_rendered = str(_render(base_url, case_data_rendered) or "").strip()
        instruction = str(raw_case.get("instruction") or raw_case.get("steps") or "").strip()
        if not instruction:
            instruction = str(raw_case.get("desc") or "").strip()
        if not instruction:
            raise SystemExit(f"用例「{name}」缺少 instruction 字段")
        instruction = str(_render(instruction, case_data_rendered))

        url = str(raw_case.get("url") or base_url_rendered or "").strip()
        url = str(_render(url, case_data_rendered)).strip()

        assertions_raw = _render(_as_str_list(raw_case.get("assertions")), case_data_rendered)
        assertions = [str(a) for a in assertions_raw if str(a).strip()]

        cases.append(
            Case(
                name=name,
                instruction=instruction,
                url=url,
                assertions=assertions,
                timeout_s=float(raw_case.get("timeout_s") or 0),
                max_steps=int(raw_case.get("max_steps") or 0),
                expected_fail=bool(raw_case.get("expected_fail") or False),
                expected_fail_reason=str(raw_case.get("expected_fail_reason") or "").strip(),
                data=case_data_rendered,
            )
        )

    if only:
        keyword = only.strip()
        cases = [c for c in cases if keyword in c.name or keyword in c.instruction]
        if not cases:
            raise SystemExit(f"没有匹配「{only}」的用例")

    if not cases:
        raise SystemExit(f"用例文件里没有有效用例：{suite_path}")

    return Suite(name=suite_name, cases=cases, path=suite_path, base_url=base_url, data=suite_data)
