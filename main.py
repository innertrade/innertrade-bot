# main.py — Innertrade Kai Mentor Bot (v8.0.1 Orchestrated)
# Дата: 2025-10-18
# Изм.: TG_WEBHOOK_SECRET обязателен только при SET_WEBHOOK=true

import os
import json
import time
import logging
import threading
import hashlib
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, List

import requests
from flask import Flask, request, abort, jsonify
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool
import telebot
from telebot import types
from openai import OpenAI

def _code_hash():
    try:
        with open(__file__, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except Exception:
        return "unknown"

BOT_VERSION = f"2025-10-18-{_code_hash()}"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PUBLIC_URL     = os.getenv("PUBLIC_URL", "").strip()
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "webhook").strip()
TG_SECRET      = os.getenv("TG_WEBHOOK_SECRET", "").strip()

DATABASE_URL   = os.getenv("DATABASE_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

OFFSCRIPT_ENABLED = os.getenv("OFFSCRIPT_ENABLED", "true").lower() == "true"
SET_WEBHOOK_FLAG  = os.getenv("SET_WEBHOOK", "false").lower() == "true"
LOG_LEVEL         = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_BODY          = int(os.getenv("MAX_BODY", "1000000"))

IDLE_MINUTES_REMIND   = int(os.getenv("IDLE_MINUTES_REMIND", "60"))
IDLE_MINUTES_RESET    = int(os.getenv("IDLE_MINUTES_RESET", "240"))
REMINDERS_ENABLED     = os.getenv("REMINDERS_ENABLED", "true").lower() == "true"

HIST_LIMIT = 16

# ========= Guards (секрет обязателен только если ставим вебхук) =========
missing_env = []
for k in ["TELEGRAM_TOKEN", "PUBLIC_URL", "WEBHOOK_PATH", "DATABASE_URL"]:
    if not os.getenv(k, "").strip():
        missing_env.append(k)
if SET_WEBHOOK_FLAG and not TG_SECRET:
    missing_env.append("TG_WEBHOOK_SECRET")
if missing_env:
    raise RuntimeError(f"ENV variables missing: {', '.join(missing_env)}")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("kai-mentor")
log.info(f"Starting bot version: {BOT_VERSION}")

INTENT_GREET = "greet"
INTENT_FREE  = "free"
INTENT_ERR   = "error"
INTENT_DONE  = "done"

STEP_ASK_STYLE  = "ask_style"
STEP_FREE_INTRO = "free_intro"
STEP_ERR_DESCR  = "err_describe"
STEP_MER_CTX    = "mer_context"
STEP_MER_EMO    = "mer_emotions"
STEP_MER_THO    = "mer_thoughts"
STEP_MER_BEH    = "mer_behavior"
STEP_GOAL       = "goal_positive"
STEP_TOTE_OPS   = "tote_ops"
STEP_TOTE_TEST  = "tote_test"
STEP_TOTE_EXIT  = "tote_exit"

MER_ORDER = [STEP_MER_CTX, STEP_MER_EMO, STEP_MER_THO, STEP_MER_BEH]

oai_client = None
openai_status = "disabled"
if OPENAI_API_KEY and OFFSCRIPT_ENABLED:
    try:
        oai_client = OpenAI(api_key=OPENAI_API_KEY)
        oai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        openai_status = "active"
        log.info("OpenAI ready")
    except Exception as e:
        log.error(f"OpenAI init error: {e}")
        oai_client = None
        openai_status = f"error: {e}"

engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
)

def db_exec(sql: str, params: Optional[Dict[str, Any]] = None):
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})

def init_db():
    db_exec("""
    CREATE TABLE IF NOT EXISTS user_state(
        user_id BIGINT PRIMARY KEY,
        intent TEXT,
        step TEXT,
        data TEXT,
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    """)
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_updated_at ON user_state(updated_at)")
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_intent_step ON user_state(intent, step)")
    log.info("DB initialized")

