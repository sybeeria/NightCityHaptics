-- Run: luajit tests/cet_harness.lua   (from the mod workshop folder)
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
  if kind == "boolean" or kind == "number" then return tostring(value) end
  if kind == "string" then
    return '"' .. value:gsub('[\\"%z\1-\31]', function(c)
      local special = {['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t'}
      return special[c] or string.format('\\u%04x', c:byte())
    end) .. '"'
  end
  assert(kind == "table", "cannot encode " .. kind)
  local keys, array = {}, #value > 0
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
local files = {}
io.open = function(path, mode)
  if mode == "w" then
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
  gamePSMDetailedLocomotionStates = {"NotInBaseLocomotion", "Stand", "AimWalk", "Crouch", "Sprint", "Slide", "SlideFall", "Dodge", "Climb", "Vault", "Ladder", "LadderSprint", "LadderSlide", "LadderJump", "Fall", "AirThrusters", "AirHover", "SuperheroFall", "Jump", "DoubleJump", "ChargeJump", "HoverJump", "DodgeAir", "RegularLand", "HardLand", "VeryHardLand", "DeathLand", "SuperheroLand"},
  gamePSMLandingState = {"Default", "RegularLand", "HardLand", "VeryHardLand", "DeathLand", "SuperheroLand", "SuperheroLandRecovery"},
  gamePSMMeleeWeapon = {"NotReady", "Equipping", "Idle", "Safe", "PublicSafe", "Parried", "Hold", "ChargedHold", "Block", "Targeting", "Deflect", "ComboAttack", "FinalAttack", "StrongAttack"},
  gamePSMTakedown = {"Default", "EnteringGrapple", "Grapple", "Leap", "Takedown"},
  gamedataItemType = {"Invalid", "Wea_AssaultRifle", "Wea_Handgun", "Wea_HeavyMachineGun", "Wea_LightMachineGun", "Wea_PrecisionRifle", "Wea_Revolver", "Wea_Rifle", "Wea_Shotgun", "Wea_ShotgunDual", "Wea_SniperRifle", "Wea_SubmachineGun"},
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
    psm = {Locomotion = 0, LocomotionDetailed = 1, Landing = 0, MeleeWeapon = 2, Takedown = 0},
    mounted = false, combat = false, heat = 0, health = 100, effects = {}, uploads = {}, facts = {},
    speed = 0, gear = 1, handbrake = 0, slip = 0, braindance = false, weapon = "Wea_Shotgun",
    position = {x = 0, y = 0, z = 0}, levels = {Level = 10}}
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
local defs = setmetatable({}, {__index = function(_, board) return setmetatable({}, {__index = function(_, key) return key end}) end})
Game = {
  GetPlayer = function() return world.attached and player or nil end,
  GetAllBlackboardDefs = function() return defs end,
  GetBlackboardSystem = function() return {Get = function() return {GetBool = function(_, key)
    if key == "IsInMenu" then return world.menu end
    if key == "IsActive" then return world.photo end
    return false end} end} end,
  GetTimeSystem = function() return {IsPausedState = function() return world.paused end, IsTimeDilationActive = function() return false end} end,
  GetStatPoolsSystem = function() return {GetStatPoolValue = function() return world.health end, GetStatPoolMaxPointValue = function() return 400 end} end,
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
NameToString = function(v) return v end
ItemID = {IsValid = function() return true end, IsOfTDBID = function(item, id) return item == id end}
gamedataStatPoolType = {Health = "Health"}
gamedataProficiencyType = setmetatable({}, {__index = function(_, k) return k end})
gameJournalEntryState = {Succeeded = "Succeeded", Failed = "Failed"}
VehicleComponent = {GetOwnerVehicleSpeed = function() return world.speed end}
ScriptedPuppet = {GetActiveWeapon = function() return {GetItemID = function() return world.weapon end} end}
WeaponObject = {GetWeaponType = function(id) return Enum.new("gamedataItemType", id) end}
ImGuiCond, ImGuiWindowFlags = {FirstUseEver = 0}, {AlwaysAutoResize = 0}
local drawn = {}
ImGui = setmetatable({Begin = function() return true end, Button = function(label) drawn[#drawn + 1] = label; return false end},
  {__index = function() return function() end end})

local events, hooks, hotkeys = {}, {}, {}
registerForEvent = function(name, callback) events[name] = callback end
registerHotkey = function(name, _, callback) hotkeys[name] = callback end
Observe = function(class, method, callback) hooks[class .. "." .. method] = callback end

package.path = root .. "/src/cet/nightCityHaptics/?.lua;" .. package.path
local mod = dofile(root .. "/src/cet/nightCityHaptics/init.lua")

local function frames(seconds) for _ = 1, math.floor(seconds * 60 + .5) do events.onUpdate(1 / 60) end end
local function eventIds()
  local result = {}
  for _, e in ipairs(state().events) do result[#result + 1] = e[2] end
  return table.concat(result, ",")
end
local function fire(name, ...) hooks[name](...); events.onUpdate(1 / 60) end
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
frames(.2)
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
check(math.abs(state().levels.lowHealth - (.35 + .65 * .5)) < 1e-6, "low health level grows below 40%")
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

-- Romance facts: already-set facts never fire; only the moment they turn on does.
world.facts.sq027_panzer_sex = 1; world.menu = true; frames(.1); world.menu = false; frames(.1)
check(last()[2] ~= "romance", "a fact already set when sampling (re)starts is only a baseline")
world.facts.sq028_05_sex_scene_started = 1; frames(.1)
check(last()[2] == "romance", "Kerry's scene start fires romance")

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

-- Old events age out; the API works for other mods.
frames(2.5)
check(eventIds() == "", "events older than two seconds are dropped")
check(mod.Pulse(.4, 3) == true and last()[2] == "external" and last()[3] == .4 and last()[5] == 3, "Pulse API")

-- Sessions: a new player attachment starts a fresh session.
local session = state().session
hooks["PlayerPuppet.OnGameAttached"](player)
check(state().session ~= session and state().seq == 1, "new session on player attach")

-- Companion status shows in the overlay.
files["status.json"] = '{"v":2,"running":true,"devices":["Lovense Hush"],"output":0.3,"enabled":true}'
frames(.6)
events.onOverlayOpen()
check(true, "status read without errors")
check(next(state().errors) == nil, "no runtime errors: " .. (next(state().errors) or ""))

print(string.format("cet_harness: %d checks passed", passed))
