#!/usr/bin/env python3
"""
xsukax RSS to Mastodon — Multi-Instance Edition
=================================================
Single-file cross-platform Python web app.
Supports multiple Mastodon accounts, per-feed hashtags, and a live scheduler timer.

Install:  pip install flask apscheduler feedparser requests
Run:      python rss_mastodon.py
Access:   http://localhost:5000

@author   xsukax
@version  2.0.0
@license  GPL-3.0 (https://www.gnu.org/licenses/gpl-3.0.html)
"""

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION  ─  Edit and restart to apply
# ══════════════════════════════════════════════════════════════════════════════
ADMIN_USERNAME    = "admin"
ADMIN_PASSWORD    = "admin@123"
MASTODON_APP_NAME = "xsukax RSS to Mastodon"
RSS_INTERVAL_MINS = 30        # Feed check interval in minutes
WEB_HOST          = "0.0.0.0"
WEB_PORT          = 5000
DB_PATH           = "rss_mastodon.db"
POST_LIMIT        = 5         # Max new posts per feed/account pair per run
API_DELAY         = 0.7       # Seconds between Mastodon API calls
# ══════════════════════════════════════════════════════════════════════════════

import os, re, sys, time, json, secrets, logging, sqlite3
import html as _html, threading
from datetime import datetime
from functools import wraps
from urllib.parse import urlencode, urlparse

# ─── Dependency guard ─────────────────────────────────────────────────────────
import importlib.util as _ilu
_missing = [p for p in ("requests","feedparser","flask","apscheduler") if not _ilu.find_spec(p)]
if _missing:
    sys.exit("Missing: pip install " + " ".join(_missing))

import requests, feedparser
from flask import (Flask, render_template_string, request,
                   session, redirect, url_for, g, jsonify)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

APP_NAME    = "xsukax RSS to Mastodon"
APP_VERSION = "2.0.0"

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("rss_mastodon")
logging.getLogger("apscheduler").setLevel(logging.WARNING)

