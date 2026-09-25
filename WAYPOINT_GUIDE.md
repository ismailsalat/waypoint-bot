# Funnel Bot

Runs a network of small Discord "feeder" servers and funnels their members into
one main community server.

Someone finds a feeder on DISBOARD and joins it. A few seconds later the bot
checks whether they are already in the main server, and if not sends them one
DM with a join button. The button uses an invite that belongs to that feeder
alone, so when they join the main server the credit goes to the right place.

Everything else — which server is the main one, channel names, message text,
DM delay, button label, tags, age rules — is edited on a dashboard that runs on
your own computer. Nothing about your servers is hardcoded.

---

## How it fits together

```
Railway, always on                     Your laptop, when you need it
┌──────────────────────┐               ┌──────────────────────┐
│  python -m bot.main  │               │  python dashboard.py │
│  discord.py gateway  │               │  FastAPI :8000       │
└──────────┬───────────┘               └──────────┬───────────┘
           │            PostgreSQL                │
           └────────────► (Railway) ◄─────────────┘
```

The dashboard has no Discord connection. When you press *Repair* or *Send test
to me*, it writes a row to a `tasks` table; the bot polls that table every five
seconds and does the work. That is why the dashboard can live on `127.0.0.1`
with no domain, no port forwarding and no public URL.

---

## Project layout

```
funnel-bot/
  bot/
    main.py            start-up, intents, keeping the database in sync with reality
    feeder_setup.py    creates channels, tracking invites, the public funnel post
    member_events.py   joins the bot handles: new guilds, new members
    funnel_dm.py       the DM itself, and every reason one might not be sent
    invite_tracker.py  invite use counts and conversion attribution
    age_rules.py       "if someone gets this role, do this"
    maintenance.py     auto-repair loop and the dashboard task worker
    messages.py        turns a saved message into a Discord embed and button
  core/
    config.py          reads .env
    constants.py       shared names for statuses and types
    settings.py        network defaults and per-feeder overrides
    rendering.py       {variable} substitution, used by the bot and the preview
  database/
    models.py          the schema
    database.py        engine and sessions
    crud.py            shared queries
    analytics.py       the numbers behind the Analytics page
  dashboard/
    app.py             every page and action
    templates/  static/
  tests/
  dashboard.py         python dashboard.py -> http://127.0.0.1:8000
```

---

## Database tables

| Table | What it holds |
| --- | --- |
| `settings` | Network defaults, including which guild is the main server |
| `servers` | Every guild the bot has been in, its type and its overrides |
| `tracking_invites` | One active invite per feeder, pointing at the main server |
| `member_joins` | Who joined which server and when, and whether it was a test |
| `dm_events` | Every DM attempt or skip, with the reason and the tags live at the time |
| `pending_invite_uses` | Invite uses seen but not yet matched to a member |
| `message_versions` | Drafts and published versions, global or per feeder |
| `tag_experiments` | The DISBOARD tags a feeder used during a window of time |
| `conversions` | Main-server joins, with source, invite, tags, message version and whether it was a test |
| `role_rules` | Role-triggered actions |
| `audit_logs` | Plain-language record of what changed |
| `tasks` | Jobs the dashboard leaves for the bot |

---

## 1. Create the Discord application

1. Go to <https://discord.com/developers/applications> and create an application.
2. Open **Bot**, press **Reset Token**, copy it. This is `DISCORD_BOT_TOKEN`.
3. On the same page turn on **Server Members Intent**. The bot cannot see joins
   without it. Message Content is not needed.
4. Open **OAuth2 → URL Generator**, tick **bot**, then tick:
   Manage Server, Manage Channels, Manage Roles, Kick Members, Create Invite,
   View Channels, Send Messages, Embed Links, Read Message History.

   Not every permission is needed everywhere. In a **feeder** the bot needs
   Manage Channels, Manage Roles, View Channels, Send Messages, Embed Links and
   Read Message History — it does not need Create Invite there, because the
   tracking invite is created in the main server. In the **main** server it
   needs Create Invite, plus Manage Server to read invite counts, which is how
   attribution works. Kick Members is only needed where you switch on age
   enforcement.
5. Use the generated link to invite the bot to your main server and to each
   feeder.

You also need your own Discord user ID: turn on Developer Mode in Discord
(Settings → Advanced), right-click your name, Copy User ID.

---

## 2. Run it locally first

```bash
git clone <your repo>
cd funnel-bot
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`. Two values are all you need:

