# Claude Usage for Rainmeter

An [illustro](https://docs.rainmeter.net/manual/installing-skins/)-styled Rainmeter skin that shows your **Claude Code** 5-hour and 7-day usage: used percent, a thin amber bar, and the reset countdown.

It talks to the same `https://api.anthropic.com/api/oauth/usage` endpoint Claude Code uses. It does **not** ship an API key and it cannot log into your account.

![Rainmeter](https://img.shields.io/badge/Rainmeter-4.5%2B-76c1ff?logo=rainmeter)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

## Requirements

- Windows, [Rainmeter](https://www.rainmeter.net/) 4.5+
- [Python 3.10+](https://www.python.org/downloads/) with the **py launcher** ticked in the installer (this is the default)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and logged in once (`claude`)

## Install

1. Download the `.rmskin` from the [latest release](https://github.com/Sto3IV/claude-usage-rainmeter/releases/latest).
2. Double-click it and press **Install**.
3. Rainmeter loads `ClaudeUsage\ClaudeUsage.ini`. Drag it wherever you want.

Manual install: copy the `Skins/ClaudeUsage` folder into your Rainmeter Skins directory (usually `Documents\Rainmeter\Skins\`) and refresh Rainmeter.

## What you see

| Row | Meaning |
| --- | --- |
| Session (5h) | Utilization of the current 5-hour window (0% = unused) |
| Weekly (7d) | Utilization of the 7-day window |
| Resets | Countdown until that window rolls over |

Numbers refresh every 60 seconds. Left-click the title (or right-click → **Refresh now**) to fetch immediately.

## How authentication works

The skin never stores your token. On each poll it looks up Claude Code's own credentials, in this order:

1. Environment variable `CLAUDE_CODE_OAUTH_TOKEN`, if set
2. `%USERPROFILE%\.claude\.credentials.json` → `claudeAiOauth.accessToken`

That file stays on your machine. It is not copied into the skin, the `.rmskin`, or this repository.

If the token is missing, expired, or Anthropic returns 401/429, the skin shows an explicit error string. It does **not** invent `0%`.

## Privacy / what is *not* in this package

- No OAuth access or refresh tokens
- No Anthropic API keys
- No baked-in account path or Python install path
- Runtime snapshot (`@Resources/snapshot.json`) holds only used/remaining percents and reset labels. Delete it anytime; the next poll recreates it.

If you fork or audit a checkout, `python tests/test_fetch.py` includes a hygiene check that greps the shipped files for token prefixes and local account paths.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `Python 3 not found` | Install Python from python.org and leave **py launcher** enabled. Rainmeter is a GUI app and does not use your PowerShell PATH. |
| `No credentials found` | Run `claude` once and complete login. |
| `Credentials expired` | Re-run `claude` to refresh the OAuth token. |
| `Rate limited` | The usage endpoint is shared with Claude Code. Wait a minute; the skin already polls only once per 60s. |

## Develop

```bat
git clone https://github.com/Sto3IV/claude-usage-rainmeter.git
cd claude-usage-rainmeter
py -3 tests/test_fetch.py
```

To load the working copy in Rainmeter, copy `Skins\ClaudeUsage` into your Skins folder (or point Rainmeter's `SkinPath` at this repo's `Skins` directory).

Package a release `.rmskin`:

```bat
py -3 scripts/package.py
```

## Credits

- Usage endpoint contract and token lookup order match [bozdemir/claude-usage-widget](https://github.com/bozdemir/claude-usage-widget).
- Visual language (9-slice panel, Trebuchet, 1px amber bar) follows **illustro** by poiru. `Background.png` is the illustro panel, [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/). The rest of this project is MIT.

## License

Code: [MIT](LICENSE) © Sto3IV.

`Skins/ClaudeUsage/@Resources/Background.png`: © poiru / illustro, Creative Commons Attribution-NonCommercial-ShareAlike 3.0.
