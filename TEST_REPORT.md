# Validation report · multiple accounts and destinations

## Build checks

The final combined pytest run passed **338 tests in 56.80 seconds**, with one inherited Python `audioop` deprecation warning. Python compilation and JavaScript syntax checks also passed. The environment uses Python 3.12 with the merged project's pinned Python requirements.

A byte-for-byte comparison confirmed that `app/adapters/user_token.py` is unchanged from the uploaded Bump Scheduler Pro archive. Multiple user-token accounts remain supported, with configurable account and server cooldowns.

## Scenario coverage

The new and existing tests exercise:

- Multiple account queues running simultaneously.
- The default 30-minute account gap and 120-minute server cooldown, both independently editable.
- Per-server cooldown overrides and configurable defaults for new accounts.
- Two accounts targeting the same server, with only one allowed to claim its cooldown.
- Persistent timers, process locking, safe shutdown, simulation and transitions back to live settings.
- Feeder configuration changes while account workers are running.
- Waiting during feeder rebuilds, following a recreated bump channel, and continuing other ready targets.
- Assigning a managed target before its channel exists.
- One feeder routing to several main destinations, and separate feeders routing to different main servers.
- Separate tracking invites and custom button labels for each destination.
- One DM containing multiple destinations, leaving out joined communities, and suppressing duplicate DMs.
- Explicit routes with no global default, and pinned routes surviving a default-main change.
- Persistent interactive buttons, invite rotation, and rejecting removed destinations.
- Preflight checks across all destinations before destructive setup, plus protection for additional main servers.
- Correct attribution when a member joins an additional main server.
- Message previews with multiple buttons and text length limits.
- Original feeder, message, database, scheduler, retry and dashboard regression coverage.

The tests use temporary databases and mocked Discord calls. No real tokens or live Discord requests were used.

## Dashboard JavaScript and startup

A JSDOM harness loaded the actual served HTML and scripts against a running local Uvicorn API. It passed:

- Three available main destinations and selecting two for a feeder.
- A message preview with two separately named destination buttons.
- Creating two accounts with User token selected by default.
- Reading the original 30/120 defaults and changing them to 7.5/95 minutes.
- Setting a 65-minute per-server override.
- Selecting the managed bump channel from the bot's server list.
- Start all, simulation, shared-server cooldown and Stop all.

No JavaScript errors were reported. The dashboard's real startup upgraded a QA SQLite database created by the prior build and retained its existing server rows.

This is a DOM/control-flow check, not a visual browser test. Dialog presentation was supplied by the harness because JSDOM does not implement native dialog rendering.

The launcher now initializes the shared database before starting its two child processes, avoiding concurrent first-run schema/default-message creation.

## Still requires a live test

- Real Discord authentication, DISBOARD requests and live message/DM delivery.
- Real server permissions, privileged intents, and credentials supplied by the user.
- Railway production deployment and production PostgreSQL connectivity.
- Windows launch behavior and the actual Windows/macOS OS keychain.
- Visual desktop/mobile rendering; the browser engine was unavailable in this environment.

The original user transport's responses remain labeled sent/unconfirmed. Automated test success does not establish that a live DISBOARD bump succeeded. Server-imposed cooldowns still apply regardless of locally selected timer values.
