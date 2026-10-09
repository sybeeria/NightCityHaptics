-- Night City Haptics 0.2.1 (CET part)
-- Reports what happens in the game to the Night City Haptics companion app by
-- writing state.json in this folder about 30 times a second. The companion owns
-- every setting, pattern and device; this file never talks to hardware.
--
-- Signatures checked against CDPR's decompiled scripts (Cyberpunk-Scripts):
-- player/player.script: OnGameAttached, OnDetach, OnDeath, OnHitAnimation (3806),
--   OnTargetNeutraliziedEvent (6423, drives the kill marker);
-- psm/weaponTransitions.script:2337 ShootEvents.OnEnter (one call per player shot);
-- core/gameplay/psmImports.script: every gamePSM* enum polled below;
-- audioSystem.script:28, fragGrenade.script:38, ExplosiveDevice.script:261,
--   vehicleComponent.script:2445/3100/3152: explosions and collisions;
-- itemsQueue/currencyNotification/levelupNotification/journalNotification queues;
-- items/actions/useHealCharge.script:4. Romance facts: HISTORY.md § scene-sources.
local mod = {version = "0.2.1"}
local R = {
  session = "", seq = 0, clock = 0, sampleClock = 0, writeClock = 0, overlay = false,
  loading = false, paused = false, shutdown = false, active = false, reason = "starting",
  levels = {}, events = {}, eventCount = 0, seen = {}, cooldowns = {}, playerKey = nil,
  companion = {}, statusRaw = nil, statusClock = nil, hooks = {}, errors = {}, dirty = false,
}

local function attempt(name, callback, ...)
  local ok, result = pcall(callback, ...)
  if ok then R.errors[name] = nil; return result, true end
  R.errors[name] = tostring(result)
  return nil, false
end
local function clamp(value)
  if type(value) ~= "number" or value ~= value then return 0 end
  return math.max(0, math.min(1, value))
end
local function enum(typeName, valueName) return tonumber(EnumInt(Enum.new(typeName, valueName))) end
local function isOneOf(typeName, value, names)
  for _, name in ipairs(names) do if value == enum(typeName, name) then return true end end
  return false
end
local function samePlayer(object)
  local player = Game.GetPlayer()
  return player ~= nil and object ~= nil and tostring(player:GetEntityID().hash) == tostring(object:GetEntityID().hash)
end

-- state.json ----------------------------------------------------------------
local function encodeState()
  local events = {}
  for i, e in ipairs(R.events) do
    events[i] = e.dur and {e.n, e.id, e.mag, e.t, e.dur} or {e.n, e.id, e.mag, e.t}
  end
  return json.encode({v = 2, session = R.session, seq = R.seq, t = R.clock, active = R.active,
    paused = R.paused, reason = R.reason, levels = R.active and R.levels or {}, events = events,
    hooks = R.hooks, errors = R.errors})
end
local function writeState()
  while #R.events > 24 or (R.events[1] and R.clock - R.events[1].t > 2) do table.remove(R.events, 1) end
  R.seq = R.seq + 1
  local raw, ok = attempt("encode", encodeState)
  if not ok then return end
  local file = io.open("state.json", "w")
  if not file then R.errors["state.json"] = "cannot write"; return end
  file:write(raw); file:close()
  R.dirty, R.writeClock = false, 0
end
local function newSession(reason)
  R.session = tostring(os.time()) .. "-" .. string.format("%.6f", os.clock()) .. "-" .. tostring({}):gsub("[^%w]", "")
  R.seq, R.events, R.seen, R.cooldowns, R.levels, R.paused = 0, {}, {}, {}, {}, false
  R.active, R.reason, R.playerKey = false, reason, nil
  writeState()
end

-- Events are queued with an increasing number; the companion plays each once.
local function event(id, magnitude, seconds, always)
  if not always and not R.active then return false end
  magnitude = magnitude == nil and 1 or clamp(magnitude)
  if magnitude <= 0 then return false end
  R.eventCount = R.eventCount + 1
  table.insert(R.events, {n = R.eventCount, id = id, mag = magnitude, t = R.clock, dur = seconds})
  -- Write at once for low latency; several events in one frame share one write.
  if R.writeClock > 0 then writeState() else R.dirty = true end
  return true
end
-- Rising/changed edges. The first reading is only a baseline, so loading a save
-- never replays something that was already true.
local function changed(key, value)
  local previous = R.seen[key]
  R.seen[key] = value
  return previous ~= nil and previous ~= value
