#!/usr/bin/env python3
"""End-to-end check through the official Intiface Engine and a simulated Lovense.

Runs everything inside a private network namespace (`unshare -rn`), so it never
touches real ports, Bluetooth or a running game/test rig. Path of the signal:
fake CET heartbeat -> Rust companion -> Intiface Engine -> simulated device.

    .venv/bin/python tools/e2e_intiface.py            # writes reports/e2e-intiface-<date>.json
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
# Uses the same engine search order as the native app.
data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "night-city-haptics"
candidates = [os.environ.get("NCH_INTIFACE_ENGINE", ""),
              str(data / "intiface-engine-5.0.4/intiface-engine"),
              str(ROOT / "src/companion/intiface-engine"), shutil.which("intiface-engine") or ""]
ENGINE = next((Path(p) for p in candidates if p and Path(p).is_file() and os.access(p, os.X_OK)), Path("intiface-engine"))


def received_since(receipt, sequence):
    return [h["value"] for h in receipt.get("history", [])
            if h.get("received") and h.get("sequence", 0) > sequence]


def checks_pass(checks, expected_hurt):
    return (checks["companion_connected"] and checks["device_listed"] == ["Lovense Hush"]
            and abs(checks["hurt_peak"] - expected_hurt) <= .05
            and len(checks["engine_values"]) >= 2
            and checks["heartbeat_started_positive"] and checks["heartbeat_zero_received"]
            and checks["final_after_heartbeat_stops"] == 0 and checks["try_peak"] > .3
            and checks["quit_started_positive"] and checks["quit_zero_received"]
            and checks["final_after_quit"] == 0)


def inner():
    work = Path(tempfile.mkdtemp(prefix="nch-e2e-"))
    mod = work / "mod"
    mod.mkdir()
    (mod / "companion").mkdir()
    shutil.copyfile(ROOT / "src/companion/catalog.json", mod / "companion/catalog.json")
    (work / "settings.json").write_text(json.dumps({"start_intiface": False, "max_strength": .7}))
    received = work / "received.json"
    processes = []
    log = open(work / "log.txt", "w")

    def start(*args):
        processes.append(subprocess.Popen([str(a) for a in args], stdout=log, stderr=subprocess.STDOUT))

    def http(path, body=None):
        request = urllib.request.Request("http://127.0.0.1:19870" + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", "X-NCH": "1"})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    seq = 0

    def beat(**fields):
        nonlocal seq
        seq += 1
        state = {"v": 2, "session": "e2e", "seq": seq, "t": 50 + seq / 30, "active": True, "paused": False,
                 "levels": [], "events": [], "scene": []}
        state.update(fields)
        tmp = mod / "state.json.tmp"
        tmp.write_text(json.dumps(state))
        os.replace(tmp, mod / "state.json")

    def receipt():
        try:
            return json.loads(received.read_text())
        except (OSError, ValueError, KeyError):
            return {}

    def values(after=0):
        return received_since(receipt(), after)

    def boundary():
        return receipt().get("command_count", 0)

    result = {"engine": str(ENGINE), "checks": {}}
    try:
        start(ENGINE, "--websocket-port", 12345, "--max-ping-time", 3000, "--use-device-websocket-server",
              "--user-device-config-file", ROOT / "tools/virtual-device-config.json", "--log", "warn")
        time.sleep(1)
        start(sys.executable, ROOT / "tools/virtual_device.py", "--port", 19875, "--state-file", received, "--mod-dir", mod)
        start(ROOT / "src/companion/night-city-haptics", "--mod-dir", mod, "--settings", work / "settings.json", "--no-browser")
        deadline = time.monotonic() + 20
        state = {}
        while time.monotonic() < deadline:
            try:
                state = http("/api/state")
                if state["devices"]:
                    break
            except OSError:
                pass
            time.sleep(.2)
        result["checks"]["companion_connected"] = state.get("link") == "connected"
        result["checks"]["device_listed"] = [d["name"] for d in state.get("devices", [])]
        # Baseline heartbeat, then live gameplay with a hit and a level that is on by default (burning).
        for _ in range(5):
            beat(); time.sleep(1 / 30)
        hurt_start = boundary()
        beat(events=[[1, "hurt", 1, 50 + seq / 30]], levels={"burning": 0})
        for _ in range(6):
            time.sleep(1 / 30); beat(events=[[1, "hurt", 1, 50]])
        time.sleep(.3)
        hurt = values(hurt_start)
        result["checks"]["hurt_peak"] = max(hurt, default=0)
        engine_start = boundary()
        for _ in range(40):
            beat(levels={"burning": 1}); time.sleep(1 / 30)
        engine = values(engine_start)
        result["checks"]["engine_values"] = sorted(set(engine))
        heartbeat_start = boundary()
        result["checks"]["heartbeat_started_positive"] = bool(engine and engine[-1] > 0)
        time.sleep(1.6)  # heartbeat stops: the companion must zero the device
        stopped = values(heartbeat_start)
        result["checks"]["heartbeat_zero_received"] = 0 in stopped
        result["checks"]["final_after_heartbeat_stops"] = stopped[-1] if stopped else None
        try_start = boundary()
        http("/api/try", {"id": "levelUp"})
        time.sleep(1.5)
        result["checks"]["try_peak"] = max(values(try_start), default=0)
        test_start = boundary()
        http("/api/test-device", {"key": state["devices"][0]["key"]})
        deadline = time.monotonic() + .6
        while time.monotonic() < deadline and not any(v > 0 for v in values(test_start)):
            time.sleep(.02)
        current = values(test_start)
        result["checks"]["quit_started_positive"] = bool(current and current[-1] > 0)
        quit_start = boundary()
        http("/api/quit", {})
        time.sleep(1)
        stopped = values(quit_start)
        result["checks"]["quit_zero_received"] = 0 in stopped
        result["checks"]["final_after_quit"] = stopped[-1] if stopped else None
        expected_hurt = .9 * .7
        result["passed"] = checks_pass(result["checks"], expected_hurt)
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(3)
            except subprocess.TimeoutExpired:
                process.kill()
        log.close()
        result["log_tail"] = (work / "log.txt").read_text()[-1500:]
    print(json.dumps(result))


def main():
    if os.environ.get("NCH_E2E_INNER"):
        return inner()
    if not ENGINE.is_file():
        raise SystemExit("Intiface Engine not found: set NCH_INTIFACE_ENGINE or use the app's Download button")
    command = "ip link set lo up && NCH_E2E_INNER=1 exec " + shlex.quote(sys.executable) + " " + shlex.quote(__file__)
    output = subprocess.run(["unshare", "-rn", "sh", "-c", command],
                            capture_output=True, text=True, timeout=120)
    line = next((l for l in output.stdout.splitlines() if l.startswith("{")), None)
    if not line:
        raise SystemExit(output.stdout + output.stderr)
    result = json.loads(line)
    result["date"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report = Path(os.environ.get("NCH_E2E_REPORT", str(ROOT / "reports" / f"e2e-intiface-{time.strftime('%Y%m%d')}.json")))
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["checks"], indent=1))
    print("PASSED" if result.get("passed") else "FAILED", "->", report)
    sys.exit(0 if result.get("passed") else 1)


if __name__ == "__main__":
    main()
