"""
Hermes Stream Manager v5.0 — raw TS relay, no server FFmpeg
  Server simply buffers incoming TS, serves directly via HTTP.
  Browser plays via <video> tag (some browsers support TS natively).
"""
import threading, time, logging

logger = logging.getLogger("hermes.stream_mgr")

_streams = {}
_streams_lock = threading.Lock()

MAX_CHUNKS = 600  # ~60s at 10 chunks/s


class StreamSession:
    def __init__(self, device_id):
        self.device_id = device_id
        self.active = False
        self._chunks = []
        self._lock = threading.Lock()
        self._seq = 0
        self.client_count = 0

    def add_chunk(self, data: bytes):
        if not self.active:
            self.active = True
        with self._lock:
            self._seq += 1
            self._chunks.append((self._seq, data))
            while len(self._chunks) > MAX_CHUNKS:
                self._chunks.pop(0)

    def get_chunks_since(self, since_seq=0):
        with self._lock:
            return [(seq, data) for seq, data in self._chunks if seq > since_seq]

    def stop(self):
        self.active = False
        with self._lock:
            self._chunks.clear()


# ── Public API ──

def get_or_create_stream(device_id: str) -> StreamSession:
    with _streams_lock:
        if device_id not in _streams:
            _streams[device_id] = StreamSession(device_id)
        return _streams[device_id]


def feed_stream(device_id: str, ts_data: bytes):
    session = get_or_create_stream(device_id)
    session.add_chunk(ts_data)


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
        "chunks": session._seq,
        "clients": session.client_count,
    }
