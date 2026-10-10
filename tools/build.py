#!/usr/bin/env python3
"""Package verified source at game-relative paths. Never publishes."""
from pathlib import Path
import hashlib
import json
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.3"
MOD = Path("bin/x64/plugins/cyber_engine_tweaks/mods/nightCityHaptics")


def main():
    dist = ROOT / "dist"
    staging = dist / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    files = {}
    for source_dir, target_dir in ((ROOT / "src/cet/nightCityHaptics", MOD),
                                   (ROOT / "src/companion", MOD / "companion")):
        if not source_dir.exists():
            raise RuntimeError(f"Missing payload: {source_dir}")
        for source in sorted(source_dir.rglob("*")):
            if not source.is_file() or source.suffix not in (".lua", ".json", ".py", ".txt", ".sh", ".cmd", ".html", ".woff2"):
                continue
            if "__pycache__" in source.parts or source.name in ("state.json", "status.json", "settings.json"):
                continue
            rel = target_dir / source.relative_to(source_dir)
            destination = staging / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            files[rel.as_posix()] = hashlib.sha256(destination.read_bytes()).hexdigest()
    assert (MOD / "init.lua").as_posix() in files
    assert (MOD / "companion/nch.py").as_posix() in files
    assert not any("/bridge/" in rel or rel.endswith("core.lua") for rel in files), "stale 0.1.0 files"
    leaked = []
    for rel in files:
        data = (staging / rel).read_bytes()
        for fragment in (b"/home/", b"/Users/", b"linuxbrew", b".pdb"):
            if fragment in data or fragment.decode().encode("utf-16-le") in data:
                leaked.append(rel)
    if leaked:
        raise RuntimeError(f"Private path or account data in payload: {leaked}")
    package = dist / f"NightCityHaptics-{VERSION}.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(files):
            info = zipfile.ZipInfo(rel, (2026, 10, 9, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100755 if rel.endswith((".sh", "nch.py")) else 0o100644) << 16
            z.writestr(info, (staging / rel).read_bytes())
    with zipfile.ZipFile(package) as z:
        assert z.testzip() is None
        assert set(z.namelist()) == set(files)
        for rel, digest in files.items():
            assert hashlib.sha256(z.read(rel)).hexdigest() == digest
    manifest = {"version": VERSION, "files": files, "zip": package.name,
                "zip_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
                "privacy_check": "No local paths/account IDs/debug artifacts in either encoding"}
    (dist / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"package": str(package), "files": len(files), "sha256": manifest["zip_sha256"]}))


if __name__ == "__main__":
    main()
