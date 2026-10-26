#!/usr/bin/env python3
"""abs-stats: Audiobookshelf stats collector with a Tautulli-style web UI."""
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ABS_URL = os.environ["ABS_URL"].rstrip("/")
ABS_TOKEN = os.environ["ABS_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "/data/abs-stats.db")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))
ITEMS_INTERVAL = int(os.environ.get("ITEMS_INTERVAL", "1800"))
ACTIVE_WINDOW = int(os.environ.get("ACTIVE_WINDOW", "300"))
INTERVAL_CHOICES = (60, 120, 300, 900, 1800, 3600)
CHAPTER_FETCH_LIMIT = 40  # per sync, so the first backfill doesn't hammer the server
WEB_PORT = int(os.environ.get("WEB_PORT", "8484"))
STATS_USERNAME = os.environ.get("STATS_USERNAME", "abs-stats")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
NTFY_URL = os.environ.get("NTFY_URL", "").strip()
OVERLAP_MS = 6 * 3600 * 1000
RETRY_SECONDS = 60
COVER_MAX_AGE = 7 * 86400
UA = "abs-stats/1.2"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
COVER_DIR = os.path.join(os.path.dirname(DB_PATH), "covers")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("abs-stats")

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("TZ", "America/New_York"))
except Exception:
    log.warning("timezone data unavailable, using UTC")
    LOCAL_TZ = timezone.utc

SYNC_NOW = threading.Event()
STATUS = {"syncing": False, "last_error": None}

# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT, username TEXT,
  library_id TEXT, library_item_id TEXT, media_type TEXT,
  title TEXT, author TEXT, narrator TEXT, genres TEXT,
  duration REAL, time_listening REAL, start_position REAL, end_position REAL,
  play_method INTEGER, client TEXT, device TEXT,
  date TEXT, day_of_week TEXT,
  started_at INTEGER, updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_item ON sessions(library_item_id);

CREATE TABLE IF NOT EXISTS progress (
  user_id TEXT, username TEXT, library_item_id TEXT, episode_id TEXT,
  progress REAL, is_finished INTEGER,
  started_at INTEGER, finished_at INTEGER, last_update INTEGER,
  PRIMARY KEY (user_id, library_item_id, episode_id)
);

CREATE TABLE IF NOT EXISTS library_items (
  id TEXT PRIMARY KEY, library_id TEXT,
  title TEXT, author TEXT, duration REAL, added_at INTEGER
);

CREATE TABLE IF NOT EXISTS libraries (id TEXT PRIMARY KEY, name TEXT, media_type TEXT);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, username TEXT, type TEXT,
  is_active INTEGER, created_at INTEGER, last_seen INTEGER
);

CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS chapters (item_id TEXT PRIMARY KEY, data TEXT, item_updated_at INTEGER, fetched_at INTEGER);
"""

MIGRATIONS = {
    "sessions": {
        "ip": "TEXT", "os": "TEXT", "client_version": "TEXT", "device_name": "TEXT", "browser": "TEXT",
        "server_version": "TEXT",
    },
    "library_items": {
        "subtitle": "TEXT", "narrator": "TEXT", "series": "TEXT", "genres": "TEXT",
        "published_year": "TEXT", "media_type": "TEXT", "num_chapters": "INTEGER",
        "size": "INTEGER", "updated_at": "INTEGER",
    },
}


def _local(ms):
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000, LOCAL_TZ)
    except (TypeError, ValueError, OSError):
        return None


def _fmt(pattern):
    def fn(ms):
        d = _local(ms)
        return d.strftime(pattern) if d else None
    return fn


def _hour(ms):
    d = _local(ms)
    return d.hour if d else None


def _dow(ms):
    d = _local(ms)
    return d.weekday() if d else None


def connect():
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    for name, fn in (("local_dt", _fmt("%Y-%m-%d %H:%M")), ("local_date", _fmt("%Y-%m-%d")),
                     ("local_month", _fmt("%Y-%m")), ("local_dh", _fmt("%Y-%m-%d %H")), ("local_hour", _hour), ("local_dow", _dow)):
        db.create_function(name, 1, fn, deterministic=True)
    return db


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = connect()
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    db.commit()
    db.close()


def get_state(db, key, default=None):
    row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(db, key, value):
    db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, str(value)))


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- ABS sync

def api(path, **params):
    url = ABS_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ABS_TOKEN}", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


ITEM_COLS = ("id", "library_id", "title", "author", "duration", "added_at", "subtitle", "narrator",
             "series", "genres", "published_year", "media_type", "num_chapters", "size", "updated_at")


def sync_items(db, notify_new):
    known = {r[0] for r in db.execute("SELECT id FROM library_items")}
    seen_items, seen_libs, new = set(), set(), []
    libs = api("/api/libraries")
    libs = libs.get("libraries", []) if isinstance(libs, dict) else libs
    sql = f"INSERT OR REPLACE INTO library_items ({','.join(ITEM_COLS)}) VALUES ({','.join('?' * len(ITEM_COLS))})"
    for lib in libs:
        seen_libs.add(lib["id"])
        db.execute("INSERT OR REPLACE INTO libraries (id, name, media_type) VALUES (?,?,?)",
                   (lib["id"], lib.get("name"), lib.get("mediaType")))
        page = 0
        while True:
            data = api(f"/api/libraries/{lib['id']}/items", minified=1, limit=500, page=page)
            results = data.get("results", [])
            for it in results:
                media = it.get("media") or {}
                md = media.get("metadata") or {}
                row = (
                    it["id"], lib["id"], md.get("title"), md.get("authorName") or md.get("author"),
                    media.get("duration"), it.get("addedAt"), md.get("subtitle"), md.get("narratorName"),
                    md.get("seriesName"), json.dumps(md.get("genres") or []), md.get("publishedYear"),
                    it.get("mediaType"), media.get("numChapters"), it.get("size") or media.get("size"),
                    it.get("updatedAt"),
                )
                db.execute(sql, row)
                seen_items.add(it["id"])
                if notify_new and it["id"] not in known:
                    new.append((md.get("title"), md.get("authorName") or md.get("author")))
            page += 1
            if not results or page * 500 >= data.get("total", 0):
                break
    for gone in known - seen_items:
        db.execute("DELETE FROM library_items WHERE id = ?", (gone,))
    for (lib_id,) in db.execute("SELECT id FROM libraries").fetchall():
        if lib_id not in seen_libs:
            db.execute("DELETE FROM libraries WHERE id = ?", (lib_id,))
    log.info("items synced: %d total, %d new", len(seen_items), len(new))
    return new


SESSION_COLS = ("id", "user_id", "username", "library_id", "library_item_id", "media_type", "title", "author",
                "narrator", "genres", "duration", "time_listening", "start_position", "end_position", "play_method",
                "client", "device", "date", "day_of_week", "started_at", "updated_at",
                "ip", "os", "client_version", "device_name", "browser", "server_version")
SESSION_SQL = (f"INSERT OR REPLACE INTO sessions ({','.join(SESSION_COLS)}) "
               f"VALUES ({','.join('?' * len(SESSION_COLS))})")


def _join(*parts):
    return " ".join(p for p in parts if p) or None


def save_session(db, s):
    md = s.get("mediaMetadata") or {}
    user = s.get("user") or {}
    di = s.get("deviceInfo") or {}
    username = user.get("username")
    if not username and s.get("userId"):
        row = db.execute("SELECT username FROM users WHERE id = ?", (s["userId"],)).fetchone()
        username = row[0] if row else None
    db.execute(SESSION_SQL, (
        s["id"], s.get("userId"), username,
        s.get("libraryId"), s.get("libraryItemId"), s.get("mediaType"),
        s.get("displayTitle") or md.get("title"), s.get("displayAuthor"),
        ", ".join(md.get("narrators") or []), json.dumps(md.get("genres") or []),
        s.get("duration"), s.get("timeListening"), s.get("startTime"), s.get("currentTime"),
        s.get("playMethod"), di.get("clientName") or s.get("mediaPlayer"),
        di.get("model") or di.get("browserName") or di.get("osName"),
        s.get("date"), s.get("dayOfWeek"), s.get("startedAt"), s.get("updatedAt"),
        di.get("ipAddress"), _join(di.get("osName"), di.get("osVersion")), di.get("clientVersion"),
        di.get("deviceName") or _join(di.get("manufacturer"), di.get("model")),
        _join(di.get("browserName"), di.get("browserVersion")), s.get("serverVersion"),
    ))
    return s.get("updatedAt") or 0


def sync_sessions(db):
    cursor = int(get_state(db, "sessions_cursor", "0"))
    newest, page, count = cursor, 0, 0
    while True:
        data = api("/api/sessions", page=page, itemsPerPage=100, sort="updatedAt", desc=1)
        sessions = data.get("sessions", [])
        if not sessions:
            break
        for s in sessions:
            newest = max(newest, save_session(db, s))
            count += 1
        oldest = min(s.get("updatedAt") or 0 for s in sessions)
        page += 1
        if (cursor and oldest < cursor - OVERLAP_MS) or page >= data.get("numPages", 0):
            break
    set_state(db, "sessions_cursor", newest)
    log.info("sessions synced: %d processed", count)


def sync_open_sessions(db):
    """Sessions currently open in Audiobookshelf. Best effort: older servers may not expose this."""
    try:
        data = api("/api/sessions/open")
    except Exception as e:
        log.info("open sessions unavailable: %s", e)
        return
    ids = []
    for s in data.get("sessions", []):
        if s.get("id"):
            save_session(db, s)
            ids.append(s["id"])
    set_state(db, "open_sessions", json.dumps(ids))


def sync_users(db, notify_finished):
    finished = []
    for u in api("/api/users").get("users", []):
        if u.get("username") == STATS_USERNAME:
            continue
        full = api(f"/api/users/{u['id']}")
        db.execute(
            "INSERT OR REPLACE INTO users (id, username, type, is_active, created_at, last_seen) VALUES (?,?,?,?,?,?)",
            (u["id"], full.get("username"), full.get("type"), 1 if full.get("isActive", True) else 0,
             full.get("createdAt"), full.get("lastSeen")),
        )
        for p in full.get("mediaProgress", []):
            ep = p.get("episodeId") or ""
            prev = db.execute(
                "SELECT is_finished FROM progress WHERE user_id=? AND library_item_id=? AND episode_id=?",
                (u["id"], p.get("libraryItemId"), ep),
            ).fetchone()
            is_fin = 1 if p.get("isFinished") else 0
            if notify_finished and is_fin and (prev is None or not prev[0]):
                finished.append((full.get("username"), p.get("libraryItemId")))
            db.execute(
                "INSERT OR REPLACE INTO progress VALUES (?,?,?,?,?,?,?,?,?)",
                (u["id"], full.get("username"), p.get("libraryItemId"), ep, p.get("progress"),
                 is_fin, p.get("startedAt"), p.get("finishedAt"), p.get("lastUpdate")),
            )
    log.info("users synced: %d newly finished", len(finished))
    return finished


def post(url, body, headers):
    req = urllib.request.Request(url, data=body, method="POST", headers={"User-Agent": UA, **headers})
    urllib.request.urlopen(req, timeout=30).close()


def notify(lines):
    if not lines:
        return
    text = "\n".join(lines)
    if DISCORD_WEBHOOK_URL:
        try:
            post(DISCORD_WEBHOOK_URL, json.dumps({"content": text[:1900]}).encode(), {"Content-Type": "application/json"})
        except Exception as e:
            log.warning("discord notify failed: %s", e)
    if NTFY_URL:
        try:
            post(NTFY_URL, text.encode(), {"Title": "Audiobookshelf"})
        except Exception as e:
            log.warning("ntfy notify failed: %s", e)


def sync_chapters(db):
    """Cache chapter lists for books people have listened to. Refetched when the item changes."""
    todo = db.execute("""SELECT s.library_item_id, MAX(s.started_at) AS last, i.updated_at
        FROM sessions s LEFT JOIN library_items i ON i.id = s.library_item_id
        LEFT JOIN chapters c ON c.item_id = s.library_item_id
        WHERE COALESCE(s.media_type, 'book') = 'book' AND s.library_item_id IS NOT NULL
          AND (c.item_id IS NULL OR COALESCE(i.updated_at, 0) > COALESCE(c.item_updated_at, 0))
        GROUP BY s.library_item_id ORDER BY last DESC LIMIT ?""", (CHAPTER_FETCH_LIMIT,)).fetchall()
    for item_id, _, updated in todo:
        try:
            item = api(f"/api/items/{item_id}")
            chaps = [[c.get("start", 0), c.get("end", 0), c.get("title") or ""]
                     for c in ((item.get("media") or {}).get("chapters") or [])]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            chaps = []
        db.execute("INSERT OR REPLACE INTO chapters VALUES (?,?,?,?)", (item_id, json.dumps(chaps), updated or 0, now_ms()))
    if todo:
        log.info("chapters synced: %d titles", len(todo))


def chapter_at(chaps, pos):
    if not chaps or pos is None:
        return None
    for i, (start, end, title) in enumerate(chaps):
        if start <= pos < end or i == len(chaps) - 1:
            return {"index": i + 1, "count": len(chaps), "title": title,
                    "start": start, "end": end, "left": max(0, end - pos)}
    return None


def add_chapters(db, items, pos_key="end_position", item_key="item_id"):
    """Attach the chapter at each row's position, as row['chapter']."""
    ids = {r[item_key] for r in items if r.get(item_key)}
    cache = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for item_id, data in db.execute(f"SELECT item_id, data FROM chapters WHERE item_id IN ({marks})", list(ids)):
            try:
                cache[item_id] = json.loads(data)
            except ValueError:
                pass
    for r in items:
        r["chapter"] = chapter_at(cache.get(r.get(item_key)), r.get(pos_key))
    return items


