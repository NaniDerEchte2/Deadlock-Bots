-- ===================================================================
-- DEADLOCK PERFORMANCE TRACKER - ADVANCED VERSION
-- Source 2 Engine Integration mit Hero-spezifischen Benchmarks
-- Version 1.0 - VAC-Safe Implementation
-- ===================================================================

-- Globale Variablen
local DeadlockPerformanceTracker = {}
DeadlockPerformanceTracker.version = "1.0"
DeadlockPerformanceTracker.enabled = true
DeadlockPerformanceTracker.demo_mode = false
DeadlockPerformanceTracker.debug = false

-- Stats Container
DeadlockPerformanceTracker.stats = {
    souls = 0,
    kills = 0,
    deaths = 0,
    assists = 0,
    heroDamage = 0,
    creepDamage = 0,
    matchStartTime = 0,
    currentHero = "Unknown",
    matchDuration = 0,
    teamPosition = 0,
    lastUpdateTime = 0
}

-- Hero-spezifische Benchmarks (SPM Targets)
DeadlockPerformanceTracker.heroBenchmarks = {
    ["Seven"] = { target_spm = 400, role = "DPS", difficulty = "Medium" },
    ["Bebop"] = { target_spm = 350, role = "Tank", difficulty = "Easy" },
    ["Infernus"] = { target_spm = 420, role = "DPS", difficulty = "Hard" },
    ["Lash"] = { target_spm = 380, role = "Assassin", difficulty = "Hard" },
    ["Paradox"] = { target_spm = 390, role = "Support", difficulty = "Medium" },
    ["Wraith"] = { target_spm = 410, role = "DPS", difficulty = "Medium" },
    ["McGinnis"] = { target_spm = 340, role = "Tank", difficulty = "Easy" },
    ["Lady Geist"] = { target_spm = 395, role = "Support", difficulty = "Hard" },
    ["Abrams"] = { target_spm = 320, role = "Tank", difficulty = "Easy" },
    ["Haze"] = { target_spm = 430, role = "DPS", difficulty = "Hard" },
    ["Kelvin"] = { target_spm = 360, role = "Tank", difficulty = "Medium" },
    ["Dynamo"] = { target_spm = 370, role = "Support", difficulty = "Medium" },
    ["Ivy"] = { target_spm = 385, role = "Support", difficulty = "Medium" },
    ["Pocket"] = { target_spm = 400, role = "DPS", difficulty = "Medium" },
    ["Viscous"] = { target_spm = 375, role = "Tank", difficulty = "Medium" },
    ["Warden"] = { target_spm = 355, role = "Tank", difficulty = "Easy" },
    ["Yamato"] = { target_spm = 415, role = "Assassin", difficulty = "Hard" },
    ["Mo & Krill"] = { target_spm = 345, role = "Tank", difficulty = "Medium" },
    ["Default"] = { target_spm = 380, role = "Unknown", difficulty = "Medium" }
}

-- UI References
local hud_panel = nil
local update_timer = nil
local position_x = 1640  -- Rechts oben (für 1920x1080)
local position_y = 20

-- ===== INITIALIZATION =====
function DeadlockPerformanceTracker:Initialize()
    if self.debug then
        print("[DPT] 🎮 Initializing Deadlock Performance Tracker v" .. self.version)
    end
    
    -- Error handling für Game API
    local success = pcall(function()
        self:RegisterGameEvents()
        self:CreateHUD()
        self:RegisterConsoleCommands()
        self:StartUpdateTimer()
        self:ResetStats()
        
        if self.debug then
            print("[DPT] ✅ Performance Tracker loaded successfully!")
            print("[DPT] 💬 Commands: perf_toggle, perf_reset, perf_position, perf_debug")
        end
    end)
    
    if not success then
        self.demo_mode = true
        self:InitializeDemoMode()
    end
end

-- ===== DEMO MODE (Fallback) =====
function DeadlockPerformanceTracker:InitializeDemoMode()
    print("[DPT] ⚠️ Game API not available - Starting Demo Mode")
    self.demo_mode = true
    self.stats.matchStartTime = os.time()
    self.stats.currentHero = "Seven"
    
    -- Simulierte Demo-Daten
    Timer.SetTimeout(function()
        self:GenerateDemoData()
        self:UpdateHUD()
    end, 2.0)
end

