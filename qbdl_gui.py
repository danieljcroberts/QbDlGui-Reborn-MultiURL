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

# Persist the encryption key across container restarts.
# Priority: ENCRYPTION_KEY env var → /config/encryption_key.bin → generate & save new one.
_KEY_FILE = os.environ.get('ENCRYPTION_KEY_FILE', '/config/encryption_key.bin')
if os.environ.get('ENCRYPTION_KEY'):
    encryption_key = os.environ['ENCRYPTION_KEY'].encode() if isinstance(os.environ['ENCRYPTION_KEY'], str) else os.environ['ENCRYPTION_KEY']
elif os.path.isfile(_KEY_FILE):
    with open(_KEY_FILE, 'rb') as _kf:
        encryption_key = _kf.read().strip()
else:
    encryption_key = Fernet.generate_key()
    try:
        os.makedirs(os.path.dirname(_KEY_FILE), exist_ok=True)
        with open(_KEY_FILE, 'wb') as _kf:
            _kf.write(encryption_key)
        logging.info(f'Generated and saved new encryption key to {_KEY_FILE}')
    except Exception as _ke:
        logging.warning(f'Could not save encryption key to {_KEY_FILE}: {_ke}')

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
        custom_cover_path=item.get('cover_path') or None,
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
                'cover_url': f"https://coverartarchive.org/release-group/{rg['id']}/front-250",
            })

        # Sort by release_count DESC (popularity proxy), then date DESC (newest first)
        releases.sort(key=lambda x: (-x['release_count'], -(int(x['year']) if x['year'] else 0)))

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


@app.route('/search_qobuz_albums_batch', methods=['POST'])
def search_qobuz_albums_batch():
    """Batch-search Qobuz for albums by artist + title. Used by album-type chart previews."""
    data = request.get_json(force=True, silent=False)
    albums = data.get('albums', [])
    if not albums:
        return jsonify(error='No albums provided'), 400

    qobuz, err = get_qobuz_client()
    if err:
        return jsonify(error=err), 400

    results = []
    for alb in albums:
        query = f"{alb.get('artist', '')} {alb.get('name', '')}".strip()
        try:
            r = qobuz.client.search_albums(query, 1)
            items = r.get('albums', {}).get('items', [])
            if items:
                album = items[0]
                results.append({
                    'found': True,
                    'qobuz_url': f"{QOBUZ_WEB}album/{album['id']}",
                    'qobuz_title': album.get('title', ''),
                    'qobuz_artist': (album.get('artist') or {}).get('name', ''),
                    'tracks_count': album.get('tracks_count') or None,
                })
            else:
                results.append({'found': False, 'qobuz_url': None, 'qobuz_title': None, 'qobuz_artist': None})
        except Exception as e:
            logging.warning(f"Qobuz album search failed for '{query}': {e}")
            results.append({'found': False, 'qobuz_url': None, 'qobuz_title': None, 'qobuz_artist': None})

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


@app.route('/upload_cover', methods=['POST'])
def upload_cover():
    """Accept a playlist cover image as a binary file upload (avoids base64 JSON bloat).
    Saves it to /tmp so the downloader can copy it into each track subfolder."""
    f = request.files.get('cover')
    if not f:
        return jsonify(error='No file provided'), 400
    cover_dir = '/tmp/playlist_covers'
    os.makedirs(cover_dir, exist_ok=True)
    cover_path = os.path.join(cover_dir, f'{uuid.uuid4()}.jpg')
    f.save(cover_path)
    return jsonify(cover_path=cover_path)


@app.route('/upload_chart_cover', methods=['POST'])
def upload_chart_cover():
    """Upload a custom chart cover to persistent /config storage so it survives
    container restarts and is available for future scheduled runs."""
    f = request.files.get('cover')
    if not f:
        return jsonify(error='No file provided'), 400
    cover_dir = '/config/chart_covers'
    os.makedirs(cover_dir, exist_ok=True)
    cover_path = os.path.join(cover_dir, f'custom_{uuid.uuid4()}.jpg')
    f.save(cover_path)
    return jsonify(cover_path=cover_path)


