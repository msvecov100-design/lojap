/**
 * Админка + статистика входов/выходов для Telegram-игры.
 * Подключение (в файле, где у вас создан bot (telegraf) и app (express)):
 *
 *   require('./admin')(app, bot);
 *
 * Переменные окружения:
 *   BOT_TOKEN   — токен бота (нужен для проверки initData)
 *   ADMIN_IDS   — ваши Telegram ID через запятую, напр. 123456789
 *   ADMIN_KEY   — любой длинный секрет для веб-панели /admin?key=...
 *   TZ_HOURS    — ваш часовой пояс от UTC (по умолчанию 5 — Екатеринбург)
 *   STATS_FILE  — путь к файлу статистики (по умолчанию ./stats.json)
 */
const crypto = require('crypto'), fs = require('fs'), path = require('path');
const { Markup } = require('telegraf');

const TOKEN = process.env.9218724571:AAH51tGCAhXRkiUe4SV-hrMMULMCqjC3Fi4 || process.env.TOKEN || '';
// ВПИШИТЕ СЮДА ВАШ TELEGRAM ID (узнать: напишите боту /myid или откройте Настройки в игре), напр. [123456789]
const OWNER_IDS = [1301667270];
const ADMINS = [...new Set([...OWNER_IDS, ...(process.env.ADMIN_IDS || '').split(',').map(s => +s.trim()).filter(Boolean)])];
const KEY = process.env.ADMIN_KEY || '';
const TZ = +(process.env.TZ_HOURS || 5) * 36e5;
const FILE = process.env.STATS_FILE || path.join(__dirname, 'stats.json');
const ONLINE = 75e3; // нет пингов дольше 75 с — считаем, что вышел

let DB = { users: {}, log: [], notify: {} };
try { DB = Object.assign(DB, JSON.parse(fs.readFileSync(FILE, 'utf8'))); } catch (e) {}
let dirty = 0;
setInterval(() => { if (dirty) { dirty = 0; try { fs.writeFileSync(FILE, JSON.stringify(DB)); } catch (e) { console.error('stats save', e.message); } } }, 10000);