function DeadlockPerformanceTracker:GenerateDemoData()
    if not self.demo_mode then return end
    
    local current_time = os.time()
    local match_duration = current_time - self.stats.matchStartTime
    local minutes = math.max(1, match_duration / 60)
    
    -- Simuliere realistische Performance-Daten
    local base_spm = 250 + math.random(0, 200)  -- 250-450 SPM
    self.stats.souls = math.floor(base_spm * minutes)
    self.stats.kills = math.floor(minutes * (0.3 + math.random() * 0.4))  -- 0.3-0.7 KPM
    self.stats.deaths = math.floor(minutes * (0.1 + math.random() * 0.3))  -- 0.1-0.4 DPM
    self.stats.assists = math.floor(minutes * (0.2 + math.random() * 0.5))  -- 0.2-0.7 APM
    self.stats.heroDamage = math.floor(minutes * (200 + math.random() * 300))  -- 200-500 HDPM
    self.stats.matchDuration = match_duration
    
    -- Demo Modus weiterlaufen lassen
    Timer.SetTimeout(function()
        self:GenerateDemoData()
        self:UpdateHUD()
    end, 2.0)
end

-- ===== GAME EVENT REGISTRATION =====
function DeadlockPerformanceTracker:RegisterGameEvents()
    -- Versuche Deadlock-spezifische Events zu registrieren
    local events = {
        "player_killed",
        "player_death", 
        "souls_gained",
        "match_started",
        "hero_selected",
        "match_ended",
        "player_spawn"
    }
    
    for _, event in ipairs(events) do
        local success = pcall(function()
            ListenToGameEvent(event, function(data)
                self:HandleGameEvent(event, data)
            end, self)
        end)
        
        if not success and self.debug then
            print("[DPT] ⚠️ Could not register event: " .. event)
        end
    end
end

-- ===== GAME EVENT HANDLERS =====
function DeadlockPerformanceTracker:HandleGameEvent(event, data)
    if not self.enabled then return end
    
    local localPlayer = self:GetLocalPlayer()
    if not localPlayer then return end
    
    if event == "player_killed" then
        self:OnPlayerKilled(data, localPlayer)
    elseif event == "player_death" then
        self:OnPlayerDeath(data, localPlayer)
    elseif event == "souls_gained" then
        self:OnSoulsGained(data, localPlayer)
    elseif event == "match_started" then
        self:OnMatchStarted(data)
    elseif event == "hero_selected" then
        self:OnHeroSelected(data, localPlayer)
    elseif event == "match_ended" then
        self:OnMatchEnded(data)
    end
    
    self:UpdateHUD()
end

function DeadlockPerformanceTracker:OnPlayerKilled(data, localPlayer)
    if data.killer == localPlayer then
        self.stats.kills = self.stats.kills + 1
        self.stats.souls = self.stats.souls + (data.bounty or 400)
        
        if self.debug then
            print("[DPT] 💀 Kill registered! Bounty: " .. (data.bounty or 400))
        end
        
        self:PlayKillSound()
    elseif data.assister == localPlayer then
        self.stats.assists = self.stats.assists + 1
        self.stats.souls = self.stats.souls + (data.assist_bounty or 150)
        
        if self.debug then
            print("[DPT] 🤝 Assist registered! Bounty: " .. (data.assist_bounty or 150))
        end
    end
end

function DeadlockPerformanceTracker:OnPlayerDeath(data, localPlayer)
    if data.victim == localPlayer then
        self.stats.deaths = self.stats.deaths + 1
        
        if self.debug then
            print("[DPT] 💀 Death registered!")
        end
    end
end

function DeadlockPerformanceTracker:OnSoulsGained(data, localPlayer)
    if data.player == localPlayer then
        self.stats.souls = self.stats.souls + (data.amount or 0)
        
        if self.debug and data.amount > 100 then
            print("[DPT] 💎 Large soul gain: " .. data.amount)
        end
    end
end

function DeadlockPerformanceTracker:OnMatchStarted(data)
    self:ResetStats()
    self.stats.matchStartTime = self:GetGameTime()
    
    if self.debug then
        print("[DPT] 🎮 Match started - stats reset")
    end
end

function DeadlockPerformanceTracker:OnHeroSelected(data, localPlayer)
    if data.player == localPlayer then
        self.stats.currentHero = data.hero_name or "Unknown"
        
        if self.debug then
            print("[DPT] 🦸 Hero selected: " .. self.stats.currentHero)
        end
    end