def poll_interval(db):
    try:
        v = int(get_state(db, "poll_interval", str(POLL_INTERVAL)))
    except ValueError:
        v = POLL_INTERVAL
    return max(60, v)


def run_once(db):
    initialized = get_state(db, "initialized") == "1"
    started = now_ms()
    new_items = []
    if not initialized or started - int(get_state(db, "last_items_sync", "0")) >= ITEMS_INTERVAL * 1000 - 5000:
        new_items = sync_items(db, initialized)
        set_state(db, "last_items_sync", started)
    finished = sync_users(db, initialized)
    sync_sessions(db)
    sync_open_sessions(db)
    db.commit()
    try:
        sync_chapters(db)
    except Exception as e:
        log.warning("chapter sync failed: %s", e)
    set_state(db, "initialized", "1")
    set_state(db, "last_sync", started)
    db.commit()
    lines = []
    for username, item_id in finished:
        row = db.execute("SELECT title, author FROM library_items WHERE id=?", (item_id,)).fetchone()
        lines.append(f"\u2705 {username} finished " + (f"{row[0]} by {row[1]}" if row else item_id))
    for title, author in new_items:
        lines.append(f"\U0001F4DA New: {title} by {author}")
    notify(lines)


def poll_loop():
    db = connect()
    while True:
        STATUS["syncing"] = True
        try:
            run_once(db)
            STATUS["last_error"] = None
            wait = poll_interval(db)
        except Exception as e:
            log.exception("sync failed, retrying in %ds", RETRY_SECONDS)
            db.rollback()
            STATUS["last_error"] = f"{type(e).__name__}: {e}"
            wait = RETRY_SECONDS
        STATUS["syncing"] = False
        SYNC_NOW.wait(wait)
        SYNC_NOW.clear()


