"""
Telegram HTML Web Hosting Bot  (v2)

User .html file bhejta hai -> site live. Ek hi backend (Render) se kai domains
chalte hain: Render URL, Cloudflare Worker/domain, Netlify proxy, ya apna custom domain.

Stack: python-telegram-bot, Flask (+waitress), Upstash Redis
"""
import os
import re
import time
import asyncio
import hashlib
import html as H
import logging
import threading
import urllib.request

from flask import Flask, Response, request
from upstash_redis import Redis
from telegram import (
    Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("webhost-bot")

# =====================================================================
#  CONFIG  (sab ENV variables se aata hai)
# =====================================================================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_ID", "").replace(" ", "").split(",")
    if x.lstrip("-").isdigit()
}
DEV_NAME = os.environ.get("DEV_NAME", "Developer")
DEV_USERNAME = os.environ.get("DEV_USERNAME", "").lstrip("@")
MAX_SITES = int(os.environ.get("MAX_SITES", "10"))
MAX_BYTES = int(float(os.environ.get("MAX_FILE_MB", "1")) * 1024 * 1024)
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "60"))     # site cache (memory + browser/CDN)
NOTIFY_ADMIN = os.environ.get("NOTIFY_ADMIN", "1") != "0"       # nayi site par admin ko alert
KEEP_ALIVE = os.environ.get("KEEP_ALIVE", "1") != "0"           # Render free sleep rokne ke liye self-ping

RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")   # Render khud set karta hai
BASE_URL = (os.environ.get("BASE_URL") or RENDER_URL or "http://localhost:10000").rstrip("/")

redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,29}$")
RESERVED = {
    "admin", "api", "health", "static", "login", "bot", "www", "index",
    "favicon", "robots", "mail", "ftp", "app", "dashboard", "support",
}
DOMAIN_RE = re.compile(r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")

# =====================================================================
#  REDIS KEYS
#   site:<name>      -> html            owner:<name>   -> user id
#   user:<id>:sites  -> set of names    allsites       -> set of all site names
#   users / banned   -> sets            uinfo:<id>     -> "name\tusername"
#   channels, chan:<id>                 domains        -> extra domains (set)
#   primary_domain   -> main domain
# =====================================================================


# =====================================================================
#  SMALL TTL CACHE  (Upstash ke commands bachane ke liye)
# =====================================================================
class TTLCache:
    def __init__(self, max_items=256):
        self._d = {}
        self._lock = threading.Lock()
        self.max_items = max_items

    def get(self, key):
        """(hit, value) deta hai."""
        with self._lock:
            item = self._d.get(key)
            if item and item[0] > time.time():
                return True, item[1]
            self._d.pop(key, None)
        return False, None

    def set(self, key, value, ttl):
        with self._lock:
            if len(self._d) >= self.max_items:
                now = time.time()
                self._d = {k: v for k, v in self._d.items() if v[0] > now}
                if len(self._d) >= self.max_items:
                    self._d.clear()
            self._d[key] = (time.time() + ttl, value)

    def drop(self, key):
        with self._lock:
            self._d.pop(key, None)


site_cache = TTLCache(max_items=64)      # HTML content
misc_cache = TTLCache(max_items=2000)    # domains, ban status, join status
MAX_CACHE_ITEM = 512 * 1024              # isse bade HTML memory mein cache nahi honge
_seen_users = {}                         # uid -> (name, username), baar baar redis write na ho


# =====================================================================
#  DOMAINS  (Render / Cloudflare / Netlify / custom, sab ek saath)
# =====================================================================
def norm_domain(raw):
    """'https://my.site/' -> 'https://my.site' | '*.my.site' -> '*.my.site' | galat -> None"""
    s = (raw or "").strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0].strip()
    if not s or len(s) > 45 or not DOMAIN_RE.match(s):
        return None
    return s if s.startswith("*.") else "https://" + s


def _env_extra_domains():
    out = []
    for d in os.environ.get("EXTRA_DOMAINS", "").split(","):
        if not d.strip():
            continue
        n = norm_domain(d)
        if n:
            out.append(n)
        else:
            log.warning("EXTRA_DOMAINS mein galat domain ignore kiya: %s", d)
    return out


ENV_DOMAINS = [BASE_URL] + [d for d in _env_extra_domains() if d != BASE_URL]


def all_domains():
    """Primary sabse pehle. ENV domains + admin panel se add kiye domains."""
    hit, val = misc_cache.get("domains")
    if hit:
        return val
    extra, primary = [], None
    try:
        extra = sorted(str(x) for x in (redis.smembers("domains") or []))
        p = redis.get("primary_domain")
        primary = str(p) if p else None
    except Exception as e:
        log.warning("domains load failed: %s", e)
    out = []
    for d in ENV_DOMAINS + extra:
        if d and d not in out:
            out.append(d)
    if primary in out:
        out.remove(primary)
        out.insert(0, primary)
    misc_cache.set("domains", out, 30)
    return out


