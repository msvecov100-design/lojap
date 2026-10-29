# Бэкенд для Telegram Stars + поиск соперников.
# Запуск: BOT_TOKEN=... python stars_backend.py   (порт берётся из PORT, по умолчанию 8080)
# Нужен HTTPS-адрес -> впишите его в indexx.html: const BOT_API='https://ваш-домен'
import asyncio, logging, os, random, sys, time
from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stars")

# Токен вписан прямо в файл. Репозиторий на GitHub ДОЛЖЕН быть приватным (Private).
# Если задана переменная BOT_TOKEN в Render, используется она.
TOKEN = (os.environ.get("BOT_TOKEN") or "9218724571:AAH51tGCAhXRkiUe4SV-hrMMULMCqjC3Fi4").strip()
if not TOKEN:
    sys.exit("Не задана переменная окружения BOT_TOKEN (токен бота от @BotFather)")
API = f"https://api.telegram.org/bot{TOKEN}"
PORT = int(os.environ.get("PORT", "8080"))

PRICES = {  # sku: (название, описание, цена в звёздах)
    "start": ("Стартовый набор", "5 круток «Плюс», удача ×2 на день, SIM «Золото»", 30),
    "vipw": ("Неделя VIP+", "VIP на 7 дней, удача ×2 и 10 круток «Плюс»", 120),
    "vip14": ("VIP на 14 дней", "Вторая дорожка наград, больше мест, значок VIP у ника", 150),
    "vip30": ("VIP на 30 дней", "Вторая дорожка наград, больше мест, значок VIP у ника", 240),
    "vipperm": ("VIP навсегда", "Вторая дорожка наград, больше мест, значок VIP у ника", 480),
}

# ---------- CORS ----------
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}

@web.middleware
async def cors_mw(req, handler):
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    try:
        resp = await handler(req)
    except web.HTTPException as e:
        resp = e
    except Exception:
        log.exception("handler error")
        resp = web.json_response({"error": "server"}, status=500)
    resp.headers.update(CORS)
    return resp

def err(code, status=400):
    return web.json_response({"error": code}, status=status)

def clean_uid(v):
    v = (v or "").strip()
    return v if 0 < len(v) <= 64 else None

# ---------- Telegram ----------
async def call(s, method, **p):
    try:
        async with s.post(f"{API}/{method}", json=p) as r:
            return await r.json()
    except Exception as e:
        log.warning("telegram %s failed: %r", method, e)
        return {"ok": False}

# ---------- Оплата ----------
async def invoice(req):
    sku = req.query.get("sku")
    if sku not in PRICES:
        return err("sku")
    uid = clean_uid(req.query.get("uid")) or ""
    t, d, p = PRICES[sku]
    r = await call(req.app["s"], "createInvoiceLink", title=t, description=d,
                   payload=f"{sku}:{uid}", provider_token="",
                   currency="XTR", prices=[{"label": t, "amount": p}])
    if not r.get("ok"):
        log.warning("createInvoiceLink: %s", r)
        return web.json_response({"error": "telegram", "detail": r.get("description", "no response")}, status=502)
    return web.json_response({"link": r["result"]})

# ---------- Поиск соперников ----------
# Бой начинается, только когда в очередь встал второй живой игрок.
Q, RES, SEEN = {}, {}, {}   # очередь (игра, ставка) -> uid; готовые результаты; последний визит
ALIVE = 6                   # сколько секунд игрок считается «живым» без запросов

async def queue(req):
    g, bet, u = req.query.get("game"), req.query.get("bet", ""), clean_uid(req.query.get("uid"))
    if g not in ("duel", "dice") or not u:
        return err("bad")
    now = time.time()
    SEEN[u] = now
    k = (g, bet)
    w = Q.get(k)
    if w and w != u and now - SEEN.get(w, 0) < ALIVE:
        del Q[k]
        lo, hi = (1, 100) if g == "duel" else (2, 12)
        a, c = random.randint(lo, hi), random.randint(lo, hi)
        RES[w] = {"done": 1, "me": a, "opp": c, "t": now}
        RES[u] = {"done": 1, "me": c, "opp": a, "t": now}
    else:
        Q[k] = u
    return web.json_response({"ok": 1})