# ---------------------------------------------------------------- web API

class NotFound(Exception):
    pass


def rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args)]


def one(db, sql, args=()):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else None


def since(q):
    days = int(q.get("days", 30))
    return (now_ms() - days * 86400000 if days > 0 else 0), days


def api_status(db, q):
    return {"last_sync": int(get_state(db, "last_sync", "0")), "syncing": STATUS["syncing"],
            "last_error": STATUS["last_error"], "interval": poll_interval(db), "choices": INTERVAL_CHOICES,
            "active": len(activity_rows(db))}


def activity_window(db):
    """Readers count as active if Audiobookshelf heard from them in the last ACTIVE_WINDOW seconds
    (plus a minute for sync lag), measured from now so cards drop off on their own. With syncs slower
    than the window, the window stretches to one sync interval so readers don't vanish between syncs."""
    return now_ms() - (effective_window(db) + 60) * 1000


def effective_window(db):
    return max(ACTIVE_WINDOW, poll_interval(db))


def activity_rows(db):
    if not int(get_state(db, "last_sync", "0")):
        return []
    try:
        open_ids = set(json.loads(get_state(db, "open_sessions", "[]")))
    except ValueError:
        open_ids = set()
    since_ms = activity_window(db)
    sessions = rows(db, f"""SELECT s.*, COALESCE(s.username, u.username) AS uname
        FROM sessions s LEFT JOIN users u ON u.id = s.user_id
        WHERE s.updated_at >= ?
        ORDER BY s.started_at""", [since_ms])
    # One card per user and title: where this stretch started, total listened, latest connection.
    groups = {}
    for r in sessions:
        g = groups.get((r["user_id"], r["library_item_id"]))
        if g is None:
            groups[(r["user_id"], r["library_item_id"])] = g = {
                "user_id": r["user_id"], "username": r["uname"], "item_id": r["library_item_id"],
                "title": r["title"], "author": r["author"], "media_type": r["media_type"], "duration": r["duration"],
                "start_position": r["start_position"], "started_at": r["started_at"],
                "seconds": 0, "sessions": 0, "open": False,
            }
        g["seconds"] += r["time_listening"] or 0
        g["sessions"] += 1
        g["open"] = g["open"] or r["id"] in open_ids
        g.update(updated_at=r["updated_at"], client=r["client"], client_version=r["client_version"],
                 device=r["device_name"] or r["device"], os=r["os"], browser=r["browser"], ip=r["ip"],
                 play_method=r["play_method"], end_position=r["end_position"])
    return sorted(groups.values(), key=lambda g: (not g["open"], -(g["updated_at"] or 0)))


def api_activity(db, q):
    return {"since": activity_window(db), "last_sync": int(get_state(db, "last_sync", "0")),
            "interval": poll_interval(db), "window": effective_window(db),
            "rows": add_chapters(db, activity_rows(db))}


