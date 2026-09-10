#!/usr/bin/env python3
"""
TikTok + YouTube + Facebook Video Downloader Telegram Bot (v3.1, production-ready)
==================================================================================

Single-file bot using python-telegram-bot (v20+, async) and yt-dlp.

v3 architectural refactor (the 5 requested issues):
    1. NON-BLOCKING I/O
       - All async-context file I/O uses `aiofiles` (cookie upload/save).
       - Heavy/blocking work (yt-dlp, file size stat, temp-dir cleanup)
         is always wrapped in `asyncio.to_thread(...)` so the PTB event
         loop is never blocked.
    2. SQLite STORAGE (aiosqlite)
       - Replaced the old JSON files (stats.json / users.json) with an
         async SQLite database:
             * users       -> user data + bans
             * settings    -> admin settings (admin id, limits)
             * rate_limits -> persistent per-user rate limiting
             * downloads   -> download log used for /stats
    3. STRICT TEMP-FILE MANAGEMENT
       - Every download gets its own unique subdirectory under ./downloads
         and the ENTIRE download+upload path runs inside a single
         try...finally that `shutil.rmtree`s the directory no matter what
         happens (success, network error, timeout, or exception).
    4. STRICT URL & INPUT VALIDATION
       - Input is checked with a deny-list for shell/injection characters
         and then parsed with urllib.urlparse + a strict host allow-list
         (tiktok.com / youtube.com / youtu.be / facebook.com / fb.watch /
         fb.com). Unknown hosts, IPs, localhost, credentials, and odd ports
         are all rejected before anything reaches yt-dlp.
    5. MEMORY-EFFICIENT UPLOADING
       - send_video()/send_audio() receive the on-disk *path* (str), not a
         bytes object, so python-telegram-bot streams the file from disk to
         Telegram instead of reading the whole file into RAM.

v3.1 UI/UX + admin-visibility additions:
    - Download progress bar shown immediately (0%) and updated with ETA.
    - Premium custom emojis in the welcome message and the /users list.
    - Admin commands are hidden from regular users via BotCommandScope:
      regular users only see /start /help /myid, while the admin sees the
      full command set in the chat menu.

All admin commands (/admin /broadcast /banned /stats /users /ban /unban
/setcookies /config /setadmin /setmaxsize /setratelimit /ping) and user
features are preserved.

Setup (Ubuntu VPS):
    pip install python-telegram-bot yt-dlp aiofiles aiosqlite python-dotenv
    sudo apt install ffmpeg

    1. Copy .env.example to .env and set BOT_TOKEN / ADMIN_ID.
    2. python bot_v2.py
"""

import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
import aiosqlite
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import yt_dlp

# ----------------------------------------------------------------------------
# CONFIG (env vars override defaults; DB "settings" table can override at runtime)
# ----------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or "8953839870:AAG5PBpFq68FaooPorS16sJPb9q-A_vw6Hs"

# Admin ID is loaded from ADMIN_ID env, else from the SQLite `settings` table
# (so it can be changed at runtime with /setadmin without editing code).
ADMIN_ID = int(os.getenv("ADMIN_ID") or "0") or None

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB") or "50")   # Telegram Bot API hard limit
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS") or "10")
PROGRESS_UPDATE_INTERVAL = 4     # seconds between progress-message edits
DOWNLOAD_TIMEOUT = 900           # 15 minutes max per download
RETRY_MAX_ATTEMPTS = 3           # 1 initial try + 2 retries
RETRY_BASE_DELAY = 2.0           # seconds; doubles each retry (2s, 4s)

SCRIPT_DIR = Path(__file__).resolve().parent
TEMP_DIR = SCRIPT_DIR / "downloads"
DATA_DIR = SCRIPT_DIR / "data"
DB_PATH = DATA_DIR / "bot.db"

# ----------------------------------------------------------------------------
# Premium custom emoji IDs (Telegram premium animated/custom emoji).
# Rendered with <tg-emoji emoji-id="...">fallback</tg-emoji> in HTML mode.
# ----------------------------------------------------------------------------
EMOJI_WINK = "5960938367989323889"     # 😉  (welcome message)
EMOJI_DISC = "4938653911507534983"     # 🥏  (/users list, before @username)

# ----------------------------------------------------------------------------
# Cookie runtime files (TikTok / Facebook cookies updated live via Telegram)
# ----------------------------------------------------------------------------
COOKIE_RUNTIME_FILES = {
    "tiktok": SCRIPT_DIR / "tiktok_cookies_runtime.txt",
    "facebook": SCRIPT_DIR / "facebook_cookies_runtime.txt",
}

