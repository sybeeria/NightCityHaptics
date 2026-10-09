#!/usr/bin/env python3
"""End-to-end check through the official Intiface Engine and a simulated Lovense.

Runs everything inside a private network namespace (`unshare -rn`), so it never
touches real ports, Bluetooth or a running game/test rig. Path of the signal:
fake CET heartbeat -> nch.py companion -> Intiface Engine -> simulated device.

    .venv/bin/python tools/e2e_intiface.py            # writes reports/e2e-intiface-<date>.json
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/companion"))
import nch  # noqa: E402

# Uses the engine the app would use (NCH_INTIFACE_ENGINE, the app's download, PATH).
ENGINE = nch.Engine().executable() or Path("intiface-engine")


def inner():
    work = Path(tempfile.mkdtemp(prefix="nch-e2e-"))
    mod = work / "mod"
    mod.mkdir()
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
                 "levels": {}, "events": []}
        state.update(fields)
        tmp = mod / "state.json.tmp"
        tmp.write_text(json.dumps(state))
        os.replace(tmp, mod / "state.json")

    def values():
        try:
            return [h["value"] for h in json.loads(received.read_text())["history"] if h.get("received")]
        except (OSError, ValueError, KeyError):
            return []

    result = {"engine": str(ENGINE), "checks": {}}
    try:
        start(ENGINE, "--websocket-port", 12345, "--use-device-websocket-server",
              "--user-device-config-file", ROOT / "tools/virtual-device-config.json", "--log", "warn")
        time.sleep(1)
        start(sys.executable, ROOT / "tools/virtual_device.py", "--port", 19875, "--state-file", received)
        start(sys.executable, ROOT / "src/companion/nch.py", "--mod-dir", mod, "--settings", work / "settings.json", "--no-browser")
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
        # Baseline heartbeat, then live gameplay with a hit and a running engine.
        for _ in range(5):
            beat(); time.sleep(1 / 30)
        start_count = len(values())
        beat(events=[[1, "hurt", 1, 50 + seq / 30]], levels={"engine": 0})
        for _ in range(6):
            time.sleep(1 / 30); beat(events=[[1, "hurt", 1, 50]])
        time.sleep(.3)
        hurt = values()[start_count:]
        result["checks"]["hurt_peak"] = max(hurt, default=0)
        for _ in range(40):
            beat(levels={"engine": 1}); time.sleep(1 / 30)
        engine = values()[start_count + len(hurt):]
        result["checks"]["engine_values"] = sorted(set(engine))
        time.sleep(1.6)  # heartbeat stops: the companion must zero the device
        result["checks"]["final_after_heartbeat_stops"] = values()[-1] if values() else None
        http("/api/try", {"id": "levelUp"})
        time.sleep(1.5)
        result["checks"]["try_peak"] = max(values()[-40:], default=0)
        http("/api/quit", {})
        time.sleep(1)
        result["checks"]["final_after_quit"] = values()[-1] if values() else None
        expected_hurt = .9 * .7
        result["passed"] = (result["checks"]["companion_connected"] and result["checks"]["device_listed"] == ["Lovense Hush"]
                            and abs(result["checks"]["hurt_peak"] - expected_hurt) <= .05
                            and len(result["checks"]["engine_values"]) >= 2
                            and result["checks"]["final_after_heartbeat_stops"] == 0
                            and result["checks"]["try_peak"] > .3 and result["checks"]["final_after_quit"] == 0)
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
    output = subprocess.run(["unshare", "-rn", "sh", "-c", f'ip link set lo up && NCH_E2E_INNER=1 exec "{sys.executable}" "{__file__}"'],
                            capture_output=True, text=True, timeout=120)
    line = next((l for l in output.stdout.splitlines() if l.startswith("{")), None)
    if not line:
        raise SystemExit(output.stdout + output.stderr)
    result = json.loads(line)
    result["date"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report = ROOT / "reports" / f"e2e-intiface-{time.strftime('%Y%m%d')}.json"
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["checks"], indent=1))
    print("PASSED" if result.get("passed") else "FAILED", "->", report)
    sys.exit(0 if result.get("passed") else 1)


if __name__ == "__main__":
    main()
