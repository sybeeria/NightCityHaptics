# Night City Haptics

Cyberpunk 2077 haptics for any [Intiface](https://intiface.com)-compatible device (18+). Gunfire
kicks, hits thud, low health beats like a heart and engines rev. There are 47 interactions,
including romance and joytoy scene tracks. Each one has its own switch, feel and strength, which
you tune live on a local control page.

![Control page](docs/screenshot.png)

## You need

- Cyberpunk 2077 on PC (Windows, or Linux / SteamOS through Proton) with
  [Cyber Engine Tweaks](https://www.nexusmods.com/cyberpunk2077/mods/107).
- Python 3.9 or newer ([python.org](https://www.python.org/downloads/) on Windows). The app
  needs no other packages.
- A device with **Buttplug.io Support** in the [compatible-device index](https://iostindex.com/).

You don't need to install Intiface. The app downloads the official Intiface Engine once,
checks its checksum and starts it. If Intiface Central is already running on port 12345, the
app uses that instead.

## Setup

1. Download [`NightCityHaptics-0.3.0.zip`](dist/NightCityHaptics-0.3.0.zip) and extract it
   into the game folder (the one that contains `bin`).
2. In `bin/x64/plugins/cyber_engine_tweaks/mods/nightCityHaptics/companion/`, run **`start.cmd`**
   (Windows) or **`start.sh`** (Linux). The control page opens in your browser.
3. On first run, click **Download engine** in Settings. Then put your device in pairing mode.
4. Play. Feedback runs only during live gameplay, not in menus, loading screens, photo mode or
   the CET overlay.

**Optional, Steam on Linux/SteamOS:** to start the app with the game, save
[nch-steam-launch](https://raw.githubusercontent.com/sybeeria/NightCityHaptics/main/tools/nch-steam-launch)
in the game folder and set the game's Launch Options to:

```sh
/bin/sh "/path/to/Cyberpunk 2077/nch-steam-launch" -- %command%
```

Put existing environment overrides first, other wrappers after `--`, and game flags after
`%command%`. The app closes when the game does.

## Using it

- **Interactions:** set a switch, feel and strength for each interaction, or switch a whole
  group. The shape next to each feel shows how it plays. **Try** previews it without the game.
- **Max strength** caps the whole mix (default 70%). **On/Off** stops all of it. **Pause** in the
  CET overlay suspends game feedback; Pause and Test buzz can be bound in CET's Bindings.
- **Devices:** each device has a switch, strength, battery level and one button per output it
  has. **Test** sends a short pulse at half that device's strength.

| Output | What it does |
|---|---|
| Vibrate | follows the mix |
| Rotate | follows the mix; a sudden hit reverses the spin |
| Thrust | thrusting and oscillating motors follow the mix |
| Stroke | strokes faster and further as the mix rises |
| Squeeze | suction and air pumps ease toward the mix (off by default) |
| Heat | warms with sustained feedback, cools after (off by default) |
| Light | follows the mix |

A device with a vibrator starts with Vibrate and Light on. Other devices start with their motion
outputs and Light on.

- **Settings** holds the Intiface address (default `ws://127.0.0.1:12345`), engine auto-start and
  **Reset interactions**. The reset keeps your device and Intiface settings. Settings save
  automatically.
- **Quit** stops your devices and the engine the app started.

<details>
<summary>All 47 interactions and their defaults</summary>

Strength is a share of Max strength.

| Group | Interaction | Default |
|---|---|---|
| Combat | Gunfire | 45%, Kick |
|  | Getting hit | 90%, Thud |
|  | Low health | 60%, Heartbeat |
|  | Explosions | 100%, Shockwave |
|  | Melee swings | 45%, Kick |
|  | Enemy down | 45%, Double tap |
|  | Grabs & takedowns | 55%, Thud |
|  | Combat starts | 35%, Alarm |
|  | Charging up | 40%, Rumble |
|  | Reloads | 20%, Tap |
|  | Weapon overheat | 60%, Rumble burst |
|  | Blocks & parries | 45%, Kick |
|  | Grenade throws | 30%, Ramp |
|  | Hits landed | 20%, Tap, off |
| Status effects | Burning | 50%, Rumble |
|  | Electrocuted & EMP | 45%, Flutter |
|  | Poisoned & bleeding | 30%, Throb |
|  | Knocked down & stunned | 70%, Thud |
|  | Blinded | 50%, Rumble burst |
|  | Relic glitches | 50%, Glitch |
|  | Being hacked | 40%, Static |
| Cyberware | Sandevistan & Kerenzikov | 35%, Wave |
|  | Berserk | 55%, Build |
|  | Quickhack upload | 25%, Flutter |
|  | Optical camo | 20%, Wave, off |
| Movement | Sprinting | 20%, Footsteps |
|  | Jumps | 30%, Tap |
|  | Landings | 55%, Thud |
|  | Dodges & slides | 35%, Tap |
|  | Climbs & vaults | 30%, Kick |
|  | Out of stamina | 35%, Ripple |
|  | Running out of air | 50%, Heartbeat |
| Driving | Engine | 25%, Rumble |
|  | Crashes | 100%, Shockwave |
|  | Drifting | 45%, Rumble |
|  | Gear shifts | 20%, Tap, off |
| Rewards & world | Police heat | 60%, Alarm |
|  | Level up | 70%, Fanfare |
|  | Quest completed | 50%, Fanfare |
|  | Eddies received | 30%, Tap |
|  | Healing | 35%, Swell |
|  | Loot pickups | 15%, Tap, off |
|  | Incoming calls | 40%, Phone ring |
| Scenes | Romance scenes | 80%, Follow the scene |
|  | Joytoys | 80%, Follow the scene |
|  | Braindances | 30%, Wave, off |
|  | Other mods | 80%, Swell |

</details>

## Scenes

- **Romance:** Judy, Kerry, Panam, River and Meredith Stout each have their own track. Judy, Kerry
  and Panam switch to shorter tracks for the censored cut.
- **Joytoys:** Jig-Jig Street and Heywood restart their track at each clip, in the game's own
  rumble rhythm.
- Tracks fade out when a scene ends. Loading a save after a romance doesn't replay it.

## Troubleshooting

- **Device not found:** close its phone app, then press **Scan**. On Linux, turn Bluetooth on.
- **`start.cmd` says Python is missing:** install it from python.org with the "py launcher"
  option ticked.
- **Logs:** `%LOCALAPPDATA%\NightCityHaptics\` (Windows) or `~/.local/share/night-city-haptics/`
  (Linux).

## Privacy

The control page is served only on `127.0.0.1:19870` and rejects cross-site requests. There is no
telemetry and no account. The app connects to the internet only when you click Download engine,
which fetches Intiface's official GitHub release.

## For mod authors

Other CET mods can send a pulse through the **Other mods** interaction:

```lua
GetMod("nightCityHaptics").Pulse(0.8, 2)   -- strength 0–1, optional seconds
```

## Development

The CET mod only reads the game and writes `state.json`. The companion (`nch.py`, standard library
only) mixes that into device output over the Buttplug protocol. It stops when the game's heartbeat
does.

```sh
luajit tests/cet_harness.lua
python3 -m pip install 'websockets>=15,<17'          # tests only
python3 -m unittest discover -s tests
python3 tools/build.py --output-dir .build/candidate
python3 tools/e2e_intiface.py                        # Linux: real Intiface Engine + simulated device
```

## Status

Version 0.3.0. Gameplay, scene timing and physical devices still need wider playtesting.

Built on [Buttplug / Intiface](https://buttplug.io) by Nonpolynomial.