async def mpoll(req):
    u = clean_uid(req.query.get("uid"))
    if not u:
        return err("bad")
    SEEN[u] = time.time()
    r = RES.pop(u, None)
    if r:
        r.pop("t", None)
        return web.json_response(r)
    return web.json_response({"done": 0})

async def cancel(req):
    u = clean_uid(req.query.get("uid"))
    if not u:
        return err("bad")
    for k in [k for k, v in Q.items() if v == u]:
        del Q[k]
    return web.json_response({"ok": 1})

async def health(req):
    return web.Response(text="ok")

async def cleaner():
    # чистим память от ушедших игроков и неразобранных результатов
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for k in [k for k, v in Q.items() if now - SEEN.get(v, 0) > ALIVE * 5]:
            del Q[k]
        for u in [u for u, r in RES.items() if now - r.get("t", 0) > 120]:
            del RES[u]
        for u in [u for u, t in SEEN.items() if now - t > 300]:
            del SEEN[u]

# ---------- Платежи: без ответа на pre_checkout оплата не пройдёт ----------
async def poll(s):
    await call(s, "deleteWebhook")   # getUpdates не работает, пока установлен webhook
    off = 0
    while True:
        u = await call(s, "getUpdates", offset=off, timeout=25,
                       allowed_updates=["pre_checkout_query", "message", "callback_query"])
        if not u.get("ok"):
            await asyncio.sleep(3)   # ошибка/конфликт (409) — не крутим цикл вхолостую
            continue
        for x in u.get("result", []):
            off = x["update_id"] + 1
            try:
                await bot_admin(s, x)
            except Exception:
                log.exception("admin")
            pq = x.get("pre_checkout_query")
            if pq:
                await call(s, "answerPreCheckoutQuery", pre_checkout_query_id=pq["id"], ok=True)
            m = x.get("message") or {}
            if "successful_payment" in m:
                log.info("paid: %s", m["successful_payment"].get("invoice_payload"))
                await call(s, "sendMessage", chat_id=m["chat"]["id"], text="Оплата прошла, спасибо! ⭐")


# =====================================================================
# АДМИНКА И СТАТИСТИКА: кто зашёл / кто вышел
# Переменные Render (необязательно): ADMIN_IDS (доп. админы через запятую),
# ADMIN_KEY (пароль веб-панели /admin?key=...), TZ_HOURS (по умолчанию 5),
# STATS_FILE (путь к файлу статистики; для сохранения между деплоями — Render Disk)
# =====================================================================
import hmac, hashlib, json, html as _html
from urllib.parse import parse_qsl

OWNER_IDS = {1301667270}
ADMINS = OWNER_IDS | {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
TZ_SEC = float(os.environ.get("TZ_HOURS", "5")) * 3600
STATS_FILE = os.environ.get("STATS_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.json")
ONLINE_MS = 75000   # нет пингов дольше 75 с — считаем, что вышел

DB = {"users": {}, "log": [], "notify": {}}
try:
    with open(STATS_FILE, encoding="utf-8") as _f:
        DB.update(json.load(_f))
except Exception:
    pass
_dirty = False
_sess = None   # aiohttp-сессия для отправки сообщений админам

def now_ms(): return int(time.time() * 1000)
def day_key(t=None): return time.strftime("%Y-%m-%d", time.gmtime((t or now_ms()) / 1000 + TZ_SEC))
def hhmm(t): return time.strftime("%H:%M", time.gmtime(t / 1000 + TZ_SEC))
def esc(s): return _html.escape(str(s if s is not None else ""), quote=False)
def fnum(n): return f"{round(n or 0):,}".replace(",", " ")
def dur(ms):
    s = round(ms / 1000)
    if s < 60: return f"{s} с"
    m = round(s / 60)
    return f"{m} мин" if m < 60 else f"{m // 60} ч {m % 60} мин"
def nm(r): return esc(r.get("name") or "Игрок") + (" @" + esc(r["un"]) if r.get("un") else "") + f" <code>{r['id']}</code>"

def check_init(init_data):
    """Проверка подписи initData от Telegram. Возвращает dict пользователя или None."""
    try:
        d = dict(parse_qsl(init_data or "", keep_blank_values=True))
        h = d.pop("hash", None)
        if not h: return None
        s = "\n".join(f"{k}={d[k]}" for k in sorted(d))
        sec = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(sec, s.encode(), hashlib.sha256).hexdigest(), h): return None
        if time.time() - int(d.get("auth_date", 0)) > 7 * 86400: return None
        return json.loads(d["user"])
    except Exception:
        return None

