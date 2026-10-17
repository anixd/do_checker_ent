#!/bin/sh
set -e

mkdir -p "${LOG_DIR:-/logs}"
mkdir -p "${DATA_DIR:-/data}"
mkdir -p "${DATA_DIR:-/data}/config"
mkdir -p "${DATA_DIR:-/data}/catalog"

# Both heredocs below are quoted (<<'YAML'), so no shell expansion happens
# inside them. Any ${VAR} written here would land in the file verbatim -
# hence the literal 8888 instead of ${APP_PORT}. The real port comes from
# APP_PORT in .env, which ConfigStore applies on top of this file.

APP_YAML="${DATA_DIR:-/data}/config/app.yaml"
if [ ! -f "$APP_YAML" ]; then
  cat > "$APP_YAML" <<'YAML'
# Settings are read once, when the application starts. After editing this file -
# here or through the Settings page - restart the container:
#   docker compose restart
#
# A value this file cannot provide is replaced with a built-in default and
# reported on every page, so a typo here will not keep the application down.
app:
  host: 127.0.0.1
  port: 8888
  # themes: 'auto', 'light' or 'dark'
  default_theme: auto
logging:
  # Log levels: DEBUG, INFO, WARNING, ERROR
  level: "INFO"
paths:
  logs_dir: /logs
  data_dir: /data
execution:
  # How many checks run at once. Sessions open at any moment roughly equal this
  # number, whatever the size of the run.
  max_concurrency: 3
  timeout_sec: 60
screenshots:
  # Whether the Screenshot box starts ticked
  enabled_default: false
  max_workers: 1
  width: 1366
  height: 768
  timeout_sec: 30
  wait_after_load_sec: 15
soax:
  host: proxy.soax.com
  port: 1337
  # Default transport the forms preselect: http | socks5.
  # Leave it empty (or remove the line) to have no default at all - then the
  # transport has to be chosen explicitly for every run.
  # A socks5 run takes its screenshot through a local SOCKS5 shim, so the
  # picture comes from the same exit node as the check itself.
  transport: http
  # task = one SOAX session per checked site (independent samples)
  # geo  = one session per country, so its results share an exit node
  session_scope: task
  # What the gateway does with a failing exit node:
  # replace swaps in another one, retry retries on the same one up to N times,
  # fail returns the error as it is.
  on_error: retry
  on_error_retries: 3  # 1..10
  # incompatible with on_error: replace
  bind_node: false
  # Resolve the exit IP and the node's real location before the check itself.
  # Cheap, and it is what turns a gateway refusal into a readable message.
  preflight: true
  # SOAX_PACKAGE_ID and SOAX_PACKAGE_PASSWORD live in .env only: this file is
  # editable through the Settings page and rendered into the browser.
alive_check:
  # Before each run, fetch a few third-party sites through the gateway. When the
  # proxy service is down, a run otherwise reports every mirror as unavailable -
  # a false report that also costs the traffic of producing it.
  enabled: true
  # How many of the targets must answer for the run to start. Each target gets
  # its own session, and probing stops as soon as this many have replied.
  # Three unrelated operators, so "two of three failed" means the proxy and not
  # one site having a bad day. Each answers a few hundred bytes: this runs
  # before every check and the traffic is paid for. google.com is deliberately
  # absent - it answers proxy addresses with captchas and 429, which would read
  # as an outage on a healthy gateway.
  targets:
    - http://example.com
    - http://detectportal.firefox.com/success.txt
    - http://cloudflare.com/cdn-cgi/trace
  required_ok: 2
  timeout_sec: 10
dns:
  # false (default) drops AAAA records from every name this application resolves.
  # SOAX does not route IPv6, so an attempt to leave over it is a guaranteed
  # failure that looks like the checked mirror being down.
  use_ipv6: false
http_client:
  user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
  accept: "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
  accept_language: "en-US,en;q=0.5"
  # NOTE: an empty section MUST keep the curly braces: 'custom_headers: {}'
  # When adding headers, remove the braces and indent entries below:
  #   custom_headers:
  #     X-CF-Bypass: "MySecretToken123"
  #     Another-Header: "SomeValue"
  custom_headers: {}
YAML
fi

# Fallback catalog only. Where an existing installation is available, copy its
# data/catalog/soax_geo.json over instead - the country list is maintained by
# hand and cannot be rebuilt from the SOAX API on the enterprise plan.
SOAX_JSON="${DATA_DIR:-/data}/catalog/soax_geo.json"
if [ ! -f "$SOAX_JSON" ]; then
  cat > "$SOAX_JSON" <<'JSON'
{
  "version": 1,
  "generated_at": "2025-10-25T00:00:00Z",
  "countries": [
    {"code": "kz", "name": "Kazakhstan", "regions": [], "cities": [], "isps": []},
    {"code": "az", "name": "Azerbaijan", "regions": [], "cities": [], "isps": []},
    {"code": "in", "name": "India", "regions": [], "cities": [], "isps": []},
    {"code": "uz", "name": "Uzbekistan", "regions": [], "cities": [], "isps": []},
    {"code": "bd", "name": "Bangladesh", "regions": [], "cities": [], "isps": []},
    {"code": "cl", "name": "Chile", "regions": [], "cities": [], "isps": []},
    {"code": "ru", "name": "Russia", "regions": [], "cities": [], "isps": []},
    {"code": "tr", "name": "Turkey", "regions": [], "cities": [], "isps": []},
    {"code": "kg", "name": "Kyrgyzstan", "regions": [], "cities": [], "isps": []},
    {"code": "ca", "name": "Canada", "regions": [], "cities": [], "isps": []},
    {"code": "tj", "name": "Tajikistan", "regions": [], "cities": [], "isps": []},
    {"code": "tm", "name": "Turkmenistan", "regions": [], "cities": [], "isps": []},
    {"code": "de", "name": "Germany", "regions": [], "cities": [], "isps": []}
  ]
}
JSON
fi

exec "$@"
