# -*- coding: utf-8 -*-
"""
hermes.main — 组装入口
启动 Agent 后台线程 + Chat UI + 更新检查。
版本: 1.0
"""
__version__ = "1.0"

import sys, threading

from hermes.config import hide_console
from hermes.updater import startup_update, UpdateChecker, DevicePoller
from hermes.agent import run_agent
from hermes.chat import run_chat


def main():
    """主入口：launcher 下载完模块后调用此函数"""
    hide_console()

    # 启动更新检查（后台下载新模块）
    startup_update()
    UpdateChecker().start()
    DevicePoller().start()

    # 启动 Agent 后台线程
    agent_thread = threading.Thread(
        target=run_agent, daemon=True, name="gui-agent")
    agent_thread.start()

    # 启动 Chat UI（主线程，阻塞到窗口关闭）
    run_chat()


if __name__ == "__main__":
    main()
