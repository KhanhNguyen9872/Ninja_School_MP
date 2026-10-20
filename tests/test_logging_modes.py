"""Console verbosity and file logging contract."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from nso_server.service import RoomServer


class LoggingModesTest(unittest.TestCase):
    def test_default_console_is_lifecycle_only(self):
        server = RoomServer()
        output = io.StringIO()
        with redirect_stdout(output):
            server.log("command", command="state")
            server.log_hot_path("combat", hp=1)
            server.log("room_joined", room="ROOM", players=1)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn('"room_joined"', lines[0])

    def test_verbose_restores_command_and_hot_detail(self):
        server = RoomServer(trace_hot_path=True)
        output = io.StringIO()
        with redirect_stdout(output):
            server.log("command", command="state")
            server.log_hot_path("combat", hp=1)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)

    def test_log_file_receives_detail_and_quiet_silences_console(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.log"
            server = RoomServer(log_file=str(path), quiet=True)
            output = io.StringIO()
            with redirect_stdout(output):
                server.log("room_joined", room="ROOM", players=1)
                server.log("command", command="state")
                server.log_hot_path("combat", hp=1)
            server.close_log()
            self.assertEqual(output.getvalue(), "")
            events = [line for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(events), 3)
            self.assertIn('"command"', "\n".join(events))


if __name__ == "__main__":
    unittest.main()
