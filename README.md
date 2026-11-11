# 🌐 Telegram HTML Web Hosting Bot (v2)

User bot ko `.html` file bhejta hai, site ka naam deta hai, aur site turant live. Ek hi bot se **Render, Cloudflare, Netlify aur apna custom domain**, sab chal sakte hain.

**Stack:** Python, python-telegram-bot, Flask + waitress, Upstash Redis, Render

---

## 🧠 Domains kaise kaam karte hain (zaroor padho)

Bot (Python) sirf **Render** par chal sakta hai, kyunki Cloudflare Pages aur Netlify par Python server nahi chalta. Isliye:

```
Visitor ──► Cloudflare Worker / Netlify / Custom domain ──► Render (bot + sites) ──► Upstash Redis
```

- **Render** = asli server (yahi bot chalata hai)
- **Cloudflare** aur **Netlify** = "front door" (proxy). Ye sirf request Render ko forward karte hain. Inse tumhe unke domain milte hain (`xxx.workers.dev`, `xxx.netlify.app`, ya apna domain).
- Admin Panel → **🌍 Domains** se jitne chaho domain add karo. User ko site ke saath **saare links** dikhte hain.

| Domain type | Link kaisa banega | Kahan se milega |
|---|---|---|
| Path | `domain.com/sitename` | Render, Cloudflare Worker, Netlify, kisi bhi custom domain par |
| Subdomain (wildcard) | `sitename.domain.com` | Sirf Cloudflare par apna domain ho to (`*.domain.com`) |

---

## 🔑 Environment Variables (Render mein)

| Key | Value |
|---|---|
| `BOT_TOKEN` | @BotFather se mila token |
| `UPSTASH_REDIS_REST_URL` | Upstash REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash REST Token |
| `BASE_URL` | Render ka link bina `/` ke, jaise `https://yourapp.onrender.com` |
| `EXTRA_DOMAINS` | (optional) extra domains comma se: `sites.pages.dev,*.mydomain.com` |
| `ADMIN_ID` | Tumhara Telegram ID (kai ho to `123,456`) |
| `DEV_NAME` / `DEV_USERNAME` | Developer ka naam / Telegram username (bina `@`) |
| `PYTHON_VERSION` | `3.12.8` |
| `MAX_SITES` | Per user max sites (default `10`) |
| `MAX_FILE_MB` | Max file size MB (default `1`) |
| `CACHE_SECONDS` | Site cache seconds (default `60`) |
| `NOTIFY_ADMIN` | `0` karoge to nayi site ka alert band (default `1`) |
| `KEEP_ALIVE` | `0` karoge to self-ping band (default `1`) |

> Domains Admin Panel se bhi add ho jaate hain, to `EXTRA_DOMAINS` zaroori nahi.

---

## 🚀 Step 1: Bot + Render (pehle yahi karo)

1. **@BotFather** → `/newbot` → token copy. Apna ID **@userinfobot** se lo.
2. **upstash.com** → Create Database → REST URL aur REST TOKEN copy.
3. **GitHub** par naya repo banao, is folder ki saari files upload karo (`.env` kabhi nahi).
4. **render.com** → New → **Web Service** → repo connect:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python main.py`
   - Plan: **Free**
   - Upar wale Environment Variables daalo.
5. Deploy ho jaye to Render ka link (`https://xxx.onrender.com`) `BASE_URL` mein daalo → **Manual Deploy**.
6. Bot ko Telegram par `/start` karke test karo. Free plan par bot ab khud har 10 min mein ping karta hai (sleep nahi hoga).

Render par apna domain lagana ho (sabse aasan): Service → **Settings → Custom Domains → Add** → jo CNAME bole wo apne DNS mein daalo. Phir bot mein Admin Panel → 🌍 Domains → Add.

---

## ☁️ Step 2: Cloudflare (xxx.workers.dev ya apna domain)

