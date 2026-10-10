"""Validate native output scheduling against deterministic command fixtures.

Build with cargo build --manifest-path rust/Cargo.toml --example transport_probe.
The native probe never opens a socket and cannot communicate with hardware.
"""
import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
PROBE = Path(os.environ.get("NCH_RUST_TRANSPORT_PROBE", ROOT / "rust/target/debug/examples/transport_probe"))


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


@unittest.skipUnless(PROBE.is_file(), "Build the native transport_probe first")
class RustTransportSchedulingTests(unittest.TestCase):
    def check_fixture(self, name):
        records = fixture_records(f"transport-{name}.jsonl")
        for index, record in enumerate(records):
            with self.subTest(scenario=index):
                result = subprocess.run([str(PROBE)], input=json.dumps(record["request"], allow_nan=False),
                                        text=True, capture_output=True, timeout=60, check=True)
                self.assertEqual(json.loads(result.stdout), record["expected"])

    def test_all_channels_follower_release_hold_and_quiet(self):
        self.check_fixture("all_channels_follower_release_hold_and_quiet")

    def test_random_settings_capability_forms_and_backpressure(self):
        self.check_fixture("random_settings_capability_forms_and_backpressure")

    def test_hotplug_capability_replacement_and_unicode_preferences(self):
        self.check_fixture("hotplug_capability_replacement_and_unicode_preferences")

    def test_rounding_small_changes_and_direction_impacts(self):
        self.check_fixture("rounding_small_changes_and_direction_impacts")


if __name__ == "__main__":
    unittest.main()
