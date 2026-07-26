import importlib.util
import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_streamer(relative_path):
    spec = importlib.util.spec_from_file_location(relative_path.replace("/", "_"), ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def box(kind, payload=b""):
    return struct.pack(">I", len(payload) + 8) + kind + payload


class StreamerSegmentTests(unittest.TestCase):
    def test_incomplete_moof_mdat_pair_is_not_emitted(self):
        moof = box(b"moof", b"header")
        mdat = box(b"mdat", b"video-payload")
        partial = moof + mdat[:-3]

        for relative_path in ("client/streamer.py", "modules/streamer.py"):
            streamer = load_streamer(relative_path)
            init, segments = streamer._extract_fmp4_segments(partial)
            self.assertEqual(init, b"")
            self.assertEqual(segments, [], relative_path)

            init, segments = streamer._extract_fmp4_segments(moof + mdat)
            self.assertEqual(init, b"")
            self.assertEqual(segments, [moof + mdat], relative_path)


if __name__ == "__main__":
    unittest.main()