def domains_changed():
    misc_cache.drop("domains")


def link_for(domain: str, name: str) -> str:
    if domain.startswith("*."):
        return f"https://{name}.{domain[2:]}"
    return f"{domain}/{name}"


def site_url(name: str) -> str:
    return link_for(all_domains()[0], name)


def site_links(name: str):
    return [link_for(d, name) for d in all_domains()]


def links_text(name: str) -> str:
    return "\n".join(f"🔗 {u}" for u in site_links(name))


def url_ok(u: str) -> bool:
    """Telegram button sirf public https links leta hai."""
    return u.startswith("https://") and "localhost" not in u and "127.0.0.1" not in u


def open_row(name: str):
    u = site_url(name)
    return [Btn("🔗 Open Site", url=u)] if url_ok(u) else []


def wildcard_roots():
    return [d[2:] for d in all_domains() if d.startswith("*.")]


# =====================================================================
#  USER / SITE HELPERS
# =====================================================================
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_banned(uid: int) -> bool:
    key = f"ban:{uid}"
    hit, val = misc_cache.get(key)
    if hit:
        return val
    val = bool(redis.sismember("banned", str(uid)))
    misc_cache.set(key, val, 20)
    return val


def set_ban(uid, banned: bool):
    if banned:
        redis.sadd("banned", str(uid))
    else:
        redis.srem("banned", str(uid))
    misc_cache.drop(f"ban:{uid}")


def owns(name: str, uid: int) -> bool:
    return is_admin(uid) or str(redis.get(f"owner:{name}")) == str(uid)


def save_site(name: str, content: str, owner=None):
    redis.set(f"site:{name}", content)
    if owner is not None:
        redis.set(f"owner:{name}", str(owner))
        redis.sadd(f"user:{owner}:sites", name)
    redis.sadd("allsites", name)
    site_cache.drop(name)


def delete_site(name: str):
    owner = redis.get(f"owner:{name}")
    if owner:
        redis.srem(f"user:{owner}:sites", name)
    redis.delete(f"site:{name}", f"owner:{name}")
    redis.srem("allsites", name)
    site_cache.drop(name)


def migrate():
    """Purane version ki sites ko 'allsites' set mein daalta hai (sirf ek baar)."""
    try:
        if redis.get("migrated_v2"):
            return
        names = [str(k)[5:] for k in (redis.keys("site:*") or [])]
        for n in names:
            redis.sadd("allsites", n)
        redis.set("migrated_v2", "1")
        log.info("migration done: %d sites", len(names))
    except Exception as e:
        log.warning("migration skipped: %s", e)


# =====================================================================
#  FLASK  (sites serve karta hai)
# =====================================================================
web = Flask(__name__)

HOME_HTML = f"""<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Free HTML Hosting</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;text-align:center}}
h1{{font-size:2rem}}p{{color:#94a3b8}}</style></head>
<body><div><h1>🌐 Free HTML Hosting</h1>
<p>Telegram bot se apni HTML site live karo.</p>
<p>Developed by {H.escape(DEV_NAME)}</p></div></body></html>"""

PAGE_TMPL = """<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{code}</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;text-align:center}}</style></head>
<body><div><h1>{code}</h1><p>{msg}</p></div></body></html>"""

NOT_FOUND = PAGE_TMPL.format(code="404", msg="Ye site exist nahi karti.")
UNAVAILABLE = PAGE_TMPL.format(code="503", msg="Server abhi busy hai, thodi der baad try karo.")


def html_response(content: str, status: int = 200, cacheable: bool = False):
    resp = Response(content, status=status, mimetype="text/html")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if cacheable and status == 200:
        etag = hashlib.blake2b(content.encode("utf-8", "ignore"), digest_size=16).hexdigest()
        resp.set_etag(etag)
        resp.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
        resp.make_conditional(request)
    elif status == 404:
        resp.headers["Cache-Control"] = "public, max-age=10"
    return resp


def fetch_site(name: str):
    hit, val = site_cache.get(name)
    if hit:
        return val
    val = redis.get(f"site:{name}")
    if val is not None:
        val = str(val)
    if val is None:
        site_cache.set(name, None, 10)                       # bots ke baar baar 404 se bachne ke liye
    elif len(val) <= MAX_CACHE_ITEM:
        site_cache.set(name, val, max(CACHE_SECONDS, 1))
    return val


def serve_name(name: str):
    name = (name or "").lower()
    if not NAME_RE.match(name):
        return html_response(NOT_FOUND, 404)
    try:
        content = fetch_site(name)
    except Exception as e:
        log.error("serve failed for %s: %s", name, e)
        return html_response(UNAVAILABLE, 503)
    if not content:
        return html_response(NOT_FOUND, 404)
    return html_response(content, 200, cacheable=True)