def api_summary(db, q):
    s, _ = since(q)
    r = one(db, """SELECT COALESCE(SUM(time_listening),0) AS seconds, COUNT(*) AS sessions,
                   COUNT(DISTINCT user_id) AS listeners, COUNT(DISTINCT library_item_id) AS titles
                   FROM sessions WHERE started_at >= ?""", (s,))
    r["finished"] = db.execute("SELECT COUNT(*) FROM progress WHERE is_finished=1 AND finished_at >= ?", (s,)).fetchone()[0]
    r["library"] = one(db, "SELECT COUNT(*) AS items, COALESCE(SUM(duration),0) AS duration, COALESCE(SUM(size),0) AS size FROM library_items")
    return r


GENRES_JOIN = "json_each(CASE WHEN json_valid(s.genres) THEN s.genres ELSE '[]' END) j"


def api_home(db, q):
    s, _ = since(q)
    title_sql = """SELECT library_item_id AS id, MAX(title) AS title, MAX(author) AS author,
                   SUM(time_listening) AS seconds, COUNT(*) AS sessions, COUNT(DISTINCT user_id) AS listeners
                   FROM sessions WHERE started_at >= ? GROUP BY library_item_id ORDER BY {} LIMIT 5"""
    return {
        "top_titles": rows(db, title_sql.format("seconds DESC"), (s,)),
        "popular": rows(db, title_sql.format("listeners DESC, seconds DESC"), (s,)),
        "top_authors": rows(db, """SELECT author AS name, SUM(time_listening) AS seconds,
                   COUNT(DISTINCT library_item_id) AS titles,
                   (SELECT s2.library_item_id FROM sessions s2 WHERE s2.author = s.author
                    GROUP BY s2.library_item_id ORDER BY SUM(s2.time_listening) DESC LIMIT 1) AS cover_id
                   FROM sessions s WHERE started_at >= ? AND COALESCE(author,'') != ''
                   GROUP BY author ORDER BY seconds DESC LIMIT 5""", (s,)),
        "top_users": rows(db, """SELECT user_id AS id, MAX(username) AS name, SUM(time_listening) AS seconds,
                   COUNT(*) AS sessions, COUNT(DISTINCT library_item_id) AS titles
                   FROM sessions WHERE started_at >= ? GROUP BY user_id ORDER BY seconds DESC LIMIT 5""", (s,)),
        "top_clients": rows(db, """SELECT COALESCE(NULLIF(client,''),'Unknown') AS name, SUM(time_listening) AS seconds,
                   COUNT(DISTINCT user_id) AS users FROM sessions WHERE started_at >= ?
                   GROUP BY name ORDER BY seconds DESC LIMIT 5""", (s,)),
        "top_genres": rows(db, f"""SELECT j.value AS name, SUM(s.time_listening) AS seconds,
                   COUNT(DISTINCT s.library_item_id) AS titles FROM sessions s, {GENRES_JOIN}
                   WHERE s.started_at >= ? GROUP BY j.value ORDER BY seconds DESC LIMIT 5""", (s,)),
        "recent_sessions": rows(db, """SELECT id, user_id, username, library_item_id AS item_id, title, author,
                   client, time_listening, end_position, duration, started_at
                   FROM sessions ORDER BY started_at DESC LIMIT 8"""),
        "recently_added": rows(db, """SELECT id, title, author, added_at FROM library_items
                   ORDER BY added_at DESC LIMIT 16"""),
        "recently_finished": rows(db, """SELECT p.user_id, p.username, p.library_item_id AS item_id,
                   COALESCE(i.title, (SELECT MAX(title) FROM sessions WHERE library_item_id = p.library_item_id)) AS title,
                   i.author, p.finished_at FROM progress p LEFT JOIN library_items i ON i.id = p.library_item_id
                   WHERE p.is_finished = 1 AND p.finished_at IS NOT NULL
                   ORDER BY p.finished_at DESC LIMIT 16"""),
    }


def api_history(db, q):
    s, _ = since(q)
    where, args = ["started_at >= ?"], [s]
    if q.get("user"):
        where.append("user_id = ?")
        args.append(q["user"])
    if q.get("item"):
        where.append("library_item_id = ?")
        args.append(q["item"])
    if q.get("search"):
        where.append("(title LIKE ? OR author LIKE ? OR username LIKE ? OR client LIKE ?)")
        args += [f"%{q['search']}%"] * 4
    w = " AND ".join(where)
    per = min(max(int(q.get("per", 50)), 1), 500)
    page = max(int(q.get("page", 1)), 1)
    total = db.execute(f"SELECT COUNT(*), COALESCE(SUM(time_listening),0) FROM sessions WHERE {w}", args).fetchone()
    data = rows(db, f"""SELECT id, user_id, username, library_item_id AS item_id, title, author, client, device,
                    play_method, time_listening, start_position, end_position, duration, started_at, updated_at
                    FROM sessions WHERE {w} ORDER BY started_at DESC LIMIT ? OFFSET ?""", args + [per, (page - 1) * per])
    return {"total": total[0], "seconds": total[1], "page": page, "per": per, "rows": add_chapters(db, data)}