def load_state(uid: int) -> Dict[str, Any]:
    row = db_exec("SELECT intent, step, data FROM user_state WHERE user_id=:uid", {"uid": uid}).mappings().first()
    if row:
        data = {}
        if row["data"]:
            try:
                data = json.loads(row["data"])
            except Exception as e:
                log.error("Failed to parse user data: %s", e)
                data = {}
        if "history" not in data:
            data["history"] = []
        return {"user_id": uid, "intent": row["intent"] or INTENT_GREET, "step": row["step"] or STEP_ASK_STYLE, "data": data}
    return {"user_id": uid, "intent": INTENT_GREET, "step": STEP_ASK_STYLE, "data": {"history": []}}

def save_state(uid: int, intent=None, step=None, data=None) -> Dict[str, Any]:
    cur = load_state(uid)
    intent = intent or cur["intent"]
    step   = step   or cur["step"]
    new_data = cur["data"].copy()
    if data:
        new_data.update(data)
    new_data["last_state_write_at"] = datetime.now(timezone.utc).isoformat()
    db_exec("""
        INSERT INTO user_state (user_id, intent, step, data, updated_at)
        VALUES (:uid, :intent, :step, :data, now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent=EXCLUDED.intent, step=EXCLUDED.step, data=EXCLUDED.data, updated_at=now()
    """, {"uid": uid, "intent": intent, "step": step, "data": json.dumps(new_data, ensure_ascii=False)})
    return {"user_id": uid, "intent": intent, "step": step, "data": new_data}

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)

MAIN_MENU = types.ReplyKeyboardMarkup(resize_keyboard=True)
MAIN_MENU.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
MAIN_MENU.row("📄 Паспорт", "🗒 Панель недели")
MAIN_MENU.row("🆘 Экстренно", "🤔 Не знаю, с чего начать")

STYLE_KB = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
STYLE_KB.row("ты", "вы")

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _append_history(data: Dict[str, Any], role: str, content: str) -> Dict[str, Any]:
    hist = data.get("history", [])
    if len(hist) >= HIST_LIMIT:
        hist = hist[-(HIST_LIMIT - 1):]
    hist.append({"role": role, "content": content})
    data["history"] = hist
    return data

def mer_prompt_for(step: str) -> str:
    return {
        STEP_MER_CTX: "Зафиксируем картинку. Где и когда это было? (коротко)",
        STEP_MER_EMO: "Что почувствовал в моменте (2–3 слова)?",
        STEP_MER_THO: "Какие мысли мелькали (2–3 коротких фразы)?",
        STEP_MER_BEH: "Что сделал фактически? Действия.",
    }.get(step, "Продолжим.")

def gpt_orchestrator(uid: int, text_in: str, st: Dict[str, Any]) -> Dict[str, Any]:
    fallback = {
        "reply": "Понял. Давай сфокусируем картинку: где/когда это было и в какой момент понял, что отклоняешься от плана?",
        "phase": "calibrate",
        "summary_draft": "",
        "mercedes": {"next_step": None, "allow_backtrack": True},
        "tote": {"next_step": None},
        "buttons": [],
        "require_user_confirm": False
    }
    if not oai_client or not OFFSCRIPT_ENABLED:
        return fallback

    style = st["data"].get("style", "ты")
    history = st["data"].get("history", [])

    system = f"""
Ты — Алекс, коуч-наставник (на «{style}»). Фазы: калибровка → структурный разбор → план 3 сделки.
Пока проблема не конкретна — задавай 1–2 уточняющих вопроса (без лекций).
Когда есть конкретика — дай краткий черновик summary_draft и спроси подтверждение (phase: ask_confirm).
После подтверждения — предложи перейти к разбору (phase: ready_for_mercedes).
В разборе по шагам не называй техники. Когда карты достаточно — предложи план на 3 сделки (phase: ready_for_tote).
Ответ в JSON: reply, phase, summary_draft, mercedes{{next_step,allow_backtrack}}, tote{{next_step}}, buttons, require_user_confirm.
""".strip()

    msgs = [{"role": "system", "content": system}]
    for h in history[-HIST_LIMIT:]:
        if h.get("role") in ("user","assistant") and isinstance(h.get("content"), str):
            msgs.append(h)
    msgs.append({"role": "user", "content": text_in})

    try:
        res = oai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=msgs,
            temperature=0.6,
            response_format={"type": "json_object"},
        )
        dec = json.loads(res.choices[0].message.content or "{}")
        for k in ["reply","phase","summary_draft","mercedes","tote","buttons","require_user_confirm"]:
            if k not in dec:
                return fallback
        dec["reply"] = re.sub(r'\s+', ' ', (dec.get("reply") or "")).strip()
        return dec
    except Exception as e:
        log.error("gpt_orchestrator error: %s", e)
        return fallback

