-- Reads snapshot.json. Recalculates the weekly reset countdown every tick.

-- Stays at a minute: this one reads a local .jsonl in ~0.4s, so there is no
-- remote budget to spend and nothing to gain by slowing it down.
FETCH_EVERY = 60

-- Ceiling for the failure backoff.
FETCH_MAX = 1800

-- A window whose reset time already passed: check sooner for the next one,
-- rather than showing a lapsed window for a whole cycle.
FETCH_LAPSED = 60

-- Re-read snapshot.json on a timer. FinishAction alone is not enough: if that
-- bang is ever missed the skin would show stale numbers forever with no tell.
APPLY_EVERY = 5

-- Three missed cycles: a hiccup stays quiet, a real outage speaks up. Derived
-- rather than hardcoded so it cannot drift out of step with FETCH_EVERY.
STALE_AFTER = FETCH_EVERY * 3

function Initialize()
    snapshotPath = SKIN:MakePathAbsolute(SKIN:GetVariable("@") .. "Grok\\snapshot.json")
    weeklyResetUnix = 0
    lastWeeklyReset = nil
    lastFetch = 0
    lastApply = 0
    lapsed = false
    fetchBackoff = FETCH_EVERY
    -- -1 so the first Apply() adopts the on-disk checked_at without arming the
    -- fetch timer, keeping the fetch-immediately-on-load behaviour.
    lastCheckedAt = -1
    Apply()
end

function Update()
    TickCountdowns()
    local now = os.time()
    if now - lastApply >= APPLY_EVERY then
        Apply()
    end
    -- A backoff in progress outranks the lapsed-window rush, so a rollover
    -- cannot restart hammering while the source is still failing.
    local every
    if fetchBackoff > FETCH_EVERY then
        every = fetchBackoff
    elseif lapsed then
        every = FETCH_LAPSED
    else
        every = FETCH_EVERY
    end
    if now - lastFetch >= every then
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
    -- No reset time and a window that already lapsed both mean the same thing
    -- to a reader: nothing is counting down.
    if not seconds or seconds <= 0 then
        return "--"
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
    local weekly = "--"
    lapsed = false
    if weeklyResetUnix and weeklyResetUnix > 0 then
        local left = weeklyResetUnix - os.time()
        lapsed = left <= 0
        weekly = format_countdown_seconds(left)
    end
    if weekly ~= lastWeeklyReset then
        lastWeeklyReset = weekly
        SKIN:Bang("!SetVariable", "WeeklyReset", weekly)
        SKIN:Bang("!UpdateMeter", "meterWeeklyReset")
        SKIN:Bang("!Redraw")
    end
end

-- Failure backoff, plus the "is what we're showing still trustworthy" line.
-- Split out of Apply() because it is identical in every skin and is easy to get
-- subtly wrong in one of them.
function UpdateHealth(raw)
    local lastError = extract_string(raw, "last_error") or ""
    local checkedAt = tonumber(extract_number(raw, "checked_at")) or 0

    -- Advance the backoff once per fetch ATTEMPT, not once per read. Apply()
    -- runs every APPLY_EVERY seconds while the snapshot only changes when a
    -- fetch lands, so doubling per read would hit FETCH_MAX inside a minute.
    if checkedAt ~= lastCheckedAt then
        if lastCheckedAt >= 0 then
            -- A landed fetch restarts the interval, which also lets a manual
            -- refresh postpone the next scheduled one.
            lastFetch = os.time()
        end
        lastCheckedAt = checkedAt
        if lastError ~= "" then
            fetchBackoff = math.min(fetchBackoff * 2, FETCH_MAX)
        else
            fetchBackoff = FETCH_EVERY
        end
    end

    -- Stay quiet about a failure the next cycle will fix: while the data is
    -- still fresh, what is on screen is correct. Snapshots predating fetched_at
    -- carry no stamp and skip the check rather than cry wolf.
    local stampedAt = tonumber(extract_number(raw, "fetched_at")) or 0
    local age = (stampedAt > 0) and (os.time() - stampedAt) or 0
    if age <= STALE_AFTER then
        SKIN:Bang("!SetVariable", "Error", "")
        SKIN:Bang("!SetVariable", "ErrorHidden", "1")
        return
    end
    local note
    if lastError ~= "" then
        note = string.format("%s (%dm old)", lastError, math.floor(age / 60))
    else
        note = string.format("Stale data -- last fetch %dm ago", math.floor(age / 60))
    end
    SKIN:Bang("!SetVariable", "Error", note)
    SKIN:Bang("!SetVariable", "ErrorHidden", "0")
end

function Apply()
    lastApply = os.time()
    local handle = io.open(snapshotPath, "r")
    if not handle then
        weeklyResetUnix = 0
        SKIN:Bang("!SetVariable", "WeeklyUsedText", "--")
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
        local weekly = extract_number(raw, "weekly_used")
        weeklyResetUnix = tonumber(extract_number(raw, "weekly_reset_unix")) or 0
        if weeklyResetUnix <= 0 then
            weeklyResetUnix = iso_to_unix(extract_string(raw, "weekly_resets_at"))
        end
        SKIN:Bang("!SetVariable", "WeeklyUsed", weekly or "0")
        SKIN:Bang("!SetVariable", "WeeklyUsedText", format_pct(weekly))
        SKIN:Bang("!SetVariable", "HasData", "1")
        UpdateHealth(raw)
        lastWeeklyReset = nil
        TickCountdowns()
        return
    end

    if err == "" then
        err = "Usage fetch failed"
    end
    weeklyResetUnix = 0
    SKIN:Bang("!SetVariable", "WeeklyUsedText", "--")
    SKIN:Bang("!SetVariable", "WeeklyReset", "--")
    SKIN:Bang("!SetVariable", "Error", err)
    SKIN:Bang("!SetVariable", "HasData", "0")
    SKIN:Bang("!SetVariable", "ErrorHidden", "0")
end
