import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
import aiosqlite
from telegram import (
    Bot,
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
ADMIN_ID = int(os.getenv("ADMIN_ID") or "5566718291") or None

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB") or "500")  # see caveat below
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

PLATFORM_NAMES = {"tiktok": "TikTok", "facebook": "Facebook"}

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

    # Register command menus (hidden admin commands for regular users).
    await _setup_bot_commands()

    # Start the daily cookie-expiry checker (reports once shortly after startup,
    # then every day at 09:00 local time).
    asyncio.create_task(_cookie_check_loop())


async def _setup_bot_commands() -> None:
    """Hide admin commands from regular users via BotCommandScope."""
    bot = Bot(BOT_TOKEN)
    user_cmds = [
        BotCommand("start", "Welcome message"),
        BotCommand("help", "How to use + limits"),
        BotCommand("myid", "Show your Telegram user ID"),
    ]
    admin_cmds = user_cmds + [
        BotCommand("stats", "Today's stats"),
        BotCommand("users", "Top users"),
        BotCommand("ban", "Ban a user"),
        BotCommand("unban", "Unban a user"),
        BotCommand("banned", "List banned users"),
        BotCommand("broadcast", "Message all users"),
        BotCommand("setcookies", "Upload cookies (reply to .txt)"),
        BotCommand("config", "Show configuration"),
        BotCommand("setadmin", "Change admin ID"),
        BotCommand("setmaxsize", "Change max file size"),
        BotCommand("setratelimit", "Change rate limit"),
        BotCommand("admin", "Admin panel"),
        BotCommand("ping", "Check if alive"),
    ]
    try:
        await bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
        if ADMIN_ID:
            await bot.set_my_commands(
                admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID)
            )
    except Exception as e:
        logger.warning("Could not set bot command scopes: %s", e)


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
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "tiktok"
    if host == "facebook.com" or host.endswith(".facebook.com"):
        return "facebook"
    if host == "fb.watch" or host.endswith(".fb.watch"):
        return "facebook"
    if host == "fb.com" or host.endswith(".fb.com"):
        return "facebook"
    return None