@bot.message_handler(commands=["start", "reset"])
def cmd_start(m: types.Message):
    uid = m.from_user.id
    st = save_state(uid, INTENT_GREET, STEP_ASK_STYLE, {"history": []})
    bot.send_message(uid,
        "👋 Привет! Как удобнее — <b>ты</b> или <b>вы</b>?\n\n"
        "Если захочешь начать с чистого листа — напиши: <b>новый разбор</b>.",
        reply_markup=STYLE_KB
    )

@bot.message_handler(commands=["version", "v"])
def cmd_version(m: types.Message):
    info = (
        f"🔄 Версия бота: {BOT_VERSION}\n"
        f"📝 Хэш кода: {_code_hash()}\n"
        f"🕒 Время сервера: {datetime.now(timezone.utc).isoformat()}\n"
        f"🤖 OpenAI: {openai_status}"
    )
    bot.reply_to(m, info)

@bot.message_handler(commands=["menu"])
def cmd_menu(m: types.Message):
    bot.send_message(m.chat.id, "Меню:", reply_markup=MAIN_MENU)

@bot.message_handler(content_types=["text"])
def on_text(m: types.Message):
    handle_text_message(m.from_user.id, m.text.strip(), m)

def handle_text_message(uid: int, text_in: str, original_message=None):
    st = load_state(uid)
    txt = (text_in or "").strip()
    log.info("User %s: intent=%s step=%s text='%s'", uid, st["intent"], st["step"], txt[:200])

    if txt.lower() in ("новый разбор", "новый", "с чистого листа", "start over"):
        st = save_state(uid, INTENT_FREE, STEP_FREE_INTRO, {"history": [], "struct_offer_shown": False})
        bot.send_message(uid, "Окей, начнём с чистого листа. Расскажи коротко, что хочется поправить сейчас?", reply_markup=MAIN_MENU)
        return

    st["data"] = _append_history(st["data"], "user", txt)
    st["data"]["last_user_msg_at"] = _now_iso()
    st["data"]["awaiting_reply"] = True

    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if txt.lower() in ("ты", "вы"):
            st["data"]["style"] = txt.lower()
            st = save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
            bot.send_message(uid, f"Принято ({txt}). С чего начнём? Что сейчас в трейдинге хочется поправить?", reply_markup=MAIN_MENU)
        else:
            save_state(uid, data=st["data"])
            bot.send_message(uid, "Выбери «ты» или «вы».", reply_markup=STYLE_KB)
        return

    if st["intent"] == INTENT_ERR:
        proceed_struct(uid, txt, st)
        return

    decision = gpt_orchestrator(uid, txt, st)
    reply = decision.get("reply") or "Понял. Скажи ещё, где именно отступил от плана — вход, стоп или выход?"
    phase = decision.get("phase", "calibrate")

    if decision.get("summary_draft"):
        st["data"]["problem_draft"] = decision["summary_draft"]

    st["data"] = _append_history(st["data"], "assistant", reply)
    save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
    if original_message:
        bot.reply_to(original_message, reply, reply_markup=MAIN_MENU)
    else:
        bot.send_message(uid, reply, reply_markup=MAIN_MENU)

    if phase == "ask_confirm" and st["data"].get("problem_draft"):
        kb = types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Да, это оно", callback_data="confirm_problem"),
            types.InlineKeyboardButton("Нет, переформулировать", callback_data="refine_problem")
        )
        bot.send_message(uid, f"Суммирую коротко:\n\n<b>{st['data']['problem_draft']}</b>\n\nПодходит?", reply_markup=kb)
        return

    if phase == "ready_for_mercedes":
        kb = types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Разобрать по шагам", callback_data="start_error_flow"),
            types.InlineKeyboardButton("Пока нет", callback_data="skip_error_flow")
        )
        bot.send_message(uid, "Готов пройтись по шагам сейчас?", reply_markup=kb)
        return

    if phase == "in_mercedes":
        nxt = (decision.get("mercedes") or {}).get("next_step")
        if nxt in ("ctx", "emo", "thoughts", "behavior"):
            nxt_map = {"ctx": STEP_MER_CTX, "emo": STEP_MER_EMO, "thoughts": STEP_MER_THO, "behavior": STEP_MER_BEH}
            save_state(uid, INTENT_ERR, nxt_map[nxt], st["data"])
            bot.send_message(uid, mer_prompt_for(nxt_map[nxt]), reply_markup=MAIN_MENU)
            return

    if phase == "ready_for_tote":
        kb = types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Перейти к плану на 3 сделки", callback_data="start_tote"),
            types.InlineKeyboardButton("Ещё уточнить", callback_data="back_to_mercedes")
        )
        bot.send_message(uid, "Хочешь сформировать короткий план на 3 сделки?", reply_markup=kb)
        return

    if phase == "wrap_up":
        bot.send_message(uid, "Если нужно — подведу итог и вынесу в «фокус недели».", reply_markup=MAIN_MENU)
        return

