"""
Hermes Updater — 外部更新器
  由客户端 exe 启动后退出，updater 等待 exe 解锁，执行替换，再启动新版。

用法：
  python updater.py <old_exe> <new_exe> [--restart <exe_to_launch>] [--version <ver>]

流程：
  1. 等待 old_exe 解锁（最多 30 秒）
  2. 备份 old_exe → old_exe.bak
  3. 替换 new_exe → old_exe 的位置
  4. 启动新版 exe
  5. 删除自身临时文件
"""

import os, sys, time, shutil, subprocess

def wait_for_unlock(filepath, timeout=30):
    """等待文件不再被锁定"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # 尝试以独占方式打开文件
            with open(filepath, "r+b") as f:
                pass
            return True
        except (PermissionError, OSError):
            time.sleep(1)
    return False

def main():
    if len(sys.argv) < 3:
        print("Usage: updater.py <old_exe> <new_exe> [--restart <exe>]")
        sys.exit(1)

    old_exe = sys.argv[1]
    new_exe = sys.argv[2]

    # 解析 --restart 参数
    restart_exe = None
    if "--restart" in sys.argv:
        idx = sys.argv.index("--restart")
        if idx + 1 < len(sys.argv):
            restart_exe = sys.argv[idx + 1]

    # 解析 --version 参数
    version_str = None
    if "--version" in sys.argv:
        idx = sys.argv.index("--version")
        if idx + 1 < len(sys.argv):
            version_str = sys.argv[idx + 1]

    if not os.path.exists(new_exe):
        print(f"[ERROR] New exe not found: {new_exe}")
        sys.exit(1)

    # 等待旧 exe 解锁
    if os.path.exists(old_exe):
        print(f"[INFO] Waiting for {old_exe} to unlock...")
        if not wait_for_unlock(old_exe, timeout=30):
            print(f"[WARN] File still locked after 30s, forcing replace...")

        # 备份旧版
        bak = old_exe + ".bak"
        try:
            if os.path.exists(bak):
                os.remove(bak)
            shutil.copy2(old_exe, bak)
            print(f"[INFO] Backed up to {bak}")
        except Exception as e:
            print(f"[WARN] Backup failed: {e}")

        # 删除旧版
        try:
            os.remove(old_exe)
            print(f"[INFO] Removed old exe")
        except Exception as e:
            print(f"[ERROR] Cannot remove old exe: {e}")
            # 尝试重命名
            try:
                os.rename(old_exe, old_exe + ".old")
                print(f"[INFO] Renamed old exe to .old")
            except:
                sys.exit(1)

    # 移动新版到目标位置
    try:
        shutil.move(new_exe, old_exe)
        print(f"[INFO] Installed new exe: {old_exe}")
    except Exception as e:
        print(f"[ERROR] Cannot install new exe: {e}")
        # 回滚
        bak = old_exe + ".bak"
        if os.path.exists(bak):
            try:
                shutil.move(bak, old_exe)
                print(f"[INFO] Rolled back to backup")
            except:
                pass
        sys.exit(1)

    # 写版本文件
    try:
        if version_str:
            ver_file = os.path.join(os.path.dirname(old_exe), "version.txt")
            with open(ver_file, "w") as f:
                f.write(version_str)
    except:
        pass

    # 启动新版
    target = restart_exe or old_exe
    if os.path.exists(target):
        print(f"[INFO] Starting {target}...")
        subprocess.Popen([target], close_fds=True)
        print("[INFO] Update complete!")
    else:
        print(f"[WARN] Target not found: {target}")

    # 清理旧备份（保留最近一个）
    time.sleep(2)

if __name__ == "__main__":
    main()
