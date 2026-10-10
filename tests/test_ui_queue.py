"""Request ordering regressions from the actual control page, with fake HTTP."""
from pathlib import Path
import shutil
import subprocess
import unittest


class UIQueueTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for control-page checks")
    def test_stop_ordering_failures_and_preview_settings(self):
        result = subprocess.run(["node", str(Path(__file__).with_name("ui_queue.js"))],
                                capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("PASS "), 7)