def notify(text):
    if not _sess: return
    for a in ADMINS:
        if DB["notify"].get(str(a)):
            asyncio.ensure_future(call(_sess, "sendMessage", chat_id=a, text=text, parse_mode="HTML", disable_notification=True))

def add_log(e):
    DB["log"].append(e)
    if len(DB["log"]) > 3000: del DB["log"][:len(DB["log"]) - 3000]

def end_session(r, at, why):
    global _dirty
    if not r["sAt"]: return
    d = max(0, at - r["sAt"]); r["time"] += d
    k = day_key(at); r["days"][k] = r["days"].get(k, 0) + d
    add_log({"t": at, "id": r["id"], "ev": "leave", "d": d, "why": why})
    r["sAt"] = 0; _dirty = True
    notify(f"🔴 Вышел: {nm(r)}\nв игре был: {dur(d)}")

def start_session(r, now, silent=False):
    global _dirty
    r["sAt"] = now; r["sessions"] += 1
    k = day_key(now); r["days"].setdefault(k, 0); _dirty = True
    add_log({"t": now, "id": r["id"], "ev": "join"})
    if not silent:
        notify(f"🟢 Зашёл: {nm(r)}\nур. {r.get('lvl') or 1} · {fnum(r.get('coins'))} коинов · сессия №{r['sessions']}")

def record(u, d):
    global _dirty
    now = now_ms(); key = str(u["id"]); r = DB["users"].get(key)
    if not r:
        r = DB["users"][key] = {"id": u["id"], "first": now, "sessions": 0, "time": 0, "days": {}, "sAt": 0, "seen": 0}
        add_log({"t": now, "id": u["id"], "ev": "new"})
        notify(f"🆕 Новый игрок: {esc(u.get('first_name'))} {'@' + esc(u['username']) if u.get('username') else ''} <code>{u['id']}</code>")
    r["name"] = u.get("first_name") or ""; r["un"] = u.get("username") or ""
    for k in ("coins", "lvl", "n", "col", "page"):
        r[k] = d.get(k)
    ev = d.get("ev")
    if ev == "open":
        if r["sAt"] and now - r["seen"] > ONLINE_MS: end_session(r, r["seen"], "timeout")
        if not r["sAt"]: start_session(r, now)
        r["seen"] = now
    elif ev == "ping":
        if not r["sAt"]: start_session(r, now, True)
        r["seen"] = now
    elif ev == "close":
        r["seen"] = now; end_session(r, now, "close")
    r["last"] = now; _dirty = True

def stats():
    now = now_ms(); U = list(DB["users"].values()); today = day_key(now)
    wk = [day_key(now - i * 86400000) for i in range(7)]
    online = [r for r in U if r["sAt"] and now - r["seen"] <= ONLINE_MS]
    act = lambda k: [r for r in U if k in r["days"]]
    return {"now": now, "total": len(U), "online": online, "dau": len(act(today)),
            "wau": len([r for r in U if any(k in r["days"] for k in wk)]),
            "newToday": len([r for r in U if day_key(r["first"]) == today]),
            "newWeek": len([r for r in U if now - r["first"] < 7 * 86400000]),
            "timeToday": sum(r["days"].get(today, 0) for r in U), "sessions": sum(r["sessions"] for r in U),
            "totalTime": sum(r["time"] for r in U), "days": [{"k": k, "n": len(act(k))} for k in reversed(wk)], "U": U}