end
local function rose(key, value)
  local previous = R.seen[key]
  R.seen[key] = value
  return previous == false and value == true
end

-- gameplay gate: silent in menus, pause, loading, photo mode, tutorials and the overlay
local function gate()
  if R.shutdown then return false, "shutdown" end
  if R.overlay then return false, "CET overlay open" end
  if R.loading then return false, "loading" end
  local result, ok = attempt("gate", function()
    local player = Game.GetPlayer()
    if not player or not player:IsAttached() then return {false, "no player"} end
    if player:IsDeadNoStatPool() then return {false, "dead"} end
    if GetSingleton("inkMenuScenario"):GetSystemRequestsHandler():IsPreGame() then return {false, "main menu"} end
    local defs, boards, time = Game.GetAllBlackboardDefs(), Game.GetBlackboardSystem(), Game.GetTimeSystem()
    if time:IsPausedState() then return {false, "paused"} end
    if boards:Get(defs.UI_System):GetBool(defs.UI_System.IsInMenu) then return {false, "menu"} end
    if boards:Get(defs.PhotoMode):GetBool(defs.PhotoMode.IsActive) then return {false, "photo mode"} end
    if time:IsTimeDilationActive("UI_TutorialPopup") then return {false, "tutorial"} end
    return {true, "gameplay", player}
  end)
  if not ok then return false, "probe failure" end
  return result[1], result[2], result[3]
end

-- hooks -----------------------------------------------------------------------
local function hook(class, method, callback)
  local name = class .. "." .. method
  attempt("hook " .. name, function()
    Observe(class, method, function(...)
      if R.shutdown then return end
      R.hooks[name] = (R.hooks[name] or 0) + 1
      attempt(name, callback, ...)
    end)
  end)
end

local weaponKick = {Wea_Shotgun = 1, Wea_ShotgunDual = 1, Wea_SniperRifle = 1, Wea_PrecisionRifle = .9,
  Wea_Revolver = .9, Wea_HeavyMachineGun = .8, Wea_LightMachineGun = .7, Wea_Handgun = .65,
  Wea_AssaultRifle = .6, Wea_Rifle = .6, Wea_SubmachineGun = .5}
local kickByType
local function shotStrength(player)
  if not kickByType then
    kickByType = {}
    for name, value in pairs(weaponKick) do
      local ordinal = attempt("weapon enum " .. name, enum, "gamedataItemType", name)
      if ordinal then kickByType[ordinal] = value end
    end
  end
  local kind = attempt("weapon type", function()
    return EnumInt(WeaponObject.GetWeaponType(ScriptedPuppet.GetActiveWeapon(player):GetItemID()))
  end)
  return kickByType[kind] or .7
end

local function explosionAt(position)
  local player = Game.GetPlayer()
  if not R.active or not player or not position then return end
  local origin = player:GetWorldPosition()
  local dx, dy, dz = position.x - origin.x, position.y - origin.y, position.z - origin.z
  local strength = 1 - math.sqrt(dx * dx + dy * dy + dz * dz) / 30
  if strength <= 0 then return end
  -- Several notifications can describe one blast; merge them for 0.15 s.
  local key = string.format("%.0f.%.0f.%.0f", position.x, position.y, position.z)
  if R.cooldowns[key] and R.clock - R.cooldowns[key] < .15 then return end
  R.cooldowns[key] = R.clock
  event("explosion", math.max(.15, strength))
end

