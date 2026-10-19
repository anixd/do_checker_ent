# DO Mirrors Checker (do_checker_ent)

A web application that checks whether websites ("mirrors") are reachable through
the SOAX proxy service from a chosen country,
over HTTP or SOCKS5. Results stream into the page as the run progresses,
and every check is also written to disk as a Markdown log with an optional screenshot.
The application runs locally in Docker.

## Prerequisites

* **Docker** and **Docker Compose v2**.
  Check with `docker compose version`.
  Installation instructions: [Get Docker](https://docs.docker.com/get-docker/).
* **Python 3.12 or newer** — only if you intend to run the unit tests on the host.
  The application itself needs nothing but Docker.

---

## Setup

1. **Clone the repository:**

   ```bash
   git clone https://github.com/anixd/do_checker_ent
   cd do_checker_ent
   ```

2. **Create the `.env` file:**

   ```bash
   cp .env.example .env
   ```

   Then edit it.
   The important entries:

| Variable | Required | Notes |
|---|---|---|
| `SOAX_PACKAGE_PASSWORD` | **yes** | The **Package password** from the SOAX dashboard, Settings tab. This is what authenticates the proxy. Without it the application starts and every page works, but it refuses to run checks. |
| `SOAX_PACKAGE_ID` | no | The package UUID from the same tab. Not used in the connection string; reserved for a future catalog API. |
| `APP_HOST`, `APP_PORT` | no | Defaults to `127.0.0.1:8888`. |
| `DEFAULT_THEME` | no | `light`, `dark` or `auto`. Defaults to `auto`. |
| `TZ` | no | e.g. `Europe/Kyiv`. Defaults to `UTC`, and every timestamp in the logs follows it. |
| `MAX_CONCURRENCY`, `MAX_SCREENSHOT_WORKERS`, timeouts | no | Tune to your workstation's RAM. The defaults are conservative on purpose — see [Concurrency](#concurrency). |

> **Do not put the package password in `app.yaml`.** That file is editable
> through the Settings page and rendered into the browser, so anything in it
> ends up in screenshots and shared sessions.
> Secrets belong in `.env` only.

> **`MAX_CONCURRENCY`, `MAX_SCREENSHOT_WORKERS`,
> and timeouts:** You can adjust performance parameters, although,
> those contained in the `.env.example` are **reasonable** defaults,
> I do not recommend changing them.
> In any case, change these options only if you understand what you're doing and why.
> Consider yourself warned ;)

---

## Running the Application

### Initial Run (First Time)

1. **Build and run in the foreground**, from the project root:

   ```bash
   docker compose up --build
   ```

   The `--build` flag is needed the first time:
   it builds the `do-checker-ent:<version>` image named in `docker-compose.yml`.
   **The first build takes 5-8 minutes** — it downloads Playwright and Chromium.
   Logs appear in your terminal.

2. **Open the application** at `http://127.0.0.1:8888`, or at the host and port
   you set in `.env`.

3. **Stop it** with `Ctrl+C`.

   Avoid stopping it while a run is in progress:
   checks that are in flight are killed,
   and the log will say so in the least helpful way possible (`Worker was sent SIGTERM!`).

### Day to day

```bash
docker compose up -d        # start in the background
docker compose logs -f      # follow the log
docker compose restart      # apply changes to app.yaml or .env
docker compose down         # stop and remove the container
```

The project directory is mounted into the container,
so code changes are picked up without a rebuild.
A rebuild is only needed when dependencies change.

### Updating to a new version

1. **Pull the latest code:**

   ```bash
   git pull origin master
   ```

   This also updates the `VERSION` file and the image tag in
   `docker-compose.yml` — both are committed by the release automation.

2. **Rebuild and restart:**

   ```bash
   docker compose up -d --build
   ```

3. **(Optional) Clean up** images you no longer need:

   ```bash
   docker image prune -f
   ```

### Upgrading from 1.x to 2.0.0

**Nothing in your configuration has to change.** An existing `.env` and an
existing `app.yaml` keep working;
the only thing you will see in the log is a note that `soax.proxy_type` has been
renamed to `soax.transport` and is still honoured under the old name.