```
DISCORD_BOT_TOKEN=...
OWNER_USER_IDS=your-user-id
```

`DATABASE_URL` is optional on your own machine. Leave it out and the app
creates `funnel.db` in the project folder on first start and uses it from then
on. There is no PostgreSQL to install, no database to create, no tables to set
up and no separate setup command; the schema is created and upgraded
automatically every time it starts.

Everything else — which server is main, development mode, the test user, all
the feeder defaults — is configured on the dashboard and stored in the
database. `.env.example` lists three optional bootstrap values you can set if
you prefer to start a fresh deploy with them, but they are read only while the
matching setting has never been saved; see *What lives where* below.

One command:

```bash
python start_local.py
```

That really is the whole thing. On the first run it creates `.venv`, installs
the requirements and re-launches itself inside that environment; after that it
uses it directly. If you have no `.env` it copies `.env.example` across and
tells you which two values to fill in, then stops.

Once it is running it starts the bot and the dashboard together, streams both
logs into one window with `[bot]` and `[dashboard]` prefixes, waits for the
dashboard to answer, opens it in your browser and stops both cleanly on Ctrl+C.
It reads `DASHBOARD_HOST` and `DASHBOARD_PORT` from your `.env`, so if you move
the dashboard to another port the launcher checks, waits for and opens that
port. It reports which database this run will use — SQLite or PostgreSQL, with
a loud warning if you have pointed it at production — without ever printing the
URL, and it refuses to start a second bot or to use an occupied port.

On Windows you can double-click **start-local.bat**, which does the same thing
through the batch file.

Or run the two processes yourself:

```bash
python -m bot.main        # the bot
python dashboard.py       # the dashboard, then open http://127.0.0.1:8000
```

Both print which database they are using, for example
`Local environment, SQLite database (funnel.db)`. They resolve it the same way,
so they always land on the same file.

The first time you open the dashboard it shows a six-step setup screen: choose
the main server, name the network, name the two channels, set the DM delay,
finish. After that it goes straight to the overview.

---

## 3. Deploy the bot to Railway

1. Push the project to GitHub. `.env` is in `.gitignore`, so your token stays
   on your machine.
2. On <https://railway.app>, **New Project → Deploy from GitHub repo**, pick
   the repository.
3. In the same project, **New → Database → PostgreSQL**. Railway creates a
   `DATABASE_URL` variable.
4. Open the bot service → **Variables** and add:

   ```
   DISCORD_BOT_TOKEN   your token
   OWNER_USER_IDS      your user id
   DATABASE_URL        ${{Postgres.DATABASE_URL}}
   ```

   Optionally add `DEVELOPMENT_MODE=true` for a cautious first deploy. It only
   sets the initial value of the dashboard toggle; after that the dashboard
   controls it and the variable is ignored.

   The `${{Postgres.DATABASE_URL}}` reference links the two services; Railway
   offers it in the variable editor. The code accepts the `postgres://` form
   Railway gives you and converts it for the async driver.
5. Deploy. The `Procfile` runs `python -m bot.main` as a worker, and
   `railway.json` restarts it if it ever stops. Tables and any newly added
   columns are created on start, so there is no migration step to run by hand.
6. Check the deploy logs for `Signed in as ...`.

### Local SQLite is not your Railway database

They are separate on purpose and are never synced, merged, uploaded or
downloaded in either direction. Test feeders and fake joins you make on your
laptop stay on your laptop; production data stays on Railway.

The one way to cross that line is deliberate: paste the Railway
`DATABASE_URL` into your local `.env` and `python dashboard.py` will connect to
production. The dashboard then shows a red **PRODUCTION DATABASE** banner on
every page so you cannot mistake it for local. Remove the line to go back to
SQLite. Nothing ever discovers or connects to Railway on its own.

### How the database is chosen

| Where | `DATABASE_URL` | Result |
| --- | --- | --- |
| Local | not set | SQLite `./funnel.db`, created automatically |
| Local | `sqlite...` | that SQLite file |
| Local | `postgres...` | that PostgreSQL server, with the production banner |
| Railway | `postgres...` | that PostgreSQL server |
| Railway | not set | start-up stops with a clear error |

The last row is deliberate. Railway's filesystem is replaced on every deploy,
so a SQLite fallback there would look like it was working while losing all your
data each time you shipped. Both `postgres://` and `postgresql://` forms are
accepted and converted for the async driver; the converted URL is never logged
or displayed.

---

## 4. Adding feeder number twenty

