# QbDlGui-Reborn (danieljcroberts fork)

A web GUI for downloading lossless music from Qobuz, forked from [lilkidsuave/QbDlGui-Reborn](https://github.com/lilkidsuave/QbDlGui-Reborn).

## Enhancements in this fork

- **Multi-URL queue**: paste as many Qobuz URLs as you like (one per line) — they are processed sequentially
- **Live queue status table**: shows each URL with its status (queued with position, downloading, done, failed), updated in real-time via WebSockets
- **Persistent settings**: clicking *Save Settings* writes email, password, download location and quality to `/config/settings.json` inside the container — they survive restarts without needing env vars or session cookies. The URL field is never saved.
- **Clear Finished button**: removes completed/failed items from the queue display
- **Larger URL input**: textarea instead of a single-line field

## Docker

```yaml
services:
  qbdlgui:
    image: ghcr.io/danieljcroberts/qbdlgui-reborn-multiurl:latest
    ports:
      - 5000:5000
    volumes:
      - /your/music/path:/downloads
      - qbdlgui-config:/config
    restart: unless-stopped

volumes:
  qbdlgui-config:
```

Access at `http://localhost:5000`

## Usage

1. Fill in your Qobuz credentials, download path and quality, then click **Save Settings**
2. Paste one or more Qobuz URLs into the URL box (one per line)
3. Click **Add to Queue & Download** — downloads start immediately and the queue table updates live
4. Use **Clear Finished** to tidy up completed entries

## Credits

Original project by [@vitiko98](https://github.com/vitiko98), [@lilkidsuave](https://github.com/lilkidsuave), and Gyarbij.  
This fork maintained by [@danieljcroberts](https://github.com/danieljcroberts).