def user_rows(st, n):
    return [{"id": r["id"], "name": r.get("name"), "un": r.get("un"), "on": bool(r["sAt"] and st["now"] - r["seen"] <= ONLINE_MS),
             "last": r.get("last", 0), "first": r.get("first", 0), "sessions": r["sessions"], "time": r["time"], "lvl": r.get("lvl"), "coins": r.get("coins")}
            for r in sorted(st["U"], key=lambda r: -r.get("last", 0))[:n]]

def log_rows(n):
    return [{**e, "name": (DB["users"].get(str(e["id"])) or {}).get("name")} for e in reversed(DB["log"][-n:])]

# ---- тексты для бота ----
def t_main():
    s = stats(); mx = max([1] + [d["n"] for d in s["days"]])
    avg = f" · средняя {dur(s['totalTime'] / s['sessions'])}" if s["sessions"] else ""
    bars = "\n".join(f"{d['k'][5:]}  {'█' * min(20, -(-d['n'] * 20 // mx))} {d['n']}" for d in s["days"])
    return (f"📊 <b>Статистика игры</b>\n\n🟢 Онлайн сейчас: <b>{len(s['online'])}</b>\n👥 Всего игроков: <b>{fnum(s['total'])}</b>\n"
            f"🆕 Новых сегодня: <b>{s['newToday']}</b> · за 7 дней: {s['newWeek']}\n📅 Заходили сегодня (DAU): <b>{s['dau']}</b>\n"
            f"📆 За 7 дней (WAU): <b>{s['wau']}</b>\n⏱ Время в игре сегодня: {dur(s['timeToday'])}\n🔁 Сессий всего: {fnum(s['sessions'])}{avg}\n\n<b>Активность по дням</b>\n{bars}")

def t_online():
    s = stats()
    if not s["online"]: return "🟢 Сейчас в игре никого нет."
    rows = sorted(s["online"], key=lambda r: -r["sAt"])[:40]
    return f"🟢 <b>Онлайн: {len(s['online'])}</b>\n\n" + "\n".join(
        f"• {nm(r)}\n   ур. {r.get('lvl') or 1} · {fnum(r.get('coins'))} · {esc(r.get('page') or 'home')} · в игре {dur(now_ms() - r['sAt'])}" for r in rows)

def t_log():
    l = DB["log"][-30:][::-1]
    if not l: return "Событий пока нет."
    ic = {"join": "🟢 зашёл", "leave": "🔴 вышел", "new": "🆕 новый"}
    out = []
    for e in l:
        r = DB["users"].get(str(e["id"])) or {"id": e["id"]}
        out.append(f"{day_key(e['t'])[5:]} {hhmm(e['t'])}  {ic.get(e['ev'], e['ev'])}  {esc(r.get('name'))} {'@' + esc(r['un']) if r.get('un') else ''} <code>{e['id']}</code>{' · ' + dur(e['d']) if e.get('d') else ''}")
    return "📜 <b>Последние события</b>\n\n" + "\n".join(out)

def t_top():
    U = sorted(stats()["U"], key=lambda r: -(r.get("coins") or 0))[:15]
    return "🏆 <b>Топ по коинам</b>\n\n" + "\n".join(
        f"{i + 1}. {esc(r.get('name') or 'Игрок')} {'@' + esc(r['un']) if r.get('un') else ''} — {fnum(r.get('coins'))} · ур. {r.get('lvl') or 1} · круток {fnum(r.get('n'))}" for i, r in enumerate(U))

def t_user(r):
    on = r["sAt"] and now_ms() - r["seen"] <= ONLINE_MS
    return (f"👤 {nm(r)}\n{'🟢 сейчас онлайн' if on else '⚪️ был: ' + day_key(r.get('last')) + ' ' + hhmm(r.get('last', 0))}\n"
            f"Первый вход: {day_key(r['first'])} {hhmm(r['first'])}\nСессий: {r['sessions']} · всего в игре {dur(r['time'])}\n"
            f"Ур. {r.get('lvl') or 1} · коинов {fnum(r.get('coins'))} · круток {fnum(r.get('n'))} · номеров {r.get('col') or 0}\nДней с активностью: {len(r['days'])}")