def proceed_struct(uid: int, text_in: str, st: Dict[str, Any]):
    step = st["step"]
    data = st["data"].copy()

    if step == STEP_ERR_DESCR:
        data["error_description"] = text_in
        save_state(uid, INTENT_ERR, STEP_MER_CTX, data)
        bot.send_message(uid, mer_prompt_for(STEP_MER_CTX), reply_markup=MAIN_MENU)
        return

    if step in MER_ORDER:
        mer = data.get("mer", {})
        mer[step] = text_in
        data["mer"] = mer
        idx = MER_ORDER.index(step)
        if idx + 1 < len(MER_ORDER):
            nxt = MER_ORDER[idx + 1]
            save_state(uid, INTENT_ERR, nxt, data)
            bot.send_message(uid, mer_prompt_for(nxt), reply_markup=MAIN_MENU)
        else:
            save_state(uid, INTENT_ERR, STEP_GOAL, data)
            bot.send_message(uid, "Сформулируй позитивную цель: что будешь делать вместо прежнего поведения?", reply_markup=MAIN_MENU)
        return

    if step == STEP_GOAL:
        data["goal"] = text_in
        save_state(uid, INTENT_ERR, STEP_TOTE_OPS, data)
        bot.send_message(uid, "Для ближайших 3 сделок назови 2–3 конкретных шага (коротко, списком).", reply_markup=MAIN_MENU)
        return

    if step == STEP_TOTE_OPS:
        tote = data.get("tote", {})
        tote["ops"] = text_in
        data["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_TEST, data)
        bot.send_message(uid, "Как поймёшь, что получилось? Один простой критерий.", reply_markup=MAIN_MENU)
        return

    if step == STEP_TOTE_TEST:
        tote = data.get("tote", {})
        tote["test"] = text_in
        data["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_EXIT, data)
        bot.send_message(uid, "Если проверка покажет «не получилось» — что сделаешь?", reply_markup=MAIN_MENU)
        return

    if step == STEP_TOTE_EXIT:
        tote = data.get("tote", {})
        tote["exit"] = text_in
        data["tote"] = tote

        mer = data.get('mer', {})
        summary = [
            "<b>Итог разбора</b>",
            f"Проблема: {data.get('error_description', data.get('problem', '—'))}",
            f"Контекст: {mer.get(STEP_MER_CTX, '—')}",
            f"Эмоции: {mer.get(STEP_MER_EMO, '—')}",
            f"Мысли: {mer.get(STEP_MER_THO, '—')}",
            f"Поведение: {mer.get(STEP_MER_BEH, '—')}",
            f"Цель: {data.get('goal', '—')}",
            f"Шаги (3 сделки): {data.get('tote', {}).get('ops', '—')}",
            f"Проверка: {data.get('tote', {}).get('test', '—')}",
            f"Если не вышло: {data.get('tote', {}).get('exit', '—')}",
        ]
        save_state(uid, INTENT_DONE, STEP_FREE_INTRO, data)
        bot.send_message(uid, "\n".join(summary), reply_markup=MAIN_MENU)
        bot.send_message(uid, "Готов вынести это в «фокус недели» или идём дальше?", reply_markup=MAIN_MENU)
        return

    save_state(uid, INTENT_FREE, STEP_FREE_INTRO, data)
    bot.send_message(uid, "Окей, вернёмся на шаг назад и уточним ещё чуть-чуть.", reply_markup=MAIN_MENU)

MENU_BTNS = {
    "🚑 У меня ошибка": "error",
    "🧩 Хочу стратегию": "strategy",
    "📄 Паспорт": "passport",
    "🗒 Панель недели": "weekpanel",
    "🆘 Экстренно": "panic",
    "🤔 Не знаю, с чего начать": "start_help",
}

@bot.message_handler(func=lambda m: m.text in MENU_BTNS.keys())
def handle_menu(m: types.Message):
    uid = m.from_user.id
    st = load_state(uid)
    label = m.text
    code = MENU_BTNS[label]

    st["data"] = _append_history(st["data"], "user", label)

    if code == "error":
        if st["data"].get("problem_confirmed"):
            save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
            bot.send_message(uid, "Опиши последний кейс ошибки: где/когда, вход/стоп/план, где отступил, чем закончилось.")
        else:
            save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
            bot.send_message(uid, "Коротко — что именно сейчас мешает? Сформулируй в одном-двух предложениях.", reply_markup=MAIN_MENU)
    elif code == "start_help":
        bot.send_message(uid, "План: 1) короткий разбор, 2) фокус недели, 3) каркас ТС. С чего начнём?", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])
    else:
        bot.send_message(uid, "Ок. Если хочешь ускориться — нажми «🚑 У меня ошибка».", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])

