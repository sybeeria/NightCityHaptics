-- Night City Haptics 0.2.2 (CET part)
-- Reports what happens in the game to the Night City Haptics companion app by
-- sampling about 30 times a second and writing state.json on changes/events or
-- at a quiet 5 Hz heartbeat. The companion owns
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
-- items/actions/useHealCharge.script:4;
-- devices/stash/invisibleSceneStash.script:11/40 (OnQuestUndressPlayer / OnQuestDressPlayer);
-- core/entity/gameObject.script:3358 OnCustomUIAnimationEvent (Panam's 'sex_hud' pulses);
-- core/systems/statusEffectSystem.script:39 HasStatusEffectOfTypeName (gamedataStatusEffectType names);
-- UI/hud/phone/newHudPhoneGameController.script:394-408 (UI_ComDevice.PhoneCallInformation);
-- psm/crosshairTransitions.script:93 (PlayerStateMachine.Weapon = gamePSMRangedWeaponStates);
-- player.script:5049/5387 (JohnnySickness tag, PlayerStateMachine.Swimming).
-- psm/finisher.script:284 (PlayerInFinisherWorkspot on workspot and fast finishers);
-- levelupNotification.script:162 (Espionage notification converts its level to a delta);
-- core/systems/timeSystem.script:19, core/data/engineTime.script:6 (scene simulation clock).
-- Scene facts and timings checked against current vanilla .scene files.
local mod = {version = "0.2.2"}
local R = {
  session = "", seq = 0, clock = 0, sampleClock = 0, writeClock = 0, overlay = false,
  loading = false, paused = false, shutdown = false, active = false, reason = "starting",
  levels = {}, events = {}, eventCount = 0, seen = {}, cooldowns = {}, playerKey = nil,
  companion = {}, statusRaw = nil, statusClock = nil, hooks = {}, errors = {}, dirty = false,
  written = nil, scene = {}, sceneCount = 0, joytoyUntil = 0,
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
local function finite(value)
  return type(value) == "number" and value == value and value ~= math.huge and value ~= -math.huge
end
-- Enum ordinals never change while the game runs, so each is looked up once.
local enums = {}
local function enum(typeName, valueName)
  local key = typeName .. "." .. valueName
  local value = enums[key]
  if value == nil then value = tonumber(EnumInt(Enum.new(typeName, valueName))); enums[key] = value end
  return value
end
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
    scene = R.scene.playing and {name = R.scene.playing, instance = R.scene.instance,
      elapsed = math.floor(R.scene.elapsed * 1000 + .5) / 1000} or {},
    hooks = R.hooks, errors = R.errors})
end
local function writeState()
  while #R.events > 24 or (R.events[1] and R.clock - R.events[1].t > 2) do table.remove(R.events, 1) end
  R.written = nil
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
  R.scene, R.sceneCount, R.joytoyUntil, R.chargeStart = {}, 0, 0, nil
  R.active, R.reason, R.playerKey = false, reason, nil
  writeState()
end

-- Events are queued with an increasing number; the companion plays each once.
local function event(id, magnitude, seconds, always)
  if not always and not R.active then return false end
  if seconds ~= nil and (not finite(seconds) or seconds <= 0) then return false end
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
  for other, time in pairs(R.cooldowns) do if R.clock - time > 1 then R.cooldowns[other] = nil end end
  R.cooldowns[key] = R.clock
  event("explosion", math.max(.15, strength))
end

-- Intimate scenes. CET reports the live scene's simulation offset and its
-- start/pulse/end events; the companion's catalog.json holds each timeline. A scene is "armed" by
-- its quest fact and started by the anchor the research found for it.
local JOYTOY_CLIPS = {"hey_gle_female_rich_sex_clip", "hey_gle_male_rich_sex_clip", "wbr_jpn_female_poor_sex_clip"}
local JOYTOY_STOPS = {"hey_gle_prostittute_female_stop_rumble", "hey_gle_prostittute_male_stop_rumble",
  "wbr_sm_jpn_prostitute_female_stop_rumble", "wbr_sm_jpn_prostitute_male_stop_rumble"}