@app.route('/add_spotify_to_queue', methods=['POST'])
def add_spotify_to_queue():
    global download_running
    data = request.get_json(force=True, silent=False)
    settings = load_settings()

    playlist_name = data.get('playlist_name', 'Spotify Playlist')
    cover_path = data.get('cover_path', '')   # server-side file path from /upload_cover
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
                'cover_path': cover_path,
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


# ─── Auto Charts ─────────────────────────────────────────────────────────────
from apscheduler.schedulers.background import BackgroundScheduler

CHART_SCHEDULES_FILE = os.environ.get('CHART_SCHEDULES_FILE', '/config/chart_schedules.json')

CHART_SOURCES = {
    'apple': {
        'name': 'Apple Music',
        'needs_key': False,
        'has_country': True,
        'charts': [
            {'id': 'most-played',  'name': 'Most Played',   'type': 'track'},
            {'id': 'top-songs',    'name': 'Top Songs',     'type': 'track'},
            {'id': 'new-releases', 'name': 'New Releases',  'type': 'album'},
        ],
        'countries': [
            {'id': 'us', 'name': 'United States'},
            {'id': 'gb', 'name': 'United Kingdom'},
            {'id': 'au', 'name': 'Australia'},
            {'id': 'ca', 'name': 'Canada'},
            {'id': 'de', 'name': 'Germany'},
            {'id': 'fr', 'name': 'France'},
            {'id': 'jp', 'name': 'Japan'},
        ],
    },
    'deezer': {
        'name': 'Deezer',
        'needs_key': False,
        'has_country': False,
        'charts': [
            {'id': 'global', 'name': 'Global Top',         'type': 'track'},
            {'id': '132',    'name': 'Pop',                'type': 'track'},
            {'id': '116',    'name': 'Rap / Hip-Hop',      'type': 'track'},
            {'id': '152',    'name': 'Rock',               'type': 'track'},
            {'id': '113',    'name': 'Dance / Electronic', 'type': 'track'},
            {'id': '165',    'name': 'R&B / Soul',         'type': 'track'},
            {'id': '85',     'name': 'Alternative',        'type': 'track'},
            {'id': '129',    'name': 'Jazz',               'type': 'track'},
            {'id': '84',     'name': 'Country',            'type': 'track'},
            {'id': '464',    'name': 'Metal',              'type': 'track'},
        ],
    },
    'lastfm': {
        'name': 'Last.fm',
        'needs_key': True,
        'has_country': False,
        'charts': [
            {'id': 'global',        'name': 'Global Top',  'type': 'track'},
            {'id': 'tag:pop',       'name': 'Pop',         'type': 'track'},
            {'id': 'tag:hip-hop',   'name': 'Hip-Hop',     'type': 'track'},
            {'id': 'tag:rock',      'name': 'Rock',        'type': 'track'},
            {'id': 'tag:electronic','name': 'Electronic',  'type': 'track'},
            {'id': 'tag:r-n-b',     'name': 'R&B',         'type': 'track'},
            {'id': 'tag:country',   'name': 'Country',     'type': 'track'},
            {'id': 'tag:jazz',      'name': 'Jazz',        'type': 'track'},
            {'id': 'tag:metal',     'name': 'Metal',       'type': 'track'},
            {'id': 'tag:indie',     'name': 'Indie',       'type': 'track'},
            {'id': 'tag:classical', 'name': 'Classical',   'type': 'track'},
        ],
    },
    'pitchfork': {
        'name': 'Pitchfork',
        'needs_key': False,
        'has_country': False,
        'charts': [
            {'id': 'best', 'name': 'Best New Music', 'type': 'album'},
        ],
    },
}


def _fetch_apple_chart(chart_id, country, limit):
    url = f'https://rss.applemarketingtools.com/api/v2/{country}/music/{chart_id}/{limit}/songs.json'
    r = req_lib.get(url, timeout=15)
    r.raise_for_status()
    return [{'name': i.get('name', ''), 'artist': i.get('artistName', '')}
            for i in r.json().get('feed', {}).get('results', [])]


def _fetch_deezer_chart(chart_id, limit):
    path = '0' if chart_id == 'global' else chart_id
    r = req_lib.get(f'https://api.deezer.com/chart/{path}/tracks?limit={limit}', timeout=15)
    r.raise_for_status()
    return [{'name': i.get('title', ''), 'artist': (i.get('artist') or {}).get('name', '')}
            for i in r.json().get('data', [])]