def api_users(db, q):
    return rows(db, """SELECT u.id, u.username, u.type, u.is_active, u.last_seen, u.created_at,
        (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id) AS sessions,
        (SELECT COALESCE(SUM(time_listening),0) FROM sessions s WHERE s.user_id = u.id) AS seconds,
        (SELECT COUNT(*) FROM progress p WHERE p.user_id = u.id AND p.is_finished = 1) AS finished,
        (SELECT MAX(started_at) FROM sessions s WHERE s.user_id = u.id) AS last_played,
        (SELECT title FROM sessions s WHERE s.user_id = u.id ORDER BY started_at DESC LIMIT 1) AS last_title,
        (SELECT library_item_id FROM sessions s WHERE s.user_id = u.id ORDER BY started_at DESC LIMIT 1) AS last_item,
        (SELECT client FROM sessions s WHERE s.user_id = u.id ORDER BY started_at DESC LIMIT 1) AS last_client
        FROM users u ORDER BY seconds DESC, u.username""")


def api_user(db, uid):
    u = one(db, "SELECT * FROM users WHERE id = ?", (uid,))
    if not u:
        raise NotFound()
    n = now_ms()
    u["windows"] = []
    for label, days in (("Last 24 hours", 1), ("Last 7 days", 7), ("Last 30 days", 30), ("All time", 0)):
        r = one(db, """SELECT COALESCE(SUM(time_listening),0) AS seconds, COUNT(*) AS sessions,
                       COUNT(DISTINCT library_item_id) AS titles FROM sessions WHERE user_id = ? AND started_at >= ?""",
                (uid, n - days * 86400000 if days else 0))
        r["label"] = label
        u["windows"].append(r)
    u["finished_count"] = db.execute("SELECT COUNT(*) FROM progress WHERE user_id=? AND is_finished=1", (uid,)).fetchone()[0]
    u["clients"] = rows(db, """SELECT COALESCE(NULLIF(client,''),'Unknown') AS name, SUM(time_listening) AS seconds,
                    COUNT(*) AS sessions FROM sessions WHERE user_id = ? GROUP BY name ORDER BY seconds DESC""", (uid,))
    u["in_progress"] = rows(db, """SELECT p.library_item_id AS item_id, COALESCE(i.title,
                    (SELECT MAX(title) FROM sessions WHERE library_item_id = p.library_item_id)) AS title,
                    i.author, p.progress, p.last_update FROM progress p
                    LEFT JOIN library_items i ON i.id = p.library_item_id
                    WHERE p.user_id = ? AND p.is_finished = 0 AND p.progress > 0 AND p.episode_id = ''
                    ORDER BY p.last_update DESC LIMIT 12""", (uid,))
    u["finished"] = rows(db, """SELECT p.library_item_id AS item_id, COALESCE(i.title,
                    (SELECT MAX(title) FROM sessions WHERE library_item_id = p.library_item_id)) AS title,
                    i.author, p.finished_at FROM progress p LEFT JOIN library_items i ON i.id = p.library_item_id
                    WHERE p.user_id = ? AND p.is_finished = 1 ORDER BY p.finished_at DESC LIMIT 12""", (uid,))
    return u


def api_item(db, iid):
    item = one(db, """SELECT i.*, l.name AS library FROM library_items i
                      LEFT JOIN libraries l ON l.id = i.library_id WHERE i.id = ?""", (iid,))
    if not item:
        item = one(db, """SELECT library_item_id AS id, MAX(title) AS title, MAX(author) AS author,
                          MAX(narrator) AS narrator, MAX(duration) AS duration, MAX(genres) AS genres
                          FROM sessions WHERE library_item_id = ? GROUP BY library_item_id""", (iid,))
        if not item:
            raise NotFound()
        item["removed"] = True
    try:
        item["genres"] = json.loads(item.get("genres") or "[]")
    except ValueError:
        item["genres"] = []
    item["stats"] = one(db, """SELECT COALESCE(SUM(time_listening),0) AS seconds, COUNT(*) AS sessions,
                    COUNT(DISTINCT user_id) AS listeners, MIN(started_at) AS first_played, MAX(started_at) AS last_played
                    FROM sessions WHERE library_item_id = ?""", (iid,))
    item["listeners"] = rows(db, """SELECT p.user_id, p.username, p.progress, p.is_finished, p.finished_at, p.last_update,
                    (SELECT COALESCE(SUM(time_listening),0) FROM sessions s
                     WHERE s.user_id = p.user_id AND s.library_item_id = p.library_item_id) AS seconds
                    FROM progress p WHERE p.library_item_id = ? AND p.episode_id = ''
                    ORDER BY p.last_update DESC""", (iid,))
    return item


