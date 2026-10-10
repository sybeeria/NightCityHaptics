#!/bin/sh
""":"
# Keep Steam's loader libraries away from the native Python companion.
if [ "${LD_LIBRARY_PATH+x}" = x ]; then
    export NCH_STEAM_SAVED_LD_LIBRARY_PATH="$LD_LIBRARY_PATH"
else
    unset NCH_STEAM_SAVED_LD_LIBRARY_PATH
fi
if [ "${LD_PRELOAD+x}" = x ]; then
    export NCH_STEAM_SAVED_LD_PRELOAD="$LD_PRELOAD"
else
    unset NCH_STEAM_SAVED_LD_PRELOAD
fi
unset LD_LIBRARY_PATH LD_PRELOAD
exec /usr/bin/python3 "$0" "$@"
":"""
"""Start the local companion alongside a Steam command, then stop only our child.

Save this file in the Cyberpunk 2077 game folder. On Linux/SteamOS, Steam supplies
the complete command after ``--``. Needs Python 3.9+, standard library only.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request


STARTUP_SECONDS = 2.0
STOP_SECONDS = 5.0
KILL_SECONDS = 1.0
SAVED_VARIABLES = {
    "LD_LIBRARY_PATH": "NCH_STEAM_SAVED_LD_LIBRARY_PATH",
    "LD_PRELOAD": "NCH_STEAM_SAVED_LD_PRELOAD",
}


def warn(message):
    print("Night City Haptics: " + message, file=sys.stderr, flush=True)


def environments(source):
    """Restore Steam's loader variables only for the game, including empty ones."""
    game = dict(source)
    companion = dict(source)
    for name, marker in SAVED_VARIABLES.items():
        if marker in game:
            game[name] = game.pop(marker)
        companion.pop(marker, None)
        companion.pop(name, None)
    return game, companion


class LocalPingOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        # App detection is strictly localhost; a foreign listener must not send
        # launch-time probes to a different host or endpoint.
        return None


def already_running(port, timeout=0.2):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), LocalPingOnly())
    try:
        with opener.open("http://127.0.0.1:{}/api/ping".format(port), timeout=timeout) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            return False
        reply = json.loads(payload)
        return isinstance(reply, dict) and reply.get("app") == "night-city-haptics"
    except (OSError, ValueError, http.client.HTTPException):
        return False


def start_companion(path, port, environment, interrupted):
    if already_running(port):
        return None
    if interrupted():
        return None
    try:
        if not path.is_file():
            warn("companion not found at {}; continuing with the game.".format(path))
            return None
        process = subprocess.Popen(
            [sys.executable, str(path), "--no-browser", "--port", str(port),
             "--mod-dir", str(path.parent.parent)],
            env=environment, stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        warn("could not start the companion ({}); continuing with the game.".format(exc))
        return None

    deadline = time.monotonic() + STARTUP_SECONDS
    while not interrupted():
        result = process.poll()
        if result is not None:
            # A simultaneous launcher may have won the port; its process is not ours.
            if not already_running(port):
                warn("companion exited with status {}; continuing with the game.".format(result))
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            warn("companion is still starting; continuing with the game.")
            break
        if already_running(port, min(0.2, remaining)):
            break
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.05, remaining))
    return process


def stop_owned_companion(process):
    if process is None or process.poll() is not None:
        return
    try:
        # nch.py owns its engine and sends StopAll during its SIGTERM cleanup.
        # Never signal a process group or quit an instance through the shared API.
        process.terminate()
        process.wait(STOP_SECONDS)
    except subprocess.TimeoutExpired:
        warn("companion did not stop within five seconds; stopping our child.")
        try:
            process.kill()
            process.wait(KILL_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            warn("could not finish stopping our companion child.")
    except OSError:
        pass


class Supervisor:
    def __init__(self):
        self.interrupted = 0
        self.game = None

    def forward(self, number):
        if self.game is not None and self.game.poll() is None:
            try:
                os.killpg(self.game.pid, number)
            except ProcessLookupError:
                pass

    def handle_signal(self, number, _frame):
        self.interrupted = number
        self.forward(number)


def port_number(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer")
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start Night City Haptics with Steam on Linux/SteamOS.")
    parser.add_argument("--companion", type=Path, default=Path(__file__).resolve().parent /
                        "bin/x64/plugins/cyber_engine_tweaks/mods/nightCityHaptics/companion/nch.py")
    parser.add_argument("--port", type=port_number, default=19870)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if len(args.command) < 2 or args.command[0] != "--":
        parser.error("supply Steam's command after --")
    command = args.command[1:]
    game_environment, companion_environment = environments(os.environ)
    supervisor = Supervisor()
    previous_handlers = {}
    companion = None
    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[number] = signal.signal(number, supervisor.handle_signal)
        companion = start_companion(args.companion, args.port, companion_environment,
                                    lambda: supervisor.interrupted)
        if supervisor.interrupted:
            return 128 + supervisor.interrupted
        try:
            supervisor.game = subprocess.Popen(command, env=game_environment,
                                               start_new_session=True)
        except OSError as exc:
            warn("could not start Steam's command: {}".format(exc))
            return 127 if isinstance(exc, FileNotFoundError) else 126
        # Cover a cancellation delivered while Popen was creating the game child.
        if supervisor.interrupted:
            supervisor.forward(supervisor.interrupted)
        result = supervisor.game.wait()
        return result if result >= 0 else 128 - result
    finally:
        stop_owned_companion(companion)
        for number, previous in previous_handlers.items():
            signal.signal(number, previous)


if __name__ == "__main__":
    sys.exit(main())