1. Create the Discord server.
2. Invite the bot with the OAuth link.
3. Done.

Because your user ID is in `OWNER_USER_IDS`, the bot recognises that you own
the new server, marks it as a feeder, creates the public funnel channel and the
private bump channel, creates a fresh tracking invite to the main server, and
posts the funnel message with the button.

If someone else invites the bot to a server you do not own, it is recorded as
`DISABLED` and nothing is touched. You can promote it by hand on the Servers
page, which queues the same setup.

---

## 5. Testing with a throwaway server

Set `DEVELOPMENT_MODE=true` first. Real members then get no production DMs;
only `ADMIN_TEST_USER_ID` does. The dashboard shows an orange banner so you
cannot forget.

1. Create a test server, invite the bot, watch it create the channels.
2. Open **Servers** and confirm the test server shows up as `FEEDER`.
3. Open the feeder page and check the tracking invite exists.
4. Join the test server with a second Discord account. Since that account is
   not your test user, development mode suppresses the DM — the skip is
   recorded on the Health page. To see a real DM, either set
   `DEVELOPMENT_MODE=false`, or use **Message studio → Send test to me**.
5. Use the tracking invite from the second account to join the main server.
   The Conversions page shows one attributed conversion with the tags that were
   live at the time.
6. Delete the funnel channel in the test server, then press **Repair**. It
   comes back within a few seconds, along with a line in the audit log.

For the DM path end to end, turn development mode off and use a second account
that is not yet in the main server. Anyone already in the main server is
skipped by design.

---

## The dashboard

**Overview** — network totals and a per-feeder table.

**Servers** — every guild the bot has seen. Change a server between `MAIN`,
`FEEDER` and `DISABLED` from the dropdown.

**Feeders** — one page per feeder: channel names, DM delay, funnel mode, auto
repair, age enforcement. Each setting can say *use the global default* or hold
its own value. Also where you set the DISBOARD tags, rotate the tracking
invite and add role rules.

**Message studio** — edit the DM or the public post, watch the Discord-style
preview update as you type, send a test to yourself, save a draft, publish it.
Publishing is the only thing that changes what members receive. Old versions
stay in the list and can be copied back into a new draft. A feeder can override
the global message, and the override wins for that feeder only.

Variables: `{user_name}` `{user_display_name}` `{feeder_name}`
`{main_server_name}` `{network_name}` `{invite_url}`. Anything in braces that
is not on this list is flagged in the preview and sent as written.

**Tag experiments** — every tag change closes the old experiment and opens a
new one. Nothing is overwritten, so a conversion from last week keeps last
week's tags.

**Conversions** — every main-server join with its source feeder, invite code,
tags at the time, message version and attribution status. Joins recorded while
development mode was on are listed with a `test` label and are left out of
every figure on the Analytics page.

**Analytics** — by feeder, by tag, by message version, plus how many members
two feeders share. Production activity only: no join, DM or conversion recorded while
development mode was on, and no test send from the message studio, appears in
any of these numbers. They are observed counts. A tag that appears alongside a
higher rate did not necessarily cause it, and the page says so.

**Health** — worker status at the top (ONLINE with the age of the last
heartbeat, or OFFLINE), then every feeder with each managed resource marked OK,
MISSING, BROKEN or OUTDATED, with Repair and Fresh Setup buttons. Below that
the job queue, showing each job's status as PENDING, RUNNING, DONE or FAILED
with its result or error, a Retry button on failures, and a Clear for finished
history. If the worker is offline, pending jobs say so instead of leaving you
guessing.

**Settings** — three sections. *Network settings* holds the network name, the
main server dropdown, the development mode switch and the test DM user ID.
*Feeder defaults* holds the channel names, DM delay, funnel mode, auto repair,
age enforcement and the deduplication rules that every feeder inherits.
*System* shows only whether your token, database and approved owners are
configured; no secret is ever displayed. Saving tells you exactly what changed,
and nothing here needs a bot restart.

---

## What lives where

`.env` holds secrets and the values that decide trust. Nothing here can be
changed from the dashboard:

| Variable | Why it stays |
| --- | --- |
| `DISCORD_BOT_TOKEN` | Secret |
| `DATABASE_URL` | Contains credentials |
| `OWNER_USER_IDS` | Decides whose servers the bot will configure by itself |

Everything else is a dashboard setting stored in the database, and takes effect
without restarting the bot: the main server, development mode, the test DM
user, the network name, channel names, DM delay, funnel mode, deduplication
policy and cooldowns, auto repair, age enforcement, and the repair interval.