local function registerHooks()
  hook("PlayerPuppet", "OnGameAttached", function(self) if samePlayer(self) then newSession("player attached") end end)
  hook("PlayerPuppet", "OnDetach", function(self) if samePlayer(self) or not Game.GetPlayer() then newSession("player detached") end end)
  hook("PlayerPuppet", "OnDeath", function(self) if samePlayer(self) then R.active, R.reason = false, "dead"; writeState() end end)
  hook("LoadingScreenProgressBarController", "OnInitialize", function() R.loading, R.active = true, false; writeState() end)
  hook("LoadingScreenProgressBarController", "SetProgress", function(_, progress) if progress >= 1 then R.loading = false end end)
  hook("FastTravelSystem", "OnLoadingScreenFinished", function(_, finished) if finished then R.loading = false end end)

  hook("ShootEvents", "OnEnter", function(_, _, interface)
    if interface and samePlayer(interface.executionOwner) then event("gunfire", shotStrength(interface.executionOwner)) end
  end)
  hook("PlayerPuppet", "OnHitAnimation", function(self, hit)
    if not samePlayer(self) or not hit or not hit.attackComputed then return end
    local damage = hit.attackComputed:GetTotalAttackValue(gamedataStatPoolType.Health)
    local maximum = Game.GetStatPoolsSystem():GetStatPoolMaxPointValue(self:GetEntityID(), gamedataStatPoolType.Health)
    if maximum > 0 and damage > 0 then event("hurt", math.max(.3, damage / maximum / .25)) end
  end)
  hook("PlayerPuppet", "OnTargetNeutraliziedEvent", function(self) if samePlayer(self) then event("kill") end end)
  hook("AudioSystem", "PlayShockwave", function(_, kind, position) if NameToString(kind) == "explosion" then explosionAt(position) end end)
  hook("WeaponGrenade", "OnExplosion", function(self) explosionAt(self:GetWorldPosition()) end)
  hook("ExplosiveDevice", "Explosion", function(self) explosionAt(self:GetWorldPosition()) end)
  hook("VehicleComponent", "ProcessExplosionEffects", function(self) explosionAt(self:GetVehicle():GetWorldPosition()) end)
  hook("VehicleComponent", "OnVehicleNotifyPassengersOfCollision", function(self)
    if self:GetVehicle():IsPlayerMounted() then event("crash") end
  end)
  hook("ItemsNotificationQueue", "OnUILootedItemEvent", function(_, e)
    if e and ItemID.IsValid(e.itemID) and not ItemID.IsOfTDBID(e.itemID, TweakDBID.new("Items.money")) then event("loot") end
  end)
  hook("CurrencyChangeInventoryCallback", "OnItemQuantityChanged", function(_, item, difference, _, silent)
    if not silent and difference > 0 and ItemID.IsOfTDBID(item, TweakDBID.new("Items.money")) then
      event("money", .35 + .65 * math.min(1, difference / 2500))
    end
  end)
  hook("LevelUpNotificationQueue", "OnCharacterLevelUpdated", function(_, value)
    local data = FromVariant(value)
    if not data then return end
    local key = "level." .. tostring(data.type)
    if R.seen[key] and data.lvl > R.seen[key] then event("levelUp") end
    R.seen[key] = data.lvl
  end)
  hook("JournalNotificationQueue", "PushQuestNotification", function(_, entry, state)
    if entry and state == gameJournalEntryState.Succeeded then event("questDone") end
  end)
  hook("UseHealChargeAction", "CompleteAction", function(self) if samePlayer(self:GetExecutor()) then event("heal") end end)
end

