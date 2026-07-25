"""
Hermes Streamer v1.0 — FFmpeg desktop capture + H.264 TS → Bridge v5 stream

Hardware auto-detect:
  NVENC (NVIDIA) → QuickSync (Intel) → libx264 (CPU)
"""
import subprocess, os, sys, threading, base64, json, time, logging, struct

logger = logging.getLogger("hermes.streamer")

# ── 全局状态 ──
_ffmpeg_proc = None
_stream_thread = None
_stream_lock = threading.Lock()
_stream_running = False
_stream_sock = None          # 由 agent 注入
_stream_sock_lock = None     # 由 agent 注入
_stream_fps = 20
_stream_bitrate = "2M"
_stream_preset = "ultrafast"
_stream_width = 0
_stream_height = 0

# ── 调试日志（写入文件，stdout 被 unified 重定向了）──
_DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streamer_debug.log")
def _dbg(msg):
    try:
        with open(_DEBUG_LOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except:
        pass

# ── FFmpeg 路径（Windows 常见位置）──
_FFMPEG_CANDIDATES = [
    "ffmpeg.exe",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg_portable", "bin", "ffmpeg.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg_portable", "ffmpeg.exe"),
    os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "ffmpeg", "bin", "ffmpeg.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "ffmpeg", "ffmpeg.exe"),
]

# 自动下载地址（按优先级）
_FFMPEG_DOWNLOAD_URLS = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip",
]

_FFMPEG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg_portable")
_FFMPEG_EXE = os.path.join(_FFMPEG_DIR, "bin", "ffmpeg.exe")
# 也有可能是根目录的 ffmpeg.exe（不同打包方式）
_FFMPEG_EXE_ALT = os.path.join(_FFMPEG_DIR, "ffmpeg.exe")


