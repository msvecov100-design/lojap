# Бэкенд для Telegram Stars + поиск соперников.
# Запуск: BOT_TOKEN=... python stars_backend.py   (порт берётся из PORT, по умолчанию 8080)
# Нужен HTTPS-адрес -> впишите его в indexx.html: const BOT_API='https://ваш-домен'
import asyncio, logging, os, random, sys, time
from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stars")

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    sys.exit("Не задана переменная окружения BOT_TOKEN (токен бота от @BotFather)")
API = f"https://api.telegram.org/bot{TOKEN}"
PORT = int(os.environ.get("PORT", "8080"))

PRICES = {  # sku: (название, описание, цена в звёздах)
    "start": ("Стартовый набор", "5 круток «Плюс», удача ×2 на день, SIM «Золото»", 59),
    "vipw": ("Неделя VIP+", "VIP на 7 дней, удача ×2 и 10 круток «Плюс»", 239),
}

# ---------- CORS ----------
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
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
        return err("telegram", 502)
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
                       allowed_updates=["pre_checkout_query", "message"])
        if not u.get("ok"):
            await asyncio.sleep(3)   # ошибка/конфликт (409) — не крутим цикл вхолостую
            continue
        for x in u.get("result", []):
            off = x["update_id"] + 1
            pq = x.get("pre_checkout_query")
            if pq:
                await call(s, "answerPreCheckoutQuery", pre_checkout_query_id=pq["id"], ok=True)
            m = x.get("message") or {}
            if "successful_payment" in m:
                log.info("paid: %s", m["successful_payment"].get("invoice_payload"))
                await call(s, "sendMessage", chat_id=m["chat"]["id"], text="Оплата прошла, спасибо! ⭐")

async def bg(app):
    app["s"] = ClientSession(timeout=ClientTimeout(total=40))
    tasks = [asyncio.create_task(poll(app["s"])), asyncio.create_task(cleaner())]
    yield
    for t in tasks:
        t.cancel()
    await app["s"].close()

def make_app():
    app = web.Application(middlewares=[cors_mw])
    app.add_routes([web.get("/", health), web.get("/invoice", invoice), web.get("/queue", queue),
                    web.get("/poll", mpoll), web.get("/cancel", cancel)])
    app.cleanup_ctx.append(bg)
    return app

if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
