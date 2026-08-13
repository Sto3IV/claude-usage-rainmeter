# Claude Usage

A small [illustro](https://docs.rainmeter.net/manual/installing-skins/)-style Rainmeter skin for **Claude Code** plan limits.

[![GitHub release](https://img.shields.io/github/v/release/Sto3IV/claude-usage-rainmeter)](https://github.com/Sto3IV/claude-usage-rainmeter/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Shows how much of your 5-hour session and 7-day window you’ve used, plus a live countdown to each reset.

## Screenshots

![Claude Usage widget](screenshots/widget.png)

Size next to the Start button and a typical clock widget:

<p>
<img src="screenshots/compare-start.png" alt="Widget next to Start / taskbar icons" height="178" />
<img src="screenshots/compare-clock.png" alt="Widget next to a clock" height="178" />
</p>

## Features

- Session (5h) and Weekly (7d) usage bars
- Reset countdown that ticks every second
- Usage numbers refresh about once a minute
- Click the title (or right-click → **Refresh now**) for an immediate update
- Same look as the built-in illustro skins

## Requirements

- Windows 10 or later
- [Rainmeter 4.5+](https://www.rainmeter.net/)
- [Python 3.10+](https://www.python.org/downloads/) with the **py launcher** enabled (default)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and logged in (`claude`)

## Getting started

### Automatic install

1. Download the latest `.rmskin` from [Releases](https://github.com/Sto3IV/claude-usage-rainmeter/releases/latest).
2. Double-click it and go through the Rainmeter installer.
3. The skin loads as `ClaudeUsage\ClaudeUsage.ini`. Drag it wherever you want.

### Manual install

1. Copy `Skins\ClaudeUsage` into `Documents\Rainmeter\Skins\`.
2. Right-click the Rainmeter tray icon → **Refresh all**.
3. Load **ClaudeUsage** from Manage.

## How authentication works

Uses the same local login as Claude Code (`CLAUDE_CODE_OAUTH_TOKEN`, or `%USERPROFILE%\.claude\.credentials.json`). Tokens stay there — they are not in the `.rmskin` or this repo.

If you are not logged in, or the token has expired, the skin shows an error instead of a fake 0%. Re-run `claude` and refresh.

The usage endpoint is shared with Claude Code. Don’t poll faster than once a minute or you’ll get rate-limited.

## Building

```bat
git clone https://github.com/Sto3IV/claude-usage-rainmeter.git
cd claude-usage-rainmeter
py -3 tests/test_fetch.py
py -3 scripts/package.py
```

## Special thanks

- For inspiration: [bozdemir/claude-usage-widget](https://github.com/bozdemir/claude-usage-widget)
- Panel artwork: **illustro** by poiru ([CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/))
