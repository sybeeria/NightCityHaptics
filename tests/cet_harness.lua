-- Run: luajit tests/cet_harness.lua   (from the repository root)
-- Drives the real CET init.lua with stubbed CET/game APIs and in-memory files.
local root = arg[0]:gsub("/?tests/cet_harness.lua$", "")
if root == "" then root = "." end
local passed = 0
local function check(condition, message)
  passed = passed + 1
  if not condition then error("FAIL: " .. message, 2) end
end

-- This permits real unified default JSON and real serialized telemetry to be checked.
local function encode(value)
  local kind = type(value)
  if kind == "nil" then return "null" end
  if kind == "number" then
    assert(value == value and value ~= math.huge and value ~= -math.huge, "JSON rejects nonfinite numbers")
    return tostring(value)
  end
  if kind == "boolean" then return tostring(value) end
  if kind == "string" then
    return '"' .. value:gsub('[\\"%z\1-\31]', function(c)
      local special = {['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t'}
      return special[c] or string.format('\\u%04x', c:byte())
    end) .. '"'
  end
  assert(kind == "table", "cannot encode " .. kind)
  -- CET's JSON binding serializes an empty Lua table as [], including levels
  -- and the cleared scene object; consumers must treat an empty collection as absent.
  local keys, array = {}, true
  for key in pairs(value) do keys[#keys + 1] = key; if type(key) ~= "number" then array = false end end
  local parts = {}
  if array then for index = 1, #value do parts[#parts + 1] = encode(value[index]) end; return '[' .. table.concat(parts, ',') .. ']' end
  table.sort(keys)
  for _, key in ipairs(keys) do parts[#parts + 1] = encode(tostring(key)) .. ':' .. encode(value[key]) end
  return '{' .. table.concat(parts, ',') .. '}'
end
local function decode(text)
  local position, parse = 1
  local function whitespace() local _, last = text:find("^%s*", position); position = (last or position - 1) + 1 end
  local function quoted()
    assert(text:sub(position, position) == '"'); position = position + 1
    local result = {}
    while position <= #text do
      local character = text:sub(position, position); position = position + 1
      if character == '"' then return table.concat(result) end
      if character == '\\' then
        character = text:sub(position, position); position = position + 1
        local escapes = {['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b', f = '\f', n = '\n', r = '\r', t = '\t'}
        if character == 'u' then
          local hex = text:sub(position, position + 3); position = position + 4
          local code = assert(tonumber(hex, 16)); assert(code < 128, "harness JSON unicode outside ASCII"); character = string.char(code)
        else character = assert(escapes[character], "invalid escape") end
      end
      result[#result + 1] = character
    end
    error("unterminated string")
  end
  parse = function()
    whitespace(); local character = text:sub(position, position)
    if character == '"' then return quoted() end
    if character == '[' or character == '{' then
      local object = character == '{'; local close = object and '}' or ']'; local result = {}
      position = position + 1; whitespace()
      if text:sub(position, position) == close then position = position + 1; return result end
      while true do
        local key
        if object then key = quoted(); whitespace(); assert(text:sub(position, position) == ':'); position = position + 1 end
        local value = parse()
        if object then result[key] = value else result[#result + 1] = value end
        whitespace(); character = text:sub(position, position); position = position + 1
        if character == close then return result end
        assert(character == ',', "expected comma"); whitespace()
      end
    end
    for word, value in pairs({['true'] = true, ['false'] = false}) do
      if text:sub(position, position + #word - 1) == word then position = position + #word; return value end
    end
    if text:sub(position, position + 3) == 'null' then position = position + 4; return nil end
    local number = text:match('^-?%d+%.?%d*[eE]?[+-]?%d*', position)
    assert(number and tonumber(number), "invalid value at " .. position)
    position = position + #number; return tonumber(number)
  end
  local value = parse(); whitespace(); assert(position > #text, "trailing data"); return value
end
json = {encode = encode, decode = decode}

-- In-memory files ---------------------------------------------------------------
local files, writes = {}, 0
io.open = function(path, mode)
  if mode == "w" then
    if path == "state.json" then writes = writes + 1 end
    local buffer = {}
    return {write = function(_, text) buffer[#buffer + 1] = text; return true end,
            close = function() files[path] = table.concat(buffer); return true end}
  end
  if files[path] == nil then return nil end
  local text = files[path]
  return {read = function() return text end, close = function() return true end}
end
local function state() return json.decode(files["state.json"]) end

-- Enums: Enum.new(type, name) -> ordinal, as CET's EnumInt returns.
local enums = {
  gamePSMLocomotionStates = {"Default", "Crouch", "Sprint", "Kereznikov", "Jump", "Vault", "Dodge", "DodgeAir", "Workspot", "Slide", "SlideFall", "CrouchSprint", "CrouchDodge"},
  gamePSMDetailedLocomotionStates = {"NotInBaseLocomotion", "Stand", "AimWalk", "Crouch", "Sprint", "Slide", "SlideFall", "Dodge", "Climb", "Vault", "Ladder", "LadderSprint", "LadderSlide", "LadderJump", "Fall", "AirThrusters", "AirHover", "SuperheroFall", "Jump", "DoubleJump", "ChargeJump", "HoverJump", "DodgeAir", "RegularLand", "HardLand", "VeryHardLand", "DeathLand", "SuperheroLand", "SuperheroLandRecovery", "Knockdown", "CrouchSprint", "Felled"},
  gamePSMLandingState = {"Default", "RegularLand", "HardLand", "VeryHardLand", "DeathLand", "SuperheroLand", "SuperheroLandRecovery"},
  gamePSMMeleeWeapon = {"NotReady", "Equipping", "Idle", "Safe", "PublicSafe", "Parried", "Hold", "ChargedHold", "Block", "Targeting", "Deflect", "ComboAttack", "FinalAttack", "StrongAttack", "SafeAttack", "BlockAttack", "SprintAttack", "CrouchAttack", "JumpAttack", "ThrowAttack", "DeflectAttack", "EquipAttack", "Default"},
  gamePSMTakedown = {"Default", "EnteringGrapple", "Grapple", "Leap", "Takedown"},
  gamePSMRangedWeaponStates = {"Default", "Charging", "Reload", "QuickMelee", "NoAmmo", "Ready", "Safe", "Overheat", "Shoot"},
  gamePSMSwimming = {"Default", "Surface", "Diving", "Climbing"},
  questPhoneCallPhase = {"Undefined", "IncomingCall", "StartCall", "EndCall"},
  EInkAnimationPlaybackOption = {"PLAY", "STOP", "PAUSE", "RESUME", "CONTINUE", "GO_TO_START", "GO_TO_END"},
  gamedataItemType = {"Clo_Face", "Clo_Feet", "Clo_Head", "Clo_InnerChest", "Clo_Legs", "Clo_OuterChest", "Clo_Outfit", "Con_Ammo", "Con_Edible", "Con_Inhaler", "Con_Injector", "Con_LongLasting", "Con_Skillbook", "Cyb_Ability", "Cyb_HealingAbility", "Cyb_Launcher", "Cyb_MantisBlades", "Cyb_NanoWires", "Cyb_StrongArms", "Cyberware", "CyberwareStatsShard", "CyberwareUpgradeShard", "Fla_Launcher", "Fla_Rifle", "Fla_Shock", "Fla_Support", "Gad_Grenade", "Gen_CraftingMaterial", "Gen_DataBank", "Gen_Jewellery", "Gen_Junk", "Gen_Keycard", "Gen_Misc", "Gen_MoneyShard", "Gen_Readable", "Gen_Tarot", "GrenadeDelivery", "Grenade_Core", "Prt_AR_SMG_LMGMod", "Prt_BladeMod", "Prt_BluntMod", "Prt_BootsFabricEnhancer", "Prt_Capacitor", "Prt_FabricEnhancer", "Prt_FaceFabricEnhancer", "Prt_Fragment", "Prt_HandgunMod", "Prt_HandgunMuzzle", "Prt_HeadFabricEnhancer", "Prt_LongScope", "Prt_Magazine", "Prt_MeleeMod", "Prt_Mod", "Prt_Muzzle", "Prt_OuterTorsoFabricEnhancer", "Prt_PantsFabricEnhancer", "Prt_PowerMod", "Prt_PowerSniperScope", "Prt_Precision_Sniper_RifleMod", "Prt_Program", "Prt_RangedMod", "Prt_Receiver", "Prt_RifleMuzzle", "Prt_Scope", "Prt_ScopeRail", "Prt_ShortScope", "Prt_ShotgunMod", "Prt_SmartMod", "Prt_Stock", "Prt_TargetingSystem", "Prt_TechMod", "Prt_TechSniperScope", "Prt_ThrowableMod", "Prt_TorsoFabricEnhancer", "VendorToken", "Wea_AssaultRifle", "Wea_Axe", "Wea_Chainsword", "Wea_Fists", "Wea_GrenadeLauncher", "Wea_Hammer", "Wea_Handgun", "Wea_HeavyMachineGun", "Wea_Katana", "Wea_Knife", "Wea_LightMachineGun", "Wea_LongBlade", "Wea_Machete", "Wea_Melee", "Wea_OneHandedClub", "Wea_PrecisionRifle", "Wea_Revolver", "Wea_Rifle", "Wea_ShortBlade", "Wea_Shotgun", "Wea_ShotgunDual", "Wea_SniperRifle", "Wea_SubmachineGun", "Wea_Sword", "Wea_TwoHandedClub", "Wea_VehicleMissileLauncher", "Wea_VehiclePowerWeapon", "Count", "Invalid"},
}
local function ordinal(typeName, name)
  for index, value in ipairs(enums[typeName]) do if value == name then return index - 1 end end
  error("unknown enum " .. typeName .. "." .. name)
end
Enum = {new = function(t, n) return {t = t, n = n} end}
EnumInt = function(e) return ordinal(e.t, e.n) end

-- World stub ------------------------------------------------------------------------
local world = {}
local function reset()
  world = {attached = true, dead = false, pregame = false, paused = false, menu = false, photo = false,
    psm = {Locomotion = 0, LocomotionDetailed = 1, Landing = 0, MeleeWeapon = 2, Takedown = 0, Weapon = 0, Swimming = 0},
    phone = nil, oxygen = 100, simTime = 0,
    mounted = false, combat = false, heat = 0, health = 100, effects = {}, uploads = {}, facts = {},
    speed = 0, gear = 1, handbrake = 0, slip = 0, braindance = false, weapon = "Wea_Shotgun",
    position = {x = 0, y = 0, z = 0}, levels = {Level = 10, Espionage = 0}}
end
reset()
local playerID = {hash = 42}
local player = {
  GetEntityID = function() return playerID end, IsAttached = function() return world.attached end,
  IsDeadNoStatPool = function() return world.dead end, IsInCombat = function() return world.combat end,
  GetWorldPosition = function() return world.position end, GetGame = function() return {} end,
  GetPreventionSystem = function() return {GetHeatStageAsInt = function() return world.heat end} end,
  GetPlayerStateMachineBlackboard = function() return {
    GetInt = function(_, key) return world.psm[key] or 0 end,
    GetBool = function(_, key) if key == "MountedToVehicle" then return world.mounted end; return false end,
    GetVariant = function() return world.uploads end} end,
}
for _, name in ipairs({"GetEntityID", "IsAttached", "IsDeadNoStatPool", "IsInCombat", "GetWorldPosition", "GetGame", "GetPreventionSystem", "GetPlayerStateMachineBlackboard"}) do
  local f = player[name]; player[name] = function(self, ...) return f(...) end
end
local other = {GetEntityID = function() return {hash = 7} end}
local defs = setmetatable({}, {__index = function(t, board)
  local value = setmetatable({}, {__index = function(_, key) return key end})
  rawset(t, board, value); return value
end})
Game = {
  GetPlayer = function() return world.attached and player or nil end,
  GetAllBlackboardDefs = function() return defs end,
  GetBlackboardSystem = function() return {Get = function(_, board) return {GetBool = function(_, key)
    if key == "IsInMenu" then return world.menu end
    if key == "IsActive" then return board == defs.Braindance and world.braindance or board == defs.PhotoMode and world.photo or false end
    return false end,
    GetVariant = function(_, key) if key == "PhoneCallInformation" then return world.phone end end} end} end,
  GetTimeSystem = function() return {GetSimTime = function() return world.simTime end, IsPausedState = function() return world.paused end, IsTimeDilationActive = function() return false end} end,
  GetStatPoolsSystem = function() return {GetStatPoolValue = function(_, _, kind) return kind == "Oxygen" and world.oxygen or world.health end, GetStatPoolMaxPointValue = function() return 400 end} end,
  GetStatusEffectSystem = function() return {
    HasStatusEffectOfTypeName = function(_, _, name) return world.effects[name] == true end,
    HasStatusEffect = function(_, _, id) return world.effects[id] == true end,
    HasStatusEffectWithTag = function(_, _, tag) return world.effects[tag] == true end} end,
  GetScriptableSystemsContainer = function() return {Get = function() return {GetProficiencyLevel = function(_, _, kind) return world.levels[kind] or 1 end} end} end,
  GetMountingFacility = function() return {GetMountingInfoSingleWithIds = function() return {parentId = 1} end} end,
  FindEntityByID = function() return {GetBlackboard = function() return {
    GetInt = function(_, key) if key == "GearValue" then return world.gear end; return world.handbrake end,
    GetFloat = function() return world.slip end} end} end,
  GetQuestsSystem = function() return {GetFactStr = function(_, fact) return world.facts[fact] or 0 end} end,
}
GetSingleton = function() return {GetSystemRequestsHandler = function() return {IsPreGame = function() return world.pregame end} end} end
TweakDBID = {new = function(s) return s end}
CName = {new = function(s) return s end}
FromVariant = function(v) return v end
EngineTime = {ToDouble = function(time) return time end}
NameToString = function(v) return v end
ItemID = {IsValid = function() return true end, IsOfTDBID = function(item, id) return item == id end}
gamedataStatPoolType = {Health = "Health", Oxygen = "Oxygen"}
gamedataProficiencyType = setmetatable({}, {__index = function(_, k) return k end})
gameJournalEntryState = {Succeeded = "Succeeded", Failed = "Failed"}
VehicleComponent = {GetOwnerVehicleSpeed = function() return world.speed end}
ScriptedPuppet = {GetActiveWeapon = function() return {GetItemID = function() return world.weapon end} end}
WeaponObject = {GetWeaponType = function(id) return Enum.new("gamedataItemType", id) end}
ImGuiCond, ImGuiWindowFlags = {FirstUseEver = 0}, {AlwaysAutoResize = 0}
ImGuiCol = setmetatable({}, {__index = function(_, name) return name end})
ImGuiStyleVar = {WindowRounding = "WindowRounding", FrameRounding = "FrameRounding"}
local styleDepth = {color = 0, var = 0}
local drawn = {}
ImGui = setmetatable({Begin = function() return true end, Button = function(label) drawn[#drawn + 1] = label; return false end,
  PushStyleColor = function(index) assert(type(index) == "string", "unknown ImGuiCol"); styleDepth.color = styleDepth.color + 1 end,
  PopStyleColor = function(n) styleDepth.color = styleDepth.color - (n or 1) end,
  PushStyleVar = function(index) assert(index, "unknown ImGuiStyleVar"); styleDepth.var = styleDepth.var + 1 end,
  PopStyleVar = function(n) styleDepth.var = styleDepth.var - (n or 1) end},
  {__index = function() return function() end end})

local events, hooks, hotkeys = {}, {}, {}
registerForEvent = function(name, callback) events[name] = callback end
registerHotkey = function(name, _, callback) hotkeys[name] = callback end
Observe = function(class, method, callback) hooks[class .. "." .. method] = callback end

package.path = root .. "/src/cet/nightCityHaptics/?.lua;" .. package.path
local mod = dofile(root .. "/src/cet/nightCityHaptics/init.lua")

local function update(dt)
  if not world.paused and not world.menu then world.simTime = world.simTime + dt end
  events.onUpdate(dt)
end
local function frames(seconds) for _ = 1, math.floor(seconds * 60 + .5) do update(1 / 60) end end
local function eventIds()
  local result = {}
  for _, e in ipairs(state().events) do result[#result + 1] = e[2] end
  return table.concat(result, ",")
end
local function fire(name, ...) hooks[name](...); update(1 / 60) end
local function last() local list = state().events; return list[#list] end

-- Start up -------------------------------------------------------------------------
events.onInit()
local s = state()
check(s.v == 2 and type(s.session) == "string" and s.seq >= 1, "onInit writes a v2 heartbeat")
check(hooks["ShootEvents.OnEnter"] and hooks["PlayerPuppet.OnTargetNeutraliziedEvent"], "hooks registered")
frames(.2)
s = state()
check(s.active == true and s.reason == "gameplay", "gameplay is active")
local seq = s.seq
frames(.3)
check(state().seq > seq, "the heartbeat sequence keeps advancing")
check(eventIds() == "", "no events on a quiet start")

-- Gunfire strength follows the weapon class; other players' shots are ignored.
fire("ShootEvents.OnEnter", nil, nil, {executionOwner = player})
check(last()[2] == "gunfire" and last()[3] == 1, "shotgun kicks at full strength")
world.weapon = "Wea_SubmachineGun"
fire("ShootEvents.OnEnter", nil, nil, {executionOwner = player})
check(last()[3] == .5, "SMG kicks softer")
local count = #state().events
fire("ShootEvents.OnEnter", nil, nil, {executionOwner = other})
check(#state().events == count, "NPC shots are ignored")
check(last()[1] > state().events[1][1], "event numbers increase")

-- Damage scales with the share of health lost, with a noticeable floor.
fire("PlayerPuppet.OnHitAnimation", player, {attackComputed = {GetTotalAttackValue = function() return 50 end}})
check(last()[2] == "hurt" and math.abs(last()[3] - .5) < 1e-6, "a 12.5% hit is half strength")
fire("PlayerPuppet.OnHitAnimation", player, {attackComputed = {GetTotalAttackValue = function() return 1 end}})
check(last()[3] == .3, "tiny hits still reach the 0.3 floor")
fire("PlayerPuppet.OnTargetNeutraliziedEvent", player, {})
check(last()[2] == "kill", "kills are reported")

-- Explosions fall off over 30 m and duplicate notifications merge.
world.position = {x = 0, y = 0, z = 0}
fire("AudioSystem.PlayShockwave", nil, "explosion", {x = 15, y = 0, z = 0})
check(last()[2] == "explosion" and math.abs(last()[3] - .5) < 1e-6, "15 m blast is half strength")
count = #state().events
fire("WeaponGrenade.OnExplosion", {GetWorldPosition = function() return {x = 15, y = 0, z = 0} end})
check(#state().events == count, "the same blast reported twice plays once")
fire("AudioSystem.PlayShockwave", nil, "explosion", {x = 45, y = 0, z = 0})
check(#state().events == count, "blasts beyond 30 m are ignored")

-- Movement edges: jump, double jump, dodge and landings by severity.
frames(.1)
world.psm.LocomotionDetailed = ordinal("gamePSMDetailedLocomotionStates", "Jump"); frames(.1)
check(last()[2] == "jump", "jump fires on entering the jump state")
count = #state().events
frames(.3)
check(#state().events == count, "holding the jump state does not repeat")
world.psm.LocomotionDetailed = ordinal("gamePSMDetailedLocomotionStates", "DoubleJump"); frames(.1)
check(last()[2] == "jump", "double jump fires again")
world.psm.LocomotionDetailed = ordinal("gamePSMDetailedLocomotionStates", "DodgeAir"); frames(.1)
check(last()[2] == "dodge", "air dash is a dodge")
world.psm.Landing = ordinal("gamePSMLandingState", "HardLand"); frames(.1)
check(last()[2] == "landing" and last()[3] == .7, "hard landing is 70%")
world.psm.Landing = 0; frames(.1)
world.psm.Landing = ordinal("gamePSMLandingState", "SuperheroLand"); frames(.1)
check(last()[3] == 1, "superhero landing is full strength")
world.psm.Locomotion = ordinal("gamePSMLocomotionStates", "Sprint"); frames(.1)
check(state().levels.sprint == 1, "sprint is a level")
world.psm.Locomotion = 0

-- Melee: each new attack state is a swing; finishers are stronger.
world.psm.MeleeWeapon = ordinal("gamePSMMeleeWeapon", "ComboAttack"); frames(.1)
check(last()[2] == "melee" and last()[3] == .7, "combo swing")
world.psm.MeleeWeapon = ordinal("gamePSMMeleeWeapon", "FinalAttack"); frames(.1)
check(last()[3] == 1, "final combo hit is full strength")

-- Combat, police and low health.
world.combat = true; frames(.1)
check(last()[2] == "combatStart", "combat start")
world.heat = 2; frames(.1)
check(last()[2] == "police", "police heat rising")
world.heat = 1; count = #state().events; frames(.1)
check(#state().events == count, "heat dropping is silent")
world.health = 20; frames(.1)
check(math.abs(state().levels.lowHealth - (.35 + .65 * .5)) < .006, "low health level grows below 40% (two decimals)")
world.health = 80; frames(.1)
check(state().levels.lowHealth == 0, "healthy is silent")

-- Cyberware and quickhacks.
world.effects.Sandevistan = true; world.effects.Berserk = true; world.uploads = {1}; frames(.1)
s = state()
check(s.levels.slowmo == 1 and s.levels.berserk == 1 and s.levels.quickhack == 1, "cyberware levels")
world.effects, world.uploads = {}, {}

-- Driving: engine idles in a car and builds with speed; gear changes click.
world.mounted, world.speed = true, 0; frames(.1)
check(math.abs(state().levels.engine - .15) < 1e-6, "engine idles at 15%")
world.speed = 40; world.handbrake = 1; frames(.1)
s = state()
check(s.levels.engine == 1 and s.levels.drift == .6, "full speed + handbrake drift")
world.gear = 2; frames(.1)
check(last()[2] == "gearShift", "gear shift")
fire("VehicleComponent.OnVehicleNotifyPassengersOfCollision", {GetVehicle = function() return {IsPlayerMounted = function() return true end} end})
check(last()[2] == "crash", "crash")
world.mounted, world.handbrake, world.speed = false, 0, 0; frames(.1)
check(state().levels.engine == 0, "engine stops when you get out")

-- Rewards.
fire("CurrencyChangeInventoryCallback.OnItemQuantityChanged", nil, "Items.money", 2500, nil, false)
check(last()[2] == "money" and last()[3] == 1, "a big payout is full strength")
fire("LevelUpNotificationQueue.OnCharacterLevelUpdated", nil, {type = "Level", lvl = 11})
check(last()[2] == "levelUp", "first level-up after loading counts (levels are seeded)")
count = #state().events
fire("LevelUpNotificationQueue.OnCharacterLevelUpdated", nil, {type = "Level", lvl = 11})
check(#state().events == count, "repeated level notification is ignored")
fire("JournalNotificationQueue.PushQuestNotification", nil, {}, "Succeeded")
check(last()[2] == "questDone", "quest completed")

-- Status effects, weapon states, calls and air.
world.effects = {Burning = true, EMP = true, Bleeding = true, JohnnySickness = true}; frames(.1)
s = state()
check(s.levels.burning == 1 and s.levels.shock == 1 and s.levels.poison == 1 and s.levels.relic == 1, "status levels")
world.effects.Knockdown = true; frames(.1)
check(last()[2] == "knockdown", "knockdown fires once")
count = last()[1]; frames(.2)
check(last()[1] == count, "staying knocked down does not repeat")
world.effects.Blind = true; frames(.1)
check(last()[2] == "flashed", "blinded")
world.effects = {}; frames(.1)
check(state().levels.burning == 0 and state().levels.relic == 0, "status levels clear")
world.psm.Weapon = ordinal("gamePSMRangedWeaponStates", "Charging"); frames(.1)
local charge = state().levels.charge
frames(1)
check(charge > .25 and state().levels.charge > charge, "charging builds up")
frames(1)
check(state().levels.charge == 1, "a full charge holds at 100%")
world.psm.Weapon = ordinal("gamePSMRangedWeaponStates", "Overheat"); frames(.1)
check(last()[2] == "overheat" and state().levels.charge == 0, "overheat fires, charge resets")
world.psm.Weapon = ordinal("gamePSMRangedWeaponStates", "Reload"); frames(.1)
check(last()[2] == "reload", "reload")
world.psm.Weapon = 0
world.phone = {isPlayerCalling = false, callPhase = Enum.new("questPhoneCallPhase", "IncomingCall")}; frames(.1)
check(state().levels.phoneRing == 1, "an incoming call rings")
world.phone.callPhase = Enum.new("questPhoneCallPhase", "StartCall"); frames(.1)
check(state().levels.phoneRing == 0, "answering stops the ring")
world.phone = {isPlayerCalling = true, callPhase = Enum.new("questPhoneCallPhase", "IncomingCall")}; frames(.1)
check(state().levels.phoneRing == 0, "your own outgoing call does not ring")
world.phone = nil
world.psm.Swimming = ordinal("gamePSMSwimming", "Diving"); world.oxygen = 60; frames(.1)
check(state().levels.oxygen == 0, "plenty of air is silent")
world.oxygen = 0; frames(.1)
check(state().levels.oxygen == 1, "no air left is full strength")
world.psm.Swimming = ordinal("gamePSMSwimming", "Surface"); frames(.1)
check(state().levels.oxygen == 0, "surfacing stops it")
world.psm.Swimming, world.oxygen = 0, 100

-- Romance: facts already set never fire; only the moment they change does.
local function idsSince(n)
  local result = {}
  for _, e in ipairs(state().events) do if e[1] > n then result[#result + 1] = e[2] end end
  return table.concat(result, ",")
end
local function lastNumber() return last() and last()[1] or 0 end
world.facts.sq027_panzer_sex = 1; world.facts.sq028_kerry_sex = 1
world.menu = true; frames(.1); world.menu = false; frames(.1)
local mark = lastNumber()
frames(.1)
check(idsSince(mark) == "", "facts already set when sampling (re)starts are only a baseline")
world.facts.sq028_kerry_sex = 2; frames(.1)
check(last()[2] == "romance:kerry", "Kerry: a rising kerry_sex counter starts her track")
world.facts.censorship_oversexualized = 1; world.facts.sq028_kerry_sex = 3; frames(.1)
check(last()[2] == "romance:kerryCensored", "censored builds get the censored cut")
world.facts.censorship_oversexualized = 0
world.facts.sq030_sex_done = 1; frames(.1)
check(last()[2] == "romance:judy", "Judy")
world.facts.sq029_river_had_sex = 1; frames(.1)
check(last()[2] == "romance:river", "River")
fire("InvisibleSceneStash.OnQuestDressPlayer", {}, {})
check(last()[2] == "romance:end", "dressing ends a playing scene")
mark = lastNumber()
fire("InvisibleSceneStash.OnQuestUndressPlayer", {}, {}); fire("InvisibleSceneStash.OnQuestDressPlayer", {}, {})
check(idsSince(mark) == "", "a shower (undress/dress with nothing armed) is silent")
world.facts.q003_stout_sex_agreed = 1; frames(.1)
check(idsSince(mark) == "", "Stout's agreement only arms her scene")
fire("InvisibleSceneStash.OnQuestUndressPlayer", {}, {})
check(last()[2] == "romance:stout", "Stout: undressing starts the clip chain")
fire("InvisibleSceneStash.OnQuestDressPlayer", {}, {})
check(last()[2] == "romance:end", "Stout ends on dress")
-- Panam: her full scene waits for the neural link; each flash is a pulse.
world.facts.sq027_panzer_sex = 0; frames(.1); world.facts.sq027_panzer_sex = 1; frames(.1)
mark = lastNumber()
fire("InvisibleSceneStash.OnQuestUndressPlayer", {}, {})
check(idsSince(mark) == "", "Panam is not started by undressing")
local function hud(animation, option)
  fire("GameObject.OnCustomUIAnimationEvent", player, {libraryItemName = "sex_hud", animationName = animation,
    playbackOption = Enum.new("EInkAnimationPlaybackOption", option)})
end
hud("random", "PLAY")
check(idsSince(mark) == "romance:panam,romance:pulse" and last()[3] == .65, "first flash starts Panam's track and pulses")
hud("MAIN", "PLAY")
check(last()[2] == "romance:pulse" and last()[3] == 1, "MAIN flashes are full pulses")
count = last()[1]
hud("random", "STOP")
check(last()[1] == count, "a stopped flash is not a pulse")
hud("synchronizing", "STOP")
check(last()[1] == count, "a stopped synchronizing animation is not a pulse")
hud("synchronizing", "PLAY")
check(last()[2] == "romance:pulse" and last()[5] == 1.5, "playing synchronizing is the long peak")
count = last()[1]
fire("GameObject.OnCustomUIAnimationEvent", other, {libraryItemName = "sex_hud", animationName = "MAIN",
  playbackOption = Enum.new("EInkAnimationPlaybackOption", "PLAY")})
fire("GameObject.OnCustomUIAnimationEvent", player, {libraryItemName = "other_hud", animationName = "MAIN",
  playbackOption = Enum.new("EInkAnimationPlaybackOption", "PLAY")})
check(last()[1] == count, "other objects and other overlays are ignored")
-- Joytoys: every clip boundary is a beat; the stop fact ends the encounter.
world.facts.hey_gle_female_rich_sex_clip = 0; world.facts.hey_gle_prostittute_female_stop_rumble = 1; frames(.1)
world.facts.hey_gle_female_rich_sex_clip = 1; frames(.1)
check(last()[2] == "joytoy", "first clip")
frames(2); world.facts.hey_gle_female_rich_sex_clip = 2; frames(.1)
check(last()[2] == "joytoy", "next clip")
world.facts.hey_gle_female_rich_sex_clip = 0; frames(.1)
check(last()[2] == "joytoy", "the counter's reset is a clip boundary too")
world.facts.hey_gle_prostittute_female_stop_rumble = 2; frames(.1)
check(last()[2] == "joytoy:end", "stop_rumble ends the encounter")

-- Gate: overlay, menus and pause are silent; levels clear; Test buzz still works.
events.onOverlayOpen()
s = state()
check(s.active == false, "overlay makes output inactive")
count = #s.events
fire("ShootEvents.OnEnter", nil, nil, {executionOwner = player})
check(#state().events == count, "no gameplay events with the overlay open")
for _, label in ipairs(drawn) do drawn[_] = nil end
events.onDraw()
check(#drawn == 2, "overlay has two buttons (pause, test)")
check(styleDepth.color == 0 and styleDepth.var == 0, "overlay pops every style it pushes")
hotkeys.nchTest()
check(last()[2] == "test", "test buzz works with the overlay open")
events.onOverlayClose(); frames(.1)
world.menu = true; frames(.1)
check(state().active == false and state().reason == "menu" and next(state().levels) == nil, "menus are silent and clear levels")
world.menu = false; frames(.1)
check(state().active == true, "back to gameplay")
hotkeys.nchPause()
check(state().paused == true, "pause hotkey")
hotkeys.nchPause()
check(state().paused == false, "resume hotkey")

-- Performance: a steady game writes the 5-per-second heartbeat, not one file per sample.
frames(2.5); writes = 0; frames(1)
check(writes >= 4 and writes <= 6, "steady gameplay writes state.json about 5 times a second (" .. writes .. ")")
world.mounted = true
for _ = 1, 60 do world.speed = (world.speed + 3) % 40; events.onUpdate(1 / 60) end
check(writes > 15, "changing levels are written as they change")
world.mounted, world.speed = false, 0; frames(.1)

-- Old events age out; the API works for other mods.
frames(2.5)
check(eventIds() == "", "events older than two seconds are dropped")
local pulseAccepted = mod.Pulse(.4, 3); frames(.05)
check(pulseAccepted == true and last()[2] == "external" and last()[3] == .4 and last()[5] == 3, "Pulse API")

-- Regression checks keep one realistic game world per scenario.
local function fresh(configure)
  events.onOverlayClose(); reset()
  if configure then configure() end
  events.onInit(); frames(.1)
end

for _, entry in ipairs({
    {"sq030_sex_done", "judy", false}, {"sq030_sex_done", "judyCensored", true},
    {"sq028_kerry_sex", "kerry", false}, {"sq028_kerry_sex", "kerryCensored", true},
    {"sq029_river_had_sex", "river", false}, {"sq027_panzer_sex", "panamCensored", true}}) do
  fresh(function() world.facts.censorship_oversexualized = entry[3] and 1 or 0 end)
  world.facts[entry[1]] = 1; frames(.1)
  check(state().scene.name == entry[2] and last()[2] == "romance:" .. entry[2], "exact catalog scene variant: " .. entry[2])
end

fresh()
world.effects["BaseStatusEffect.PlayerInFinisherWorkspot"] = true; frames(.1)
check(last()[2] == "takedown", "ordinary and fast finisher buff starts the takedown interaction")
mark = lastNumber(); frames(.5)
check(idsSince(mark) == "", "one finisher produces only one pulse")
fresh(function() world.effects["BaseStatusEffect.PlayerInFinisherWorkspot"] = true end)
check(eventIds() == "", "loading during a finisher establishes a baseline")

fresh()
world.levels.Espionage = 1
fire("LevelUpNotificationQueue.OnCharacterLevelUpdated", nil, {type = "Espionage", lvl = 1})
check(last()[2] == "levelUp", "first Relic progression counts after its zero baseline")
mark = lastNumber(); world.levels.Espionage = 2
fire("LevelUpNotificationQueue.OnCharacterLevelUpdated", nil, {type = "Espionage", lvl = 1})
check(idsSince(mark) == "levelUp", "Relic notification delta does not hide successive progression")
mark = lastNumber()
fire("LevelUpNotificationQueue.OnCharacterLevelUpdated", nil, {type = "Espionage", lvl = 1})
check(idsSince(mark) == "", "repeated Relic notification without progression is ignored")

fresh(); events.onOverlayOpen()
world.facts.sq030_sex_done = 1; world.combat = true
world.psm.LocomotionDetailed = ordinal("gamePSMDetailedLocomotionStates", "Jump")
frames(.1); events.onOverlayClose(); frames(.1)
check(eventIds() == "" and next(state().scene) == nil, "overlay reopening baselines facts, combat and movement")

fresh(); world.facts.sq030_sex_done = 1; frames(.1)
local scene = state().scene
check(scene.name == "judy" and scene.instance > 0 and scene.elapsed >= 0, "scene telemetry identifies its track and instance")
local sceneInstance, elapsed = scene.instance, scene.elapsed
world.menu = true; world.paused = true; frames(.1)
elapsed = state().scene.elapsed; frames(2)
check(state().scene.instance == sceneInstance and math.abs(state().scene.elapsed - elapsed) < .04,
  "native game pause preserves the scene at its simulation offset")
mark = lastNumber(); world.menu = false; world.paused = false; frames(.1)
check(state().scene.instance == sceneInstance and idsSince(mark) == "", "menu resume publishes the existing scene without replaying its fact")
elapsed = state().scene.elapsed; hotkeys.nchPause(); frames(2)
check(state().scene.elapsed > elapsed + 1.9 and state().scene.instance == sceneInstance,
  "CET feedback Pause keeps pace with a scene that continues in the game")
hotkeys.nchPause(); elapsed = state().scene.elapsed; events.onOverlayOpen(); frames(1)
check(state().scene.elapsed > elapsed + .9, "overlay suspension retains the current simulation offset")
events.onOverlayClose(); frames(.1)
check(state().scene.instance == sceneInstance, "overlay resume retains the same encounter")
fire("InvisibleSceneStash.OnQuestDressPlayer", {}, {})
check(next(state().scene) == nil and last()[2] == "romance:end", "dress explicitly clears resumable scene telemetry")
world.facts.sq030_sex_done = 0; frames(.1); world.facts.sq030_sex_done = 1; frames(.1)
check(state().scene.instance > sceneInstance, "another anchored encounter gets a new instance")
world.simTime = world.simTime + 150; frames(.1)
check(next(state().scene) == nil, "a bounded scene expires even without dress")

fresh()
hud("random", "STOP")
check(eventIds() == "" and next(state().scene) == nil, "a Panam STOP cannot create a scene")
hud("synchronizing", "STOP")
check(eventIds() == "", "an isolated synchronizing STOP cannot invent an encounter")
events.onOverlayOpen(); hud("MAIN", "PLAY"); frames(1)
check(eventIds() == "" and next(state().scene) == nil, "gated live Panam cue waits without emitting output")
events.onOverlayClose(); frames(.1)
check(state().scene.name == "panam" and state().scene.elapsed > .9 and last()[2] == "romance:panam",
  "verified gated Panam anchor resumes once at its actual offset")
mark = lastNumber(); hud("MAIN", "PLAY")
check(idsSince(mark) == "romance:pulse", "later Panam cue accents the restored base track")
mark = lastNumber(); hud("synchronizing", "STOP"); hud("MAIN", "STOP")
check(idsSince(mark) == "" and state().scene.name == "panam", "active-scene STOP cues neither pulse nor replace its track")

fresh(); world.facts.q003_stout_sex_agreed = 1; frames(.1)
events.onOverlayOpen(); fire("InvisibleSceneStash.OnQuestUndressPlayer", {}, {}); frames(1)
events.onOverlayClose(); frames(.1)
check(state().scene.name == "stout" and state().scene.elapsed > .9,
  "a verified armed undress cue also survives output suspension")
fresh(); fire("InvisibleSceneStash.OnQuestUndressPlayer", {}, {})
check(next(state().scene) == nil and eventIds() == "", "an unarmed shower never becomes a pending scene")
fresh(); world.facts.q003_stout_sex_agreed = 1; frames(.1)
world.paused = true; frames(121); world.paused = false; frames(.1)
fire("InvisibleSceneStash.OnQuestUndressPlayer", {}, {})
check(state().scene.name == "stout", "native pause does not consume an armed scene's bounded game-time window")
fresh(); world.facts.q003_stout_sex_agreed = 1; frames(.1); frames(121)
fire("InvisibleSceneStash.OnQuestUndressPlayer", {}, {})
check(next(state().scene) == nil and eventIds() == "", "an expired Stout arm cannot start an unrelated undress")

fresh(); world.facts.sq030_sex_done = 1; frames(.1)
mark = lastNumber()
fire("LoadingScreenProgressBarController.OnInitialize", {})
check(not state().active and next(state().scene) == nil, "loading clears the current live encounter")
world.combat = true; world.psm.LocomotionDetailed = ordinal("gamePSMDetailedLocomotionStates", "Jump")
fire("LoadingScreenProgressBarController.SetProgress", {}, 1)
check(next(state().scene) == nil and idsSince(mark) == "", "save facts stay baselines after loading")
mark = lastNumber(); frames(.1)
check(idsSince(mark) == "", "loading recovery baselines movement and combat")
world.facts.sq030_sex_done = 0; frames(.1); world.facts.sq030_sex_done = 1; frames(.1)
world.dead = true; fire("PlayerPuppet.OnDeath", player)
check(not state().active and next(state().scene) == nil, "death clears resumable encounter telemetry")

fresh(); local oldClock = EngineTime.ToDouble
EngineTime.ToDouble = function() return 0 / 0 end
world.facts.sq030_sex_done = 1; frames(.4)
check(state().scene.name == "judy" and state().scene.elapsed > 0 and next(state().errors) == nil,
  "an invalid simulation reading falls back to finite active elapsed time")
hotkeys.nchPause(); elapsed = state().scene.elapsed; frames(1)
check(math.abs(state().scene.elapsed - elapsed) < .04, "fallback timing remains frozen when feedback is paused")
EngineTime.ToDouble = oldClock

fresh(); local before = state().seq
check(mod.Pulse(.4, 0 / 0) == false and mod.Pulse(.4, math.huge) == false and mod.Pulse(.4, -math.huge) == false,
  "nonfinite external durations are rejected before they reach JSON")
frames(1.2)
check(state().seq > before and next(state().errors) == nil, "invalid durations cannot stall the heartbeat")
check(mod.Pulse(.4, 1000) and last()[5] == 600, "finite durations retain the documented upper bound")

fresh(); world.braindance = true; frames(.1)
check(state().active and state().levels.braindance == 1, "braindance stub independently exercises its own board")
world.photo = true; frames(.1)
check(not state().active, "braindance and photo mode are separate gates")

fresh()
for _, name in ipairs({"ComboAttack", "FinalAttack", "StrongAttack", "SafeAttack", "BlockAttack", "SprintAttack",
    "CrouchAttack", "JumpAttack", "ThrowAttack", "DeflectAttack", "EquipAttack"}) do
  world.psm.MeleeWeapon = ordinal("gamePSMMeleeWeapon", name); frames(.1)
  check(last()[2] == "melee", "actual vanilla attack ordinal works: " .. name)
end
world.psm.MeleeWeapon = ordinal("gamePSMMeleeWeapon", "Idle"); frames(.1)
check(next(state().errors) == nil, "all vanilla melee enum paths, including idle, stay valid")

fresh()
local textLines, oldText, oldColored = {}, ImGui.Text, ImGui.TextColored
ImGui.Text = function(text) textLines[#textLines + 1] = text end
ImGui.TextColored = function(_, _, _, _, text) textLines[#textLines + 1] = text end
files["status.json"] = json.encode({running = true, heartbeat = 1, devices = {}, output = 0})
frames(.6); events.onOverlayOpen()
for heartbeat = 2, 5 do
  files["status.json"] = json.encode({running = true, heartbeat = heartbeat, devices = {}, output = 0})
  frames(1)
end
events.onDraw()
check(table.concat(textLines, " "):find("Running", 1, true) ~= nil, "changing status heartbeat keeps a quiet app fresh")
frames(4); textLines = {}; events.onDraw()
check(table.concat(textLines, " "):find("App not running", 1, true) ~= nil, "a stopped status heartbeat correctly expires")
ImGui.Text, ImGui.TextColored = oldText, oldColored
events.onOverlayClose(); frames(.1)

-- An arm is cancelled by dressing too; it cannot leak into a later shower.
fresh(); world.facts.q003_stout_sex_agreed = 1; frames(.1)
mark = lastNumber()
fire("InvisibleSceneStash.OnQuestDressPlayer", {}, {})
fire("InvisibleSceneStash.OnQuestUndressPlayer", {}, {})
check(next(state().scene) == nil and idsSince(mark) == "", "dressing cancels an unplayed Stout arm without inventing an end event")

-- Invalid external magnitudes/durations and sensor values must remain silent.
fresh(); mark = lastNumber()
for _, strength in ipairs({0 / 0, math.huge, -math.huge, 0, -1, "0.5", false}) do
  check(mod.Pulse(strength, 1) == false, "invalid external strength is rejected: " .. tostring(strength))
end
check(mod.Pulse(nil, 1) == false and mod.Pulse(.5, 0) == false and mod.Pulse(.5, -1) == false,
  "Pulse requires a positive finite strength and duration")
frames(.1)
check(idsSince(mark) == "", "invalid external input does not emit a substitute buzz")
for _, position in ipairs({{x = 0 / 0, y = 0, z = 0}, {x = math.huge, y = 0, z = 0}}) do
  fire("AudioSystem.PlayShockwave", nil, "explosion", position)
end
check(idsSince(mark) == "", "invalid explosion coordinates cannot become a minimum-strength blast")
world.mounted, world.speed = true, math.huge; frames(.1)
check(state().levels.engine == 0 and state().levels.drift == 0, "an invalid vehicle speed cannot become full engine output")
world.speed, world.slip = 10, math.huge; frames(.1)
check(state().levels.engine > 0 and state().levels.drift == 0, "invalid slip remains silent while valid engine feedback continues")
world.psm.Swimming, world.oxygen = ordinal("gamePSMSwimming", "Diving"), -math.huge; frames(.1)
check(state().levels.oxygen == 0, "invalid oxygen cannot become full-strength feedback")

-- File boundary failures must be caught and later writes must recover.
fresh()
local normalOpen = io.open
for _, mode in ipairs({"open_nil", "open_throw", "write_nil", "write_throw", "close_nil", "close_throw"}) do
  local beforeFailure = state().seq
  io.open = function(path, openMode)
    if path ~= "state.json" or openMode ~= "w" then return normalOpen(path, openMode) end
    if mode == "open_nil" then return nil, "permission denied" end
    if mode == "open_throw" then error("permission denied") end
    return {
      write = function()
        if mode == "write_nil" then return nil, "disk full" end
        if mode == "write_throw" then error("disk full") end
        return true
      end,
      close = function()
        if mode == "close_nil" then return nil, "flush failed" end
        if mode == "close_throw" then error("flush failed") end
        return true
      end}
  end
  check(pcall(frames, .3), "telemetry " .. mode .. " does not escape the CET callback")
  check(state().seq == beforeFailure, "a failed telemetry write does not publish a false heartbeat: " .. mode)
  io.open = normalOpen; frames(.5)
  check(state().seq > beforeFailure and state().errors["state.json"] == nil,
    "telemetry heartbeat recovers after " .. mode)
end
io.open = function(path, mode)
  if path == "status.json" then error("unreadable status") end
  return normalOpen(path, mode)
end
check(pcall(frames, .6), "a status read failure cannot stall the CET heartbeat")
io.open = normalOpen; frames(.6)
check(state().errors["status.json"] == nil, "status read errors clear after the file recovers")

-- Malformed display fields cannot crash ImGui or refresh app liveness.
fresh()
local oldProgress, displayed = ImGui.ProgressBar
ImGui.ProgressBar = function(value) displayed = value end
files["status.json"] = json.encode({running = true, heartbeat = 101, devices = {"valid"}, output = 50})
frames(.6); events.onOverlayOpen(); events.onDraw()
check(displayed == 1, "finite status output is clamped before reaching ImGui")
for _, bad in ipairs({
    {running = true, heartbeat = 102, devices = {42}, output = .5},
    {running = true, heartbeat = 103, devices = "wrong", output = .5},
    {running = true, heartbeat = 104, devices = {}, output = "wrong"},
    {running = "wrong", heartbeat = 105, devices = {}, output = .5}}) do
  files["status.json"] = json.encode(bad); frames(4)
  check(pcall(events.onDraw) and displayed == 0, "invalid status display fields expire safely")
end
files["status.json"] = '{"running":true,"heartbeat":106,"devices":[],"output":1e999}'
frames(.6)
check(pcall(events.onDraw) and displayed == 0, "an infinite JSON output is rejected before rendering")
files["status.json"] = string.rep(" ", 65537)
check(pcall(frames, .6), "oversized status input stays bounded and cannot stall telemetry")
files["status.json"] = json.encode({running = true, heartbeat = 107, devices = {}, output = .4})
frames(.6); events.onDraw()
check(displayed == .4, "valid status recovers after malformed snapshots")
ImGui.ProgressBar = oldProgress; events.onOverlayClose(); frames(.1)

-- Sessions: a new player attachment starts a fresh session and clears its scene.
world.facts.sq030_sex_done = 1; frames(.1)
local session = state().session
hooks["PlayerPuppet.OnGameAttached"](player)
check(state().session ~= session and state().seq == 1 and next(state().scene) == nil, "new session on player attach clears scene telemetry")

-- Companion status shows in the overlay.
files["status.json"] = '{"v":2,"running":true,"devices":["Lovense Hush"],"output":0.3,"enabled":true}'
frames(.6)
events.onOverlayOpen()
check(true, "status read without errors")
check(next(state().errors) == nil, "no runtime errors: " .. (next(state().errors) or ""))
events.onOverlayClose(); frames(.1)
world.facts.sq030_sex_done = 0; frames(.1); world.facts.sq030_sex_done = 1; frames(.1)
events.onShutdown()
check(not state().active and next(state().scene) == nil, "shutdown clears the live scene before its final heartbeat")

print(string.format("cet_harness: %d checks passed", passed))
