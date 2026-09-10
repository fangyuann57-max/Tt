"""
TikTok + YouTube + Facebook Video Downloader Telegram Bot
Single-file bot using python-telegram-bot (v20+, async) and yt-dlp.

Setup (Ubuntu VPS):
    pip install python-telegram-bot yt-dlp
    sudo apt install ffmpeg

    1. Set BOT_TOKEN below (get one from @BotFather).
    2. (Optional) Paste fresh TikTok cookies into TIKTOK_COOKIES below,
       Netscape format, if downloads start getting blocked. Leave it
       empty ("") to download without cookies — works for most public
       videos.
    3. Run the bot once, DM it /myid to get your Telegram user ID, put
       that number in ADMIN_ID below, then restart the bot.
    4. python bot.py

    After setup, you (the admin) can update TikTok cookies anytime by
    just sending the new cookies.txt file directly to the bot in a DM —
    no code edits or restarts needed. Only your ADMIN_ID can do this;
    other users' uploads are ignored.
"""

import os
import re
import json
import time
import uuid
import asyncio
import logging
import tempfile
import threading
import traceback
from datetime import datetime, date

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

import yt_dlp

try:
    from yt_dlp.utils import DownloadCancelled
except ImportError:  # older yt-dlp fallback
    class DownloadCancelled(Exception):
        pass

# ----------------------------------------------------------------------------
# CONFIG — fill these in before running
# ----------------------------------------------------------------------------
BOT_TOKEN = "8953839870:AAG5PBpFq68FaooPorS16sJPb9q-A_vw6Hs"  # from @BotFather

# Your Telegram user ID — only this account can update cookies via the bot.
# Send /myid to the bot once to find out your ID, then set it here.
ADMIN_ID = 5566718291

MAX_FILE_SIZE_MB = 50        # Hard limit on Telegram's standard Bot API
RATE_LIMIT_SECONDS = 10      # 1 request per user per 10s
TEMP_DIR = "downloads"

# Retry network-ish failures (timeout / connection reset / DNS hiccups)
# this many extra times, with exponential backoff, before giving up.
NETWORK_RETRY_ATTEMPTS = 2
NETWORK_RETRY_BASE_DELAY = 3  # seconds — attempt N waits BASE_DELAY * 2**(N-1)

# How long an entry may sit in the in-memory rate-limiter dict before a
# periodic cleanup job sweeps it out (keeps memory flat as users grow).
RATE_LIMIT_ENTRY_TTL = 3600  # 1 hour
RATE_LIMIT_CLEANUP_INTERVAL = 1800  # run the sweep every 30 min

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATS_FILE = os.path.join(_BASE_DIR, "bot_stats.json")
BANNED_USERS_FILE = os.path.join(_BASE_DIR, "banned_users.json")

# How often (seconds) the "Downloading…" status message is edited with
# fresh progress. Telegram throttles rapid edits to the same message, so
# this stays a few seconds apart rather than updating on every yt-dlp tick.
PROGRESS_UPDATE_INTERVAL = 4

# Where cookies uploaded via Telegram (see /setcookies) get saved, so they
# persist across restarts. If a platform's file exists, it's used INSTEAD
# of that platform's *_COOKIES text below — i.e. Telegram-updated cookies
# always win. Which file an upload goes to is auto-detected from its
# content (tiktok.com vs facebook.com cookie entries).
COOKIE_RUNTIME_FILES = {
    "tiktok": os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "tiktok_cookies_runtime.txt"
    ),
    "facebook": os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "facebook_cookies_runtime.txt"
    ),
}

# Optional: paste fresh TikTok cookies here (Netscape format) if downloads
# start getting blocked. Leave as "" to skip cookies entirely — most
# public TikTok videos download fine without them. This is only the
# starting/fallback value — send a new cookies.txt to the bot anytime to
# update it live (see ADMIN_ID above).
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