def host_site():
    """name.mydomain.com jaisa wildcard host ho to 'name' lautata hai."""
    host = (request.headers.get("X-Original-Host") or request.host or "").split(":")[0].lower()
    for root in wildcard_roots():
        if host.endswith("." + root):
            sub = host[: -(len(root) + 1)]
            if "." not in sub and NAME_RE.match(sub):
                return sub
    return None


@web.route("/")
def home():
    sub = host_site()
    if sub:
        return serve_name(sub)
    return html_response(HOME_HTML)


@web.route("/health")
def health():
    return Response("ok", mimetype="text/plain", headers={"Cache-Control": "no-store"})


@web.route("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\n", mimetype="text/plain")


@web.route("/favicon.ico")
def favicon():
    return Response(status=204)


@web.route("/<name>", strict_slashes=False)
def serve_site(name):
    if host_site():                       # subdomain mode mein path wali site nahi chalti
        return html_response(NOT_FOUND, 404)
    return serve_name(name)


def run_web():
    port = int(os.environ.get("PORT", "10000"))
    try:
        from waitress import serve
        log.info("Web server: waitress, port %s", port)
        serve(web, host="0.0.0.0", port=port, threads=8, ident="webhost")
    except ImportError:
        log.warning("waitress nahi mila, Flask dev server chala raha hu")
        web.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)


def keep_alive():
    """Render Free 15 min idle par sleep ho jata hai, ye har 10 min khud ko ping karta hai."""
    target = (RENDER_URL or BASE_URL)
    if not url_ok(target):
        return
    url = target + "/health"
    log.info("Keep-alive ON: %s", url)
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(f"{url}?t={int(time.time())}", timeout=20).read()
        except Exception as e:
            log.debug("keep-alive ping failed: %s", e)


# =====================================================================
#  TEXTS & KEYBOARDS
# =====================================================================
def main_menu(uid: int) -> Markup:
    rows = [
        [Btn("📤 Site Upload Karo", callback_data="upload")],
        [Btn("📂 Meri Sites", callback_data="mysites"), Btn("ℹ️ Help", callback_data="help")],
    ]
    extra = []
    if DEV_USERNAME:
        extra.append(Btn("👨‍💻 Developer", url=f"https://t.me/{DEV_USERNAME}"))
    if is_admin(uid):
        extra.append(Btn("🛡 Admin Panel", callback_data="admin"))
    if extra:
        rows.append(extra)
    return Markup(rows)


def back_btn() -> Markup:
    return Markup([[Btn("🏠 Home", callback_data="home")]])


WELCOME = (
    "👋 <b>Welcome, {name}!</b>\n\n"
    "Main ek <b>Free HTML Hosting Bot</b> hu.\n"
    "Apni <code>.html</code> file bhejo, naam do, aur turant tumhari site live! 🚀\n\n"
    "👇 Neeche button se shuru karo"
)

HELP = (
    "ℹ️ <b>Kaise use karein</b>\n\n"
    "1️⃣ <b>📤 Site Upload Karo</b> dabao\n"
    "2️⃣ Apni <code>.html</code> file bhejo\n"
    "3️⃣ Site ka naam bhejo (a-z, 0-9, -)\n"
    "4️⃣ Link mil jayega ✅\n\n"
    f"📏 Max file size: <b>{MAX_BYTES / (1024 * 1024):g} MB</b>\n"
    f"🌐 Max sites per user: <b>{MAX_SITES}</b>\n"
    "✏️ Same site ko <b>Update</b> button se nayi file bhejke badal sakte ho.\n\n"
    "💡 CSS/JS apni HTML file ke andar hi likho (single file)."
)

JOIN_TEXT = (
    "🔒 <b>Bot use karne ke liye pehle ye channels join karo</b>\n\n"
    "Join karne ke baad neeche <b>✅ Maine Join Kar Liya</b> dabao."
)
PAGE = 8


async def safe_edit(q, text, markup=None):
    try:
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:  # message same ho to Telegram error deta hai, ignore
        log.debug("edit skipped: %s", e)


def chan_info(cid):
    raw = redis.get(f"chan:{cid}")
    if not raw:
        return str(cid), ""
    title, _, link = str(raw).partition("\t")
    return title or str(cid), link


def user_label(uid):
    raw = redis.get(f"uinfo:{uid}")
    if raw:
        name, _, un = str(raw).partition("\t")
        return (name or str(uid)), un
    return str(uid), ""


# =====================================================================
#  GUARD  (ban + force-join)
# =====================================================================
async def missing_channels(bot, uid: int):
    out = []
    for cid in sorted(redis.smembers("channels") or []):
        try:
            m = await bot.get_chat_member(int(cid), uid)
            if m.status in ("left", "kicked"):
                out.append(cid)
        except Exception as e:
            log.warning("channel check failed %s: %s", cid, e)
    return out


