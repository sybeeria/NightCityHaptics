# Night City Haptics

Cyberpunk 2077 haptics for any [Intiface](https://intiface.com)-compatible device (18+). Gunfire
kicks, hits thud, low health beats like a heart and engines rumble with speed. You get 41
interactions, including scene-specific romance and joytoy tracks, weapon charging and status
effects. Each one has its own switch, feel and strength, tuned live from a local control page.

![Control page](docs/screenshot.png)

## You need

- Cyberpunk 2077 on PC (Windows, or Linux / SteamOS through Proton) with
  [Cyber Engine Tweaks](https://www.nexusmods.com/cyberpunk2077/mods/107).
- Python 3.9 or newer. On Windows, get it from
  [python.org](https://www.python.org/downloads/). The app needs no other packages.
- A device that Intiface supports. Check yours on [iostindex.com](https://iostindex.com).

You don't need to install or start Intiface separately. The app downloads the official Intiface
Engine once and verifies its checksum. After that it starts the engine by itself. If you already
run Intiface Central on its default port (12345), the app uses that instead.

## Setup

1. Download [`NightCityHaptics-0.2.4.zip`](dist/NightCityHaptics-0.2.4.zip). Extract it into the
   game folder, the one that contains `bin`.
2. Open `bin/x64/plugins/cyber_engine_tweaks/mods/nightCityHaptics/companion/` and run
   **`start.cmd`** on Windows or **`start.sh`** on Linux. The control page opens in your browser.

   **Steam on Linux/SteamOS:** save [nch-steam-launch](https://raw.githubusercontent.com/sybeeria/NightCityHaptics/main/tools/nch-steam-launch)
   in the game folder. In Steam → Cyberpunk 2077 → Properties → Launch Options, use:

   ```sh
   /bin/sh "/path/to/Cyberpunk 2077/nch-steam-launch" -- %command%
   ```

   Replace the path. Keep existing overrides first, wrappers after `--`, and game flags after `%command%`.
   This starts the companion with the game and stops the instance it started when you exit.
   Tune it on [the control page](http://127.0.0.1:19870/).

3. On the first run, click **Download engine** in Settings. Put your device in pairing mode
   and the app finds it.
4. Play. Game-driven feedback runs only during live gameplay. Menus, pause, loading screens,
   photo mode and the CET overlay suspend it.

## Using it

- **Interactions:** grouped and collapsible. Switch each one on or off (or a whole group), pick a
  feel, set its strength and press **Try** to feel it. Every interaction is a share of
  **Max strength**, and their mixed output never goes above it. Max strength defaults to **70%**.
- **On/Off:** stops or enables the interaction mix, including Try previews. **Pause** in the CET
  overlay suspends game-driven feedback. Both Pause and **Test buzz** can be bound in CET's
  Bindings.
- **Devices:** enable each device, set its strength, choose **All motors**, and **Test** or
  **Scan** again. Devices default to enabled, 100% strength and vibration motors only;
  All motors is off.
- **Previews:** Try works without a running game and can preview an interaction that is switched
  off. Device Test is a separate 0.8-second pulse at 50% of that device's strength; it runs even
  with the mix Off and does not use Max strength. **Quit** stops devices and exits the app.
- **No device yet?** The live meter shows the interaction mix that would be sent; device strength
  and the separate Device Test can differ from the meter.
- **Settings** (under Devices) holds the Intiface address (default `ws://127.0.0.1:12345`),
  engine auto-start (on by default), and **Reset interactions**. The reset keeps your device and
  Intiface settings.
- **Quit** (top right) stops your devices and any Intiface Engine started by the app. Settings
  save automatically to `companion/settings.json`. The app starts with the mix On by default.

| Group | Interactions (* = off by default) |
|---|---|
| Combat | gunfire (by weapon class), getting hit, low health, explosions (by distance), melee, kills, takedowns, combat start, charging a shot, reloads, weapon overheat |
| Status effects | burning, electrocuted / EMP, poisoned / bleeding, knocked down / stunned, blinded, Relic glitches |
| Cyberware | Sandevistan / Kerenzikov, Berserk, quickhack upload, optical camo* |
| Movement | sprinting, jumps, landings, dodges / slides, running out of air |
| Driving | engine (by speed), crashes, drifting, gear shifts* |
| Rewards & world | police heat, level up, quest completed, eddies, healing, loot pickups*, incoming calls |
| Scenes | romance scenes, joytoys, braindances*, other mods |

Charging builds over 1.5 seconds. Low-air feedback starts below half your oxygen while diving and
increases as it runs out. Incoming calls use a two-buzz ringing pattern until you answer or decline.

<details>
<summary>Every interaction's default switch, strength and feel</summary>

Strength is a percentage of Max strength, before the game signal and feel are applied.

| Group | Interaction | Switch | Strength | Feel |
|---|---|---|---|---|
| Combat | Gunfire | On | 45% | Kick |
| Combat | Getting hit | On | 90% | Thud |
| Combat | Low health | On | 60% | Heartbeat |
| Combat | Explosions | On | 100% | Rumble burst |
| Combat | Melee swings | On | 40% | Tap |
| Combat | Enemy down | On | 45% | Double tap |
| Combat | Grabs & takedowns | On | 55% | Thud |
| Combat | Combat starts | On | 35% | Alarm |
| Combat | Charging a shot | On | 40% | Rumble |
| Combat | Reloads | On | 20% | Tap |
| Combat | Weapon overheat | On | 60% | Rumble burst |
| Status effects | Burning | On | 50% | Rumble |
| Status effects | Electrocuted & EMP | On | 45% | Flutter |
| Status effects | Poisoned & bleeding | On | 30% | Throb |
| Status effects | Knocked down & stunned | On | 70% | Thud |
| Status effects | Blinded | On | 50% | Rumble burst |
| Status effects | Relic glitches | On | 50% | Glitch |
| Cyberware | Sandevistan & Kerenzikov | On | 45% | Wave |
| Cyberware | Berserk | On | 60% | Throb |
| Cyberware | Quickhack upload | On | 25% | Flutter |
| Cyberware | Optical camo | Off | 20% | Wave |
| Movement | Sprinting | On | 20% | Footsteps |
| Movement | Jumps | On | 30% | Tap |
| Movement | Landings | On | 55% | Thud |
| Movement | Dodges & slides | On | 35% | Tap |
| Movement | Running out of air | On | 50% | Heartbeat |
| Driving | Engine | On | 30% | Rumble |
| Driving | Crashes | On | 100% | Thud |
| Driving | Drifting | On | 45% | Rumble |
| Driving | Gear shifts | Off | 20% | Tap |
| Rewards & world | Police heat | On | 60% | Alarm |
| Rewards & world | Level up | On | 70% | Fanfare |
| Rewards & world | Quest completed | On | 50% | Fanfare |
| Rewards & world | Eddies received | On | 30% | Tap |
| Rewards & world | Healing | On | 35% | Swell |
| Rewards & world | Loot pickups | Off | 15% | Tap |
| Rewards & world | Incoming calls | On | 40% | Phone ring |
| Scenes | Romance scenes | On | 80% | Follow the scene |
| Scenes | Joytoys | On | 80% | Follow the scene |
| Scenes | Braindances | Off | 30% | Wave |
| Scenes | Other mods | On | 80% | Swell |

</details>

## Scenes

- **Romance scenes:** Judy, Kerry, Panam, River and Meredith Stout each have their own track.
  **Follow the scene** uses each scene's timeline and accents instead of the old generic
  two-minute build. Panam also uses live pulses from the tank's neural-link sequence.
- **Joytoys:** Jig-Jig Street and Heywood scenes restart their track at each clip. The feel
  recreates the game's heavy/light rumble and pause pattern; it does not read native rumble
  output directly.
- **Censored scenes:** Judy, Kerry and Panam automatically use shorter tracks when the game's
  censorship fact selects that cut. River and Stout use their normal tracks.
- Scene-end signals fade the track over 1.5 seconds. Existing romance or relationship facts are
  treated as a baseline when sampling starts, so loading a save after a romance does not replay it.
  Romance scenes and Joytoys both default to On, 80% strength and **Follow the scene**. You can
  choose another feel for either interaction.

## Troubleshooting

- **Device not found:** close its phone app, which holds the connection. Then press **Scan** under
  Devices. On Linux, Bluetooth must be on (BlueZ).
- **`start.cmd` says Python is missing:** install it from python.org, and keep the "py launcher"
  option ticked.
- **Old romance feel after updating:** choose **Follow the scene** for Romance scenes, or reset
  the interactions. Saved choices take precedence over new defaults.
- **Logs:** `%LOCALAPPDATA%\NightCityHaptics\` on Windows, or `~/.local/share/night-city-haptics/`
  on Linux.

## Privacy

The control page is served only on `127.0.0.1:19870`; its controls reject cross-site requests.
The app has no telemetry and no accounts. By default, its server connection stays on your PC.
The optional engine download comes from Intiface's official GitHub release only when you click
Download engine. If you set a remote Intiface address, the app connects to that server.

## For mod authors

Other CET mods can send a pulse through the **Other mods** interaction:

```lua
GetMod("nightCityHaptics").Pulse(0.8, 2)   -- strength 0–1, seconds (optional)
```

## How it works

The CET mod (`init.lua`) only observes the game and never talks to hardware. It samples about
30 times a second, rounds levels to two decimal places, and writes `state.json` when events or
state change, or every 0.2 seconds as a heartbeat. Unchanged state therefore needs five writes a
second rather than 30.

The companion (`nch.py`, pure Python standard library) reads that file, mixes the result into one
output and drives devices through the Buttplug protocol. Its loop runs at 30 Hz while the game is
live or a preview is playing, and 10 Hz when idle. The control page reuses HTTP connections, and
`status.json` is written only on change or once a second.

Game-driven output requires a heartbeat whose sequence advanced within the last second. A file
left over from an earlier session is only a baseline; it cannot replay old events. Game feedback
stops when the heartbeat expires or the gameplay gate closes. Deliberate previews are separate,
as described above.

## Development

Tests need LuaJIT and `websockets>=15,<17`. Linux E2E also needs
Intiface Engine, `unshare`, `ip`, and working user/network namespaces.

```sh
luajit tests/cet_harness.lua                  # the real init.lua against a stubbed CET + game
python3 -m pip install 'websockets>=15,<17'     # tests only; no extra runtime packages
python3 -m unittest discover -s tests
python3 tools/build.py                        # builds dist/ (game-relative ZIP + manifest)
python3 tools/e2e_intiface.py                 # Linux: real Intiface Engine + simulated device
```

## Status

This is a prototype (0.2.4). The current functionality passed offline tests and an end-to-end run
through the real Intiface Engine with a simulated device. The added signals and scene timing
have not yet been playtested in-game, and physical feedback remains unverified. Please open an
issue if something misfires.

Built on [Buttplug / Intiface](https://buttplug.io) by Nonpolynomial.
