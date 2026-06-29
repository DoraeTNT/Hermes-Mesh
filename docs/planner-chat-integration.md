# Planner ↔ Chat 集成方案设计

> 状态: 设计文档 | 日期: 2026-06-26 | 作者: 高级程序员

## 1. 背景

当前系统存在两条并行的 Agent 操作路径：

```
路径 A (Chat):  用户 → Chat UI → chat_server → LLM 推理 + 工具调用 → Bridge → Agent
路径 B (Planner): 脚本/手动 → planner/cli.py → Planner 状态机 → Bridge → Agent
```

**问题**：路径 A 缺少 Planner 的状态机能力（重试/回退/恢复/跨步骤上下文），路径 B 无法通过自然语言对话触发。

## 2. 目标

在 `chat_server.py` 中新增 `execute_plan` 工具，使 LLM 能将复杂任务输出为 TaskPlan JSON，交给 Planner 状态机执行，享受以下能力：

| 能力 | 当前 Chat 路径 | 集成后 |
|------|:---:|:---:|
| 单步执行 | ✅ | ✅ |
| 自动重试（N 次） | ❌ 手动 | ✅ RetryPolicy |
| 验证失败 → 回退动作 | ❌ | ✅ FALLBACK |
| 跨步骤上下文共享（截图尺寸等） | ❌ | ✅ Memory DB |
| 设备离线检测 + 指数退避重连 | ❌ | ✅ DeviceMonitor |
| 会话管理（UI 元素/对话框/里程碑） | ❌ | ✅ SessionManager |

## 3. 设计方案

### 3.1 架构

```
用户 → Chat UI → chat_server POST /chat
                       ↓
                  LLM (deepseek-v4-flash)
                       ↓
                  判断任务复杂度
                  ├── 简单（1-2步）→ 现有工具调用（screenshot/click/...）
                  └── 复杂（3+步）→ 输出 TaskPlan JSON → execute_plan 工具
                                                    ↓
                                              Planner 状态机
                                              ├── Step 1: retry × 3 → fallback
                                              ├── Step 2: store_context → read_context
                                              └── Step N: ...
                                                    ↓
                                              Bridge → Agent
```

### 3.2 新增工具定义

```python
{
    "type": "function",
    "function": {
        "name": "execute_plan",
        "description": "执行一个多步骤任务计划。适用于 3 步以上的复杂操作（如打开浏览器搜索、下载安装软件等）。"
                       "每个步骤可配置重试次数、回退动作和失败策略。步骤间可通过 store_context/read_context 共享数据。",
        "parameters": {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "object",
                    "description": "TaskPlan JSON：包含 steps 数组，每步指定 action/params + 可选的 validate/retry/fallback/on_failure",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "action": {"type": "string", "enum": ["screenshot","cmd","click","type_text","key","press","scroll","clipboard","open_url"]},
                                    "params": {"type": "object"},
                                    "validate": {"type": "object", "properties": {"type": {"type": "string"}, "value": {}}},
                                    "retry": {"type": "object", "properties": {"max": {"type": "integer"}, "delay": {"type": "number"}}},
                                    "fallback": {"type": "object", "properties": {"action": {"type": "string"}, "params": {"type": "object"}}},
                                    "on_failure": {"type": "string", "enum": ["abort", "skip", "continue"]},
                                    "read_context": {"type": "array", "items": {"type": "string"}},
                                    "store_context": {"type": "object"},
                                },
                                "required": ["id", "action"]
                            }
                        }
                    },
                    "required": ["steps"]
                }
            },
            "required": ["plan"]
        }
    }
}
```

### 3.3 实现步骤

#### Step 1: `chat_server.py` 添加 `execute_plan` 工具处理

```python
# 在 execute_tool() 中添加
elif tool_name == "execute_plan":
    from planner import Planner, plan_from_dict, SessionManager, MemoryDB
    from planner.cli import make_bridge_executor
    
    plan_dict = args["plan"]
    plan = plan_from_dict(plan_dict)
    
    executor = make_bridge_executor(
        f"http://127.0.0.1:{config.BRIDGE_HTTP_PORT}",
        config.SHARED_SECRET,
    )
    db = MemoryDB("/home/admin/hermes_gui_agent/data/memory.db")
    sm = SessionManager(db)
    session = sm.start(device_id or "chat-default", ttl=600)
    
    planner = Planner(executor, verbose=False, session_manager=sm)
    result = planner.run(plan, session=session)
    
    return json.dumps({
        "status": "success" if result.success else "failed",
        "completed": result.completed,
        "failed": result.failed,
        "total_steps": result.total,
        "duration_ms": result.duration_ms,
        "steps": [
            {"id": s.step_id, "status": s.status, "error": s.error}
            for s in result.steps
        ],
    }), []
```

