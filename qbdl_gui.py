from flask_socketio import SocketIO, emit
from flask import Flask, render_template, request, session, jsonify
from cryptography.fernet import Fernet
import base64
import logging
import os
import json
import re
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
try:
    with open(os.path.join(os.path.dirname(__file__), 'build_sha.txt')) as _f:
        BUILD_SHA = _f.read().strip()[:7]
except Exception:
    BUILD_SHA = os.environ.get('BUILD_SHA', 'dev')[:7]
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


def _download_spotify_track(qobuz, item, download_location, quality):
    """Download a single Qobuz track into a Spotify playlist folder with metadata overrides."""
    from qobuz_dl.downloader import Download
    from pathvalidate import sanitize_filename

    url = item['url']
    playlist_dir = item['playlist_dir']
    playlist_name = item['playlist_name']
    track_number = item.get('track_number', 1)
    total_tracks = item.get('total_tracks', 0)

    # Track ID is the last path segment of the Qobuz URL
    track_id = url.rstrip('/').split('/')[-1]

    os.makedirs(playlist_dir, exist_ok=True)

    # Each track gets its own {Artist} - {Album} ({Year}) [{Quality}]/ subfolder
    # inside playlist_dir, with the Qobuz album cover downloaded automatically.
    dl = Download(
        client=qobuz.client,
        item_id=track_id,
        path=download_location,
        quality=quality,
        embed_art=True,
        downgrade_quality=True,
        playlist_dir=playlist_dir,
        playlist_track_number=track_number,
        metadata_overrides={
            'albumartist': 'Various Artists',
            'album': playlist_name,
            'tracknumber': str(track_number),
            'tracktotal': str(total_tracks) if total_tracks else '',
        },
    )
    dl.download_track()


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
            if next_item.get('is_spotify_track'):
                _download_spotify_track(qobuz, next_item, download_location, quality)
            else:
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
                           navidrome_password=settings.get('navidrome_password', ''),
                           build_sha=BUILD_SHA)


@app.route('/test_navidrome', methods=['POST'])
def test_navidrome():
    data = request.get_json(force=True, silent=False)
    url = data.get('navidrome_url', '').rstrip('/')
    user = data.get('navidrome_user', '')
    password = data.get('navidrome_password', '')

    if not url or not user:
        return jsonify(status='error', message='URL and username are required.')

    try:
        r = req_lib.post(
            f'{url}/auth/login',
            json={'username': user, 'password': password},
            timeout=10
        )
        if r.status_code == 200 and r.json().get('token'):
            return jsonify(status='ok', message='Connected to Navidrome successfully.')
        elif r.status_code == 401:
            return jsonify(status='error', message='Wrong username or password — please check your Navidrome credentials.')
        elif r.status_code == 404:
            return jsonify(status='error', message='Navidrome not found at that URL — check the address and port.')
        else:
            return jsonify(status='error', message=f'Unexpected response from Navidrome (HTTP {r.status_code}).')
    except req_lib.exceptions.ConnectionError:
        return jsonify(status='error', message='Could not reach Navidrome — check the URL and make sure it is running.')
    except req_lib.exceptions.Timeout:
        return jsonify(status='error', message='Connection timed out — Navidrome may be unreachable from this container.')
    except Exception as e:
        return jsonify(status='error', message=str(e))


@app.route('/save_settings', methods=['POST'])
def save_settings_route():
    data = request.get_json(force=True, silent=False)
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
    data = request.get_json(force=True, silent=False)
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


QOBUZ_WEB = "https://play.qobuz.com/"


def get_qobuz_client():
    settings = load_settings()
    email = settings.get('email', '')
    password = settings.get('password', '')
    download_location = settings.get('download_location', '/downloads')
    quality = int(settings.get('quality', 7))
    if not email or not password:
        return None, 'Qobuz credentials not configured in settings.'
    try:
        from qobuz_dl import QobuzDL
        qobuz = QobuzDL(directory=download_location, quality=quality)
        qobuz.get_tokens()
        qobuz.initialize_client(email, password, qobuz.app_id, qobuz.secrets)
        return qobuz, None
    except Exception as e:
        return None, str(e)


@app.route('/qobuz_search')
def qobuz_search_route():
    query = request.args.get('q', '').strip()
    limit = int(request.args.get('limit', 10))
    if not query:
        return jsonify(error='No query provided'), 400
    qobuz, err = get_qobuz_client()
    if err:
        return jsonify(error=err), 400
    try:
        results = qobuz.client.search_albums(query, limit)
        albums = results.get('albums', {}).get('items', [])
        items = []
        for a in albums:
            released_at = a.get('released_at') or a.get('release_date_original', '')
            if isinstance(released_at, int):
                from datetime import datetime
                year = str(datetime.utcfromtimestamp(released_at).year)
            else:
                year = str(released_at)[:4] if released_at else ''
            items.append({
                'id': str(a.get('id', '')),
                'title': a.get('title', ''),
                'artist': (a.get('artist') or {}).get('name', ''),
                'url': f"{QOBUZ_WEB}album/{a.get('id', '')}",
                'track_count': a.get('tracks_count') or 0,
                'hires': bool(a.get('hires_streamable')),
                'year': year,
            })
        return jsonify(results=items)
    except Exception as e:
        logging.error(f"Qobuz search error: {e}")
        return jsonify(error=str(e)), 500


