"""
Hermes Streamer v2.0 — gdigrab → H.264 fMP4 → Bridge stream
Client produces fMP4 directly, server just relays — no double encode.
"""
import subprocess, os, sys, threading, base64, json, time, logging, struct

logger = logging.getLogger("hermes.streamer")

# ── 全局状态 ──
_ffmpeg_proc = None
_stream_thread = None
_stream_lock = threading.Lock()
_stream_running = False
_stream_sock = None
_stream_sock_lock = None
_stream_fps = 20
_stream_bitrate = "2M"
_stream_width = 0
_stream_height = 0
MAX_STREAM_WIDTH = 1280

# ── 调试日志 ──
_DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streamer_debug.log")
def _dbg(msg):
    try:
        with open(_DEBUG_LOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except:
        pass

# ── FFmpeg 路径 ──
_FFMPEG_CANDIDATES = [
    "ffmpeg.exe",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg_portable", "bin", "ffmpeg.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg_portable", "ffmpeg.exe"),
    os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "ffmpeg", "bin", "ffmpeg.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "ffmpeg", "ffmpeg.exe"),
]

_FFMPEG_DOWNLOAD_URLS = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip",
]

_FFMPEG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg_portable")
_FFMPEG_EXE = os.path.join(_FFMPEG_DIR, "bin", "ffmpeg.exe")
_FFMPEG_EXE_ALT = os.path.join(_FFMPEG_DIR, "ffmpeg.exe")