def join_markup(missing) -> Markup:
    rows = []
    for cid in missing:
        title, link = chan_info(cid)
        if link:
            rows.append([Btn(f"📢 {title[:30]}", url=link)])
    rows.append([Btn("✅ Maine Join Kar Liya", callback_data="chk")])
    return Markup(rows)


def touch_user(u):
    info = ((u.full_name or "").replace("\t", " "), u.username or "")
    if _seen_users.get(u.id) != info:
        redis.sadd("users", str(u.id))
        redis.set(f"uinfo:{u.id}", f"{info[0]}\t{info[1]}")
        _seen_users[u.id] = info


async def guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """True = aage badho. False = banned ya channel join nahi kiya."""
    u = update.effective_user
    uid = u.id
    q = update.callback_query
    if is_banned(uid):
        if q:
            await q.answer("🚫 Tum banned ho.", show_alert=True)
        else:
            await update.effective_message.reply_text("🚫 Tumhe is bot se ban kar diya gaya hai.")
        return False
    touch_user(u)

    if not is_admin(uid):
        if q and q.data == "chk":
            misc_cache.drop(f"join:{uid}")
        hit, _ = misc_cache.get(f"join:{uid}")
        if not hit:
            missing = await missing_channels(ctx.bot, uid)
            if missing:
                markup = join_markup(missing)
                if q:
                    await q.answer("❌ Pehle saare channel join karo", show_alert=True)
                    if q.data != "chk":
                        await safe_edit(q, JOIN_TEXT, markup)
                else:
                    await update.effective_message.reply_text(
                        JOIN_TEXT, parse_mode=ParseMode.HTML, reply_markup=markup)
                return False
            misc_cache.set(f"join:{uid}", True, 60)
    return True


# =====================================================================
#  ADMIN SCREENS
# =====================================================================
async def show_admin(q):
    users = redis.scard("users")
    banned = redis.scard("banned")
    sites = redis.scard("allsites")
    chans = redis.scard("channels")
    await safe_edit(
        q,
        "🛡 <b>Admin Panel</b>\n\n"
        f"👥 Total users: <b>{users}</b>\n"
        f"🌐 Total sites: <b>{sites}</b>\n"
        f"🚫 Banned: <b>{banned}</b>\n"
        f"📡 Force-join channels: <b>{chans}</b>\n"
        f"🌍 Domains: <b>{len(all_domains())}</b>\n\n"
        "<b>Commands:</b>\n"
        "<code>/ban user_id</code>\n"
        "<code>/unban user_id</code>\n"
        "<code>/delsite name</code>",
        Markup([
            [Btn("👥 Users & Sites", callback_data="users:0")],
            [Btn("🌍 Domains", callback_data="domains"), Btn("📡 Channels", callback_data="channels")],
            [Btn("📢 Broadcast", callback_data="broadcast")],
            [Btn("🔄 Refresh", callback_data="admin"), Btn("🏠 Home", callback_data="home")],
        ]),
    )