#### Step 2: 更新 SYSTEM_PROMPT

在 `chat_server.py` 的 SYSTEM_PROMPT 中增加：

```
8. 当用户要求执行复杂操作（3 步以上：如"打开淘宝搜索花茶并截图"），使用 execute_plan 工具，
   将步骤组织为 TaskPlan JSON。每步可以指定验证条件、重试次数和回退动作。
   简单操作（1-2 步）继续使用现有的单个工具调用。
```

#### Step 3: TaskPlan 模板库

在 `planner/task_schema.py` 中扩展内置模板，覆盖常见场景：

```python
# 新增模板
BUILTIN_TEMPLATES = {
    # ...existing templates...
    "open_browser_search": {
        "steps": [
            {"id": "screenshot_before", "action": "screenshot", "params": {"quality": 40},
             "store_context": {"scr_w": "width", "scr_h": "height"}},
            {"id": "open_browser", "action": "cmd",
             "params": {"cmd": "start msedge https://www.taobao.com", "timeout": 15},
             "validate": {"type": "not_equals", "value": {"exit_code": 1}},
             "retry": {"max": 2, "delay": 3}},
            {"id": "wait_load", "action": "cmd",
             "params": {"cmd": "timeout /t 5 /nobreak >nul & echo done", "timeout": 10}},
            {"id": "screenshot_result", "action": "screenshot", "params": {"quality": 40}},
        ]
    },
    "search_and_screenshot": {
        "steps": [
            {"id": "focus_browser", "action": "cmd",
             "params": {"cmd": "start msedge https://s.taobao.com/search?q=${query}", "timeout": 15},
             "retry": {"max": 2, "delay": 3}},
            {"id": "wait_render", "action": "cmd",
             "params": {"cmd": "ping -n 6 127.0.0.1 >nul & echo done", "timeout": 10}},
            {"id": "capture", "action": "screenshot", "params": {"quality": 30}},
            {"id": "scroll_page", "action": "scroll",
             "params": {"direction": "down", "amount": 3},
             "on_failure": "continue"},
            {"id": "capture_scrolled", "action": "screenshot", "params": {"quality": 30}},
        ]
    },
}
```

### 3.4 LLM Prompt 工程

为引导 LLM 正确输出 TaskPlan JSON，需要在 SYSTEM_PROMPT 中提供 2-3 个示例：

```
TaskPlan 格式示例：

简单截图：{"steps": [{"id":"1","action":"screenshot","params":{"quality":40}}]}

带验证和重试：{"steps": [
  {"id":"1","action":"cmd","params":{"cmd":"start msedge https://example.com","timeout":15},
   "validate":{"type":"not_equals","value":{"exit_code":1}},
   "retry":{"max":2,"delay":3},
   "on_failure":"abort"},
  {"id":"2","action":"screenshot","params":{"quality":40}}
]}

跨步骤共享：{"steps": [
  {"id":"1","action":"screenshot","params":{"quality":30},
   "store_context":{"w":"width","h":"height"}},
  {"id":"2","action":"click",
   "params":{"x":"${w}/2","y":"${h}/2"},
   "read_context":["w","h"]}
]}
```

## 4. 风险评估

| 风险 | 概率 | 缓解措施 |
|------|:---:|------|
| LLM 输出的 TaskPlan JSON 格式错误 | 中 | plan_from_dict() 入口做 schema 校验，错误时返回友好提示让 LLM 修正 |
| Planner 执行耗时超过 LLM API 超时 (120s) | 低 | Planner 有步骤级超时（每步 180s）；LLM 端超时可调高到 240s |
| Memory DB 在并发 Chat 会话中冲突 | 低 | Memory DB 已按 `{device_id}:{chain_id}` namespace 隔离 |
| LLM 过度使用 execute_plan（简单任务也用） | 中 | Prompt 中明确「3 步以上才用」；可在 execute_plan 内部检查 step 数 < 3 时降级为直接执行 |

## 5. 实施计划

| 阶段 | 内容 | 工作量 |
|------|------|--------|
| Phase 1 | chat_server 添加 execute_plan 工具 + 验证 | 0.5 天 |
| Phase 2 | 更新 SYSTEM_PROMPT + 模板库 | 0.5 天 |
| Phase 3 | 端到端测试（3 个典型场景） | 0.5 天 |
| Phase 4 | 看板 Dashboard 增加 Plan 执行历史视图 | 1 天（可选） |

## 6. 决策点（需 @产品部管理-小王 确认）

1. execute_plan 工具是否对所有 Chat 用户开放，还是仅限特定 API Key？
2. 模板库中的"打开淘宝搜索"等电商场景是否需要（目前 1688/拼多多/京东反爬严格）？
3. Phase 4（看板 Plan 历史视图）是否纳入近期规划？