def validate_url(text: str) -> tuple[str | None, str | None]:
    """
    Strictly validate user input as a supported video/photo URL.

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
    if platform == "tiktok":
        return TIKTOK_COOKIES
    return FACEBOOK_COOKIES


# ----------------------------------------------------------------------------
# Daily cookie-expiry checker — auto-tests cookie validity and DMs the admin.
# ----------------------------------------------------------------------------
def _cookie_earliest_expiry(content: str) -> int | None:
    """Return the soonest cookie expiry timestamp in a Netscape cookie file.

    Netscape format columns:
        domain  includeSubdomains  path  secure  expiry  name  value
    Column index 4 is the Unix expiry timestamp (0 = session cookie).
    Returns None if no fixed expiry is found.
    """
    soonest = None
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            parts = line.split()  # some exports use spaces
        if len(parts) < 5:
            continue
        try:
            exp = int(parts[4])
        except (ValueError, IndexError):
            continue
        if exp <= 0:
            continue  # session cookie — no fixed expiry
        if soonest is None or exp < soonest:
            soonest = exp
    return soonest


def _describe_expiry(expiry: int | None) -> str:
    if expiry is None:
        return "no fixed expiry (session cookies / unparsable)"
    delta = expiry - int(time.time())
    if delta < 0:
        return f"⚠️ EXPIRED {abs(delta) // 3600}h ago"
    days = delta // 86400
    hours = (delta % 86400) // 3600
    return f"valid for ~{days}d {hours}h"


def _detect_cookie_platform(content: str) -> str | None:
    """Detect which platform a Netscape cookie file belongs to."""
    low = content.lower()
    if any(d in low for d in ("facebook.com", "fb.com", "fb.watch", "fbsbx.com")):
        return "facebook"
    if "tiktok.com" in low:
        return "tiktok"
    return None


async def _check_cookies_and_report() -> str:
    """Check all platform cookie files and build a human-readable report."""
    lines = ["🍪 *Daily cookie health check*\n"]
    for platform, path in COOKIE_RUNTIME_FILES.items():
        if not path.exists():
            lines.append(f"• {platform.capitalize()}: ❌ no cookie file")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            lines.append(f"• {platform.capitalize()}: ❌ unreadable")
            continue
        if not content.strip():
            lines.append(f"• {platform.capitalize()}: ❌ empty")
            continue
        exp = _cookie_earliest_expiry(content)
        lines.append(f"• {platform.capitalize()}: {_describe_expiry(exp)}")
    return "\n".join(lines)


async def _cookie_check_loop() -> None:
    """Run the daily cookie check; DM the admin a report every day at 09:00."""
    bot = Bot(BOT_TOKEN)
    while True:
        # Report once shortly after startup, then once per day.
        await asyncio.sleep(5)
        try:
            report = await _check_cookies_and_report()
            if ADMIN_ID:
                await bot.send_message(ADMIN_ID, report, parse_mode="Markdown")
        except Exception as e:
            logger.error("Cookie check failed: %s", e)
        # Sleep until the next 09:00 local time.
        now = datetime.now()
        next_run = (now + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        await asyncio.sleep((next_run - now).total_seconds())


# ----------------------------------------------------------------------------
# Error formatting — platform-specific, user-friendly messages
# ----------------------------------------------------------------------------
def _format_error(platform: str, error: str | None) -> str:
    pname = PLATFORM_NAMES.get(platform, "video")
    s = (error or "").lower()

    if "cancelled" in s or "cancel" in s:
        return "🚫 Download cancelled."

    # Anti-bot / rate-limit walls are NOT "private" — detect them first
    # so we don't mislabel a public post as private/sign-in.
    if any(m in s for m in ("confirm you're not a bot", "not a bot", "sign in to confirm")):
        return (
            f"🤖 {pname} is temporarily asking us to verify we're not a bot "
            "(this is a rate-limit, not a private video). Please retry in a "
            "minute, or send fresh cookies via /setcookies."
        )

    # "only available for registered users" (Facebook) => needs cookies.
    if any(m in s for m in ("registered users", "login required", "requires login", "log in required")):
        return (
            f"🔒 This {pname} post requires a logged-in account.\n"
            "Please send fresh cookies via /setcookies and try again."
        )

    if any(m in s for m in ("private", "sign in", "members-only", "premium")):
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
        "not a bot",
        "confirm you're not a bot",
    )
    return any(m in s for m in markers)


# ----------------------------------------------------------------------------
# yt-dlp download — a SYNCHRONOUS function run inside asyncio.to_thread().
# ----------------------------------------------------------------------------
class DownloadCancelled(Exception):
    """Raised from a progress hook to abort an in-flight download."""


# Extensions used to decide whether to send the file as photo/video/audio.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav"}


def _media_type(filepath: str) -> str:
    """Classify a downloaded file by extension for the correct Telegram send."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in IMAGE_EXTS:
        return "photo"
    if ext in AUDIO_EXTS:
        return "audio"
    return "video"


