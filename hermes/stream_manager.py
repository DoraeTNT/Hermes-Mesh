"""In-memory relay for the Windows client's H.264 fragmented-MP4 stream."""

import base64
import threading


MAX_SEGMENTS = 600

_streams = {}
_streams_lock = threading.Lock()


class StreamSession:
    """Keeps one fMP4 initialization segment and a bounded segment history."""

    def __init__(self, device_id):
        self.device_id = device_id
        self.active = False
        self.client_count = 0
        self._init = b""
        self._segments = []
        self._seq = 0
        self._lock = threading.Lock()

    def add_init(self, data: bytes):
        """Start a stream generation with the ftyp/moov initialization bytes."""
        if not data:
            return
        with self._lock:
            self.active = True
            self._init = data
            self._segments.clear()
            self._seq = 0

    def add_segment(self, data: bytes):
        """Append a moof/mdat fMP4 media segment."""
        if not data:
            return
        with self._lock:
            self.active = True
            self._seq += 1
            self._segments.append((self._seq, data))
            while len(self._segments) > MAX_SEGMENTS:
                self._segments.pop(0)

    def add_chunk(self, data: bytes):
        """Legacy alias for clients that do not label their media packets."""
        self.add_segment(data)

    def get_init(self):
        with self._lock:
            return self._init

    def get_segments_since(self, since_seq=0):
        with self._lock:
            return [(seq, data) for seq, data in self._segments if seq > since_seq]

    def live_start_sequence(self, history=3):
        """Return a near-live sequence position with a small decoder warm-up."""
        with self._lock:
            return max(0, self._seq - max(0, history))

    def get_chunks_since(self, since_seq=0):
        """Compatibility alias used by the raw relay endpoints."""
        return self.get_segments_since(since_seq)

    @property
    def chunks(self):
        with self._lock:
            return list(self._segments)

    @property
    def frames(self):
        """MJPEG is not produced for fMP4 input; retain the old API safely."""
        return []

    def get_frames_since(self, since_id=0):
        return []

    def stop(self):
        with self._lock:
            self.active = False


def get_or_create_stream(device_id: str) -> StreamSession:
    with _streams_lock:
        if device_id not in _streams:
            _streams[device_id] = StreamSession(device_id)
        return _streams[device_id]


def feed_stream(device_id: str, data: bytes, subtype="segment"):
    """Store a client packet, preserving the init/segment fMP4 distinction."""
    session = get_or_create_stream(device_id)
    if subtype == "init":
        session.add_init(data)
    else:
        session.add_segment(data)


def stop_stream(device_id: str):
    with _streams_lock:
        session = _streams.pop(device_id, None)
    if session:
        session.stop()


def get_stream_status(device_id: str) -> dict:
    """Return the fMP4 metadata consumed by the dashboard's MediaSource player."""
    with _streams_lock:
        session = _streams.get(device_id)
    if not session:
        return {"active": False, "init_b64": "", "segments": 0, "clients": 0}
    with session._lock:
        init = session._init
        segments = len(session._segments)
        sequence = session._seq
        active = session.active
    return {
        "active": active,
        "init_b64": base64.b64encode(init).decode() if init else "",
        "segments": segments,
        "sequence": sequence,
        "clients": session.client_count,
    }