def _ensure_ffmpeg():
    """查找或自动下载 FFmpeg，返回 ffmpeg.exe 路径或 None"""
    # 1. 先找系统 PATH 里的
    for path in _FFMPEG_CANDIDATES:
        try:
            r = subprocess.run([path, "-version"], capture_output=True, timeout=5,
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            if r.returncode == 0:
                logger.info("[STREAM] FFmpeg found: %s", path)
                return path
        except Exception:
            continue

    # 2. 检查是否已自动下载过
    for exe in (_FFMPEG_EXE, _FFMPEG_EXE_ALT):
        if os.path.exists(exe):
            try:
                r = subprocess.run([exe, "-version"], capture_output=True, timeout=5,
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
                if r.returncode == 0:
                    logger.info("[STREAM] FFmpeg found (portable): %s", exe)
                    return exe
            except Exception:
                pass

    # 3. 自动下载
    return _download_ffmpeg()


def _download_ffmpeg():
    """从网络下载 FFmpeg 便携版到 modules/ffmpeg_portable/"""
    import urllib.request, zipfile, io

    logger.info("[STREAM] FFmpeg not found, downloading (~40MB)...")

    for url in _FFMPEG_DOWNLOAD_URLS:
        try:
            logger.info("[STREAM] Trying: %s", url)
            req = urllib.request.Request(url, headers={"User-Agent": "HermesStreamer/1.0"})
            resp = urllib.request.urlopen(req, timeout=120)
            data = resp.read()
            if len(data) < 1000000:
                logger.warning("[STREAM] Downloaded file too small (%d bytes), trying next URL", len(data))
                continue

            logger.info("[STREAM] Downloaded %d MB, extracting...", len(data) // 1048576)

            # 解压 zip 到 ffmpeg_portable/
            os.makedirs(_FFMPEG_DIR, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                # 找到 ffmpeg.exe 所在的顶层目录
                ffmpeg_bin = None
                for name in zf.namelist():
                    if name.endswith("/ffmpeg.exe") or name.endswith("\\ffmpeg.exe"):
                        ffmpeg_bin = name
                        break
                if not ffmpeg_bin:
                    logger.warning("[STREAM] ffmpeg.exe not found in archive")
                    continue

                # 解压所有文件（跳过目录项）
                for name in zf.namelist():
                    if name.endswith("/") or name.endswith("\\"):
                        continue
                    # 去掉顶层目录前缀
                    parts = name.replace("\\", "/").split("/")
                    if len(parts) > 1:
                        rel_path = "/".join(parts[1:])
                    else:
                        rel_path = parts[0]
                    dest = os.path.join(_FFMPEG_DIR, rel_path)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with zf.open(name) as src, open(dest, "wb") as dst:
                        dst.write(src.read())

            logger.info("[STREAM] FFmpeg extracted to: %s", _FFMPEG_DIR)

            # 验证
            for exe in (_FFMPEG_EXE, _FFMPEG_EXE_ALT):
                if os.path.exists(exe):
                    return exe

            logger.error("[STREAM] FFmpeg extracted but exe not found")
            return None

        except Exception as e:
            logger.warning("[STREAM] Download failed from %s: %s", url, e)
            continue

    logger.error("[STREAM] All download URLs failed. Install FFmpeg manually: https://www.gyan.dev/ffmpeg/builds/")
    return None


def _detect_encoder():
    """使用 CPU 软编码（兜底最稳定）"""
    return "libx264", ["-preset", "ultrafast", "-tune", "zerolatency", "-b:v", _stream_bitrate]


def _build_ffmpeg_cmd(encoder, encoder_opts):
    """构建 FFmpeg 命令行"""
    global _stream_fps, _stream_width, _stream_height

    # 基础参数
    cmd = [
        "-f", "gdigrab",              # Windows 桌面采集
        "-framerate", str(_stream_fps),
        "-i", "desktop",
        "-c:v", encoder,
        *encoder_opts,
        "-pix_fmt", "yuv420p",
        "-g", str(_stream_fps * 2),   # GOP = 2 秒
        "-keyint_min", str(_stream_fps),
        "-f", "mpegts",               # 输出 MPEG-TS
        "-mpegts_flags", "system_b",
    ]

    # 如果知道分辨率，限制采集区域
    if _stream_width > 0 and _stream_height > 0:
        # 插入 video filter 来设置分辨率
        cmd.insert(cmd.index("-c:v"), "-vf")
        cmd.insert(cmd.index("-c:v"), f"scale={_stream_width}:{_stream_height}")

    cmd.append("pipe:1")  # 输出到 stdout
    return cmd


def _stream_loop():
    """流线程主循环：读 FFmpeg stdout → 发 stream 包"""
    global _ffmpeg_proc, _stream_running, _stream_sock, _stream_sock_lock

    _dbg("=== stream_loop started ===")

    encoder, encoder_opts = _detect_encoder()
    _dbg(f"encoder={encoder} opts={encoder_opts}")

    ffmpeg = _ensure_ffmpeg()
    _dbg(f"ffmpeg_path={ffmpeg}")
    if not ffmpeg:
        _dbg("FFMPEG NOT FOUND - aborting")
        _stream_running = False
        return

    cmd = [ffmpeg] + _build_ffmpeg_cmd(encoder, encoder_opts)
    _dbg(f"cmd={' '.join(cmd)}")

    try:
        _ffmpeg_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # 改为 PIPE 捕获错误
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
        )
        _dbg(f"FFmpeg started pid={_ffmpeg_proc.pid}")
    except Exception as e:
        _dbg(f"FFmpeg start FAILED: {e}")
        _stream_running = False
        return

    fps_counter = 0
    fps_timer = time.time()
    chunk_size = 1316

    try:
        while _stream_running and _ffmpeg_proc.poll() is None:
            ts_chunk = _ffmpeg_proc.stdout.read(chunk_size)
            if not ts_chunk:
                # 检查 FFmpeg 是否已退出（有错误）
                if _ffmpeg_proc.poll() is not None:
                    err_out = _ffmpeg_proc.stderr.read(4096).decode(errors='replace')
                    _dbg(f"FFmpeg exited code={_ffmpeg_proc.returncode} err={err_out[:500]}")
                    break
                time.sleep(0.001)
                continue

            if fps_counter == 0:
                _dbg(f"First TS chunk received: {len(ts_chunk)} bytes  sock={_stream_sock is not None} lock={_stream_sock_lock is not None}")

            # 封装为 Bridge v5 stream 消息
            pkt = json.dumps({
                "type": "stream",
                "codec": "h264",
                "data": base64.b64encode(ts_chunk).decode(),
            })
            _send_packet(pkt)

            fps_counter += 1
            now = time.time()
            if now - fps_timer >= 1.0:
                actual_fps = fps_counter / (now - fps_timer)
                _dbg(f"[TS pkt/s] {actual_fps:.0f}  in={len(ts_chunk)}B  total={fps_counter}")
                fps_counter = 0
                fps_timer = now

    except Exception as e:
        logger.error("[STREAM] Error: %s", e)
    finally:
        _cleanup_ffmpeg()
        logger.info("[STREAM] Stopped")


def _send_packet(data_str):
    """通过 agent 的 socket 发送 stream 包（4字节长度前缀 + JSON）"""
    global _stream_sock, _stream_sock_lock
    if not _stream_sock:
        _dbg("SEND FAIL: _stream_sock is None")
        return
    if not _stream_sock_lock:
        _dbg("SEND FAIL: _stream_sock_lock is None")
        return
    data = data_str.encode() if isinstance(data_str, str) else data_str
    with _stream_sock_lock:
        try:
            _stream_sock.sendall(struct.pack(">I", len(data)) + data)
        except Exception as e:
            _dbg(f"SEND FAIL: {e}")
            global _stream_running
            _stream_running = False


def _cleanup_ffmpeg():
    """清理 FFmpeg 进程"""
    global _ffmpeg_proc
    if _ffmpeg_proc:
        try:
            _ffmpeg_proc.terminate()
            _ffmpeg_proc.wait(timeout=3)
        except Exception:
            try:
                _ffmpeg_proc.kill()
            except Exception:
                pass
        _ffmpeg_proc = None


# ── 公开 API ──

def start_stream(sock=None, sock_lock=None, fps=20, bitrate="2M", width=0, height=0):
    """启动视频流"""
    global _ffmpeg_proc, _stream_thread, _stream_running
    global _stream_sock, _stream_sock_lock, _stream_fps, _stream_bitrate
    global _stream_width, _stream_height

    if _stream_running:
        return {"status": "already_running"}

    _stream_sock = sock
    _stream_sock_lock = sock_lock
    _stream_fps = int(fps)
    _stream_bitrate = str(bitrate)
    _stream_width = int(width)
    _stream_height = int(height)

    _stream_running = True
    _stream_thread = threading.Thread(target=_stream_loop, name="streamer", daemon=True)
    _stream_thread.start()

    logger.info("[STREAM] Started: %d FPS @ %s", _stream_fps, _stream_bitrate)
    return {"status": "started", "fps": _stream_fps, "bitrate": _stream_bitrate}


def stop_stream():
    """停止视频流"""
    global _stream_running, _stream_thread
    _stream_running = False
    _cleanup_ffmpeg()
    if _stream_thread and _stream_thread.is_alive():
        _stream_thread.join(timeout=3)
    logger.info("[STREAM] Stopped by request")
    return {"status": "stopped"}


def stream_status():
    """获取流状态"""
    return {
        "running": _stream_running,
        "fps": _stream_fps,
        "bitrate": _stream_bitrate,
        "encoder": _detect_encoder()[0] if not _stream_running else "active",
    }
