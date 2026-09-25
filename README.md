# Waypoint Suite · Parley setup

One local dashboard runs your **multiple user-token bump accounts** alongside the **Waypoint feeder-management bot**. The original user-token bump transport is unchanged.

## Start

1. Install Python 3.12, extract this folder, and double-click **start-local.bat**.
2. The first run installs dependencies and creates `.env`. Fill in `DISCORD_BOT_TOKEN` for the management bot and `OWNER_USER_IDS` for your approved server owners. Start the batch file again.
3. Open **http://127.0.0.1:8000**. The launcher runs the bot, dashboard and scheduler together. Keep the computer awake and the launcher running. Closing a browser tab does not stop the app; Ctrl+C in the launcher does.

Alternatively, run `python start_local.py`. Use a fresh folder when upgrading rather than reusing a virtual environment with old dependencies.

The scheduler starts **disabled**, with **simulation on**. Configure and inspect it before enabling live requests. The management bot's **Development mode** is a separate setting controlling feeder DMs.

## Set up Parley and your other main servers

1. Invite the management bot to **every main server and feeder** you want it to manage. Enable its Server Members Intent in the Discord developer portal. See WAYPOINT_GUIDE.md for permission details.
2. On **Servers**, mark Parley as **Main (default)**. Mark your other communities as **Main (additional)**.
3. Mark your feeder servers as **Feeder**, then open **Configure** for each one.
4. Under **Where should this feeder send people?**, choose the default or select **Use the main servers checked below**, check one or more destinations, and save.

For example:

| Feeder | Main destinations |
| --- | --- |
| Feeder A | Parley |
| Feeder B | Community Two |
| Feeder C | Parley + Community Two + Community Three |

A feeder can have **1–25 selected main destinations**. Each feeder/destination pair has its own tracking invite, preserving source attribution. Individually selected destinations stay fixed when you change the default main server.

### Welcome channel, rebuild, and DMs

- Normal setup creates or repairs the public welcome/funnel channel and private bump channel, keeping unrelated channels.
- **Fresh Setup** deletes every channel/category in that feeder and rebuilds its layout. It requires typing `FRESH`; it never runs on default or additional main servers. The worker checks permissions and every destination before deleting anything.
- The welcome post has one button for each destination with a working invite. Customize the text, embed, emoji and button appearance in **Message studio**. The preview shows the separate buttons.
- A joining member receives **one DM**, containing buttons for selected communities they have not already joined. If they belong to all destinations, no DM is sent. Existing deduplication, development-mode and closed-DM rules still apply.
- Interactive buttons survive bot restarts, read the current invite at click time, and reject destinations removed from that feeder. Direct link buttons use the invite embedded when posted; old direct links in previously sent DMs cannot be rewritten.
- The tracking-invite panel and Health page show each selected destination separately. **Rotate all invites** rotates all currently selected destinations for that feeder.

Changing destinations updates the welcome post through the bot's task queue. It controls future DMs; it does not resend previously delivered DMs or rewrite past conversions. If a destination is unavailable, working destinations can still be offered and the missing destination appears in Health. Fix it there and use Repair.

## Multiple bump accounts and both timers

Open **Scheduler → Add account**. **User token · bump account** is the default. Add your account's credential, assign its servers and configure its timings. Repeat for your other accounts.

| Control | Default | What it changes |
| --- | --- | --- |
| **Delay before next server** | **30 minutes** | Minimum gap between actions by this account, regardless of which server comes next |
| **Same-server cooldown** | **120 minutes / 2 hours** | Time reserved before that server can be bumped again |
| Cooldown override on a server target | Inherit account | A different same-server cooldown for that particular target |
| Random extra delay | 0 minutes | Optional extra time added to a server cooldown |
| Gap between account start times | 5 minutes | Staggers Start all / automatic startup; set 0 to start accounts together |

**Both 30 minutes and 120 minutes are editable independently.** Each account can use its own values. **Scheduler settings** also lets you choose defaults for new accounts. Service-provided rate limits still apply.

Multiple accounts can run simultaneously and can list the same server. A **shared server clock in this app instance** prevents overlapping accounts from bumping that server at once. The account that claims the run reserves its configured server cooldown. Duplicate entries for the same server within one account are rejected. Do not run a second independent scheduler on another computer against the same servers: local reservations are not synchronized across machines.

Use **Start / Pause / Resume / Stop** per account, or **Start all / Stop all**. Stop an account before editing its configuration. Changing feeder settings does not require stopping all bump accounts. A request already in flight may finish after Stop.

### Bumping while feeder setup is running

