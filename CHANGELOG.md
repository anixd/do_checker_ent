# Changelog

## About the numbering

This checker existed before moving to SOAX enterprise plan,
and that version was developed up to **1.5.3**.
The enterprise plan replaced port-based proxying with session-mode targeting,
which meant rewriting the parts of the application that talk to the gateway.
Rather than continue the old line,
the project was forked into this repository and the numbering restarted at **1.0.0**.

Entries for 1.5.3 and below therefore describe the previous incarnation and are
kept for reference:
the user interface and the result format have a common ancestry,
and an old note still explains why some things look the way they do.
Anything from 1.0.0 onwards describes this repository.

---

## [Unreleased]

### What's new

* **Country codes are checked against ISO 3166-1 alpha-2 before a run starts.**
  A code the table does not know is refused on the Checker page,
  reported per row on the Multi-Geo page,
  and rejected when added on the Geo Catalog page.
  Until now only the shape was checked,
  so `zz` reached the gateway and spent a request per mirror to be told it is
  not a country.
  The message carries the correction when there is an obvious one:
  `uk` → `gb`, `usa` → `us`, `germany` → `de`.
* **A country added to the catalog is named from the same table,**
  so the dropdown reads `Ukraine` instead of the placeholder `UA`.
  The name is the short one: `United Kingdom`, not the 52-character official
  title, and `Taiwan` rather than the political long form.
  Names already in the catalog are left as they are.
* **In ALL mode, catalog entries the ISO table does not know are skipped**
  with a line in the log naming them,
  rather than paired with every URL of the run.

### Upgrading

* This release adds the first external dependency, `iso3166`
  (9 KB, no dependencies of its own),
  so update with `docker compose up -d --build` rather than a restart.

---

## [2.0.2] - 2026-10-19

### What's new

* **TROUBLESHOOTING.md.** What the application writes down and how to get an
  answer out of it:
  what each marker in `engine.log` means,
  and a recipe for every question that usually sends someone into the log —
  why a run did not start, whether the proxy or the site is at fault,
  why a screenshot is missing, why a worker died.
  Linked from the README and from the Help page.

### Changed

* **`Clear logs` keeps the application's own logs.**
  The button removes the per-run directories under `./logs/` and leaves
  `engine.log`, `access.log` and `faulthandler.log` alone.
  It used to wipe the whole directory,
  so clearing out old reports also destroyed the record of how they were produced.

---

## [2.0.0] - 2026-10-18

A major release because the way a run reaches the proxy changed, not only what it reports.
Everything below is new since the fork baseline.

### What's new

* **SOCKS5 transport.** A run now picks its transport — HTTP or SOCKS5 —
  in the **Transport** dropdown on either checking page.
  The same mirror can answer differently over the two,
  so checking over one is not evidence about the other.
  `soax.transport` in `app.yaml` sets which one the forms preselect;
  with no value there, the transport must be chosen explicitly for every run.
* **DNS resolve mode.** `on exit node` (default) resolves the hostname where the
  check exits, `local` resolves it locally and sends an address.
* **Screenshots over SOCKS5.** Chromium refuses a SOCKS5 proxy that needs
  credentials,
  so a SOCKS5 run takes its screenshot through a local SOCKS5 shim/middleware
  that holds the credentials itself.
  Same session, same exit node as the check.
  If it cannot start, the screenshot falls back to HTTP and the log says so.
* **Pre-run proxy check.** Before a run,
  a few third-party sites are fetched through the proxy; if too few answer,
  the run is refused with an explanation instead of reporting every mirror as unavailable.
  Configurable under `alive_check:` — targets, how many must answer, timeout,
  and an on/off switch.
* **IPv6 switch** (`dns.use_ipv6`, off by default).
  SOAX does not route IPv6,
  so an attempt to leave over it is a guaranteed failure that reads as a mirror
  being down.
  With the switch off, AAAA records are dropped from every lookup the application makes.
* **Totals above and below the results**: how many checks,
  domains and countries a run covers, and how long it took.
* **Diagnostics for post-mortem reading**:
  one log format and one destination for the application and gunicorn alike,
  every line carrying the run and the URL it belongs to;
  a fault handler for hard crashes; a watchdog around screenshots,
  the one unbounded step of a check.
* **Gunicorn settings from the environment**: `GUNICORN_WORKERS`,
  `GUNICORN_TIMEOUT`, `GUNICORN_GRACEFUL_TIMEOUT`, `GUNICORN_RELOAD`.

### Changed

* **The application always starts.** An unreadable `app.yaml`, a missing one,
  a duplicate key, an unusable value —
  each is reported and replaced with a default, and the pages list what was corrected.
  Previously a stray key pasted into the file could leave the container
  restarting in a loop, with no interface and no log to find out why.