Three of those can also be seeded from `.env` on a completely fresh database —
`MAIN_GUILD_ID`, `DEVELOPMENT_MODE` and `ADMIN_TEST_USER_ID`. They are read
only while the matching setting has never been saved. Once it exists, the
database wins and the variable is ignored, so a stale `MAIN_GUILD_ID` can never
override a choice you made on the dashboard.

### Starting with nothing configured

The bot runs fine with no main server. It connects, records the servers it is
in, and logs a note. Feeders still get their public and bump channels, because
those are safe to create, but no tracking invite is made and the Health page
says `main server not configured`. Funnel DMs are skipped rather than sent to a
dead link. Pick a server in the setup wizard or on the Settings page and the
next repair pass finishes the job.

## The job queue

The dashboard has no Discord connection, so anything needing Discord is written
to the `tasks` table and picked up by the bot within a few seconds. Jobs move
PENDING → RUNNING → DONE or FAILED, recording when they started, when they
finished, and what happened.

The worker writes a heartbeat on every pass. If the bot is not running, the
Health page says so rather than leaving a job sitting at PENDING with no
explanation. If the bot stops in the middle of a job, the job is picked back up
on the next start; after three abandoned attempts it is failed rather than
retried forever.

## Buttons

Discord decides what a button can look like, and the two options are mutually
exclusive:

- **Direct link button** — carries the tracking invite itself. One click, no
  round trip. Discord fixes its colour; there is no way to change it, and this
  project does not pretend otherwise.
- **Interactive coloured button** — Primary, Secondary, Success or Danger. It
  calls back into the bot, which replies privately with that feeder's current
  tracking invite.

Attribution is identical either way, because both hand over the same invite.
The interactive button reads the invite at the moment it is clicked, so a
message posted weeks ago keeps working after a rotation. Embed colour is
separate and does take a hex value.

## Message versions

Drafts can be deleted, duplicated, renamed and published. A published version
that real messages were sent with can be **archived** but never deleted: DM
events and conversions reference it, and the message performance table reads it
back. Archiving hides it from the working list while every historical record
keeps resolving. A version nothing has ever referenced can be deleted outright.

## Invite manager

Each feeder page has a Tracking Invite panel showing status, the invite,
destination, when it was created and how many times it has been used, with
buttons to copy, rotate, repair or repost the funnel message.

Rotating creates a new invite, retires the old one, updates the public post and
points future DMs at the replacement. Conversions already credited through the
old code keep their history — old attribution is never rewritten. Repair does
the same automatically if it finds the invite has been deleted in Discord.

Tracking invites never expire and have unlimited uses, because anything else
quietly breaks long-term attribution. Discord does not allow editing an invite
code, so changing one means rotating it.

## How attribution works

The bot keeps a cached count of how many times each invite to the main server
has been used. When someone joins it fetches the counts again and compares.

- Exactly one invite moved and it is a tracking invite → that feeder gets the
  conversion.
- Exactly one invite moved and it is not one of ours → `UNKNOWN`.
- Two or more invites moved → `AMBIGUOUS`, even when only one of them is a
  tracking invite. If your invite and an ordinary one both went up, the person
  may well have used the ordinary one, so no feeder is credited.
- Nothing moved → `UNKNOWN`.

It never falls back to checking whether the person is still in a feeder,
because people often leave the feeder before joining the main server.

### Two people through the same invite

If one tracking invite jumps by more than one between snapshots, the extra
uses are banked in `pending_invite_uses`. The next joins that arrive with no
visible invite change take one from the bank and are credited properly. The
bank lives in the database, so a restart does not lose it. If uses from two
different invites are waiting at once, nothing is consumed and those joins are
recorded as ambiguous rather than guessed at.

### What counts as a test

Anything recorded while `DEVELOPMENT_MODE=true` is flagged at the moment it
happens: the feeder join, the DM, and the conversion. Flagged rows are stored
in full and shown on the Conversions page with a `test` label, but they are
left out of every figure on the Analytics page — join totals, DM totals,
conversion totals, rates, tag counts, message-version counts and feeder
overlap.

A conversion takes its status from the funnel behind it rather than from the
setting at the moment someone joins, because the two can be days apart. The
order is: the DM that reached them, then their most recent feeder join, and
only with no history at all does the current setting decide. So a test run that
finishes after you switch development mode off stays a test, and a real member
who joins during a later testing session still counts.