@bot.callback_query_handler(func=lambda call: True)
def on_callback(call: types.CallbackQuery):
    uid = call.from_user.id
    data = call.data or ""
    bot.answer_callback_query(call.id, "Ок")

    st = load_state(uid)

    if data == "confirm_problem":
        st["data"]["problem"] = st["data"].get("problem_draft", "—")
        st["data"]["problem_confirmed"] = True
        st["data"]["struct_offer_shown"] = False
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
        kb = types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Разобрать по шагам", callback_data="start_error_flow"),
            types.InlineKeyboardButton("Пока нет", callback_data="skip_error_flow"),
        )
        bot.send_message(uid, "Принято. Готов разобрать это по шагам?", reply_markup=kb)
        return

    if data == "refine_problem":
        st["data"]["problem_confirmed"] = False
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
        bot.send_message(uid, "Хорошо. Сформулируй тогда поконкретнее, что именно разбирать.", reply_markup=MAIN_MENU)
        return

    if data == "start_error_flow":
        st["data"]["problem_confirmed"] = True
        st = save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        bot.send_message(uid, "Начинаем разбор. Опиши последний случай: вход/план, где отступил, результат.")
        return

    if data == "skip_error_flow":
        bot.send_message(uid, "Окей, вернёмся к этому позже.", reply_markup=MAIN_MENU)
        return

    if data == "start_tote":
        st = save_state(uid, INTENT_ERR, STEP_GOAL, st["data"])
        bot.send_message(uid, "Сформулируй позитивную цель: что будешь делать вместо прежнего поведения?", reply_markup=MAIN_MENU)
        return

    if data == "back_to_mercedes":
        st = save_state(uid, INTENT_ERR, STEP_MER_CTX, st["data"])
        bot.send_message(uid, mer_prompt_for(STEP_MER_CTX), reply_markup=MAIN_MENU)
        return

    if data == "continue_session":
        st["data"]["awaiting_reply"] = False
        st["data"]["last_nag_at"] = _now_iso()
        save_state(uid, data=st["data"])
        bot.send_message(uid, "Продолжаем. На чём остановились?", reply_markup=MAIN_MENU)
        return

    if data == "restart_session":
        st = save_state(uid, INTENT_FREE, STEP_FREE_INTRO, {"history": [], "struct_offer_shown": False})
        bot.send_message(uid, "Окей, начнём заново. Что сейчас хочется поправить?", reply_markup=MAIN_MENU)
        return

