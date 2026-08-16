import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import HEAVY_GAME_PROFILE, UDP_LOAD_SEQUENCE_FLAG, ServiceState


class ServiceStateTests(unittest.TestCase):
    def test_heavy_session_tracks_game_and_pressure_packets_separately(self) -> None:
        state = ServiceState(37821, 37822)
        session = state.create_session(
            "127.0.0.1",
            60,
            120,
            256,
            profile=HEAVY_GAME_PROFILE,
            udp_min_payload_size=40,
            udp_max_payload_size=1024,
            target_mbps=3.0,
            udp_echo_payload=True,
        )

        self.assertTrue(session.accepts_udp_payload(40))
        self.assertTrue(session.accepts_udp_payload(1024))
        self.assertFalse(session.accepts_udp_payload(39))
        self.assertTrue(session.record_udp(7))
        self.assertTrue(session.record_udp(UDP_LOAD_SEQUENCE_FLAG | 7))
        self.assertFalse(session.record_udp(UDP_LOAD_SEQUENCE_FLAG | 7))
        self.assertEqual(session.snapshot()["udp_received"], 1)
        self.assertEqual(session.snapshot()["udp_load_received"], 1)


if __name__ == "__main__":
    unittest.main()
