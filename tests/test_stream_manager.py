import base64
import unittest

from hermes import stream_manager


class StreamManagerTests(unittest.TestCase):
    def setUp(self):
        with stream_manager._streams_lock:
            stream_manager._streams.clear()

    def tearDown(self):
        with stream_manager._streams_lock:
            sessions = list(stream_manager._streams.values())
            stream_manager._streams.clear()
        for session in sessions:
            session.stop()

    def test_fmp4_init_and_segments_are_available_to_http_handlers(self):
        init = b"ftyp-init-moov"
        stream_manager.feed_stream("device-1", init, "init")
        stream_manager.feed_stream("device-1", b"moof-one", "segment")
        stream_manager.feed_stream("device-1", b"moof-two", "segment")

        session = stream_manager.get_or_create_stream("device-1")
        self.assertEqual(session.get_init(), init)
        self.assertEqual(session.get_segments_since(), [(1, b"moof-one"), (2, b"moof-two")])
        self.assertEqual(session.get_chunks_since(1), [(2, b"moof-two")])
        self.assertEqual(len(session.chunks), 2)
        self.assertEqual(session.frames, [])
        status = stream_manager.get_stream_status("device-1")
        self.assertTrue(status["active"])
        self.assertEqual(status["segments"], 2)
        self.assertEqual(status["sequence"], 2)
        self.assertEqual(base64.b64decode(status["init_b64"]), init)

    def test_unknown_stream_has_an_inactive_status(self):
        self.assertEqual(
            stream_manager.get_stream_status("missing"),
            {"active": False, "init_b64": "", "segments": 0, "clients": 0},
        )


if __name__ == "__main__":
    unittest.main()
