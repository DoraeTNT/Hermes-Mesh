"""
Hermes Stream Manager v4.0 — buffered TS → fMP4 → <video> native playback
  - Accumulates TS chunks into 32KB+ buffers before feeding FFmpeg
  - FFmpeg: TS → fragmented MP4 (libx264 re-encode)
  - Serves fMP4 via HTTP, <video> tag plays natively (no MSE needed)
"""
import subprocess, threading, time, logging, struct

logger = logging.getLogger("hermes.stream_mgr")

_streams = {}
_streams_lock = threading.Lock()

TS_BUFFER_MIN = 4096   # buffer at least 4KB before feeding FFmpeg (TS needs full 188B packets)
MAX_SEGMENTS = 120       # ~60s of fMP4 at 2 segments/s
GOP_FRAMES = 20
FRAG_DURATION = 500000  # 0.5s


class StreamSession:
    def __init__(self, device_id):
        self.device_id = device_id
        self.active = False
        self.ffmpeg_proc = None
        self._init_data = b""
        self._segments = []            # [(seq, fmp4_bytes), ...]
        self._seg_lock = threading.Lock()
        self._seq = 0
        self.client_count = 0
        self._start_lock = threading.Lock()
        self._ts_buf = b""             # TS accumulation buffer

    def start_ffmpeg(self):
        if self.ffmpeg_proc is not None:
            return
        with self._start_lock:
            if self.ffmpeg_proc is not None:
                return
            cmd = [
                "ffmpeg",
                "-fflags", "+genpts",
                "-f", "mpegts",
                "-i", "pipe:0",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-profile:v", "main",
                "-level", "4.0",
                "-g", str(GOP_FRAMES),
                "-keyint_min", str(GOP_FRAMES),
                "-sc_threshold", "0",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-crf", "23",
                "-an",
                "-f", "mp4",
                "-movflags", "frag_keyframe+default_base_moof+omit_tfhd_offset",
                "-frag_duration", str(FRAG_DURATION),
                "pipe:1",
            ]
            try:
                self.ffmpeg_proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.active = True
                logger.info("[STREAM_MGR] FFmpeg fMP4 started for %s", self.device_id)
                # Stderr drain
                def _drain():
                    try:
                        while self.active and self.ffmpeg_proc:
                            self.ffmpeg_proc.stderr.read(4096)
                    except Exception:
                        pass
                threading.Thread(target=_drain, daemon=True).start()
                # Output reader
                t = threading.Thread(target=self._read_output, daemon=True,
                                     name=f"fmp4-out-{self.device_id[:8]}")
                t.start()
            except Exception as e:
                logger.error("[STREAM_MGR] FFmpeg start failed: %s", e)
                self.active = False

    def feed_ts(self, data: bytes):
        """Accumulate TS, flush when buffer reaches threshold. First chunk always fed immediately."""
        if not self.active or self.ffmpeg_proc is None:
            return False
        if not self._ts_buf and len(data) >= 4096:
            # First large chunk — feed immediately so FFmpeg gets PAT/PMT right away
            try:
                self.ffmpeg_proc.stdin.write(data)
                self.ffmpeg_proc.stdin.flush()
                return True
            except (BrokenPipeError, OSError):
                logger.warning("[STREAM_MGR] Pipe broken for %s", self.device_id)
                self.stop()
                return False
        self._ts_buf += data
        if len(self._ts_buf) >= TS_BUFFER_MIN:
            try:
                self.ffmpeg_proc.stdin.write(self._ts_buf)
                self.ffmpeg_proc.stdin.flush()
                logger.info("[STREAM_MGR] Flushed %d bytes TS to FFmpeg for %s",
                           len(self._ts_buf), self.device_id)
            except (BrokenPipeError, OSError):
                logger.warning("[STREAM_MGR] Pipe broken for %s", self.device_id)
                self.stop()
                return False
            self._ts_buf = b""
            return True
        return False

    def flush_ts(self):
        """Force flush remaining TS buffer."""
        if self._ts_buf and self.active and self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.stdin.write(self._ts_buf)
                self.ffmpeg_proc.stdin.flush()
            except Exception:
                pass
            self._ts_buf = b""

    def _read_output(self):
        buf = b""
        init_done = False
        try:
            while self.active and self.ffmpeg_proc:
                chunk = self.ffmpeg_proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 8:
                    if not init_done:
                        # Find moov box
                        moov_pos = buf.find(b"moov")
                        if moov_pos < 4:
                            break
                        moov_size = struct.unpack('>I', buf[moov_pos-4:moov_pos])[0]
                        if moov_size < 8 or moov_size > 100000:
                            buf = buf[moov_pos+1:]
                            continue
                        moov_end = moov_pos - 4 + moov_size
                        if len(buf) < moov_end:
                            break
                        self._init_data = bytes(buf[:moov_end])
                        init_done = True
                        logger.info("[STREAM_MGR] fMP4 init %d bytes for %s",
                                   len(self._init_data), self.device_id)
                        buf = buf[moov_end:]
                        continue

                    # find moof
                    moof_pos = buf.find(b"moof")
                    if moof_pos < 4:
                        break
                    moof_size = struct.unpack('>I', buf[moof_pos-4:moof_pos])[0]
                    if moof_size < 32 or moof_size > 500000:
                        buf = buf[moof_pos+1:]
                        continue
                    mdat_off = moof_pos - 4 + moof_size
                    if mdat_off + 8 > len(buf):
                        break
                    if buf[mdat_off+4:mdat_off+8] != b"mdat":
                        buf = buf[moof_pos+1:]
                        continue
                    mdat_size = struct.unpack('>I', buf[mdat_off:mdat_off+4])[0]
                    if mdat_size < 8 or mdat_size > 500000:
                        buf = buf[moof_pos+1:]
                        continue
                    seg_end = mdat_off + mdat_size
                    if seg_end > len(buf):
                        break
                    segment = bytes(buf[moof_pos-4:seg_end])
                    buf = buf[seg_end:]
                    self._add_segment(segment)
        except Exception as e:
            logger.error("[STREAM_MGR] fMP4 read error: %s", e, exc_info=True)
        finally:
            logger.info("[STREAM_MGR] fMP4 reader stopped for %s", self.device_id)

    def _add_segment(self, data: bytes):
        with self._seg_lock:
            self._seq += 1
            self._segments.append((self._seq, data))
            while len(self._segments) > MAX_SEGMENTS:
                self._segments.pop(0)

    def get_init(self) -> bytes:
        return self._init_data

    def get_segments_since(self, since_seq=0):
        with self._seg_lock:
            return [(seq, data) for seq, data in self._segments if seq > since_seq]

    def stop(self):
        self.active = False
        if self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.stdin.close()
                self.ffmpeg_proc.terminate()
                self.ffmpeg_proc.wait(timeout=3)
            except Exception:
                try:
                    self.ffmpeg_proc.kill()
                except Exception:
                    pass
            self.ffmpeg_proc = None
        logger.info("[STREAM_MGR] Stopped for %s", self.device_id)


# ── Public API ──

def get_or_create_stream(device_id: str) -> StreamSession:
    with _streams_lock:
        if device_id not in _streams:
            _streams[device_id] = StreamSession(device_id)
        return _streams[device_id]


def feed_stream(device_id: str, ts_data: bytes):
    """Called by bridge when TS data arrives."""
    session = get_or_create_stream(device_id)
    if not session.active:
        session.start_ffmpeg()
    session.feed_ts(ts_data)


def stop_stream(device_id: str):
    with _streams_lock:
        session = _streams.pop(device_id, None)
    if session:
        session.stop()


def get_stream_status(device_id: str) -> dict:
    with _streams_lock:
        session = _streams.get(device_id)
    if not session:
        return {"active": False, "clients": 0, "segments": 0}
    return {
        "active": session.active,
        "clients": session.client_count,
        "segments": session._seq,
        "init_ready": len(session._init_data) > 0,
    }
