-- macwhspr bootstrap for Hammerspoon.
--
-- The actual logic (F18 hotkey + overlay) lives in ~/.hammerspoon/macwhspr.lua,
-- installed by setup.sh. This file is the snippet that gets appended to
-- ~/.hammerspoon/init.lua so Hammerspoon loads the module and exposes it as a
-- global (for the `hs -c "macwhspr.show(...)"` CLI bridge used by the daemon).
--
-- Wrapped in BEGIN/END markers so setup.sh can replace it idempotently on
-- subsequent re-installs.

-- BEGIN macwhspr (managed) --
require("hs.ipc")
macwhspr = require("macwhspr")
-- END macwhspr (managed) --
