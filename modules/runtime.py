# -*- coding: utf-8 -*-
"""
runtime.py — Phase 1: 统一 Runtime 层
  超时控制 / 自动重试 / Session 管理 / 错误恢复
"""
import time
import threading
import subprocess
import logging

logger = logging.getLogger("hermes.runtime")

# ── 通用重试装饰器 ──
class RetryConfig:
    def __init__(self, max_attempts=3, base_delay=1.0, max_delay=10.0, backoff=2.0):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff = backoff


def retry_on_failure(config=None):
    """装饰器：自动重试，指数退避"""
    cfg = config or RetryConfig()

    def decorator(func):
        def wrapper(*args, **kwargs):
            last_err = None
            delay = cfg.base_delay
            for attempt in range(cfg.max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    if attempt < cfg.max_attempts - 1:
                        logger.warning(
                            "RETRY %s attempt %d/%d after %.1fs: %s",
                            func.__name__, attempt + 1, cfg.max_attempts, delay, e
                        )
                        time.sleep(delay)
                        delay = min(delay * cfg.backoff, cfg.max_delay)
            raise last_err
        return wrapper
    return decorator


# ── 带超时的操作包装 ──
class TimeoutError(Exception):
    pass


def run_with_timeout(func, timeout, *args, **kwargs):
    """在线程中运行 func，超时抛异常"""
    result = [None]
    error = [None]
    done = threading.Event()

    def _runner():
        try:
            result[0] = func(*args, **kwargs)
        except Exception as e:
            error[0] = e
        finally:
            done.set()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    if not done.wait(timeout):
        raise TimeoutError(f"Operation timed out after {timeout}s")
    if error[0]:
        raise error[0]
    return result[0]


# ── Persistent CMD Session ──
class CmdSession:
    """持久 cmd.exe 会话，支持连续命令执行，保持环境变量/工作目录"""

    def __init__(self, timeout=30):
        self.timeout = timeout
        self._proc = None
        self._lock = threading.Lock()
        self._start()

    def _start(self):
        """启动持久 cmd 进程"""
        if self._proc and self._proc.poll() is None:
            return
        startupinfo = None
        if hasattr(subprocess, 'STARTUPINFO'):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self._proc = subprocess.Popen(
            ['cmd.exe'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            startupinfo=startupinfo,
        )

    def run(self, command: str, timeout: float = None) -> dict:
        """在持久 session 中执行命令，返回 {stdout, exit_code}"""
        timeout = timeout or self.timeout
        with self._lock:
            try:
                if self._proc is None or self._proc.poll() is not None:
                    self._start()

                # 发送命令 + 结束标记
                marker = f"__CMD_DONE_{int(time.time()*1000)}__"
                full_cmd = f"{command}\r\necho {marker} %errorlevel%\r\n"
                self._proc.stdin.write(full_cmd)
                self._proc.stdin.flush()

                # 读取输出直到标记
                output_lines = []
                exit_code = 0
                deadline = time.time() + timeout

                while time.time() < deadline:
                    line = self._proc.stdout.readline()
                    if not line:
                        break
                    if marker in line:
                        # 解析 exit code
                        parts = line.strip().split()
                        try:
                            exit_code = int(parts[-1])
                        except (ValueError, IndexError):
                            pass
                        break
                    output_lines.append(line.rstrip('\r\n'))

                stdout = '\n'.join(output_lines)
                logger.debug("CMD [%s] exit=%d out=%s", command[:60], exit_code, stdout[:100])
                return {"stdout": stdout, "exit_code": exit_code}

            except Exception as e:
                logger.error("CMD session error: %s", e)
                self._proc = None
                return {"stdout": "", "exit_code": -1, "error": str(e)}

    def close(self):
        if self._proc:
            try:
                self._proc.stdin.write("exit\r\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()
            self._proc = None


# ── 全局 session 实例 ──
_cmd_session = None
_session_lock = threading.Lock()


def get_cmd_session() -> CmdSession:
    global _cmd_session
    with _session_lock:
        if _cmd_session is None:
            _cmd_session = CmdSession()
        return _cmd_session