### Which tags a conversion belongs to

The tags recorded against a conversion are the ones that were live when the
person was reached, not the ones live when they finally joined. Every funnel DM
stores the tag experiment that was active when it went out, and the conversion
reads it back from there. If there was no DM, the experiment covering that
person's most recent feeder join is used. Only when there is no history at all
does it fall back to the current experiment. That means you can change a
feeder's tags at any time without rewriting what earlier conversions mean.

## DM rules

One DM per person, controlled by the policy on the Settings page: once per
feeder, once anywhere in the network, or not again for a number of days. A DM
that fails because someone's DMs are closed is recorded once and not retried
for the number of days you choose.

Three modes, globally or per feeder:

- **LIVE** — DMs are sent.
- **DRY RUN** — nothing is sent, but the event is recorded with the message
  that would have gone out.
- **OFF** — the funnel DM is disabled for that feeder.

## Changing the main server

Pick a different server on the Settings page, or choose `MAIN` in the dropdown
on the Servers page. Exactly one server is MAIN at a time: promoting a new one
demotes the old one automatically. The old main goes back to whatever it was
before it was promoted, or to `DISABLED` if it was never anything else.

Past conversions, DM events and tag experiments are left exactly as they are,
still pointing at the server people actually joined. Each feeder is queued for
a check and gets a new tracking invite to the new main server. Nothing is
deleted.

A feeder's **Destination** is always the current main server. It is shown on
the feeder page and the Servers page marked *inherited*, because the backend
sets it from the main-server setting; it is not a per-feeder field you can
point somewhere else.

## Age rules

Self-attestation only; no birth dates are collected anywhere. Every server the
bot is in has a Configure page — feeders at **Feeders**, the main server
through **Configure** on the Servers page — with an age enforcement switch and
its own role rules: *if someone receives this role, then kick / add a role /
remove a role / just log it*.

The switch is per server and defaults to off, so the usual setup is age
enforcement ON for the main server with an `Under 18 → Kick` rule, and OFF
everywhere else. You never have to turn it on globally.

---

## Tests

```bash
python -m pytest
```

204 tests covering feeder registration and repair, message rendering and
versioning, DM duplicate protection, bots being ignored, the already-in-main
skip, invite comparison, attribution including the unknown and ambiguous
cases, banked invite uses surviving a restart, conversions keeping the tags
that were live when the DM was sent, tag experiment switching, role actions on
the main server, single-MAIN switching, development mode staying out of
production numbers including its joins and conversions even when the setting
is flipped mid-funnel, the dashboard pages, settings resolving database-first with the
environment only as a first-run bootstrap, and the database selection rules
including the Railway guard and restart persistence, the task
worker's claim/fail/recover cycle driven through the real loop, the slash
commands and their safeguards, button modes, version archiving, invite
rotation, the launcher's env handling and bootstrap decisions, and the split
feeder/main-server preflight. They run against in-memory
SQLite
and mock Discord; the bot itself always uses real discord.py calls.

## Installing the dependencies

```bash
pip install -r requirements.txt
```

Every version in `requirements.txt` is pinned to one that has been installed
into an empty virtual environment and run against the full test suite. Note
that `pytest-asyncio` 1.4.0 is the current release; there is no 2.x.

## Troubleshooting

**No DMs arrive.** Check development mode is off, the feeder's funnel mode is
`LIVE`, and that the test account is not already in the main server. The
Health page lists every skip with its reason.

**Attribution keeps coming back unknown.** The bot needs Manage Server in the
main server to read invite counts. Without it, joins are still recorded, just
unattributed.

**The bot did not configure a new server.** Your user ID must be in
`OWNER_USER_IDS`, and it must be the ID of the account that *owns* the server.
Restart the bot after changing the variable.

**Channels are not created.** The bot needs Manage Channels in the feeder. The
error appears on the Health page.

**"Railway environment detected but PostgreSQL DATABASE_URL is not
configured."** Add a PostgreSQL service to the Railway project and set
`DATABASE_URL=${{Postgres.DATABASE_URL}}` on the bot service. The bot refuses
to start rather than create a throwaway file database.

**Where is my local data?** In `funnel.db` in the project folder. Nothing
deletes or resets it; back it up by copying the file.

**Will repair undo my channel permissions?** No. On an existing channel the bot
only writes the overwrites it owns — `@everyone`, itself, and the staff roles
named in Settings. Any other role overwrite you add by hand is left alone every
time auto repair runs.
