"""
Hermes Stream Manager v2.1 — dual pipeline
  - Legacy: FFmpeg TS→fMP4 transcoding (for old clients)
  - Native: fMP4 relay (for new clients, no server-side FFmpeg)
"""
import subprocess, threading, time, logging, struct

logger = logging.getLogger("hermes.stream_mgr")

_streams = {}
_streams_lock = threading.Lock()

MAX_BUFFER_SEGMENTS = 60
SEGMENT_DURATION = 0.5
GOP_FRAMES = 20


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
        self.codec = "h264"
        self._ffmpeg_ready = threading.Event()
        self._seg_id = 0
        self._native = False  # True: fMP4 relay, False: FFmpeg transcode

    def start_native(self, init_data: bytes):
        """fMP4 relay mode — no server FFmpeg."""
        self.init_segment = init_data
        self.active = True
        self._native = True
        logger.info("[STREAM_MGR] Native fMP4 relay for %s, init=%d bytes",
                   self.device_id, len(init_data))

    def add_native_segment(self, data: bytes):
        if not self.active:
            return
        with self.segments_lock:
            self._seg_id += 1
            self.segments.append((self._seg_id, data))
            while len(self.segments) > MAX_BUFFER_SEGMENTS:
                self.segments.pop(0)
        self.last_frame_time = time.time()

    def start_ffmpeg(self):
        """Legacy: TS→fMP4 transcoding with FFmpeg."""
        if self.ffmpeg_proc is not None:
            return
        if not hasattr(self, '_start_lock'):
            self._start_lock = threading.Lock()
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
                    stderr=subprocess.PIPE,  # capture errors for diagnostics
                )
                self.active = True
                self._ffmpeg_ready.set()
                logger.info("[STREAM_MGR] FFmpeg transcode started for %s", self.device_id)

                # stderr drainer thread (prevents pipe blockage)
                def _drain_stderr():
                    try:
                        while self.active and self.ffmpeg_proc and self.ffmpeg_proc.stderr:
                            line = self.ffmpeg_proc.stderr.readline()
                            if not line:
                                break
                    except Exception:
                        pass
                threading.Thread(target=_drain_stderr, daemon=True,
                                 name=f"ffmpeg-err-{self.device_id[:8]}").start()

                t = threading.Thread(target=self._read_ffmpeg_output, daemon=True,
                                     name=f"ffmpeg-out-{self.device_id[:8]}")
                t.start()
            except Exception as e:
                logger.error("[STREAM_MGR] FFmpeg start failed: %s", e)
                self.active = False

    def feed_ts(self, ts_data: bytes):
        """Feed TS data to FFmpeg for transcoding."""
        if not self.active or self.ffmpeg_proc is None:
            return
        try:
            self.ffmpeg_proc.stdin.write(ts_data)
            self.ffmpeg_proc.stdin.flush()
        except (BrokenPipeError, OSError):
            logger.warning("[STREAM_MGR] FFmpeg pipe broken for %s", self.device_id)
            self.stop()

    def stop(self):
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
        logger.info("[STREAM_MGR] Stopped for %s", self.device_id)

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
                if len(chunk) > 0 and buf == chunk:
                    # First chunk received
                    preview = buf[:32].hex()
                    logger.info("[STREAM_MGR] First chunk %d bytes for %s, hex=%s",
                               len(chunk), self.device_id, preview)
                while len(buf) >= 8 and segments_this_round < 20:
                    # ── Step 1: Extract init segment (ftyp + moov) ──
                    if not init_done:
                        # Find moov box — look for "moov" marker anywhere in buffer
                        moov_pos = buf.find(b"moov")
                        if moov_pos < 4:
                            break  # need more data
                        # Verify: 4 bytes before "moov" should be a valid box size
                        moov_size = struct.unpack('>I', buf[moov_pos-4:moov_pos])[0]
                        if moov_size < 8 or moov_size > 100000:
                            buf = buf[moov_pos+1:]  # false positive, skip past
                            continue
                        moov_end = moov_pos - 4 + moov_size
                        if len(buf) < moov_end:
                            break  # need more data
                        self.init_segment = bytes(buf[:moov_end])
                        init_done = True
                        logger.info("[STREAM_MGR] Init %d bytes for %s", len(self.init_segment), self.device_id)
                        buf = buf[moov_end:]
                        continue

                    # ── Step 2: Extract media segments — scan for moof box ──
                    moof_pos = buf.find(b"moof")
                    if moof_pos < 4:
                        if init_done and len(buf) > 10000:
                            # Buffer growing but no moof found — log what we have
                            logger.warning("[STREAM_MGR] Buffer %d bytes but no moof found, first bytes: %s",
                                          len(buf), buf[:16].hex())
                            buf = buf[-8192:]  # keep last 8K to prevent infinite growth
                        break

                    moof_size = struct.unpack('>I', buf[moof_pos-4:moof_pos])[0]
                    if moof_size < 32 or moof_size > 500000:
                        buf = buf[moof_pos+1:]
                        continue

                    # Find trailing mdat
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


def feed_init(device_id: str, data: bytes):
    """Native fMP4 relay: init segment from new client."""
    session = get_or_create_stream(device_id)
    session.start_native(data)


def feed_stream(device_id: str, data: bytes):
    """
    Handles both old (TS) and new (fMP4 segment) clients.
    - If session is already in native mode → add as fMP4 segment
    - If session is in legacy/FFmpeg mode → feed TS data
    - If session just started and hasn't committed → feed TS (legacy default)
    """
    session = get_or_create_stream(device_id)
    if session._native:
        session.add_native_segment(data)
    else:
        # Legacy TS → start FFmpeg and feed
        if not session.active:
            session.start_ffmpeg()
        session.feed_ts(data)


def stop_stream(device_id: str):
    with _streams_lock:
        session = _streams.pop(device_id, None)
    if session:
        session.stop()


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
        "native": session._native,
    }


def list_active_streams() -> list:
    with _streams_lock:
        return [
            {"device_id": did, "active": s.active, "clients": s.client_count,
             "segments": len(s.segments), "native": s._native}
            for did, s in _streams.items()
        ]
