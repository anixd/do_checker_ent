# DutyOps Mirrors Checker (do_checker_ent)

A web application to check the availability of websites ("mirrors") through the SOAX proxy service using specified geographic parameters. The application runs locally using Docker.

## Prerequisites

* **Python 3.12** or higher (been tested on 3.14).
* **Docker:** Ensure Docker is installed and running. Installation instructions: [Get Docker](https://docs.docker.com/get-docker/)
* **Docker Compose:** Usually installed with Docker Desktop. Check with `docker-compose --version`.

---

## Setup

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/anixd/do_checker_ent
    cd do_checker_ent
    ```

2.  **Create the `.env` file:**
    Copy the `.env.example` file to `.env`:
        ```bash
        cp .env.example .env
        ```
Then edit it. The important entries:

| Variable | Required | Notes                                                                                                                                      |
|---|---|--------------------------------------------------------------------------------------------------------------------------------------------|
| `SOAX_PACKAGE_PASSWORD` | **yes** | The **Package password** from the SOAX dashboard, Settings tab. This is what authenticates the proxy. The app refuses to start without it. |
| `SOAX_PACKAGE_ID` | no | The package UUID from the same tab. Not used in the connection string; reserved for a future catalog API.                                  |
| `APP_HOST`, `APP_PORT` | no | Defaults to `127.0.0.1:8888`.                                                                                                              |
| `DEFAULT_THEME` | no | `light`, `dark` or `auto`. Defaults to `auto`.                                                                                             |
| `TZ` | no | e.g. `Europe/Kyiv`. Defaults to `UTC`.                                                                                                     |
| `MAX_CONCURRENCY`, `MAX_SCREENSHOT_WORKERS`, timeouts | no | Tune to your workstation's RAM. The defaults are conservative on purpose — see [Concurrency](#concurrency).                                |

> **Do not put the package password in `app.yaml`.** That file is editable
> through the Settings page and rendered into the browser, so anything in it
> ends up in screenshots and shared sessions. Secrets belong in `.env` only.

> **`MAX_CONCURRENCY`, `MAX_SCREENSHOT_WORKERS`, and timeouts:** You can adjust performance parameters, although, these are **reasonable** defaults, I do not recommend changing them. In any case, change these options only if you understand what you're doing and why. Consider yourself warned ;)

---

## Running the Application

### Initial Run (First Time)

1.  **Build and run (foreground):**
    * Run the following command in the project's root directory:
        ```bash
        docker-compose up --build
        ```
    * **Note**: *First launch may take 5-8 minutes due to Playwright and Chromium image download.*
    * The `--build` flag is necessary for the first run. This will build the `do-checker:YYYYMMDD` image specified in `docker-compose.yml` and start the container. You will see logs in your terminal.

2.  **Access the application:**
    * Open your web browser and navigate to `http://127.0.0.1:8888` (or the host/port you set in `.env`).

3.  **Stopping (foreground):**
    * Press `Ctrl+C` in the terminal.

### Updating to a New Version

1.  **Pull changes:**
    * Get the latest code from the repository:
        ```bash
        git pull
        ```

2.  **Rebuild the image:**
    * Build the new version of the image (it will get a new image tag based on the `image:` definition in `docker-compose.yml`):
        ```bash
        docker-compose build
        ```

3.  **Restart the application (detached):**
    * Stop the old container and start the new one in the background:
        ```bash
        docker-compose up -d
        ```

4.  **(Optional) Clean up old images:**
    * After you confirm the new version works, you can clean up old, untagged images to save disk space:
        ```bash
        docker image prune
        ```

---

## First Run: Syncing the Geo Catalog

When the application starts for the first time, it uses an **empty** Geo catalog (`soax_geo.json`). To use the checker, you **must** fetch the Geo data from SOAX first.

**Requires `SOAX_API_KEY` and `SOAX_PACKAGE_KEY` to be set in your `.env` file.**

1.  Open the **Geo Catalog** page.
2.  The list "Keep/Remove existing countries" will be pre-filled with a default list (KZ, AZ, IN, etc.). You can add new ISO-2 codes (e.g., `pl`) or remove unneeded ones using the form. Click **"Save List Changes"**.
3.  Then click the **"Refresh from SOAX (in background)"** button.
4.  Wait 1-2 minutes. The page will show a flash message when started, and the "Generated At" timestamp will update when finished (you may need to refresh the page to see the final result).

Once this is done, the **Checker** page will correctly show countries, regions, cities, and ISPs in the dropdown menus.

---

## Usage

### Checker (`/`)

Paste a list of URLs, pick one country and one network type, run. Results stream
in live over SSE.

URLs may be written bare (`example.com`) — the scheme defaults to `http://`, and
redirects to HTTPS are followed and recorded.

### Multi-Geo Checker (`/multi-geo`)

One `domain country_code` pair per line:

```
site1.org ca
site1.org az
site2.org tr
```

Rows with a bad format or an invalid country code are reported individually
rather than failing the whole run.

Selecting **ALL** in the Checker's country dropdown expands into the same
mechanism: every URL is paired with every country in the catalog.

### Geo Catalog (`/catalog`)

Add or remove countries by ISO 3166-1 alpha-2 code. Existing entries keep their
names; new codes get a placeholder name you can edit in the JSON directly.

> Region, city and ISP targeting is **not available** in this build. The SOAX
> API that supplied those lists is not reachable on the enterprise plan, and the
> gateway rejects invented values with `400 ERR_INVALID_VALUE`. See
> [Known limitations](#known-limitations).

### Settings (`/settings`)

Live editing of `data/config/app.yaml`. Invalid YAML and invalid values are
rejected before the file is written, so a bad edit cannot leave the app unable
to restart — but it is still the sharpest tool in the box.

---

## How sessions work

Each check runs inside a **SOAX session**, which pins all its requests to one
exit node and therefore one IP address.

This is not a nicety. A single page render in Chromium issues dozens of proxied
requests — HTML, CSS, JS bundles, fonts, images. Without a session, each one
would exit from a different address, in a different city, on a different ISP.
Many sites break outright under that; others serve a CAPTCHA. The screenshot
would be worthless and the verdict misleading.

By default the app opens **one session per checked site** (`session_scope: task`).
Fifteen URLs in one country means fifteen independent samples: a single bad exit
node spoils one row, not the whole report.

Setting `session_scope: geo` in `app.yaml` switches to one session per country,
so every site in that country is checked from the same IP. Useful when you need
a reproducible "from this specific address in Baku, these mirrors behaved like
this" — for a provider ticket, say. The trade-off is that one unlucky node can
skew the entire country's results.

Rotation rules (`rotate-timed`, `rotate-requests`) are deliberately not
supported: both would change the IP mid-check, which is exactly what sessions
exist to prevent.

### Concurrency

`MAX_CONCURRENCY` controls the app's thread pool, not a SOAX limit. Sessions
active at any moment roughly equal the thread count; total sessions per run
equal the number of checks. Even a run of hundreds of checks keeps only a
handful of sessions open at once.

`MAX_SCREENSHOT_WORKERS` is separate and defaults to 1, because each screenshot
starts its own Chromium instance and they are memory-hungry.

---

## Configuration

Settings load in order, later overriding earlier:

1. `data/config/app.yaml` — created on first run if missing
2. environment variables from `.env`

Most things are best set in `.env`. Two structures live only in `app.yaml`.

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

`on_error` maps to the gateway's own error handling: `replace` swaps in a new
node immediately, `retry` retries on the same one up to N times, `fail` returns
the error as-is. `retry` is the default — it survives a single flaky node
without silently hiding a genuine outage.

`transport` is only the value the forms preselect — a run carries its own, picked
in the **Transport** dropdown on either checking page. Leave the field empty (or
delete the line) and there is no default at all: the dropdown starts unselected
and a run without an explicit transport is refused. An unrecognised value is
treated the same way, with a warning in the log rather than a refusal to start —
which transport suits the work at hand is the operator's call, not a reason to
keep the app down. The old name `proxy_type` still works as a synonym, so an
existing `app.yaml` keeps its default after an upgrade.

Why both transports exist: the same mirror can answer differently over HTTP and
over SOCKS5, so checking over one is not evidence about the other. **DNS resolve**
decides where the hostname is resolved — `on exit node` (the default, a
geo-targeted check whose DNS happened in our datacentre is only half a check) or
`local`.

`preflight` adds one lightweight request per session against SOAX's own IP
lookup. It fills in the exit IP and the node's real location, and turns gateway
errors into readable messages. Turning it off saves a negligible amount of
traffic and costs you most of the diagnostics.

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

An empty section must keep its braces — `custom_headers: {}`. When adding
entries, remove the braces.

---


## Reading results

Every check writes a Markdown card to `./logs/YYYY-MM-DD/`, alongside a PNG when
screenshots are enabled.

The card records both the **requested** country and the **actual** exit node:

```
Geo: AZ (requested) — exit node: AZ / Baku / Azertelecom AS29049
Proxy: SOAX session dca1b2c3d407, ext_ip: 5.191.10.20
```

A mismatch between the two is flagged in the notes. It is worth paying attention
to: SOAX occasionally routes through a neighbouring country when a pool is thin,
and a result labelled "reachable from AZ" that actually came from Georgia is
worse than no result at all.

### Result classes

| Class | Meaning |
|---|---|
| `success` | HTTP 2xx or 3xx |
| `http_error` | the site answered with 4xx or 5xx |
| `proxy_error` | **the gateway** rejected the request — see below |
| `timeout`, `dns_error`, `tls_error`, `connect_error` | transport-level failures |

`proxy_error` is a configuration or quota problem on our side, not a property of
the site. The note carries SOAX's own diagnosis:

| Gateway code | Usual cause |
|---|---|
| `AUTH_FAILED` | wrong or missing `SOAX_PACKAGE_PASSWORD` |
| `TRAFFIC_LIMIT_EXCEEDED` | the package is out of traffic |
| `ERR_INVALID_VALUE` | a rule value the gateway does not recognise |
| `NODE_NOT_FOUND` | filters are valid but no node matches |
| `SESSION_PARAMS_MISMATCH` | a session ID was reused with different rules |

---

## Logs and data

* `./logs/` — check results (`.md`, `.png`) and `engine.log`
* `./data/config/app.yaml` — configuration
* `./data/catalog/soax_geo.json` — country list

Both directories are bind-mounted from the host, so they survive container
rebuilds.

The **Clear Logs** button on the Checker page wipes `./logs/` entirely. There is
no undo.

---

## Known limitations

**No region / city / ISP targeting.** The gateway supports it, but the values
must be exact slugs from a catalog API that is not exposed on the enterprise
plan. The dashboard's own dropdowns call an internal endpoint that is not
documented. Until that access materialises, targeting is by country only.

**The country list is maintained by hand.** Now there is no way to refresh it from
SOAX. Copy `soax_geo.json` between installations rather than rebuilding it.

**Screenshots always go over HTTP.** Chromium cannot authenticate against a
SOCKS5 proxy, and without authentication the gateway carries no targeting rules —
so a screenshot taken during a SOCKS5 run goes over HTTP instead, reusing the
same session and therefore the same exit node. The card says so in its notes.
A screenshot taken *through* SOCKS5 needs a local shim and is planned separately.

**Gateway errors are invisible over SOCKS5.** A refusal that HTTP reports as
`400 ERR_INVALID_VALUE` with an explanation arrives over SOCKS5 as a nameless
connection failure — a wrong rule and a wrong password look identical. This is
why `preflight` goes over HTTP even for a SOCKS5 run, and why turning it off
costs more than it used to.

**A SOAX session expires after 60 seconds of inactivity.** If a screenshot waits a
long time for its slot, its session may have lapsed and Chromium will land on a
different SOAX node than the HTTP check did. In practice runs are fast enough that
this rarely happens, but a screenshot that disagrees with the recorded exit IP
is the symptom.

---

## Versioning & Updates

## Versioning

Semantic versioning (`X.Y.Z`). Image tags in `docker-compose.yml` point at
specific releases rather than `latest`, so a rebuild cannot silently change what
you are running.

### Update Notifications

The application automatically checks for new versions in a background thread every 3 hours. If a newer version is detected on GitHub, a **"New version available"** badge will appear in the web interface header next to the theme toggle.

### Update Procedure

Thanks to GitHub Actions automation, the update process is straightforward and does not require manual editing of configuration files:

1. **Pull the latest changes** from the master branch:
```bash
git pull origin master

```

*This command automatically updates the `VERSION` file and the image tag in `docker-compose.yml` to the latest release.*
2. **Rebuild and restart** the container:
```bash
docker-compose up -d --build

```

3. **(Optional) Clean up** old images to save disk space:
```bash
docker image prune -f

```

---

## Stopping the Application

* Press `Ctrl+C` in the terminal (if running in foreground).
* Run `docker-compose down` (if running in detached mode).