local function sceneTime()
  local value = attempt("scene clock", function()
    return EngineTime.ToDouble(Game.GetTimeSystem():GetSimTime())
  end)
  return finite(value) and value or nil
end
local function startScene(name, seconds, elapsed, simulationStart)
  if not R.active then return false end
  R.sceneCount = R.sceneCount + 1
  local time = simulationStart or sceneTime()
  R.scene = {playing = name, duration = seconds, elapsed = elapsed or 0, instance = R.sceneCount,
    simulationStart = simulationStart or (time and time - (elapsed or 0))}
  return event("romance:" .. name)
end
local function armScene(name, anchor, seconds)
  R.scene = {armed = name, anchor = anchor, armedUntil = R.clock + seconds,
    simulationArm = sceneTime(), armDuration = seconds}
end
local function anchorReached(anchor)
  local scene = R.scene
  if not scene.armed or scene.anchor ~= anchor then return end
  local time = sceneTime()
  local valid = time and scene.simulationArm and time - scene.simulationArm < scene.armDuration
  if not time or not scene.simulationArm then valid = R.clock < scene.armedUntil end
  if valid then
    if not startScene(scene.armed, 120) then
      R.scene = {pending = scene.armed, duration = 120, elapsed = 0, simulationStart = sceneTime()}
    end
  end
end
local function updateScene(dt)
  local scene = R.scene
  if not scene.playing and not scene.pending then return end
  local time = sceneTime()
  if time and scene.simulationStart then
    local elapsed = time - scene.simulationStart
    if finite(elapsed) then scene.elapsed = math.max(scene.elapsed, elapsed) end
  elseif R.active and not R.paused then
    scene.elapsed = scene.elapsed + dt
  end
  if scene.elapsed >= scene.duration then
    R.scene = {}; R.dirty = true
    event("romance:end")
  elseif scene.pending and R.active and not R.paused then
    startScene(scene.pending, scene.duration, scene.elapsed, scene.simulationStart)
  end
end
local function clearEdges()
  R.seen, R.chargeStart = {}, nil
end

local function registerHooks()
  hook("PlayerPuppet", "OnGameAttached", function(self) if samePlayer(self) then newSession("player attached") end end)
  hook("PlayerPuppet", "OnDetach", function(self) if samePlayer(self) or not Game.GetPlayer() then newSession("player detached") end end)
  hook("PlayerPuppet", "OnDeath", function(self)
    if samePlayer(self) then R.active, R.reason, R.scene = false, "dead", {}; clearEdges(); writeState() end
  end)
  hook("LoadingScreenProgressBarController", "OnInitialize", function()
    R.loading, R.active, R.scene = true, false, {}; clearEdges(); writeState()
  end)
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
    local level = data.lvl
    if data.type == gamedataProficiencyType.Espionage then
      level = Game.GetScriptableSystemsContainer():Get("PlayerDevelopmentSystem"):GetProficiencyLevel(Game.GetPlayer(), data.type)
    end
    if R.seen[key] and level > R.seen[key] then event("levelUp") end
    R.seen[key] = level
  end)
  hook("JournalNotificationQueue", "PushQuestNotification", function(_, entry, state)
    if entry and state == gameJournalEntryState.Succeeded then event("questDone") end
  end)
  hook("UseHealChargeAction", "CompleteAction", function(self) if samePlayer(self:GetExecutor()) then event("heal") end end)

  -- Undress/Dress also run for every apartment shower, so they only count for an armed or playing scene.
  hook("InvisibleSceneStash", "OnQuestUndressPlayer", function() anchorReached("undress") end)
  hook("InvisibleSceneStash", "OnQuestDressPlayer", function()
    if R.scene.playing or R.scene.pending then R.scene = {}; R.dirty = true; event("romance:end") end
  end)
  -- Panam's neural-link overlay: each flash is a pulse, "synchronizing" is the peak.
  hook("GameObject", "OnCustomUIAnimationEvent", function(self, e)
    if not e or NameToString(e.libraryItemName) ~= "sex_hud" or not samePlayer(self) then return end
    local animation = NameToString(e.animationName)
    local playing = tonumber(EnumInt(e.playbackOption)) == enum("EInkAnimationPlaybackOption", "PLAY")
    if playing and R.scene.playing ~= "panam" and R.scene.pending ~= "panam" then
      if not startScene("panam", 240) then
        -- A verified live cue can arrive while output is suspended. Preserve its
        -- simulation anchor; permanent facts acquired behind a gate stay baselines.
        R.scene = {pending = "panam", duration = 240, elapsed = 0, simulationStart = sceneTime()}
      end
    end
    if playing and animation == "synchronizing" and R.scene.playing == "panam" then event("romance:pulse", 1, 1.5)
    elseif playing and R.scene.playing == "panam" then
      event("romance:pulse", animation == "MAIN" and 1 or .65)
    end
  end)
