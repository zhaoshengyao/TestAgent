# 排查报告：DSH v0.2.1-rc.2 — "format v4 message requires a producer-owned source kind"

## 🎯 根因定位

**触发者：`mnemon_subagent_result` 工具（Mnemon Memory 插件）**

## 证据

### 1. 错误堆栈（来自 `C:\Users\16509\AppData\Roaming\DSH Desktop\logs\host\dsh-2026-11-12.error.log` 第50行）

```json
{
  "source": "host.tools",
  "event": "execute.failed",
  "tool": "mnemon_subagent_result",
  "sessionId": "session-5dc078f6-9855-4b78-b2c5-7f46299f7ffc",
  "error": {
    "name": "SessionFormatError",
    "message": "format v4 message requires a producer-owned source kind",
    "stack": [
      "at source (...dsh-session-format-v3-to-v4/lib/index.js:126)",
      "at assertV4SourceRowAdmission (...dsh-session-format-v3-to-v4/lib/index.js:150)",
      "at assertV4RowAdmission (...dsh-session-format-v3-to-v4/lib/index.js:1112)",
      "at Object.encodeEvent (...dsh-session-format-v3-to-v4/lib/index.js:1097)",
      "at eventLine (...dsh-session-persistence-jsonl/lib/index.js:960)",
      "at Proxy.encodeEventBatch (...dsh-session-persistence-jsonl/lib/index.js:3280)"
    ]
  }
}
```

### 2. 所有受影响的 session

| Session ID | 首次触发时间 | 最后触发时间 |
|---|---|---|
| `session-8d44e635-d38f-4dcf-b676-b1cebe5bb9d6` | 11-09 15:43 | 11-09 16:48 |
| `session-5dc078f6-9855-4b78-b2c5-7f46299f7ffc` | 11-12 15:08 | 11-12 (多次) |

### 3. 错误产生机制

```
mnemon_subagent_result 工具执行
  → 返回 tool/result event，写入 session log
  → session-projection-cache 尝试持久化
  → dsh-session-persistence-jsonl 调用 encodeEventBatch()
  → dsh-session-format-v3-to-v4 的 assertV4SourceRowAdmission() 验证消息 source.kind
  → 验证失败：消息的 source.kind 不是 producer-owned 类型
  → 抛出 SessionFormatError
```

### 4. "producer-owned source kind" 是什么

从 DSH 源码（`@deepseek-ai/dsh-llm` 的 `message.ts`）看，v4 格式要求每条消息的 `source.kind` 必须是以下 producer 声明的类型之一：

| kind | 生产者 | 角色 |
|---|---|---|
| `'model'` | LLM adapter | assistant 消息 |
| `'tool'` | Tool executor | tool/result 消息 |
| `'system-prompt'` | system-prompt 插件 | system 消息 |
| `'user'` | user message producer | user 消息 |

v3→v4 的迁移层（`dsh-session-format-v3-to-v4`）在写入 JSONL 持久化文件时做严格验证。`mnemon_subagent_result` 返回的 tool/result 消息中，其 message.source.kind 很可能**缺少 `'tool'`** 或者有一个 v4 不认识的自定义 kind。

## 根本原因

**Mnemon Memory 插件（Mnemon Runtime Protocol）的 `mnemon_subagent_result` 工具在构建 tool result message 时，没有正确填充 v4 format 要求的 producer-owned source.kind 字段。** 这是 Mnemon 插件与 DSH v0.2.1-rc.2 的 v4 session format 之间的兼容性 bug。

## 临时解决方案

### 方案 A：删除损坏的 session 文件（推荐立即执行）

1. 找到 DSH session 持久化目录。根据代码推断路径可能是：
   ```
   %USERPROFILE%\.dsh\... 或 APPDATA\Deeptrain\...
   ```
2. 找到对应的 session 文件（名字类似 `session-8d44e635-d38f-4dcf-b676-b1cebe5bb9d6` 或 `session-5dc078f6-...`）
3. 备份后删除
4. 重启 DSH Desktop

### 方案 B：禁用 mnemon 插件

在 DSH Desktop 设置 → 插件中，找到 Mnemon Memory 相关插件，先禁用它们验证问题是否消失。

## 长期修复

1. **升级 DSH**：npm 上已有 0.2.2-rc.x 版本，小版本升级很可能修复了这个兼容性问题
2. **等待 Mnemon 插件更新**：向 Mnemon 插件作者报告此兼容性 bug
3. **向 DSH 官方提 issue**：在 GitHub 上搜索已有的 issue，如果没有就提一个新的，标题建议：
   ```
   SessionFormatError: "format v4 message requires a producer-owned source kind" from mnemon_subagent_result tool
   ```
