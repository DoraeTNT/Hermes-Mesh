"""
Hermes Stream Manager v3.0 — pure relay, no server-side FFmpeg
  - Client sends TS packets, server buffers and serves via SSE
  - Browser uses JS TS demuxer to play via MSE
"""
import threading, time, logging

logger = logging.getLogger("hermes.stream_mgr")

_streams = {}
_streams_lock = threading.Lock()

MAX_CHUNKS = 300  # ~30s of TS at 10 chunks/s


class StreamSession:
    def __init__(self, device_id):
        self.device_id = device_id
        self.active = False
        self.chunks = []        # [(seq, ts_data), ...]
        self.chunks_lock = threading.Lock()
        self.client_count = 0
        self._seq = 0

    def add_chunk(self, data: bytes):
        if not self.active:
            self.active = True
        with self.chunks_lock:
            self._seq += 1
            self.chunks.append((self._seq, data))
            while len(self.chunks) > MAX_CHUNKS:
                self.chunks.pop(0)

    def get_chunks_since(self, since_seq=0) -> list:
        with self.chunks_lock:
            return [(seq, data) for seq, data in self.chunks if seq > since_seq]

    def cleanup(self):
        self.active = False
        with self.chunks_lock:
            self.chunks.clear()


# ── Public API ──

def get_or_create_stream(device_id: str) -> StreamSession:
    with _streams_lock:
        if device_id not in _streams:
            _streams[device_id] = StreamSession(device_id)
        return _streams[device_id]


def feed_stream(device_id: str, ts_data: bytes):
    """Called by bridge when TS data arrives from client."""
    session = get_or_create_stream(device_id)
    session.add_chunk(ts_data)


def stop_stream(device_id: str):
    with _streams_lock:
        session = _streams.pop(device_id, None)
    if session:
        session.cleanup()


def get_stream_status(device_id: str) -> dict:
    with _streams_lock:
        session = _streams.get(device_id)
    if not session:
        return {"active": False}
    return {
        "active": session.active,
        "chunks_buffered": len(session.chunks),
        "clients": session.client_count,
    }
