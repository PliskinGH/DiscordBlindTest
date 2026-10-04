# DiscordBlindTest

Django backend and discord.py bot running a quiz (e.g. blind test) on Discord.

## Requirements

- Python 3.14 (`.python-version`)
- PostgreSQL
- Redis (optional)

## Setup

```sh
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
```

`.env` variables:

| Variable | Required | Format | Description |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Yes | | Discord application Bot token |
| `SECRET_KEY` | Yes | | Django secret key (required when `DEBUG` is off) |
| `DEBUG` | Yes | `True` or `False` | `False` in production |
| `ALLOWED_HOSTS` | Yes | | Space-separated hosts |
| `DATABASE_URL` | Yes | `postgres://user:password@host:port/database` | Database used by the bot
| `TEST_GUILD_ID` | No | | Guild IDs to sync slash commands to instantly, space-separated |
| `DISCORD_CLIENT_ID` | No | | Discord application ID, for the web admin login |
| `DISCORD_CLIENT_SECRET` | No | | Discord application client secret, for the web admin login |
| `DISCORD_REDIRECT_URI` | No | | OAuth2 redirect URI registered on the application |
| `REDIS_URL` | No | `redis://host:port/index` | Cache shared by the web process and the bot |

## Discord application

1. Create an application on https://discord.com/developers/applications, add a bot and copy its token into `DISCORD_TOKEN`.
2. Invite the bot with the `bot` and `applications.commands` scopes.
3. Enable the **Server Members Intent** on the bot (only needed for the web admin).
4. Add `<DISCORD_REDIRECT_URI>` to the application's OAuth2 redirects (only needed for the web admin).

## Commands

```sh
python manage.py runbot       # run the Discord bot
python manage.py runserver    # Web admin
python manage.py test         # test suite
```

## Slash commands

| Command | Description |
| --- | --- |
| `/ping` | Gateway latency and number of games in the database |
| `/quiz setup` | Set up a game: optional `channel` where to play the game and `role` to ping, plus name, quiz type and scoring mode (hosts only) |
| `/quiz publish` | Publish the game being prepared and announce it in its channel (hosts only) |
| `/quiz panel` | Reopen the private controls of the running game (hosts only) |
| `/quiz guess` | Submit your answer for the round in play (players) |
| `/quiz queue` | Queue the question of the next round, optionally played as another quiz type (hosts only) |
| `/quiz unqueue` | Drop a question queued for a round (hosts only) |
| `/quiz clear` | Drop every queued question of the game (hosts only) |
| `/quiz copy` | Queue the questions another game was played with (hosts only) |
| `/quiz next` | Open the next round: the queued question or a drawn one (hosts only) |
| `/quiz reveal` | Reveal the current round and publish the standings (hosts only) |
| `/quiz end` | End the game of this server, revealing the round left open before the final scores. A game still being prepared closes without a recap (hosts only) |
| `/admin host add` | Allow a user or a role to host (server administrators) |
| `/admin host remove` | Withdraw host rights (server administrators) |
| `/admin host list` | Show the hosts of this server (server administrators) |
| `/admin channel set` | Default channel for the games (server administrators) |
| `/admin channel clear` | Clear the default channel (server administrators) |
| `/admin channel show` | Show where games are played by default (server administrators) |
| `/admin ping set` | Default role to ping when a game is published or a round opens (server administrators) |
| `/admin ping clear` | Clear the default ping (server administrators) |
| `/admin ping show` | Show the role pinged by default (server administrators) |
| `/library answer add` | Register an answer in this server's library (hosts) |
| `/library variant add` | Accept another text for an answer, e.g. `Song (Remastered)` for `Song` (hosts) |
| `/library variant list` | Show the variants accepted for an answer (hosts) |
| `/library variant remove` | Stop accepting a variant (hosts) |
| `/library question add` | Create a question, with multiple choice options when `choices` is given and a listening `media` link. Separate the accepted variants of an answer with `\|` (hosts) |
| `/library question edit` | Change the fields of a question, with the options of `question add`: an option left out keeps its field, `-` drops it. The `answer` itself cannot be dropped (hosts) |

`/blindtest` is an alias of `/quiz`: every subcommand exists under both names, and takes the same options, except the two that name a quiz type.

| Command | Difference |
| --- | --- |
| `/blindtest setup` | No `quiz_type`: the game is always a blind test |
| `/blindtest queue` | No `quiz_type`: the question is always queued as a blind test |

## Usage

- Host rights are per server:
  - A server appears in the web admin after the first command used in it.
  - Its page lists hosts by name — `@Name` for a user, `@Role` for a role — and falls back to the mention when Discord no longer knows it.
  - Members with the Discord "Manage Server" permission can always host, and manage the host list with `/admin host ...`.

- The channel the game is played in can be configured:
  - `/admin channel set` records the default channel of the server.
  - `/quiz setup channel=...` plays that game in the named channel or thread.
  - The current interaction channel is the fallback in case none of these are set.

- Same for the role that would be pinged whenever the game is published and at the start of every round:
  - `/admin ping set` records the default role to ping on the server.
  - `/quiz setup role=...` defines a specific role for the game.
  - No ping if none of these are set.