-- polled state ------------------------------------------------------------------
local function sample(player)
  local levels = {}
  local defs = Game.GetAllBlackboardDefs()
  local psm = player:GetPlayerStateMachineBlackboard()
  local id = player:GetEntityID()
  local P = defs.PlayerStateMachine
  local function probe(name, callback) local value = attempt("probe " .. name, callback); return value end

  probe("movement", function()
    local loco, detailed = psm:GetInt(P.Locomotion), psm:GetInt(P.LocomotionDetailed)
    levels.sprint = isOneOf("gamePSMLocomotionStates", loco, {"Sprint", "CrouchSprint"}) and 1 or 0
    local jumping = isOneOf("gamePSMDetailedLocomotionStates", detailed, {"Jump", "DoubleJump", "ChargeJump", "HoverJump"})
    local dodging = isOneOf("gamePSMDetailedLocomotionStates", detailed, {"Dodge", "DodgeAir", "Slide"})
    if changed("detailed", detailed) then
      if jumping then event("jump") elseif dodging then event("dodge") end
    end
    local landing = psm:GetInt(P.Landing)
    if changed("landing", landing) and landing ~= enum("gamePSMLandingState", "Default") then
      local strength = {RegularLand = .4, HardLand = .7, VeryHardLand = 1, DeathLand = 1, SuperheroLand = 1}
      for name, value in pairs(strength) do if landing == enum("gamePSMLandingState", name) then event("landing", value) end end
    end
  end)
  probe("melee", function()
    local state = psm:GetInt(P.MeleeWeapon)
    if changed("meleeWeapon", state) and isOneOf("gamePSMMeleeWeapon", state, {"ComboAttack", "FinalAttack", "StrongAttack",
      "SafeAttack", "BlockAttack", "SprintAttack", "CrouchAttack", "JumpAttack", "ThrowAttack", "DeflectAttack", "EquipAttack"}) then
      event("melee", isOneOf("gamePSMMeleeWeapon", state, {"FinalAttack", "StrongAttack"}) and 1 or .7)
    end
  end)
  probe("levels", function()
    -- Baseline every progression level so the first level-up after loading counts.
    if R.seen.levelsSeeded then return end
    local system = Game.GetScriptableSystemsContainer():Get("PlayerDevelopmentSystem")
    for _, name in ipairs({"Level", "StreetCred", "CoolSkill", "IntelligenceSkill", "ReflexesSkill", "StrengthSkill", "TechnicalAbilitySkill"}) do
      local kind = gamedataProficiencyType[name]
      R.seen["level." .. tostring(kind)] = system:GetProficiencyLevel(player, kind)
    end
    R.seen.levelsSeeded = true
  end)
  probe("takedown", function()
    if rose("takedown", psm:GetInt(P.Takedown) ~= enum("gamePSMTakedown", "Default")) then event("takedown") end
  end)
  probe("combat", function() if rose("combat", player:IsInCombat()) then event("combatStart") end end)
  probe("police", function()
    local heat = player:GetPreventionSystem():GetHeatStageAsInt()
    local previous = R.seen.heat
    R.seen.heat = heat
    if previous and heat > previous then event("police") end
  end)
  probe("health", function()
    local pools = Game.GetStatPoolsSystem()
    local health = pools:GetStatPoolValue(id, gamedataStatPoolType.Health, true) / 100
    levels.lowHealth = (health > 0 and health < .4) and .35 + .65 * (.4 - health) / .4 or 0
  end)
  probe("cyberware", function()
    local effects = Game.GetStatusEffectSystem()
    local sandy = effects:HasStatusEffectOfTypeName(id, "Sandevistan")
    local kerenzikov = effects:HasStatusEffect(id, TweakDBID.new("BaseStatusEffect.KerenzikovPlayerBuff"))
    levels.slowmo = (sandy or kerenzikov) and 1 or 0
    levels.berserk = effects:HasStatusEffectOfTypeName(id, "Berserk") and 1 or 0
    levels.camo = effects:HasStatusEffectWithTag(id, CName.new("CamoActiveOnPlayer")) and 1 or 0
    local uploads = FromVariant(psm:GetVariant(P.UploadingQuickHackIDs))
    levels.quickhack = type(uploads) == "table" and #uploads > 0 and 1 or 0
  end)
  probe("vehicle", function()
    levels.engine, levels.drift = 0, 0
    if not psm:GetBool(P.MountedToVehicle) then R.seen.gear = nil; return end
    local speed = math.abs(VehicleComponent.GetOwnerVehicleSpeed(player:GetGame(), player))
    levels.engine = .15 + .85 * math.min(1, speed / 40)
    local mount = Game.GetMountingFacility():GetMountingInfoSingleWithIds(id)
    local vehicle = Game.FindEntityByID(mount.parentId)
    local board = vehicle and vehicle:GetBlackboard()
    if not board then return end
    if speed > 2 then
      local handbrake = board:GetInt(defs.Vehicle.IsHandbraking) ~= 0 and .6 or 0
      levels.drift = math.max(handbrake, math.min(1, math.abs(board:GetFloat(defs.Vehicle.MeanSlipRatio)) / .5))
    end
    if changed("gear", board:GetInt(defs.Vehicle.GearValue)) then event("gearShift") end
  end)
  probe("braindance", function()
    local boards = Game.GetBlackboardSystem()
    levels.braindance = boards:Get(defs.Braindance):GetBool(defs.Braindance.IsActive) and 1 or 0
  end)
  probe("romance", function()
    local quests = Game.GetQuestsSystem()
    -- Both facts stay set after the scene, so only the moment they turn on counts.
    for _, fact in ipairs({"sq028_05_sex_scene_started", "sq027_panzer_sex"}) do
      if rose(fact, quests:GetFactStr(fact) > 0) then event("romance") end
    end
  end)
  -- CET's json.encode rejects NaN; a bad reading must never stall the heartbeat.
  for name, value in pairs(levels) do levels[name] = clamp(value) end
  return levels
end

-- CET events -------------------------------------------------------------------
local function readStatus()
  local file = io.open("status.json", "r")
  if not file then return end
  local raw = file:read("*a"); file:close()
  if raw == R.statusRaw then return end
  local ok, decoded = pcall(json.decode, raw)
  if ok and type(decoded) == "table" then R.companion, R.statusRaw, R.statusClock = decoded, raw, R.clock end
end
local function companionUp()
  return R.statusClock ~= nil and R.clock - R.statusClock < 3 and R.companion.running == true
end
local function togglePause()
  R.paused = not R.paused
  writeState()