# Optional: paste Facebook cookies here (Netscape format) if you need to
# download private/restricted videos, or if downloads start getting
# blocked. Leave as "" for public videos — usually works fine without
# cookies. Update live anytime by sending a cookies.txt to the bot (see
# ADMIN_ID above) — the upload is auto-detected as Facebook or TikTok
# from its content.
FACEBOOK_COOKIES = ""

# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# URL patterns
# ----------------------------------------------------------------------------
TIKTOK_URL_RE = re.compile(
    r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/[\w\-./?=&%@#]+",
    re.IGNORECASE,
)

YOUTUBE_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/|live/)[\w\-]+"
    r"|youtu\.be/[\w\-]+)",
    re.IGNORECASE,
)

FACEBOOK_URL_RE = re.compile(
    r"https?://(?:www\.|web\.|m\.)?facebook\.com/[\w\-./?=&%@#]+"
    r"|https?://fb\.watch/[\w\-]+",
    re.IGNORECASE,
)

# ----------------------------------------------------------------------------
# In-memory rate limiter: {user_id: last_request_timestamp}
# ----------------------------------------------------------------------------
_last_request: dict[int, float] = {}


def is_rate_limited(user_id: int) -> bool:
    """Return True if the user is within the rate-limit window."""
    now = time.time()
    last = _last_request.get(user_id)
    if last is not None and (now - last) < RATE_LIMIT_SECONDS:
        return True
    _last_request[user_id] = now
    return False