# Fallback cookies baked into the script (Netscape format). Leave "" to skip.
TIKTOK_COOKIES = """# Netscape HTTP Cookie File
# https://curl.haxx.se/rfc/cookie_spec.html
# This is a generated file! Do not edit.

.tiktok.com	TRUE	/	TRUE	1822483705	_ttp	3D6h52nGd9CeuQOrVPfq3XjD0VZ
.tiktok.com	TRUE	/	TRUE	0	tt_csrf_token	JOeXeD44-spA89Pup5Y9VDEtV_ZIZ96m_tNs
.tiktok.com	TRUE	/	TRUE	1804394467	tt_chain_token	42G1ielbKmlaJotoPUAo0w==
www.tiktok.com	FALSE	/	FALSE	0	x-web-secsdk-uid	2924af37-7722-4b8c-b73d-79288d9d9e50
.www.tiktok.com	TRUE	/	TRUE	1814762468	tiktok_webapp_theme_source	auto
.www.tiktok.com	TRUE	/	TRUE	1814762468	tiktok_webapp_theme	dark
.www.tiktok.com	TRUE	/	TRUE	1814762424	delay_guest_mode_vid	5
www.tiktok.com	FALSE	/	FALSE	1804394427	g_state	{"i_l":0,"i_ll":1788842427729,"i_b":"M0O/7OCJT87Ziaj4noOgf2s0DuZuTW3FEju0BULYZMg","i_e":{"enable_itp_optimization":24},"i_et":1788842427729}
.tiktok.com	TRUE	/	TRUE	0	s_v_web_id	verify_mts6lstm_H1HHuiOn_Nt5K_4wU6_9zF2_nrmmq1vdz7zW
.tiktok.com	TRUE	/	TRUE	1794026457	multi_sids	7683019600871998485%3Ae589ac32e890bd690ebfdb3d8933d9dc
.tiktok.com	TRUE	/	TRUE	1794026457	cmpl_token	AgQYAPOw_hfkTtK6oX85aGQdLPD-CJ8CVT-FO2Cldew
.tiktok.com	TRUE	/	FALSE	1791434457	passport_auth_status	fb1d508e091e48daf6613c81f3cf09d1%2C
.tiktok.com	TRUE	/	TRUE	1791434457	passport_auth_status_ss	fb1d508e091e48daf6613c81f3cf09d1%2C
.tiktok.com	TRUE	/	TRUE	1819946457	sid_guard	e589ac32e890bd690ebfdb3d8933d9dc%7C1788842454%7C15552000%7CSun%2C+07-Mar-2027+04%3A40%3A54+GMT
.tiktok.com	TRUE	/	TRUE	1804394457	uid_tt	61955faaf48c572e6999cf4abae165c2b16197fcb61d0c806c2fa8c43a2f752f
.tiktok.com	TRUE	/	TRUE	1804394457	uid_tt_ss	61955faaf48c572e6999cf4abae165c2b16197fcb61d0c806c2fa8c43a2f752f
.tiktok.com	TRUE	/	TRUE	1804394457	sid_tt	e589ac32e890bd690ebfdb3d8933d9dc
.tiktok.com	TRUE	/	TRUE	1804394457	sessionid	e589ac32e890bd690ebfdb3d8933d9dc
.tiktok.com	TRUE	/	TRUE	1804394457	sessionid_ss	e589ac32e890bd690ebfdb3d8933d9dc
.tiktok.com	TRUE	/	TRUE	1804394457	tt_session_tlb_tag	sttt%7C2%7C5YmsMuiQvWkOv9s9iTPZ3P_________AR925Jip38fcGqccIPEBAFr1yVDqkEskoHGuAuWEIryA%3D
.tiktok.com	TRUE	/	TRUE	1804394457	sid_ucp_v1	1.0.1-KDljMmJhYjQ4OGU3YmU5ZGMxM2VmZTY1YmY5NzhhZWRmMjY3N2RkNDAKIQiViNuC7rPkz2oQ1qP-1AYYswsgDDCxo_7UBjgIQBJIBBADGgNteTIiIGU1ODlhYzMyZTg5MGJkNjkwZWJmZGIzZDg5MzNkOWRjMk4KIEaDoDdSqaaGTQbB17ydGLBnK_sHQgTUtHIT3mhNeGKmEiD-BR2IW4wqiO6fFJlQE1k66FXa_RAp__ZP5ClxTCuLMBgDIgZ0aWt0b2s
.tiktok.com	TRUE	/	TRUE	1804394457	ssid_ucp_v1	1.0.1-KDljMmJhYjQ4OGU3YmU5ZGMxM2VmZTY1YmY5NzhhZWRmMjY3N2RkNDAKIQiViNuC7rPkz2oQ1qP-1AYYswsgDDCxo_7UBjgIQBJIBBADGgNteTIiIGU1ODlhYzMyZTg5MGJkNjkwZWJmZGIzZDg5MzNkOWRjMk4KIEaDoDdSqaaGTQbB17ydGLBnK_sHQgTUtHIT3mhNeGKmEiD-BR2IW4wqiO6fFJlQE1k66FXa_RAp__ZP5ClxTCuLMBgDIgZ0aWt0b2s
.tiktok.com	TRUE	/	FALSE	1804394457	store-idc	alisg
.tiktok.com	TRUE	/	FALSE	1804394457	store-country-code	mm
.tiktok.com	TRUE	/	FALSE	1804394457	store-country-code-src	uid
.tiktok.com	TRUE	/	FALSE	1804394457	tt-target-idc	alisg
.tiktok.com	TRUE	/	FALSE	1820378472	tt-target-idc-sign	LPttts-qaKbJOpuR2PKW3343hWVhp0VtmxchXiekShkvH_rjD__3Yu-1CSMwwU6b8tTErHRkyHTRKL2Z_wGbEyXw4ZKSMAwTJbzoIx9XI86Dd8fumKi0dun3-8rnwPIdFvVg8nC6WkforGHgVipfLBhpqj8AMGE52X0iYIvEnkmsa78pTzCHA1stmJ7Bs37UOjIKGeN7C2BeQo0uU-WSp4PosiPWQk14e-w-epDkVGJTCsVW88nYQKe_VSDQtsfYRyN0L7YTUy7T10HFLFMOEKQ0huwD1vuD1S6eI3yTGiytRrp1bOgfKhgUwDYcJ3uBnyo2ZZ4PtZNgWibv3SMKhz4TPUBN6JA0falrgExgEN47CgpxyXjpWiWqVu6CvVmdBw2bd8W8AMH8NK4QvdgvVBrRb3tSjFE9l5etnlrdF4X8o6TAG19GhEWrB9x-aIOpoQGaspWk1UlX_l5A2Cz2eJfoFhRsjNmFii0zqLowN_WOeJhpRwP0OSplhjV_PCMQ
www.tiktok.com	FALSE	/	FALSE	1796618457	last_login_method	google
.www.tiktok.com	TRUE	/	FALSE	0	passport_fe_beating_status	true
www.tiktok.com	FALSE	/	FALSE	1788928872	tt_ticket_guard_has_set_public_key	1
.tiktok.com	TRUE	/	TRUE	1820378472	ttwid	1%7CS09nPdSDvdZZqeIF19yqjaWMDx938C-d6lL6QkRGGLM%7C1788842469%7Ce40e8db6a2f555711b19d4358a7ab9b8ee662b0de26fd25664b17267c739d7f9
www.tiktok.com	FALSE	/	FALSE	1796618473	msToken	7De3jYgO-BnDC1_iwPwIqDHZLOc-v4o9k0U5q3MeHzB2O3vqamXa_U7TmN9c4iPRZpOGpd8cyWSFodlkE0olQ8JUE9BQHN74IsNDKPSFuTV2DEieD8irX0SP4-U6eMjjXnMG08iS8nTKGbOonQwpwg_M826UDIt45EBOz9Ev
.www.tiktok.com	TRUE	/	TRUE	1789447274	perf_feed_cache	{%22expireTimestamp%22:1789444800000%2C%22itemIds%22:[%227678097179499564309%22%2C%227658863978981903623%22%2C%227677888267508911380%22]}
.tiktok.com	TRUE	/	FALSE	1804394457	store-country-sign	MEIEDFj-TmxCFrIKxMkJDgQgwXUC6fmEPIe2siM6jMHkpZwM81V5EGII0zgvOMi2k-QEEOmun7dyVAQ5q5g09t0Fl6I
.tiktok.com	TRUE	/	FALSE	1820378483	odin_tt	9f8b895bda2c3e2f19ee511240058bdb4dc620d65bd1051898de9bd421dc3e9dc18a950e022f1254dde2cf149add42728c1a4325f4f980e92de8910ddb7858f40848c4603f50aa2c616871acdcad691d
.tiktok.com	TRUE	/	TRUE	1789706533	msToken	WhwxiZiDwrBW1zgq1nmulGMiZXcp4X5SSBqQ5ddgQ5IerSocdMXPv-DL8urRmlAfiCoDLqkOX3-MFv_QY1Fav7092ZvFps1IliJmei1KzuRKBsFYMHInhVZD6M2nsbFt3q-usc15nRz0m9KDOFVFf9eGPdoLEwJRTCRRKsFi
"""

