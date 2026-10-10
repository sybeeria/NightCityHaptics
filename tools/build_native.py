#!/usr/bin/env python3
"""Build the Linux and Windows native companions. Development tooling only."""
import argparse
import os
from pathlib import Path
import shutil
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("all", "linux", "windows"), default="all")
    args = parser.parse_args()
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
    environment = dict(os.environ)
    environment["PATH"] = str(Path(cargo).parent) + os.pathsep + environment.get("PATH", "")
    if not shutil.which("cc"):
        # SteamOS keeps /usr read-only. Use the user's existing Homebrew toolchain
        # without inheriting GCC's Homebrew runtime search paths in the payload.
        brew = shutil.which("brew")
        clang = shutil.which("clang")
        if not brew or not clang:
            raise RuntimeError("Install a C linker (cc or Homebrew clang/gcc) before building")
        gcc_root = Path(subprocess.check_output([brew, "--prefix", "gcc"], text=True).strip())
        glibc_root = Path(subprocess.check_output([brew, "--prefix", "glibc"], text=True).strip())
        gcc_dir = next(p for p in gcc_root.glob("lib/gcc/current/gcc/x86_64-pc-linux-gnu/*") if (p / "crtbeginS.o").is_file())
        linker = ROOT / ".build/native-tools/cc"
        linker.parent.mkdir(parents=True, exist_ok=True)
        linker.write_text("#!/bin/sh\nexec " + shlex.join([clang, "--gcc-install-dir=" + str(gcc_dir), "-isystem", str(glibc_root / "include")]) + ' "$@"\n')
        linker.chmod(0o755)
        environment["CC"] = str(linker)
        environment["CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER"] = str(linker)
    # Remap dependency/build paths before compilation, including panic locations.
    flags = environment.get("RUSTFLAGS", "")
    environment["RUSTFLAGS"] = flags + " --remap-path-prefix=" + str(Path.home()) + "=source --remap-path-prefix=/home/linuxbrew/.linuxbrew=toolchain"
    common_flags = environment["RUSTFLAGS"]
    for platform in ("linux", "windows") if args.platform == "all" else (args.platform,):
        target = "x86_64-unknown-linux-gnu" if platform == "linux" else "x86_64-pc-windows-msvc"
        environment["RUSTFLAGS"] = common_flags + (" -C target-feature=+crt-static -C link-arg=/DEBUG:NONE" if platform == "windows" else "")
        command = [cargo, "build" if platform == "linux" else "xwin", "--release", "--locked", "--target", target]
        if platform == "windows":
            command.insert(2, "build")
        subprocess.run(command, cwd=ROOT / "rust", env=environment, check=True)
        name = "night-city-haptics" + (".exe" if platform == "windows" else "")
        destination = ROOT / "src/companion" / name
        shutil.copy2(ROOT / "rust/target" / target / "release" / name, destination)
        if platform == "linux":
            destination.chmod(0o755)
        print(destination)


if __name__ == "__main__":
    main()