const dayKey = t => new Date((t || Date.now()) + TZ).toISOString().slice(0, 10);
const esc = s => String(s ?? '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
const num = n => Math.round(n || 0).toLocaleString('ru-RU');
const dur = ms => { const s = Math.round(ms / 1000); if (s < 60) return s + ' с'; const m = Math.round(s / 60); return m < 60 ? m + ' мин' : Math.floor(m / 60) + ' ч ' + (m % 60) + ' мин'; };
const hhmm = t => new Date(t + TZ).toISOString().slice(11, 16);
const nm = r => esc(r.name || 'Игрок') + (r.un ? ' @' + esc(r.un) : '') + ' <code>' + r.id + '</code>';

// ---- проверка initData Telegram ----
function check(initData) {
  try {
    const p = new URLSearchParams(initData), h = p.get('hash'); if (!h) return null; p.delete('hash');
    const s = [...p.entries()].sort((a, b) => a[0] < b[0] ? -1 : 1).map(([k, v]) => k + '=' + v).join('\n');
    const sec = crypto.createHmac('sha256', 'WebAppData').update(TOKEN).digest();
    if (crypto.createHmac('sha256', sec).update(s).digest('hex') !== h) return null;
    if (Date.now() / 1000 - (+p.get('auth_date') || 0) > 7 * 864e5) return null;
    return JSON.parse(p.get('user'));
  } catch (e) { return null; }
}

let bot = null;
function ping(text) {
  if (!bot) return;
  ADMINS.forEach(id => { if (DB.notify[id]) bot.telegram.sendMessage(id, text, { parse_mode: 'HTML', disable_notification: true }).catch(() => {}); });
}
function addLog(e) { DB.log.push(e); if (DB.log.length > 3000) DB.log.splice(0, DB.log.length - 3000); }

function endSession(r, at, why) {
  if (!r.sAt) return;
  const d = Math.max(0, at - r.sAt); r.time += d; const k = dayKey(at); r.days[k] = (r.days[k] || 0) + d;
  addLog({ t: at, id: r.id, ev: 'leave', d, why }); r.sAt = 0; dirty = 1;
  ping(`🔴 Вышел: ${nm(r)}\nв игре был: ${dur(d)}`);
}
function startSession(r, now, silent) {
  r.sAt = now; r.sessions++; const k = dayKey(now); r.days[k] = r.days[k] || 0; dirty = 1;
  addLog({ t: now, id: r.id, ev: 'join' });
  if (!silent) ping(`🟢 Зашёл: ${nm(r)}\nур. ${r.lvl || 1} · ${num(r.coins)} коинов · сессия №${r.sessions}`);
}
function record(u, d) {
  const now = Date.now(); let r = DB.users[u.id];
  if (!r) { r = DB.users[u.id] = { id: u.id, first: now, sessions: 0, time: 0, days: {}, sAt: 0, seen: 0 }; addLog({ t: now, id: u.id, ev: 'new' }); ping(`🆕 Новый игрок: ${esc(u.first_name)} ${u.username ? '@' + esc(u.username) : ''} <code>${u.id}</code>`); }
  r.name = u.first_name || ''; r.un = u.username || '';
  r.coins = d.coins; r.lvl = d.lvl; r.n = d.n; r.col = d.col; r.page = d.page;
  if (d.ev === 'open') {
    if (r.sAt && now - r.seen > ONLINE) endSession(r, r.seen, 'timeout');
    if (!r.sAt) startSession(r, now);
    r.seen = now;
  } else if (d.ev === 'ping') {
    if (!r.sAt) startSession(r, now, true);
    r.seen = now;
  } else if (d.ev === 'close') {
    r.seen = now; endSession(r, now, 'close');
  }
  r.last = now; dirty = 1;
}
setInterval(() => { const now = Date.now(); Object.values(DB.users).forEach(r => { if (r.sAt && now - r.seen > ONLINE) endSession(r, r.seen, 'timeout'); }); }, 20000);

// ---- статистика ----
function stats() {
  const now = Date.now(), U = Object.values(DB.users), today = dayKey(now), wk = [...Array(7)].map((_, i) => dayKey(now - i * 864e5));
  const online = U.filter(r => r.sAt && now - r.seen <= ONLINE);
  const act = k => U.filter(r => r.days[k] !== undefined);
  return {
    now, total: U.length, online, dau: act(today).length, wau: U.filter(r => wk.some(k => r.days[k] !== undefined)).length,
    newToday: U.filter(r => dayKey(r.first) === today).length, newWeek: U.filter(r => now - r.first < 7 * 864e5).length,
    timeToday: U.reduce((a, r) => a + (r.days[today] || 0), 0), sessions: U.reduce((a, r) => a + r.sessions, 0),
    totalTime: U.reduce((a, r) => a + r.time, 0), coins: U.reduce((a, r) => a + (r.coins || 0), 0),
    days: wk.reverse().map(k => ({ k, n: act(k).length })), U
  };
}
const text = {
  main() {
    const s = stats();
    return `📊 <b>Статистика игры</b>\n\n🟢 Онлайн сейчас: <b>${s.online.length}</b>\n👥 Всего игроков: <b>${num(s.total)}</b>\n🆕 Новых сегодня: <b>${s.newToday}</b> · за 7 дней: ${s.newWeek}\n📅 Заходили сегодня (DAU): <b>${s.dau}</b>\n📆 За 7 дней (WAU): <b>${s.wau}</b>\n⏱ Время в игре сегодня: ${dur(s.timeToday)}\n🔁 Сессий всего: ${num(s.sessions)}` +
      (s.sessions ? ` · средняя ${dur(s.totalTime / s.sessions)}` : '') + `\n\n<b>Активность по дням</b>\n` + s.days.map(d => `${d.k.slice(5)}  ${'█'.repeat(Math.min(20, Math.ceil(d.n / Math.max(1, ...s.days.map(x => x.n)) * 20)))} ${d.n}`).join('\n');
  },
  online() {
    const s = stats(); if (!s.online.length) return '🟢 Сейчас в игре никого нет.';
    return `🟢 <b>Онлайн: ${s.online.length}</b>\n\n` + s.online.sort((a, b) => b.sAt - a.sAt).slice(0, 40).map(r => `• ${nm(r)}\n   ур. ${r.lvl || 1} · ${num(r.coins)} · ${r.page || 'home'} · в игре ${dur(Date.now() - r.sAt)}`).join('\n');
  },
  log() {
    const l = DB.log.slice(-30).reverse(); if (!l.length) return 'Событий пока нет.';
    const ic = { join: '🟢 зашёл', leave: '🔴 вышел', new: '🆕 новый' };
    return '📜 <b>Последние события</b>\n\n' + l.map(e => { const r = DB.users[e.id] || { id: e.id }; return `${dayKey(e.t).slice(5)} ${hhmm(e.t)}  ${ic[e.ev]}  ${esc(r.name || '')} ${r.un ? '@' + esc(r.un) : ''} <code>${e.id}</code>${e.d ? ' · ' + dur(e.d) : ''}`; }).join('\n');
  },
  top() {
    const U = stats().U.sort((a, b) => (b.coins || 0) - (a.coins || 0)).slice(0, 15);
    return '🏆 <b>Топ по коинам</b>\n\n' + U.map((r, i) => `${i + 1}. ${esc(r.name || 'Игрок')} ${r.un ? '@' + esc(r.un) : ''} — ${num(r.coins)} · ур. ${r.lvl || 1} · круток ${num(r.n)}`).join('\n');
  },
  user(r) {
    const now = Date.now(); return `👤 ${nm(r)}\n${r.sAt && now - r.seen <= ONLINE ? '🟢 сейчас онлайн' : '⚪️ был: ' + dayKey(r.last) + ' ' + hhmm(r.last)}\nПервый вход: ${dayKey(r.first)} ${hhmm(r.first)}\nСессий: ${r.sessions} · всего в игре ${dur(r.time)}\nУр. ${r.lvl || 1} · коинов ${num(r.coins)} · круток ${num(r.n)} · номеров ${r.col || 0}\nДней с активностью: ${Object.keys(r.days).length}`;
  }
};

module.exports = function setup(app, b) {
  bot = b;
  // --- приём событий из игры ---
  const express = require('express');
  app.options('/track', (q, s) => { s.set({ 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*' }); s.sendStatus(204); });
  app.post('/track', express.text({ type: '*/*', limit: '10kb' }), (q, s) => {
    s.set('Access-Control-Allow-Origin', '*');
    try {
      const d = JSON.parse(q.body), u = check(d.initData);
      if (!u || !['open', 'ping', 'close'].includes(d.ev)) return s.sendStatus(403);
      record(u, d); s.sendStatus(204);
    } catch (e) { s.sendStatus(400); }
  });

  // --- админка внутри игры (доступ по проверенному initData + ADMINS) ---
  app.options('/admin/api', (q, s) => { s.set({ 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*' }); s.sendStatus(204); });
  app.post('/admin/api', express.text({ type: '*/*', limit: '10kb' }), (q, s) => {
    s.set('Access-Control-Allow-Origin', '*');
    try {
      const d = JSON.parse(q.body), u = check(d.initData);
      if (!u || !ADMINS.includes(u.id)) return s.json({ admin: false });
      const st = stats();
      s.json({ admin: true, online: st.online.length, total: st.total, dau: st.dau, wau: st.wau, newToday: st.newToday, newWeek: st.newWeek, timeToday: st.timeToday, sessions: st.sessions, days: st.days,
        users: st.U.sort((a, b) => b.last - a.last).slice(0, 100).map(r => ({ id: r.id, name: r.name, un: r.un, on: !!(r.sAt && st.now - r.seen <= ONLINE), last: r.last, sessions: r.sessions, time: r.time, lvl: r.lvl, coins: r.coins })),
        log: DB.log.slice(-40).reverse().map(e => ({ ...e, name: (DB.users[e.id] || {}).name })) });
    } catch (e) { s.sendStatus(400); }
  });

  // --- веб-панель ---
  const ok = q => KEY && q.query.key === KEY;
  app.get('/admin/data', (q, s) => {
    if (!ok(q)) return s.sendStatus(403); const st = stats();
    s.json({ online: st.online.length, total: st.total, dau: st.dau, wau: st.wau, newToday: st.newToday, days: st.days,
      users: st.U.sort((a, b) => b.last - a.last).slice(0, 200).map(r => ({ id: r.id, name: r.name, un: r.un, on: !!(r.sAt && st.now - r.seen <= ONLINE), last: r.last, first: r.first, sessions: r.sessions, time: r.time, lvl: r.lvl, coins: r.coins })),
      log: DB.log.slice(-100).reverse().map(e => ({ ...e, name: (DB.users[e.id] || {}).name })) });
  });
  app.get('/admin', (q, s) => {
    if (!ok(q)) return s.sendStatus(403);
    s.type('html').send(`<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Админка</title>
<style>body{font:14px system-ui;background:#0b0b0e;color:#fff;margin:0;padding:14px}h1{font-size:20px}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}.c{background:#17171c;border-radius:12px;padding:10px}.c b{display:block;font-size:24px}table{width:100%;border-collapse:collapse;margin:10px 0}td,th{padding:6px;border-bottom:1px solid #222;text-align:left;font-size:13px}.on{color:#3ddc84}.m{color:#888}</style>
<h1>📊 Статистика</h1><div class=g id=t></div><h3>Игроки</h3><table id=u></table><h3>События</h3><table id=l></table>
<script>const K=${JSON.stringify(KEY)},f=t=>new Date(t).toLocaleString('ru-RU'),n=x=>Math.round(x||0).toLocaleString('ru-RU'),d=ms=>Math.round(ms/60000)+' мин';
async function L(){const j=await (await fetch('/admin/data?key='+encodeURIComponent(K))).json();
t.innerHTML=[['Онлайн',j.online],['Всего',j.total],['Сегодня',j.dau],['7 дней',j.wau],['Новых сегодня',j.newToday]].map(a=>'<div class=c>'+a[0]+'<b>'+a[1]+'</b></div>').join('');
u.innerHTML='<tr><th></th><th>Игрок<th>Ур.<th>Коины<th>Сессий<th>В игре<th>Был</tr>'+j.users.map(r=>'<tr><td class='+(r.on?'on':'m')+'>●<td>'+(r.name||'')+' '+(r.un?'@'+r.un:'')+'<br><span class=m>'+r.id+'</span><td>'+(r.lvl||1)+'<td>'+n(r.coins)+'<td>'+r.sessions+'<td>'+d(r.time)+'<td>'+f(r.last)+'</tr>').join('');
l.innerHTML=j.log.map(e=>'<tr><td>'+f(e.t)+'<td>'+({join:'🟢 зашёл',leave:'🔴 вышел',new:'🆕 новый'}[e.ev])+'<td>'+(e.name||e.id)+'<td>'+(e.d?d(e.d):'')+'</tr>').join('')}
L();setInterval(L,10000)</script>`);
  });

  // --- команды бота ---
  const isAdm = ctx => ADMINS.includes(ctx.from && ctx.from.id);
  const kb = id => Markup.inlineKeyboard([
    [Markup.button.callback('🟢 Онлайн', 'adm_online'), Markup.button.callback('📊 Сводка', 'adm_main')],
    [Markup.button.callback('📜 События', 'adm_log'), Markup.button.callback('🏆 Топ', 'adm_top')],
    [Markup.button.callback(DB.notify[id] ? '🔔 Уведомления: ВКЛ' : '🔕 Уведомления: ВЫКЛ', 'adm_notify')]
  ]);
  const show = async (ctx, t) => { const o = { parse_mode: 'HTML', ...kb(ctx.from.id) }; try { await ctx.editMessageText(t, o); } catch (e) { if (!/not modified/.test(e.message || '')) await ctx.reply(t, o); } };
  bot.command('myid', ctx => ctx.reply('Ваш Telegram ID: ' + ctx.from.id));
  bot.command('admin', ctx => { if (!isAdm(ctx)) return; ctx.reply(text.main(), { parse_mode: 'HTML', ...kb(ctx.from.id) }); });
  bot.command('user', ctx => {
    if (!isAdm(ctx)) return; const q = (ctx.message.text.split(/\s+/)[1] || '').replace('@', '').toLowerCase();
    const r = Object.values(DB.users).find(r => String(r.id) === q || (r.un || '').toLowerCase() === q);
    ctx.reply(r ? text.user(r) : 'Не найден. Использование: /user <id или @ник>', { parse_mode: 'HTML' });
  });
  ['main', 'online', 'log', 'top'].forEach(k => bot.action('adm_' + k, async ctx => { if (!isAdm(ctx)) return ctx.answerCbQuery('Нет доступа'); await ctx.answerCbQuery(); show(ctx, text[k]()); }));
  bot.action('adm_notify', async ctx => { if (!isAdm(ctx)) return ctx.answerCbQuery('Нет доступа'); DB.notify[ctx.from.id] = !DB.notify[ctx.from.id]; dirty = 1; await ctx.answerCbQuery(DB.notify[ctx.from.id] ? 'Буду писать о каждом входе и выходе' : 'Уведомления выключены'); show(ctx, text.main()); });
};