FACEBOOK_COOKIES = ""

# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

PLATFORM_NAMES = {"tiktok": "TikTok", "youtube": "YouTube", "facebook": "Facebook"}

# One-time startup directory creation (negligible, runs before polling).
TEMP_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)


# ----------------------------------------------------------------------------
# SQLite storage (aiosqlite) — replaces the old JSON files entirely.
# ----------------------------------------------------------------------------
_db_conn: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    downloads   INTEGER NOT NULL DEFAULT 0,
    banned      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limits (
    user_id      INTEGER PRIMARY KEY,
    last_request REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS downloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    platform    TEXT NOT NULL,
    mode        TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    success     INTEGER NOT NULL DEFAULT 1,
    ts          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_downloads_ts   ON downloads(ts);
CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads(user_id);
"""


async def init_db() -> None:
    """Create the DB connection + schema and load persisted settings."""
    global _db_conn
    _db_conn = await aiosqlite.connect(DB_PATH)
    _db_conn.row_factory = aiosqlite.Row
    # WAL improves concurrent read/write and survives crashes better than the
    # default rollback journal.
    await _db_conn.execute("PRAGMA journal_mode=WAL")
    await _db_conn.execute("PRAGMA synchronous=NORMAL")
    await _db_conn.executescript(SCHEMA)
    await _db_conn.commit()
    await load_settings()

    # The rate limiter must be (re)built after the cooldown setting is known.
    global rate_limiter
    rate_limiter = RateLimiter(RATE_LIMIT_SECONDS, _db_conn)


async def close_db() -> None:
    global _db_conn
    if _db_conn is not None:
        await _db_conn.close()
        _db_conn = None


async def _fetchone(sql: str, params: tuple = ()):
    cur = await _db_conn.execute(sql, params)
    return await cur.fetchone()


async def _fetchall(sql: str, params: tuple = ()):
    cur = await _db_conn.execute(sql, params)
    return await cur.fetchall()


async def get_setting(key: str) -> str | None:
    row = await _fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else None


async def set_setting(key: str, value) -> None:
    await _db_conn.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    await _db_conn.commit()


async def load_settings() -> None:
    """Load runtime config from env (highest priority) then DB settings."""
    global ADMIN_ID, MAX_FILE_SIZE_MB, RATE_LIMIT_SECONDS

    # Admin ID
    if os.getenv("ADMIN_ID"):
        ADMIN_ID = int(os.getenv("ADMIN_ID"))
        await set_setting("admin_id", str(ADMIN_ID))
    else:
        v = await get_setting("admin_id")
        if v:
            ADMIN_ID = int(v)

    # Max file size
    if os.getenv("MAX_FILE_SIZE_MB"):
        MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB"))
    else:
        v = await get_setting("max_file_size_mb")
        if v:
            MAX_FILE_SIZE_MB = int(v)

    # Rate limit
    if os.getenv("RATE_LIMIT_SECONDS"):
        RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS"))
    else:
        v = await get_setting("rate_limit_seconds")
        if v:
            RATE_LIMIT_SECONDS = float(v)


# ----------------------------------------------------------------------------
# User / stats / ban helpers (all async, all backed by SQLite)
# ----------------------------------------------------------------------------
async def record_user(user_id: int, username: str | None) -> None:
    now = int(time.time())
    await _db_conn.execute(
        "INSERT INTO users(user_id, username, first_seen, last_seen, downloads, banned) "
        "VALUES(?,?,?,?,0,0) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "  username=COALESCE(excluded.username, users.username), "
        "  last_seen=excluded.last_seen",
        (user_id, username, now, now),
    )
    await _db_conn.commit()


async def record_download(user_id: int, platform: str, mode: str, size_bytes: int, success: bool = True) -> None:
    now = int(time.time())
    await _db_conn.execute(
        "INSERT INTO downloads(user_id, platform, mode, size_bytes, success, ts) "
        "VALUES(?,?,?,?,?,?)",
        (user_id, platform, mode, size_bytes, 1 if success else 0, now),
    )
    await _db_conn.execute(
        "UPDATE users SET downloads = downloads + 1, last_seen = ? WHERE user_id = ?",
        (now, user_id),
    )
    await _db_conn.commit()


async def is_banned(user_id: int) -> bool:
    row = await _fetchone("SELECT banned FROM users WHERE user_id=?", (user_id,))
    return bool(row and row["banned"])


async def set_ban(user_id: int, banned: bool) -> None:
    now = int(time.time())
    await _db_conn.execute(
        "INSERT INTO users(user_id, username, first_seen, last_seen, downloads, banned) "
        "VALUES(?,NULL,?,?,0,?) "
        "ON CONFLICT(user_id) DO UPDATE SET banned=excluded.banned",
        (user_id, now, now, 1 if banned else 0),
    )
    await _db_conn.commit()


async def get_stats() -> dict:
    """Aggregate stats for the /stats command."""
    now_dt = datetime.now()
    midnight_ts = datetime(now_dt.year, now_dt.month, now_dt.day).timestamp()

    async def scalar(sql, params=()):
        row = await _fetchone(sql, params)
        return row[0] if row else 0

    downloads_today = await scalar("SELECT COUNT(*) FROM downloads WHERE ts >= ?", (midnight_ts,))
    unique_today = await scalar("SELECT COUNT(DISTINCT user_id) FROM downloads WHERE ts >= ?", (midnight_ts,))
    total_downloads = await scalar("SELECT COUNT(*) FROM downloads")
    total_users = await scalar("SELECT COUNT(*) FROM users")
    banned = await scalar("SELECT COUNT(*) FROM users WHERE banned=1")
    rate_mem = await scalar("SELECT COUNT(*) FROM rate_limits")

    return {
        "downloads_today": downloads_today,
        "unique_today": unique_today,
        "total_downloads": total_downloads,
        "total_users": total_users,
        "banned": banned,
        "rate_mem": rate_mem,
    }


async def get_top_users(limit: int = 50):
    return await _fetchall(
        "SELECT user_id, username, downloads, banned FROM users "
        "ORDER BY downloads DESC LIMIT ?",
        (limit,),
    )


async def get_banned_users():
    return await _fetchall(
        "SELECT user_id, username FROM users WHERE banned=1 ORDER BY user_id"
    )


async def get_all_user_ids() -> list[int]:
    rows = await _fetchall("SELECT user_id FROM users")
    return [r["user_id"] for r in rows]


# ----------------------------------------------------------------------------
# Rate limiter — persisted in SQLite so it survives bot restarts.
# ----------------------------------------------------------------------------
class RateLimiter:
    """Per-user rate limiter backed by the `rate_limits` SQLite table."""

    def __init__(self, cooldown: float, conn: aiosqlite.Connection) -> None:
        self.cooldown = cooldown
        self._conn = conn
        self._lock = asyncio.Lock()  # serialize check-and-set to avoid races

    async def is_allowed(self, user_id: int) -> bool:
        async with self._lock:
            now = time.time()
            row = await _fetchone(
                "SELECT last_request FROM rate_limits WHERE user_id=?", (user_id,)
            )
            if row and row["last_request"] and (now - row["last_request"]) < self.cooldown:
                return False
            await self._conn.execute(
                "INSERT INTO rate_limits(user_id, last_request) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_request=excluded.last_request",
                (user_id, now),
            )
            await self._conn.commit()
            return True

    async def remaining(self, user_id: int) -> float:
        row = await _fetchone(
            "SELECT last_request FROM rate_limits WHERE user_id=?", (user_id,)
        )
        if not row or not row["last_request"]:
            return 0.0
        return max(0.0, self.cooldown - (time.time() - row["last_request"]))

    async def cleanup(self, max_age: float = 3600.0) -> int:
        """Delete rows older than max_age seconds (called periodically)."""
        cutoff = time.time() - max_age
        cur = await self._conn.execute(
            "DELETE FROM rate_limits WHERE last_request < ?", (cutoff,)
        )
        await self._conn.commit()
        return cur.rowcount


rate_limiter: RateLimiter | None = None


# ----------------------------------------------------------------------------
# Inline keyboards
# ----------------------------------------------------------------------------
def choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎬 Video", callback_data="dl:video"),
                InlineKeyboardButton("🎵 Audio only", callback_data="dl:audio"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")]]
    )


# ----------------------------------------------------------------------------
# STRICT URL validation (issue #4) — deny-list + urlparse + host allow-list.
# ----------------------------------------------------------------------------
# Characters that have no legitimate place in our supported URLs and could be
# used for command injection / SSRF tricks if ever passed to a subprocess.
_DANGEROUS_URL_CHARS = re.compile(r"[\s'\"`;|<>\\$]")


def _platform_for_host(host: str) -> str | None:
    """Map a validated hostname to a supported platform (strict allow-list)."""
    if host == "youtu.be" or host.endswith(".youtu.be"):
        return "youtube"
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "tiktok"
    if host == "youtube.com" or host.endswith(".youtube.com"):
        return "youtube"
    if host == "facebook.com" or host.endswith(".facebook.com"):
        return "facebook"
    if host == "fb.watch" or host.endswith(".fb.watch"):
        return "facebook"
    if host == "fb.com" or host.endswith(".fb.com"):
        return "facebook"
    return None


def validate_url(text: str) -> tuple[str | None, str | None]:
    """
    Strictly validate user input as a supported video URL.

    Returns (platform, normalized_url) or (None, None). Rejects anything that
    isn't an http/https URL on an allow-listed host (no IPs, no localhost, no
    credentials, no weird ports, no whitespace/shell metacharacters).
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 2048:
        return None, None
    if _DANGEROUS_URL_CHARS.search(raw):
        return None, None

    try:
        parsed = urlparse(raw)
    except ValueError:
        return None, None

    if parsed.scheme.lower() not in ("http", "https"):
        return None, None
    if parsed.username or parsed.password:
        return None, None
    if parsed.port is not None and parsed.port not in (80, 443):
        return None, None

    host = (parsed.hostname or "").lower()
    if not host:
        return None, None

    platform = _platform_for_host(host)
    if not platform:
        return None, None

    return platform, parsed.geturl()


