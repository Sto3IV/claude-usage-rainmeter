-- Reads the snapshot written by the shipped fetch.py. Displays used%, not remaining.

function Initialize()
    snapshotPath = SKIN:MakePathAbsolute(SKIN:GetVariable("@") .. "snapshot.json")
    Apply()
end

function Update()
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

function Apply()
    local handle = io.open(snapshotPath, "r")
    if not handle then
        SKIN:Bang("!SetVariable", "SessionUsedText", "--")
        SKIN:Bang("!SetVariable", "WeeklyUsedText", "--")
        SKIN:Bang("!SetVariable", "SessionReset", "--")
        SKIN:Bang("!SetVariable", "WeeklyReset", "--")
        SKIN:Bang("!SetVariable", "Error", "Waiting for first fetch...")
        SKIN:Bang("!SetVariable", "HasData", "0")
        SKIN:Bang("!SetVariable", "ErrorHidden", "0")
        return
    end
    local raw = handle:read("*a") or ""
    handle:close()

    local ok = extract_bool(raw, "ok")
    local err = extract_string(raw, "error") or ""
    if ok == "true" then
        local session = extract_number(raw, "session_used")
        local weekly = extract_number(raw, "weekly_used")
        SKIN:Bang("!SetVariable", "SessionUsed", session or "0")
        SKIN:Bang("!SetVariable", "WeeklyUsed", weekly or "0")
        SKIN:Bang("!SetVariable", "SessionUsedText", format_pct(session))
        SKIN:Bang("!SetVariable", "WeeklyUsedText", format_pct(weekly))
        SKIN:Bang("!SetVariable", "SessionReset", extract_string(raw, "session_reset") or "--")
        SKIN:Bang("!SetVariable", "WeeklyReset", extract_string(raw, "weekly_reset") or "--")
        SKIN:Bang("!SetVariable", "Error", "")
        SKIN:Bang("!SetVariable", "HasData", "1")
        SKIN:Bang("!SetVariable", "ErrorHidden", "1")
        return
    end

    if err == "" then
        err = "Usage fetch failed"
    end
    SKIN:Bang("!SetVariable", "SessionUsedText", "--")
    SKIN:Bang("!SetVariable", "WeeklyUsedText", "--")
    SKIN:Bang("!SetVariable", "SessionReset", "--")
    SKIN:Bang("!SetVariable", "WeeklyReset", "--")
    SKIN:Bang("!SetVariable", "Error", err)
    SKIN:Bang("!SetVariable", "HasData", "0")
    SKIN:Bang("!SetVariable", "ErrorHidden", "0")
end
