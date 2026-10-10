"""Host tests for firmware/tools/diag_inject.py against a fake diag console on a pty (stdlib
unittest; skipped without pyserial)."""
import os
import select
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
try:
    import diag_inject as di
except ImportError:  # no pyserial
    di = None


class FakeDevice:
    """Answers `diag ...` lines like the firmware's DiagConsole; `diag bad` is an error."""

    def __init__(self):
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        self.slave = slave
        self.received = []
        self.stop = False
        threading.Thread(target=self.run, daemon=True).start()

    def emit(self, text):
        os.write(self.master, (text + "\r\n").encode())

    def run(self):
        buf = b""
        while not self.stop:
            if not select.select([self.master], [], [], 0.1)[0]:
                continue
            try:
                buf += os.read(self.master, 256)
            except OSError:
                return
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode().strip()
                self.received.append(line)
                if line == "diag bad":
                    self.emit(f"W (10) DiagConsole: error {line}: unknown command; try diag help")
                elif line.startswith("diag gesture "):
                    self.emit(f"I (11) WaveshareEsp32s3TouchAMOLED1inch75: IMU gesture: {line.split()[2]} (count 1)")
                    self.emit(f"I (12) DiagConsole: ok {line}: {line.split()[2]}")
                elif line.startswith("diag"):
                    self.emit(f"I (13) DiagConsole: ok {line}: done")

    def close(self):
        self.stop = True
        time.sleep(0.2)
        os.close(self.master)
        os.close(self.slave)


@unittest.skipIf(di is None, "pyserial not installed")
class DiagInjectTest(unittest.TestCase):
    def setUp(self):
        self.dev = FakeDevice()
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "serial.log")
        self.diag = di.DiagSerial(self.dev.path, self.log)

    def tearDown(self):
        self.diag.close()
        self.dev.close()
        self.tmp.cleanup()

    def test_send_waits_for_its_answer(self):
        ok, result, n = self.diag.send("diag gesture face_down")
        self.assertTrue(ok)
        self.assertEqual(result, "face_down")
        self.assertIsNotNone(self.diag.find_all(r"IMU gesture: face_down"))
        self.assertGreater(n, 0)
        ok, result, _ = self.diag.send("diag bad")
        self.assertFalse(ok)
        self.assertIn("unknown command", result)

    def test_no_answer_times_out(self):
        self.dev.stop = True
        time.sleep(0.3)
        ok, result, _ = self.diag.send("diag dump", timeout=0.5)
        self.assertFalse(ok)
        self.assertIn("no answer", result)

    def test_wait_for_and_redaction(self):
        since = self.diag.mark()
        self.dev.emit("I (20) wifi: connected with HomeNet, aid = 1, bssid = aa:bb:cc:dd:ee:ff")
        self.dev.emit("I (21) EspTcp: Resolved host -> 192.168.8.220 token=abc")
        hit = self.diag.wait_for(r"Resolved", 2, since)
        self.assertIsNotNone(hit)
        self.assertNotIn("192.168", hit[1])
        self.assertNotIn("abc", hit[1])
        time.sleep(0.2)
        with open(self.log, encoding="utf-8") as f:
            text = f.read()
        for secret in ("HomeNet", "aa:bb:cc", "192.168.8.220", "token=abc"):
            self.assertNotIn(secret, text)

    def test_script(self):
        script = os.path.join(self.tmp.name, "s.txt")
        with open(script, "w") as f:
            f.write("# comment\ndiag gesture pick_up\nexpect 'IMU gesture: pick_up' 2\n"
                    "sleep 0.1\nsend diag swipe left\nexpect 'never seen' 0.3\n")
        failures = di.run_script(self.diag, script)
        self.assertEqual(failures, 1)
        self.assertEqual(self.dev.received[:2], ["diag gesture pick_up", "diag swipe left"])

    def test_control_socket(self):
        path = os.path.join(self.tmp.name, "c.sock")
        srv = di.serve_control(self.diag, path)
        try:
            self.assertEqual(di.control_send(path, "diag gesture face_up", 3), "ok face_up")
            self.assertTrue(di.control_send(path, "diag bad", 3).startswith("error"))
            with self.assertRaises(SystemExit):
                di.serve_control(self.diag, path)
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