What is new in `app.yaml`, all optional and all with defaults:

| Key | Default |
|---|---|
| `soax.transport` | *(none)* — the new name of `soax.proxy_type` |
| `alive_check.enabled` | `true` |
| `alive_check.targets` | three third-party sites |
| `alive_check.required_ok` | `2` |
| `alive_check.timeout_sec` | `10` |
| `dns.use_ipv6` | `false` |

New optional variables in `.env`:

- `SOAX_TRANSPORT`
- `ALIVE_CHECK_ENABLED`, `ALIVE_CHECK_TARGETS`, `ALIVE_CHECK_REQUIRED_OK`,
  `ALIVE_CHECK_TIMEOUT_SEC`
- `DNS_USE_IPV6`
- the gunicorn group: `GUNICORN_WORKERS`, `GUNICORN_TIMEOUT`,
  `GUNICORN_GRACEFUL_TIMEOUT`, `GUNICORN_RELOAD`

The current `.env.example` and the `app.yaml` template inside
`docker/entrypoint.sh` carry all of them with comments —
copy from there rather than from this table.

Four changes in behaviour are worth knowing about before the first run,
because they happen whether you touch the configuration or not:

* **Every run starts by checking that the proxy service answers at all**,
  before a single mirror is touched.
  If it does not,
  the run is refused with an explanation instead of reporting forty mirrors as
  unavailable.
  Costs two or three requests per run; switch it off with `alive_check.enabled: false`.
* **IPv6 is disabled.** SOAX does not route it,
  so an attempt to leave over IPv6 is a guaranteed failure that reads as a
  mirror being down.
* **A run needs an explicit transport** if neither `soax.transport` nor the old
  `soax.proxy_type` is set: the dropdown starts unselected.