end

function DeadlockPerformanceTracker:OnMatchEnded(data)
    if self.debug then
        local metrics = self:CalculateMetrics()
        print("[DPT] 🏁 Match ended - Final Score: " .. metrics.overallScore .. "%")
    end
end

-- ===== METRICS CALCULATION =====
function DeadlockPerformanceTracker:CalculateMetrics()
    local currentTime = self:GetGameTime()
    local matchTime = math.max(1, currentTime - self.stats.matchStartTime)
    local timeMinutes = matchTime / 60
    
    -- Hero-spezifische Benchmarks
    local heroBenchmark = self.heroBenchmarks[self.stats.currentHero] or self.heroBenchmarks["Default"]
    local target_spm = heroBenchmark.target_spm
    
    -- Core Metrics (exakte Formeln aus Spezifikation)
    local spm = math.floor(self.stats.souls / timeMinutes)
    local kda = self.stats.deaths > 0 and 
        math.floor(((self.stats.kills + self.stats.assists) / self.stats.deaths) * 100) / 100 or
        (self.stats.kills + self.stats.assists)
    
    -- Performance Scores (0-100) - exakte Formeln
    local farm_score = math.min(100, math.floor((spm / target_spm) * 100))  -- Hero-spezifisch
    local combat_score = math.min(100, math.floor(kda * 20))  -- 5.0 KDA = 100%
    local damage_score = math.min(100, math.floor((self.stats.heroDamage / 25000) * 100))  -- 25k = 100%
    local overall_score = math.floor((farm_score + combat_score + damage_score) / 3)
    
    -- Relative Position (simuliert)
    local team_rank = self:CalculateTeamRank(spm)
    
    return {
        spm = spm,
        kda = kda,
        farmScore = farm_score,
        combatScore = combat_score,
        damageScore = damage_score,
        overallScore = overall_score,
        targetSPM = target_spm,
        heroRole = heroBenchmark.role,
        heroDifficulty = heroBenchmark.difficulty,
        teamRank = team_rank,
        matchMinutes = timeMinutes
    }
end

function DeadlockPerformanceTracker:CalculateTeamRank(current_spm)
    -- Simuliere Team-Ranking (1-6 Position)
    local estimated_avg_spm = 350
    if current_spm >= estimated_avg_spm * 1.3 then
        return 1  -- Top Performer
    elseif current_spm >= estimated_avg_spm * 1.1 then
        return 2  -- Above Average
    elseif current_spm >= estimated_avg_spm * 0.9 then
        return 3  -- Average
    elseif current_spm >= estimated_avg_spm * 0.7 then
        return 4  -- Below Average
    else
        return 5  -- Needs Improvement
    end
end

-- ===== HUD CREATION =====
function DeadlockPerformanceTracker:CreateHUD()
    if hud_panel and hud_panel:IsValid() then
        hud_panel:Remove()
    end
    
    -- Source 2 VGUI Panel erstellen
    hud_panel = vgui.Create("DPanel")
    hud_panel:SetSize(280, 200)
    hud_panel:SetPos(position_x, position_y)
    hud_panel:SetBackgroundColor(Color(0, 0, 0, 217))  -- rgba(0,0,0,0.85)
    hud_panel:SetVisible(self.enabled)
    hud_panel:SetZPos(1000)  -- Über andere UI-Elemente
    
    self:CreateHUDElements()
    
    if self.debug then
        print("[DPT] 🎨 HUD created at position: " .. position_x .. ", " .. position_y)
    end
end

