# Troubleshooting: reading the logs

What the application writes down,
and how to get an answer out of it when a run behaved strangely.
For where the files live and how the result directories are named,
see the [README](README.md).

## The three files

| File | What it holds |
|---|---|
| `logs/engine.log` | the application and the server, one format, one file |
| `logs/access.log` | requests to the application's own pages |
| `logs/faulthandler.log` | stack dumps from crashes that kill a worker outright; empty in normal use |

Per-check log files live under `logs/<date>/<run>/` next to the screenshots.
Those are the report; `engine.log` is the story of how it was produced.

The **Clear logs** button removes the per-run directories and leaves these three
files alone:
clearing out old reports is not a reason to lose the record of how they were
produced.

## Reading a line

```
2026-10-17 13:43:50 [INFO] [pid=14] [run=ab12cd34ef56 url=example.com] engine: Task started.
```

* `pid` — which worker.
  There are four by default, and they log into the same file,
  so lines from different runs interleave.
* `run=` — the run id, also returned to the browser when the run starts.
* `url=` — present on every line that belongs to a particular check.

Anything without `run=` is not about a check: startup, configuration,
the version check, the server's own lifecycle.

**One run, in order:**

```bash
grep "run=ab12cd34ef56" logs/engine.log
```

**One mirror across all runs of a day:**

```bash
grep "url=example.com" logs/engine.log
```

## When something looks wrong

### The run did not start at all

The browser shows a red line instead of results.
Three possible reasons, and the log names which one.

```bash
grep -E "rejected|alive-check soax" logs/engine.log | tail -20
```

* `[alive-check soax] dead: 1/3 targets ok (need 2) via az over http` —
  the proxy service itself did not answer.
  The lines above it say what happened to each target.
  Nothing was checked and no traffic was spent on mirrors.
* `Run rejected: Configuration is incomplete: ...` —
  the package password is missing from `.env`.
* `Run rejected: Country is required.` / `No URLs provided.` / `invalid network` —
  the form was incomplete.
  These never reach the proxy.

### Every mirror came back unavailable

Almost always the proxy and not forty sites at once.
Look for the gateway's own verdict:

```bash
grep -E "gateway rejected|Preflight warning" logs/engine.log | tail
```

`Provider gateway rejected the request: AUTH_FAILED` and friends are quoted
straight from SOAX; the README lists what each code usually means.
A result class of `proxy_error` in the report says the same thing.

If instead you see `_measure_http failed:` with a timeout,
the requests did leave and nothing came back — a network problem on this side,
or the gateway accepting connections and going nowhere.

### One mirror failed while the rest are fine

That is a verdict about the mirror,
and the per-check log file in `logs/<date>/<run>/` has the detail:
the redirect chain, the timings, the exit node it was checked from.
`engine.log` adds only what the check itself logged:

```bash
grep "url=the-mirror.com" logs/engine.log | tail -20
```

A row that never reached the proxy at all says so:

```
Task rejected before the provider: Invalid country code: 'zzz' (expected 2 letters)
Task rejected before the provider: Invalid format (expected: url cc)
```

### A check succeeded but there is no screenshot

```bash
grep -E "Screenshot failed|semaphore|shim" logs/engine.log | tail -20
```

* `Screenshot failed for <url>: <error>` — Chromium's own complaint.
  A timeout here usually means the page never finished loading.
* `Acquiring screenshot semaphore...` with no `Semaphore acquired` for a long
  while —
  screenshots are serialised (one Chromium at a time by default) and this one is
  queued behind others.
* `Could not start the SOCKS5 shim (...); screenshot falls back to http` —
  the picture was taken over HTTP during a SOCKS5 run.
  The report says so too.
* At `DEBUG`,
  `Shim started: 127.0.0.1:43643 -> proxy.soax.com:1337` and the matching
  `Shim stopped` bracket each screenshot of a SOCKS5 run.

### The browser showed nothing, but the results are on disk

```bash
grep "No SSE queue" logs/engine.log | tail
```