def _ensure_ffmpeg():
    for path in _FFMPEG_CANDIDATES:
        try:
            r = subprocess.run([path, "-version"], capture_output=True, timeout=5,
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            if r.returncode == 0:
                logger.info("[STREAM] FFmpeg found: %s", path)
                return path
        except Exception:
            continue
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
    return _download_ffmpeg()


def _download_ffmpeg():
    import urllib.request, zipfile, io
    logger.info("[STREAM] FFmpeg not found, downloading (~40MB)...")
    for url in _FFMPEG_DOWNLOAD_URLS:
        try:
            logger.info("[STREAM] Trying: %s", url)
            req = urllib.request.Request(url, headers={"User-Agent": "HermesStreamer/2.0"})
            resp = urllib.request.urlopen(req, timeout=120)
            data = resp.read()
            if len(data) < 1000000:
                continue
            logger.info("[STREAM] Downloaded %d MB, extracting...", len(data) // 1048576)
            os.makedirs(_FFMPEG_DIR, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                ffmpeg_bin = None
                for name in zf.namelist():
                    if name.endswith("/ffmpeg.exe") or name.endswith("\\ffmpeg.exe"):
                        ffmpeg_bin = name
                        break
                if not ffmpeg_bin:
                    continue
                for name in zf.namelist():
                    if name.endswith("/") or name.endswith("\\"):
                        continue
                    parts = name.replace("\\", "/").split("/")
                    rel_path = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
                    dest = os.path.join(_FFMPEG_DIR, rel_path)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with zf.open(name) as src, open(dest, "wb") as dst:
                        dst.write(src.read())
            logger.info("[STREAM] FFmpeg extracted to: %s", _FFMPEG_DIR)
            for exe in (_FFMPEG_EXE, _FFMPEG_EXE_ALT):
                if os.path.exists(exe):
                    return exe
            return None
        except Exception as e:
            logger.warning("[STREAM] Download failed from %s: %s", url, e)
    logger.error("[STREAM] All download URLs failed.")
    return None


def _detect_encoder():
    """CPU soft encoding — most compatible, always works."""
    return ("libx264", ["-preset", "ultrafast", "-tune", "zerolatency",
                         "-b:v", _stream_bitrate, "-maxrate", _stream_bitrate,
                         "-bufsize", str(int(float(_stream_bitrate.replace("M", "")) * 512)) + "k"])


def _build_ffmpeg_cmd(ffmpeg_path, encoder, encoder_opts):
    global _stream_fps, _stream_width, _stream_height
    keyframe_interval = max(1, _stream_fps // 2)

    cmd = [ffmpeg_path,
           # Desktop Duplication is considerably more reliable than gdigrab
           # for a high-DPI desktop. dup_frames also keeps a steady output
           # cadence when the desktop is momentarily static.
           "-f", "lavfi",
           "-i", f"ddagrab=output_idx=0:framerate={_stream_fps}:draw_mouse=1:dup_frames=1",
           "-vf", f"hwdownload,format=bgra,scale={MAX_STREAM_WIDTH}:-2:flags=fast_bilinear:force_original_aspect_ratio=decrease",
           "-c:v", encoder,
           *encoder_opts,
           "-pix_fmt", "yuv420p",
           "-profile:v", "main",
           "-level", "4.0",
           "-g", str(keyframe_interval),
           "-keyint_min", str(keyframe_interval),
           "-sc_threshold", "0",
           "-an",
           # Output fMP4 with empty moov + fragments at each keyframe
           "-f", "mp4",
           "-movflags", "frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
           "-frag_duration", "250000",  # 0.25s fragments for lower latency
           "pipe:1",
           ]
    return cmd


# ── fMP4 box types ──
_FTYP = b"ftyp"
_MOOV = b"moov"
_MOOF = b"moof"
_MDAT = b"mdat"


def _extract_fmp4_segments(data: bytes):
    """Parse fMP4 output stream into (init_data, segment_list)."""
    # FFmpeg with empty_moov outputs: ftyp + moov(empty) then moof+mdat pairs
    # We send the very first output as init, subsequent as segments.
    init = b""
    segments = []

    buf = data
    offset = 0

    while offset + 8 <= len(buf):
        size = struct.unpack('>I', buf[offset:offset+4])[0]
        box_type = buf[offset+4:offset+8]

        if size < 8 or offset + size > len(buf):
            break

        if box_type == _FTYP or box_type == _MOOV:
            init += buf[offset:offset+size]
        elif box_type == _MOOF:
            # moof box — find trailing mdat
            mdat_start = offset + size
            if mdat_start + 8 <= len(buf):
                mdat_size = struct.unpack('>I', buf[mdat_start:mdat_start+4])[0]
                if buf[mdat_start+4:mdat_start+8] == _MDAT:
                    seg_end = mdat_start + mdat_size
                    if seg_end <= len(buf):
                        segments.append(bytes(buf[offset:seg_end]))
                        offset = seg_end
                        continue
            # stdout reads can split moof from mdat. Keep this incomplete pair
            # buffered; MSE rejects a bare moof as a media segment.
            break
        elif box_type == _MDAT and not init:
            # Preserve an orphaned/partial mdat so the next read can be parsed
            # with its preceding complete fragment instead of discarding bytes.
            break

        offset += size

    return init, segments


def _stream_loop():
    global _ffmpeg_proc, _stream_running, _stream_sock, _stream_sock_lock

    _dbg("=== stream_loop v2.0 started ===")

    encoder, encoder_opts = _detect_encoder()
    _dbg(f"encoder={encoder}")

    ffmpeg = _ensure_ffmpeg()
    if not ffmpeg:
        _dbg("FFMPEG NOT FOUND")
        _stream_running = False
        return

    cmd = _build_ffmpeg_cmd(ffmpeg, encoder, encoder_opts)
    _dbg(f"cmd={' '.join(cmd[:3])} ...")

    try:
        _ffmpeg_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
        )
        _dbg(f"FFmpeg started pid={_ffmpeg_proc.pid}")
    except Exception as e:
        _dbg(f"FFmpeg start FAILED: {e}")
        _stream_running = False
        return

    init_sent = False
    fps_counter = 0
    fps_timer = time.time()

    # Buffer for accumulating fMP4 output
    buf = b""

    try:
        while _stream_running and _ffmpeg_proc.poll() is None:
            chunk = _ffmpeg_proc.stdout.read(65536)
            if not chunk:
                if _ffmpeg_proc.poll() is not None:
                    err_out = _ffmpeg_proc.stderr.read(4096).decode(errors='replace')
                    _dbg(f"FFmpeg exited code={_ffmpeg_proc.returncode} err={err_out[:300]}")
                    break
                time.sleep(0.001)
                continue

            buf += chunk

            if not init_sent:
                # Wait for init segment to be complete (ftyp + moov)
                init, _ = _extract_fmp4_segments(buf)
                if init:
                    # Send init
                    pkt_init = json.dumps({
                        "type": "stream",
                        "codec": "h264",
                        "subtype": "init",
                        "data": base64.b64encode(init).decode(),
                    })
                    _send_packet(pkt_init)
                    _dbg(f"Init sent: {len(init)} bytes")
                    # After init is sent, we can trim buf to start of first segment
                    # Find where init ends in buf
                    buf = buf[len(init):]
                    init_sent = True
                    continue

            if init_sent:
                # Extract and send segments
                _, segments = _extract_fmp4_segments(buf)
                for seg in segments:
                    pkt_seg = json.dumps({
                        "type": "stream",
                        "codec": "h264",
                        "subtype": "segment",
                        "data": base64.b64encode(seg).decode(),
                    })
                    _send_packet(pkt_seg)
                    fps_counter += 1
                    # Trim sent segment from buf
                    idx = buf.find(seg)
                    if idx >= 0:
                        buf = buf[idx + len(seg):]

                # Report FPS periodically
                now = time.time()
                if fps_counter > 0 and now - fps_timer >= 2.0:
                    actual_fps = fps_counter / (now - fps_timer)
                    _dbg(f"[fMP4] {actual_fps:.1f} fps  segments={fps_counter}")
                    fps_counter = 0
                    fps_timer = now

    except Exception as e:
        logger.error("[STREAM] Error: %s", e)
    finally:
        _cleanup_ffmpeg()
        logger.info("[STREAM] Stopped")


def _send_packet(data_str):
    global _stream_sock, _stream_sock_lock
    if not _stream_sock or not _stream_sock_lock:
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


# ── Public API ──

def start_stream(sock=None, sock_lock=None, fps=20, bitrate="2M", width=0, height=0):
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

    logger.info("[STREAM] Started: v2.0 fMP4 mode, %d FPS @ %s", _stream_fps, _stream_bitrate)
    return {"status": "started", "fps": _stream_fps, "bitrate": _stream_bitrate}


def stop_stream():
    global _stream_running, _stream_thread
    _stream_running = False
    _cleanup_ffmpeg()
    if _stream_thread and _stream_thread.is_alive():
        _stream_thread.join(timeout=3)
    logger.info("[STREAM] Stopped by request")
    return {"status": "stopped"}


def stream_status():
    return {
        "running": _stream_running,
        "fps": _stream_fps,
        "bitrate": _stream_bitrate,
        "encoder": _detect_encoder()[0] if not _stream_running else "active",
    }