TEXTS = {"main": t_main, "online": t_online, "log": t_log, "top": t_top}

def adm_kb(uid):
    return {"inline_keyboard": [
        [{"text": "🟢 Онлайн", "callback_data": "adm_online"}, {"text": "📊 Сводка", "callback_data": "adm_main"}],
        [{"text": "📜 События", "callback_data": "adm_log"}, {"text": "🏆 Топ", "callback_data": "adm_top"}],
        [{"text": "🔔 Уведомления: ВКЛ" if DB["notify"].get(str(uid)) else "🔕 Уведомления: ВЫКЛ", "callback_data": "adm_notify"}]]}

async def bot_admin(s, x):
    """Команды и кнопки админа в боте. Вызывается из poll() для каждого апдейта."""
    global _dirty
    cq = x.get("callback_query")
    if cq and str(cq.get("data", "")).startswith("adm_"):
        uid = cq["from"]["id"]
        if uid not in ADMINS:
            return await call(s, "answerCallbackQuery", callback_query_id=cq["id"], text="Нет доступа")
        k = cq["data"][4:]; msg = cq.get("message") or {}; note = None
        if k == "notify":
            DB["notify"][str(uid)] = not DB["notify"].get(str(uid)); _dirty = True
            note = "Буду писать о каждом входе и выходе" if DB["notify"][str(uid)] else "Уведомления выключены"; k = "main"
        if k in TEXTS:
            await call(s, "answerCallbackQuery", callback_query_id=cq["id"], **({"text": note} if note else {}))
            await call(s, "editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"], text=TEXTS[k](), parse_mode="HTML", reply_markup=adm_kb(uid))
        return
    m = x.get("message") or {}; t = (m.get("text") or "").strip()
    if not t.startswith("/"): return
    parts = t.split(); cmd = parts[0].split("@")[0].lower(); uid = (m.get("from") or {}).get("id"); chat = m["chat"]["id"]
    if cmd == "/myid":
        await call(s, "sendMessage", chat_id=chat, text=f"Ваш Telegram ID: {uid}")
    elif cmd == "/admin" and uid in ADMINS:
        await call(s, "sendMessage", chat_id=chat, text=t_main(), parse_mode="HTML", reply_markup=adm_kb(uid))
    elif cmd == "/user" and uid in ADMINS:
        q = (parts[1] if len(parts) > 1 else "").lstrip("@").lower()
        r = next((r for r in DB["users"].values() if str(r["id"]) == q or (r.get("un") or "").lower() == q), None)
        await call(s, "sendMessage", chat_id=chat, text=t_user(r) if r else "Не найден. Использование: /user <id или @ник>", parse_mode="HTML")

# ---- HTTP ----
async def h_track(req):
    try:
        d = json.loads(await req.text()); u = check_init(d.get("initData"))
        if not u or d.get("ev") not in ("open", "ping", "close"): return web.Response(status=403)
        record(u, d); return web.Response(status=204)
    except Exception:
        return web.Response(status=400)

async def h_admin_api(req):
    try:
        d = json.loads(await req.text()); u = check_init(d.get("initData"))
        if not u or u["id"] not in ADMINS: return web.json_response({"admin": False})
        st = stats()
        return web.json_response({"admin": True, "online": len(st["online"]), "total": st["total"], "dau": st["dau"], "wau": st["wau"],
                                  "newToday": st["newToday"], "newWeek": st["newWeek"], "timeToday": st["timeToday"], "sessions": st["sessions"],
                                  "days": st["days"], "users": user_rows(st, 100), "log": log_rows(40)})
    except Exception:
        return web.Response(status=400)

def _key_ok(req): return bool(ADMIN_KEY) and req.query.get("key") == ADMIN_KEY

