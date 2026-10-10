#!/usr/bin/env python3
"""Package verified source at game-relative paths. Never publishes."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.4.1"
MOD = Path("bin/x64/plugins/cyber_engine_tweaks/mods/nightCityHaptics")
REQUIRED = {"init.lua", "companion/night-city-haptics", "companion/night-city-haptics.exe",
            "companion/catalog.json", "companion/ui.html", "companion/start.sh", "companion/start.cmd",
            "companion/fonts/archivo.woff2", "companion/fonts/OFL.txt"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist",
                        help="use a separate directory for an unreleased candidate")
    args = parser.parse_args(argv)
    dist = args.output_dir.resolve()
    dist.mkdir(parents=True, exist_ok=True)
    package = dist / f"NightCityHaptics-{VERSION}.zip"
    if package.is_symlink() or (dist / "manifest.json").is_symlink() or (dist / "staging").is_symlink():
        raise RuntimeError("Build outputs must not be symlinks")
    with tempfile.TemporaryDirectory(prefix=".nch-build-", dir=dist) as temporary:
        work = Path(temporary)
        staging = work / "staging"
        files = {}
        for source_dir, target_dir in ((ROOT / "src/cet/nightCityHaptics", MOD),
                                       (ROOT / "src/companion", MOD / "companion")):
            ancestors, current = [], ROOT
            for part in source_dir.relative_to(ROOT).parts:
                current = current / part
                ancestors.append(current)
            if not source_dir.is_dir() or any(path.is_symlink() for path in ancestors):
                raise RuntimeError(f"Missing or symlinked payload: {source_dir}")
            for source in sorted(source_dir.rglob("*")):
                if source.is_symlink():
                    raise RuntimeError(f"Payload must not contain symlinks: {source}")
                if not source.is_file() or (target_dir / source.relative_to(source_dir)).relative_to(MOD).as_posix() not in REQUIRED:
                    continue
                if "__pycache__" in source.parts or source.name in ("state.json", "status.json", "settings.json"):
                    continue
                rel = target_dir / source.relative_to(source_dir)
                destination = staging / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                files[rel.as_posix()] = hashlib.sha256(destination.read_bytes()).hexdigest()
        required = {(MOD / rel).as_posix() for rel in REQUIRED}
        if not required <= files.keys():
            raise RuntimeError("Payload is missing required files: " + ", ".join(sorted(required - files.keys())))
        leaked = []
        for rel in files:
            data = (staging / rel).read_bytes()
            for fragment in (b"/home/", b"/Users/", b"linuxbrew", b".pdb"):
                if fragment in data or fragment.decode().encode("utf-16-le") in data:
                    leaked.append(rel)
        if leaked:
            raise RuntimeError(f"Private path or account data in payload: {leaked}")
        candidate = work / package.name
        with zipfile.ZipFile(candidate, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for rel in sorted(files):
                info = zipfile.ZipInfo(rel, (2026, 10, 9, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (0o100755 if rel.endswith((".sh", "/night-city-haptics")) else 0o100644) << 16
                archive.writestr(info, (staging / rel).read_bytes())
        with zipfile.ZipFile(candidate) as archive:
            if archive.testzip() is not None or set(archive.namelist()) != set(files):
                raise RuntimeError("Package verification failed")
            for rel, digest in files.items():
                if hashlib.sha256(archive.read(rel)).hexdigest() != digest:
                    raise RuntimeError(f"Package digest mismatch: {rel}")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        # A version identifies fixed archive bytes. Keep published packages intact;
        # audit candidates use --output-dir until a version bump is requested.
        created_package = False
        try:
            os.link(candidate, package)
            created_package = True
        except FileExistsError:
            if package.is_symlink() or hashlib.sha256(package.read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"{package.name} already exists with different content; use --output-dir for this candidate")
        manifest = {"version": VERSION, "files": files, "zip": package.name,
                    "zip_sha256": digest,
                    "privacy_check": "No local paths/account IDs/debug artifacts in either encoding"}
        manifest_path = work / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        old_staging = work / "previous-staging"
        existing = dist / "staging"
        if existing.exists():
            os.replace(existing, old_staging)
        try:
            os.replace(staging, existing)
            os.replace(manifest_path, dist / "manifest.json")
        except BaseException:
            if existing.exists():
                shutil.rmtree(existing)
            if old_staging.exists():
                os.replace(old_staging, existing)
            if created_package:
                package.unlink()
            raise
    print(json.dumps({"package": str(package), "files": len(files), "sha256": digest}))


if __name__ == "__main__":
    main()
