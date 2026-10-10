"""Build boundaries using tiny temporary source trees and output directories."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("nch_build_audit", ROOT / "tools/build.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.sensor = self.root / "src/cet/nightCityHaptics/init.lua"
        self.companion = self.root / "src/companion/night-city-haptics"
        for rel in builder.REQUIRED:
            path = self.root / ("src/cet/nightCityHaptics" if rel == "init.lua" else "src") / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("payload " + rel)
        self.patch = mock.patch.object(builder, "ROOT", self.root)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.dist = self.root / "dist"

    def build(self, output=None):
        with contextlib.redirect_stdout(io.StringIO()):
            builder.main([] if output is None else ["--output-dir", str(output)])

    def test_deterministic_archive_and_manifest(self):
        self.build()
        archive = self.dist / f"NightCityHaptics-{builder.VERSION}.zip"
        before = archive.read_bytes()
        self.build()
        self.assertEqual(archive.read_bytes(), before)
        manifest = json.loads((self.dist / "manifest.json").read_text())
        self.assertEqual(manifest["zip_sha256"], hashlib.sha256(before).hexdigest())
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(set(z.namelist()), set(manifest["files"]))
            for rel, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256(z.read(rel)).hexdigest(), digest)

    def test_existing_version_is_immutable_and_candidate_output_is_separate(self):
        self.build()
        archive = self.dist / f"NightCityHaptics-{builder.VERSION}.zip"
        before = {p: p.read_bytes() for p in self.dist.rglob("*") if p.is_file()}
        self.sensor.write_text("-- changed sensor")
        with self.assertRaisesRegex(RuntimeError, "already exists with different content"):
            self.build()
        self.assertEqual({p: p.read_bytes() for p in self.dist.rglob("*") if p.is_file()}, before)
        candidate = self.root / "candidate"
        self.build(candidate)
        self.assertNotEqual((candidate / archive.name).read_bytes(), archive.read_bytes())
        self.assertEqual(archive.read_bytes(), before[archive])

    def test_private_paths_and_state_fail_before_package_publication(self):
        (self.companion.parent / "settings.json").write_text('{"secret":"private"}')
        (self.companion.parent / "status.json").write_text('{"running":true}')
        (self.companion.parent / "state.json").write_text('{}')
        self.build()
        manifest = json.loads((self.dist / "manifest.json").read_text())
        self.assertEqual(len(manifest["files"]), len(builder.REQUIRED))
        before = (self.dist / "manifest.json").read_bytes()
        self.companion.write_text("# private /home/someone/account")
        with self.assertRaisesRegex(RuntimeError, "Private path"):
            self.build()
        self.assertEqual((self.dist / "manifest.json").read_bytes(), before)

    def test_source_symlinks_are_rejected(self):
        outside = self.root / "outside.py"
        outside.write_text("# not payload")
        self.companion.unlink(); self.companion.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "symlinks"):
            self.build()
        self.assertFalse((self.dist / f"NightCityHaptics-{builder.VERSION}.zip").exists())

    def test_symlinked_output_is_rejected_without_following_it(self):
        self.dist.mkdir()
        outside = self.root / "outside"
        outside.mkdir(); (outside / "sentinel").write_text("untouched")
        (self.dist / "staging").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlinks"):
            self.build()
        self.assertEqual((outside / "sentinel").read_text(), "untouched")

    def test_missing_required_file_uses_runtime_validation(self):
        self.sensor.unlink()
        with self.assertRaisesRegex(RuntimeError, "required files"):
            self.build()
        self.assertFalse((self.dist / f"NightCityHaptics-{builder.VERSION}.zip").exists())


if __name__ == "__main__":
    unittest.main()