def _months_between(start, end):
    y, m = map(int, start.split("-"))
    out = []
    while f"{y:04d}-{m:02d}" <= end:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _stack(data, labels):
    totals = {}
    for r in data:
        totals[r["user"]] = totals.get(r["user"], 0) + (r["seconds"] or 0)
    users = sorted(totals, key=lambda u: -totals[u])
    idx = {l: i for i, l in enumerate(labels)}
    series = {u: [0] * len(labels) for u in users}
    for r in data:
        if r["b"] in idx:
            series[r["user"]][idx[r["b"]]] += r["seconds"] or 0
    return {"labels": labels, "series": [{"name": u, "data": series[u]} for u in users]}


def api_graphs(db, q):
    s, days = since(q)
    today = datetime.now(LOCAL_TZ).date()
    monthly = days == 0 or days > 180
    hourly = 0 < days <= 2
    bucket = "local_month" if monthly else "local_dh" if hourly else "local_date"
    data = rows(db, f"""SELECT {bucket}(started_at) AS b, COALESCE(username,'Unknown') AS user,
                    SUM(time_listening) AS seconds FROM sessions WHERE started_at >= ? GROUP BY b, user""", (s,))
    if monthly:
        first = min((r["b"] for r in data if r["b"]), default=today.strftime("%Y-%m"))
        if days:
            first = max(first, (today - timedelta(days=days)).strftime("%Y-%m"))
        labels = _months_between(first, today.strftime("%Y-%m"))
    elif hourly:
        top = datetime.now(LOCAL_TZ).replace(minute=0, second=0, microsecond=0)
        labels = [(top - timedelta(hours=i)).strftime("%Y-%m-%d %H") for i in range(days * 24 - 1, -1, -1)]
    else:
        labels = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]

    y, m = today.year, today.month - 11
    if m <= 0:
        y, m = y - 1, m + 12
    year_start = int(datetime(y, m, 1, tzinfo=LOCAL_TZ).timestamp() * 1000)
    months = rows(db, """SELECT local_month(started_at) AS b, COALESCE(username,'Unknown') AS user,
                    SUM(time_listening) AS seconds FROM sessions WHERE started_at >= ? GROUP BY b, user""", (year_start,))

    def series_of(fn, n):
        out = [0] * n
        for r in rows(db, f"SELECT {fn}(started_at) AS k, SUM(time_listening) AS seconds FROM sessions WHERE started_at >= ? GROUP BY k", (s,)):
            if r["k"] is not None:
                out[int(r["k"])] = r["seconds"] or 0
        return out

    return {
        "bucket": "month" if monthly else "hour" if hourly else "day",
        "timeline": _stack(data, labels),
        "months": _stack(months, _months_between(f"{y:04d}-{m:02d}", today.strftime("%Y-%m"))),
        "dow": series_of("local_dow", 7),
        "hour": series_of("local_hour", 24),
        "clients": rows(db, """SELECT COALESCE(NULLIF(client,''),'Unknown') AS name, SUM(time_listening) AS seconds
                    FROM sessions WHERE started_at >= ? GROUP BY name ORDER BY seconds DESC LIMIT 8""", (s,)),
        "devices": rows(db, """SELECT COALESCE(NULLIF(device,''),'Unknown') AS name, SUM(time_listening) AS seconds
                    FROM sessions WHERE started_at >= ? GROUP BY name ORDER BY seconds DESC LIMIT 8""", (s,)),
        "genres": rows(db, f"""SELECT j.value AS name, SUM(s.time_listening) AS seconds FROM sessions s, {GENRES_JOIN}
                    WHERE s.started_at >= ? GROUP BY j.value ORDER BY seconds DESC LIMIT 10""", (s,)),
        "methods": rows(db, """SELECT play_method AS name, SUM(time_listening) AS seconds FROM sessions
                    WHERE started_at >= ? GROUP BY play_method ORDER BY seconds DESC""", (s,)),
    }