async def cleanup_rate_limiter(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: drop rate-limiter entries older than their TTL so the
    dict doesn't grow forever as more distinct users send links."""
    now = time.time()
    stale = [uid for uid, ts in _last_request.items() if now - ts > RATE_LIMIT_ENTRY_TTL]
    for uid in stale:
        _last_request.pop(uid, None)
    if stale:
        logger.info("Rate-limiter cleanup: dropped %d stale entries.", len(stale))


# ----------------------------------------------------------------------------
# Persisted admin state: stats (downloads/users) + ban list.
# Small JSON files next to the script — simple, no DB dependency, and
# survives bot restarts (unlike the plain in-memory rate limiter above).
# ----------------------------------------------------------------------------
def _load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to load %s: %s", path, e)
    return default


def _save_json(path: str, data) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError as e:
        logger.error("Failed to save %s: %s", path, e)


_stats: dict = _load_json(
    STATS_FILE,
    {"date": "", "downloads_today": 0, "users_today": [], "total_downloads": 0, "all_users": []},
)
_banned_users: set[int] = set(_load_json(BANNED_USERS_FILE, []))


def _today_str() -> str:
    return date.today().isoformat()


def record_download(user_id: int) -> None:
    """Update persisted stats after a successful download."""
    today = _today_str()
    if _stats.get("date") != today:
        _stats["date"] = today
        _stats["downloads_today"] = 0
        _stats["users_today"] = []

    _stats["downloads_today"] += 1
    _stats["total_downloads"] = _stats.get("total_downloads", 0) + 1

    users_today = set(_stats.get("users_today", []))
    users_today.add(user_id)
    _stats["users_today"] = list(users_today)

    all_users = set(_stats.get("all_users", []))
    all_users.add(user_id)
    _stats["all_users"] = list(all_users)

    _save_json(STATS_FILE, _stats)


def record_seen_user(user_id: int) -> None:
    """Track every user who's touched the bot (for /users), even if their
    request never turns into a successful download."""
    all_users = set(_stats.get("all_users", []))
    if user_id not in all_users:
        all_users.add(user_id)
        _stats["all_users"] = list(all_users)
        _save_json(STATS_FILE, _stats)


def is_banned(user_id: int) -> bool:
    return user_id in _banned_users


def ban_user(user_id: int) -> None:
    _banned_users.add(user_id)
    _save_json(BANNED_USERS_FILE, list(_banned_users))


def unban_user(user_id: int) -> None:
    _banned_users.discard(user_id)
    _save_json(BANNED_USERS_FILE, list(_banned_users))


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Best-effort DM to ADMIN_ID — used for critical/unexpected errors so
    they show up in Telegram instead of only in the server log file."""
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🛑 *Bot error*\n\n{text[:3500]}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Failed to notify admin: %s", e)


# ----------------------------------------------------------------------------
# In-memory state for the pending "choose format" step and cancellable
# in-flight downloads. Keyed by a short request id carried in the inline
# keyboard's callback_data (Telegram caps callback_data at 64 bytes, so we
# can't stuff the full URL in there).
# ----------------------------------------------------------------------------
_pending_requests: dict[str, dict] = {}   # request_id -> {url, platform, user_id}
_active_cancel_events: dict[str, threading.Event] = {}  # request_id -> Event


def new_request_id() -> str:
    return uuid.uuid4().hex[:10]


def _format_bytes(n: float) -> str:
    """Human-readable MB formatting for progress messages."""
    return f"{n / (1024 * 1024):.1f} MB"


def _build_progress_bar(percent: int, width: int = 12) -> str:
    percent = max(0, min(100, percent))
    filled = round(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


async def run_progress_updates(status_message, progress_state: dict, stop_flag: dict) -> None:
    """
    Background task: while a download is in progress, periodically edit the
    status message with a live progress bar, downloaded/total size, and
    speed — sourced from progress_state, which download_video() updates
    from yt-dlp's progress_hooks (running on a worker thread).
    """
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

            if percent is not None:
                bar = _build_progress_bar(percent)
                text = (
                    f"⬇️ Downloading…\n"
                    f"{bar}  {percent}%\n"
                    f"📦 {_format_bytes(downloaded)} / {_format_bytes(total)}  •  ⚡ {speed_str}"
                )
            else:
                # Some extractors never report a total size up front.
                text = (
                    f"⬇️ Downloading…\n"
                    f"📦 {_format_bytes(downloaded)} downloaded so far  •  ⚡ {speed_str}"
                )

            try:
                await status_message.edit_text(text)
            except Exception:
                pass  # e.g. "message not modified" or rate-limited — skip a beat

        elif status == "merging" and last_shown != "merging":
            last_shown = "merging"
            try:
                await status_message.edit_text("🔧 Processing / merging video…")
            except Exception:
                pass


async def run_chat_action_loop(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, action: str, stop_flag: dict
) -> None:
    """
    Keep re-sending a chat action (e.g. "uploading video…") while a
    download/upload is in progress. Telegram only shows the indicator for
    ~5 seconds per call, so this re-sends it on a short interval until the
    caller flips stop_flag["done"].
    """
    while not stop_flag["done"]:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=action)
        except Exception:
            pass
        await asyncio.sleep(4)


# ----------------------------------------------------------------------------
# Platform detection — returns (platform, clean_url) or (None, None)
# ----------------------------------------------------------------------------
def detect_platform(text: str) -> tuple[str | None, str | None]:
    m = TIKTOK_URL_RE.search(text)
    if m:
        return "tiktok", m.group(0)
    m = YOUTUBE_URL_RE.search(text)
    if m:
        return "youtube", m.group(0)
    m = FACEBOOK_URL_RE.search(text)
    if m:
        return "facebook", m.group(0)
    return None, None


def get_active_cookies(platform: str) -> str:
    """
    Return the cookies actually in use right now for the given platform
    ("tiktok" or "facebook"). Cookies updated live via Telegram upload
    always take priority over the ones baked into this script at deploy
    time.
    """
    runtime_file = COOKIE_RUNTIME_FILES.get(platform)
    if runtime_file and os.path.exists(runtime_file):
        try:
            with open(runtime_file, "r", encoding="utf-8") as f:
                content = f.read()
            if content.strip():
                return content
        except OSError:
            pass
    return TIKTOK_COOKIES if platform == "tiktok" else FACEBOOK_COOKIES


# ----------------------------------------------------------------------------
# yt-dlp download
# ----------------------------------------------------------------------------
def download_video(
    url: str,
    platform: str,
    out_dir: str,
    progress_state: dict | None = None,
    audio_only: bool = False,
    cancel_event: "threading.Event | None" = None,
) -> tuple[str | None, str | None]:
    """
    Download a video using yt-dlp.
    Returns (filepath, None) on success, or (None, error_message) on failure.

    If progress_state is given (a plain dict), it's updated live from
    yt-dlp's progress_hooks — this function runs inside a worker thread
    (via asyncio.to_thread), while progress_state is read back on the main
    event loop by run_progress_updates() to drive the Telegram status
    message. Only primitive values are written, so no locking is needed.
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

    def _progress_hook(d: dict) -> None:
        # Cooperative cancellation: the Cancel button sets this Event from
        # the main event loop; we check it here since this hook fires
        # frequently on the worker thread doing the actual download.
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Cancelled by user")

        if progress_state is None:
            return
        status = d.get("status")
        if status == "downloading":
            progress_state["status"] = "downloading"
            progress_state["downloaded"] = d.get("downloaded_bytes") or 0
            progress_state["total"] = (
                d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            )
            progress_state["speed"] = d.get("speed") or 0
        elif status == "finished":
            # File finished downloading but ffmpeg may still need to
            # merge video+audio or remux — that shows as "merging".
            progress_state["status"] = "merging"
        elif status == "error":
            progress_state["status"] = "error"

    ydl_opts["progress_hooks"] = [_progress_hook]

    if audio_only:
        # Best audio track, extracted straight to mp3 — no video stream
        # downloaded at all, so this is faster and much smaller.
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    elif platform == "youtube":
        # Best mp4 video + audio, merged into mp4 (needs ffmpeg).
        ydl_opts["format"] = (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/best[ext=mp4]/best"
        )
        ydl_opts["merge_output_format"] = "mp4"
        # "android" is currently the most reliable client that works
        # without a PO token. "tv" now also requires sign-in + a PO token
        # in recent YouTube changes, so we stick to "android" only. No
        # cookies needed — public videos download fine without them, and
        # mixing web-session cookies with a non-web client is itself a
        # known cause of "The page needs to be reloaded" errors.
        ydl_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android"],
            }
        }
    elif platform == "facebook":
        # Facebook: best mp4 video + audio, merged into mp4.
        ydl_opts["format"] = (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        )
        ydl_opts["merge_output_format"] = "mp4"
    else:
        # TikTok: best available mp4 (watermark-free when possible).
        ydl_opts["format"] = "best[ext=mp4]/best"
        ydl_opts["merge_output_format"] = "mp4"

    tmp_cookie_path = None
    if platform in ("tiktok", "facebook"):
        active_cookies = get_active_cookies(platform)
        if active_cookies.strip():
            # Write the cookie string to a temp file — yt-dlp needs a real
            # file path, not a string.
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
            filepath = ydl.prepare_filename(info)

            # After merge, the extension may change (e.g. webm -> mp4).
            if not os.path.exists(filepath):
                base = os.path.splitext(filepath)[0]
                for f in os.listdir(out_dir):
                    if f.startswith(os.path.basename(base)):
                        filepath = os.path.join(out_dir, f)
                        break
            return filepath, None
    except Exception as e:
        logger.error("Download failed for %s: %s", url, e)
        return None, str(e)
    finally:
        if tmp_cookie_path:
            try:
                os.remove(tmp_cookie_path)
            except OSError:
                pass


