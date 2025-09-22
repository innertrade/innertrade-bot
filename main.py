# main.py — Innertrade Kai Mentor Bot
# Версия: 2025-09-22

import os
import json
import time
import logging
import threading
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List
from difflib import SequenceMatcher

import requests
from flask import Flask, request, abort, jsonify
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool
import telebot
from telebot import types
from openai import OpenAI

# ========= Version =========
def get_code_version():
    try:
        with open(__file__, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except:
        return "unknown"

BOT_VERSION = f"2025-09-22-{get_code_version()}"

# ========= ENV =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "webhook").strip()
TG_SECRET = os.getenv("TG_WEBHOOK_SECRET", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OFFSCRIPT_ENABLED = os.getenv("OFFSCRIPT_ENABLED", "true").lower() == "true"
SET_WEBHOOK_FLAG = os.getenv("SET_WEBHOOK", "false").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_BODY = int(os.getenv("MAX_BODY", "1000000"))
HIST_LIMIT = 12

if not TELEGRAM_TOKEN or not DATABASE_URL or not PUBLIC_URL or not TG_SECRET:
    raise RuntimeError("ENV variables missing")

# ========= Logging =========
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("kai-mentor")
log.info(f"Starting bot version: {BOT_VERSION}")

# ========= Intents =========
INTENT_GREET = "greet"
INTENT_FREE = "free"
INTENT_ERR = "error"

STEP_ASK_STYLE = "ask_style"
STEP_FREE_INTRO = "free_intro"
STEP_ERR_DESCR = "err_describe"
STEP_MER_CTX = "mer_context"
STEP_MER_EMO = "mer_emotions"
STEP_MER_THO = "mer_thoughts"
STEP_MER_BEH = "mer_behavior"
STEP_GOAL = "goal_positive"
STEP_TOTE_OPS = "tote_ops"
STEP_TOTE_TEST = "tote_test"
STEP_TOTE_EXIT = "tote_exit"

MER_ORDER = [STEP_MER_CTX, STEP_MER_EMO, STEP_MER_THO, STEP_MER_BEH]

# ========= OpenAI =========
oai_client = None
openai_status = "disabled"

if OPENAI_API_KEY and OFFSCRIPT_ENABLED:
    try:
        oai_client = OpenAI(api_key=OPENAI_API_KEY)
        oai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=5
        )
        openai_status = "active"
        log.info("OpenAI ready")
    except Exception as e:
        log.error(f"OpenAI init error: {e}")
        oai_client = None
        openai_status = f"error: {e}"

# ========= DB =========
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
    );""")
    log.info("DB initialized")

# ========= State =========
def load_state(uid: int) -> Dict[str, Any]:
    row = db_exec("SELECT intent, step, data FROM user_state WHERE user_id=:uid", {"uid": uid}).mappings().first()
    if row:
        data = {}
        if row["data"]:
            try:
                data = json.loads(row["data"])
            except:
                pass
        return {"user_id": uid, "intent": row["intent"] or INTENT_GREET,
                "step": row["step"] or STEP_ASK_STYLE, "data": data}
    return {"user_id": uid, "intent": INTENT_GREET, "step": STEP_ASK_STYLE, "data": {"history": []}}

def save_state(uid: int, intent=None, step=None, data=None) -> Dict[str, Any]:
    cur = load_state(uid)
    intent = intent or cur["intent"]
    step = step or cur["step"]
    new_data = cur["data"].copy()
    if data:
        new_data.update(data)
    db_exec("""
        INSERT INTO user_state (user_id, intent, step, data, updated_at)
        VALUES (:uid, :intent, :step, :data, now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent=EXCLUDED.intent, step=EXCLUDED.step, data=EXCLUDED.data, updated_at=now()
    """, {"uid": uid, "intent": intent, "step": step, "data": json.dumps(new_data, ensure_ascii=False)})
    return {"user_id": uid, "intent": intent, "step": step, "data": new_data}

# ========= Bot =========
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)

MAIN_MENU = types.ReplyKeyboardMarkup(resize_keyboard=True)
MAIN_MENU.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
MAIN_MENU.row("📄 Паспорт", "🗒 Панель недели")
MAIN_MENU.row("🆘 Экстренно", "🤔 Не знаю, с чего начать")

STYLE_KB = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
STYLE_KB.row("ты", "вы")

# ========= Helpers =========
def anti_echo(user_text: str, model_text: str) -> str:
    if SequenceMatcher(None, user_text.lower(), model_text.lower()).ratio() > 0.7:
        return "Понял. Скажу иначе: " + model_text
    return model_text

def mer_prompt_for(step: str) -> str:
    prompts = {
        STEP_MER_CTX: "В какой ситуации это произошло?",
        STEP_MER_EMO: "Что чувствовал?",
        STEP_MER_THO: "Какие мысли были?",
        STEP_MER_BEH: "Что сделал конкретно?",
    }
    return prompts.get(step, "Продолжим.")

# ========= GPT Decision =========
def gpt_decide(uid: int, text_in: str, st: Dict[str, Any]) -> Dict[str, Any]:
    if not oai_client:
        return {"next_step": st["step"], "intent": st["intent"], "response_text": "Продолжим.", "store": {}}

    system_prompt = f"""
    Ты — коуч-наставник по трейдингу по имени Алекс. Всегда веди к разбору, а не к советам.
    Никогда не начинай с банальных фраз. 
    Если видишь описание проблемы — предлагай разобрать последний случай (MERCEDES+TOTE).
    Всегда в JSON: next_step, intent, response_text, store, is_structural.
    """

    msgs = [{"role": "system", "content": system_prompt}]
    for h in st["data"].get("history", [])[-HIST_LIMIT:]:
        msgs.append(h)
    msgs.append({"role": "user", "content": text_in})

    res = oai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=msgs,
        temperature=0.4,
        response_format={"type": "json_object"}
    )
    return json.loads(res.choices[0].message.content or "{}")

# ========= Handlers =========
@bot.message_handler(commands=["start", "reset"])
def cmd_start(m: types.Message):
    save_state(m.from_user.id, INTENT_GREET, STEP_ASK_STYLE, {"history": []})
    bot.send_message(m.from_user.id, "👋 Привет! Как удобнее — <b>ты</b> или <b>вы</b>?", reply_markup=STYLE_KB)

@bot.message_handler(content_types=["text"])
def on_text(m: types.Message):
    uid, text_in = m.from_user.id, m.text.strip()
    st = load_state(uid)

    # Greeting
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if text_in.lower() in ("ты", "вы"):
            st["data"]["style"] = text_in.lower()
            save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
            bot.send_message(uid, f"Принято ({text_in}). Расскажи, что происходит в твоей торговле?", reply_markup=MAIN_MENU)
        else:
            bot.send_message(uid, "Выбери «ты» или «вы».", reply_markup=STYLE_KB)
        return

    # Structural flow
    if st["intent"] == INTENT_ERR:
        handle_structural(uid, text_in, st)
        return

    # Free flow
    decision = gpt_decide(uid, text_in, st)
    resp = decision.get("response_text", "Продолжим.")

    history = st["data"].get("history", [])
    history.append({"role": "user", "content": text_in})
    history.append({"role": "assistant", "content": resp})
    st["data"]["history"] = history

    save_state(uid, decision.get("intent", st["intent"]), decision.get("next_step", st["step"]), st["data"])
    bot.send_message(uid, resp, reply_markup=MAIN_MENU)

def handle_structural(uid: int, text_in: str, st: Dict[str, Any]):
    if st["step"] == STEP_ERR_DESCR:
        st["data"]["error_description"] = text_in
        save_state(uid, INTENT_ERR, STEP_MER_CTX, st["data"])
        bot.send_message(uid, mer_prompt_for(STEP_MER_CTX))
        return
    if st["step"] in MER_ORDER:
        st["data"].setdefault("mer", {})[st["step"]] = text_in
        idx = MER_ORDER.index(st["step"])
        if idx + 1 < len(MER_ORDER):
            next_step = MER_ORDER[idx+1]
            save_state(uid, INTENT_ERR, next_step, st["data"])
            bot.send_message(uid, mer_prompt_for(next_step))
        else:
            save_state(uid, INTENT_ERR, STEP_GOAL, st["data"])
            bot.send_message(uid, "Сформулируй позитивную цель.")
        return
    if st["step"] == STEP_GOAL:
        st["data"]["goal"] = text_in
        save_state(uid, INTENT_ERR, STEP_TOTE_OPS, st["data"])
        bot.send_message(uid, "Назови 2–3 шага для удержания цели.")
        return
    if st["step"] == STEP_TOTE_OPS:
        st["data"].setdefault("tote", {})["ops"] = text_in
        save_state(uid, INTENT_ERR, STEP_TOTE_TEST, st["data"])
        bot.send_message(uid, "Как поймёшь, что получилось?")
        return
    if st["step"] == STEP_TOTE_TEST:
        st["data"].setdefault("tote", {})["test"] = text_in
        save_state(uid, INTENT_ERR, STEP_TOTE_EXIT, st["data"])
        bot.send_message(uid, "Что сделаешь, если не выйдет?")
        return
    if st["step"] == STEP_TOTE_EXIT:
        st["data"].setdefault("tote", {})["exit"] = text_in
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
        bot.send_message(uid, "Разбор завершён ✅", reply_markup=MAIN_MENU)

# ========= Flask =========
@app.get("/")
def root(): return jsonify({"ok": True, "time": datetime.utcnow().isoformat()})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_SECRET:
        abort(401)
    update = telebot.types.Update.de_json(request.data.decode("utf-8"))
    if update: bot.process_new_updates([update])
    return "OK", 200

if __name__ == "__main__":
    init_db()
    if SET_WEBHOOK_FLAG: bot.set_webhook(url=f"{PUBLIC_URL}/{WEBHOOK_PATH}", secret_token=TG_SECRET)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