async def h_admin_data(req):
    if not _key_ok(req): return web.Response(status=403)
    st = stats()
    return web.json_response({"online": len(st["online"]), "total": st["total"], "dau": st["dau"], "wau": st["wau"], "newToday": st["newToday"],
                              "days": st["days"], "users": user_rows(st, 200), "log": log_rows(100)})

ADMIN_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Админка</title>
<style>body{font:14px system-ui;background:#0b0b0e;color:#fff;margin:0;padding:14px}h1{font-size:20px}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}.c{background:#17171c;border-radius:12px;padding:10px}.c b{display:block;font-size:24px}table{width:100%;border-collapse:collapse;margin:10px 0}td,th{padding:6px;border-bottom:1px solid #222;text-align:left;font-size:13px}.on{color:#3ddc84}.m{color:#888}</style>
<h1>📊 Статистика</h1><div class=g id=t></div><h3>Игроки</h3><table id=u></table><h3>События</h3><table id=l></table>
<script>const K=__KEY__,f=t=>new Date(t).toLocaleString('ru-RU'),n=x=>Math.round(x||0).toLocaleString('ru-RU'),d=ms=>Math.round(ms/60000)+' мин';
async function L(){const j=await (await fetch('/admin/data?key='+encodeURIComponent(K))).json();
t.innerHTML=[['Онлайн',j.online],['Всего',j.total],['Сегодня',j.dau],['7 дней',j.wau],['Новых сегодня',j.newToday]].map(a=>'<div class=c>'+a[0]+'<b>'+a[1]+'</b></div>').join('');
u.innerHTML='<tr><th></th><th>Игрок<th>Ур.<th>Коины<th>Сессий<th>В игре<th>Был</tr>'+j.users.map(r=>'<tr><td class='+(r.on?'on':'m')+'>●<td>'+(r.name||'')+' '+(r.un?'@'+r.un:'')+'<br><span class=m>'+r.id+'</span><td>'+(r.lvl||1)+'<td>'+n(r.coins)+'<td>'+r.sessions+'<td>'+d(r.time)+'<td>'+f(r.last)+'</tr>').join('');
l.innerHTML=j.log.map(e=>'<tr><td>'+f(e.t)+'<td>'+({join:'🟢 зашёл',leave:'🔴 вышел',new:'🆕 новый'}[e.ev])+'<td>'+(e.name||e.id)+'<td>'+(e.d?d(e.d):'')+'</tr>').join('')}
L();setInterval(L,10000)</script>"""

async def h_admin_page(req):
    if not _key_ok(req): return web.Response(status=403)
    return web.Response(text=ADMIN_PAGE.replace("__KEY__", json.dumps(ADMIN_KEY)), content_type="text/html")

async def admin_bg():
    """Раз в 10 с: закрывает «зависшие» сессии и сохраняет статистику в файл."""
    global _dirty
    while True:
        await asyncio.sleep(10)
        now = now_ms()
        for r in list(DB["users"].values()):
            if r["sAt"] and now - r["seen"] > ONLINE_MS: end_session(r, r["seen"], "timeout")
        if _dirty:
            _dirty = False
            try:
                with open(STATS_FILE, "w", encoding="utf-8") as f: json.dump(DB, f)
            except Exception as e:
                log.warning("stats save failed: %r", e)

async def bg(app):
    global _sess
    app["s"] = _sess = ClientSession(timeout=ClientTimeout(total=40))
    tasks = [asyncio.create_task(poll(app["s"])), asyncio.create_task(cleaner()), asyncio.create_task(admin_bg())]
    yield
    for t in tasks:
        t.cancel()
    await app["s"].close()

def make_app():
    app = web.Application(middlewares=[cors_mw])
    app.add_routes([web.get("/", health), web.get("/invoice", invoice), web.get("/queue", queue),
                    web.get("/poll", mpoll), web.get("/cancel", cancel),
                    web.post("/track", h_track), web.post("/admin/api", h_admin_api),
                    web.get("/admin/data", h_admin_data), web.get("/admin", h_admin_page)])
    app.cleanup_ctx.append(bg)
    return app

if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