function DeadlockPerformanceTracker:CreateHUDElements()
    if not hud_panel or not hud_panel:IsValid() then return end
    
    -- Title Bar
    local title = vgui.Create("DLabel", hud_panel)
    title:SetText("Performance Tracker")
    title:SetTextColor(Color(0, 255, 136))  -- Deadlock Green
    title:SetFont("DefaultBold")
    title:SetPos(10, 5)
    title:SetSize(220, 20)
    title:SetContentAlignment(4)  -- Left
    
    -- Close Button
    local close_btn = vgui.Create("DButton", hud_panel)
    close_btn:SetText("×")
    close_btn:SetPos(250, 5)
    close_btn:SetSize(20, 20)
    close_btn:SetFont("DefaultBold")
    close_btn:SetTextColor(Color(255, 255, 255))
    close_btn.Paint = function() end  -- Transparent background
    close_btn.DoClick = function()
        self:ToggleHUD()
    end
    
    -- Metrics Row 1: SPM, KDA, Score
    self.spm_label = vgui.Create("DLabel", hud_panel)
    self.spm_label:SetText("0")
    self.spm_label:SetTextColor(Color(0, 255, 136))
    self.spm_label:SetFont("DefaultLarge")
    self.spm_label:SetPos(20, 35)
    self.spm_label:SetSize(60, 30)
    self.spm_label:SetContentAlignment(5)
    
    local spm_title = vgui.Create("DLabel", hud_panel)
    spm_title:SetText("SPM")
    spm_title:SetTextColor(Color(200, 200, 200))
    spm_title:SetPos(20, 65)
    spm_title:SetSize(60, 15)
    spm_title:SetContentAlignment(5)
    
    self.kda_label = vgui.Create("DLabel", hud_panel)
    self.kda_label:SetText("0.0")
    self.kda_label:SetTextColor(Color(0, 255, 136))
    self.kda_label:SetFont("DefaultLarge")
    self.kda_label:SetPos(100, 35)
    self.kda_label:SetSize(60, 30)
    self.kda_label:SetContentAlignment(5)
    
    local kda_title = vgui.Create("DLabel", hud_panel)
    kda_title:SetText("KDA")
    kda_title:SetTextColor(Color(200, 200, 200))
    kda_title:SetPos(100, 65)
    kda_title:SetSize(60, 15)
    kda_title:SetContentAlignment(5)
    
    self.score_label = vgui.Create("DLabel", hud_panel)
    self.score_label:SetText("0%")
    self.score_label:SetTextColor(Color(0, 255, 136))
    self.score_label:SetFont("DefaultLarge")
    self.score_label:SetPos(180, 35)
    self.score_label:SetSize(80, 30)
    self.score_label:SetContentAlignment(5)
    
    local score_title = vgui.Create("DLabel", hud_panel)
    score_title:SetText("Score")
    score_title:SetTextColor(Color(200, 200, 200))
    score_title:SetPos(180, 65)
    score_title:SetSize(80, 15)
    score_title:SetContentAlignment(5)
    
    -- Performance Bars
    self.farm_bar = self:CreateProgressBar(10, 90, "Farm", Color(0, 255, 136))
    self.combat_bar = self:CreateProgressBar(10, 110, "Combat", Color(255, 165, 0))
    self.damage_bar = self:CreateProgressBar(10, 130, "Damage", Color(255, 107, 107))
    
    -- Hero Info
    self.hero_label = vgui.Create("DLabel", hud_panel)
    self.hero_label:SetText("Hero: Unknown")
    self.hero_label:SetTextColor(Color(200, 200, 200))
    self.hero_label:SetFont("DefaultSmall")
    self.hero_label:SetPos(10, 155)
    self.hero_label:SetSize(260, 15)
    self.hero_label:SetContentAlignment(5)
    
    -- Team Rank
    self.rank_label = vgui.Create("DLabel", hud_panel)
    self.rank_label:SetText("Team Rank: -")
    self.rank_label:SetTextColor(Color(200, 200, 200))
    self.rank_label:SetFont("DefaultSmall")
    self.rank_label:SetPos(10, 175)
    self.rank_label:SetSize(260, 15)
    self.rank_label:SetContentAlignment(5)
end

function DeadlockPerformanceTracker:CreateProgressBar(x, y, title, color)
    local bar_panel = vgui.Create("DPanel", hud_panel)
    bar_panel:SetPos(x, y)
    bar_panel:SetSize(260, 15)
    bar_panel:SetBackgroundColor(Color(50, 50, 50, 150))
    
    local bar_label = vgui.Create("DLabel", bar_panel)
    bar_label:SetText(title)
    bar_label:SetTextColor(Color(255, 255, 255))
    bar_label:SetFont("DefaultSmall")
    bar_label:SetPos(5, 0)
    bar_label:SetSize(50, 15)
    
    local bar_fill = vgui.Create("DPanel", bar_panel)
    bar_fill:SetPos(60, 2)
    bar_fill:SetSize(0, 11)  -- Width wird dynamisch gesetzt
    bar_fill:SetBackgroundColor(color)
    
    local bar_text = vgui.Create("DLabel", bar_panel)
    bar_text:SetText("0%")
    bar_text:SetTextColor(Color(255, 255, 255))
    bar_text:SetFont("DefaultSmall")
    bar_text:SetPos(200, 0)
    bar_text:SetSize(50, 15)
    bar_text:SetContentAlignment(6)  -- Right
    
    return {
        panel = bar_panel,
        fill = bar_fill,
        text = bar_text,
        label = bar_label
    }