def get_active_cookies(platform: str) -> str:
    """Read active cookies (sync — called inside a worker thread only)."""
    runtime_file = COOKIE_RUNTIME_FILES.get(platform)
    if runtime_file and runtime_file.exists():
        try:
            content = runtime_file.read_text(encoding="utf-8")
            if content.strip():
                return content
        except OSError:
            pass
    return TIKTOK_COOKIES if platform == "tiktok" else FACEBOOK_COOKIES


# ----------------------------------------------------------------------------
# Error formatting — platform-specific, user-friendly messages
# ----------------------------------------------------------------------------
def _format_error(platform: str, error: str | None) -> str:
    pname = PLATFORM_NAMES.get(platform, "video")
    s = (error or "").lower()

    if "cancelled" in s or "cancel" in s:
        return "🚫 Download cancelled."

    if any(m in s for m in ("private", "login required", "sign in", "members-only", "premium")):
        return (
            f"🔒 This {pname} video is *private* or requires sign-in, "
            "so I can't download it. Try a public link."
        )

    if any(m in s for m in ("age", "confirm your age", "18+", "adult")):
        return (
            f"🔞 This {pname} video is *age-restricted*. "
            "I can't download age-restricted content."
        )

    if any(
        m in s
        for m in (
            "geo",
            "region",
            "country",
            "not available in your",
            "unavailable in your",
            "blocked in your",
        )
    ):
        return (
            f"🌍 This {pname} video is *geo-blocked* and isn't available "
            "in your region."
        )

    if any(m in s for m in ("deleted", "not found", "does not exist", "unavailable", "removed", "no video")):
        return (
            f"🗑️ This {pname} video has been *deleted* or is no longer available."
        )

    if "copyright" in s:
        return (
            f"⚖️ This {pname} video was *removed due to copyright*, "
            "so I can't download it."
        )

    if any(m in s for m in ("timed out", "timeout")):
        return (
            f"⏱️ The {pname} server took too long to respond. "
            "Please try again in a moment."
        )

    if any(m in s for m in ("too large", "filesize", "file is larger")):
        return (
            f"📦 This {pname} video is larger than Telegram's "
            f"{MAX_FILE_SIZE_MB} MB limit."
        )

    detail = (error or "")[:200].replace("`", "'")
    return (
        f"❌ Sorry, I couldn't download that {pname} video.\n\n"
        f"`{detail}`"
    )


