from app import create_app
from app.cli import register_commands
import os

app = create_app()
app.secret_key = 'super-secret-benchmark-key'
register_commands(app)


if __name__ == '__main__':
    # Debug reloader spawns a second process and locks SQLite; off by default for systemd installs.
    debug = os.environ.get('BENCHVIZ_DEBUG', '').lower() in ('1', 'true', 'yes')
    app.run(debug=debug, use_reloader=debug, host='0.0.0.0', port=8765)