def _resolve_filepath(ydl, info, out_dir: str) -> str:
    """Return the final file path, accounting for merge/audio/image output."""
    filepath = ydl.prepare_filename(info)
    if os.path.exists(filepath):
        return filepath
    base = os.path.splitext(filepath)[0]
    wanted = {
        ".mp3", ".mp4", ".m4a", ".webm", ".mkv",
        ".jpg", ".jpeg", ".png", ".webp",
    }
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
    """Download a video/photo (or audio) using yt-dlp. Runs in a worker thread.

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
    elif platform == "facebook":
        # Videos first, then fall back to photo/image posts. The trailing
        # bare "best" is the important part for photos: Facebook photo
        # posts don't always expose a format matching one of the listed
        # image extensions, so without a catch-all yt-dlp raises
        # "Requested format is not available" and the photo never downloads.
        ydl_opts["format"] = (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/"
            "best[ext=mp4]/best[ext=webm]/"
            "best[ext=jpg]/best[ext=jpeg]/best[ext=png]/best[ext=webp]/"
            "best"
        )
        ydl_opts["merge_output_format"] = "mp4"
    else:  # tiktok
        # TikTok photo posts are usually returned as a slideshow mp4, but fall
        # back to raw image formats (and finally a bare "best") in case
        # yt-dlp extracts individual images instead of a rendered slideshow.
        ydl_opts["format"] = (
            "best[ext=mp4]/"
            "best[ext=jpg]/best[ext=jpeg]/best[ext=png]/best[ext=webp]/"
            "best"
        )
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
        "Send me a TikTok or Facebook link, pick <b>Video</b> or "
        "<b>Audio only</b>, and I'll send it straight back — no watermark.\n\n"
        "🎵 <b>Supported platforms:</b>\n"
        "• TikTok — tiktok.com, vm.tiktok.com, vt.tiktok.com (video & photos)\n"
        "• Facebook — facebook.com, fb.watch (videos, reels & photos)\n\n"
        "Type /help for details and limits. "
        f'<tg-emoji emoji-id="{EMOJI_WINK}">😉</tg-emoji>'
    )
    await update.message.reply_text(welcome, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "📖 *How to use*\n\n"
        "1. Copy a TikTok or Facebook link.\n"
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
    global ADMIN_ID
    if not context.args:
        await update.message.reply_text("Usage: /setadmin <user_id>")
        return
    try:
        ADMIN_ID = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ That's not a valid user ID.")
        return
    await set_setting("admin_id", str(ADMIN_ID))
    await update.message.reply_text(f"✅ Admin ID set to `{ADMIN_ID}`.", parse_mode="Markdown")


async def setmaxsize_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    global MAX_FILE_SIZE_MB
    if not context.args:
        await update.message.reply_text("Usage: /setmaxsize <mb>")
        return
    try:
        MAX_FILE_SIZE_MB = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ That's not a valid size.")
        return
    await set_setting("max_file_size_mb", str(MAX_FILE_SIZE_MB))
    await update.message.reply_text(f"✅ Max file size set to *{MAX_FILE_SIZE_MB} MB*.", parse_mode="Markdown")


async def setratelimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    global RATE_LIMIT_SECONDS
    if not context.args:
        await update.message.reply_text("Usage: /setratelimit <seconds>")
        return
    try:
        RATE_LIMIT_SECONDS = float(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ That's not a valid number.")
        return
    await set_setting("rate_limit_seconds", str(RATE_LIMIT_SECONDS))
    if rate_limiter:
        rate_limiter.cooldown = RATE_LIMIT_SECONDS
    await update.message.reply_text(f"✅ Rate limit set to *{RATE_LIMIT_SECONDS:.0f} sec*.", parse_mode="Markdown")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    text = (
        "🛠️ *Admin panel*\n\n"
        "/stats — Today's stats\n"
        "/users — Top users\n"
        "/ban <id> — Ban a user\n"
        "/unban <id> — Unban a user\n"
        "/banned — List banned users\n"
        "/broadcast <msg> — Message all users\n"
        "/setcookies — Upload cookies (reply to a .txt file)\n"
        "/config — Show configuration\n"
        "/setadmin <id> — Change admin ID\n"
        "/setmaxsize <mb> — Change max file size\n"
        "/setratelimit <sec> — Change rate limit\n"
        "/ping — Check if alive"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ----------------------------------------------------------------------------
# Cookie upload — v3.3: works by REPLYING to a cookie .txt file with /setcookies
# (or sending the .txt file directly to the admin chat).
# ----------------------------------------------------------------------------
async def _save_cookie_file(platform: str, content: str) -> bool:
    """Persist cookie content to its runtime file using aiofiles (non-blocking)."""
    path = COOKIE_RUNTIME_FILES.get(platform)
    if not path:
        return False
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(content)
    return True


def _count_cookie_entries(content: str) -> int:
    return sum(
        1 for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


async def setcookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Upload cookies by REPLYING to a cookie .txt file with /setcookies.

    Usage:
        /setcookies                              -> shows instructions
        /setcookies  (reply to a .txt document)  -> download + detect + save
        /setcookies  (reply to a text message)   -> save the text as cookies
    """
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return

    reply = update.message.reply_to_message

    if reply is None:
        await update.message.reply_text(
            "🍪 *Upload cookies*\n\n"
            "Reply to a cookie `.txt` file (Netscape format) with `/setcookies`.\n\n"
            "*How to:*\n"
            "1. Export cookies from your browser as a `.txt` file.\n"
            "2. Send that `.txt` file to me.\n"
            "3. Reply to that file with `/setcookies`.\n\n"
            "Supported: TikTok, Facebook.",
            parse_mode="Markdown",
        )
        return

    content: str | None = None

    if reply.document:
        doc = reply.document
        if doc.file_size and doc.file_size > 2 * 1024 * 1024:
            await update.message.reply_text("❌ Cookie file too large (max 2 MB).")
            return
        status = await update.message.reply_text("⬇️ Downloading cookie file…")
        try:
            tg_file = await doc.get_file()
            buf = await tg_file.download_as_bytearray()
        except Exception as e:
            await status.edit_text(f"❌ Couldn't download the file: {e}")
            return
        content = buf.decode("utf-8", errors="ignore")
        await status.delete()
    elif reply.text:
        content = reply.text

    if not content or not content.strip():
        await update.message.reply_text("❌ I couldn't find cookie text in that message.")
        return

    platform = _detect_cookie_platform(content)
    if not platform:
        await update.message.reply_text(
            "⚠️ That doesn't look like a TikTok or Facebook cookie "
            "file (no matching domain entries found). Not saved — nothing changed."
        )
        return

    await _save_cookie_file(platform, content)
    exp = _cookie_earliest_expiry(content)
    await update.message.reply_text(
        f"✅ Saved {PLATFORM_NAMES.get(platform, platform)} cookies.\n"
        f"• Entries: `{_count_cookie_entries(content)}`\n"
        f"• Expiry: {_describe_expiry(exp)}",
        parse_mode="Markdown",
    )