end

-- polled state ------------------------------------------------------------------
local K  -- game handles built once, on the first sample
local function sample(player)
  K = K or {kerenzikov = TweakDBID.new("BaseStatusEffect.KerenzikovPlayerBuff"),
    finisher = TweakDBID.new("BaseStatusEffect.PlayerInFinisherWorkspot"),
    camo = CName.new("CamoActiveOnPlayer"), johnny = CName.new("JohnnySickness")}
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
    for _, name in ipairs({"Level", "StreetCred", "CoolSkill", "IntelligenceSkill", "ReflexesSkill", "StrengthSkill", "TechnicalAbilitySkill", "Espionage"}) do
      local kind = gamedataProficiencyType[name]
      R.seen["level." .. tostring(kind)] = system:GetProficiencyLevel(player, kind)
    end
    R.seen.levelsSeeded = true
  end)
  probe("takedown", function()
    local takedown = rose("takedown", psm:GetInt(P.Takedown) ~= enum("gamePSMTakedown", "Default"))
    local finisher = rose("finisher", Game.GetStatusEffectSystem():HasStatusEffect(id, K.finisher))
    if takedown or finisher then event("takedown") end
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
    local kerenzikov = effects:HasStatusEffect(id, K.kerenzikov)
    levels.slowmo = (sandy or kerenzikov) and 1 or 0
    levels.berserk = effects:HasStatusEffectOfTypeName(id, "Berserk") and 1 or 0
    levels.camo = effects:HasStatusEffectWithTag(id, K.camo) and 1 or 0
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
  probe("status", function()
    local effects = Game.GetStatusEffectSystem()
    local function has(name) return effects:HasStatusEffectOfTypeName(id, name) end
    levels.burning = has("Burning") and 1 or 0
    levels.shock = (has("Electrocuted") or has("EMP")) and 1 or 0
    levels.poison = (has("Poisoned") or has("Bleeding")) and 1 or 0
    levels.relic = effects:HasStatusEffectWithTag(id, K.johnny) and 1 or 0
    if rose("knockdown", has("Knockdown") or has("Stunned")) then event("knockdown") end
    if rose("flashed", has("Blind")) then event("flashed") end
  end)
  probe("weapon", function()
    local state = psm:GetInt(P.Weapon)
    if state == enum("gamePSMRangedWeaponStates", "Charging") then
      R.chargeStart = R.chargeStart or R.clock
      levels.charge = .25 + .75 * math.min(1, (R.clock - R.chargeStart) / 1.5)
    else
      R.chargeStart, levels.charge = nil, 0
    end
    if changed("weaponState", state) then
      if state == enum("gamePSMRangedWeaponStates", "Reload") then event("reload")
      elseif state == enum("gamePSMRangedWeaponStates", "Overheat") then event("overheat") end
    end
  end)
  probe("phone", function()
    local board = Game.GetBlackboardSystem():Get(defs.UI_ComDevice)
    local call = FromVariant(board:GetVariant(defs.UI_ComDevice.PhoneCallInformation))
    local ringing = call ~= nil and not call.isPlayerCalling
      and tonumber(EnumInt(call.callPhase)) == enum("questPhoneCallPhase", "IncomingCall")
    levels.phoneRing = ringing and 1 or 0
  end)
  probe("oxygen", function()
    levels.oxygen = 0
    if psm:GetInt(P.Swimming) ~= enum("gamePSMSwimming", "Diving") then return end
    local oxygen = Game.GetStatPoolsSystem():GetStatPoolValue(id, gamedataStatPoolType.Oxygen, true) / 100
    if oxygen < .5 then levels.oxygen = .3 + .7 * (.5 - oxygen) / .5 end
  end)
  probe("romance", function()
    local quests = Game.GetQuestsSystem()
    local function fact(name) return quests:GetFactStr(name) end
    -- These facts stay set after the scene, so only the moment they change counts.
    local function turnedOn(name) return rose(name, fact(name) > 0) end
    local function increased(name)
      local value, previous = fact(name), R.seen[name]
      R.seen[name] = value
      return previous ~= nil and value > previous
    end
    local censored = fact("censorship_oversexualized") > 0
    if increased("sq028_kerry_sex") then startScene(censored and "kerryCensored" or "kerry", 90) end
    if turnedOn("sq030_sex_done") then startScene(censored and "judyCensored" or "judy", 140) end
    if turnedOn("sq029_river_had_sex") then startScene("river", 130) end
    if turnedOn("q003_stout_sex_agreed") then armScene("stout", "undress", 120) end
    if turnedOn("sq027_panzer_sex") then
      -- The full scene starts on its first neural-link pulse; the censored cut has none.
      if censored then startScene("panamCensored", 240) else armScene("panam", "pulse", 300) end
    end
    -- Joytoys: the clip counter changes at every clip boundary (2 s or 5 s loops).
    local ticked = false
    for _, name in ipairs(JOYTOY_CLIPS) do if changed(name, fact(name)) then ticked = true end end
    if ticked then R.joytoyUntil = R.clock + 6; event("joytoy") end
    for _, name in ipairs(JOYTOY_STOPS) do
      if increased(name) and R.clock < R.joytoyUntil then R.joytoyUntil = 0; event("joytoy:end") end
    end
  end)
  -- CET's json.encode rejects NaN; a bad reading must never stall the heartbeat.
  -- Two decimals are plenty and keep tiny wobbles from forcing a write.
  for name, value in pairs(levels) do levels[name] = math.floor(clamp(value) * 100 + .5) / 100 end
  return levels
end

-- CET events -------------------------------------------------------------------
local function readStatus()
  local file = io.open("status.json", "r")
  if not file then return end
  local raw = file:read("*a"); file:close()
  -- The companion changes its heartbeat on each rewrite, even while idle. A
  -- truly identical file is stale; merely reading it cannot prove app liveness.
  if raw == R.statusRaw then return end
  local ok, decoded = pcall(json.decode, raw)
  if ok and type(decoded) == "table" and (decoded.heartbeat == nil or
      (finite(decoded.heartbeat) and decoded.heartbeat >= 0 and decoded.heartbeat % 1 == 0)) then
    R.companion, R.statusRaw, R.statusClock = decoded, raw, R.clock
  end
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
  local sampleDelta = R.sampleClock
  R.sampleClock = 0
  local active, reason, player = gate()
  if player then
    local key = tostring(player:GetEntityID().hash)
    if R.playerKey and R.playerKey ~= key then newSession("player changed") end
    R.playerKey = key
  end
  local wasActive = R.active
  R.active, R.reason = active, reason
  updateScene(sampleDelta)
  if active then
    local levels, ok = attempt("sample", sample, player)
    R.levels = ok and levels or {}
  else
    R.levels = {}
    if wasActive then clearEdges() end  -- fresh baselines after menus/loading
  end
  -- Write when something changed; otherwise a 5-per-second heartbeat is enough.
  local content = json.encode({R.active, R.paused, R.reason, R.levels})
  if R.dirty or content ~= R.written or R.writeClock >= .2 then writeState(); R.written = content end
  if R.readClock > .5 then R.readClock = 0; readStatus() end
end)

registerForEvent("onOverlayOpen", function() R.overlay = true; R.active = false; clearEdges(); writeState() end)
registerForEvent("onOverlayClose", function() R.overlay = false end)
registerForEvent("onShutdown", function() R.shutdown, R.active, R.scene = true, false, {}; writeState() end)

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
  if seconds ~= nil and not finite(seconds) then return false end
  return event("external", strength, type(seconds) == "number" and math.max(.05, math.min(600, seconds)) or nil)
end
mod.IsActive = function() return R.active and not R.paused end
return mod