def api_libraries(db, q):
    return rows(db, """SELECT l.id, l.name, l.media_type,
        (SELECT COUNT(*) FROM library_items i WHERE i.library_id = l.id) AS items,
        (SELECT COALESCE(SUM(duration),0) FROM library_items i WHERE i.library_id = l.id) AS duration,
        (SELECT COALESCE(SUM(size),0) FROM library_items i WHERE i.library_id = l.id) AS size,
        (SELECT COUNT(DISTINCT author) FROM library_items i WHERE i.library_id = l.id) AS authors,
        (SELECT COALESCE(SUM(time_listening),0) FROM sessions s WHERE s.library_id = l.id) AS seconds,
        (SELECT COUNT(*) FROM sessions s WHERE s.library_id = l.id) AS sessions,
        (SELECT MAX(started_at) FROM sessions s WHERE s.library_id = l.id) AS last_played,
        (SELECT MAX(added_at) FROM library_items i WHERE i.library_id = l.id) AS last_added,
        (SELECT id FROM library_items i WHERE i.library_id = l.id ORDER BY added_at DESC LIMIT 6) AS cover_id
        FROM libraries l ORDER BY l.name""")


def api_library_recent(db, lib_id):
    return rows(db, """SELECT id, title, author, added_at FROM library_items WHERE library_id = ?
                       ORDER BY added_at DESC LIMIT 8""", (lib_id,))


ROUTES = {"status": api_status, "activity": api_activity, "summary": api_summary, "home": api_home, "history": api_history,
          "users": api_users, "graphs": api_graphs, "libraries": api_libraries}
ID_ROUTES = {"user": api_user, "item": api_item, "library": api_library_recent}
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


def fetch_cover(item_id):
    os.makedirs(COVER_DIR, exist_ok=True)
    path = os.path.join(COVER_DIR, item_id)
    cached = os.path.exists(path)
    if cached and time.time() - os.path.getmtime(path) < COVER_MAX_AGE:
        with open(path, "rb") as f:
            return f.read()
    try:
        req = urllib.request.Request(f"{ABS_URL}/api/items/{item_id}/cover?width=400",
                                     headers={"Authorization": f"Bearer {ABS_TOKEN}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
    except Exception:
        if cached:
            with open(path, "rb") as f:
                return f.read()
        return None
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return data


def sniff(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


class Handler(BaseHTTPRequestHandler):
    server_version = "abs-stats"

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype, cache=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, code=200):
        self.send(code, json.dumps(data, default=str).encode(), "application/json")

    def do_POST(self):
        if self.path == "/api/sync":
            SYNC_NOW.set()
            return self.send_json({"ok": True}, 202)
        if self.path == "/api/settings":
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)) or b"{}")
                interval = int(body.get("interval"))
            except (ValueError, TypeError):
                return self.send_json({"error": "interval must be a number of seconds"}, 400)
            if interval not in INTERVAL_CHOICES:
                return self.send_json({"error": f"interval must be one of {INTERVAL_CHOICES}"}, 400)
            db = connect()
            try:
                set_state(db, "poll_interval", interval)
                db.commit()
            finally:
                db.close()
            log.info("sync interval set to %ds", interval)
            SYNC_NOW.set()  # wake the loop so the new schedule starts now
            return self.send_json({"ok": True, "interval": interval})
        self.send_json({"error": "not found"}, 404)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        parts = [p for p in u.path.split("/") if p]
        try:
            if parts and parts[0] == "api":
                db = connect()
                try:
                    if len(parts) == 2 and parts[1] in ROUTES:
                        return self.send_json(ROUTES[parts[1]](db, q))
                    if len(parts) == 3 and parts[1] in ID_ROUTES and SAFE_ID.match(parts[2]):
                        return self.send_json(ID_ROUTES[parts[1]](db, parts[2]))
                    raise NotFound()
                finally:
                    db.close()
            if len(parts) == 2 and parts[0] == "cover" and SAFE_ID.match(parts[1]):
                data = fetch_cover(parts[1])
                if not data:
                    raise NotFound()
                return self.send(200, data, sniff(data), "public, max-age=86400")
            if len(parts) == 2 and parts[0] == "static":
                name = os.path.basename(parts[1])
                fp = os.path.join(STATIC_DIR, name)
                ext = os.path.splitext(name)[1]
                if ext in STATIC_TYPES and os.path.isfile(fp):
                    with open(fp, "rb") as f:
                        return self.send(200, f.read(), STATIC_TYPES[ext])
                raise NotFound()
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                return self.send(200, f.read(), STATIC_TYPES[".html"])
        except NotFound:
            self.send_json({"error": "not found"}, 404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            log.exception("request failed: %s", self.path)
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    init_db()
    threading.Thread(target=poll_loop, daemon=True).start()
    log.info("web UI on port %d", WEB_PORT)
    ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