When adding a target, select a feeder from the bot's server picker. This enables **Follow the feeder's managed bump channel**.

- A target can be assigned before its bump channel exists.
- The target waits while a Fresh Setup or initial setup job is pending/running.
- After setup, it follows the newly created bump channel automatically.
- Other ready targets and accounts continue running during that wait.
- If the management database cannot provide a known managed feeder, the target waits. For a manually managed channel, turn channel tracking off and supply its channel ID.

The catalog refreshes every two seconds. A request already sent before a rebuild was queued cannot be unsent.

### Simulation and live results

Enable the scheduler with simulation checked and start your accounts to inspect routing and timing without sending Discord requests. Simulation has a separate counter and shared clock. Stop accounts, supply credentials, turn simulation off and save, then start them when ready. Leaving simulation clears simulated waits; existing live cooldowns are preserved.

The original user-account adapter is retained unchanged. Its accepted sends are displayed as **sent/unconfirmed**, because its response lookup cannot reliably prove that the current DISBOARD request succeeded. This is not a promise that every user token, DISBOARD interaction or legacy fallback will work on the live service.

The optional **Official bot** account type sends a configured message and verifies its message ID. It does **not** invoke DISBOARD's slash command. Choose **User token · bump account** for the original behavior you requested.

## Customization

| Page | Settings |
| --- | --- |
| Customize | Dashboard name (for example Parley Control), accent, light/dark theme, spacing |
| Settings | Default main server, network name, channel defaults, DM timing, deduplication, staff roles, repairs, custom message variables |
| Feeder Configure | One/multiple main destinations, channel names, DM mode, delay, role rules, tags and overrides |
| Message studio | Welcome/DM text, embeds, buttons, variables, previews and publishing |
| Scheduler | Accounts, user tokens, server/channel assignments, both cooldowns, per-server overrides, startup and controls |
| Scheduler settings | Simulation, default timings, account stagger, retries, failure threshold and refresh frequency |

Message variables are rendered for the selected destinations. In the body, `{main_server_name}` lists those names; on each button it resolves to that individual destination. Long text is shortened to fit Discord's limits, with a notice in the preview. Discord still controls supported button styles.

## Upgrading and existing data

Stop the old programs first and keep a backup.

- **Management bot:** copy your `.env`. For local SQLite, copy the existing `funnel.db` only after stopping the old processes. For PostgreSQL, retain the same `DATABASE_URL`. Existing feeder routes continue to inherit the default main server until you select explicit destinations. New columns are added automatically; historical records stay intact.
- **Original Bump Scheduler Pro:** use **Scheduler → Import and export accounts** to import its `data/accounts.json`. Names, user-token account types, server assignments and both timing settings are retained. Imported accounts start disabled with automatic startup off. Historical counters/timers are not imported through this configuration importer.
- **Previous merged suite:** copy the stopped suite's entire `data` folder, plus its database and `.env`. This preserves settings, counters and timers. Saved reservations are upgraded to shared server clocks. Existing targets keep manual channel behavior until you select them from the server picker or enable managed-channel tracking.
- Existing account IDs are kept, so credentials in the `bump-scheduler` OS keychain can be reused on the same computer. Otherwise re-enter them. Credentials are never included in JSON exports. The dashboard states whether a credential is in the OS keychain or only in the current process's memory.

**Export accounts** exports account/target configuration only. A full stopped-app backup includes `.env`, the database and the whole `data` folder. OS keychain contents are separate.

## Runtime details

The management bot can still run on Railway with PostgreSQL. The scheduler and unified dashboard run on your computer; deploying only the Railway bot does not deploy this scheduler. If the bot is already on Railway, run **only** `python dashboard.py` locally against that database instead of starting a second management bot.

For dashboard-only installation or testing:

```text
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python dashboard.py
```

On macOS/Linux, replace `.venv\Scripts\python` with `.venv/bin/python`.

Scheduler data is in `data`, or the optional `SCHEDULER_DATA_DIR`. The dashboard is local-only and rejects remote hosts and cross-site writes. Use one dashboard process with one worker; do not enable reload. A process lock prevents two instances from sharing the same scheduler folder.

The original desktop source remains for legacy use but does not provide the new managed-channel bridge or multi-main controls. Use the browser dashboard for this combined app.

## Validation and remaining limits

Run `python -m pytest -q`. See **TEST_REPORT.md** for the actual checks and limitations. Validation uses mock Discord transports and temporary data. No real user tokens or live Discord messages were used, so real Discord/DISBOARD operation still requires your live test.

The two original guides are included for reference; this README takes precedence for the merged app's behavior.