- `/quiz setup` sets up a game without announcing it.
  - The host receives a private setup panel to add, drop or copy questions.
  - The panel runs the same operations as `/quiz queue`, `/quiz unqueue`, `/quiz clear` and `/quiz copy`.
  - `/quiz publish` announces the game in its channel and swaps the setup panel for the host panel.
  - No round can open before the game is published.
  - A game ended while it is still being prepared closes without a public recap.

- Hosts drive a published game from the private panel sent by `/quiz setup` or `/quiz panel`: next round, reveal, queue, end.
  - `/quiz panel` reopens it when Discord cleared it.
  - Every round is posted with an **Answer** button opening the answer form of the round in play.
  - The panel and the round buttons keep working after a bot restart: their state lives in the database.

- A game change that has to be announced, opened, answered or ended is recorded as a **broadcast**, posted by the bot holding the Discord connection:
  - The slash commands and their controls post the broadcast they just recorded.
  - A caller without a connection (see [Web admin](#web-admin) section) records it only, and the bot posts it on its next pass.
  - Every broadcast is made exactly once, whichever client claims it first.

- Questions and answers belong to a server, or to the global library when their guild is empty:
  - The global library is defined in the Django admin only.
  - Every Discord change stays tied to the server it is made from.
- An answer accepts alternative texts — variants:
  - Variants are listed with `|` when a question is created (`Song | Song (Remastered)`), and managed afterwards with `/library variant add`, `/library variant list` and `/library variant remove`.
  - A guess matching a variant of the expected answer is scored as correct.
- `/library question edit` changes the fields of a question of the server the command runs in:
  - It takes the options of `/library question add`; an option left out keeps its field.
  - A value of `-` drops the field, except for `answer`: a question needs one.

## Quiz types

A game has a quiz type (blind test by default) and each round inherits it, or overrides it: the type decides how a round is played.

| Type | Needs | Answer form |
| --- | --- | --- |
| Blind test | nothing beyond the expected answer | two text fields (answer and secondary answer) |
| Open question | a prompt | two text fields |
| Multiple choice | a prompt, at least two choices, and the expected answer among them | a select of the choices plus the secondary answer field |

- A round whose type cannot play its question is refused.
- A random draw skips questions the type cannot play.
- A blind test round with no prompt shows a default one.

## Embeds

Every public embed is titled with the game name — the one given at `/quiz setup`, or `<type> #<number>` — and states the quiz type and the scoring mode.

- A round embed shows the prompt only.
- The answer appears with the reveal, written as `<Answer> (<Secondary answer>)`.
- Scores are published in one shape, twice: `<game name> — Round N scores` and `<game name> — final scores`.
- Each score embed leads with the leader in its description and lists the players in a `Standings` field.
- All public messages go through `discordbot.embeds.post`:
  - It clips the content and the embeds to Discord's limits (2000 characters of content, 4096 of description, 25 fields, 6000 per message).
  - It adds an "and N more" note instead of dropping players silently.

## Deployment

Any host able to run PostgreSQL (or any compatible database, since Django ORM is agnostic), the environment variables above, and two long-running processes works.

- `Procfile` declares the following for the platforms (Heroku, Dokku, and the like) that read one:
  - `web` process: `gunicorn discordblindtest.wsgi:application`.
  - `worker` process: `python manage.py runbot`.
  - Release phase: `python manage.py migrate --no-input`.
- Elsewhere, start the commands yourself and keep `web` (if you want to use the web admin) and `worker` (the bot iself) running.
- `python manage.py collectstatic --no-input` fills `STATIC_ROOT` (`staticfiles/`), at build time (already handled by herokuish buildpacks).
- WhiteNoise serves those files from the `web` process.
- `SECRET_KEY` is required as soon as `DEBUG` is off, which is the default, so set it with `ALLOWED_HOSTS` (space-separated) before the first build.
- `DATABASE_URL` is read by `dj-database-url`, so a database add-on of the host is enough (e.g. on dokku: `dokku postgres:create` and `dokku postgres:link`).
- `REDIS_URL` is optional and read as is by Django's Redis cache backend: you can set it via your host (e.g. on dokku: `dokku redis:create`, then `dokku redis:link`), and it is what makes one cache shared by the `web` and `worker` processes.

## Web admin

The web process offers an alternative interface (mainly for hosts, but can also be used by players): log in with Discord and open a server to see its running game.

- The login uses the same Discord application as the bot, with the `identify` and `guilds` scopes.
- It needs `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` and the registered `DISCORD_REDIRECT_URI` (the last env var is not mandatory as it can be rebuilt, but preferrable).
- The dashboard lists only the servers the bot has a record of, with the possibility to add other servers or invite the bot to them.
- Each server has a **library** its hosts fill with questions, answers and accepted variants, and **settings** its administrators manage: the channel its games are played in, the role they ping, and who may host.
- Each server has a **control room** where its hosts run a game from the browser, and every member of the server answers from theirs:
  - Feature parity with the discord bot, with the host controls and also the guess from the players.
  - Every change is recorded as a **broadcast** and posted by the bot holding the Discord connection, so everything is posted on the Discord channel even if acted from the web.
  - The live parts of the control room and of the answer page follow the game with [htmx](https://htmx.org): the round, the number of answers and the standings update by themselves, so an answer given in Discord shows up without reloading.
  - Questions to queue and games to copy from are searched server-side. The search needs a cache shared by every process that serves the web admin: set `REDIS_URL` in production, since the in-memory cache of a single worker will not answer a search another worker is asked for.