```
[deadbeefdead] No SSE queue in this worker; closing the stream immediately.
```

The run was executing in one worker and the browser's event stream landed in another.
The run itself is unaffected — the logs and screenshots are written as usual.
Reloading the page after the run finishes shows nothing either,
because the stream is live-only; read the results from `logs/<date>/<run>/`.

### A worker died, or the container restarted

```bash
grep -E "SIGTERM|aborted|interrupted|Booting worker|Arbiter" logs/engine.log | tail -20
```

* `Worker interrupted (SIGINT/SIGQUIT), shutting down (pid=N)` —
  somebody stopped the container: `Ctrl+C`, `docker compose down`, `restart`.
  Expected.
* `Worker (pid:N) was sent SIGTERM!` —
  the worker did not stop in time and the arbiter killed it.
  The usual cause is stopping the container while a run with screenshots is in flight.
* `Worker aborted after 180s without a heartbeat` —
  the worker genuinely stopped responding; a stack dump for it goes to `faulthandler.log`.
* `Worker reloading: <file> modified` —
  a project file changed on the host and the server restarted the workers.
  Harmless at rest, fatal to a run in progress:
  the check being executed dies with an unrelated-looking error.
  Do not edit project files while a run is going.

### A setting I changed had no effect

Settings are read once, at startup.
After editing `app.yaml` — on the Settings page or by hand — restart the container.
What the running process actually loaded is at the top of the log:

```bash
grep -A11 "Configuration summary" logs/engine.log | tail -12
```

```
Configuration summary (version 2.0.1, pid 14):
  paths:       logs_dir=/logs data_dir=/data
  execution:   max_concurrency=4 timeout_sec=60
  screenshots: enabled_default=False max_workers=1 timeout_sec=30 wait_after_load_sec=20
  soax:        proxy.soax.com:1337 session_scope=task
  transport:   default=socks5 (empty means it must be chosen per run)
  soax rules:  on_error=retry retries=3 bind_node=False preflight=True
  credentials: package_password=AB...YZ package_id=01...ef
  alive-check: enabled=True targets=3 required_ok=2 timeout_sec=10
  dns:         use_ipv6=False (SOAX does not route IPv6)
  logging:     level=INFO
IPv6 disabled (dns.use_ipv6=false): AAAA records are dropped from every resolve in this process.
```

This is also the answer to "which version and which settings was this report
produced with", days later.

Anything the configuration could not use is reported right after the summary:

```bash
grep "Configuration:" logs/engine.log | sort -u
```

```
Configuration: app.yaml:30: duplicate key 'session_scope' (first set on line 29); keeping the value from line 29
Configuration: Ignoring soax.on_error='nonsense': expected replace|retry|fail. Using 'retry'.
```

The same list appears as a notice at the top of every page.

## Markers worth knowing

| In the log | Meaning |
|---|---|
| `Configuration summary` | the settings this worker started with |
| `Configuration:` | a setting that could not be used, and what replaced it |
| `[alive-check soax]` | the pre-run proxy check: one line per target, then the verdict |
| `Task started.` | a check began; the context says which |
| `Task rejected before the provider` | an input row that never reached the proxy |
| `Preflight warning` | the session opened, but its exit node or geo looks wrong |
| `gateway rejected the request` | SOAX refused, with its own error code |
| `_measure_http failed` | the request left and failed; the text is the reason |
| `Screenshot failed` | Chromium could not produce the picture |
| `No SSE queue in this worker` | live updates unavailable; the run is unaffected |
| `was sent SIGTERM!` | a worker was killed rather than exiting on its own |

## Turning up the detail

`LOG_LEVEL=DEBUG` in `.env` (then restart) adds, per check:
the session being built and its rule string, the proxy mapping,
the shim's lifetime and the individual connections the gateway refused during a
page render, the watchdog being armed around a screenshot.

It is verbose — a single run with screenshots can produce a few hundred lines —
but it is the level at which "the screenshot looks wrong" becomes answerable.

`docker compose logs -f` shows the same stream as `engine.log`:
everything is written to both.
