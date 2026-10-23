"""A new room must not inject a synthetic client mob."""
import unittest
from nso_server.models import Room

class WorldInitializationTest(unittest.TestCase):
    def test_new_room_stays_empty_after_ensure_world(self):
        room = Room("empty-room")
        room.ensure_world()
        room.ensure_world()
        self.assertEqual(room.mobs, {})

    def test_ensure_world_preserves_real_client_rows(self):
        room = Room("real-room")
        row = {"mob_id": "real", "map": 1, "zone": 0, "hp": 45, "max_hp": 100}
        room.mobs["1:0:real"] = row
        room.ensure_world()
        self.assertEqual(list(room.mobs), ["1:0:real"])
        self.assertIs(room.mobs["1:0:real"], row)
        self.assertEqual(row["hp"], 45)

if __name__ == "__main__":
    unittest.main()