end

-- ===== HUD UPDATE =====
function DeadlockPerformanceTracker:UpdateHUD()
    if not hud_panel or not hud_panel:IsValid() or not self.enabled then
        return
    end
    
    local metrics = self:CalculateMetrics()
    
    -- Update main metrics
    self.spm_label:SetText(tostring(metrics.spm))
    self.kda_label:SetText(tostring(metrics.kda))
    self.score_label:SetText(metrics.overallScore .. "%")
    
    -- Color coding (exakte Farben aus Spezifikation)
    local spm_color = metrics.spm >= 350 and Color(0, 255, 136) or
                     metrics.spm >= 250 and Color(255, 165, 0) or
                     Color(255, 107, 107)
    self.spm_label:SetTextColor(spm_color)
    
    local kda_color = metrics.kda >= 2.0 and Color(0, 255, 136) or
                     metrics.kda >= 1.0 and Color(255, 165, 0) or  
                     Color(255, 107, 107)
    self.kda_label:SetTextColor(kda_color)
    
    local score_color = metrics.overallScore >= 70 and Color(0, 255, 136) or
                       metrics.overallScore >= 50 and Color(255, 165, 0) or
                       Color(255, 107, 107)
    self.score_label:SetTextColor(score_color)
    
    -- Update progress bars
    self:UpdateProgressBar(self.farm_bar, metrics.farmScore)
    self:UpdateProgressBar(self.combat_bar, metrics.combatScore)
    self:UpdateProgressBar(self.damage_bar, metrics.damageScore)
    
    -- Update hero info
    local hero_text = "Hero: " .. self.stats.currentHero .. " (" .. metrics.heroRole .. ")"
    self.hero_label:SetText(hero_text)
    
    -- Update team rank
    local rank_text = "Team Rank: #" .. metrics.teamRank .. " (Target: " .. metrics.targetSPM .. " SPM)"
    self.rank_label:SetText(rank_text)
    
    self.stats.lastUpdateTime = self:GetGameTime()
end

function DeadlockPerformanceTracker:UpdateProgressBar(bar, percentage)
    if not bar or not bar.fill or not bar.text then return end
    
    local width = math.floor((percentage / 100) * 190)  -- 190px max width
    bar.fill:SetSize(width, 11)
    bar.text:SetText(percentage .. "%")
    
    -- Color based on performance
    local color = percentage >= 70 and Color(0, 255, 136) or
                 percentage >= 50 and Color(255, 165, 0) or
                 Color(255, 107, 107)
    bar.fill:SetBackgroundColor(color)
end

-- ===== UTILITY FUNCTIONS =====
function DeadlockPerformanceTracker:GetLocalPlayer()
    -- Versuche lokalen Spieler zu finden
    if Players and Players.GetLocalPlayer then
        return Players:GetLocalPlayer()
    elseif PlayerResource and PlayerResource.GetLocalPlayer then
        return PlayerResource:GetLocalPlayer()
    else
        return nil
    end
end

function DeadlockPerformanceTracker:GetGameTime()
    if GameRules and GameRules.GetGameTime then
        return GameRules:GetGameTime()
    elseif self.demo_mode then
        return os.time()
    else
        return 0
    end
end

function DeadlockPerformanceTracker:PlayKillSound()
    if Sounds and Sounds.EmitSound then
        Sounds:EmitSound("ui.achievement_earned")
    end
end

function DeadlockPerformanceTracker:ToggleHUD()
    self.enabled = not self.enabled
    if hud_panel and hud_panel:IsValid() then
        hud_panel:SetVisible(self.enabled)
    end
    
    if self.debug then
        print("[DPT] HUD toggled: " .. (self.enabled and "ON" or "OFF"))
    end
end