@app.route('/resolve_album_urls', methods=['POST'])
def resolve_album_urls():
    data = request.get_json(force=True, silent=False)
    albums = data.get('albums', [])
    if not albums:
        return jsonify(error='No albums provided'), 400
    qobuz, err = get_qobuz_client()
    if err:
        return jsonify(error=err), 400
    resolved = []
    for a in albums:
        query = f"{a.get('artist', '')} {a.get('title', '')}".strip()
        try:
            results = qobuz.client.search_albums(query, 1)
            items = results.get('albums', {}).get('items', [])
            resolved.append(f"{QOBUZ_WEB}album/{items[0]['id']}" if items else None)
        except Exception as e:
            logging.warning(f"Could not resolve URL for '{query}': {e}")
            resolved.append(None)
    return jsonify(urls=resolved)


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


def _search_cover_image(playlist_name):
    """Search DuckDuckGo Images for a square cover image for the playlist.
    Returns a base64 data URI string, or empty string on failure."""
    try:
        from duckduckgo_search import DDGS
        query = f'{playlist_name} music playlist cover art'
        with DDGS() as ddgs:
            results = list(ddgs.images(query, max_results=15))
        for r in results:
            w, h = r.get('width', 0), r.get('height', 0)
            # Must be square-ish (ratio 0.9–1.1) and at least 300px
            if w >= 300 and h >= 300 and 0.9 <= w / h <= 1.1:
                try:
                    img_r = req_lib.get(r['image'], timeout=15, stream=True)
                    img_r.raise_for_status()
                    content_type = img_r.headers.get('content-type', 'image/jpeg').split(';')[0]
                    if not content_type.startswith('image/'):
                        continue
                    img_bytes = img_r.content
                    b64 = base64.b64encode(img_bytes).decode()
                    logging.info(f"Cover image found for '{playlist_name}': {r['image']}")
                    return f'data:{content_type};base64,{b64}'
                except Exception:
                    continue
    except Exception as e:
        logging.warning(f"Cover image search failed for '{playlist_name}': {e}")
    return ''


@app.route('/search_qobuz_tracks', methods=['POST'])
def search_qobuz_tracks():
    data = request.get_json(force=True, silent=False)
    tracks = data.get('tracks', [])
    if not tracks:
        return jsonify(error='No tracks provided'), 400

    qobuz, err = get_qobuz_client()
    if err:
        return jsonify(error=err), 400

    results = []
    for t in tracks:
        query = f"{t.get('artist', '')} {t.get('name', '')}".strip()
        try:
            r = qobuz.client.search_tracks(query, 1)
            items = r.get('tracks', {}).get('items', [])
            if items:
                track = items[0]
                results.append({
                    'found': True,
                    'qobuz_url': f"{QOBUZ_WEB}track/{track['id']}",
                    'qobuz_title': track.get('title', ''),
                    'qobuz_artist': (track.get('performer') or {}).get('name', ''),
                })
            else:
                results.append({'found': False})
        except Exception as e:
            logging.warning(f"Qobuz track search failed for '{query}': {e}")
            results.append({'found': False})

    return jsonify(results=results)


@app.route('/import_spotify_csv', methods=['POST'])
def import_spotify_csv():
    """Accept a pre-parsed CSV track list from the browser (e.g. Exportify export).
    No Spotify API calls needed — the browser already parsed the file."""
    data = request.get_json(force=True, silent=False)
    playlist_name = data.get('playlist_name', 'Imported Playlist').strip() or 'Imported Playlist'
    needs_cover  = data.get('needs_cover', False)   # True when user didn't upload an image
    raw_tracks   = data.get('tracks', [])

    if not raw_tracks:
        return jsonify(error='No tracks found in CSV'), 400

    tracks = [
        {
            'id': t.get('id', i),
            'name': t.get('name', '').strip(),
            'artist': t.get('artist', '').strip(),
            'album': t.get('album', '').strip(),
        }
        for i, t in enumerate(raw_tracks)
        if t.get('name', '').strip()
    ]

    # Only run image search when the browser has no local cover to use
    cover_url = _search_cover_image(playlist_name) if needs_cover else ''

    return jsonify(
        name=playlist_name,
        cover_url=cover_url,
        tracks=tracks,
        total=len(tracks)
    )


@app.route('/add_spotify_to_queue', methods=['POST'])
def add_spotify_to_queue():
    global download_running
    data = request.get_json(force=True, silent=False)
    settings = load_settings()

    playlist_name = data.get('playlist_name', 'Spotify Playlist')
    cover_url = data.get('cover_url', '')
    tracks = data.get('tracks', [])
    download_location = data.get('download_location') or settings.get('download_location', '/downloads')
    email = data.get('email') or settings.get('email', '')
    password = data.get('password') or settings.get('password', '')
    quality = int(data.get('quality') or settings.get('quality', 7))

    from pathvalidate import sanitize_filename
    playlist_dir = os.path.join(
        download_location, 'Various Artists', sanitize_filename(playlist_name)
    )
    total_tracks = len(tracks)

    with queue_lock:
        for i, t in enumerate(tracks, 1):
            download_queue.append({
                'id': str(uuid.uuid4()),
                'url': t['qobuz_url'],
                'label': f"{t.get('artist', '')} — {t.get('name', t['qobuz_url'])}",
                'status': 'queued',
                'position': None,
                'is_spotify_track': True,
                'playlist_name': playlist_name,
                'cover_url': cover_url,
                'playlist_dir': playlist_dir,
                'track_number': i,
                'total_tracks': total_tracks,
            })
        _recalc_positions()

    emit_queue_state()

    if not download_running:
        download_running = True
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


if __name__ == "__main__":
    socketio.run(app)
