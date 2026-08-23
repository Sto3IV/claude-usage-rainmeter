-- AI usage panels for Before Dawn RM Bar.
--
-- Consolidates what used to be three separate AIUsageLimits skins: reads their
-- snapshot.json files, recomputes the reset countdowns locally every tick, and
-- schedules the shipped fetch.cmd for each service on its own timer.
--
-- The fetchers and snapshots stay in Skins\AIUsageLimits\@Resources\ so there is
-- exactly one copy of them. This script only reads.

-- Claude's endpoint refuses sustained polling (at 1/min, three of four requests
-- came back 429, answering Retry-After: 0). Antigravity costs ~2.2s of
-- subprocess spawning per poll. Grok ran at 60s while it scraped a local log;
-- since its fetcher queries cli-chat-proxy live it shares the same cadence,
-- because that endpoint's rate limit is unmeasured and the backoff below is the
-- only thing standing between us and Claude's 429 story.
FETCH_EVERY_REMOTE = 300

-- Ceiling for the failure backoff.
FETCH_MAX = 1800

-- A window whose reset time already passed: check sooner for the next one, but
-- never faster than the service's own cadence. Taken as a min(), which now that
-- every service sits at 300 uniformly means a 300 -> 120 rush at the rollover.
FETCH_LAPSED = 120

-- Re-read the snapshots on a timer. A fetch landing is not the only way they
-- change -- running fetch.cmd by hand should show up too.
APPLY_EVERY = 5

-- Three missed cycles before the header admits the data is old, floored so that
-- lowering a cadence cannot make the header flash "3m old" over one hiccup. At
-- 300 the multiplier wins (900s) and the floor is currently inert.
STALE_MULTIPLIER = 3
STALE_FLOOR = 600

SERVICES = {
    { prefix = "Claude", dir = "Claude",      measure = "MeasureFetchClaude", every = FETCH_EVERY_REMOTE, session = true  },
    { prefix = "Anti",   dir = "Antigravity", measure = "MeasureFetchAnti",   every = FETCH_EVERY_REMOTE, session = true  },
    { prefix = "Grok",   dir = "Grok",        measure = "MeasureFetchGrok",   every = FETCH_EVERY_REMOTE, session = false },
}

function Initialize()
    local root = SKIN:GetVariable("AIRoot") or ""
    for _, svc in ipairs(SERVICES) do
        svc.path = root .. svc.dir .. "\\snapshot.json"
        svc.sessionResetUnix = 0
        svc.weeklyResetUnix = 0
        svc.lastFetch = 0
        svc.lastApply = 0
        svc.lapsed = false
        svc.backoff = svc.every
        -- -1 so the first Apply adopts the on-disk checked_at without arming the
        -- fetch timer, keeping the fetch-immediately-on-load behaviour.
        svc.lastCheckedAt = -1
        svc.shown = {}
        Apply(svc)
    end
end

function Update()
    local now = os.time()
    local dirty = false
    for _, svc in ipairs(SERVICES) do
        if Tick(svc, now) then
            dirty = true
        end
        if now - svc.lastApply >= APPLY_EVERY then
            if Apply(svc) then
                dirty = true
            end
        end
        -- A backoff in progress outranks the lapsed-window rush, so a rollover
        -- cannot restart hammering while a service is still refusing us.
        local every
        if svc.backoff > svc.every then
            every = svc.backoff
        elseif svc.lapsed then
            every = math.min(svc.every, FETCH_LAPSED)
        else
            every = svc.every
        end
        if now - svc.lastFetch >= every then
            svc.lastFetch = now
            SKIN:Bang("!CommandMeasure", svc.measure, "Run")
        end
    end
    if dirty then
        SKIN:Bang("!Redraw")
    end
    return 0
end

--=============================================================================
-- snapshot.json scraping (flat key/value, no JSON parser needed)
--=============================================================================

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

--=============================================================================
-- formatting
--=============================================================================

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

local function format_countdown_seconds(seconds)
    seconds = tonumber(seconds)
    -- No reset time and a window that already lapsed mean the same thing to a
    -- reader: nothing is counting down.
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

-- The header has room for a few characters, not a sentence. "1390m ago" also
-- reads as noise; at that range the hour is the only digit worth showing.
local function format_age(seconds)
    if seconds >= 86400 then
        return string.format("%dd old", math.floor(seconds / 86400))
    end
    if seconds >= 3600 then
        return string.format("%dh old", math.floor(seconds / 3600))
    end
    return string.format("%dm old", math.floor(seconds / 60))
end

-- Seven characters, hard limit. The status sits on the header row to the right
-- of a centred title, and "ANTIGRAVITY" is wide enough that its ink ends at
-- x=670 with the panel edge at 710 -- so anything longer collides with it.
-- Measured, not guessed: "rate limited" overlapped the title by 20px.
local function short_status(text)
    local low = (text or ""):lower()
    if low == "" then
        return "error"
    end
    if low:find("rate limit", 1, true) then
        return "limited"
    end
    if low:find("no credentials", 1, true) or low:find("log in", 1, true) then
        return "no auth"
    end
    if low:find("expired", 1, true) then
        return "expired"
    end
    if low:find("unreachable", 1, true) or low:find("not running", 1, true) then
        return "offline"
    end
    if low:find("malformed", 1, true) or low:find("unexpected", 1, true) then
        return "corrupt"
    end
    if low:find("timed out", 1, true) or low:find("timeout", 1, true) then
        return "timeout"
    end
    return "error"
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
        year = tonumber(y), month = tonumber(mo), day = tonumber(d),
        hour = tonumber(h), min = tonumber(mi), sec = tonumber(s),
        isdst = false,
    })
    return as_local - timezone_offset()