def _is_transient(error: str | None) -> bool:
    """True for network/timeout errors worth retrying (not permanent ones)."""
    s = (error or "").lower()
    markers = (
        "timed out",
        "timeout",
        "connection",
        "network",
        "temporarily",
        "http error 5",
        "http error 429",
        "429",
        "502",
        "503",
        "504",
        "unreachable",
        "reset by peer",
        "broken pipe",
        "ssl",
        "connection reset",
        "curl error",
        "socket",
        "remote end closed",
        "eof",
    )
    return any(m in s for m in markers)


# ----------------------------------------------------------------------------
# yt-dlp download — a SYNCHRONOUS function run inside asyncio.to_thread().
# ----------------------------------------------------------------------------
class DownloadCancelled(Exception):
    """Raised from a progress hook to abort an in-flight download."""


def _resolve_filepath(ydl, info, out_dir: str) -> str:
    """Return the final file path, accounting for merge/audio post-processing."""
    filepath = ydl.prepare_filename(info)
    if os.path.exists(filepath):
        return filepath
    base = os.path.splitext(filepath)[0]
    wanted = {".mp3", ".mp4", ".m4a", ".webm", ".mkv"}
    for f in os.listdir(out_dir):
        if f.startswith(os.path.basename(base)) and os.path.splitext(f)[1].lower() in wanted:
            return os.path.join(out_dir, f)
    return filepath


def download_video(
    url: str,
    platform: str,
    out_dir: str,
    progress_state: dict | None = None,
    cancel_event: threading.Event | None = None,
    audio_only: bool = False,
) -> tuple[str | None, str | None]:
    """Download a video (or audio) using yt-dlp. Runs in a worker thread.

    Returns (filepath, None) on success, (None, error_message) on failure.
    """
    ydl_opts = {
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if audio_only:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    elif platform == "youtube":
        ydl_opts["format"] = (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/best[ext=mp4]/best"
        )
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["extractor_args"] = {
            "youtube": {"player_client": ["android"]},
        }
    elif platform == "facebook":
        ydl_opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        ydl_opts["merge_output_format"] = "mp4"
    else:  # tiktok
        ydl_opts["format"] = "best[ext=mp4]/best"
        ydl_opts["merge_output_format"] = "mp4"

    if progress_state is not None:
        def _progress_hook(d: dict) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("Download cancelled by user")
            status = d.get("status")
            if status == "downloading":
                progress_state["status"] = "downloading"
                progress_state["downloaded"] = d.get("downloaded_bytes") or 0
                progress_state["total"] = (
                    d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                )
                progress_state["speed"] = d.get("speed") or 0
                progress_state["eta"] = d.get("eta") or 0
            elif status == "finished":
                progress_state["status"] = "merging"
            elif status == "error":
                progress_state["status"] = "error"

        ydl_opts["progress_hooks"] = [_progress_hook]

    tmp_cookie_path = None
    if platform in ("tiktok", "facebook"):
        active_cookies = get_active_cookies(platform)
        if active_cookies.strip():
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            )
            tmp.write(active_cookies)
            tmp.close()
            tmp_cookie_path = tmp.name
            ydl_opts["cookiefile"] = tmp_cookie_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = _resolve_filepath(ydl, info, out_dir)
            return filepath, None
    except DownloadCancelled:
        logger.info("Download cancelled for %s", url)
        return None, "Download cancelled by user."
    except Exception as e:
        logger.error("Download failed for %s: %s", url, e)
        return None, str(e)
    finally:
        # Always remove the temporary cookie file.
        if tmp_cookie_path:
            try:
                os.remove(tmp_cookie_path)
            except OSError:
                pass


def download_with_retry(
    url: str,
    platform: str,
    out_dir: str,
    progress_state: dict,
    cancel_event: threading.Event,
    audio_only: bool,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
) -> tuple[str | None, str | None]:
    """Retry transient failures with exponential backoff (2s, 4s, ...)."""
    last_error = None
    for attempt in range(1, max_attempts + 1):
        if cancel_event.is_set():
            return None, "Download cancelled by user."

        filepath, error = download_video(
            url, platform, out_dir, progress_state, cancel_event, audio_only
        )
        if filepath:
            return filepath, None
        if error and "cancel" in error.lower():
            return None, error

        last_error = error
        if not _is_transient(error) or attempt >= max_attempts:
            return None, error

        delay = base_delay * (2 ** (attempt - 1))
        logger.warning(
            "Attempt %d/%d failed (%s); retrying in %.1fs",
            attempt, max_attempts, error, delay,
        )
        # time.sleep here only blocks the worker thread (run via to_thread),
        # never the asyncio event loop.
        time.sleep(delay)

    return None, last_error


