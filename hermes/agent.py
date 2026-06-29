# -*- coding: utf-8 -*-
"""
hermes.agent — GUI Agent 后台线程
连接 Bridge，执行远程命令（截图/点击/键盘等）。

v1.1: 废弃 wrapped_main() 旧协议重复实现，直接调用 agent_main()。
      连接状态通过日志消息推断，更新 agent_status。
"""
__version__ = "1.1"

import os, sys, threading

from hermes.config import (
    get_base_dir, agent_status, agent_log_queue, chat_system_queue,
)


def agent_log(tag, msg):
    """统一日志函数：写入队列 + 重要事件推送到聊天区 + 自动更新连接状态"""
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{tag}] {msg}"

    # 写入详细日志队列
    try:
        agent_log_queue.put_nowait(line)
    except:
        pass

    # 重要事件推送聊天区
    if tag in ("INIT", "AGENT", "AUTH") or "ERR" in tag or "Connected" in msg:
        try:
            chat_system_queue.put_nowait(line)
        except:
            pass

    # 自动推断连接状态（agent_main 内部管理连接，我们从日志推断）
    if tag == "AUTH" and "Authenticated" in msg:
        agent_status["connected"] = True
        agent_status["last_error"] = ""
    elif tag == "AGENT" and "Disconnected" in msg:
        agent_status["connected"] = False
        agent_status["errors"] = agent_status.get("errors", 0) + 1
        agent_status["last_error"] = msg[:60]


def run_agent():
    """后台线程：加载 windows_agent_v4 模块并直接调用 agent_main()。

    v1.1 变更：
    - 废弃 wrapped_main() 旧协议连接循环
    - 直接调用 agent_mod.agent_main()（支持 ACK 两阶段确认 + 新旧协议兼容）
    - agent_status 通过日志消息自动推断连接状态
    """
    base_dir = get_base_dir()
    sys.path.insert(0, base_dir)

    agent_path = os.path.join(base_dir, "windows_agent_v4.py")
    if not os.path.exists(agent_path) and not getattr(sys, "frozen", False):
        agent_log("INIT", f"⚠ windows_agent_v4.py 不存在于 {base_dir}")
        return

    # 强制从磁盘加载最新版（绕过 PyInstaller 冻结模块）
    import importlib.util
    spec = importlib.util.spec_from_file_location("windows_agent_v4", agent_path)
    agent_mod = importlib.util.module_from_spec(spec)
    sys.modules["windows_agent_v4"] = agent_mod
    spec.loader.exec_module(agent_mod)

    # 日志重定向：Agent 内部所有 log 调用路由到 UI 队列
    agent_mod.log = agent_log
    agent_mod.log_err = lambda tag, msg: agent_log(f"{tag}:ERR", msg)

    # 初始化设备信息
    agent_status["device_name"] = agent_mod.DEVICE_NAME
    agent_status["device_id"] = agent_mod.DEVICE_ID
    agent_status["connected"] = False
    agent_status["errors"] = 0
    agent_status["cmds"] = 0

    agent_log("INIT", f"Agent v{agent_mod.CURRENT_VERSION} | Bridge:{agent_mod.HOST}:{agent_mod.PORT}")

    # ── 直接调用 agent_main()（内置重连循环 + ACK 两阶段 + 新旧协议兼容）──
    # agent_main() 是无限循环，仅在黑名单被拒或需要热更新重启时返回
    try:
        agent_mod.agent_main()
    except KeyboardInterrupt:
        agent_log("AGENT", "KeyboardInterrupt, shutting down")
    except Exception as e:
        agent_log("AGENT:ERR", f"agent_main crashed: {e}")
    finally:
        agent_status["connected"] = False
        agent_log("AGENT", "Agent thread exiting")
