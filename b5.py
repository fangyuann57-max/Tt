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
import time
import asyncio
import logging
import tempfile

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import yt_dlp

# ----------------------------------------------------------------------------
# CONFIG — fill these in before running
# ----------------------------------------------------------------------------
BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"  # from @BotFather

# Your Telegram user ID — only this account can update cookies via the bot.
# Send /myid to the bot once to find out your ID, then set it here.
ADMIN_ID = 0

MAX_FILE_SIZE_MB = 50        # Hard limit on Telegram's standard Bot API
RATE_LIMIT_SECONDS = 10      # 1 request per user per 10s
TEMP_DIR = "downloads"

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
def download_video(url: str, platform: str, out_dir: str) -> tuple[str | None, str | None]:
    """
    Download a video using yt-dlp.
    Returns (filepath, None) on success, or (None, error_message) on failure.
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

    if platform == "youtube":
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


def get_file_size_mb(path: str) -> float:
    """Return file size in megabytes."""
    return os.path.getsize(path) / (1024 * 1024)


# ----------------------------------------------------------------------------
# Telegram handlers
# ----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        "3. Wait — large videos can take a few minutes.\n\n"
        "*Commands*\n"
        "/start — Welcome message\n"
        "/help — This help text\n\n"
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
    """Detect platform, validate, download, and send the video back."""
    user_id = update.effective_user.id
    message = update.message
    text = message.text.strip()

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

    # 3. Status message
    status = await message.reply_text(
        f"⬇️ Downloading… this can take a few minutes for large videos "
        f"(up to 15 min max)."
    )

    # 4. Download — runs in a background thread so this one long download
    # doesn't block the bot from handling other users' messages at the
    # same time (yt-dlp's extract_info call is blocking/synchronous).
    os.makedirs(TEMP_DIR, exist_ok=True)
    try:
        filepath, error = await asyncio.wait_for(
            asyncio.to_thread(download_video, url, platform, TEMP_DIR),
            timeout=900,  # 15 minutes
        )
    except asyncio.TimeoutError:
        filepath, error = None, "Download timed out after 15 minutes."

    if not filepath or not os.path.exists(filepath):
        # Show the real yt-dlp error (trimmed) so failures are diagnosable
        # straight from Telegram without needing to check terminal logs.
        detail = f"\n\n`{error[:300]}`" if error else ""
        await status.edit_text(
            "❌ Couldn't download the video. It may be private, deleted, or not found."
            + detail,
            parse_mode="Markdown",
        )
        return

    # 5. File size check — this is a hard Telegram Bot API limit, not just
    # a setting here. A regular bot token cannot upload files over 50 MB
    # no matter how long the timeout is; that requires self-hosting the
    # Bot API server, which is a separate, bigger setup.
    size_mb = get_file_size_mb(filepath)
    if size_mb > MAX_FILE_SIZE_MB:
        os.remove(filepath)
        await status.edit_text(
            f"⚠️ Video is too large ({size_mb:.1f} MB). "
            f"Telegram bots can only send files up to {MAX_FILE_SIZE_MB} MB."
        )
        return

    # 6. Send video back
    await status.edit_text("📤 Uploading to Telegram…")
    try:
        with open(filepath, "rb") as video:
            await message.reply_video(
                video,
                caption="✅ Here's your video!",
                read_timeout=900,
                write_timeout=900,
                connect_timeout=60,
                pool_timeout=60,
            )
        await status.delete()
    except Exception as e:
        logger.error("Failed to send video: %s", e)
        await status.edit_text("❌ Failed to send the video. Please try again.")
    finally:
        # 7. Cleanup temp file
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
    application.add_handler(MessageHandler(filters.Document.ALL, handle_cookie_upload))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
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