app = Flask(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mastodon_accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_url  TEXT NOT NULL DEFAULT '',
    client_id     TEXT NOT NULL DEFAULT '',
    client_secret TEXT NOT NULL DEFAULT '',
    access_token  TEXT NOT NULL DEFAULT '',
    account_name  TEXT NOT NULL DEFAULT '',
    account_url   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS feeds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    hashtags        TEXT NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1,
    last_fetched_at TEXT,
    last_status     TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS feed_accounts (
    feed_id    INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    PRIMARY KEY (feed_id, account_id),
    FOREIGN KEY (feed_id)    REFERENCES feeds(id)             ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES mastodon_accounts(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS posted_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id    INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    item_guid  TEXT    NOT NULL,
    posted_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(feed_id, account_id, item_guid),
    FOREIGN KEY (feed_id)    REFERENCES feeds(id)             ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES mastodon_accounts(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    triggered   TEXT    NOT NULL DEFAULT 'auto',
    posted      INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    summary     TEXT    NOT NULL DEFAULT '',
    ran_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# Columns to add if upgrading from an older schema
_MIGRATIONS = [
    ("feeds", "hashtags", "TEXT NOT NULL DEFAULT ''"),
]


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = open_db()
    return g.db


@app.teardown_appcontext
def _close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


def init_schema() -> None:
    conn = open_db()
    try:
        conn.executescript(_SCHEMA)
        for table, col, defn in _MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
                conn.commit()
            except Exception:
                pass
        # Migrate legacy mastodon_config → mastodon_accounts (one-time)
        try:
            old = conn.execute("SELECT * FROM mastodon_config WHERE id=1").fetchone()
            if old and old["access_token"]:
                if conn.execute("SELECT COUNT(*) FROM mastodon_accounts").fetchone()[0] == 0:
                    conn.execute(
                        "INSERT INTO mastodon_accounts "
                        "(instance_url,client_id,client_secret,access_token,account_name,account_url) "
                        "VALUES(?,?,?,?,?,?)",
                        (old["instance_url"], old["client_id"], old["client_secret"],
                         old["access_token"], old["account_name"], old.get("account_url",""))
                    )
                    conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


# ─── Settings helpers (thread-safe, own connection) ───────────────────────────

def cfg_get(key: str, default: str = "") -> str:
    conn = open_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def cfg_set(key: str, value: str) -> None:
    conn = open_db()
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES(?,?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def get_or_create_secret() -> str:
    k = cfg_get("flask_secret_key")
    if not k:
        k = secrets.token_hex(32)
        cfg_set("flask_secret_key", k)
    return k


# ── Mastodon account helpers ───────────────────────────────────────────────────

def accounts_all() -> list:
    conn = open_db()
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM mastodon_accounts ORDER BY id").fetchall()]
    finally:
        conn.close()


def account_by_id(aid: int) -> dict:
    conn = open_db()
    try:
        row = conn.execute("SELECT * FROM mastodon_accounts WHERE id=?", (aid,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def any_account_connected() -> bool:
    conn = open_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM mastodon_accounts WHERE access_token!=''"
        ).fetchone()[0] > 0
    finally:
        conn.close()


def account_display(a: dict) -> str:
    host = urlparse(a.get("instance_url","")).hostname or a.get("instance_url","")
    return f"{a.get('account_name','?')} @ {host}"


# ══════════════════════════════════════════════════════════════════════════════
#  RSS PARSING
# ══════════════════════════════════════════════════════════════════════════════

def parse_feed(url: str) -> list:
    try:
        parsed = feedparser.parse(url, agent=f"{APP_NAME}/{APP_VERSION}")
        items  = []
        for e in parsed.entries:
            guid  = (getattr(e,"id",None) or getattr(e,"link",None) or "").strip()
            title = _html.unescape(getattr(e,"title","Untitled").strip())
            link  = getattr(e,"link","").strip()
            desc  = ""
            if hasattr(e, "summary"):
                desc = e.summary
            elif getattr(e,"content",None):
                desc = e.content[0].get("value","")
            desc = _html.unescape(re.sub(r"<[^>]+>","",desc).strip())
            if len(desc) > 200:
                desc = desc[:200] + "…"
            if guid:
                items.append({"guid":guid,"title":title,"link":link,"desc":desc})
        return items
    except Exception as exc:
        logger.error("Feed parse [%s]: %s", url, exc)
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  MASTODON POSTING
# ══════════════════════════════════════════════════════════════════════════════

def format_toot(item: dict, feed_name: str, hashtags: str = "") -> str:
    """Build a ≤500-char status with the feed's custom hashtags."""
    src = feed_name or "RSS Feed"
    tag_parts = ["#RSS", "#xsukaxRSS"]
    for t in (hashtags or "").split(","):
        t = t.strip().lstrip("#")
        if t and re.match(r"^\w+$", t, re.UNICODE):
            tag_parts.append(f"#{t}")
    tags = " ".join(tag_parts)

    body = f"📰 {src}\n\n{item['title']}"
    if item.get("link"):
        body += f"\n\n{item['link']}"
    body += f"\n\n{tags}"

    if len(body) > 490:
        base = f"📰 {src}\n\n\n\n{item.get('link','')}\n\n{tags}"
        budget = max(10, 490 - len(base))
        body = f"📰 {src}\n\n{item['title'][:budget]}…"
        if item.get("link"):
            body += f"\n\n{item['link']}"
        body += f"\n\n{tags}"
    return body


def execute_feed_run(triggered_by: str = "auto") -> tuple:
    """
    Core scheduler job.
    For each active feed → for each linked account → post unseen items.
    Returns (posted, skipped, errors, log_lines).
    Uses its own DB connection — safe to call from background threads.
    """
    conn = open_db()
    try:
        feeds = [dict(r) for r in
                 conn.execute("SELECT * FROM feeds WHERE active=1 ORDER BY id").fetchall()]
        if not feeds:
            return 0, 0, 0, ["No active feeds."]

        posted = skipped = errors = 0
        lines  = []

        for feed in feeds:
            # Accounts this feed is wired to
            accs = [dict(r) for r in conn.execute("""
                SELECT ma.* FROM mastodon_accounts ma
                JOIN feed_accounts fa ON fa.account_id = ma.id
                WHERE fa.feed_id = ? AND ma.access_token != ''
            """, (feed["id"],)).fetchall()]

            if not accs:
                lines.append(f"⚠ No accounts linked: {feed['name']}")
                continue

            items = parse_feed(feed["url"])
            conn.execute(
                "UPDATE feeds SET last_fetched_at=datetime('now'),last_status=? WHERE id=?",
                ("ok" if items else "error", feed["id"])
            )
            conn.commit()

            if not items:
                errors += 1
                lines.append(f"✗ Fetch failed: {feed['url']}")
                continue

            for acc in accs:
                lbl = account_display(acc)
                unseen = [
                    i for i in items
                    if not conn.execute(
                        "SELECT 1 FROM posted_items WHERE feed_id=? AND account_id=? AND item_guid=?",
                        (feed["id"], acc["id"], i["guid"])
                    ).fetchone()
                ]
                unseen   = list(reversed(unseen))  # oldest-first
                to_post  = unseen[:POST_LIMIT]
                skipped += max(0, len(unseen) - POST_LIMIT)

                for item in to_post:
                    try:
                        resp = requests.post(
                            acc["instance_url"].rstrip("/") + "/api/v1/statuses",
                            data={"status":     format_toot(item, feed["name"], feed.get("hashtags","")),
                                  "visibility": "public"},
                            headers={"Authorization": f"Bearer {acc['access_token']}"},
                            timeout=15,
                        )
                        data = resp.json()
                        if data.get("id"):
                            conn.execute(
                                "INSERT OR IGNORE INTO posted_items (feed_id,account_id,item_guid) VALUES(?,?,?)",
                                (feed["id"], acc["id"], item["guid"])
                            )
                            conn.commit()
                            posted += 1
                            lines.append(f"✓ [{lbl}] {item['title'][:60]}")
                        else:
                            errors += 1
                            lines.append(f"✗ [{lbl}] API: {data.get('error', resp.text[:50])}")
                    except Exception as exc:
                        errors += 1
                        lines.append(f"✗ [{lbl}] Exception: {str(exc)[:70]}")
                    time.sleep(API_DELAY)

        return posted, skipped, errors, lines
    finally:
        conn.close()


def run_job(triggered_by: str = "auto") -> None:
    logger.info("Feed run starting (trigger: %s)", triggered_by)
    t0 = time.time()
    try:
        p, s, e, lines = execute_feed_run(triggered_by)
        ms = int((time.time() - t0) * 1000)
        conn = open_db()
        try:
            conn.execute(
                "INSERT INTO run_log (triggered,posted,skipped,errors,duration_ms,summary) "
                "VALUES(?,?,?,?,?,?)",
                (triggered_by, p, s, e, ms, "\n".join(lines[:40]))
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("Run done — posted:%d skipped:%d errors:%d (%dms)", p, s, e, ms)
    except Exception as exc:
        logger.error("Run exception: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

scheduler = BackgroundScheduler(daemon=True)


def start_scheduler() -> None:
    scheduler.add_job(run_job, IntervalTrigger(minutes=RSS_INTERVAL_MINS),
                      id="feed_run", replace_existing=True,
                      kwargs={"triggered_by": "auto"})
    if not scheduler.running:
        scheduler.start()
    logger.info("Scheduler running — interval: %d min", RSS_INTERVAL_MINS)


def next_run_info() -> tuple:
    """Returns (display_str, unix_ts, secs_remaining, pct_elapsed)."""
    try:
        job = scheduler.get_job("feed_run")
        if job and job.next_run_time:
            nrt  = job.next_run_time
            now  = datetime.now(tz=nrt.tzinfo)
            secs = max(0, int((nrt - now).total_seconds()))
            pct  = min(100, int(100 - secs / (RSS_INTERVAL_MINS * 60) * 100))
            return nrt.strftime("%b %d, %H:%M:%S"), int(nrt.timestamp()), secs, pct
    except Exception:
        pass
    return "—", 0, 0, 100


# ══════════════════════════════════════════════════════════════════════════════
#  AUTH & CSRF
# ══════════════════════════════════════════════════════════════════════════════

def admin_required(f):
    @wraps(f)
    def _w(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return _w


def get_csrf() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(24)
    return session["csrf"]


def ok_csrf() -> bool:
    return secrets.compare_digest(session.get("csrf",""), request.form.get("_t",""))


def flash(msg: str, ftype: str = "ok") -> None:
    session["flash_msg"]  = msg
    session["flash_type"] = ftype


def fmt_dur(s: int) -> str:
    return f"{s}s" if s < 60 else f"{s//60}m {s%60}s"


# ══════════════════════════════════════════════════════════════════════════════
#  TEMPLATE
# ══════════════════════════════════════════════════════════════════════════════

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ APP_NAME }}{% if page %} · {{ page|replace('_',' ')|title }}{% endif %}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f6f8fa;color:#24292f;font-size:14px;line-height:1.5;min-height:100vh}
a{color:#0969da;text-decoration:none}a:hover{text-decoration:underline}
.hdr{background:#24292f;height:52px;padding:0 20px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:200;box-shadow:0 1px 0 rgba(255,255,255,.06)}
.brand{display:flex;align-items:center;gap:9px;color:#fff;font-weight:600;font-size:14px;text-decoration:none!important}
.brand:hover{color:#e6edf3;text-decoration:none!important}
.hnav{display:flex;align-items:center;gap:2px}
.hnav a{color:#8b949e;font-size:13px;padding:6px 11px;border-radius:6px;transition:background .12s,color .12s}
.hnav a:hover{color:#e6edf3;background:rgba(255,255,255,.08);text-decoration:none}
.hnav a.on{color:#fff;background:rgba(255,255,255,.12)}
.hnav .out{color:#f85149}.hnav .out:hover{background:rgba(248,81,73,.12)}
.wrap{max-width:980px;margin:28px auto;padding:0 16px}
.ph{margin-bottom:22px}.ph h1{font-size:20px;font-weight:700}.ph p{color:#57606a;font-size:13px;margin-top:3px}
.al{padding:10px 14px;border-radius:6px;margin-bottom:18px;font-size:13px;display:flex;gap:8px;border:1px solid;align-items:flex-start}
.ok{background:#dafbe1;border-color:#4ac26b;color:#116329}.er{background:#ffebe9;border-color:#ff8182;color:#a40e26}
.wn{background:#fff8c5;border-color:#d4a72c;color:#7d4e00}.inf{background:#ddf4ff;border-color:#54aeff;color:#0550ae}
.card{background:#fff;border:1px solid #d0d7de;border-radius:6px;margin-bottom:20px;overflow:hidden}
.ch{padding:12px 16px;border-bottom:1px solid #d0d7de;display:flex;align-items:center;justify-content:space-between;background:#f6f8fa}
.ch h2{font-size:14px;font-weight:600}.cb{padding:16px}
.sg{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px;margin-bottom:20px}
.sc{background:#fff;border:1px solid #d0d7de;border-radius:6px;padding:18px 14px;text-align:center;position:relative;overflow:hidden}
.sc::after{content:'';position:absolute;top:0;left:0;right:0;height:3px}
.c-blue::after{background:#0969da}.c-green::after{background:#1a7f37}.c-purple::after{background:#6639ba}.c-orange::after{background:#bc4c00}
.sn{font-size:26px;font-weight:700;line-height:1.2}.sl{font-size:12px;color:#57606a;margin-top:5px}
.sched{display:flex;align-items:center;gap:14px;padding:14px 16px;background:#fff;border:1px solid #d0d7de;border-radius:6px;margin-bottom:20px;flex-wrap:wrap}
.sdot{width:10px;height:10px;border-radius:50%;flex-shrink:0;transition:background .5s}
.sd-on{background:#1a7f37;box-shadow:0 0 0 3px #dafbe1}.sd-idle{background:#0969da;box-shadow:0 0 0 3px #ddf4ff}.sd-off{background:#6e7781;box-shadow:0 0 0 3px #eaeef2}
.si{flex:1 1 200px}.si strong{font-size:13px;color:#24292f;display:block}.si span{font-size:12px;color:#57606a}
.pw{flex:1 1 220px}.pl{font-size:11px;color:#57606a;margin-bottom:5px;display:flex;justify-content:space-between;align-items:center}
.pl strong{font-size:13px;color:#24292f}
.pb{background:#eaeef2;border-radius:99px;height:8px;overflow:hidden;box-shadow:inset 0 1px 2px rgba(0,0,0,.06)}
.pf{height:100%;border-radius:99px;transition:width .9s cubic-bezier(.4,0,.2,1);background:linear-gradient(90deg,#0969da,#54aeff)}
.pf.pfo{background:linear-gradient(90deg,#bc4c00,#e16a2e)}.pf.pfg{background:linear-gradient(90deg,#1a7f37,#57ab5a)}
.fg{margin-bottom:14px}
label{display:block;font-size:13px;font-weight:600;margin-bottom:5px}label .h{font-weight:400;color:#57606a;font-size:12px;margin-left:4px}
input[type=text],input[type=password],input[type=url],input[type=search]{width:100%;padding:5px 12px;border:1px solid #d0d7de;border-radius:6px;font-size:14px;background:#fff;outline:none;transition:border-color .15s,box-shadow .15s}
input:focus{border-color:#0969da;box-shadow:0 0 0 3px rgba(9,105,218,.15)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}.row .fg{flex:1;min-width:180px;margin-bottom:0}
.acc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;margin-top:6px}
.acc-check{display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid #d0d7de;border-radius:6px;cursor:pointer;transition:background .12s,border-color .12s;background:#fff}
.acc-check:hover{background:#f6f8fa;border-color:#0969da}
.acc-check input[type=checkbox]{accent-color:#0969da;width:15px;height:15px;cursor:pointer;flex-shrink:0}
.acc-check-label{font-size:12px;line-height:1.4}
.acc-check-label strong{display:block;color:#24292f;font-size:13px}
.acc-check-label span{color:#57606a;font-size:11px}
.acc-check.selected{border-color:#0969da;background:#ddf4ff}
.btn{display:inline-flex;align-items:center;gap:5px;padding:5px 16px;border-radius:6px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid rgba(31,35,40,.15);line-height:20px;white-space:nowrap;text-decoration:none!important;transition:filter .12s,opacity .12s}
.btn:active{filter:brightness(.92)}
.bp{background:#1a7f37;color:#fff}.bp:hover{background:#2c974b;color:#fff}
.bd{background:#cf222e;color:#fff}.bd:hover{background:#a40e26;color:#fff}
.bs{background:#f6f8fa;color:#24292f;border-color:#d0d7de}.bs:hover{background:#eaeef2;color:#24292f}
.bm{background:#6364ff;color:#fff}.bm:hover{background:#5252d4;color:#fff}
.bo{background:#bc4c00;color:#fff}.bo:hover{background:#953800;color:#fff}
.sm{padding:3px 10px;font-size:12px}
table{width:100%;border-collapse:collapse}
th{background:#f6f8fa;padding:8px 12px;text-align:left;font-size:12px;font-weight:600;color:#57606a;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #d0d7de;white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid #eaeef2;vertical-align:middle}
tr:last-child td{border-bottom:none}tr:hover td{background:#f6f8fa}
.b{display:inline-flex;align-items:center;gap:3px;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600;border:1px solid;white-space:nowrap}
.bg{background:#dafbe1;color:#1a7f37;border-color:#4ac26b40}.br{background:#ffebe9;color:#a40e26;border-color:#ff818240}
.bb{background:#ddf4ff;color:#0550ae;border-color:#54aeff40}.bp2{background:#fbefff;color:#6e40c9;border-color:#d2b4ff40}
.bgy{background:#eaeef2;color:#57606a;border-color:#d0d7de}.byw{background:#fff8c5;color:#7d4e00;border-color:#d4a72c40}
.borg{background:#fff1e5;color:#bc4c00;border-color:#fb8f4430}
.tag-pill{display:inline-flex;align-items:center;padding:1px 7px;border-radius:20px;font-size:11px;background:#e8f3ff;color:#0550ae;border:1px solid #b6d4fb;margin:1px}
.acc-card{display:flex;align-items:center;gap:12px;padding:14px;border:1px solid #d0d7de;border-radius:6px;background:#fff;margin-bottom:10px}
.acc-av{width:40px;height:40px;border-radius:50%;background:#6364ff;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
.acc-info{flex:1}.acc-info strong{display:block;font-size:14px;color:#24292f}.acc-info span{font-size:12px;color:#57606a}
.acc-stat{font-size:12px;color:#57606a;margin-top:2px}
.ll{font-family:'SFMono-Regular',Consolas,monospace;font-size:11px;padding:3px 0;border-bottom:1px solid #eaeef2;color:#57606a;white-space:pre-wrap}
.ll:last-child{border-bottom:none}.ll.ok{color:#1a7f37}.ll.er{color:#a40e26}.ll.wn{color:#7d4e00}
.emp{text-align:center;padding:44px 16px;color:#57606a}.emp p{font-size:14px;margin-top:10px}
.step{display:flex;gap:12px;margin-bottom:12px;align-items:flex-start}
.stn{width:22px;height:22px;border-radius:50%;background:#0969da;color:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:2px}
.stp{font-size:13px;color:#57606a;line-height:1.5}
.lw{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}
.lb{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:40px 32px;width:100%;max-width:340px}
.llo{text-align:center;margin-bottom:28px}.llo h1{font-size:18px;font-weight:700;margin-top:10px}.llo p{font-size:12px;color:#57606a;margin-top:2px}
.ft{text-align:center;padding:20px;color:#8b949e;font-size:12px;margin-top:8px;border-top:1px solid #eaeef2}
.hint-txt{font-size:12px;color:#57606a;margin-top:4px}
@media(max-width:640px){.hdr{height:auto;flex-direction:column;align-items:flex-start;padding:10px 16px;gap:8px}.hnav{flex-wrap:wrap}.sg{grid-template-columns:1fr 1fr}.row{flex-direction:column}}
</style>
</head>
<body>

{# ════ LOGIN ═══════════════════════════════════════════════════════════════ #}
{% if page == "login" %}
<div class="lw">
  <div class="lb">
    <div class="llo">
      <h1>{{ APP_NAME }}</h1><p>Admin Portal</p>
    </div>
    {% if error %}<div class="al er">⚠️ {{ error }}</div>{% endif %}
    <form method="POST" action="/login">
      <input type="hidden" name="_t" value="{{ csrf }}">
      <div class="fg"><label>Username</label><input type="text" name="username" autofocus required autocomplete="username"></div>
      <div class="fg" style="margin-bottom:20px"><label>Password</label><input type="password" name="password" required autocomplete="current-password"></div>
      <button type="submit" class="btn bp" style="width:100%;justify-content:center;padding:7px">Sign in</button>
    </form>
  </div>
</div>

{% else %}
{# ════ APP SHELL ═══════════════════════════════════════════════════════════ #}
<header class="hdr">
  <a class="brand" href="/">
    {{ APP_NAME }}
  </a>
  <nav class="hnav">
    <a href="/" class="{{ 'on' if page=='dashboard' else '' }}">Dashboard</a>
    <a href="/feeds" class="{{ 'on' if page in ('feeds','feeds_edit') else '' }}">Feeds <span class="b bb" style="font-size:10px;padding:1px 6px">{{ feed_count }}</span></a>
    <a href="/accounts" class="{{ 'on' if page=='accounts' else '' }}">Accounts <span class="b {{ 'bg' if mc_connected else 'br' }}" style="font-size:10px;padding:1px 6px">{{ total_accounts }}</span></a>
    <a href="/log" class="{{ 'on' if page=='log' else '' }}">Run Log</a>
    <a href="/logout" class="out">Logout</a>
  </nav>
</header>

<div class="wrap">
{% if flash_msg %}
<div class="al {{ flash_type }}">{{ '✅' if flash_type=='ok' else '⚠️' }} {{ flash_msg }}</div>
{% endif %}

{# ─── DASHBOARD ─────────────────────────────────────────────────────────── #}
{% if page == "dashboard" %}
<div class="ph"><h1>Dashboard</h1><p>Live status of your RSS → Mastodon bridge</p></div>

<div class="sched" id="sched-bar">
  {% if not mc_connected %}
    <div class="sdot sd-off"></div>
    <div class="si"><strong>Scheduler Inactive</strong><span>Add a Mastodon account to activate auto-posting</span></div>
  {% else %}
    <div class="sdot sd-idle" id="sched-dot"></div>
    <div class="si">
      <strong>⏱ Auto-Scheduler Active</strong>
      <span>Every {{ RSS_INTERVAL_MINS }}m · Next run: <strong id="nrt">{{ next_run_str }}</strong></span>
    </div>
    <div class="pw">
      <div class="pl"><span>Elapsed</span><strong id="ppct">{{ pct }}%</strong></div>
      <div class="pb"><div class="pf" id="pbar" style="width:{{ pct }}%"></div></div>
      <div style="text-align:right;font-size:11px;color:#57606a;margin-top:4px">⏳ <span id="countdown">--</span> remaining</div>
    </div>
  {% endif %}
</div>

<div class="sg">
  <div class="sc c-blue"><div class="sn">{{ active_feeds }}/{{ feed_count }}</div><div class="sl">📡 Active/Total Feeds</div></div>
  <div class="sc c-green"><div class="sn">{{ posted_total }}</div><div class="sl">🐘 Items Posted</div></div>
  <div class="sc c-purple"><div class="sn">{{ total_accounts }}</div><div class="sl">🔗 Linked Accounts</div></div>
  <div class="sc c-orange"><div class="sn">{{ run_count }}</div><div class="sl">⚙️ Scheduler Runs</div></div>
</div>

{% if last_log %}
<div class="card">
  <div class="ch"><h2>📋 Last Run</h2><span class="b bgy">{{ last_log.ran_at }}</span></div>
  <div class="cb">
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
      <span class="b bg">✓ {{ last_log.posted }} posted</span>
      {% if last_log.errors %}<span class="b br">✗ {{ last_log.errors }} errors</span>{% endif %}
      <span class="b bb">⚙ {{ last_log.triggered }}</span>
      <span class="b bgy">⏱ {{ last_log.duration_ms }}ms</span>
    </div>
    {% if last_log.summary %}
    <div style="max-height:120px;overflow-y:auto;border:1px solid #d0d7de;border-radius:6px;padding:8px 10px">
      {% for ln in last_log.summary.split('\n') %}{% if ln.strip() %}
      <div class="ll {{ 'ok' if ln.startswith('✓') else 'er' if ln.startswith('✗') else 'wn' }}">{{ ln }}</div>
      {% endif %}{% endfor %}
    </div>
    {% endif %}
  </div>
</div>
{% endif %}

<div class="card">
  <div class="ch"><h2>⚡ Actions</h2></div>
  <div class="cb" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
    {% if mc_connected %}
      <a href="/run/now" class="btn bp" onclick="return confirm('Run a full feed check now?')">▶ Run Now</a>
      <a href="/feeds" class="btn bs">📡 Manage Feeds</a>
    {% else %}
      <div class="al inf" style="margin:0;flex:1">💡 <a href="/accounts">Add a Mastodon account</a> to start auto-posting.</div>
    {% endif %}
  </div>
</div>

{# ─── FEEDS ──────────────────────────────────────────────────────────────── #}
{% elif page == "feeds" %}
<div class="ph"><h1>RSS Feeds</h1><p>Checked every {{ RSS_INTERVAL_MINS }} minutes · assign each feed to one or more Mastodon accounts</p></div>

<div class="card">
  <div class="ch"><h2>➕ Add Feed</h2></div>
  <div class="cb">
    <form method="POST" action="/feeds/add">
      <input type="hidden" name="_t" value="{{ csrf }}">
      <div class="row">
        <div class="fg"><label>Feed URL <span style="color:#cf222e">*</span></label><input type="url" name="url" placeholder="https://example.com/rss.xml" required></div>
        <div class="fg"><label>Display Name <span class="h">(optional)</span></label><input type="text" name="name" placeholder="My Blog"></div>
      </div>
      <div class="fg" style="margin-top:4px">
        <label>Hashtags <span class="h">comma-separated, without #</span></label>
        <input type="text" name="hashtags" placeholder="python, news, opensource">
        <p class="hint-txt">Added to every post from this feed. #RSS and #xsukaxRSS are always included.</p>
      </div>
      {% if all_accounts %}
      <div class="fg">
        <label>Post to Accounts <span style="color:#cf222e">*</span></label>
        <div class="acc-grid">
          {% for a in all_accounts %}
          <label class="acc-check" id="ck-{{ a.id }}">
            <input type="checkbox" name="accounts" value="{{ a.id }}" onchange="toggleAccCard(this)">
            <div class="acc-check-label">
              <strong>{{ a.account_name }}</strong>
              <span>{{ a.instance_url|replace('https://','') }}</span>
            </div>
          </label>
          {% endfor %}
        </div>
      </div>
      {% else %}
      <div class="al wn" style="margin-top:8px">⚠️ No Mastodon accounts connected. <a href="/accounts">Add one first</a>.</div>
      {% endif %}
      <div style="margin-top:6px"><button type="submit" class="btn bp">Add Feed</button></div>
    </form>
  </div>
</div>

<div class="card">
  <div class="ch"><h2>📡 Feed List</h2>
    <div style="display:flex;gap:6px"><span class="b bg">✓ {{ active_feeds }} active</span><span class="b bgy">{{ feed_count - active_feeds }} paused</span></div>
  </div>
  {% if not feeds %}
  <div class="emp">
    <svg width="48" height="48" fill="none" stroke="#d0d7de" stroke-width="1.5" viewBox="0 0 24 24"><path d="M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3" stroke-linecap="round" stroke-linejoin="round"/></svg>
    <p>No feeds yet. Add an RSS or Atom URL above.</p>
  </div>
  {% else %}
  <table>
    <thead><tr><th>Feed</th><th>Hashtags</th><th>Accounts</th><th>Status</th><th>Posted</th><th>Last Fetch</th><th>Actions</th></tr></thead>
    <tbody>
    {% for f in feeds %}
    <tr>
      <td>
        <div style="font-weight:600;font-size:13px">{{ f.name }}</div>
        <a href="{{ f.url }}" target="_blank" style="font-family:monospace;font-size:11px;color:#57606a;word-break:break-all">{{ f.url[:55] }}{% if f.url|length > 55 %}…{% endif %}</a>
      </td>
      <td>
        {% if f.hashtags %}
          {% for t in f.hashtags.split(',') %}{% if t.strip() %}
          <span class="tag-pill">#{{ t.strip().lstrip('#') }}</span>
          {% endif %}{% endfor %}
        {% else %}<span style="color:#8b949e;font-size:12px">—</span>{% endif %}
      </td>
      <td>
        {% if f.linked_accounts %}
          {% for a in f.linked_accounts %}
          <div style="font-size:11px;white-space:nowrap">
            <span class="b bb" style="margin-bottom:2px">{{ a.account_name }}</span>
          </div>
          {% endfor %}
        {% else %}<span class="b br">⚠ None</span>{% endif %}
      </td>
      <td>
        {% if not f.active %}<span class="b bgy">⏸ Paused</span>
        {% elif f.last_status == 'ok' %}<span class="b bg">✓ OK</span>
        {% elif f.last_status == 'error' %}<span class="b br">✗ Error</span>
        {% else %}<span class="b byw">○ Pending</span>{% endif %}
      </td>
      <td><span class="b bp2">📤 {{ f.post_count }}</span></td>
      <td style="font-size:12px;color:#57606a;white-space:nowrap">{{ f.last_fetched_at or '—' }}</td>
      <td>
        <div style="display:flex;gap:4px;flex-wrap:wrap">
          <a href="/feeds/edit/{{ f.id }}" class="btn bs sm">✏ Edit</a>
          <a href="/feeds/toggle/{{ f.id }}" class="btn bs sm">{{ '▶' if not f.active else '⏸' }}</a>
          <a href="/feeds/delete/{{ f.id }}" class="btn bd sm" onclick="return confirm('Delete this feed?')">✕</a>
        </div>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}
</div>

{# ─── FEEDS EDIT ─────────────────────────────────────────────────────────── #}
{% elif page == "feeds_edit" %}
<div class="ph">
  <h1>✏ Edit Feed</h1>
  <p><a href="/feeds">← Back to Feeds</a></p>
</div>
<div class="card">
  <div class="ch"><h2>{{ feed_obj.name }}</h2><span class="b bgy" style="font-family:monospace;font-size:11px">{{ feed_obj.url[:55] }}{% if feed_obj.url|length>55 %}…{% endif %}</span></div>
  <div class="cb">
    <form method="POST" action="/feeds/edit/{{ feed_obj.id }}">
      <input type="hidden" name="_t" value="{{ csrf }}">
      <div class="row">
        <div class="fg"><label>Display Name</label><input type="text" name="name" value="{{ feed_obj.name }}" required></div>
        <div class="fg"><label>Feed URL</label><input type="url" name="url" value="{{ feed_obj.url }}" required></div>
      </div>
      <div class="fg">
        <label>Hashtags <span class="h">comma-separated, without #</span></label>
        <input type="text" name="hashtags" value="{{ feed_obj.hashtags }}" placeholder="python, news, tech">
        <p class="hint-txt">#RSS and #xsukaxRSS are always included automatically.</p>
      </div>
      <div class="fg">
        <label>Post to Accounts</label>
        {% if all_accounts %}
        <div class="acc-grid">
          {% for a in all_accounts %}
          <label class="acc-check {{ 'selected' if a.id in feed_account_ids else '' }}" id="ck-{{ a.id }}">
            <input type="checkbox" name="accounts" value="{{ a.id }}"
                   {{ 'checked' if a.id in feed_account_ids else '' }}
                   onchange="toggleAccCard(this)">
            <div class="acc-check-label">
              <strong>{{ a.account_name }}</strong>
              <span>{{ a.instance_url|replace('https://','') }}</span>
            </div>
          </label>
          {% endfor %}
        </div>
        {% else %}
        <div class="al wn">⚠️ No Mastodon accounts connected. <a href="/accounts">Add one first</a>.</div>
        {% endif %}
      </div>
      <div style="display:flex;gap:8px">
        <button type="submit" class="btn bp">Save Changes</button>
        <a href="/feeds" class="btn bs">Cancel</a>
      </div>
    </form>
  </div>
</div>

{# ─── ACCOUNTS ───────────────────────────────────────────────────────────── #}
{% elif page == "accounts" %}
<div class="ph"><h1>🐘 Mastodon Accounts</h1><p>Connect multiple accounts — even different users on the same instance</p></div>

<div class="card">
  <div class="ch"><h2>➕ Connect New Account</h2></div>
  <div class="cb">
    <div class="al inf" style="margin-bottom:14px">💡 The app self-registers on your instance via the public API — no manual setup needed. Works with Mastodon, Pleroma, Akkoma, Pixelfed, and more.</div>
    <form method="POST" action="/accounts/connect">
      <input type="hidden" name="_t" value="{{ csrf }}">
      <div class="row">
        <div class="fg"><label>Mastodon Instance URL</label><input type="url" name="instance" placeholder="https://mastodon.social" required></div>
        <div style="padding-top:18px"><button type="submit" class="btn bm">🐘 Authorize</button></div>
      </div>
    </form>
  </div>
</div>

<div class="card">
  <div class="ch"><h2>🔗 Connected Accounts</h2><span class="b bb">{{ total_accounts }} account{{ 's' if total_accounts != 1 else '' }}</span></div>
  <div class="cb">
    {% if not accounts %}
    <div class="emp" style="padding:28px 16px">
      <p>No accounts connected yet. Use the form above to add your first account.</p>
    </div>
    {% else %}
    {% for a in accounts %}
    <div class="acc-card">
      <div class="acc-av">🐘</div>
      <div class="acc-info">
        <strong>{{ a.account_name }}</strong>
        <span>{{ a.instance_url }}</span>
        <div class="acc-stat">
          <span class="b bgy" style="margin-right:4px">📤 {{ a.feed_count }} feed{{ 's' if a.feed_count != 1 else '' }}</span>
          <span class="b bp2">✓ {{ a.post_count }} posted</span>
        </div>
      </div>
      <div style="display:flex;gap:6px;align-items:center;flex-shrink:0">
        {% if a.account_url %}<a href="{{ a.account_url }}" target="_blank" class="btn bs sm">Profile ↗</a>{% endif %}
        <a href="/accounts/disconnect/{{ a.id }}" class="btn bd sm"
           onclick="return confirm('Disconnect {{ a.account_name }}? All feed links for this account will be removed.')">Disconnect</a>
      </div>
    </div>
    {% endfor %}
    {% endif %}
  </div>
</div>

<div class="card">
  <div class="ch"><h2>ℹ️ OAuth Flow</h2></div>
  <div class="cb">
    <div class="step"><div class="stn">1</div><p class="stp">Calls <code>POST /api/v1/apps</code> on the instance to self-register an OAuth client — automatic.</p></div>
    <div class="step"><div class="stn">2</div><p class="stp">Redirects you to your Mastodon instance to grant <em>write:statuses</em> + <em>read:accounts</em>.</p></div>
    <div class="step"><div class="stn">3</div><p class="stp">Exchanges the authorization code for an access token via <code>POST /oauth/token</code>.</p></div>
    <div class="step"><div class="stn">4</div><p class="stp">Token stored <strong>only in your local SQLite file</strong>. Nothing sent to third parties. Revoke any time from Mastodon settings.</p></div>
  </div>
</div>

{# ─── RUN LOG ────────────────────────────────────────────────────────────── #}
{% elif page == "log" %}
<div class="ph"><h1>Run Log</h1><p>Last 100 scheduler runs</p></div>
<div class="card">
  <div class="ch"><h2>📜 History</h2>
    {% if run_logs %}<a href="/log/clear" class="btn bd sm" onclick="return confirm('Clear all log entries?')">Clear</a>{% endif %}
  </div>
  {% if not run_logs %}
  <div class="emp">
    <svg width="40" height="40" fill="none" stroke="#d0d7de" stroke-width="1.5" viewBox="0 0 24 24"><path d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" stroke-linecap="round" stroke-linejoin="round"/></svg>
    <p>No runs recorded yet.</p>
  </div>
  {% else %}
  <table>
    <thead><tr><th>Date & Time</th><th>Trigger</th><th>Posted</th><th>Errors</th><th>Duration</th><th>Detail</th></tr></thead>
    <tbody>
    {% for r in run_logs %}
    <tr>
      <td style="white-space:nowrap;font-size:13px">{{ r.ran_at }}</td>
      <td><span class="b {{ 'borg' if r.triggered=='manual' else 'bb' }}">{{ r.triggered }}</span></td>
      <td><span class="b {{ 'bg' if r.posted > 0 else 'bgy' }}">✓ {{ r.posted }}</span></td>
      <td>{% if r.errors %}<span class="b br">✗ {{ r.errors }}</span>{% else %}<span style="color:#57606a">—</span>{% endif %}</td>
      <td style="font-size:12px;color:#57606a">{{ r.duration_ms }}ms</td>
      <td>
        {% if r.summary %}
        <details>
          <summary style="cursor:pointer;font-size:12px;color:#0969da">View</summary>
          <div style="margin-top:6px;border:1px solid #d0d7de;border-radius:6px;padding:6px 10px;max-height:140px;overflow-y:auto">
            {% for ln in r.summary.split('\n') %}{% if ln.strip() %}
            <div class="ll {{ 'ok' if ln.startswith('✓') else 'er' if ln.startswith('✗') else 'wn' }}">{{ ln }}</div>
            {% endif %}{% endfor %}
          </div>
        </details>
        {% else %}<span style="color:#57606a;font-size:12px">—</span>{% endif %}
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}
</div>
{% endif %}

</div><!-- /wrap -->
<div class="ft"><strong>{{ APP_NAME }}</strong> v{{ APP_VERSION }} · <a href="https://www.gnu.org/licenses/gpl-3.0.html" target="_blank">GPL-3.0</a> · Built by xsukax</div>
{% endif %}

<script>
/* ── Account checkbox style toggle ───────────────────────────────────────── */
function toggleAccCard(el) {
  el.closest('.acc-check').classList.toggle('selected', el.checked);
}
/* Pre-mark any already-checked boxes on page load */
document.querySelectorAll('.acc-check input[type=checkbox]:checked').forEach(function(el){
  el.closest('.acc-check').classList.add('selected');
});

/* ── Live scheduler timer ────────────────────────────────────────────────── */
(function(){
  var DATA = {
    nextTs:   {{ next_run_unix | int }},
    interval: {{ interval_secs | int }},
    active:   {{ 'true' if mc_connected else 'false' }}
  };
  if (!DATA.active) return;

  var pbar     = document.getElementById('pbar');
  var ppct     = document.getElementById('ppct');
  var nrt      = document.getElementById('nrt');
  var cdwn     = document.getElementById('countdown');
  var lastSync = Math.floor(Date.now() / 1000);

  function fmt(s) {
    if (s <= 0) return 'any moment…';
    var m = Math.floor(s / 60), r = s % 60;
    return (m > 0 ? m + 'm ' : '') + r + 's';
  }

  function tick() {
    var now = Math.floor(Date.now() / 1000);
    var rem = Math.max(0, DATA.nextTs - now);
    var pct = Math.min(100, Math.round((DATA.interval - rem) / DATA.interval * 100));

    if (pbar) {
      pbar.style.width = pct + '%';
      pbar.className = 'pf' + (pct >= 95 ? ' pfg' : pct >= 75 ? ' pfo' : '');
    }
    if (ppct) ppct.textContent = pct + '%';
    if (cdwn) cdwn.textContent = fmt(rem);
    if (nrt && rem <= 0) nrt.textContent = 'running…';

    /* Re-sync with server every 90 seconds */
    if (now - lastSync >= 90) {
      lastSync = now;
      fetch('/api/status')
        .then(function(r){ return r.json(); })
        .then(function(d){
          if (d.next_run_ts && d.next_run_ts > 0) DATA.nextTs = d.next_run_ts;
        })
        .catch(function(){});
    }
  }

  setInterval(tick, 1000);
  tick();
})();
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════════════
#  RENDER HELPER
# ══════════════════════════════════════════════════════════════════════════════

def render(page: str, **extra):
    db = get_db()
    feed_count   = db.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
    active_fds   = db.execute("SELECT COUNT(*) FROM feeds WHERE active=1").fetchone()[0]
    total_accs   = db.execute(
        "SELECT COUNT(*) FROM mastodon_accounts WHERE access_token!=''"
    ).fetchone()[0]
    posted_tot   = db.execute("SELECT COUNT(*) FROM posted_items").fetchone()[0]
    run_count    = db.execute("SELECT COUNT(*) FROM run_log").fetchone()[0]

    nxt_str, nxt_unix, _, pct = next_run_info()

    ctx = dict(
        page=page, APP_NAME=APP_NAME, APP_VERSION=APP_VERSION,
        RSS_INTERVAL_MINS=RSS_INTERVAL_MINS, API_DELAY=API_DELAY, POST_LIMIT=POST_LIMIT,
        feed_count=feed_count, active_feeds=active_fds,
        total_accounts=total_accs, mc_connected=total_accs > 0,
        posted_total=posted_tot, run_count=run_count,
        next_run_str=nxt_str, next_run_unix=nxt_unix,
        interval_secs=RSS_INTERVAL_MINS * 60, pct=pct,
        csrf=get_csrf(),
        flash_msg=session.pop("flash_msg", ""),
        flash_type=session.pop("flash_type", "ok"),
        last_log=None, feeds=[], run_logs=[],
        accounts=[], all_accounts=[], feed_obj=None, feed_account_ids=[],
    )
    ctx.update(extra)
    return render_template_string(TEMPLATE, **ctx)


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

# ─── Auth ─────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET","POST"])
def login():
    if session.get("admin"):
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        if not ok_csrf():
            error = "Security token mismatch."
        elif (secrets.compare_digest(request.form.get("username",""), ADMIN_USERNAME) and
              secrets.compare_digest(request.form.get("password",""), ADMIN_PASSWORD)):
            session.clear()
            session["admin"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        else:
            error = "Invalid credentials."
    return render_template_string(
        TEMPLATE, page="login", APP_NAME=APP_NAME, APP_VERSION=APP_VERSION,
        error=error, csrf=get_csrf(), flash_msg="", flash_type="ok",
        feed_count=0, active_feeds=0, total_accounts=0, mc_connected=False,
        posted_total=0, run_count=0, RSS_INTERVAL_MINS=RSS_INTERVAL_MINS,
        API_DELAY=API_DELAY, POST_LIMIT=POST_LIMIT,
        next_run_unix=0, interval_secs=0, pct=0, next_run_str="—",
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/dashboard")
@admin_required
def dashboard():
    db  = get_db()
    ll  = db.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT 1").fetchone()
    return render("dashboard", last_log=dict(ll) if ll else None)


# ─── API: live timer data ──────────────────────────────────────────────────────

@app.route("/api/status")
@admin_required
def api_status():
    _, nxt_unix, secs, pct = next_run_info()
    return jsonify({"next_run_ts": nxt_unix, "secs_remaining": secs,
                    "pct": pct, "interval_secs": RSS_INTERVAL_MINS * 60})


# ─── Feeds ────────────────────────────────────────────────────────────────────

def _load_feeds(db) -> list:
    """Return feeds enriched with linked account info and post counts."""
    rows = db.execute("SELECT * FROM feeds ORDER BY created_at DESC").fetchall()
    result = []
    for f in rows:
        f = dict(f)
        f["post_count"] = db.execute(
            "SELECT COUNT(*) FROM posted_items WHERE feed_id=?", (f["id"],)
        ).fetchone()[0]
        f["linked_accounts"] = [dict(r) for r in db.execute("""
            SELECT ma.id, ma.account_name, ma.instance_url
            FROM mastodon_accounts ma
            JOIN feed_accounts fa ON fa.account_id = ma.id
            WHERE fa.feed_id = ?
        """, (f["id"],)).fetchall()]
        result.append(f)
    return result


@app.route("/feeds")
@admin_required
def feeds():
    db   = get_db()
    accs = [dict(r) for r in db.execute(
        "SELECT * FROM mastodon_accounts WHERE access_token!='' ORDER BY id"
    ).fetchall()]
    return render("feeds", feeds=_load_feeds(db), all_accounts=accs)


@app.route("/feeds/add", methods=["POST"])
@admin_required
def feeds_add():
    if not ok_csrf():
        flash("Security error.", "er")
        return redirect(url_for("feeds"))

    url      = request.form.get("url","").strip()
    name     = request.form.get("name","").strip()
    hashtags = request.form.get("hashtags","").strip()
    accs     = request.form.getlist("accounts")  # list of account id strings

    if not url.startswith(("http://","https://")):
        flash("Invalid feed URL.", "er")
        return redirect(url_for("feeds"))

    db = get_db()
    try:
        from urllib.parse import urlparse as _up
        db.execute("INSERT INTO feeds (url,name,hashtags) VALUES(?,?,?)",
                   (url, name or (_up(url).hostname or url), hashtags))
        db.commit()
        fid = db.execute("SELECT id FROM feeds WHERE url=?", (url,)).fetchone()["id"]
        for aid in accs:
            try:
                db.execute("INSERT INTO feed_accounts (feed_id,account_id) VALUES(?,?)",
                           (fid, int(aid)))
            except Exception:
                pass
        db.commit()
        flash("Feed added.")
    except sqlite3.IntegrityError:
        flash("That feed URL already exists.", "er")
    return redirect(url_for("feeds"))


@app.route("/feeds/edit/<int:fid>", methods=["GET","POST"])
@admin_required
def feeds_edit(fid: int):
    db = get_db()
    fo = db.execute("SELECT * FROM feeds WHERE id=?", (fid,)).fetchone()
    if not fo:
        flash("Feed not found.", "er")
        return redirect(url_for("feeds"))
    fo = dict(fo)

    if request.method == "POST":
        if not ok_csrf():
            flash("Security error.", "er")
            return redirect(url_for("feeds"))
        name     = request.form.get("name","").strip()
        url      = request.form.get("url","").strip()
        hashtags = request.form.get("hashtags","").strip()
        accs     = request.form.getlist("accounts")

        if not url.startswith(("http://","https://")):
            flash("Invalid URL.", "er")
        else:
            db.execute("UPDATE feeds SET name=?,url=?,hashtags=? WHERE id=?",
                       (name, url, hashtags, fid))
            db.execute("DELETE FROM feed_accounts WHERE feed_id=?", (fid,))
            for aid in accs:
                try:
                    db.execute("INSERT INTO feed_accounts (feed_id,account_id) VALUES(?,?)",
                               (fid, int(aid)))
                except Exception:
                    pass
            db.commit()
            flash("Feed updated.")
            return redirect(url_for("feeds"))

    cur_ids = [r["account_id"] for r in
               db.execute("SELECT account_id FROM feed_accounts WHERE feed_id=?", (fid,)).fetchall()]
    accs    = [dict(r) for r in db.execute(
        "SELECT * FROM mastodon_accounts WHERE access_token!='' ORDER BY id"
    ).fetchall()]
    return render("feeds_edit", feed_obj=fo, all_accounts=accs, feed_account_ids=cur_ids)


@app.route("/feeds/toggle/<int:fid>")
@admin_required
def feeds_toggle(fid: int):
    db = get_db()
    db.execute("UPDATE feeds SET active=1-active WHERE id=?", (fid,))
    db.commit()
    return redirect(url_for("feeds"))


@app.route("/feeds/delete/<int:fid>")
@admin_required
def feeds_delete(fid: int):
    db = get_db()
    db.execute("DELETE FROM feeds WHERE id=?", (fid,))
    db.commit()
    flash("Feed deleted.")
    return redirect(url_for("feeds"))


# ─── Mastodon Accounts ────────────────────────────────────────────────────────

@app.route("/accounts")
@admin_required
def accounts_page():
    db   = get_db()
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM mastodon_accounts WHERE access_token!='' ORDER BY id"
    ).fetchall()]
    for a in rows:
        a["feed_count"] = db.execute(
            "SELECT COUNT(*) FROM feed_accounts WHERE account_id=?", (a["id"],)
        ).fetchone()[0]
        a["post_count"] = db.execute(
            "SELECT COUNT(*) FROM posted_items WHERE account_id=?", (a["id"],)
        ).fetchone()[0]
    return render("accounts", accounts=rows)


@app.route("/accounts/connect", methods=["POST"])
@admin_required
def accounts_connect():
    if not ok_csrf():
        flash("Security error.", "er")
        return redirect(url_for("accounts_page"))

    instance = request.form.get("instance","").strip().rstrip("/")
    if not instance.startswith(("http://","https://")):
        flash("Invalid instance URL.", "er")
        return redirect(url_for("accounts_page"))

    try:
        resp     = requests.post(f"{instance}/api/v1/apps", data={
            "client_name":   MASTODON_APP_NAME,
            "redirect_uris": url_for("oauth_callback", _external=True),
            "scopes":        "write:statuses read:accounts",
            "website":       url_for("dashboard", _external=True),
        }, timeout=15)
        app_data = resp.json()
    except Exception as exc:
        flash(f"App registration failed: {exc}", "er")
        return redirect(url_for("accounts_page"))

    if not app_data.get("client_id"):
        flash(f"Registration error: {app_data.get('error', resp.text[:80])}", "er")
        return redirect(url_for("accounts_page"))

    state = secrets.token_hex(20)
    cfg_set(f"oauth_{state}", json.dumps({
        "instance":      instance,
        "client_id":     app_data["client_id"],
        "client_secret": app_data["client_secret"],
    }))

    auth_url = f"{instance}/oauth/authorize?" + urlencode({
        "client_id":     app_data["client_id"],
        "redirect_uri":  url_for("oauth_callback", _external=True),
        "response_type": "code",
        "scope":         "write:statuses read:accounts",
        "state":         state,
    })
    return redirect(auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    code  = request.args.get("code","")
    state = request.args.get("state","")

    pending_json = cfg_get(f"oauth_{state}")
    if not code or not pending_json:
        flash("OAuth error: invalid state.", "er")
        return redirect(url_for("accounts_page"))

    try:
        pending = json.loads(pending_json)
    except Exception:
        flash("OAuth error: corrupted state.", "er")
        return redirect(url_for("accounts_page"))

    try:
        tok = requests.post(f"{pending['instance']}/oauth/token", data={
            "client_id":     pending["client_id"],
            "client_secret": pending["client_secret"],
            "redirect_uri":  url_for("oauth_callback", _external=True),
            "grant_type":    "authorization_code",
            "code":          code,
            "scope":         "write:statuses read:accounts",
        }, timeout=15).json()
    except Exception as exc:
        flash(f"Token exchange failed: {exc}", "er")
        return redirect(url_for("accounts_page"))

    if not tok.get("access_token"):
        flash(f"Token error: {tok.get('error','unknown')}", "er")
        return redirect(url_for("accounts_page"))

    try:
        acct = requests.get(
            f"{pending['instance']}/api/v1/accounts/verify_credentials",
            headers={"Authorization": f"Bearer {tok['access_token']}"},
            timeout=10,
        ).json()
    except Exception:
        acct = {}

    account_name = "@" + acct.get("acct","unknown")
    account_url  = acct.get("url","")

    conn = open_db()
    try:
        existing = conn.execute(
            "SELECT id FROM mastodon_accounts WHERE instance_url=? AND account_name=?",
            (pending["instance"], account_name)
        ).fetchone()
        if existing:
            conn.execute("""UPDATE mastodon_accounts
                SET client_id=?,client_secret=?,access_token=?,account_url=?
                WHERE id=?""",
                (pending["client_id"], pending["client_secret"],
                 tok["access_token"], account_url, existing["id"]))
            conn.commit()
            flash(f"Re-authorized {account_name} on {urlparse(pending['instance']).hostname}.")
        else:
            conn.execute("""INSERT INTO mastodon_accounts
                (instance_url,client_id,client_secret,access_token,account_name,account_url)
                VALUES(?,?,?,?,?,?)""",
                (pending["instance"], pending["client_id"], pending["client_secret"],
                 tok["access_token"], account_name, account_url))
            conn.commit()
            flash(f"✅ Connected {account_name} on {urlparse(pending['instance']).hostname}!")
        # Clean up state key
        conn.execute("DELETE FROM settings WHERE key=?", (f"oauth_{state}",))
        conn.commit()
    finally:
        conn.close()

    session["admin"] = True
    return redirect(url_for("accounts_page"))


@app.route("/accounts/disconnect/<int:aid>")
@admin_required
def accounts_disconnect(aid: int):
    conn = open_db()
    try:
        conn.execute("DELETE FROM mastodon_accounts WHERE id=?", (aid,))
        conn.commit()
    finally:
        conn.close()
    flash("Account disconnected.")
    return redirect(url_for("accounts_page"))


# ─── Manual run ───────────────────────────────────────────────────────────────

@app.route("/run/now")
@admin_required
def run_now():
    threading.Thread(target=run_job, kwargs={"triggered_by":"manual"}, daemon=True).start()
    flash("Manual run started — check the Run Log in a few seconds.")
    return redirect(url_for("dashboard"))


# ─── Run log ──────────────────────────────────────────────────────────────────

@app.route("/log")
@admin_required
def run_log():
    db   = get_db()
    rows = db.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT 100").fetchall()
    return render("log", run_logs=[dict(r) for r in rows])


@app.route("/log/clear")
@admin_required
def log_clear():
    db = get_db()
    db.execute("DELETE FROM run_log")
    db.commit()
    flash("Run log cleared.")
    return redirect(url_for("run_log"))


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    init_schema()
    app.secret_key = get_or_create_secret()
    start_scheduler()
    logger.info("Starting %s v%s  →  http://%s:%d", APP_NAME, APP_VERSION, WEB_HOST, WEB_PORT)
    logger.info("Login: %s / %s", ADMIN_USERNAME, ADMIN_PASSWORD)
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()