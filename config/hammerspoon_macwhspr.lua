-- macwhspr Hammerspoon module (managed by macwhspr/setup.sh)
--
-- Provides:
--   * F18 hotkey -> SIGUSR1 to the running macwhspr daemon
--   * Ctrl-Cmd-V hotkey -> chooser of recent transcripts (re-paste any of the
--     last ~20 from cleanup_log.jsonl into the focused field)
--   * macwhspr.show(state) -> floating pill overlay for recording feedback
--
-- States accepted by show():
--   "recording"     red pulsing dot, "Recording" label
--   "transcribing"  blue spinner glyph, "Transcribing" label
--   "done"          green check, auto-hides after ~0.6 s
--   "error"         red X, auto-hides after ~1.6 s
--   "hide"          immediate hide
--
-- Install: setup.sh copies this file to ~/.hammerspoon/macwhspr.lua and
-- expects init.lua to contain `require("hs.ipc"); macwhspr = require("macwhspr")`.

local M = {}

-- ---------- F18 hotkey -> SIGUSR1 ----------
local pidFile = os.getenv("HOME") .. "/.config/macwhspr/daemon.pid"

local function toggleRecording()
    local f = io.open(pidFile, "r")
    if not f then
        hs.alert.show("macwhspr daemon not running")
        return
    end
    local pid = f:read("*a"):gsub("%s+", "")
    f:close()
    if pid == "" then
        hs.alert.show("macwhspr pid file empty")
        return
    end
    hs.execute(string.format("/bin/kill -USR1 %s", pid))
end

hs.hotkey.bind({}, "F18", toggleRecording)

-- ---------- History chooser (Ctrl-Cmd-V) ----------
-- Reads the last N entries from cleanup_log.jsonl. Picking one copies it to
-- the clipboard and sends Cmd-V into whatever window had focus before the
-- chooser opened. Skipped-cleanup entries appear too (their `cleaned` field
-- equals the raw transcript).
local logFile = os.getenv("HOME") .. "/.config/macwhspr/cleanup_log.jsonl"
local HISTORY_LIMIT = 20

local function summarize(text)
    if not text or text == "" then return "(empty)" end
    local first = text:match("^([^\n]*)") or text
    if #first > 100 then first = first:sub(1, 97) .. "..." end
    return first
end

local function prettyTs(ts)
    if not ts then return "" end
    local date, time = ts:match("^(%d%d%d%d%-%d%d%-%d%d)T(%d%d:%d%d)")
    if date and time then return date .. " " .. time .. " UTC" end
    return ts
end

local function loadRecent()
    local out = hs.execute(string.format(
        "/usr/bin/tail -n %d %q 2>/dev/null", HISTORY_LIMIT, logFile))
    if not out or out == "" then return {} end
    local entries = {}
    for line in out:gmatch("[^\r\n]+") do
        local ok, parsed = pcall(hs.json.decode, line)
        if ok and type(parsed) == "table" and parsed.cleaned then
            table.insert(entries, parsed)
        end
    end
    return entries
end

local function showHistory()
    local entries = loadRecent()
    if #entries == 0 then
        hs.alert.show("macwhspr: no history yet")
        return
    end
    local choices = {}
    for i = #entries, 1, -1 do
        local e = entries[i]
        table.insert(choices, {
            text = summarize(e.cleaned),
            subText = prettyTs(e.ts),
            cleaned = e.cleaned,
        })
    end
    local chooser = hs.chooser.new(function(choice)
        if not choice then return end
        hs.pasteboard.setContents(choice.cleaned)
        -- Small delay so focus has time to return to the previous app before
        -- we synthesize Cmd-V. Same pattern as daemon.py's osascript fallback.
        hs.timer.doAfter(0.05, function()
            hs.eventtap.keyStroke({"cmd"}, "v", 10000)
        end)
    end)
    chooser:choices(choices)
    chooser:searchSubText(true)
    chooser:show()
end

hs.hotkey.bind({"ctrl", "cmd"}, "v", showHistory)

-- ---------- Overlay (hs.canvas pill) ----------
local SPINNER = { "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏" }
local canvas, animTimer, hideTimer

local function clearTimers()
    if animTimer then animTimer:stop(); animTimer = nil end
    if hideTimer then hideTimer:stop(); hideTimer = nil end
end

local function ensureCanvas()
    if canvas then return end
    local screen = hs.screen.mainScreen():frame()
    local w, h = 184, 40
    local x = screen.x + (screen.w - w) / 2
    local y = screen.y + 60
    canvas = hs.canvas.new({ x = x, y = y, w = w, h = h })
    canvas:appendElements(
        {
            type = "rectangle",
            action = "fill",
            fillColor = { red = 0.07, green = 0.07, blue = 0.08, alpha = 0.94 },
            roundedRectRadii = { xRadius = 14, yRadius = 14 },
        },
        {
            type = "circle",
            center = { x = 22, y = 20 },
            radius = 6,
            fillColor = { red = 0.95, green = 0.25, blue = 0.25, alpha = 1.0 },
        },
        {
            type = "text",
            text = "Recording",
            frame = { x = 38, y = 11, w = 140, h = 22 },
            textColor = { white = 0.96, alpha = 1.0 },
            textSize = 14,
        }
    )
    canvas:level(hs.canvas.windowLevels.overlay)
    -- Stay visible across spaces; don't steal focus.
    local behaviors = hs.canvas.windowBehaviors.canJoinAllSpaces
        + hs.canvas.windowBehaviors.stationary
    canvas:behavior(behaviors)
end

local function setDot(r, g, b, alpha)
    canvas[2].fillColor = { red = r, green = g, blue = b, alpha = alpha or 1.0 }
end

local function setLabel(text)
    canvas[3].text = text
end

function M.show(stateName)
    local ok, err = pcall(function()
        ensureCanvas()
        clearTimers()

        if stateName == "recording" then
            setDot(0.95, 0.25, 0.25)
            setLabel("Recording")
            canvas:show()
            local on = true
            animTimer = hs.timer.doEvery(0.5, function()
                on = not on
                setDot(0.95, 0.25, 0.25, on and 1.0 or 0.35)
            end)
        elseif stateName == "transcribing" then
            setDot(0.55, 0.75, 1.0)
            setLabel(SPINNER[1] .. " Transcribing")
            canvas:show()
            local i = 1
            animTimer = hs.timer.doEvery(0.08, function()
                i = (i % #SPINNER) + 1
                setLabel(SPINNER[i] .. " Transcribing")
            end)
        elseif stateName == "done" then
            setDot(0.30, 0.85, 0.45)
            setLabel("✓ Done")
            canvas:show()
            hideTimer = hs.timer.doAfter(0.6, function()
                if canvas then canvas:hide() end
            end)
        elseif stateName == "error" then
            setDot(0.95, 0.25, 0.25)
            setLabel("✕ Error")
            canvas:show()
            hideTimer = hs.timer.doAfter(1.6, function()
                if canvas then canvas:hide() end
            end)
        elseif stateName == "hide" then
            if canvas then canvas:hide() end
        end
    end)
    if not ok then
        hs.alert.show("macwhspr overlay error: " .. tostring(err))
    end
end

return M