@app.get("/")
def root():
    return jsonify({"ok": True, "time": _now_iso()})

@app.get("/version")
def version_api():
    return jsonify({"version": BOT_VERSION, "code_hash": _code_hash(), "status": "running", "timestamp": _now_iso(), "openai": openai_status})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    # При отсутствии TG_SECRET (если SET_WEBHOOK=false) — не проверяем заголовок
    if SET_WEBHOOK_FLAG:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_SECRET:
            abort(401)
    if request.content_length and request.content_length > MAX_BODY:
        abort(413, description="Body too large")
    body = request.get_data()
    if not body:
        abort(400, description="Empty body")
    try:
        update = telebot.types.Update.de_json(body.decode("utf-8"))
        if update is None:
            log.error("Failed to parse update")
            abort(400, description="Invalid update")
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        log.error("Webhook processing error: %s", e)
        abort(500)

def cleanup_old_states(days: int = 30):
    try:
        days = int(days)
        db_exec(f"DELETE FROM user_state WHERE updated_at < NOW() - INTERVAL '{days} days'")
        log.info("Old user states cleanup done (> %s days).", days)
    except Exception as e:
        log.error("Cleanup error: %s", e)

def reminder_tick():
    if not REMINDERS_ENABLED:
        return
    try:
        mins = int(IDLE_MINUTES_REMIND)
        reset_mins = int(IDLE_MINUTES_RESET)
        rows = db_exec("SELECT user_id, intent, step, data, updated_at FROM user_state").mappings().all()
        now = datetime.now(timezone.utc)
        for r in rows:
            try:
                data = json.loads(r["data"] or "{}")
            except Exception:
                data = {}
            if not data.get("awaiting_reply"):
                continue
            last_user_ts = data.get("last_user_msg_at")
            if not last_user_ts:
                continue
            try:
                last_dt = datetime.fromisoformat(last_user_ts)
            except Exception:
                continue
            delta = now - last_dt
            last_nag_at = data.get("last_nag_at")
            nag_ok = True
            if last_nag_at:
                try:
                    if (now - datetime.fromisoformat(last_nag_at)) < timedelta(minutes=mins//2 or 1):
                        nag_ok = False
                except Exception:
                    pass
            if delta >= timedelta(minutes=reset_mins) and nag_ok:
                kb = types.InlineKeyboardMarkup().row(
                    types.InlineKeyboardButton("Продолжаем", callback_data="continue_session"),
                    types.InlineKeyboardButton("Начать заново", callback_data="restart_session"),
                )
                bot.send_message(r["user_id"], "Дела затащили? Готов продолжить или начнём заново?", reply_markup=kb)
                data["last_nag_at"] = _now_iso()
                save_state(r["user_id"], data=data)
            elif delta >= timedelta(minutes=mins) and nag_ok:
                kb = types.InlineKeyboardMarkup().row(
                    types.InlineKeyboardButton("Продолжаем", callback_data="continue_session"),
                )
                bot.send_message(r["user_id"], "Как будешь готов — продолжим?", reply_markup=kb)
                data["last_nag_at"] = _now_iso()
                save_state(r["user_id"], data=data)
    except Exception as e:
        log.error("Reminder error: %s", e)

def background_housekeeping():
    last_cleanup = time.time()
    while True:
        time.sleep(60)
        reminder_tick()
        if time.time() - last_cleanup > 24*60*60:
            cleanup_old_states(30)
            last_cleanup = time.time()

try:
    init_db()
    log.info("DB initialized (import)")
except Exception as e:
    log.error("DB init (import) failed: %s", e)

if SET_WEBHOOK_FLAG:
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(
            url=f"{PUBLIC_URL}/{WEBHOOK_PATH}",
            secret_token=TG_SECRET,  # здесь секрет обязателен, но мы это уже проверили в Guards
            allowed_updates=["message", "callback_query"]
        )
        log.info("Webhook set to %s/%s", PUBLIC_URL, WEBHOOK_PATH)
    except Exception as e:
        log.error("Webhook setup error: %s", e)

try:
    th = threading.Thread(target=background_housekeeping, daemon=True)
    th.start()
except Exception as e:
    log.error("Can't start housekeeping thread: %s", e)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