* **Duplicate keys keep the first value**, with both line numbers in the log.
  YAML itself keeps the last one silently,
  and a duplicated section discards the whole first block.
* **Settings are applied at startup only.** Editing `app.yaml`,
  through the Settings page or by hand, takes effect only after `docker compose restart`.
  The page now shows the file as it is on disk and says so after saving.
* **Results appear in the order they were submitted**,
  with every row drawn as soon as the run starts and filled in place as it finishes.
* **One session per check by default** (`soax.session_scope: task`),
  or one per country (`soax.session_scope: geo`).
* **Run output is grouped per run**:
  `./logs/YYYY-MM-DD/HH-MM-SS_std-check_soax-http/`,
  with the provider and transport in the directory name.
* **The result log** reports `Provider`, `Transport` and `DNS resolve`,
  and the line that used to be called `DNS mode` is now `Network` —
  it always carried the network type, not a DNS setting.
* **Every button on the form is disabled while a run is in progress**,
  not only `Run check`.

### Fixed

* The Settings page no longer accepts text with no recognisable section,
  which used to overwrite a working configuration with defaults.
* Live updates no longer lose the events emitted before the browser connects,
  so the results header and the task list arrive as intended.
* A stream requested for a run this process does not know now closes with an
  explanation instead of hanging empty.
* The results header shows the number of checks instead of `?
  URLs`.
* The release workflow updates the image tag in `docker-compose.yml` again:
  it was looking for an image name the project stopped using at the fork,
  and silently did nothing.

### Known limitations

See the README.
In short: targeting is by country only, the country list is maintained by hand,
IPv6 is not routed by the gateway,
and live updates work only while the run and the browser share a gunicorn worker.

---

## [1.0.0] - 2026-07-28

The fork baseline: the checker rebuilt for the SOAX enterprise plan.

### What's new

* **Session-mode proxying.** Targeting rules travel inside the proxy username
  and the package password authenticates,
  replacing the port-per-geo scheme of the previous plan.
* **Country targeting** with a local catalog of countries
  (`data/catalog/soax_geo.json`),
  including an **ALL** option that pairs every submitted URL with every country in it.
* **Multi-Geo checker** — one `domain country_code` pair per line,
  with bad rows reported individually.
* **Live results** streamed into the page as the run progresses.
* **A Markdown log file per check** with timings, redirect chain,
  requested country versus actual exit node, and an optional screenshot.
* **Session scope** and the gateway's own error handling (`on_error`,
  `on_error_retries`, `bind_node`) as configuration.
* **Settings and Help pages**, update notification against the version on GitHub.

### Removed

* Port-based proxy selection, and with it region, city and ISP targeting:
  the enterprise plan exposes no catalog of those values,
  and the gateway rejects anything it does not recognise.

---

## [1.5.3] - 2026-04-19

*Entries from here down describe the pre-fork project.*

### What's new

* **All-Country Check Mode**:
  The "Country" dropdown on the Main Checker now has an "ALL" option at the top.
  Selecting it runs the submitted URL(s) against every country present in the
  local Geo catalog in a single submit.
  Region / City / ISP selectors are disabled in this mode and their values are
  ignored — SOAX picks those parameters randomly per request.
* **ALL-Mode Indicator**:
  A red "Checking in ALL available countries..." notice is shown above the
  action buttons while an ALL-mode run is in progress,
  and is hidden automatically when the run completes.

---

## [1.5.2] - 2025-12-30

### What's new

* **Smart Update Checker**:
  The application now uses `packaging.version` to compare versions.
  It only notifies about updates if the remote version is strictly higher than
  the local one.
* **CI/CD Automation**:
  Added GitHub Action to automatically sync `VERSION` and `docker-compose.yml`
  image tags when a new Git tag (vX.Y.Z) is pushed.
* **Dynamic Docker Tags**:
  The `docker-compose.yml` now uses the `${APP_VERSION}` variable,
  allowing users to switch between releases easily.

### Changed

* **URL Normalization Fix**: Refined the normalization logic.
  The domain is still lowercased for deduplication,
  but the **Local Path** and **Query Parameters** now preserve their original case.

### Fixed

* **404 Errors**: Resolved an issue where URLs with case-sensitive paths (e.g.,
  `/dDjpEn63`) failed because the entire URL was forced to lowercase.

---

## [1.5.1] - 2025-12-29

### What's new

* **Input Deduplication**:
  Both standard and Multi-Geo checkers now filter out duplicate URLs and "Domain
  + Geo" pairs before starting the run.
* **Case-Insensitive Domain Matching**:
  Added logic to treat `Domain.com` and `domain.com` as the same entry to
  prevent redundant checks.

---

