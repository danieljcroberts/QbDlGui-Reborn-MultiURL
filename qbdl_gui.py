from flask_socketio import SocketIO, emit
from flask import Flask, render_template, request, session, jsonify
from cryptography.fernet import Fernet
import logging
import os
import json
import threading
import uuid
import requests as req_lib
import time

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(24)
encryption_key = os.environ.get('ENCRYPTION_KEY') or Fernet.generate_key()
fernet = Fernet(encryption_key)

SETTINGS_FILE = os.environ.get('SETTINGS_FILE', '/config/settings.json')
MB_UA = {'User-Agent': 'QobuzDlGui/2.0 (danieljcroberts@gmail.com)'}

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
                if 'navidrome_password' in data and data['navidrome_password']:
                    data['navidrome_password'] = decrypt_password(data['navidrome_password'])
                return data
    except Exception as e:
        logging.warning(f"Could not load settings: {e}")
    return {}


def save_settings(email, password, download_location, quality,
                  navidrome_url='', navidrome_user='', navidrome_password=''):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        data = {
            'email': email,
            'password': encrypt_password(password) if password else '',
            'download_location': download_location,
            'quality': quality,
            'navidrome_url': navidrome_url,
            'navidrome_user': navidrome_user,
            'navidrome_password': encrypt_password(navidrome_password) if navidrome_password else '',
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


def trigger_navidrome_scan(navidrome_url, navidrome_user, navidrome_password):
    """Authenticate with Navidrome and trigger a library scan."""
    if not navidrome_url or not navidrome_user:
        return
    try:
        base = navidrome_url.rstrip('/')
        # Navidrome uses Subsonic-compatible auth; get a session token first
        auth_resp = req_lib.post(
            f'{base}/auth/login',
            json={'username': navidrome_user, 'password': navidrome_password},
            timeout=10
        )
        auth_resp.raise_for_status()
        token = auth_resp.json().get('token')
        if not token:
            logging.warning("Navidrome scan: no token returned from auth.")
            return
        scan_resp = req_lib.post(
            f'{base}/api/scanner/trigger',
            headers={'x-nd-authorization': f'Bearer {token}'},
            timeout=10
        )
        scan_resp.raise_for_status()
        logging.info("Navidrome library scan triggered successfully.")
    except Exception as e:
        logging.warning(f"Navidrome scan trigger failed: {e}")


def run_queue(email, password, download_location, quality,
              navidrome_url='', navidrome_user='', navidrome_password=''):
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

    any_downloaded = False

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
            any_downloaded = True
        except Exception as e:
            logging.error(f"Download failed for {next_item['url']}: {e}")
            with queue_lock:
                next_item['status'] = 'failed'
                next_item['position'] = None

        with queue_lock:
            _recalc_positions()
        emit_queue_state()

    if any_downloaded:
        trigger_navidrome_scan(navidrome_url, navidrome_user, navidrome_password)

    download_running = False
    emit_queue_state()


@app.route('/')
def index():
    settings = load_settings()
    return render_template('index.html',
                           email=settings.get('email', ''),
                           password=settings.get('password', ''),
                           download_location=settings.get('download_location', '/downloads'),
                           quality=settings.get('quality', 7),
                           navidrome_url=settings.get('navidrome_url', ''),
                           navidrome_user=settings.get('navidrome_user', ''),
                           navidrome_password=settings.get('navidrome_password', ''))


@app.route('/save_settings', methods=['POST'])
def save_settings_route():
    data = request.get_json()
    save_settings(
        data.get('email', ''),
        data.get('password', ''),
        data.get('download_location', ''),
        data.get('quality', 7),
        data.get('navidrome_url', ''),
        data.get('navidrome_user', ''),
        data.get('navidrome_password', ''),
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
        settings = load_settings()
        t = threading.Thread(
            target=run_queue,
            args=(email, password, download_location, quality,
                  settings.get('navidrome_url', ''),
                  settings.get('navidrome_user', ''),
                  settings.get('navidrome_password', '')),
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


@app.route('/artist_search')
def artist_search():
    """Search MusicBrainz for an artist and return their release groups with metadata."""
    artist_name = request.args.get('q', '').strip()
    if not artist_name:
        return jsonify(error='No artist name provided'), 400

    try:
        # Step 1: find the artist MBID
        r = req_lib.get(
            'https://musicbrainz.org/ws/2/artist/',
            params={'query': f'artist:{artist_name}', 'fmt': 'json', 'limit': 5},
            headers=MB_UA,
            timeout=10
        )
        r.raise_for_status()
        artists = r.json().get('artists', [])
        if not artists:
            return jsonify(error='Artist not found'), 404

        # Return top 5 artist matches for disambiguation
        artist_matches = [
            {
                'id': a['id'],
                'name': a['name'] or '',
                'disambiguation': a.get('disambiguation') or '',
                'country': a.get('country') or '',
                'type': a.get('type') or '',
            }
            for a in artists
        ]
        return jsonify(artists=artist_matches)

    except Exception as e:
        logging.error(f"Artist search error: {e}")
        return jsonify(error=str(e)), 500


@app.route('/artist_releases')
def artist_releases():
    """Fetch all release groups for a given MusicBrainz artist MBID."""
    mbid = request.args.get('mbid', '').strip()
    if not mbid:
        return jsonify(error='No MBID provided'), 400

    try:
        all_rgs = []
        offset, total = 0, 9999

        while offset < total:
            for attempt in range(3):
                try:
                    r = req_lib.get(
                        'https://musicbrainz.org/ws/2/release-group',
                        params={
                            'artist': mbid,
                            'fmt': 'json',
                            'limit': 100,
                            'offset': offset,
                        },
                        headers=MB_UA,
                        timeout=15
                    )
                    if r.status_code == 503 or r.status_code == 429:
                        time.sleep(2 ** attempt)
                        continue
                    r.raise_for_status()
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
            data = r.json()
            total = data.get('release-group-count', 0)
            rgs = data.get('release-groups', [])
            if not rgs:
                break
            all_rgs.extend(rgs)
            offset += 100
            if offset < total:
                time.sleep(1.0)

        releases = []
        for rg in all_rgs:
            # Build a clean type label
            primary = rg.get('primary-type', 'Other')
            secondary = rg.get('secondary-types', [])
            if secondary:
                type_label = f"{primary} + {', '.join(secondary)}"
            else:
                type_label = primary

            date = rg.get('first-release-date', '')
            year = date[:4] if date else ''

            releases.append({
                'id': rg['id'],
                'title': rg['title'],
                'type': type_label,
                'primary_type': primary,
                'secondary_types': secondary,
                'date': date,
                'year': year,
                'track_count': None,
                'release_count': rg.get('release-count', 0),
            })

        # Sort by date
        releases.sort(key=lambda x: x['date'] or '9999')

        return jsonify(releases=releases, total=len(releases))

    except Exception as e:
        logging.error(f"Artist releases error: {e}")
        return jsonify(error=str(e)), 500


if __name__ == "__main__":
    socketio.run(app)
