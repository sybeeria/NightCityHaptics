"""Validate the native mixer against deterministic request/expected fixtures.

Build with cargo build --manifest-path rust/Cargo.toml --example haptics_probe.
The probe has no hardware or network access.
"""
import json
import math
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
PROBE = Path(os.environ.get("NCH_HAPTICS_PROBE", ROOT / "rust/target/debug/examples/haptics_probe"))


def fixture_records(name):
    """Expand shared setup and state values without repeating them on disk."""
    with (FIXTURES / name).open(encoding="utf-8") as stream:
        metadata = json.loads(next(stream))
        if metadata["format"] != "native-fixtures-v1":
            raise ValueError("Unknown fixture format")
        shared = []

        def expand(value):
            if isinstance(value, dict):
                if set(value) == {"$ref"}:
                    return shared[value["$ref"]]
                return {key: expand(child) for key, child in value.items()}
            if isinstance(value, list):
                return [expand(child) for child in value]
            return value

        for value in metadata["shared"]:
            shared.append(expand(value))
        return [expand(json.loads(line)) for line in stream if line.strip()]


class RustHapticsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PROBE.is_file():
            raise unittest.SkipTest("Build the native haptics_probe first")

    def assert_nested(self, actual, expected, path="result"):
        if isinstance(expected, dict):
            self.assertIsInstance(actual, dict, path)
            self.assertEqual(set(actual), set(expected), path)
            for key in expected:
                self.assert_nested(actual[key], expected[key], f"{path}.{key}")
        elif isinstance(expected, list):
            self.assertIsInstance(actual, list, path)
            self.assertEqual(len(actual), len(expected), path)
            for index, (a, e) in enumerate(zip(actual, expected)):
                self.assert_nested(a, e, f"{path}[{index}]")
        elif isinstance(expected, (float, int)) and not isinstance(expected, bool):
            self.assertIsInstance(actual, (float, int), path)
            self.assertNotIsInstance(actual, bool, path)
            self.assertTrue(math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9),
                            f"{path}: {actual!r} != {expected!r}")
        elif isinstance(expected, bool):
            self.assertIs(actual, expected, path)
        elif expected is None:
            self.assertIsNone(actual, path)
        else:
            self.assertEqual(actual, expected, path)

    def check_fixture(self, name):
        records = fixture_records(f"haptics-{name}.jsonl")
        requests = [record["request"] for record in records]
        result = subprocess.run([str(PROBE)], input="".join(json.dumps(request, allow_nan=False) + "\n" for request in requests),
                                text=True, capture_output=True, timeout=60, check=True)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), len(records), result.stderr)
        for index, (line, record) in enumerate(zip(lines, records)):
            with self.subTest(case=index, op=record["request"]["op"]):
                self.assert_nested(json.loads(line), record["expected"])

    def test_every_feel_preview_and_texture(self):
        self.check_fixture("every_feel_preview_and_texture")

    def test_settings_values_outputs_and_device_limits(self):
        self.check_fixture("settings_values_outputs_and_device_limits")

    def test_heartbeat_boundaries_and_sequence_transitions(self):
        self.check_fixture("heartbeat_boundaries_and_sequence_transitions")

    def test_all_event_feels_scene_variants_release_and_resume(self):
        self.check_fixture("all_event_feels_scene_variants_release_and_resume")

    def test_random_overlaps_retriggers_levels_and_master_gates(self):
        self.check_fixture("random_overlaps_retriggers_levels_and_master_gates")


if __name__ == "__main__":
    unittest.main()