# ----------------------------------------------------------------------------
# Turn a raw yt-dlp/network exception string into something a non-technical
# user can actually act on, instead of a trimmed stack-trace fragment.
# ----------------------------------------------------------------------------
def friendly_error_message(platform: str, raw_error: str) -> str:
    err = (raw_error or "").lower()

    def has(*needles: str) -> bool:
        return any(n in err for n in needles)

    if has("cancelled by user", "downloadcancelled"):
        return "🚫 Download cancelled."

    if has("private video", "this video is private", "private account"):
        return "🔒 This video is private — the uploader restricted who can view it."

    if has("age-restricted", "age restricted", "sign in to confirm your age", "inappropriate for some users"):
        return "🔞 This video is age-restricted and can't be fetched without a logged-in account."

    if has("not available in your country", "geo", "blocked in your country", "content isn't available"):
        return "🌍 This video is geo-blocked — it isn't available from this server's region."

    if has("video unavailable", "this video is unavailable", "no longer available", "has been removed"):
        return "❌ This video is unavailable — it may have been deleted or taken down."

    if has("404", "not found"):
        return "❌ Video not found. Double-check the link — it may be broken or deleted."

    if has("login required", "sign in", "log in to confirm"):
        return "🔑 This content requires a logged-in account to view. Try updating cookies (admin: /setcookies)."

    if has("timed out", "timeout", "connection reset", "connection aborted",
           "temporary failure", "network is unreachable", "max retries exceeded",
           "urlopen error", "econnreset", "read timed out"):
        return "📡 Network hiccup talking to the server — please try sending the link again in a moment."

    if has("unsupported url", "no extractor"):
        return "❌ That link isn't supported. Please send a TikTok, YouTube, or Facebook video link."

    if has("copyright"):
        return "©️ This video was blocked or removed for a copyright claim."

    if has("live event", "livestream", "is live"):
        return "🔴 This is a live stream — live videos can't be downloaded until they end."

    # Fallback — still show the platform so users know where it failed,
    # but without dumping a raw exception string at them.
    return (
        f"❌ Couldn't download this {platform} video. It may be private, "
        "deleted, region-locked, or the link is invalid."
    )