1. dash.cloudflare.com → **Workers & Pages → Create → Worker** → naam do (jaise `html-host`) → Deploy.
2. **Edit code** dabao, `cloudflare/worker.js` ka poora code paste karo.
3. Upar `const ORIGIN = "https://YOUR-APP.onrender.com";` mein apna Render link daalo → **Deploy**.
4. Tumhara link mil gaya: `https://html-host.<tumhara-naam>.workers.dev`
5. Telegram bot → **🛡 Admin Panel → 🌍 Domains → ➕ Domain Add Karo** → `html-host.xxx.workers.dev` bhejo.

**Apna domain (Cloudflare par hona chahiye):**
- Worker → **Settings → Domains & Routes → Add → Custom Domain** → `sites.mydomain.com`. Bot mein `sites.mydomain.com` add karo. Link: `sites.mydomain.com/sitename`.
- **Subdomain mode (`sitename.mydomain.com`)**:
  1. DNS mein ek record: Type `AAAA`, Name `*`, Value `100::`, Proxy **ON** (orange cloud).
  2. Worker → Settings → Domains & Routes → **Add → Route** → `*.mydomain.com/*`.
  3. Bot mein `*.mydomain.com` add karo.
  - Dhyan: Cloudflare ka free SSL sirf ek level ke subdomain (`*.mydomain.com`) ko cover karta hai. `*.sites.mydomain.com` ke liye paid certificate chahiye.
  - Isse tumhare doosre subdomains (jaise `www`) par asar nahi padta jinke apne DNS records hain.

---

## 🟢 Step 3: Netlify (xxx.netlify.app ya apna domain)

1. `netlify/` folder ki `_redirects` file kholo, `YOUR-APP` ko apne Render link se badlo:
   ```
   /*  https://yourapp.onrender.com/:splat  200!
   ```
2. app.netlify.com → **Add new site → Deploy manually** → pura `netlify` folder drag & drop karo.
3. Netlify link dega: `https://random-name.netlify.app` (Site settings → Change site name se badal sakte ho).
4. Bot → Admin Panel → 🌍 Domains → us link ko add karo.
5. Apna domain: Netlify → **Domain management → Add a domain**, DNS Netlify ke batayi hui settings se point karo.
   - Netlify par sirf **path mode** (`domain/sitename`) chalta hai, subdomain wildcard ke liye Cloudflare use karo.

---

## 🛡 Admin Panel

- **👥 Users & Sites**: kisne kitni sites banayi, ban/unban, ya uski saari sites ek click mein delete
- **🌍 Domains**: domain add/remove, ⭐ primary chuno
- **📡 Channels**: force-join channels
- **📢 Broadcast**: sabko message
- Nayi site banne par admin ko alert aata hai, saath mein **🗑 Delete (spam/phishing)** button.

Commands: `/ban <id>`, `/unban <id>`, `/delsite <name>`

---

## ✨ v2 mein kya naya / theek hua

- Multi-domain support: Render + Cloudflare + Netlify + custom, path aur wildcard dono
- Admin se domain add/remove/primary
- waitress production server (Flask dev server nahi)
- Memory cache + ETag/Cache-Control: Upstash ke commands aur load kam
- Render free ke liye auto keep-alive
- Admin stats ab fast (`KEYS` scan hata diya, migration khud ho jaati hai)
- Nayi site par admin alert + one-tap delete, user ki saari sites delete
- Security: har button par owner check, admin ko ban nahi kar sakte, `nosniff` headers
- Broadcast mein rate-limit handling, error logging, `/help` command

---

## 💻 Local par chalana
```bash
pip install -r requirements.txt
cp .env.example .env     # values bharo
export $(grep -v '^#' .env | xargs)
python main.py
```

## ⚠️ Dhyan Rakho
- Saari sites ek hi domain par chalti hain. Phishing HTML ho sakti hai, isliye admin alert ka Delete button aur `/ban` use karte raho.
- Update ke baad change dikhne mein `CACHE_SECONDS` (60s) tak lag sakta hai.
- Single HTML file hi supported hai (CSS/JS file ke andar likho).
- Token kisi ke saath share mat karo.

Developed by **Aditya**