def _fetch_lastfm_chart(chart_id, api_key, limit):
    if not api_key:
        raise ValueError('Last.fm API key is required')
    params = {'format': 'json', 'api_key': api_key, 'limit': limit}
    if chart_id.startswith('tag:'):
        params.update({'method': 'tag.getTopTracks', 'tag': chart_id[4:]})
    else:
        params['method'] = 'chart.getTopTracks'
    r = req_lib.get('https://ws.audioscrobbler.com/2.0/', params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if 'error' in data:
        raise ValueError(f"Last.fm: {data.get('message', data['error'])}")
    raw = (data.get('tracks') or data.get('toptracks') or {}).get('track', [])
    return [
        {
            'name': t.get('name', ''),
            'artist': t['artist'].get('name', '') if isinstance(t.get('artist'), dict) else t.get('artist', ''),
        }
        for t in raw
    ]


def _fetch_apple_new_releases(country, limit):
    url = f'https://rss.applemarketingtools.com/api/v2/{country}/music/new-releases/{limit}/albums.json'
    r = req_lib.get(url, timeout=15)
    r.raise_for_status()
    results = r.json().get('feed', {}).get('results', [])
    return [{'name': i.get('name', ''), 'artist': i.get('artistName', '')} for i in results]


def _fetch_pitchfork_best_new_music(limit):
    import xml.etree.ElementTree as ET
    r = req_lib.get('https://pitchfork.com/rss/reviews/best/', timeout=15,
                    headers={'User-Agent': 'Mozilla/5.0 (compatible; QobuzDlGui/2.0)'})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = root.findall('.//item')
    results = []
    for item in items[:limit]:
        raw_title = (item.findtext('title') or '').strip()
        # Pitchfork titles: "Artist: Album Title" or "Artist / Album"
        if ': ' in raw_title:
            artist, name = raw_title.split(': ', 1)
        elif ' / ' in raw_title:
            artist, name = raw_title.split(' / ', 1)
        else:
            # Fallback: title is just the album, no artist split possible
            artist, name = '', raw_title
        results.append({'name': name.strip(), 'artist': artist.strip()})
    return results


def _chart_item_type(sched):
    """Return 'album' if this schedule/preview is album-type, else 'track'."""
    src = sched.get('source', '')
    chart_id = sched.get('chart', '')
    src_cfg = CHART_SOURCES.get(src, {})
    for c in src_cfg.get('charts', []):
        if c['id'] == chart_id:
            return c.get('type', 'track')
    # Whole-source fallback (e.g. pitchfork always album)
    if src == 'pitchfork':
        return 'album'
    return 'track'


def _get_chart_display_names(sched):
    """Return (source_name, chart_label, country_name) for a schedule dict."""
    src_cfg = CHART_SOURCES.get(sched.get('source', ''), {})
    source_name = src_cfg.get('name', sched.get('source', ''))
    chart_label = next(
        (c['name'] for c in src_cfg.get('charts', []) if c['id'] == sched.get('chart', '')),
        sched.get('chart', ''),
    )
    country_name = ''
    if src_cfg.get('has_country'):
        country_name = next(
            (c['name'] for c in src_cfg.get('countries', []) if c['id'] == sched.get('country', '')),
            sched.get('country', ''),
        )
    return source_name, chart_label, country_name


def _generate_chart_cover(source_name, chart_label, country_name):
    """Generate (or return cached) a 600×600 cover image for a chart.
    Cached by config hash in /config/chart_covers/ so it never changes
    across re-runs of the same schedule."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import hashlib

        cache_key = hashlib.md5(
            f'{source_name}|{chart_label}|{country_name}'.encode()
        ).hexdigest()[:12]
        cover_dir = '/config/chart_covers'
        os.makedirs(cover_dir, exist_ok=True)
        cover_path = os.path.join(cover_dir, f'{cache_key}.jpg')

        if os.path.isfile(cover_path):
            return cover_path

        # Colour scheme per source
        _src = source_name.lower()
        if 'apple' in _src:
            bg, accent = '#0d0000', '#fc3c44'
        elif 'deezer' in _src:
            bg, accent = '#0d0013', '#ef5466'
        elif 'last' in _src:
            bg, accent = '#0d0000', '#d51007'
        else:
            bg, accent = '#0d111a', '#5bc8f5'

        SIZE = 600
        img = Image.new('RGB', (SIZE, SIZE), bg)
        draw = ImageDraw.Draw(img)

        # Try to load DejaVu Bold; fall back to Pillow default
        def _font(px):
            for path in (
                '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
                '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf',
                '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            ):
                if os.path.isfile(path):
                    try:
                        return ImageFont.truetype(path, px)
                    except Exception:
                        pass
            try:
                return ImageFont.load_default(size=px)
            except TypeError:
                return ImageFont.load_default()

        # Accent bars top & bottom
        draw.rectangle([(0, 0), (SIZE, 10)], fill=accent)
        draw.rectangle([(0, SIZE - 10), (SIZE, SIZE)], fill=accent)

        cx = SIZE // 2

        # Source name
        draw.text((cx, 90), source_name.upper(), font=_font(54), fill=accent, anchor='mm')

        # Thin divider
        draw.rectangle([(50, 132), (SIZE - 50, 135)], fill=accent)

        # Chart label (may be long — split at ' / ' or wrap at ~22 chars)
        label_lines = []
        words = chart_label.split()
        line = ''
        for w in words:
            test = (line + ' ' + w).strip()
            if len(test) > 18 and line:
                label_lines.append(line)
                line = w
            else:
                line = test
        if line:
            label_lines.append(line)

        y = 230 - (len(label_lines) - 1) * 28
        for ln in label_lines:
            draw.text((cx, y), ln, font=_font(48), fill='#ffffff', anchor='mm')
            y += 58

        # Country / genre
        if country_name:
            draw.text((cx, y + 20), country_name, font=_font(34), fill='#bbbbbb', anchor='mm')

        # Watermark
        draw.text((cx, SIZE - 32), 'AUTO CHARTS', font=_font(20), fill='#444444', anchor='mm')

        img.save(cover_path, 'JPEG', quality=92)
        logging.info(f'Chart cover generated: {cover_path}')
        return cover_path

    except Exception as e:
        logging.warning(f'Could not generate chart cover: {e}')
        return None


def _fetch_chart_tracks(sched):
    src, chart_id, limit = sched['source'], sched['chart'], int(sched.get('limit', 50))
    if src == 'apple':
        if chart_id == 'new-releases':
            return _fetch_apple_new_releases(sched.get('country', 'us'), limit)
        return _fetch_apple_chart(chart_id, sched.get('country', 'us'), limit)
    if src == 'deezer':
        return _fetch_deezer_chart(chart_id, limit)
    if src == 'lastfm':
        return _fetch_lastfm_chart(chart_id, sched.get('api_key', ''), limit)
    if src == 'pitchfork':
        return _fetch_pitchfork_best_new_music(limit)
    raise ValueError(f'Unknown chart source: {src}')


def _load_chart_schedules():
    try:
        if os.path.exists(CHART_SCHEDULES_FILE):
            with open(CHART_SCHEDULES_FILE) as f:
                return json.load(f)
    except Exception as e:
        logging.warning(f'Could not load chart schedules: {e}')
    return []


def _save_chart_schedules(schedules):
    try:
        os.makedirs(os.path.dirname(CHART_SCHEDULES_FILE), exist_ok=True)
        with open(CHART_SCHEDULES_FILE, 'w') as f:
            json.dump(schedules, f, indent=2)
    except Exception as e:
        logging.error(f'Could not save chart schedules: {e}')


def _run_chart_job(schedule_id):
    global download_running
    schedules = _load_chart_schedules()
    sched = next((s for s in schedules if s['id'] == schedule_id), None)
    if not sched:
        logging.warning(f'Chart schedule {schedule_id} not found')
        return

    settings = load_settings()
    email = settings.get('email', '')
    password = settings.get('password', '')
    download_location = settings.get('download_location', '/downloads')
    quality = int(settings.get('quality', 7))
    if not email or not password:
        logging.error(f'Chart job {schedule_id}: Qobuz credentials not configured')
        return

    from datetime import datetime
    from pathvalidate import sanitize_filename
    import shutil
    chart_name = sched['name']
    is_album = _chart_item_type(sched) == 'album'

    # Fetch item list from source
    try:
        raw_items = _fetch_chart_tracks(sched)
    except Exception as e:
        logging.error(f'Chart job {schedule_id}: fetch failed: {e}')
        return

    if not raw_items:
        logging.warning(f'Chart job {schedule_id}: no items returned')
        return

    # Match on Qobuz
    qobuz, err = get_qobuz_client()
    if err:
        logging.error(f'Chart job {schedule_id}: Qobuz error: {err}')
        return

    # ── Album-type chart (e.g. Apple New Releases, Pitchfork Best New Music) ──
    if is_album:
        matched_urls = []
        for t in raw_items:
            query = f"{t['artist']} {t['name']}".strip()
            try:
                res = qobuz.client.search_albums(query, 1)
                items = res.get('albums', {}).get('items', [])
                if items:
                    matched_urls.append(f"{QOBUZ_WEB}album/{items[0]['id']}")
            except Exception as e:
                logging.warning(f'Chart job: Qobuz album search failed for "{query}": {e}')

        if not matched_urls:
            logging.warning(f'Chart job {schedule_id}: no albums matched on Qobuz')
            return

        # Persist last_run
        for s in schedules:
            if s['id'] == schedule_id:
                s['last_run'] = datetime.now().isoformat()
        _save_chart_schedules(schedules)

        with queue_lock:
            for url in matched_urls:
                download_queue.append({
                    'id': str(uuid.uuid4()),
                    'url': url,
                    'label': f'[Chart] {chart_name}',
                    'status': 'queued',
                    'position': None,
                })
            _recalc_positions()

        emit_queue_state()

        if not download_running:
            download_running = True
            threading.Thread(
                target=run_queue,
                args=(email, password, download_location, quality,
                      settings.get('navidrome_url', ''),
                      settings.get('navidrome_user', ''),
                      settings.get('navidrome_password', '')),
                daemon=True,
            ).start()

        logging.info(f'Chart job {schedule_id}: queued {len(matched_urls)} albums for "{chart_name}"')
        return

    # ── Track-type chart (playlist download) ─────────────────────────────────
    matched = []
    for i, t in enumerate(raw_items):
        query = f"{t['artist']} {t['name']}".strip()
        try:
            res = qobuz.client.search_tracks(query, 1)
            items = res.get('tracks', {}).get('items', [])
            if items:
                matched.append({
                    'qobuz_url': f"{QOBUZ_WEB}track/{items[0]['id']}",
                    'name': t['name'],
                    'artist': t['artist'],
                    'position': i + 1,
                })
        except Exception as e:
            logging.warning(f'Chart job: Qobuz search failed for "{query}": {e}')

    if not matched:
        logging.warning(f'Chart job {schedule_id}: no tracks matched on Qobuz')
        return

    playlist_dir = os.path.join(download_location, 'Charts', sanitize_filename(chart_name))

    # Delete the existing folder so the playlist is fully replaced each run
    if os.path.exists(playlist_dir):
        try:
            shutil.rmtree(playlist_dir)
            logging.info(f'Deleted existing chart folder for refresh: {playlist_dir}')
        except Exception as e:
            logging.warning(f'Could not delete {playlist_dir}: {e}')

    # Use user-uploaded custom cover if set, otherwise auto-generate
    custom = sched.get('custom_cover_path')
    if custom and os.path.isfile(custom):
        cover_path = custom
    else:
        source_name, chart_label, country_name = _get_chart_display_names(sched)
        cover_path = _generate_chart_cover(source_name, chart_label, country_name)

    # Persist last_run
    for s in schedules:
        if s['id'] == schedule_id:
            s['last_run'] = datetime.now().isoformat()
    _save_chart_schedules(schedules)

    total = len(matched)
    with queue_lock:
        for t in matched:
            download_queue.append({
                'id': str(uuid.uuid4()),
                'url': t['qobuz_url'],
                'label': f"[Chart] {t['artist']} — {t['name']}",
                'status': 'queued',
                'position': None,
                'is_spotify_track': True,
                'playlist_name': chart_name,
                'cover_path': cover_path or '',
                'playlist_dir': playlist_dir,
                'track_number': t['position'],
                'total_tracks': total,
            })
        _recalc_positions()

    emit_queue_state()

    if not download_running:
        download_running = True
        threading.Thread(
            target=run_queue,
            args=(email, password, download_location, quality,
                  settings.get('navidrome_url', ''),
                  settings.get('navidrome_user', ''),
                  settings.get('navidrome_password', '')),
            daemon=True,
        ).start()

    logging.info(f'Chart job {schedule_id}: queued {total} tracks for "{chart_name}"')


# ── APScheduler: load saved schedules on startup ─────────────────────────────
_scheduler = BackgroundScheduler(daemon=True)


def _init_scheduler():
    for sched in _load_chart_schedules():
        if sched.get('enabled', True):
            try:
                _scheduler.add_job(
                    _run_chart_job, 'cron',
                    id=sched['id'], args=[sched['id']],
                    day_of_week=sched.get('day_of_week', 'mon'),
                    hour=sched.get('hour', 3),
                    minute=0,
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
            except Exception as e:
                logging.warning(f'Could not register chart job {sched["id"]}: {e}')
    _scheduler.start()


_init_scheduler()


@app.route('/chart/sources')
def chart_sources_route():
    return jsonify(sources=CHART_SOURCES)


@app.route('/chart/preview', methods=['POST'])
def chart_preview():
    """Fetch raw track/album list from chart source (Qobuz matching done client-side)."""
    data = request.get_json(force=True, silent=False)
    sched = {
        'source': data.get('source', 'apple'),
        'chart': data.get('chart', 'most-played'),
        'limit': min(int(data.get('limit', 50)), 100),
        'country': data.get('country', 'us'),
        'api_key': data.get('api_key', ''),
    }
    try:
        tracks = _fetch_chart_tracks(sched)
    except Exception as e:
        return jsonify(error=str(e)), 400
    item_type = _chart_item_type(sched)
    return jsonify(
        type=item_type,
        tracks=[{'name': t['name'], 'artist': t['artist']} for t in tracks],
    )


@app.route('/chart/schedule/list')
def chart_schedule_list():
    return jsonify(schedules=_load_chart_schedules())


@app.route('/chart/schedule/add', methods=['POST'])
def chart_schedule_add():
    data = request.get_json(force=True, silent=False)
    schedules = _load_chart_schedules()
    sid = str(uuid.uuid4())
    sched = {
        'id': sid,
        'name': (data.get('name', 'Chart') or 'Chart').strip(),
        'source': data.get('source', 'apple'),
        'chart': data.get('chart', 'most-played'),
        'country': data.get('country', 'us'),
        'limit': min(int(data.get('limit', 50)), 100),
        'api_key': data.get('api_key', ''),
        'day_of_week': data.get('day_of_week', 'mon'),
        'hour': int(data.get('hour', 3)),
        'enabled': True,
        'custom_cover_path': data.get('custom_cover_path', '') or '',
        'last_run': None,
        'last_folder': None,
    }
    schedules.append(sched)
    _save_chart_schedules(schedules)
    try:
        _scheduler.add_job(
            _run_chart_job, 'cron',
            id=sid, args=[sid],
            day_of_week=sched['day_of_week'],
            hour=sched['hour'], minute=0,
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )
    except Exception as e:
        logging.warning(f'Could not register APScheduler job {sid}: {e}')
    return jsonify(status='ok', schedule=sched)


@app.route('/chart/schedule/<sid>', methods=['DELETE'])
def chart_schedule_delete(sid):
    schedules = [s for s in _load_chart_schedules() if s['id'] != sid]
    _save_chart_schedules(schedules)
    try:
        _scheduler.remove_job(sid)
    except Exception:
        pass
    return jsonify(status='ok')


@app.route('/chart/schedule/<sid>/run', methods=['POST'])
def chart_schedule_run(sid):
    threading.Thread(target=_run_chart_job, args=(sid,), daemon=True).start()
    return jsonify(status='ok')


@app.route('/chart/add_to_queue', methods=['POST'])
def chart_add_to_queue():
    global download_running
    data = request.get_json(force=True, silent=False)
    settings = load_settings()
    from pathvalidate import sanitize_filename
    from datetime import datetime

    chart_name = data.get('chart_name', 'Chart')
    tracks = data.get('tracks', [])
    download_location = data.get('download_location') or settings.get('download_location', '/downloads')
    email = data.get('email') or settings.get('email', '')
    password = data.get('password') or settings.get('password', '')
    quality = int(data.get('quality') or settings.get('quality', 7))

    # Use user-uploaded cover if provided, otherwise auto-generate
    custom_cover = data.get('custom_cover_path', '') or ''
    if custom_cover and os.path.isfile(custom_cover):
        cover_path = custom_cover
    else:
        sched_info = {
            'source': data.get('source', ''),
            'chart': data.get('chart', ''),
            'country': data.get('country', ''),
        }
        source_name, chart_label, country_name = _get_chart_display_names(sched_info)
        cover_path = _generate_chart_cover(source_name, chart_label, country_name)

    playlist_dir = os.path.join(download_location, 'Charts', sanitize_filename(chart_name))

    # Delete existing folder so each manual download is also a clean refresh
    if os.path.exists(playlist_dir):
        try:
            import shutil
            shutil.rmtree(playlist_dir)
            logging.info(f'Deleted existing chart folder for refresh: {playlist_dir}')
        except Exception as e:
            logging.warning(f'Could not delete {playlist_dir}: {e}')

    total = len(tracks)
    with queue_lock:
        for i, t in enumerate(tracks, 1):
            download_queue.append({
                'id': str(uuid.uuid4()),
                'url': t['qobuz_url'],
                'label': f"[Chart] {t.get('artist', '')} — {t.get('name', t['qobuz_url'])}",
                'status': 'queued',
                'position': None,
                'is_spotify_track': True,
                'playlist_name': chart_name,
                'cover_path': cover_path or '',
                'playlist_dir': playlist_dir,
                'track_number': i,
                'total_tracks': total,
            })
        _recalc_positions()

    emit_queue_state()

    if not download_running:
        download_running = True
        threading.Thread(
            target=run_queue,
            args=(email, password, download_location, quality,
                  settings.get('navidrome_url', ''),
                  settings.get('navidrome_user', ''),
                  settings.get('navidrome_password', '')),
            daemon=True,
        ).start()

    return jsonify(status='ok')


# ── Discogs routes ────────────────────────────────────────────────────────────

@app.route('/discogs/fetch', methods=['POST'])
def discogs_fetch():
    import re
    import xml.etree.ElementTree as ET  # noqa — unused here but keeps import local

    data = request.get_json(force=True, silent=False)
    url = (data.get('url') or '').strip()

    # Accept release and master URLs
    m = re.search(r'discogs\.com/(?:[^/]+/)?(?P<type>release|master)/(?P<id>\d+)', url)
    if not m:
        return jsonify(error='Could not parse a Discogs release or master URL. '
                       'Expected: https://www.discogs.com/release/12345 or …/master/12345'), 400

    release_id = m.group('id')
    is_master = m.group('type') == 'master'
    api_url = f'https://api.discogs.com/{"masters" if is_master else "releases"}/{release_id}'

    headers = {'User-Agent': 'QobuzDlGui/2.0 +https://github.com/danjcroberts/QbDlGui-Reborn'}
    try:
        r = req_lib.get(api_url, timeout=15, headers=headers)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return jsonify(error=f'Discogs API error: {e}'), 400

    title = d.get('title', 'Unknown Album')
    year = str(d.get('year', '')) if d.get('year') else ''

    # Album artist
    artists = d.get('artists', [])
    def _clean_artist(name):
        return re.sub(r'\s*\(\d+\)$', '', (name or '').rstrip('*').strip())
    album_artist = ' / '.join(_clean_artist(a.get('name', '')) for a in artists) if artists else 'Various Artists'
    if album_artist.lower() in ('various', 'various artists', 'v/a'):
        album_artist = 'Various Artists'

    # Tracklist — skip heading/index entries
    raw_tracklist = d.get('tracklist', [])
    tracklist = []
    for t in raw_tracklist:
        if t.get('type_', 'track') != 'track':
            continue
        t_artists = t.get('artists', [])
        if t_artists:
            t_artist = ' / '.join(_clean_artist(a.get('name', '')) for a in t_artists)
        else:
            t_artist = album_artist
        tracklist.append({
            'position': t.get('position', ''),
            'name': (t.get('title') or '').strip(),
            'artist': t_artist,
            'duration': t.get('duration', ''),
        })

    # Download primary cover image to /tmp/discogs_covers/{id}.jpg
    cover_path = ''
    images = d.get('images', [])
    cover_url = next(
        (img.get('uri', '') or img.get('uri150', '') for img in images if img.get('type') == 'primary'),
        (images[0].get('uri', '') or images[0].get('uri150', '')) if images else '',
    )
    if cover_url:
        try:
            cover_dir = '/tmp/discogs_covers'
            os.makedirs(cover_dir, exist_ok=True)
            cover_file = os.path.join(cover_dir, f'{release_id}.jpg')
            if not os.path.isfile(cover_file):
                img_r = req_lib.get(cover_url, timeout=20, headers=headers)
                img_r.raise_for_status()
                with open(cover_file, 'wb') as f:
                    f.write(img_r.content)
            cover_path = cover_file
        except Exception as e:
            logging.warning(f'Could not download Discogs cover: {e}')

    return jsonify(
        title=title,
        year=year,
        album_artist=album_artist,
        tracklist=tracklist,
        cover_path=cover_path,
        release_id=release_id,
    )


@app.route('/discogs/cover/<release_id>')
def discogs_cover(release_id):
    """Serve the locally cached Discogs cover image."""
    import re
    from flask import send_file
    if not re.fullmatch(r'\d+', release_id):
        return ('', 404)
    cover_file = f'/tmp/discogs_covers/{release_id}.jpg'
    if not os.path.isfile(cover_file):
        return ('', 404)
    return send_file(cover_file, mimetype='image/jpeg')


_AUDIO_EXTENSIONS = {'.flac', '.mp3', '.m4a', '.ogg', '.wav', '.aiff', '.alac', '.opus'}


def _count_audio_files(folder):
    """Recursively count audio files under folder."""
    if not os.path.isdir(folder):
        return 0
    count = 0
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if os.path.splitext(f)[1].lower() in _AUDIO_EXTENSIONS:
                count += 1
    return count


@app.route('/discogs/add_to_queue', methods=['POST'])
def discogs_add_to_queue():
    global download_running
    data = request.get_json(force=True, silent=False)
    settings = load_settings()
    from pathvalidate import sanitize_filename

    album_name = (data.get('album_name') or 'Discogs Album').strip()
    tracks = data.get('tracks', [])
    cover_path = data.get('cover_path', '') or ''
    force = bool(data.get('force', False))
    download_location = settings.get('download_location', '/downloads')
    email = settings.get('email', '')
    password = settings.get('password', '')
    quality = int(settings.get('quality', 7))

    if not tracks:
        return jsonify(status='error', message='No tracks provided'), 400

    playlist_dir = os.path.join(download_location, 'Discogs', sanitize_filename(album_name))
    total = len(tracks)

    # Check how many audio files already exist in the folder
    already_present = _count_audio_files(playlist_dir)

    if already_present >= total and not force:
        # All tracks appear to be present — tell the frontend so it can confirm
        return jsonify(
            status='already_complete',
            already_present=already_present,
            total=total,
            message=(
                f'All {total} tracks appear to already be downloaded to '
                f'"{playlist_dir}". Re-download anyway?'
            ),
        )

    # Queue all selected tracks — the per-file check in the downloader will
    # automatically skip any file that already exists on disk.
    with queue_lock:
        for i, t in enumerate(tracks, 1):
            download_queue.append({
                'id': str(uuid.uuid4()),
                'url': t['qobuz_url'],
                'label': f"[Discogs] {t.get('artist', '')} — {t.get('name', t['qobuz_url'])}",
                'status': 'queued',
                'position': None,
                'is_spotify_track': True,
                'playlist_name': album_name,
                'cover_path': cover_path,
                'playlist_dir': playlist_dir,
                'track_number': i,
                'total_tracks': total,
            })
        _recalc_positions()

    emit_queue_state()

    if not download_running:
        download_running = True
        threading.Thread(
            target=run_queue,
            args=(email, password, download_location, quality,
                  settings.get('navidrome_url', ''),
                  settings.get('navidrome_user', ''),
                  settings.get('navidrome_password', '')),
            daemon=True,
        ).start()

    missing = max(0, total - already_present)
    return jsonify(
        status='ok',
        queued=total,
        already_present=already_present,
        missing=missing,
    )


if __name__ == "__main__":
    socketio.run(app)
