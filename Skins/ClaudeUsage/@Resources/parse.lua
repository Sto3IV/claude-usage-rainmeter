-- Reads snapshot.json. Recalculates reset countdowns every tick from epoch.
-- Triggers the shipped fetch.cmd about once a minute.

FETCH_EVERY = 60

function Initialize()
    snapshotPath = SKIN:MakePathAbsolute(SKIN:GetVariable("@") .. "snapshot.json")
    sessionResetUnix = 0
    weeklyResetUnix = 0
    lastSessionReset = nil
    lastWeeklyReset = nil
    lastFetch = 0
    Apply()
    if sessionResetUnix > 0 or weeklyResetUnix > 0 then
        lastFetch = os.time()
    end
end

function Update()
    TickCountdowns()
    local now = os.time()
    if now - lastFetch >= FETCH_EVERY then
        lastFetch = now
        SKIN:Bang("!CommandMeasure", "MeasureFetch", "Run")
    end
    return 0
end

local function extract_number(raw, key)
    return raw:match('"' .. key .. '"%s*:%s*([%-%d%.]+)')
end

local function extract_string(raw, key)
    return raw:match('"' .. key .. '"%s*:%s*"(.-)"')
end

local function extract_bool(raw, key)
    return raw:match('"' .. key .. '"%s*:%s*(%a+)')
end

local function format_pct(value)
    local number = tonumber(value)
    if not number then
        return "--"
    end
    if math.abs(number - math.floor(number + 0.5)) < 0.05 then
        return string.format("%d%%", math.floor(number + 0.5))
    end
    return string.format("%.1f%%", number)
end

local function timezone_offset()
    local now = os.time()
    local localt = os.date("*t", now)
    local utct = os.date("!*t", now)
    localt.isdst = false
    utct.isdst = false
    return os.difftime(os.time(localt), os.time(utct))
end

local function iso_to_unix(iso)
    if not iso or iso == "" then
        return 0
    end
    local y, mo, d, h, mi, s = iso:match("^(%d+)%-(%d+)%-(%d+)T(%d+):(%d+):(%d+)")
    if not y then
        return 0
    end
    local as_local = os.time({
        year = tonumber(y),
        month = tonumber(mo),
        day = tonumber(d),
        hour = tonumber(h),
        min = tonumber(mi),
        sec = tonumber(s),
        isdst = false,
    })
    return as_local - timezone_offset()
end

local function format_countdown_seconds(seconds)
    seconds = tonumber(seconds)
    if not seconds then
        return "--"
    end
    if seconds <= 0 then
        return "now"
    end
    local days = math.floor(seconds / 86400)
    local rem = seconds % 86400
    local hours = math.floor(rem / 3600)
    rem = rem % 3600
    local minutes = math.floor(rem / 60)
    if days > 0 then
        if hours > 0 then
            return string.format("%dd %dh", days, hours)
        end
        return string.format("%dd", days)
    end
    if hours > 0 then
        if minutes > 0 then
            return string.format("%dh %dm", hours, minutes)
        end
        return string.format("%dh", hours)
    end
    if minutes > 0 then
        return string.format("%dm", minutes)
    end
    return "<1m"
end

function TickCountdowns()
    local session = "--"
    local weekly = "--"
    if sessionResetUnix and sessionResetUnix > 0 then
        session = format_countdown_seconds(sessionResetUnix - os.time())
    end
    if weeklyResetUnix and weeklyResetUnix > 0 then
        weekly = format_countdown_seconds(weeklyResetUnix - os.time())
    end
    local dirty = false
    if session ~= lastSessionReset then
        lastSessionReset = session
        SKIN:Bang("!SetVariable", "SessionReset", session)
        SKIN:Bang("!UpdateMeter", "meterSessionReset")
        dirty = true
    end
    if weekly ~= lastWeeklyReset then
        lastWeeklyReset = weekly
        SKIN:Bang("!SetVariable", "WeeklyReset", weekly)
        SKIN:Bang("!UpdateMeter", "meterWeeklyReset")
        dirty = true
    end
    if dirty then
        SKIN:Bang("!Redraw")
    end
end

function Apply()
    local handle = io.open(snapshotPath, "r")
    if not handle then
        sessionResetUnix = 0
        weeklyResetUnix = 0
        SKIN:Bang("!SetVariable", "SessionUsedText", "--")
        SKIN:Bang("!SetVariable", "WeeklyUsedText", "--")
        SKIN:Bang("!SetVariable", "SessionReset", "--")
        SKIN:Bang("!SetVariable", "WeeklyReset", "--")
        SKIN:Bang("!SetVariable", "Error", "Waiting for first fetch...")
        SKIN:Bang("!SetVariable", "HasData", "0")
        SKIN:Bang("!SetVariable", "ErrorHidden", "0")
        lastFetch = 0
        return
    end
    local raw = handle:read("*a") or ""
    handle:close()

    local ok = extract_bool(raw, "ok")
    local err = extract_string(raw, "error") or ""
    if ok == "true" then
        local session = extract_number(raw, "session_used")
        local weekly = extract_number(raw, "weekly_used")
        sessionResetUnix = tonumber(extract_number(raw, "session_reset_unix")) or 0
        weeklyResetUnix = tonumber(extract_number(raw, "weekly_reset_unix")) or 0
        if sessionResetUnix <= 0 then
            sessionResetUnix = iso_to_unix(extract_string(raw, "session_resets_at"))
        end
        if weeklyResetUnix <= 0 then
            weeklyResetUnix = iso_to_unix(extract_string(raw, "weekly_resets_at"))
        end
        SKIN:Bang("!SetVariable", "SessionUsed", session or "0")
        SKIN:Bang("!SetVariable", "WeeklyUsed", weekly or "0")
        SKIN:Bang("!SetVariable", "SessionUsedText", format_pct(session))
        SKIN:Bang("!SetVariable", "WeeklyUsedText", format_pct(weekly))
        SKIN:Bang("!SetVariable", "Error", "")
        SKIN:Bang("!SetVariable", "HasData", "1")
        SKIN:Bang("!SetVariable", "ErrorHidden", "1")
        lastSessionReset = nil
        lastWeeklyReset = nil
        TickCountdowns()
        return
    end

    if err == "" then
        err = "Usage fetch failed"
    end
    sessionResetUnix = 0
    weeklyResetUnix = 0
    SKIN:Bang("!SetVariable", "SessionUsedText", "--")
    SKIN:Bang("!SetVariable", "WeeklyUsedText", "--")
    SKIN:Bang("!SetVariable", "SessionReset", "--")
    SKIN:Bang("!SetVariable", "WeeklyReset", "--")
    SKIN:Bang("!SetVariable", "Error", err)
    SKIN:Bang("!SetVariable", "HasData", "0")
    SKIN:Bang("!SetVariable", "ErrorHidden", "0")
end