# ----------------------------------------------------------------------------
# Progress updates (background task)
# ----------------------------------------------------------------------------
def _format_bytes(n: float) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def _build_progress_bar(percent: int, width: int = 12) -> str:
    percent = max(0, min(100, percent))
    filled = round(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def _format_eta(eta: float | int | None) -> str:
    """Format yt-dlp's `eta` (seconds) into a short human string."""
    if not eta:
        return ""
    eta = int(eta)
    if eta >= 60:
        return f"⏳ {eta // 60}m {eta % 60}s left"
    return f"⏳ {eta}s left"


async def run_progress_updates(
    status_message,
    progress_state: dict,
    stop_flag: dict,
    bot=None,
    chat_id=None,
    action=None,
) -> None:
    """Periodically refresh the status message + keep the chat action alive."""
    last_shown = None
    while not stop_flag["done"]:
        await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)
        if stop_flag["done"]:
            break

        status = progress_state.get("status")
        if status == "downloading":
            total = progress_state.get("total") or 0
            downloaded = progress_state.get("downloaded") or 0
            percent = int(downloaded / total * 100) if total else None
            shown_key = percent if percent is not None else f"raw:{downloaded // (512 * 1024)}"
            if shown_key == last_shown:
                continue
            last_shown = shown_key

            speed = progress_state.get("speed") or 0
            speed_str = f"{speed / (1024 * 1024):.1f} MB/s" if speed else "…"
            eta_str = _format_eta(progress_state.get("eta"))

            if percent is not None:
                bar = _build_progress_bar(percent)
                meta = f"📦 {_format_bytes(downloaded)} / {_format_bytes(total)}  •  ⚡ {speed_str}"
                if eta_str:
                    meta += f"  •  {eta_str}"
                text = f"⬇️ Downloading…\n{bar}  {percent}%\n{meta}"
            else:
                text = (
                    f"⬇️ Downloading…\n"
                    f"📦 {_format_bytes(downloaded)} downloaded so far  •  ⚡ {speed_str}"
                )

            # Keep Telegram's "uploading video" indicator alive during long
            # downloads (it auto-expires after ~5s otherwise).
            if bot and chat_id and action:
                try:
                    await bot.send_chat_action(chat_id=chat_id, action=action)
                except Exception:
                    pass

            try:
                await status_message.edit_text(text, reply_markup=cancel_keyboard())
            except Exception:
                pass

        elif status == "merging" and last_shown != "merging":
            last_shown = "merging"
            try:
                await status_message.edit_text(
                    "🔧 Processing / merging video…", reply_markup=cancel_keyboard()
                )
            except Exception:
                pass


# ----------------------------------------------------------------------------
# Pending downloads + cancel events (in-memory UI state only)
# ----------------------------------------------------------------------------
_pending: dict[int, dict] = {}
_cancel_events: dict[int, threading.Event] = {}


