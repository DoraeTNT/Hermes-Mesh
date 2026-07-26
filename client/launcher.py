# -*- coding: utf-8 -*-
"""
Hermes Launcher — 薄启动器
  - 设置模块搜索路径
  - 加载加密配置
  - 启动主程序（从磁盘加载，支持热更新）

打包时只有此文件进 exe，其他 .py 放 modules/ 目录。
热更新只替换 modules/ 下的文件，exe 本体不更新。
"""
import os
import sys


def _setup_tcl_tk_runtime():
    """Point tkinter at Tcl/Tk data embedded in a PyInstaller one-file app."""
    if not getattr(sys, "frozen", False):
        return
    bundle_dir = getattr(sys, "_MEIPASS", "")
    tcl_dir = os.path.join(bundle_dir, "_tcl_data")
    tk_dir = os.path.join(bundle_dir, "_tk_data")
    if os.path.isfile(os.path.join(tcl_dir, "init.tcl")):
        os.environ["TCL_LIBRARY"] = tcl_dir
    if os.path.isfile(os.path.join(tk_dir, "tk.tcl")):
        os.environ["TK_LIBRARY"] = tk_dir


def _get_exe_dir():
    """获取 exe 所在目录（兼容 PyInstaller onefile）"""
    if getattr(sys, "frozen", False):
        # PyInstaller onefile: __file__ 在临时目录，用 sys.executable
        return os.path.dirname(os.path.abspath(sys.executable))
    else:
        return os.path.dirname(os.path.abspath(__file__))


def _setup_path(exe_dir):
    """设置模块搜索路径，优先从 ./modules/ 加载"""
    modules_dir = os.path.join(exe_dir, "modules")
    dev_mode = False

    if os.path.isdir(modules_dir):
        sys.path.insert(0, modules_dir)
    else:
        # 开发模式：exe_dir = client/，modules 在 client/ 同级的项目根目录
        dev_mode = True
        project_root = os.path.dirname(exe_dir)
        # client/ 目录本身也在 path 中（含 crypto_loader, _enc_config 等）
        if os.path.isdir(exe_dir):
            sys.path.insert(0, exe_dir)
        # 项目根（含 hermes/ 包）
        if os.path.isdir(os.path.join(project_root, "hermes")):
            sys.path.insert(0, project_root)

    return modules_dir, dev_mode


def _hide_console():
    """隐藏控制台窗口（仅 Windows）"""
    if sys.platform == "win32":
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                if getattr(sys, "frozen", False):
                    ctypes.windll.user32.ShowWindow(hwnd, 0)
                else:
                    ctypes.windll.user32.ShowWindow(hwnd, 6)
        except Exception:
            pass


def main():
    _setup_tcl_tk_runtime()
    exe_dir = _get_exe_dir()

    # 单实例锁：防止双击启动多个进程
    if sys.platform == "win32":
        import ctypes
        mutex_name = "HermesLauncher_SingleInstance"
        ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            sys.exit(0)

    modules_dir, dev_mode = _setup_path(exe_dir)
    _hide_console()

    # ── 验证模块目录 ──
    if not dev_mode and not os.path.isdir(modules_dir):
        import tkinter.messagebox as mb
        mb.showerror(
            "启动失败",
            f"模块目录未找到:\n{modules_dir}\n\n"
            "请确保 modules/ 文件夹与 HermesLauncher.exe 在同一目录。"
        )
        sys.exit(1)

    # ── 验证核心模块 ──
    required = ["unified.py", "crypto_loader.py"]
    check_dir = modules_dir if not dev_mode else exe_dir
    missing = [f for f in required if not os.path.exists(os.path.join(check_dir, f))]
    if missing:
        import tkinter.messagebox as mb
        mb.showerror(
            "启动失败",
            f"缺少模块文件:\n" + "\n".join(f"  {m}" for m in missing) +
            f"\n\n目录: {check_dir}"
        )
        sys.exit(1)

    # ── 启动主程序 ──
    try:
        import unified
    except ImportError as e:
        import tkinter.messagebox as mb
        mb.showerror(
            "启动失败",
            f"无法加载 unified 模块:\n{e}\n\n"
            f"sys.path: {sys.path[:3]}\n"
            f"modules_dir: {modules_dir}"
        )
        sys.exit(1)

    try:
        # 防止 unified 重复隐藏控制台
        unified.hide_console = lambda: None

        unified.startup_update()
        unified.UpdateChecker().start()
        unified.DevicePoller().start()

        agent_thread = unified.threading.Thread(
            target=unified.run_agent, daemon=True, name="gui-agent"
        )
        agent_thread.start()

        unified.run_chat()
    except Exception as e:
        import traceback
        import tkinter.messagebox as mb
        mb.showerror(
            "启动失败",
            f"运行出错:\n{e}\n\n{traceback.format_exc()}"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