def is_network_error(raw_error: str) -> bool:
    """Used to decide whether a failure is worth an automatic retry."""
    err = (raw_error or "").lower()
    return any(
        n in err
        for n in (
            "timed out", "timeout", "connection reset", "connection aborted",
            "temporary failure", "network is unreachable", "max retries exceeded",
            "urlopen error", "econnreset", "read timed out", "connection refused",
            "remote end closed connection",
        )
    )


def get_file_size_mb(path: str) -> float:
    """Return file size in megabytes."""
    return os.path.getsize(path) / (1024 * 1024)


# ----------------------------------------------------------------------------
# Telegram handlers
# ----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    record_seen_user(update.effective_user.id)
    welcome = (
        "👋 *Welcome to Video Downloader Bot!*\n\n"
        "Send me a TikTok, YouTube, or Facebook link and I'll send the "
        "video straight back to you — no watermark.\n\n"
        "🎵 *Supported platforms:*\n"
        "• TikTok — tiktok.com, vm.tiktok.com, vt.tiktok.com\n"
        "• YouTube — youtube.com, youtu.be, Shorts\n"
        "• Facebook — facebook.com, fb.watch (videos & reels)\n\n"
        "Type /help for details and limits."
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "📖 *How to use*\n\n"
        "1. Copy a TikTok, YouTube, or Facebook link.\n"
        "2. Send it to this chat.\n"
        "3. Choose Video or Audio only.\n"
        "4. Wait — large videos can take a few minutes. You can cancel "
        "anytime with the ❌ button on the status message.\n\n"
        "*Commands*\n"
        "/start — Welcome message\n"
        "/help — This help text\n"
        "/myid — Show your Telegram user ID\n\n"
        "⚠️ *Limits*\n"
        f"• Max file size: {MAX_FILE_SIZE_MB} MB (Telegram bot limit)\n"
        f"• Rate limit: 1 request per {RATE_LIMIT_SECONDS} seconds\n"
        "• Download timeout: up to 15 minutes for very large videos"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Anyone can run this — it just shows their own Telegram user ID,
    which the admin needs once to fill in ADMIN_ID at the top of the script."""
    await update.message.reply_text(
        f"Your Telegram user ID: `{update.effective_user.id}`",
        parse_mode="Markdown",
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: quick usage snapshot."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return

    today = _today_str()
    if _stats.get("date") != today:
        downloads_today, users_today = 0, 0
    else:
        downloads_today = _stats.get("downloads_today", 0)
        users_today = len(_stats.get("users_today", []))

    text = (
        "📊 *Bot stats*\n\n"
        f"📅 Today ({today})\n"
        f"• Downloads: {downloads_today}\n"
        f"• Unique users: {users_today}\n\n"
        f"🕰️ All-time\n"
        f"• Total downloads: {_stats.get('total_downloads', 0)}\n"
        f"• Total unique users: {len(_stats.get('all_users', []))}\n"
        f"• Banned users: {len(_banned_users)}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: list of all users the bot has ever seen (most recent
    20 shown — the full count still reflects everyone)."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return

    all_users = _stats.get("all_users", [])
    preview = ", ".join(str(u) for u in all_users[-20:]) or "—"
    text = (
        f"👥 *Total users seen:* {len(all_users)}\n\n"
        f"Most recent (up to 20): `{preview}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: /ban <user_id>"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/ban <user_id>`", parse_mode="Markdown")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a numeric user ID.")
        return
    ban_user(target)
    await update.message.reply_text(f"🚫 User `{target}` has been banned.", parse_mode="Markdown")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: /unban <user_id>"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ You're not allowed to do this.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/unban <user_id>`", parse_mode="Markdown")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a numeric user ID.")
        return
    unban_user(target)
    await update.message.reply_text(f"✅ User `{target}` has been unbanned.", parse_mode="Markdown")


async def setcookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: instructions for updating cookies."""
    if update.effective_user.id != ADMIN_ID:
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
    """
    Admin-only: any .txt document sent by ADMIN_ID is treated as a fresh
    cookie file. The platform (TikTok or Facebook) is auto-detected from
    its content and saved to the matching runtime file, overriding the
    cookies baked into the script — no restart needed. Uploads from
    anyone else are silently ignored (not an error message, to avoid
    inviting random users to poke at this).
    """
    if update.effective_user.id != ADMIN_ID:
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

    try:
        with open(COOKIE_RUNTIME_FILES[platform], "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        logger.error("Failed to save cookie file: %s", e)
        await update.message.reply_text("❌ Couldn't save the file on the server.")
        return

    await update.message.reply_text(
        f"✅ {platform.capitalize()} cookies updated! "
        "New downloads will use these right away."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect platform, validate, and ask the user to pick Video vs Audio."""
    user_id = update.effective_user.id
    message = update.message
    text = message.text.strip()

    record_seen_user(user_id)

    # 0. Ban check
    if is_banned(user_id):
        await message.reply_text("🚫 You've been banned from using this bot.")
        return

    # 1. Rate limit
    if is_rate_limited(user_id):
        await message.reply_text("⏳ Please wait a few seconds before sending another link.")
        return

    # 2. Detect platform + validate
    platform, url = detect_platform(text)
    if not platform:
        await message.reply_text(
            "❌ Invalid link. Please send a valid TikTok, YouTube, or "
            "Facebook URL.\n\n"
            "TikTok: tiktok.com, vm.tiktok.com, vt.tiktok.com\n"
            "YouTube: youtube.com, youtu.be\n"
            "Facebook: facebook.com, fb.watch"
        )
        return

    # 3. Ask Video vs Audio-only via inline keyboard. The actual URL lives
    # server-side in _pending_requests — callback_data only carries the
    # short request id (Telegram caps callback_data at 64 bytes).
    request_id = new_request_id()
    _pending_requests[request_id] = {
        "url": url,
        "platform": platform,
        "user_id": user_id,
        "created": time.time(),
    }

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎬 Video", callback_data=f"fmt:{request_id}:video"),
                InlineKeyboardButton("🎵 Audio only", callback_data=f"fmt:{request_id}:audio"),
            ]
        ]
    )
    await message.reply_text(
        f"Detected a *{platform.capitalize()}* link. What would you like?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def handle_format_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback for the Video / Audio-only buttons — kicks off the actual
    download once the user picks a format."""
    query = update.callback_query
    await query.answer()

    try:
        _, request_id, fmt = query.data.split(":", 2)
    except ValueError:
        return

    pending = _pending_requests.pop(request_id, None)
    if pending is None:
        await query.edit_message_text("⌛ This request has expired — please send the link again.")
        return

    if pending["user_id"] != query.from_user.id:
        await query.answer("This isn't your request.", show_alert=True)
        return

    await query.edit_message_text(
        "⬇️ Getting ready to download… this can take a few minutes for "
        "large videos (up to 15 min max)."
    )
    await perform_download(
        context=context,
        chat_id=query.message.chat_id,
        status_message=query.message,
        url=pending["url"],
        platform=pending["platform"],
        user_id=pending["user_id"],
        audio_only=(fmt == "audio"),
        request_id=request_id,
    )


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback for the ❌ Cancel button attached to an in-progress download."""
    query = update.callback_query
    try:
        _, request_id = query.data.split(":", 1)
    except ValueError:
        await query.answer()
        return

    event = _active_cancel_events.get(request_id)
    if event is not None:
        event.set()
        await query.answer("Cancelling…")
    else:
        await query.answer("Nothing to cancel — it may have already finished.")


async def perform_download(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    status_message,
    url: str,
    platform: str,
    user_id: int,
    audio_only: bool,
    request_id: str,
) -> None:
    """
    Runs the actual yt-dlp download + upload, shared by both the format
    picker and (for now) a single entry point. Includes:
      - a live progress bar (run_progress_updates)
      - an "uploading video/audio" chat action while busy
      - a Cancel button wired to a threading.Event checked from yt-dlp's
        progress hook
      - automatic retry with exponential backoff on network-ish failures
      - platform-aware, human-readable error messages
      - admin DM on unexpected (non-user-facing) exceptions
    """
    cancel_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{request_id}")]]
    )
    try:
        await status_message.edit_reply_markup(reply_markup=cancel_keyboard)
    except Exception:
        pass

    cancel_event = threading.Event()
    _active_cancel_events[request_id] = cancel_event

    os.makedirs(TEMP_DIR, exist_ok=True)

    chat_action_stop = {"done": False}
    action = ChatAction.UPLOAD_VOICE if audio_only else ChatAction.UPLOAD_VIDEO
    action_task = asyncio.create_task(
        run_chat_action_loop(context, chat_id, action, chat_action_stop)
    )

    filepath, error = None, None
    try:
        attempt = 0
        while True:
            attempt += 1
            progress_state: dict = {"status": "starting"}
            stop_flag = {"done": False}
            updater_task = asyncio.create_task(
                run_progress_updates(status_message, progress_state, stop_flag)
            )
            try:
                filepath, error = await asyncio.wait_for(
                    asyncio.to_thread(
                        download_video,
                        url,
                        platform,
                        TEMP_DIR,
                        progress_state,
                        audio_only,
                        cancel_event,
                    ),
                    timeout=900,  # 15 minutes
                )
            except asyncio.TimeoutError:
                filepath, error = None, "Download timed out after 15 minutes."
            finally:
                stop_flag["done"] = True
                updater_task.cancel()
                try:
                    await updater_task
                except asyncio.CancelledError:
                    pass

            if filepath and os.path.exists(filepath):
                break  # success
            if cancel_event.is_set():
                break  # user cancelled — don't retry
            if not is_network_error(error) or attempt > NETWORK_RETRY_ATTEMPTS:
                break  # not retryable, or out of attempts

            delay = NETWORK_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            try:
                await status_message.edit_text(
                    f"📡 Network hiccup — retrying in {delay}s "
                    f"(attempt {attempt}/{NETWORK_RETRY_ATTEMPTS + 1})…",
                    reply_markup=cancel_keyboard,
                )
            except Exception:
                pass
            await asyncio.sleep(delay)
    except Exception as e:
        # Something unexpected (not a normal yt-dlp/network failure) blew
        # up here in our own orchestration code — worth an admin DM.
        logger.error("Unexpected error in perform_download: %s", e)
        await notify_admin(
            context,
            f"Unexpected exception in perform_download for user {user_id}, "
            f"url {url}:\n\n{traceback.format_exc()[-2000:]}",
        )
        filepath, error = None, str(e)
    finally:
        chat_action_stop["done"] = True
        action_task.cancel()
        try:
            await action_task
        except asyncio.CancelledError:
            pass
        _active_cancel_events.pop(request_id, None)

    if not filepath or not os.path.exists(filepath):
        friendly = friendly_error_message(platform, error or "")
        # Still surface a trimmed raw detail for diagnosability, without
        # leading with a wall of stack trace.
        detail = f"\n\n`{(error or '')[:200]}`" if error else ""
        try:
            await status_message.edit_text(
                friendly + detail, parse_mode="Markdown", reply_markup=None
            )
        except Exception:
            pass
        return

    # File size check — hard Telegram Bot API limit, not just a setting
    # here. A regular bot token cannot upload files over 50 MB no matter
    # how long the timeout is; that requires self-hosting the Bot API
    # server, which is a separate, bigger setup.
    size_mb = get_file_size_mb(filepath)
    if size_mb > MAX_FILE_SIZE_MB:
        os.remove(filepath)
        await status_message.edit_text(
            f"⚠️ File is too large ({size_mb:.1f} MB). "
            f"Telegram bots can only send files up to {MAX_FILE_SIZE_MB} MB.",
            reply_markup=None,
        )
        return

    # Send the file back
    await status_message.edit_text(
        "📤 Uploading to Telegram…" if not audio_only else "📤 Uploading audio…",
        reply_markup=None,
    )
    try:
        with open(filepath, "rb") as media:
            if audio_only:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=media,
                    caption="✅ Here's your audio!",
                    read_timeout=900,
                    write_timeout=900,
                    connect_timeout=60,
                    pool_timeout=60,
                )
            else:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=media,
                    caption="✅ Here's your video!",
                    read_timeout=900,
                    write_timeout=900,
                    connect_timeout=60,
                    pool_timeout=60,
                )
        await status_message.delete()
        record_download(user_id)
    except Exception as e:
        logger.error("Failed to send media: %s", e)
        await status_message.edit_text("❌ Failed to send the file. Please try again.")
        await notify_admin(context, f"Failed to send media to user {user_id}:\n\n{e}")
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
async def _post_init(application: Application) -> None:
    """Register the visible command menu (the ⌘ button in Telegram clients)."""
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Welcome message"),
            BotCommand("help", "How to use this bot"),
            BotCommand("myid", "Show your Telegram user ID"),
        ]
    )


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler — logs everything, and DMs the admin a trimmed
    traceback for anything that reaches here uncaught, so problems surface
    in Telegram instead of only sitting in a log file on the server."""
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)
    tb = "".join(
        traceback.format_exception(None, context.error, context.error.__traceback__)
    )
    await notify_admin(context, f"Unhandled exception:\n\n{tb[-2500:]}")


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        print("ERROR: Set BOT_TOKEN at the top of this script first!")
        return

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(900)
        .write_timeout(900)
        .connect_timeout(60)
        .pool_timeout(60)
        .post_init(_post_init)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("setcookies", setcookies_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_cookie_upload))
    application.add_handler(CallbackQueryHandler(handle_format_choice, pattern=r"^fmt:"))
    application.add_handler(CallbackQueryHandler(handle_cancel, pattern=r"^cancel:"))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )
    application.add_error_handler(_on_error)

    if application.job_queue is not None:
        application.job_queue.run_repeating(
            cleanup_rate_limiter,
            interval=RATE_LIMIT_CLEANUP_INTERVAL,
            first=RATE_LIMIT_CLEANUP_INTERVAL,
        )
    else:
        logger.warning(
            "JobQueue not available (install with `pip install "
            "\"python-telegram-bot[job-queue]\"`) — periodic rate-limiter "
            "cleanup is disabled; the dict will just grow unbounded."
        )

    if ADMIN_ID == 0:
        logger.warning(
            "ADMIN_ID is not set (still 0) — cookie updates via Telegram "
            "are disabled until you set it. DM the bot /myid to get your "
            "ID, then set ADMIN_ID at the top of this script and restart."
        )

    logger.info("Bot started. Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
