# Night City Haptics

Cyberpunk 2077 haptics for any [Intiface](https://intiface.com)-compatible device (18+). Gunfire
kicks, hits thud, low health beats like a heart and engines rumble with speed. You get 29
interactions, and each one has its own switch, feel and strength. You tune them live from a local
control page.

![Control page](docs/screenshot.png)

## You need

- Cyberpunk 2077 on PC (Windows, or Linux / Steam Deck through Proton) with
  [Cyber Engine Tweaks](https://www.nexusmods.com/cyberpunk2077/mods/107).
- Python 3.9 or newer. Linux and SteamOS already have it. On Windows, get it from
  [python.org](https://www.python.org/downloads/). The app needs no other packages.
- A device that Intiface supports. Check yours on [iostindex.com](https://iostindex.com).

You don't need to install or start Intiface separately. The app downloads the official Intiface
Engine once and verifies its checksum. After that it starts the engine by itself. If you already
run Intiface Central on its default port (12345), the app uses that instead.

## Setup

1. Download [`NightCityHaptics-0.2.0.zip`](dist/NightCityHaptics-0.2.0.zip). Extract it into the
   game folder, the one that contains `bin`.
2. Open `bin/x64/plugins/cyber_engine_tweaks/mods/nightCityHaptics/companion/` and run
   **`start.cmd`** on Windows or **`start.sh`** on Linux. The control page opens in your browser.
3. On the first run, click **Download** on the Intiface tile. Put your device in pairing mode and
   the app finds it.
4. Play. Feedback runs only during live gameplay. Menus, pause, loading screens and photo mode stay
   silent.

## Using it

- **Interactions:** switch each one on or off, pick a feel, set its strength and press **Try** to
  feel it. Every interaction is a share of **Max strength**, and the total never goes above it.
- **Stop everything:** use the power button on the page, or **Pause** in the CET overlay. Both
  Pause and **Test buzz** can be bound to hotkeys in CET's Bindings.
- **Devices:** set strength per device, choose whether every motor runs, and **Test** or
  **Scan** again.
- **No device yet?** The live meter shows exactly what would be sent.
- **Quit** (top right) stops your devices and Intiface. Settings save automatically to
  `companion/settings.json`.

| Group | Interactions (* = off by default) |
|---|---|
| Combat | gunfire (by weapon class), getting hit, low health, explosions (by distance), melee, kills, takedowns, combat start |
| Cyberware | Sandevistan / Kerenzikov, Berserk, quickhack upload, optical camo* |
| Movement | sprinting, jumps, landings, dodges and slides |
| Driving | engine (by speed), crashes, drifting, gear shifts* |
| Rewards & world | police heat, level up, quest completed, eddies, healing, loot pickups* |
| Scenes | romance scenes, braindances*, other mods |

## Troubleshooting

- **Device not found:** close its phone app, which holds the connection. Then press **Scan** on
  the Devices tab. On Linux, Bluetooth must be on (BlueZ).
- **`start.cmd` says Python is missing:** install it from python.org, and keep the "py launcher"
  option ticked.
- **Logs:** `%LOCALAPPDATA%\NightCityHaptics\` on Windows, or `~/.local/share/night-city-haptics/`
  on Linux.

## Privacy

Everything runs on your PC. The page is served only on `127.0.0.1:19870` and refuses requests from
other sites. The app has no telemetry and no accounts. Its only network access is the one-time
engine download from Intiface's official GitHub release, and only when you click it.

## For mod authors

Other CET mods can send a pulse through the **Other mods** interaction:

```lua
GetMod("nightCityHaptics").Pulse(0.8, 2)   -- strength 0–1, seconds (optional)
```

## How it works

The CET mod (`init.lua`) only observes the game. About 30 times a second it writes events and
levels to `state.json`. The companion (`nch.py`, pure Python standard library) reads that file. It
mixes the result into one output and drives your devices through the Buttplug protocol. Output
stops within a second if the game stops writing.

## Development

```sh
luajit tests/cet_harness.lua                  # the real init.lua against a stubbed CET + game
pip install websockets                        # tests only: an independent Buttplug server
python3 -m unittest discover -s tests
python3 tools/build.py                        # builds dist/ (game-relative ZIP + manifest)
python3 tools/e2e_intiface.py                 # Linux: real Intiface Engine + simulated device
```

## Status

This is a prototype (0.2.0). It passed offline tests and an end-to-end run through the real
Intiface Engine with a simulated device. It also passed a run on Windows Python. In-game coverage
is still limited, so please open an issue if something misfires.

Built on [Buttplug / Intiface](https://buttplug.io) by Nonpolynomial.