async def cookie_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Convenience: auto-save a cookie .txt file sent directly to the admin."""
    if not _is_admin(update.effective_user.id):
        return
    doc = update.message.document
    if not doc:
        return
    fname = (doc.file_name or "").lower()
    if not fname.endswith(".txt"):
        return
    if doc.file_size and doc.file_size > 2 * 1024 * 1024:
        await update.message.reply_text("❌ Cookie file too large (max 2 MB).")
        return

    status = await update.message.reply_text("⬇️ Reading cookie file…")
    try:
        tg_file = await doc.get_file()
        buf = await tg_file.download_as_bytearray()
    except Exception as e:
        await status.edit_text(f"❌ Couldn't download the file: {e}")
        return
    content = buf.decode("utf-8", errors="ignore")

    platform = _detect_cookie_platform(content)
    if not platform:
        await status.edit_text(
            "⚠️ That doesn't look like a TikTok or Facebook cookie "
            "file (no matching domain entries found). Not saved — nothing changed."
        )
        return

    await _save_cookie_file(platform, content)
    exp = _cookie_earliest_expiry(content)
    await status.edit_text(
        f"✅ Auto-saved {PLATFORM_NAMES.get(platform, platform)} cookies.\n"
        f"• Entries: `{_count_cookie_entries(content)}`\n"
        f"• Expiry: {_describe_expiry(exp)}",
        parse_mode="Markdown",
    )


# ----------------------------------------------------------------------------
# Main message handler — validates URL, applies rate limit, offers choice.
# ----------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    await record_user(user.id, user.username)

    if await is_banned(user.id):
        await update.message.reply_text("🚫 You've been banned from using this bot.")
        return

    platform, url = validate_url(update.message.text)
    if not platform:
        # Not a supported link — stay silent (avoid noisy replies to casual chat).
        return

    if rate_limiter and not await rate_limiter.is_allowed(user.id):
        remaining = await rate_limiter.remaining(user.id)
        await update.message.reply_text(f"⏳ Slow down! Try again in {remaining:.0f}s.")
        return

    _pending[update.effective_chat.id] = {
        "url": url,
        "platform": platform,
        "user_id": user.id,
    }
    await update.message.reply_text(
        f"🔗 *{PLATFORM_NAMES.get(platform, 'video')} link received*\n\n"
        "Choose a download format:",
        reply_markup=choice_keyboard(),
        parse_mode="Markdown",
    )


# ----------------------------------------------------------------------------
# Callback handler — runs the actual download + upload with strict cleanup.
# ----------------------------------------------------------------------------
async def handle_format_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data

    if data == "dl:cancel":
        if chat_id in _cancel_events:
            _cancel_events[chat_id].set()
        _pending.pop(chat_id, None)
        try:
            await query.edit_message_text("🚫 Cancelled.")
        except Exception:
            pass
        return

    pending = _pending.pop(chat_id, None)
    if not pending:
        try:
            await query.edit_message_text("⚠️ This request has expired. Send the link again.")
        except Exception:
            pass
        return

    if data not in ("dl:video", "dl:audio"):
        return

    audio_only = data == "dl:audio"
    url = pending["url"]
    platform = pending["platform"]
    user_id = pending["user_id"]

    # Unique temp dir for this download so parallel requests never collide.
    work_dir = Path(tempfile.mkdtemp(prefix="dl_", dir=TEMP_DIR))
    cancel_event = threading.Event()
    _cancel_events[chat_id] = cancel_event
    progress_state = {"status": "starting", "downloaded": 0, "total": 0, "speed": 0, "eta": 0}

    status_message = await query.edit_message_text(
        "⬇️ Downloading…\n░░░░░░░░░░░░  0%",
        reply_markup=cancel_keyboard(),
    )

    # Send the chat action immediately; the progress task keeps it alive.
    action = ChatAction.UPLOAD_VIDEO
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=action)
    except Exception:
        pass

    stop_flag = {"done": False}
    progress_task = asyncio.create_task(
        run_progress_updates(
            status_message, progress_state, stop_flag,
            bot=context.bot, chat_id=chat_id, action=action,
        )
    )

    filepath = None
    error = None

    # --- Download (blocking yt-dlp runs in a worker thread) -----------------
    try:
        try:
            filepath, error = await asyncio.wait_for(
                asyncio.to_thread(
                    download_with_retry,
                    url, platform, str(work_dir), progress_state, cancel_event, audio_only,
                ),
                timeout=DOWNLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            error = "Download timed out."
            cancel_event.set()
        except Exception as e:
            error = str(e)
            cancel_event.set()
    finally:
        stop_flag["done"] = True
        if progress_task:
            progress_task.cancel()
        _cancel_events.pop(chat_id, None)

    # --- Upload + report + cleanup (always runs, even on failure) -----------
    try:
        if error or not filepath or not os.path.exists(filepath):
            await record_download(
                user_id, platform, "audio" if audio_only else "video", 0, success=False
            )
            await status_message.edit_text(_format_error(platform, error), parse_mode="Markdown")
            return

        size_bytes = os.path.getsize(filepath)
        size_mb = size_bytes / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            await record_download(
                user_id, platform, "audio" if audio_only else "video", size_bytes, success=False
            )
            await status_message.edit_text(
                f"📦 This file is {size_mb:.1f} MB, over the {MAX_FILE_SIZE_MB} MB limit."
            )
            return

        media = _media_type(filepath)
        try:
            await status_message.edit_text("📤 Uploading to Telegram…")
        except Exception:
            pass

        # Pass the on-disk *path* (str), never bytes — PTB streams from disk.
        if media == "photo":
            await context.bot.send_photo(chat_id=chat_id, photo=filepath)
        elif media == "audio":
            await context.bot.send_audio(chat_id=chat_id, audio=filepath)
        else:
            await context.bot.send_video(chat_id=chat_id, video=filepath)

        try:
            await status_message.delete()
        except Exception:
            pass
        await record_download(
            user_id, platform, "audio" if audio_only else "video", size_bytes, success=True
        )
    except Exception as e:
        logger.exception("Upload failed")
        await notify_admin(context, f"Upload failed for {url}\n\n{e}")
        try:
            await status_message.edit_text("❌ Upload to Telegram failed. Please try again.")
        except Exception:
            pass
    finally:
        # STRICT temp-file management (issue #3): always delete the dir.
        try:
            await asyncio.to_thread(shutil.rmtree, str(work_dir), True)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Error handler + entrypoint
# ----------------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error: %s", context.error, exc_info=context.error)
    await notify_admin(context, f"Unhandled exception:\n{context.error}")


def main() -> None:
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(lambda app: init_db())
        .post_shutdown(lambda app: close_db())
        .build()
    )

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("ping", ping_command))
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
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("setcookies", setcookies_command))

    # Callbacks
    application.add_handler(CallbackQueryHandler(handle_format_choice, pattern=r"^dl:"))

    # Cookie file direct-send convenience (admin only, checked inside handler).
    application.add_handler(MessageHandler(filters.Document.ALL, cookie_file_handler))

    # URL entry point
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    application.add_error_handler(error_handler)

    logger.info("Starting bot…")
    application.run_polling()


if __name__ == "__main__":
    main()
