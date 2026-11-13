import os

from flask import Flask

from . import db


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-me")
    db.init_db()

    from .filters import timeago, duration
    app.jinja_env.filters["timeago"] = timeago
    app.jinja_env.filters["duration"] = duration

    @app.context_processor
    def _inject_auto_replace_status():
        # Powers the Auto-Replace Cap widget in the sidebar (base.html) on
        # every page, not just Settings - a context processor instead of
        # threading it through every route's render_template() call.
        from . import actions
        return {"auto_replace": actions.auto_replace_status()}

    from .webhooks import bp as webhooks_bp
    from .issues_routes import bp as issues_bp
    from .settings_routes import bp as settings_bp
    from .queue_routes import bp as queue_bp

    app.register_blueprint(webhooks_bp)
    app.register_blueprint(issues_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(queue_bp)

    from .scheduler import start_scheduler
    start_scheduler()

    from .worker import start_worker
    start_worker()

    return app
