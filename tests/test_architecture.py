import sys
import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(SERVER_ROOT))

import server
from nso_server.service import RoomServer


class ArchitectureTest(unittest.TestCase):
    def test_compatibility_launcher_reexports_room_server(self):
        self.assertIs(server.RoomServer, RoomServer)

    def test_launcher_stays_small(self):
        source = (SERVER_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 40)
        self.assertNotIn("class RoomServer", source)

    def test_domain_modules_are_present(self):
        expected = {
            "config.py", "protocol.py", "models.py", "service.py", "cli.py", "udp.py",
            "mixins/core.py", "mixins/persistence.py", "mixins/social.py",
            "mixins/combat.py", "mixins/lifecycle.py", "mixins/commands.py",
            "mixins/interactions.py",
            "mixins/interaction_gameplay.py", "mixins/interaction_clan.py",
            "mixins/interaction_combat.py", "mixins/interaction_party.py",
            "mixins/interaction_trade.py",
        }
        actual = {
            path.relative_to(SERVER_ROOT / "nso_server").as_posix()
            for path in (SERVER_ROOT / "nso_server").rglob("*.py")
        }
        self.assertTrue(expected.issubset(actual))


if __name__ == "__main__":
    unittest.main()
