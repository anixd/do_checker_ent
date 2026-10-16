from flask import Flask, url_for
from config.loader import ConfigStore
from engine.dns_policy import apply_ipv6_policy
from logging_.engine_logger import setup_loggers, get_engine_logger
from dotenv import load_dotenv
import os

load_dotenv()

def create_app() -> Flask:
    app = Flask(__name__)
    app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

    ConfigStore.init()
    setup_loggers(app)
    log = get_engine_logger()

    # Process-wide, so it has to happen before anything resolves a name: the
    # version checker below is the first thing that does. After setup_loggers,
    # because it reports which mode it put the process in.
    apply_ipv6_policy(ConfigStore.get().dns.use_ipv6)

    app.secret_key = os.environ.get('SECRET_KEY')
    if not app.secret_key:
        # Was a print(): invisible in the container log, where gunicorn's stdout
        # is the only thing anyone watches.
        log.warning("SECRET_KEY not set. Flash messages won't work; using a development key.")
        app.secret_key = 'dev-secret-key'

    from . import version_checker
    version_checker.start_background_checker()

    from .routes import bp as routes_bp
    app.register_blueprint(routes_bp)

    # SSE endpoint
    from .sse import bp as sse_bp
    app.register_blueprint(sse_bp)

    # static (for screenshots)
    from .static_server import static_bp
    app.register_blueprint(static_bp)

    @app.context_processor
    def inject_static_version():
        """
        Appends the file's mtime to static URLs, so a browser that cached the
        previous version of main.js or style.css picks up the new one.

        Not cosmetic: the SSE payload changed shape in this release, and an old
        cached main.js talking to a new server shows an empty result list and no
        error - exactly the kind of thing a support person reports as "the
        update broke the checker". mtime rather than the app version, because a
        css/js fix between releases has to reach the browser too.
        """
        def static_v(filename: str) -> str:
            url = url_for("static", filename=filename)
            try:
                mtime = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
            except OSError:
                return url
            return f"{url}?v={mtime}"

        return dict(static_v=static_v)

    @app.context_processor
    def inject_default_theme():
        """
        Injects the default_theme setting into all templates.
        """
        try:
            theme = ConfigStore.get().app.default_theme
        except Exception:
            log.warning("Could not read default_theme from config; falling back to 'auto'.",
                        exc_info=True)
            theme = "auto"  # Fallback

        return dict(default_theme=theme,
            update_available=version_checker.get_update_status()
        )

    log.info("Application initialized (pid %s).", os.getpid())
    return app
