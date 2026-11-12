import os

from flask import Flask

from . import db


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-me")
    db.init_db()

    from .webhooks import bp as webhooks_bp
    from .issues_routes import bp as issues_bp
    from .settings_routes import bp as settings_bp
    from .backlog_routes import bp as backlog_bp

    app.register_blueprint(webhooks_bp)
    app.register_blueprint(issues_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(backlog_bp)

    from .scheduler import start_scheduler
    start_scheduler()

    from .backlog import start_backlog_scanner
    start_backlog_scanner()

    return app