async def show_users(q, page: int):
    ids = sorted(redis.smembers("users") or [], key=lambda x: int(x))
    total = len(ids)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = ids[page * PAGE:(page + 1) * PAGE]
    rows = []
    for x in chunk:
        name, _ = user_label(x)
        cnt = redis.scard(f"user:{x}:sites")
        flag = "🚫 " if is_banned(int(x)) else ""
        rows.append([Btn(f"{flag}{name[:18]} • 🌐 {cnt}", callback_data=f"u:{x}:{page}")])
    nav = []
    if page > 0:
        nav.append(Btn("⬅️ Prev", callback_data=f"users:{page - 1}"))
    nav.append(Btn(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(Btn("Next ➡️", callback_data=f"users:{page + 1}"))
    rows.append(nav)
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    await safe_edit(
        q, f"👥 <b>Users ({total})</b>\n\nKisi user par tap karo, uski sites dikhengi.\n"
           "Format: naam • 🌐 sites ki sankhya", Markup(rows))


async def show_user(q, x: str, page: int):
    name, un = user_label(x)
    sites = sorted(redis.smembers(f"user:{x}:sites") or [])
    banned = is_banned(int(x))
    text = (
        f"👤 <b>{H.escape(name)}</b>\n"
        f"🆔 <code>{x}</code>\n"
        + (f"🔗 @{H.escape(un)}\n" if un else "")
        + f"🌐 Sites: <b>{len(sites)}</b>\n"
        f"🚦 Status: <b>{'Banned 🚫' if banned else 'Active ✅'}</b>\n"
    )
    if sites:
        text += "\n" + "\n".join(f'• <a href="{site_url(n)}">{H.escape(n)}</a>' for n in sites[:40])
    toggle = (Btn("✅ Unban", callback_data=f"unban:{x}:{page}") if banned
              else Btn("🚫 Ban", callback_data=f"ban:{x}:{page}"))
    rows = [[toggle]]
    if sites:
        rows.append([Btn("🗑 Isski saari sites delete", callback_data=f"udel:{x}:{page}")])
    rows.append([Btn("⬅️ Back", callback_data=f"users:{page}")])
    await safe_edit(q, text, Markup(rows))


async def show_channels(q):
    chans = sorted(redis.smembers("channels") or [])
    rows = []
    for c in chans:
        title, _ = chan_info(c)
        rows.append([Btn(f"❌ {title[:28]}", callback_data=f"chdel:{c}")])
    rows.append([Btn("➕ Channel Add Karo", callback_data="addch")])
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    text = (
        "📡 <b>Force-Join Channels</b>\n\n"
        "Users ko bot use karne se pehle ye channels join karne honge.\n"
        "Hatane ke liye channel par tap karo.\n\n"
        + (f"Total: <b>{len(chans)}</b>" if chans else "Abhi koi channel add nahi hai.")
    )
    await safe_edit(q, text, Markup(rows))


async def show_domains(q):
    doms = all_domains()
    try:
        stored = {str(x) for x in (redis.smembers("domains") or [])}
    except Exception:
        stored = set()
    lines, rows = [], []
    for i, d in enumerate(doms):
        label = d.replace("https://", "")
        tag = " ⭐" if i == 0 else ""
        kind = "subdomain" if d.startswith("*.") else "path"
        src = "panel" if d in stored else "ENV"
        lines.append(f"{i + 1}. <code>{H.escape(label)}</code>{tag}  <i>({kind}, {src})</i>")
        row = []
        if i != 0:
            row.append(Btn(f"⭐ {label[:22]}", callback_data=f"dmp:{d}"))
        if d in stored:
            row.append(Btn(f"❌ {label[:22]}", callback_data=f"dmd:{d}"))
        if row:
            rows.append(row)
    rows.append([Btn("➕ Domain Add Karo", callback_data="adddm")])
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    await safe_edit(
        q,
        "🌍 <b>Domains</b>\n\n" + "\n".join(lines) + "\n\n"
        "⭐ = primary (users ko pehle yahi link dikhega). Baaki links bhi site ke saath dikhte hain.\n"
        "<b>path</b> = <code>domain.com/sitename</code>\n"
        "<b>subdomain</b> = <code>sitename.domain.com</code> (wildcard)\n"
        "ENV wale domains Render ke Environment se hi hatenge.",
        Markup(rows),
    )


# =====================================================================
#  COMMANDS
# =====================================================================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    ctx.user_data.clear()
    u = update.effective_user
    await update.message.reply_text(
        WELCOME.format(name=H.escape(u.first_name or "dost")),
        parse_mode=ParseMode.HTML, reply_markup=main_menu(u.id),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML, reply_markup=back_btn())


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Use: /ban <user_id>")
    if int(ctx.args[0]) in ADMIN_IDS:
        return await update.message.reply_text("❌ Admin ko ban nahi kar sakte.")
    set_ban(ctx.args[0], True)
    await update.message.reply_text(f"🚫 {ctx.args[0]} ban ho gaya.")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Use: /unban <user_id>")
    set_ban(ctx.args[0], False)
    await update.message.reply_text(f"✅ {ctx.args[0]} unban ho gaya.")


async def cmd_delsite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        return await update.message.reply_text("Use: /delsite <name>")
    name = ctx.args[0].lower()
    if not redis.exists(f"site:{name}"):
        return await update.message.reply_text("❌ Aisi site nahi mili.")
    delete_site(name)
    await update.message.reply_text(f"🗑 <code>{H.escape(name)}</code> delete ho gayi.", parse_mode=ParseMode.HTML)


# =====================================================================
#  BUTTONS
# =====================================================================
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    q = update.callback_query
    uid = q.from_user.id
    data = q.data
    await q.answer()
    deny = "Ye tumhari site nahi hai."

    if data in ("home", "chk"):
        ctx.user_data.clear()
        await safe_edit(q, WELCOME.format(name=H.escape(q.from_user.first_name or "dost")), main_menu(uid))

    elif data == "help":
        await safe_edit(q, HELP, back_btn())

    elif data == "upload":
        count = redis.scard(f"user:{uid}:sites")
        if count >= MAX_SITES and not is_admin(uid):
            return await safe_edit(
                q, f"❌ Tumhari limit ({MAX_SITES} sites) poori ho gayi.\nPehle koi site delete karo.",
                Markup([[Btn("📂 Meri Sites", callback_data="mysites")], [Btn("🏠 Home", callback_data="home")]]),
            )
        ctx.user_data.clear()
        ctx.user_data["state"] = "await_file"
        await safe_edit(
            q, "📤 <b>Ab apni .html file bhejo</b>\n\n(File ko document ki tarah attach karke bhejo)",
            Markup([[Btn("❌ Cancel", callback_data="home")]]),
        )

    elif data == "mysites":
        names = sorted(redis.smembers(f"user:{uid}:sites") or [])
        if not names:
            return await safe_edit(
                q, "📂 Tumhari koi site nahi hai abhi.",
                Markup([[Btn("📤 Upload Karo", callback_data="upload")], [Btn("🏠 Home", callback_data="home")]]),
            )
        rows = [[Btn(f"🌐 {n}", callback_data=f"site:{n}")] for n in names]
        rows.append([Btn("🏠 Home", callback_data="home")])
        await safe_edit(q, f"📂 <b>Tumhari Sites ({len(names)})</b>\n\nKisi par tap karo:", Markup(rows))

    elif data.startswith("site:"):
        name = data[5:]
        if not owns(name, uid):
            return await q.answer(deny, show_alert=True)
        rows = []
        if open_row(name):
            rows.append(open_row(name))
        rows.append([Btn("♻️ Update", callback_data=f"upd:{name}"), Btn("🗑 Delete", callback_data=f"del:{name}")])
        rows.append([Btn("⬅️ Back", callback_data="mysites")])
        await safe_edit(q, f"🌐 <b>{H.escape(name)}</b>\n\n{links_text(name)}", Markup(rows))

    elif data.startswith("upd:"):
        name = data[4:]
        if not owns(name, uid):
            return await q.answer(deny, show_alert=True)
        ctx.user_data.clear()
        ctx.user_data["state"] = "await_file"
        ctx.user_data["update_name"] = name
        await safe_edit(
            q, f"♻️ <b>{H.escape(name)}</b> update karne ke liye nayi .html file bhejo.",
            Markup([[Btn("❌ Cancel", callback_data=f"site:{name}")]]),
        )

    elif data.startswith("del:"):
        name = data[4:]
        if not owns(name, uid):
            return await q.answer(deny, show_alert=True)
        await safe_edit(
            q, f"⚠️ Pakka <b>{H.escape(name)}</b> delete karni hai?",
            Markup([[Btn("✅ Haan, Delete", callback_data=f"delok:{name}"), Btn("❌ Nahi", callback_data=f"site:{name}")]]),
        )

    elif data.startswith("delok:"):
        name = data[6:]
        if not owns(name, uid):
            return await q.answer(deny, show_alert=True)
        delete_site(name)
        await safe_edit(q, f"🗑 <b>{H.escape(name)}</b> delete ho gayi.",
                        Markup([[Btn("📂 Meri Sites", callback_data="mysites")], [Btn("🏠 Home", callback_data="home")]]))

    # ----- ADMIN -----
    elif data == "noop":
        pass

    elif not is_admin(uid):
        return

    elif data == "admin":
        await show_admin(q)

    elif data.startswith("users:"):
        await show_users(q, int(data.split(":")[1]))

    elif data.startswith("u:"):
        _, x, pg = data.split(":")
        await show_user(q, x, int(pg))

    elif data.startswith(("ban:", "unban:")):
        act, x, pg = data.split(":")
        if int(x) in ADMIN_IDS:
            return await q.answer("Admin ko ban nahi kar sakte.", show_alert=True)
        set_ban(x, act == "ban")
        await show_user(q, x, int(pg))

    elif data.startswith("udel:"):
        _, x, pg = data.split(":")
        n = redis.scard(f"user:{x}:sites")
        await safe_edit(
            q, f"⚠️ User <code>{x}</code> ki <b>{n}</b> sites delete karni hain?",
            Markup([[Btn("✅ Haan, Delete", callback_data=f"udelok:{x}:{pg}"), Btn("❌ Nahi", callback_data=f"u:{x}:{pg}")]]),
        )

    elif data.startswith("udelok:"):
        _, x, pg = data.split(":")
        for n in list(redis.smembers(f"user:{x}:sites") or []):
            delete_site(str(n))
        await show_user(q, x, int(pg))

    elif data.startswith("adel:"):       # naye-site alert se turant delete
        name = data[5:]
        delete_site(name)
        await safe_edit(q, f"🗑 <b>{H.escape(name)}</b> delete kar di gayi.")

    elif data == "channels":
        await show_channels(q)

    elif data == "addch":
        ctx.user_data.clear()
        ctx.user_data["state"] = "add_channel"
        await safe_edit(
            q,
            "📡 <b>Channel add karo</b>\n\n"
            "1️⃣ Pehle bot ko us channel mein <b>Admin</b> banao\n"
            "2️⃣ Phir channel ka <code>@username</code> ya ID bhejo,\n"
            "    ya channel ka koi message yahan <b>forward</b> karo.",
            Markup([[Btn("❌ Cancel", callback_data="channels")]]),
        )

    elif data.startswith("chdel:"):
        cid = data[6:]
        redis.srem("channels", cid)
        redis.delete(f"chan:{cid}")
        await show_channels(q)

    elif data == "domains":
        await show_domains(q)

    elif data == "adddm":
        ctx.user_data.clear()
        ctx.user_data["state"] = "add_domain"
        await safe_edit(
            q,
            "🌍 <b>Domain add karo</b>\n\n"
            "Domain bhejo, jaise:\n"
            "• <code>mysite.pages.dev</code> ya <code>https://sites.mydomain.com</code>\n"
            "   → link: <code>domain/sitename</code>\n"
            "• <code>*.mydomain.com</code>\n"
            "   → link: <code>sitename.mydomain.com</code> (wildcard)\n\n"
            "⚠️ Pehle domain ko Cloudflare Worker / Netlify proxy / Render Custom Domain se is bot ke server par point kar do.",
            Markup([[Btn("❌ Cancel", callback_data="domains")]]),
        )

    elif data.startswith("dmp:"):
        redis.set("primary_domain", data[4:])
        domains_changed()
        await show_domains(q)

    elif data.startswith("dmd:"):
        d = data[4:]
        redis.srem("domains", d)
        if str(redis.get("primary_domain")) == d:
            redis.delete("primary_domain")
        domains_changed()
        await show_domains(q)

    elif data == "broadcast":
        ctx.user_data.clear()
        ctx.user_data["state"] = "broadcast"
        await safe_edit(q, "📢 Jo message sabko bhejna hai wo ab likh kar bhejo.",
                        Markup([[Btn("❌ Cancel", callback_data="admin")]]))


# =====================================================================
#  FILE RECEIVE
# =====================================================================
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    uid = update.effective_user.id
    doc = update.message.document
    fname = (doc.file_name or "").lower()

    if not fname.endswith((".html", ".htm")):
        return await update.message.reply_text("❌ Sirf <code>.html</code> file bhejo.", parse_mode=ParseMode.HTML)
    if doc.file_size and doc.file_size > MAX_BYTES:
        return await update.message.reply_text(f"❌ File bahut badi hai. Max {MAX_BYTES / (1024 * 1024):g} MB.")

    update_name = ctx.user_data.get("update_name")
    if not update_name:
        if redis.scard(f"user:{uid}:sites") >= MAX_SITES and not is_admin(uid):
            return await update.message.reply_text(f"❌ Limit ({MAX_SITES} sites) poori ho gayi.")
    elif not owns(update_name, uid):
        ctx.user_data.clear()
        return await update.message.reply_text("❌ Ye site tumhari nahi hai.")

    f = await doc.get_file()
    raw = bytes(await f.download_as_bytearray())
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")

    if update_name:
        save_site(update_name, content)
        ctx.user_data.clear()
        rows = []
        if open_row(update_name):
            rows.append(open_row(update_name))
        rows.append([Btn("🏠 Home", callback_data="home")])
        return await update.message.reply_text(
            f"✅ <b>{H.escape(update_name)}</b> update ho gayi!\n\n{links_text(update_name)}\n\n"
            f"<i>(Browser/CDN cache ki wajah se change dikhne mein ~{CACHE_SECONDS}s lag sakte hain)</i>",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=Markup(rows),
        )

    ctx.user_data["html"] = content
    ctx.user_data["state"] = "await_name"
    await update.message.reply_text(
        "✅ File mil gayi!\n\n✍️ Ab <b>site ka naam</b> bhejo.\n"
        "Rules: 3-30 characters, sirf <code>a-z 0-9 -</code>\n"
        "Example: <code>my-portfolio</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=Markup([[Btn("❌ Cancel", callback_data="home")]]),
    )


# =====================================================================
#  TEXT
# =====================================================================
async def notify_admins(ctx, uid, user, name):
    if not NOTIFY_ADMIN:
        return
    text = (
        f"🆕 <b>Nayi site</b>: <code>{H.escape(name)}</code>\n"
        f"👤 {H.escape(user.full_name or str(uid))}"
        + (f" (@{H.escape(user.username)})" if user.username else "")
        + f"\n🆔 <code>{uid}</code>\n{site_url(name)}"
    )
    markup = Markup([[Btn("🗑 Delete (spam/phishing)", callback_data=f"adel:{name}")]])
    for a in ADMIN_IDS:
        if a == uid:
            continue
        try:
            await ctx.bot.send_message(a, text, parse_mode=ParseMode.HTML,
                                       reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            log.debug("admin notify failed: %s", e)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    state = ctx.user_data.get("state")

    if state == "add_channel" and is_admin(uid):
        ref = None
        fo = getattr(update.message, "forward_origin", None)
        if fo is not None and getattr(fo, "chat", None) is not None:
            ref = fo.chat.id
        elif text.startswith("@"):
            ref = text
        elif text.lstrip("-").isdigit():
            ref = int(text)
        if ref is None:
            return await update.message.reply_text("❌ @username, ID ya forwarded message bhejo.")
        try:
            chat = await ctx.bot.get_chat(ref)
            me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
            if me.status not in ("administrator", "creator"):
                raise ValueError("bot admin nahi hai")
        except Exception as e:
            log.warning("add channel failed: %s", e)
            return await update.message.reply_text(
                "❌ Channel nahi mila ya bot us channel mein admin nahi hai.\n"
                "Bot ko admin banao aur dobara bhejo.")
        link = f"https://t.me/{chat.username}" if chat.username else ""
        if not link:
            try:
                link = chat.invite_link or await ctx.bot.export_chat_invite_link(chat.id)
            except Exception:
                link = ""
        title = (chat.title or str(chat.id)).replace("\t", " ")
        redis.sadd("channels", str(chat.id))
        redis.set(f"chan:{chat.id}", f"{title}\t{link}")
        misc_cache.drop(f"join:{uid}")
        ctx.user_data.clear()
        warn = "" if link else "\n\n⚠️ Invite link nahi mila. Bot ko 'Invite users' permission do, warna users ko join button nahi dikhega."
        return await update.message.reply_text(
            f"✅ <b>{H.escape(title)}</b> add ho gaya!{warn}",
            parse_mode=ParseMode.HTML,
            reply_markup=Markup([[Btn("📡 Channels", callback_data="channels")], [Btn("🛡 Admin Panel", callback_data="admin")]]),
        )

    if state == "add_domain" and is_admin(uid):
        d = norm_domain(text)
        if not d:
            return await update.message.reply_text(
                "❌ Galat domain. Example: mysite.pages.dev, sites.mydomain.com ya *.mydomain.com")
        redis.sadd("domains", d)
        domains_changed()
        ctx.user_data.clear()
        demo = link_for(d, "demo")
        return await update.message.reply_text(
            f"✅ Domain add ho gaya!\n\nTest link: {demo}\n\n"
            "Agar ye link khulta nahi to domain ko Render/Cloudflare/Netlify se point karna baaki hai (README dekho).",
            disable_web_page_preview=True,
            reply_markup=Markup([[Btn("🌍 Domains", callback_data="domains")]]),
        )

    if state == "broadcast" and is_admin(uid):
        users = list(redis.smembers("users") or [])
        ok = fail = 0
        await update.message.reply_text(f"📢 Bhej raha hu... ({len(users)} users)")
        for u in users:
            for attempt in range(2):
                try:
                    await ctx.bot.send_message(int(u), text)
                    ok += 1
                    break
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception:
                    fail += 1
                    break
            else:
                fail += 1
            await asyncio.sleep(0.05)        # Telegram limit (~30 msg/sec) se bachne ke liye
        ctx.user_data.clear()
        return await update.message.reply_text(f"✅ Done\nSent: {ok}\nFailed: {fail}", reply_markup=back_btn())

    if state == "await_name":
        name = text.lower()
        if not NAME_RE.match(name):
            return await update.message.reply_text(
                "❌ Galat naam. 3-30 characters, sirf <code>a-z 0-9 -</code>, shuru letter/number se.",
                parse_mode=ParseMode.HTML,
            )
        if name in RESERVED:
            return await update.message.reply_text("❌ Ye naam reserved hai, koi aur naam do.")
        if redis.exists(f"site:{name}"):
            return await update.message.reply_text("❌ Ye naam pehle se le liya gaya hai. Koi aur naam do.")

        html_content = ctx.user_data.get("html")
        if not html_content:
            ctx.user_data.clear()
            return await update.message.reply_text("⚠️ File nahi mili, dobara upload karo.", reply_markup=main_menu(uid))

        save_site(name, html_content, owner=uid)
        ctx.user_data.clear()

        rows = []
        if open_row(name):
            rows.append(open_row(name))
        rows.append([Btn("📂 Meri Sites", callback_data="mysites"), Btn("🏠 Home", callback_data="home")])
        await update.message.reply_text(
            f"🎉 <b>Site live ho gayi!</b>\n\n🌐 Naam: <code>{name}</code>\n{links_text(name)}",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=Markup(rows),
        )
        return await notify_admins(ctx, uid, update.effective_user, name)

    await update.message.reply_text("👇 Menu se option chuno", reply_markup=main_menu(uid))


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Update handle karte waqt error", exc_info=ctx.error)


# =====================================================================
#  RUN
# =====================================================================
def main():
    migrate()
    threading.Thread(target=run_web, daemon=True).start()
    if KEEP_ALIVE:
        threading.Thread(target=keep_alive, daemon=True).start()
    # Python 3.14 mein event loop khud banana padta hai
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("delsite", cmd_delsite))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("Bot started | domains: %s", ", ".join(ENV_DOMAINS))
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