end

--=============================================================================
-- variable plumbing
--=============================================================================

-- Only bang Rainmeter when the rendered value actually changed. Meters carry
-- DynamicVariables=1, so a set is enough -- but at 1Hz across three services
-- that would be a lot of pointless traffic.
local function put(svc, suffix, value)
    if svc.shown[suffix] == value then
        return false
    end
    svc.shown[suffix] = value
    SKIN:Bang("!SetVariable", svc.prefix .. suffix, value)
    return true
end

function Tick(svc, now)
    local dirty = false
    svc.lapsed = false

    if svc.session then
        local text = "--"
        if svc.sessionResetUnix > 0 then
            local left = svc.sessionResetUnix - now
            svc.lapsed = svc.lapsed or left <= 0
            text = format_countdown_seconds(left)
        end
        dirty = put(svc, "SessionReset", text) or dirty
    end

    local text = "--"
    if svc.weeklyResetUnix > 0 then
        local left = svc.weeklyResetUnix - now
        svc.lapsed = svc.lapsed or left <= 0
        text = format_countdown_seconds(left)
    end
    dirty = put(svc, "WeeklyReset", text) or dirty

    -- The age suffix has to move on its own, not only when a fetch lands.
    if svc.stampedAt and svc.stampedAt > 0 then
        local age = now - svc.stampedAt
        if age > math.max(svc.every * STALE_MULTIPLIER, STALE_FLOOR) then
            dirty = put(svc, "Status", format_age(age)) or dirty
        elseif not svc.hardError then
            dirty = put(svc, "Status", "") or dirty
        end
    end

    return dirty
end

local function blank(svc, status)
    local dirty = false
    if svc.session then
        dirty = put(svc, "Session", "0") or dirty
        dirty = put(svc, "SessionText", "--") or dirty
        dirty = put(svc, "SessionReset", "--") or dirty
    end
    dirty = put(svc, "Weekly", "0") or dirty
    dirty = put(svc, "WeeklyText", "--") or dirty
    dirty = put(svc, "WeeklyReset", "--") or dirty
    dirty = put(svc, "Status", status) or dirty
    svc.sessionResetUnix = 0
    svc.weeklyResetUnix = 0
    svc.stampedAt = 0
    svc.hardError = true
    return dirty
end

function Apply(svc)
    svc.lastApply = os.time()
    local raw = read_file(svc.path)
    if not raw then
        svc.lastFetch = 0
        return blank(svc, "no data")
    end

    if extract_bool(raw, "ok") ~= "true" then
        return blank(svc, short_status(extract_string(raw, "error")))
    end

    local dirty = false
    svc.hardError = false

    if svc.session then
        local used = extract_number(raw, "session_used")
        dirty = put(svc, "Session", used or "0") or dirty
        dirty = put(svc, "SessionText", format_pct(used)) or dirty
        svc.sessionResetUnix = tonumber(extract_number(raw, "session_reset_unix")) or 0
        if svc.sessionResetUnix <= 0 then
            svc.sessionResetUnix = iso_to_unix(extract_string(raw, "session_resets_at"))
        end
    end

    local used = extract_number(raw, "weekly_used")
    dirty = put(svc, "Weekly", used or "0") or dirty
    dirty = put(svc, "WeeklyText", format_pct(used)) or dirty
    svc.weeklyResetUnix = tonumber(extract_number(raw, "weekly_reset_unix")) or 0
    if svc.weeklyResetUnix <= 0 then
        svc.weeklyResetUnix = iso_to_unix(extract_string(raw, "weekly_resets_at"))
    end

    svc.stampedAt = tonumber(extract_number(raw, "fetched_at")) or 0


    -- Advance the backoff once per fetch ATTEMPT, not once per read. Apply runs
    -- every APPLY_EVERY seconds while the snapshot only changes when a fetch
    -- lands, so doubling per read would hit FETCH_MAX inside a minute.
    local checkedAt = tonumber(extract_number(raw, "checked_at")) or 0
    if checkedAt ~= svc.lastCheckedAt then
        if svc.lastCheckedAt >= 0 then
            -- A landed fetch restarts the interval, so a manual run of
            -- fetch.cmd also postpones the next scheduled one.
            svc.lastFetch = os.time()
        end
        svc.lastCheckedAt = checkedAt
        if (extract_string(raw, "last_error") or "") ~= "" then
            svc.backoff = math.min(svc.backoff * 2, FETCH_MAX)
        else
            svc.backoff = svc.every
        end
    end

    return Tick(svc, os.time()) or dirty
end

-- Called from each fetcher's FinishAction so a landed fetch shows immediately
-- instead of waiting out the APPLY_EVERY poll.
function Refresh()
    local dirty = false
    for _, svc in ipairs(SERVICES) do
        if Apply(svc) then
            dirty = true
        end
    end
    if dirty then
        SKIN:Bang("!Redraw")
    end
end
