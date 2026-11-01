local function format_ago(seconds)
    seconds = tonumber(seconds)
    if not seconds or seconds < 0 then
        return "--"
    end
    if seconds < 60 then
        return "<1m ago"
    end
    local days = math.floor(seconds / 86400)
    local rem = seconds % 86400
    local hours = math.floor(rem / 3600)
    rem = rem % 3600
    local minutes = math.floor(rem / 60)
    if days > 0 then
        if hours > 0 then
            return string.format("%dd %dh ago", days, hours)
        end
        return string.format("%dd ago", days)
    end
    if hours > 0 then
        if minutes > 0 then
            return string.format("%dh %dm ago", hours, minutes)
        end
        return string.format("%dh ago", hours)
    end
    return string.format("%dm ago", minutes)
end

-- Reads snapshot.json for DeepSeek API credits.
-- Triggers the shipped fetch.cmd about once every five minutes.

FETCH_EVERY = 300
FETCH_MAX = 1800
APPLY_EVERY = 5
STALE_AFTER = FETCH_EVERY * 3

function Initialize()
    snapshotPath = SKIN:MakePathAbsolute(SKIN:GetVariable("@") .. "DeepSeek\\snapshot.json")
    lastFetch = 0
    lastApply = 0
    fetchBackoff = FETCH_EVERY
    lastCheckedAt = -1
    stampedAt = 0
    Apply()
end

local function fetch_is_running()
    local measure = SKIN:GetMeasure("MeasureFetch")
    if not measure then
        return false
    end
    return measure:GetValue() == 0
end

local function start_fetch(now)
    if fetch_is_running() then
        SKIN:Bang("!CommandMeasure", "MeasureFetch", "Kill")
        lastFetch = 0
        return
    end
    lastFetch = now
    SKIN:Bang("!CommandMeasure", "MeasureFetch", "Run")
end

function Update()
    local now = os.time()
    if now - lastApply >= APPLY_EVERY then
        Apply()
    end
    if stampedAt and stampedAt > 0 then
        SKIN:Bang("!SetVariable", "Updated", format_ago(now - stampedAt))
    end
    local every = (fetchBackoff > FETCH_EVERY) and fetchBackoff or FETCH_EVERY
    local due = (now - lastFetch >= every)
    if (not due) and lastCheckedAt >= 0 and (now - lastCheckedAt) >= every then
        due = (now - lastFetch >= APPLY_EVERY)
    end
    if due then
        start_fetch(now)
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

local function read_file(path)
    local handle = io.open(path, "r")
    if not handle then
        return nil
    end
    local raw = handle:read("*a")
    handle:close()
    return raw or ""
end

function Apply()
    lastApply = os.time()
    local raw = read_file(snapshotPath)
    if not raw then
        SKIN:Bang("!SetVariable", "USDText", "--")
        SKIN:Bang("!SetVariable", "CNYText", "--")
        SKIN:Bang("!SetVariable", "USDSub", "--")
        SKIN:Bang("!SetVariable", "CNYSub", "--")
        SKIN:Bang("!SetVariable", "Error", "Waiting for first fetch...")
        SKIN:Bang("!SetVariable", "ErrorHidden", "0")
        return
    end

    local ok = extract_bool(raw, "ok")
    if ok ~= "true" then
        local err = extract_string(raw, "error") or "Fetch failed"
        SKIN:Bang("!SetVariable", "Error", err)
        SKIN:Bang("!SetVariable", "ErrorHidden", "0")
        return
    end

    local usdText = extract_string(raw, "usd_text") or "0.00"
    local cnyText = extract_string(raw, "cny_text") or "0.00"
    local lastError = extract_string(raw, "last_error") or ""

    stampedAt = tonumber(extract_number(raw, "fetched_at")) or 0
    local updatedText = "--"
    if stampedAt > 0 then
        updatedText = format_ago(os.time() - stampedAt)
    end

    SKIN:Bang("!SetVariable", "USDText", usdText)
    SKIN:Bang("!SetVariable", "CNYText", cnyText)
    SKIN:Bang("!SetVariable", "Updated", updatedText)
    SKIN:Bang("!SetVariable", "ErrorHidden", "1")

    local checkedAt = tonumber(extract_number(raw, "checked_at")) or 0
    if checkedAt ~= lastCheckedAt then
        if lastCheckedAt >= 0 then
            lastFetch = os.time()
        end
        lastCheckedAt = checkedAt
        if lastError:lower():find("rate limit", 1, true) then
            fetchBackoff = math.min(fetchBackoff * 2, FETCH_MAX)
        else
            fetchBackoff = FETCH_EVERY
        end
    end
end
