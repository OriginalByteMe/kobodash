# AGENTS.md

Instructions for an AI coding agent helping a person set up kobodash. Read `README.md` first for the full picture.

kobodash turns a Kobo e-reader into a wireless e-ink dashboard. `make setup` installs a small client on the Kobo over USB. The Kobo then wakes every few minutes, asks a server for a picture, draws it and sleeps. `make serve` runs the server, which renders `dashboards/<name>/dashboard.html` (Jinja2, filled from `dashboard.json` sources) to a PNG with headless Chromium. Kobo only. Kindle is not supported.

## Hard rules

- Never write anything to the Kobo unless the person has said an explicit yes in this conversation.
- Never type the `yes` at the setup prompt or the unknown-model override phrase for the person. They read the warning and type it themselves.
- Never delete files on the Kobo except through `make uninstall`.
- Never edit `.kobo/KoboReader.sqlite` or any Kobo setting, and never touch their books.
- Never disable, skip or edit the pinned checksums in `cli/pins.json` or the sha256 checks in `cli/kobodash.py` and `server/kobodash.py`.
- Never commit `.env`. It holds the token and the update key. Check with `git check-ignore .env`.
- Put secrets only in `.env`. Never in `dashboard.json`, `dashboard.html`, `sample.json` or the README.
- Do not replace an existing `.env` or turn on SSH without asking. If the person says yes to SSH, the key must be theirs.

## Setting up

1. Check the computer has Python 3.9 or newer, `make`, and either Docker or Chromium. Linux and macOS only.
2. Ask the person to plug the Kobo into the computer with its USB cable and tap **Connect** on the Kobo's screen. If the Kobo is already showing a dashboard, they press its power button once first and wait for its normal screen.
3. Show what setup will do before anything is written. Run:

   ```
   python3 cli/kobodash.py setup --dry-run
   ```

   It writes nothing to the Kobo or to `.env`. It does download about 30 MB into `.cache/`. Add `--mount /path/to/Kobo` if the drive is not listed. Tell the person what it printed in plain words.
4. Ask the person to run `make setup` in their own terminal and pick their Kobo from the list. You may explain each question. They read the warning and type `yes` themselves. Do not pipe answers into a real run.
5. Setup writes `.env`. It asks where the server will run (the computer's LAN address, like `192.168.1.50:8737`), a name for the Kobo, latitude and longitude, a dashboard and whether to turn on SSH. SSH is optional and not needed.
6. After setup the person ejects the Kobo safely and unplugs it. It restarts by itself and installs in about a minute.
7. Start the server with `make serve` (Docker), or `make serve-local` on a computer with Chromium. Both read `.env`. The server uses port 8737; the Kobo must be able to reach it.
8. Wait for the Kobo's next refresh. The server log (`docker compose logs kobodash`, or the terminal for `make serve-local`) shows a `display id=...` line for every request. A `display refused` line means the token in `.env` does not match the Kobo's.

If the person wants a different model, or setup refuses it, report the reason it printed. Do not look for a way around the refusal.

## Configuring for the person

- Ask where they live. Put it in `.env` as `LAT` and `LON`, plus `TZ` (an IANA name like `Europe/London`). Weather and air quality need them.
- Ask which dashboard they want: `home`, `weather`, `server`, `cards`, `agenda`, `color` or `url`. Set `DASHBOARD=<folder>` in `.env`.
- Ask for their own APIs and tokens (`AGENDA_URL`, `CARDS_URL`, `DASHBOARD_URL`, `NTFY_URL`, `NTFY_TOPICS`, `NTFY_TOKEN`, `DOCKER_URL`, or any `${NAME}` a `dashboard.json` uses). Put the values in `.env`. After `.env` changes, run `make serve` again.
- To build a custom dashboard, copy the closest folder in `dashboards/` and edit three files: `dashboard.json` (sources), `dashboard.html` (layout) and `sample.json` (fake data for each source, in the live shape). Template names are `src`, `errors`, `now`, `next`, `device`, `screen`, `font_dir`. Read `errors` with `errors.get('name')`; `errors.name` stops the render when nothing failed. Black on white, heavy weights, big numbers, no thin grey text.
- Always run `make preview DASHBOARD=<folder>` and open `preview.png` and look at it before you tell the person it is done. Fix what is cut off, too small or too light.
- Run `make test` after changing any template or Python file.
- Do not change files in `device/client/` unless asked. If you do, change `device/client/version.txt` too, or Kobos will not update. Remember that the server only offers updates when `OTA_KEY` is set.

## Getting someone back to the normal Kobo

Give the least invasive option that fits.

1. Power button: unplug the cable (a Kobo on a charger never sleeps), wait until it is asleep, press the power button once. It restarts into the normal Kobo, once. If nothing happens in a minute, press again.
2. Three failed starts in a row make the Kobo stay normal by itself. NickelMenu's "Start dashboard" starts it again.
3. A file named `disabled` in `.adds/kobodash` on the Kobo keeps it normal. Tell the person how to make it. Do not make it yourself unless they ask.
4. To remove kobodash, the person runs `make uninstall`, types `yes`, ejects and unplugs. The Kobo restarts and removes the startup hook. When its normal screen is back they connect it and run `make uninstall` a second time to delete `.adds/kobodash`. You may explain the steps. Do not type their confirmation.

## Where things live

| Path | What is there |
| --- | --- |
| `cli/kobodash.py` | The `setup` and `uninstall` commands. Standard library only. |
| `cli/pins.json` | Pinned upstream commit and sha256 of every download. |
| `server/kobodash.py` | The server: sources, rendering, the `/api/display` protocol, `--preview`. |
| `dashboards/<name>/` | `dashboard.json`, `dashboard.html`, `sample.json` for each dashboard. `url` has only `dashboard.json`. |
| `device/client/` | What runs on the Kobo (`autostart.sh`, `trmnlloop.sh`, `scripts/update.sh`, `version.txt`). Also served as the signed update. |
| `device/rootfs/` | Files packed into the Kobo's update: boot hook, udev rule, flags that keep SSH, telnet and FTP off. |
| `tests/` | Unit tests for the CLI, the server and every dashboard. |
| `.env`, `.env.example` | Your settings (never committed) and the documented template. |
| `docker-compose.yml`, `Dockerfile` | `make serve`. |
| `.cache/` | Downloads, fonts, the built `KoboRoot.tgz`. Git-ignored. |
| `docs/images/` | The pictures in the README. |

## Verify your own changes

```
make test
make preview DASHBOARD=<folder>        # then look at preview.png
python3 cli/kobodash.py setup --dry-run --mount <a folder holding .kobo/version>
git check-ignore .env                  # prints .env when it is ignored
```

For changes to the Kobo shell scripts, also run `sh -n device/client/autostart.sh device/client/trmnlloop.sh`.
