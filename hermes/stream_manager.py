"""
Hermes Stream Manager — H.264 TS → fMP4 via FFmpeg libx264
  - Transcodes TS to browser-compatible H.264 Main profile
  - Proper GOP/keyframe/codec for MSE compatibility
"""
import subprocess, os, threading, time, logging, base64, json, struct

logger = logging.getLogger("hermes.stream_mgr")

_streams = {}
_streams_lock = threading.Lock()

MAX_BUFFER_SEGMENTS = 60
SEGMENT_DURATION = 1.0


class StreamSession:
    def __init__(self, device_id):
        self.device_id = device_id
        self.active = False
        self.ffmpeg_proc = None
        self.init_segment = b""
        self.segments = []
        self.segments_lock = threading.Lock()
        self.client_count = 0
        self.last_frame_time = 0
        self.fps = 0
        self.codec = "h264"
        self.resolution = "unknown"
        self._ffmpeg_ready = threading.Event()
        self._seg_id = 0

    def start_ffmpeg(self):
        if self.ffmpeg_proc is not None:
            return
        if not hasattr(self, '_start_lock'):
            self._start_lock = threading.Lock()
        with self._start_lock:
            if self.ffmpeg_proc is not None:
                return

            cmd = [
                "ffmpeg",
                "-fflags", "+genpts",              # regenerate PTS from DTS
                "-f", "mpegts",
                "-i", "pipe:0",
                # Transcode to browser-compatible H.264
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-profile:v", "main",
                "-level", "4.0",
                "-g", "48",                        # GOP = 48 frames (~2.4s at 20fps)
                "-keyint_min", "48",
                "-sc_threshold", "0",              # force keyframe at GOP boundary
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-crf", "23",
                "-an",                             # no audio (gdigrab has no audio)
                "-f", "mp4",
                "-movflags", "frag_keyframe+default_base_moof+omit_tfhd_offset",
                "-frag_duration", str(int(SEGMENT_DURATION * 1_000_000)),
                "-start_at_zero",
                "-avioflags", "direct",
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "pipe:1",
            ]

            try:
                self.ffmpeg_proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                self.active = True
                self._ffmpeg_ready.set()
                logger.info("[STREAM_MGR] FFmpeg started (libx264 main L4.0) for %s", self.device_id)

                t = threading.Thread(target=self._read_ffmpeg_output, daemon=True,
                                     name=f"ffmpeg-out-{self.device_id[:8]}")
                t.start()
            except Exception as e:
                logger.error("[STREAM_MGR] FFmpeg start failed: %s", e)
                self.active = False

    def stop_ffmpeg(self):
        self.active = False
        self._ffmpeg_ready.clear()
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
        logger.info("[STREAM_MGR] FFmpeg stopped for %s", self.device_id)

    def feed_ts(self, ts_data: bytes):
        if not self.active or self.ffmpeg_proc is None:
            return
        try:
            self.ffmpeg_proc.stdin.write(ts_data)
            self.ffmpeg_proc.stdin.flush()
        except (BrokenPipeError, OSError):
            logger.warning("[STREAM_MGR] FFmpeg pipe broken for %s", self.device_id)
            self.stop_ffmpeg()

    def _read_ffmpeg_output(self):
        buf = b""
        init_done = False
        try:
            while self.active and self.ffmpeg_proc:
                chunk = self.ffmpeg_proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk

                segments_this_round = 0
                while len(buf) >= 8 and segments_this_round < 20:
                    if not init_done:
                        moov_pos = buf.find(b"moov")
                        if moov_pos < 4:
                            break
                        moov_size = struct.unpack('>I', buf[moov_pos-4:moov_pos])[0]
                        if moov_size < 8 or moov_size > 100000:
                            buf = buf[1:]
                            continue
                        moov_end = moov_pos - 4 + moov_size
                        if len(buf) < moov_end:
                            break
                        self.init_segment = bytes(buf[:moov_end])
                        init_done = True
                        logger.info("[STREAM_MGR] Init %d bytes for %s", len(self.init_segment), self.device_id)
                        buf = buf[moov_end:]
                        continue

                    if buf[4:8] != b"moof":
                        buf = buf[1:]
                        continue

                    moof_size = struct.unpack('>I', buf[:4])[0]
                    if moof_size < 32 or moof_size > 500000:
                        buf = buf[1:]
                        continue

                    mdat_off = moof_size
                    if mdat_off + 8 > len(buf):
                        break

                    mdat_size = struct.unpack('>I', buf[mdat_off:mdat_off+4])[0]
                    if mdat_size < 8 or mdat_size > 500000:
                        buf = buf[1:]
                        continue

                    total = moof_size + mdat_size
                    if total > len(buf):
                        break

                    segment = bytes(buf[:total])
                    buf = buf[total:]
                    self._add_segment(segment)
                    self.last_frame_time = time.time()
                    segments_this_round += 1

        except Exception as e:
            logger.error("[STREAM_MGR] FFmpeg read error: %s", e, exc_info=True)
        finally:
            logger.info("[STREAM_MGR] FFmpeg output reader stopped for %s", self.device_id)

    def _add_segment(self, data: bytes):
        with self.segments_lock:
            self._seg_id += 1
            self.segments.append((self._seg_id, data))
            while len(self.segments) > MAX_BUFFER_SEGMENTS:
                self.segments.pop(0)

    def get_init(self) -> bytes:
        return self.init_segment

    def get_segments_since(self, since_id=0) -> list:
        with self.segments_lock:
            return [(sid, data) for sid, data in self.segments if sid > since_id]


# ── Public API ──

def get_or_create_stream(device_id: str) -> StreamSession:
    with _streams_lock:
        if device_id not in _streams:
            _streams[device_id] = StreamSession(device_id)
        return _streams[device_id]


def start_stream(device_id: str) -> StreamSession:
    session = get_or_create_stream(device_id)
    if not session.active:
        session.start_ffmpeg()
    return session


def stop_stream(device_id: str):
    with _streams_lock:
        session = _streams.pop(device_id, None)
    if session:
        session.stop_ffmpeg()


def feed_stream(device_id: str, ts_data: bytes):
    session = get_or_create_stream(device_id)
    if not session.active:
        session.start_ffmpeg()
    session.feed_ts(ts_data)


def get_stream_status(device_id: str) -> dict:
    with _streams_lock:
        session = _streams.get(device_id)
    if not session:
        return {"active": False}
    return {
        "active": session.active,
        "clients": session.client_count,
        "init_ready": len(session.init_segment) > 0,
        "segments_buffered": len(session.segments),
        "codec": session.codec,
    }


def list_active_streams() -> list:
    with _streams_lock:
        return [
            {"device_id": did, "active": s.active, "clients": s.client_count}
            for did, s in _streams.items()
        ]
