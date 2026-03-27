from flask_socketio import SocketIO, emit
from flask import Flask, render_template, request, session, jsonify
from cryptography.fernet import Fernet
import logging
import os
import json
import threading
import uuid

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(24)
encryption_key = os.environ.get('ENCRYPTION_KEY') or Fernet.generate_key()
fernet = Fernet(encryption_key)

SETTINGS_FILE = os.environ.get('SETTINGS_FILE', '/config/settings.json')

# In-memory queue: list of dicts {id, url, status, position}
download_queue = []
queue_lock = threading.Lock()
download_running = False


def encrypt_password(password):
    return fernet.encrypt(password.encode()).decode()


def decrypt_password(encrypted_password):
    try:
        return fernet.decrypt(encrypted_password.encode()).decode()
    except Exception:
        return ''


def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                data = json.load(f)
                if 'password' in data and data['password']:
                    data['password'] = decrypt_password(data['password'])
                return data
    except Exception as e:
        logging.warning(f"Could not load settings: {e}")
    return {}


def save_settings(email, password, download_location, quality):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        data = {
            'email': email,
            'password': encrypt_password(password) if password else '',
            'download_location': download_location,
            'quality': quality,
        }
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(data, f)
        logging.info("Settings saved.")
    except Exception as e:
        logging.error(f"Could not save settings: {e}")


def emit_queue_state():
    with queue_lock:
        socketio.emit('queue_update', {'queue': list(download_queue)})


def _recalc_positions():
    """Must be called under queue_lock."""
    pos = 1
    for item in download_queue:
        if item['status'] == 'queued':
            item['position'] = pos
            pos += 1
        elif item['status'] != 'downloading':
            item['position'] = None


def run_queue(email, password, download_location, quality):
    global download_running
    from qobuz_dl import QobuzDL

    try:
        qobuz = QobuzDL(directory=download_location, quality=quality)
        qobuz.get_tokens()
        qobuz.initialize_client(email, password, qobuz.app_id, qobuz.secrets)
    except Exception as e:
        logging.error(f"Failed to initialize Qobuz client: {e}")
        with queue_lock:
            for item in download_queue:
                if item['status'] in ('queued', 'downloading'):
                    item['status'] = 'failed'
                    item['position'] = None
        emit_queue_state()
        download_running = False
        return

    while True:
        next_item = None
        with queue_lock:
            for item in download_queue:
                if item['status'] == 'queued':
                    next_item = item
                    break

        if next_item is None:
            break

        with queue_lock:
            next_item['status'] = 'downloading'
            _recalc_positions()
        emit_queue_state()

        try:
            qobuz.handle_url(next_item['url'])
            with queue_lock:
                next_item['status'] = 'downloaded'
                next_item['position'] = None
        except Exception as e:
            logging.error(f"Download failed for {next_item['url']}: {e}")
            with queue_lock:
                next_item['status'] = 'failed'
                next_item['position'] = None

        with queue_lock:
            _recalc_positions()
        emit_queue_state()

    download_running = False
    emit_queue_state()


@app.route('/')
def index():
    settings = load_settings()
    return render_template('index.html',
                           email=settings.get('email', ''),
                           password=settings.get('password', ''),
                           download_location=settings.get('download_location', '/downloads'),
                           quality=settings.get('quality', 7))


@app.route('/save_settings', methods=['POST'])
def save_settings_route():
    data = request.get_json()
    save_settings(
        data.get('email', ''),
        data.get('password', ''),
        data.get('download_location', ''),
        data.get('quality', 7),
    )
    return jsonify(status='ok')


@app.route('/add_urls', methods=['POST'])
def add_urls():
    global download_running
    data = request.get_json()
    email = data.get('email', '')
    password = data.get('password', '')
    download_location = data.get('download_location', '/downloads')
    quality = int(data.get('quality', 7))
    urls = [u.strip() for u in data.get('urls', []) if u.strip()]

    if not urls:
        return jsonify(status='error', message='No URLs provided'), 400

    with queue_lock:
        for url in urls:
            download_queue.append({
                'id': str(uuid.uuid4()),
                'url': url,
                'status': 'queued',
                'position': None,
            })
        _recalc_positions()

    emit_queue_state()

    if not download_running:
        download_running = True
        t = threading.Thread(
            target=run_queue,
            args=(email, password, download_location, quality),
            daemon=True
        )
        t.start()

    return jsonify(status='ok')


@app.route('/queue_state')
def queue_state_route():
    with queue_lock:
        return jsonify(queue=list(download_queue))


@app.route('/clear_finished', methods=['POST'])
def clear_finished():
    with queue_lock:
        to_remove = [i for i, item in enumerate(download_queue)
                     if item['status'] in ('downloaded', 'failed')]
        for i in reversed(to_remove):
            download_queue.pop(i)
        _recalc_positions()
    emit_queue_state()
    return jsonify(status='ok')


if __name__ == "__main__":
    socketio.run(app)