function DeadlockPerformanceTracker:ResetStats()
    self.stats = {
        souls = 0,
        kills = 0,
        deaths = 0,
        assists = 0,
        heroDamage = 0,
        creepDamage = 0,
        matchStartTime = self:GetGameTime(),
        currentHero = self.stats.currentHero or "Unknown",
        matchDuration = 0,
        teamPosition = 0,
        lastUpdateTime = 0
    }
    
    self:UpdateHUD()
    
    if self.debug then
        print("[DPT] Stats reset")
    end
end

function DeadlockPerformanceTracker:SetPosition(x, y)
    position_x = tonumber(x) or position_x
    position_y = tonumber(y) or position_y
    
    if hud_panel and hud_panel:IsValid() then
        hud_panel:SetPos(position_x, position_y)
    end
    
    if self.debug then
        print("[DPT] HUD moved to: " .. position_x .. ", " .. position_y)
    end
end

function DeadlockPerformanceTracker:StartUpdateTimer()
    if update_timer then
        Timer.Destroy(update_timer)
    end
    
    update_timer = Timer.Create("dpt_update", 1.0, 0, function()
        if self.enabled then
            self:UpdateHUD()
        end
    end)
end

-- ===== CONSOLE COMMANDS =====
function DeadlockPerformanceTracker:RegisterConsoleCommands()
    local commands = {
        {
            name = "perf_toggle",
            callback = function()
                self:ToggleHUD()
                print("Performance HUD " .. (self.enabled and "enabled" or "disabled"))
            end,
            description = "Toggle performance overlay",
            flags = 0
        },
        {
            name = "perf_reset", 
            callback = function()
                self:ResetStats()
                print("Performance stats reset")
            end,
            description = "Reset performance statistics",
            flags = 0
        },
        {
            name = "perf_position",
            callback = function(args)
                if args[1] and args[2] then
                    self:SetPosition(args[1], args[2])
                    print("HUD moved to " .. position_x .. ", " .. position_y)
                else
                    print("Usage: perf_position <x> <y>")
                end
            end,
            description = "Set HUD position: perf_position <x> <y>",
            flags = 0
        },
        {
            name = "perf_debug",
            callback = function()
                self.debug = not self.debug
                print("Debug mode: " .. (self.debug and "ON" or "OFF"))
            end,
            description = "Toggle debug output",
            flags = 0
        },
        {
            name = "perf_demo",
            callback = function()
                self.demo_mode = not self.demo_mode
                if self.demo_mode then
                    self:InitializeDemoMode()
                    print("Demo mode enabled")
                else
                    print("Demo mode disabled")
                end
            end,
            description = "Toggle demo mode with simulated data",
            flags = 0
        },
        {
            name = "perf_info",
            callback = function()
                local metrics = self:CalculateMetrics()
                print("=== Deadlock Performance Tracker v" .. self.version .. " ===")
                print("SPM: " .. metrics.spm .. " (Target: " .. metrics.targetSPM .. ")")
                print("KDA: " .. metrics.kda)
                print("Score: " .. metrics.overallScore .. "%")
                print("Hero: " .. self.stats.currentHero .. " (" .. metrics.heroRole .. ")")
                print("Match Time: " .. string.format("%.1f", metrics.matchMinutes) .. " minutes")
                print("Demo Mode: " .. (self.demo_mode and "ON" or "OFF"))
            end,
            description = "Show detailed performance information",
            flags = 0
        }
    }
    
    -- Registriere Commands
    for _, cmd in ipairs(commands) do
        local success = pcall(function()
            if Convars and Convars.RegisterCommand then
                Convars:RegisterCommand(cmd.name, cmd.callback, cmd.description, cmd.flags)
            end
        end)
        
        if not success and self.debug then
            print("[DPT] ⚠️ Could not register command: " .. cmd.name)
        end
    end
end

-- ===== AUTO-INITIALIZATION =====
-- Warte auf Game-Ready und initialisiere
if GameRules then
    DeadlockPerformanceTracker:Initialize()
else
    local function WaitForGameReady()
        if GameRules then
            DeadlockPerformanceTracker:Initialize()
        else
            Timer.SetTimeout(function() WaitForGameReady() end, 1.0)
        end
    end
    WaitForGameReady()
end

-- Export für globalen Zugriff
_G.DeadlockPerformanceTracker = DeadlockPerformanceTracker

-- Performance Tracker erfolgreich geladen
print("✅ Deadlock Performance Tracker v" .. DeadlockPerformanceTracker.version .. " loaded!")
print("💬 Type 'perf_toggle' to start, 'perf_info' for details")