end
local function testBuzz() event("test", 1, nil, true); writeState() end

registerForEvent("onInit", function()
  newSession("CET initialized")
  registerHooks()
end)

registerForEvent("onUpdate", function(dt)
  if R.shutdown then return end
  R.clock = R.clock + dt
  R.sampleClock, R.writeClock, R.readClock = R.sampleClock + dt, R.writeClock + dt, (R.readClock or 0) + dt
  if R.sampleClock < 1 / 30 then
    if R.dirty then writeState() end
    return
  end
  R.sampleClock = 0
  local active, reason, player = gate()
  if player then
    local key = tostring(player:GetEntityID().hash)
    if R.playerKey and R.playerKey ~= key then newSession("player changed") end
    R.playerKey = key
  end
  local wasActive = R.active
  R.active, R.reason = active, reason
  if active then
    local levels, ok = attempt("sample", sample, player)
    R.levels = ok and levels or {}
  else
    R.levels = {}
    if wasActive then R.seen = {} end  -- fresh baselines after menus/loading
  end
  writeState()
  if R.readClock > .5 then R.readClock = 0; readStatus() end
end)

registerForEvent("onOverlayOpen", function() R.overlay = true; R.active = false; writeState() end)
registerForEvent("onOverlayClose", function() R.overlay = false end)
registerForEvent("onShutdown", function() R.shutdown, R.active = true, false; writeState() end)

-- Overlay colours match the companion page: paper, ink and one red.
local INK, PAPER, RED = {.07, .07, .07, 1}, {.97, .97, .96, .98}, {.84, .06, .06, 1}
local STYLE = {
  {"WindowBg", PAPER}, {"TitleBg", {.9, .9, .89, 1}}, {"TitleBgActive", {.9, .9, .89, 1}},
  {"TitleBgCollapsed", {.9, .9, .89, 1}}, {"Text", INK}, {"TextDisabled", {.42, .42, .42, 1}},
  {"Border", INK}, {"Button", {.86, .86, .85, 1}}, {"ButtonHovered", {.95, .7, .7, 1}},
  {"ButtonActive", {.95, .55, .55, 1}}, {"FrameBg", {.86, .86, .85, 1}}, {"PlotHistogram", RED},
}

registerForEvent("onDraw", function()
  if not R.overlay then return end
  for _, entry in ipairs(STYLE) do
    local c = entry[2]
    ImGui.PushStyleColor(ImGuiCol[entry[1]], c[1], c[2], c[3], c[4])
  end
  ImGui.PushStyleVar(ImGuiStyleVar.WindowRounding, 0)
  ImGui.PushStyleVar(ImGuiStyleVar.FrameRounding, 10)
  ImGui.SetNextWindowSize(300, 0, ImGuiCond.FirstUseEver)
  if ImGui.Begin("Night City Haptics", ImGuiWindowFlags.AlwaysAutoResize) then
    local up = companionUp()
    if up then
      local devices = R.companion.devices or {}
      ImGui.Text("Running · " .. (#devices > 0 and table.concat(devices, ", ") or "no device yet"))
      if R.companion.enabled == false then ImGui.TextColored(RED[1], RED[2], RED[3], 1, "Feedback is off in the app") end
    else
      ImGui.TextColored(RED[1], RED[2], RED[3], 1, "App not running")
      ImGui.TextWrapped("Start it with start.sh or start.cmd in this mod's companion folder.")
    end
    local output = up and tonumber(R.companion.output) or 0
    ImGui.ProgressBar(output, 280, 0, string.format("%d%%", math.floor(output * 100 + .5)))
    if ImGui.Button(R.paused and "Resume" or "Pause", 136, 0) then togglePause() end
    ImGui.SameLine()
    if ImGui.Button("Test buzz", 136, 0) then testBuzz() end
    if R.paused then ImGui.TextColored(RED[1], RED[2], RED[3], 1, "Paused") end
  end
  ImGui.End()
  ImGui.PopStyleVar(2)
  ImGui.PopStyleColor(#STYLE)
end)

registerHotkey("nchPause", "Pause / resume haptics", togglePause)
registerHotkey("nchTest", "Test buzz", testBuzz)

-- For other CET mods: GetMod("nightCityHaptics").Pulse(strength 0-1, seconds?)
mod.Pulse = function(strength, seconds)
  return event("external", strength, type(seconds) == "number" and math.max(.05, math.min(600, seconds)) or nil)
end
mod.IsActive = function() return R.active and not R.paused end
return mod