* **Settings are applied at startup only**,
  including edits made through the Settings page.
  See [Settings](#settings-settings).

One thing to watch if you keep local modifications to `docker-compose.yml`
rather than taking the repository's copy:
the file has gained an `environment:` block,
and a variable not listed there never reaches the container.
See [Where a setting comes from](#where-a-setting-comes-from).

---

## The country catalog

The list of countries offered in the **Country** dropdown lives in
`data/catalog/soax_geo.json` and ships with a default list.
Edit it on the **Geo Catalog** page: add ISO 3166-1 alpha-2 codes,
remove the ones you do not need, save.
A code the ISO 3166-1 table does not know is refused with a note saying so,
and a near miss is corrected in it (`uk` → `gb`, `usa` → `us`, `germany` → `de`);
an accepted one is named from the same table,
so the dropdown reads `Ukraine` rather than `UA`.

**The list is maintained by hand.** The "Refresh from SOAX" button needs a
catalog API that the enterprise plan does not expose, and it will not bring anything back.
When setting up another installation,
copy `soax_geo.json` across rather than retyping the list.

The catalog does not limit what you can check:
the **Multi-Geo** page accepts any valid two-letter country code,
whether or not it is in the catalog.
The catalog drives the dropdown and the **ALL** option —
and in **ALL** an entry the ISO table does not know is skipped,
with a line in the log naming it,
rather than paired with every URL of the run.

---

## Usage

### Main Checker (`/`)

Paste a list of URLs, pick one country and one network type, choose a transport, run.
Results stream in live.

URLs may be written bare (`example.com`) — the scheme defaults to `http://`,
and redirects to HTTPS are followed and recorded.

Rows appear in the order you submitted them, as soon as the run starts,
and fill in place as each check finishes.
Above and below the results is a line with the totals: how many checks,
over how many domains and countries, and how long the run took.

While a run is in progress all three buttons — `Run check`,
`Clear logs` and `Reset Form` — are disabled.

### Multi-Geo Checker (`/multi-geo`)

One `domain country_code` pair per line:

```
site1.org ca
site1.org az
site2.org tr
```

Rows with a bad format or a country code that is not in ISO 3166-1 are reported
individually rather than failing the whole run,
and the note on the row carries the correction when there is an obvious one.
A code is checked before the run starts,
so a typo costs a row in the report instead of a request through the proxy.

Selecting **ALL** in the Checker's country dropdown expands into the same
mechanism: every URL is paired with every country in the catalog.

### Geo Catalog (`/catalog`)

See [The country catalog](#the-country-catalog).

### Settings (`/settings`)

Direct editing of `data/config/app.yaml`.

**Changes take effect only after a restart** — `docker compose restart` —
and that applies to edits made by hand as well.
The page shows the file as it is on disk,
and saving writes the file and nothing more:
the running application keeps the settings it started with.
Saying so plainly because the opposite is the natural assumption for a settings page.

Text that is not YAML, or that contains no recognisable section,
is refused and your file is left alone.
Values that are merely wrong are accepted here and replaced with defaults at the
next startup, with a note on the page listing what was corrected.

---

## Transports and DNS resolve

A run goes over **HTTP** or **SOCKS5**, chosen in the **Transport** dropdown.
The thing is that the SOAX often responds differently to the same site through
`HTTP` and through `SOCKS5`.
Which one the dropdown starts on comes from `soax.transport`;
with no value there, the choice is explicit every time.

**DNS resolve** decides where the hostname is resolved:

* `on exit node` (default) — the exit node resolves it,
  which is what a user in that country would get.
  A geo-targeted check whose DNS happened in our datacentre is only half a
  geo-targeted check.
* `local` — resolved here, and an address is sent to the gateway.

Screenshots are taken through the same transport and the same session as the
check, so the picture comes from the same exit node as the verdict.
Chromium refuses a SOCKS5 proxy that requires credentials,
so during a SOCKS5 run the screenshot goes through a local SOCKS5 front end that
holds the credentials itself.
If that cannot start,
the screenshot falls back to HTTP and the check's log file says so in its notes —
a picture taken over another transport is not evidence about this one.

---

## Before each run: the proxy check

Before the mirrors are touched,
the application fetches a few third-party sites through the gateway.
If fewer than `alive_check.required_ok` of them answer,
the run does not start and the page says why.

The reason this exists: when the proxy service is down,
a run without this check reports every mirror as unavailable.
That is not a useless report, it is a misleading one —
and it costs the traffic of producing it.

Each target is probed in its own session,
through the transport and country the run is about to use,
and probing stops as soon as enough have answered.
The log records each one under `[alive-check soax]`.

The default targets are three sites run by three unrelated operators,
each answering a few hundred bytes.
Both properties matter:
a shared operator would make "two of three failed" one failure rather than two,
and this runs before every check on traffic you pay for.
If you replace the list,
avoid anything that treats proxy addresses as suspicious — `google.com`, for one,
often answers them with captchas and `429`.

---

## How sessions work

Each check runs inside a **SOAX session**,
which asks the gateway to keep all of its requests on one exit node.

A single page render in Chromium issues dozens of proxied requests — HTML, CSS,
JS bundles, fonts, images.
Without a session, each one would exit from a different address,
in a different city, on a different ISP.
Many sites break outright under that; others serve a CAPTCHA.
The screenshot would be worthless and the verdict misleading.

By default the app opens **one session per checked site** (`session_scope: task`).
Fifteen URLs in one country means fifteen sessions and, with them,
up to fifteen different exit nodes.
That is the point: each mirror is sampled independently,
so a single bad node spoils one row rather than the whole report,
and a mirror that fails on one node while its neighbours succeed is a property
of the mirror, not of the proxy.
This is the setting to keep for routine availability reports.

Its cost is that two rows of the same report may have come from different cities
and ISPs,
so the report answers "is this mirror reachable from AZ" rather than "what does
one user in AZ see".

Setting `session_scope: geo` in `app.yaml` switches to one session per country,
so the sites in that country are checked from the same exit node.
Useful when you need a reproducible "from this address in Istanbul,
these mirrors behaved like this" — for a provider ticket, say.
The trade-off is that one unlucky node can skew the entire country's results.

Node affinity within a session is best-effort on the gateway's side,
not a guarantee:
two checks in one session have been seen leaving from different addresses.
The exit IP of each check is recorded in its own log file, so when it matters,
read it there rather than assuming.

Rotation rules (`rotate-timed`,
`rotate-requests`) are deliberately not supported:
both would change the IP mid-check, which is exactly what sessions exist to prevent.

A session expires after 60 seconds without requests.

### Concurrency

`MAX_CONCURRENCY` controls the app's thread pool, not a SOAX limit.
Sessions active at any moment roughly equal the thread count;
total sessions per run equal the number of checks.
Even a run of hundreds of checks keeps only a handful of sessions open at once.

`MAX_SCREENSHOT_WORKERS` is separate and defaults to 1,
because each screenshot starts its own Chromium instance and they are memory-hungry.

---

## Configuration

### Where a setting comes from

Four files have a say, and they are not alternatives to each other — each does one job:

| File | Job |
|---|---|
| `.env` | the environment of the container: secrets, paths, anything you want to set once per installation |
| `data/config/app.yaml` | settings with structure (lists, nested sections) and everything meant to be editable from the Settings page |
| `docker-compose.yml`, `environment:` | the list of variable **names** allowed through to the container, and nothing more |
| `docker/entrypoint.sh` | runs once at container start and creates `app.yaml` and the country catalog **if they are missing**. It never changes a file that already exists |

Inside the application the order is: **built-in defaults**, then `app.yaml`,
then environment variables.
The last one wins —
a variable in the environment overrides the same setting in `app.yaml`,
which is why the file is the right place for a lasting choice and the variable
is the right place for a temporary one.

Docker Compose adds one step before that.
A variable reaches the container only if its name appears in the `environment:`
block, and its value is resolved in this order:

| Where it is set | Result |
|---|---|
| in your shell, in front of the command | that value wins — `GUNICORN_WORKERS=1 docker compose up -d --force-recreate` |
| in `.env` | that value is used — Compose passes the whole file into the container |
| nowhere | the variable is not set at all, so `app.yaml` (or the application's default) decides |

This is why some names in that block overlap with `.env` and some do not.
The block is not a second configuration file —
it is a list of what may be overridden from the shell for a single run,
which is exactly what you want when testing something:
one worker instead of four, IPv6 on for one run,
the reachability check pointed at unreachable targets to see what happens.
`SOAX_PACKAGE_PASSWORD` is in the list for the same reason,
even though its real value always comes from `.env`.

A problem in `app.yaml` does not stop the application.
An unreadable file, a missing one, a duplicated key, a value that makes no sense —
each is replaced with a default and listed in a notice at the top of every page.
Duplicated keys keep the **first** value:
a line pasted in by hand is almost always the later one.
The only problem that stops checks from running is a missing package password,
and the notice says so.

### `soax:`

```yaml
soax:
  host: proxy.soax.com
  port: 1337
  transport: http           # http | socks5; empty means "no default, choose per run"
  session_scope: task       # task | geo
  on_error: retry           # replace | retry | fail
  on_error_retries: 3       # 1..10
  bind_node: false          # incompatible with on_error: replace
  preflight: true           # resolve ext_ip and actual geo before checking
```

`on_error` maps to the gateway's own error handling:
`replace` swaps in a new node immediately,
`retry` retries on the same one up to N times, `fail` returns the error as-is.
`retry` is the default —
it survives a single flaky node without silently hiding a genuine outage.

`transport` is only the value the forms preselect — a run carries its own.
Leave the field empty (or delete the line) and there is no default at all:
the dropdown starts unselected and a run without an explicit transport is refused.
An unrecognised value is treated the same way,
with a warning in the log rather than a refusal to start —
which transport suits the work at hand is the operator's call,
not a reason to keep the app down.
The old name `proxy_type` still works as a synonym,
so an existing `app.yaml` keeps its default after an upgrade.

`preflight` adds one lightweight request per session against SOAX's own IP lookup.
It fills in the exit IP and the node's real location,
and turns gateway errors into readable messages.
Turning it off saves a negligible amount of traffic and costs you most of the diagnostics.

### `alive_check:`

```yaml
alive_check:
  enabled: true
  targets:
    - http://example.com
    - http://detectportal.firefox.com/success.txt
    - http://cloudflare.com/cdn-cgi/trace
  required_ok: 2
  timeout_sec: 10
```

See [Before each run](#before-each-run-the-proxy-check).
`required_ok` is clamped to the number of targets,
and a value below 1 is raised to 1 —
otherwise the check could either never pass or never fail.

### `dns:`

```yaml
dns:
  use_ipv6: false
```

With IPv6 off, AAAA records are dropped from every name the application resolves,
and a name that has no IPv4 address fails with a message saying so.
The gateway does not route IPv6,
so the alternative is a guaranteed failure reported as an unavailable mirror.
Turning it on restores the previous behaviour; nothing else changes.

### `http_client:custom_headers`

Adds headers to every check, both `requests` and Playwright:

```yaml
http_client:
  user_agent: "Mozilla/5.0..."
  # ...
  custom_headers:
    X-CF-Bypass: "MySecretToken123"
    Another-Header: "SomeValue"
```

An empty section must keep its braces — `custom_headers: {}`.
When adding entries, remove the braces.

### Gunicorn

The server is configured by `docker/gunicorn.conf.py`,
which reads four variables: `GUNICORN_WORKERS` (4), `GUNICORN_TIMEOUT` (180),
`GUNICORN_GRACEFUL_TIMEOUT` (30) and `GUNICORN_RELOAD` (true).
They can be set per run without editing anything:

```bash
GUNICORN_WORKERS=1 docker compose up -d --force-recreate
```

`GUNICORN_TIMEOUT` is not a limit on how long a check may take:
a long screenshot does not count against it.
It is insurance against a worker that has genuinely stopped responding.

---

## Reading results

Every run gets its own directory under `./logs/`:

```
logs/2026-10-17/13-43-50_std-check_soax-http/
    13-43-50_example.com.md
    13-43-50_example.com.png
```

The directory name carries the time,
the mode (`std-check` or `multi-geo`) and the channel the run used (`soax-http`,
`soax-socks5`),
because that is what you need to know when comparing two reports about the same mirror.
Multi-Geo file names end with the country code.

A check's log file looks like this:

```
# example.com
Started: 2026-10-17T13:43:53
Geo: AZ (requested) — exit node: AZ / Baku / Example Telecom AS64496
Proxy: SOAX session dcab12cd3400, ext_ip: 203.0.113.42
Provider: soax
Transport: http
DNS resolve: remote (on the exit node)
Network: res_mob
Timeout: 30s
URL: http://example.com
```

followed by the redirect chain, the timings, the HTTP status and size,
and the verdict with any notes.

The log file records both the **requested** country and the **actual** exit node.
A mismatch between the two is flagged in the notes,
and it is worth paying attention to:
SOAX occasionally routes through a neighbouring country when a pool is thin,
and a result labelled "reachable from AZ" that actually came from Georgia is
worse than no result at all.

### Result classes

| Class | Meaning |
|---|---|
| `success` | HTTP 2xx or 3xx |
| `http_error` | the site answered with 4xx or 5xx |
| `proxy_error` | **the gateway** rejected the request — see below |
| `timeout`, `dns_error`, `tls_error`, `connect_error` | transport-level failures |

`proxy_error` is a configuration or quota problem on our side, not a property of the site.
The note carries SOAX's own diagnosis:

| Gateway code | Usual cause |
|---|---|
| `AUTH_FAILED` | wrong or missing `SOAX_PACKAGE_PASSWORD` |
| `TRAFFIC_LIMIT_EXCEEDED` | the package is out of traffic |
| `ERR_INVALID_VALUE` | a rule value the gateway does not recognise |
| `NODE_NOT_FOUND` | filters are valid but no node matches |
| `SESSION_PARAMS_MISMATCH` | a session ID was reused with different rules |

---

## Logs and data

* `./logs/<date>/<run>/` — the logs and screenshots of each run
* `./logs/engine.log` — what the application and the server did
* `./logs/access.log` — HTTP requests to the application itself
* `./logs/faulthandler.log` — stack dumps from hard crashes, empty in normal use
* `./data/config/app.yaml` — configuration
* `./data/catalog/soax_geo.json` — country list

Both directories are bind-mounted from the host, so they survive container rebuilds.

`engine.log` holds the application and the server in one file and one format,
with the run and the URL on every line that belongs to a check:

```
2026-10-17 13:43:50 [INFO] [pid=14] [run=ab12cd34ef56 url=example.com] engine: Task started.
```

That is what makes a report from days ago readable:
take the run id from a check's log file, grep `engine.log` for it,
and the whole sequence is there, including what the server was doing at the time.
The same stream goes to stdout, so `docker compose logs -f` shows it too.

**[TROUBLESHOOTING.md](TROUBLESHOOTING.md) is the companion to this section:**
what each marker in the log means,
and the recipe for every question that usually sends someone into it —
why a run did not start, whether the proxy or the site is at fault,
why a screenshot is missing, why a worker died.

`LOG_LEVEL=DEBUG` in `.env` adds the per-request detail:
the proxy session being built, the screenshot machinery,
individual connections refused by the gateway during a page render.

The **Clear Logs** button on the Checker page removes the per-run directories.
The application's own logs are left alone.
There is no undo.

---

## Known limitations

**No region / city / ISP targeting.** The gateway supports it,
but the values must be exact slugs from a catalog API that is not exposed on the
enterprise plan.
The dashboard's own dropdowns call an internal endpoint that is not documented.
Until that access materialises, targeting is by country only.

**The country list is maintained by hand.** There is no way to refresh it from
SOAX on this plan.
Copy `soax_geo.json` between installations rather than rebuilding it.

**The gateway does not route IPv6.** A mirror that exists only on IPv6 cannot be
checked through SOAX by any transport.
This is why `dns.use_ipv6` defaults to off: the failure is certain,
and it is better to name it than to report it as an unavailable site.

**Gateway errors are invisible over SOCKS5.** A refusal that HTTP reports as
`400 ERR_INVALID_VALUE` with an explanation arrives over SOCKS5 as a nameless
connection failure — a wrong rule and a wrong password look identical.
This is why `preflight` goes over HTTP even for a SOCKS5 run,
and why turning it off costs more than it looks.

**A SOAX session expires after 60 seconds of inactivity.** If a screenshot waits
a long time for its slot,
its session may have lapsed and Chromium will land on a different node than the check did.
In practice runs are fast enough that this rarely happens,
but a screenshot that disagrees with the recorded exit IP is the symptom.

**Live results need the browser and the run to share a worker.** Run state lives
in the memory of the process that started it, and there are four by default.
The browser normally stays on one connection and therefore one worker,
so this is rarely visible; when it is not the case,
the page says live updates are unavailable and the run continues regardless —
the logs in `./logs/` are unaffected.

**Settings are applied at startup only**, including edits through the Settings page.
Restart the container after changing `app.yaml`.

**The Settings page keeps no backup.** It refuses text it cannot parse,
but a valid edit overwrites the previous configuration with no copy kept.
Worth a `cp data/config/app.yaml data/config/app.yaml.bak` before a substantial change.

---

## Versioning and updates

Semantic versioning (`X.Y.Z`).
Image tags in `docker-compose.yml` point at specific releases rather than
`latest`, so a rebuild cannot silently change what you are running.
See [CHANGELOG.md](CHANGELOG.md) — including why the numbering restarts at 1.0.0.

### Update notifications

A background thread compares the local `VERSION` with the one in the repository
every three hours.
When a newer version exists,
a **"New version available"** badge appears in the header, next to the theme toggle.

### Update procedure

See [Updating to a New Version](#updating-to-a-new-version).
The release automation commits the new `VERSION` and image tag to master,
so `git pull` is enough — no manual editing of configuration files.

---

## Stopping the Application

* `Ctrl+C` in the terminal, if it is running in the foreground.
* `docker compose down`, if it is running detached.

Either way, let a run in progress finish first.