# ----------------------------------------------------------------------------
# Telegram handlers
# ----------------------------------------------------------------------------
def _is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID and user_id == ADMIN_ID)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """DM the admin about critical errors so manual log checks aren't needed."""
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(
            ADMIN_ID,
            "🚨 *Critical error*\n\n" + text[:3800],
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Failed to notify admin: %s", e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # HTML parse mode so we can use the premium custom emoji (<tg-emoji>).
    welcome = (
        "👋 <b>Welcome to Video Downloader Bot!</b>\n\n"
        "Send me a TikTok, YouTube, or Facebook link, pick <b>Video</b> or "
        "<b>Audio only</b>, and I'll send it straight back — no watermark.\n\n"
        "🎵 <b>Supported platforms:</b>\n"
        "• TikTok — tiktok.com, vm.tiktok.com, vt.tiktok.com\n"
        "• YouTube — youtube.com, youtu.be, Shorts\n"
        "• Facebook — facebook.com, fb.watch (videos & reels)\n\n"
        "Type /help for details and limits. "
        f'<tg-emoji emoji-id="{EMOJI_WINK}">😉</tg-emoji>'
    )
    await update.message.reply_text(welcome, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "📖 *How to use*\n\n"
        "1. Copy a TikTok, YouTube, or Facebook link.\n"
        "2. Send it here.\n"
        "3. Choose 🎬 Video or 🎵 Audio only.\n"
        "4. Wait — large videos can take a few minutes (❌ Cancel anytime).\n\n"
        "*Commands*\n"
        "/start — Welcome message\n"
        "/help — This help text\n"
        "/myid — Show your Telegram user ID\n\n"
        "⚠️ *Limits*\n"
        f"• Max file size: {MAX_FILE_SIZE_MB} MB (Telegram bot limit)\n"
        f"• Rate limit: 1 request per {RATE_LIMIT_SECONDS:.0f} seconds\n"
        "• Download timeout: up to 15 minutes for very large videos"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Your Telegram user ID: `{update.effective_user.id}`",
        parse_mode="Markdown",
    )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🏓 Pong! I'm alive.")


# ----------------------------------------------------------------------------
# Admin commands
# ----------------------------------------------------------------------------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    s = await get_stats()
    text = (
        "📊 *Bot statistics*\n\n"
        f"• Downloads today: *{s['downloads_today']}*\n"
        f"• Unique users today: *{s['unique_today']}*\n"
        f"• Total downloads (all time): *{s['total_downloads']}*\n"
        f"• Total known users: *{s['total_users']}*\n"
        f"• Banned users: *{s['banned']}*\n"
        f"• Rate-limiter rows: *{s['rate_mem']}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    rows = await get_top_users(50)
    if not rows:
        await update.message.reply_text("No users yet.")
        return
    # HTML mode to render the premium 🥏 custom emoji before each @username.
    lines = ["👥 <b>Top users (by downloads)</b>\n"]
    for r in rows:
        username = html.escape(r["username"] or "—")
        flag = " 🚫" if r["banned"] else ""
        lines.append(
            f'<code>{r["user_id"]}</code> — '
            f'<tg-emoji emoji-id="{EMOJI_DISC}">🥏</tg-emoji>'
            f'@{username} — {r["downloads"]} dl{flag}'
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ That's not a valid user ID.")
        return
    await set_ban(uid, True)
    await update.message.reply_text(f"🚫 Banned user `{uid}`.", parse_mode="Markdown")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ That's not a valid user ID.")
        return
    await set_ban(uid, False)
    await update.message.reply_text(f"✅ Unbanned user `{uid}`.", parse_mode="Markdown")


async def banned_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    rows = await get_banned_users()
    if not rows:
        await update.message.reply_text("No banned users.")
        return
    lines = ["🚫 *Banned users*\n"]
    for r in rows:
        username = r["username"] or "—"
        lines.append(f"`{r['user_id']}` — @{username}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    user_ids = await get_all_user_ids()
    sent = failed = 0
    status = await update.message.reply_text("📡 Broadcasting…")
    for uid in user_ids:
        try:
            await context.bot.send_message(int(uid), "📢 " + text)
            sent += 1
            await asyncio.sleep(0.05)  # avoid hitting Telegram rate limits
        except Exception:
            failed += 1
    await status.edit_text(f"✅ Broadcast sent to *{sent}* users ({failed} failed).",
                           parse_mode="Markdown")


async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    text = (
        "⚙️ *Current configuration*\n\n"
        f"• Admin ID: `{ADMIN_ID}`\n"
        f"• Max file size: *{MAX_FILE_SIZE_MB} MB*\n"
        f"• Rate limit: *{RATE_LIMIT_SECONDS:.0f} sec*\n"
        f"• DB: `{DB_PATH.name}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def setadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setadmin <user_id>")
        return
    try:
        new_admin = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ That's not a valid user ID.")
        return
    global ADMIN_ID
    ADMIN_ID = new_admin
    await set_setting("admin_id", str(new_admin))
    await update.message.reply_text(f"✅ Admin ID set to `{new_admin}`.", parse_mode="Markdown")


async def setmaxsize_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setmaxsize <mb>")
        return
    try:
        new_size = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ That's not a valid number.")
        return
    if new_size <= 0:
        await update.message.reply_text("❌ Size must be positive.")
        return
    global MAX_FILE_SIZE_MB
    MAX_FILE_SIZE_MB = new_size
    await set_setting("max_file_size_mb", str(new_size))
    await update.message.reply_text(f"✅ Max file size set to *{new_size} MB*.", parse_mode="Markdown")


async def setratelimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setratelimit <seconds>")
        return
    try:
        new_rate = float(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ That's not a valid number.")
        return
    if new_rate < 0:
        await update.message.reply_text("❌ Must be >= 0.")
        return
    global RATE_LIMIT_SECONDS
    RATE_LIMIT_SECONDS = new_rate
    await set_setting("rate_limit_seconds", str(new_rate))
    if rate_limiter is not None:
        rate_limiter.cooldown = new_rate
    await update.message.reply_text(f"✅ Rate limit set to *{new_rate:.0f} sec*.", parse_mode="Markdown")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    text = (
        "🛠 *Admin panel*\n\n"
        "/stats — Today's downloads, users, totals\n"
        "/users — Top users by downloads\n"
        "/ban <id> — Ban a user\n"
        "/unban <id> — Unban a user\n"
        "/banned — List banned users\n"
        "/broadcast <msg> — Message all known users\n"
        "/setcookies — Update TikTok/Facebook cookies (send a .txt)\n"
        "/config — Show current configuration\n"
        "/setadmin <id> — Change admin ID\n"
        "/setmaxsize <mb> — Change max file size\n"
        "/setratelimit <sec> — Change rate limit\n"
        "/ping — Check if the bot is alive"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ----------------------------------------------------------------------------
# Cookie upload handler (admin only) — uses aiofiles for async file I/O.
# ----------------------------------------------------------------------------
async def setcookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    await update.message.reply_text(
        "📎 Send me the new cookies as a *.txt file* (Netscape format) — "
        "TikTok or Facebook, I'll auto-detect which one from the file "
        "content. Just upload/attach it directly, no caption needed. "
        "I'll start using it immediately.",
        parse_mode="Markdown",
    )


async def handle_cookie_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    doc = update.message.document
    if not doc:
        return
    try:
        file = await doc.get_file()
        raw = await file.download_as_bytearray()
        content = bytes(raw).decode("utf-8", errors="ignore")
    except Exception as e:
        logger.error("Failed to read uploaded cookie file: %s", e)
        await update.message.reply_text("❌ Couldn't read that file. Try again.")
        return

    lower = content.lower()
    if "tiktok.com" in lower:
        platform = "tiktok"
    elif "facebook.com" in lower:
        platform = "facebook"
    else:
        await update.message.reply_text(
            "⚠️ That doesn't look like a TikTok or Facebook cookie file "
            "(no matching domain entries found). Not saved — nothing changed."
        )
        return

    # aiofiles: non-blocking write of the cookie file.
    try:
        async with aiofiles.open(COOKIE_RUNTIME_FILES[platform], "w", encoding="utf-8") as f:
            await f.write(content)
    except OSError as e:
        logger.error("Failed to save cookie file: %s", e)
        await update.message.reply_text("❌ Couldn't save the file on the server.")
        return

    await update.message.reply_text(
        f"✅ {platform.capitalize()} cookies updated! "
        "New downloads will use these right away."
    )


# ----------------------------------------------------------------------------
# Link handler — validates URL, then shows Video/Audio/Cancel keyboard.
# ----------------------------------------------------------------------------
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip() if update.message.text else ""

    if await is_banned(user_id):
        await update.message.reply_text("🚫 You've been banned from using this bot.")
        return

    if not await rate_limiter.is_allowed(user_id):
        wait = await rate_limiter.remaining(user_id)
        await update.message.reply_text(
            f"⏳ Please wait {wait:.0f} seconds before sending another link."
        )
        return

    # STRICT validation happens BEFORE anything touches the downloader.
    platform, url = validate_url(text)
    if not platform:
        await update.message.reply_text(
            "❌ Invalid link. Please send a valid TikTok, YouTube, or "
            "Facebook URL.\n\n"
            "TikTok: tiktok.com, vm.tiktok.com, vt.tiktok.com\n"
            "YouTube: youtube.com, youtu.be\n"
            "Facebook: facebook.com, fb.watch"
        )
        return

    await record_user(user_id, user.username)

    msg = await update.message.reply_text(
        f"🔗 Found a *{PLATFORM_NAMES[platform]}* link.\n"
        "How would you like to download it?",
        reply_markup=choice_keyboard(),
        parse_mode="Markdown",
    )
    _pending[msg.message_id] = {
        "url": url,
        "platform": platform,
        "user_id": user_id,
        "chat_id": update.effective_chat.id,
        "ts": time.time(),
    }


# ----------------------------------------------------------------------------
# Callback handler — Video / Audio / Cancel. Strict try...finally temp cleanup.
# ----------------------------------------------------------------------------
async def handle_format_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    msg_id = query.message.message_id
    data = query.data or ""
    action = data.split(":")[1] if ":" in data else data

    # Cancel can happen either in the choice phase or mid-download.
    if action == "cancel":
        ev = _cancel_events.get(msg_id)
        if ev:
            ev.set()
            await query.answer("Cancelling…")
            return
        _pending.pop(msg_id, None)
        try:
            await query.edit_message_text("🚫 Cancelled.")
        except Exception:
            pass
        return

    pending = _pending.pop(msg_id, None)
    if not pending:
        await query.edit_message_text(
            "⌛ This request expired. Please send the link again."
        )
        return

    if query.from_user.id != pending["user_id"]:
        await query.answer("This isn't your request.", show_alert=True)
        return

    audio_only = action == "audio"

    # Show the progress bar immediately at 0% so the user gets instant feedback.
    initial_text = (
        "⬇️ Downloading audio…" if audio_only else "⬇️ Downloading…"
    ) + "\n" + _build_progress_bar(0) + "  0%"
    await query.edit_message_text(initial_text, reply_markup=cancel_keyboard())

    # Show "upload_video" chat action while the download is running.
    download_action = ChatAction.UPLOAD_VIDEO
    try:
        await context.bot.send_chat_action(
            chat_id=pending["chat_id"], action=download_action
        )
    except Exception:
        pass

    # STRICT TEMP-FILE MANAGEMENT (issue #3): each request gets its own unique
    # subdirectory that is recursively deleted in `finally` no matter what.
    workdir = tempfile.mkdtemp(prefix="dl_", dir=str(TEMP_DIR))
    filepath = None

    progress_state: dict = {"status": "starting"}
    stop_flag = {"done": False}
    cancel_event = threading.Event()
    _cancel_events[msg_id] = cancel_event

    updater = asyncio.create_task(
        run_progress_updates(
            query.message,
            progress_state,
            stop_flag,
            bot=context.bot,
            chat_id=pending["chat_id"],
            action=download_action,
        )
    )

    try:
        # Run the blocking yt-dlp downloader in a worker thread so the event
        # loop stays responsive (never blocks on network I/O).
        filepath, error = await asyncio.to_thread(
            download_with_retry,
            pending["url"],
            pending["platform"],
            workdir,
            progress_state,
            cancel_event,
            audio_only,
        )

        if cancel_event.is_set():
            await query.edit_message_text("🚫 Download cancelled.")
            return

        if not filepath or error:
            friendly = _format_error(pending["platform"], error)
            await record_download(
                pending["user_id"],
                pending["platform"],
                "audio" if audio_only else "video",
                0,
                success=False,
            )
            await query.edit_message_text(friendly, parse_mode="Markdown")
            return

        # Size check (blocking os.path.getsize -> worker thread).
        size_bytes = await asyncio.to_thread(os.path.getsize, filepath)
        max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
        if size_bytes > max_bytes:
            await record_download(
                pending["user_id"],
                pending["platform"],
                "audio" if audio_only else "video",
                size_bytes,
                success=False,
            )
            await query.edit_message_text(
                f"📦 This video is {_format_bytes(size_bytes)} — larger than "
                f"Telegram's {MAX_FILE_SIZE_MB} MB limit."
            )
            return

        progress_state["status"] = "uploading"
        await query.edit_message_text("📤 Uploading to Telegram…")

        # MEMORY-EFFICIENT UPLOAD (issue #5): pass the on-disk *path* (str) so
        # python-telegram-bot streams the file from disk to Telegram instead of
        # reading the whole file into RAM.
        if audio_only:
            await context.bot.send_audio(
                chat_id=pending["chat_id"],
                audio=filepath,
                caption="🎵 Here's your audio!",
            )
        else:
            await context.bot.send_video(
                chat_id=pending["chat_id"],
                video=filepath,
                caption="✅ Here's your video!",
                supports_streaming=True,
            )

        await record_download(
            pending["user_id"],
            pending["platform"],
            "audio" if audio_only else "video",
            size_bytes,
            success=True,
        )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("Unhandled error in download flow for %s", pending["url"])
        await notify_admin(
            context,
            f"Unhandled error (user {pending['user_id']})\n"
            f"URL: {pending['url']}\nError: {e}",
        )
        try:
            await query.edit_message_text("❌ Something went wrong. Please try again later.")
        except Exception:
            pass
    finally:
        # Always stop the progress updater and release the cancel event.
        stop_flag["done"] = True
        _cancel_events.pop(msg_id, None)
        if not updater.done():
            updater.cancel()

        # STRICT TEMP-FILE CLEANUP (issue #3): remove the whole work dir
        # regardless of success / error / timeout / cancel.
        try:
            await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Global error handler — log + notify admin (critical errors only).
# ----------------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update: %s", context.error)
    await notify_admin(context, f"Unhandled exception: {context.error}")


# ----------------------------------------------------------------------------
# Command visibility — hide admin commands from regular users.
# ----------------------------------------------------------------------------
# Regular users only see these in the "/" command menu.
USER_COMMANDS = [
    BotCommand("start", "🚀 Start the bot"),
    BotCommand("help", "📖 How to use"),
    BotCommand("myid", "🆔 Your Telegram ID"),
]

# The admin (and only the admin) sees the full command set.
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand("admin", "🛠 Admin panel"),
    BotCommand("stats", "📊 Statistics"),
    BotCommand("users", "👥 Top users"),
    BotCommand("ban", "🚫 Ban a user"),
    BotCommand("unban", "✅ Unban a user"),
    BotCommand("banned", "🚫 Banned list"),
    BotCommand("broadcast", "📡 Broadcast message"),
    BotCommand("setcookies", "🍪 Update cookies"),
    BotCommand("config", "⚙️ Show config"),
    BotCommand("setadmin", "👤 Change admin"),
    BotCommand("setmaxsize", "📦 Set max size"),
    BotCommand("setratelimit", "⏱️ Set rate limit"),
    BotCommand("ping", "🏓 Ping"),
]


async def post_init(application: Application) -> None:
    """Runs once at startup: init DB, then publish scoped command menus."""
    await init_db()

    # Regular users only see the user commands.
    await application.bot.set_my_commands(
        USER_COMMANDS, scope=BotCommandScopeDefault()
    )

    # Only the admin chat sees the full command set (admin commands hidden
    # from everyone else's menu).
    if ADMIN_ID:
        await application.bot.set_my_commands(
            ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=ADMIN_ID)
        )


async def post_shutdown(application: Application) -> None:
    """Runs at shutdown: close DB and clean up leftover temp downloads."""
    await close_db()
    try:
        await asyncio.to_thread(shutil.rmtree, TEMP_DIR, ignore_errors=True)
    except Exception:
        pass


def main() -> None:
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # User commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("ping", ping_command))

    # Admin commands (each handler re-checks admin status internally)
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("banned", banned_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("config", config_command))
    application.add_handler(CommandHandler("setadmin", setadmin_command))
    application.add_handler(CommandHandler("setmaxsize", setmaxsize_command))
    application.add_handler(CommandHandler("setratelimit", setratelimit_command))
    application.add_handler(CommandHandler("setcookies", setcookies_command))

    # Document uploads (admin cookie updates) + text links
    application.add_handler(MessageHandler(filters.Document.ALL, handle_cookie_upload))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    # Inline keyboard (Video / Audio / Cancel)
    application.add_handler(CallbackQueryHandler(handle_format_choice, pattern="^dl:"))

    application.add_error_handler(error_handler)

    logger.info("Starting bot…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
