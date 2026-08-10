# Hermes Discord Presence

Show what you're doing in [Hermes Agent](https://hermes-agent.nousresearch.com/docs) as a **live Rich Presence** in Discord — your friends see *what model you're on, what you're doing, and for how long*.

![Platform](https://img.shields.io/badge/platform-Windows-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What this is

A tiny Windows tray app that watches your Hermes Agent and mirrors its live state into a Discord **"Playing" activity**. It's not a static label — it updates in real time:

```
┌─────────────────────────────────────────────┐
│  🎮 Playing: Hermes Agent                   │
│  Model: deepseek/deepseek-v4-flash-0731     │
│  Running: python • 3.4M tok • $0.58  ⏱ 42m │
└─────────────────────────────────────────────┘
```

The activity appears only **while the Hermes window is open** and disappears ~ when you close it — no need to fiddle with Discord settings.

**Because it reads real state, it shows real things:**
| What | Example | Source |
|------|---------|--------|
| **Model** | `deepseek/deepseek-v4-flash-0731` — the model actually serving you | parsed live from Hermes' agent log |
| **Action** | `Running: python` / `Chatting with Hermes` / `Idle in Hermes` / `stale` | your latest tool call or message; `stale` = no updates for `stale_after` seconds |
| **Stats** | `264 msgs • 3.4M tok • $0.58` | your session counters from Hermes' database |
| **Timer** | how long you've been in the session | session start time |

And you control it from a **tray icon** — pause, open config, open log, quit. No command line needed while it runs.

## What you need

| Requirement | Notes |
|-------------|-------|
| **Windows 10/11** | the app uses a few Win32 APIs |
| **Python 3.10+** | test with whatever you have: `python --version` |
| **Hermes Agent** | installed with its default folder under `%LOCALAPPDATA%\hermes` |
| **Discord desktop app** | the free app *for Windows*, logged in (Rich Presence needs the desktop client, not the browser version) |

---

## Configure — step by step (first-time)

You do this **once**. Copy `hermes_presence.example.json` → `hermes_presence.json` (next to `hermes_presence.py`) and fill in your Discord Application ID.

### 1. Create a Discord Application

1. Go to **https://discord.com/developers/applications**
2. Click **New Application** (top right)
3. Give it a name — e.g. `Hermes Agent` (it's the *name shown in your activity*; friends will see it). Click **Create**.

### 2. Find your Application ID

1. You're now on the app's **General Information** page
2. There's a white **Application ID** number, with a **Copy** button
3. Click **Copy**

That number is the only "secret-ish" thing you need. It goes into the config below.

### 3. (Optional but nice) Load an activity icon

Without this, Discord shows a generic placeholder. To get a custom icon:

1. In the Developer Portal, in the **left sidebar** click **Rich Presence → Art Assets**
2. Drag & drop any square image (256×256 works well)
3. Give it **exactly** this name: **`hermes_logo`** (this is the default; it must match `HERMES_LARGE_IMAGE`)
4. **Save Change** / wait a minute. The icon can take a while to propagate in Discord.

> If you use a different asset name, set it in the config (`HERMES_LARGE_IMAGE`) or the env var — the script reads `HERMES_LARGE_IMAGE` first, else `hermes_logo`.

### 4. Fill in the config

Open `hermes_presence.json` (the copy you just made) and edit **at least** `discord_app_id`:

```json
{
  "hermes_path": "%LOCALAPPDATA%\\hermes",
  "state_db": "",
  "agent_log": "",
  "discord_app_id": "1234567890123456789",
  "status_template": "{action} • {tokens} tok • ${cost}",
  "poll_interval": 5,
  "stale_after": 900
}
```

| Key | Meaning | Default |
|-----|---------|---------|
| `hermes_path` | Hermes' data folder. `%LOCALAPPDATA%` is replaced automatically. Leave default unless you moved Hermes. | `%LOCALAPPDATA%\hermes` |
| `state_db` / `agent_log` | Override individual file paths (rarely needed). Empty = derive from `hermes_path`. | auto |
| `discord_app_id` | **Your** Application ID from step 2. | — |
| `status_template` | What the second line of the activity says. Fields: `{action} {model} {msgs} {tokens} {cost}`. Change the order / drop fields freely. | `{action} • {tokens} tok • ${cost}` |
| `poll_interval` | How often (seconds) it checks Hermes. | `5` |
| `stale_after` | After this many seconds of *no updates*, the status says `stale`. Too low → "stale" shows during quiet moments. | `900` |

> Every key is optional. You can also override any of them with environment variables, which win over the file:
> `HERMES_DISCORD_APP_ID`, `HERMES_HOME`, `HERMES_STATE_DB`, `HERMES_AGENT_LOG`, `HERMES_STATUS_TEMPLATE`, `HERMES_POLL_INTERVAL`, `HERMES_STALE_AFTER`.

### 5. Install and run

```bash
# from the project folder — base requirements (no tray needed to run):
pip install -r requirements.txt

# optional — tray icon (pause/resume/quit, status dot):
pip install -r requirements-tray.txt

# then start it:
python hermes_presence.py
```

On first run it creates the tray icon (if `pystray`/`Pillow` are installed; without them the app still works). That's it.

> **Test without Discord:** `python hermes_presence.py --dry-run` prints exactly
> what would be sent to Discord — no connection, no tray icon.

---

## What you should see in Discord

1. Click your **avatar** (bottom-left) → your profile card
2. Click the **Activity** tab (right side of the card)
3. You'll see **Hermes Agent — Playing**, with:
   - the model (`Model: deepseek/deepseek-v4-flash-0731`)
   - the action + stats (`Running: python • 3.4M tok • $0.58`)
   - an elapsed timer

Close the Hermes window → the activity disappears. Reopen it → it comes back.

### The tray icon

A colored dot sits in your system tray (near the clock):

| Color | Meaning |
|-------|---------|
| 🟢 green | Hermes open, connected |
| 🟡 yellow | **Waiting** for Hermes window |
| ⚪ grey | paused |
| 🔴 red | an error was logged |

**Right-click** it for the menu: pause/resume, open the config, open the log, or **Quit** (which also removes the activity and cleans up — nothing left in Task Manager).

---

## Running at startup (auto-start)

1. Press **Win + R**, type `shell:startup`, press **Enter**
2. Create a file `hermes_presence.bat` there:
```bat
@echo off
start "" "C:\Path\To\pythonw.exe" "C:\Path\To\hermes_presence.py"
```
> Use `pythonw.exe` (not `python.exe`) so it runs with **no console window**. Find its real path with `where pythonw`.

---

## FAQ

### Discord doesn't show the presence
- Is the **Discord desktop app** running and logged in? (The browser version doesn't support Rich Presence.)
- Is your **Application ID** correctly in `discord_app_id` and not blank?
- Is the **Hermes window open**? The activity only shows while the window is visible. If the tray is **yellow**, it means "waiting for Hermes" — open Hermes.
- Try **restarting the app** after changing the ID.

### "Hermes not found" / presence never appears
Check the **tray color**. If it's yellow (waiting), the script can't find a window whose title contains **"hermes"**:
- Is the Hermes window actually open on screen?
- Did you rename it? If its title no longer contains `hermes`, the script won't detect it — that's expected behaviour.

### `state.db` is missing / stats are blank
The script looks for `%LOCALAPPDATA%\hermes\state.db`:
- Confirm Hermes has been launched **at least once** (it creates the database).
- If you installed Hermes elsewhere, set `hermes_path` (or `state_db`) in the config.
- A missing database is not fatal — the script logs it and keeps retrying; the activity just won't show stats until it appears.

### Status is stuck / shows "stale"
"Stale" means neither the log nor the database updated for `stale_after` seconds — Hermes is either idle, closed, or froze.
- If you're just not using Hermes for a while, that's normal — bump `stale_after` if you find it noisy.
- If you *were* actively using Hermes but it still shows stale, check `hermes_presence.log` for errors.

### Discord was restarted (or crashed)
**Nothing to do.** The app detects the lost connection and **reconnects automatically** — you'll see `RPC connected` in the log. No manual restart needed.

### I changed the config, why nothing happened?
Config is read at startup. Restart the app (`Quit` from the tray, then run it again) or restart via the `.bat`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `pypresence is not installed` | `pip install -r requirements.txt` with the **same** interpreter you run the script with |
| Console window flashes every few seconds | You used `python.exe`; switch to `pythonw.exe` (this script never spawns subprocesses, so it's silent on its own) |
| No tray icon | Install the optional deps: `pip install -r requirements-tray.txt`; the app still works without the tray |
| Activity shows wrong model | It's read from the agent log; the "stale" suffix means the log hasn't updated recently |
| Everything looks right but it's still broken | Read `%USERPROFILE%\hermes_presence.log` — every decision and error is logged there |

---

## Privacy

This script never reads your chat history. It only:
- checks whether a window titled *Hermes* is visible,
- regex-extracts `model=…` from the **last 200 KB** of the agent log,
- reads aggregate counters (message count, tokens, cost) from Hermes' database.

The **only** network traffic is the Discord Rich Presence update itself. The database is opened **read-only**.

## Author

**[anglmertv](https://github.com/anglmertv)** — built with [Hermes Agent](https://hermes-agent.nousresearch.com/docs).

## License

[MIT](LICENSE)
