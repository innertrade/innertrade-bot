# main.py — Innertrade Kai Mentor Bot
# Версия: 2025-09-22-full

import os
import json
import time
import logging
import threading
import hashlib
import re
from datetime import datetime, timezone, timedelta
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
        with open(__file__, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except Exception:
        return "unknown"

BOT_VERSION = f"2025-09-22-{get_code_version()}"

# ========= ENV =========
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
PUBLIC_URL       = os.getenv("PUBLIC_URL", "").strip()
WEBHOOK_PATH     = os.getenv("WEBHOOK_PATH", "webhook").strip()
TG_SECRET        = os.getenv("TG_WEBHOOK_SECRET", "").strip()
DATABASE_URL     = os.getenv("DATABASE_URL", "").strip()

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OFFSCRIPT_ENABLED= os.getenv("OFFSCRIPT_ENABLED", "true").lower() == "true"

SET_WEBHOOK_FLAG = os.getenv("SET_WEBHOOK", "false").lower() == "true"
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_BODY         = int(os.getenv("MAX_BODY", "1000000"))
HIST_LIMIT       = int(os.getenv("HIST_LIMIT", "12"))

# Возврат к разбору/устаревание
STALE_HOURS      = int(os.getenv("STALE_HOURS", "48"))

# Мягкие «пинки» (по умолчанию выключены)
NUDGE_ENABLED    = os.getenv("NUDGE_ENABLED", "false").lower() == "true"
NUDGE_SHORT_MIN  = int(os.getenv("NUDGE_SHORT_MIN", "5"))
NUDGE_LONG_MIN   = int(os.getenv("NUDGE_LONG_MIN", "60"))

# ========= Validation =========
if not TELEGRAM_TOKEN: raise RuntimeError("TELEGRAM_TOKEN is required")
if not DATABASE_URL:   raise RuntimeError("DATABASE_URL is required")
if not PUBLIC_URL:     raise RuntimeError("PUBLIC_URL is required")
if not TG_SECRET:      raise RuntimeError("TG_WEBHOOK_SECRET is required")

# ========= Logging =========
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("innertrade")
log.info(f"Starting bot version: {BOT_VERSION}")

# ========= Intents/steps =========
INTENT_GREET = "greet"
INTENT_FREE  = "free"
INTENT_ERR   = "error"

STEP_ASK_STYLE   = "ask_style"
STEP_FREE_INTRO  = "free_intro"

STEP_ERR_DESCR   = "err_descr"
STEP_MER_CTX     = "ctx"
STEP_MER_EMO     = "emo"
STEP_MER_THO     = "tho"
STEP_MER_BEH     = "beh"
STEP_GOAL        = "goal"
STEP_TOTE_OPS    = "ops"
STEP_TOTE_TEST   = "test"
STEP_TOTE_EXIT   = "exit"

MER_ORDER = [STEP_MER_CTX, STEP_MER_EMO, STEP_MER_THO, STEP_MER_BEH]

# ========= Helpers =========
def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()

def is_state_stale(updated_at_iso: str, hours: int = STALE_HOURS) -> bool:
    try:
        dt = datetime.fromisoformat((updated_at_iso or "").replace("Z", "+00:00"))
    except Exception:
        return False
    return datetime.now(timezone.utc) - dt > timedelta(hours=hours)

def anti_echo(user_text: str, model_text: str) -> str:
    u = (user_text or "").strip().lower()
    m = (model_text or "").strip()
    if len(u) < 15 or len(m) < 15:
        return m
    sim = SequenceMatcher(None, u, m.lower()).ratio()
    if sim > 0.7:
        return "Скажу по-своему: " + m
    return m

def remove_template_phrases(text: str) -> str:
    templates = [
        "Понимаю, это", "Я понимаю", "Это может быть", "Важно понять",
        "Давай рассмотрим", "Это поможет", "Было бы полезно",
        "Как долго", "В каких ситуациях"
    ]
    for ph in templates:
        text = re.sub(rf"{re.escape(ph)}[^.!?]*[.!?]", "", text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'^[,\s\.!?]+', '', text)
    return text

def detect_trading_patterns(text_in: str) -> List[str]:
    patterns = {
        "fomo": ["упустить", "поезд уедет", "без меня", "fear of missing out", "fomo"],
        "stop_fear": ["страх стоп", "боюсь стопа", "зацепит стоп"],
        "move_stop": ["двигаю стоп", "переставил стоп", "отодвинул стоп"],
        "early_entry": ["зашёл раньше", "вошел раньше", "ранний вход"],
        "averaging": ["усредн", "докуп", "добавлялся"]
    }
    detected = []
    low = (text_in or "").lower()
    for k, kws in patterns.items():
        if any(w in low for w in kws):
            detected.append(k)
    return detected

def mer_prompt_for(step: str) -> str:
    prompts = {
        STEP_MER_CTX: "Где и когда это было? 1–2 строки контекста.",
        STEP_MER_EMO: "Что чувствовал в тот момент? 1–3 слова.",
        STEP_MER_THO: "Какие мысли мелькали? 2–3 короткие фразы.",
        STEP_MER_BEH: "Что сделал фактически? Действия по шагам."
    }
    return prompts.get(step, "Продолжим.")

def ask_next_humanized(uid: int, step: str):
    texts = {
        STEP_ERR_DESCR: "Опиши последний эпизод (что планировал, что сделал, чем кончилось).",
        STEP_MER_CTX:   mer_prompt_for(STEP_MER_CTX),
        STEP_MER_EMO:   mer_prompt_for(STEP_MER_EMO),
        STEP_MER_THO:   mer_prompt_for(STEP_MER_THO),
        STEP_MER_BEH:   mer_prompt_for(STEP_MER_BEH),
        STEP_GOAL:      "Как прозвучит новая привычка вместо старого шаблона? Одним предложением.",
        STEP_TOTE_OPS:  "Назови 2–3 мини-шага на ближайшие 3 сделки, которые удержат тебя в рамке плана.",
        STEP_TOTE_TEST: "По какому признаку поймёшь, что получилось держаться плана в этот раз?",
        STEP_TOTE_EXIT: "Если сорвёшься — какой у тебя план Б, чтобы быстро вернуться в фокус?"
    }
    bot.send_message(uid, texts.get(step, "Продолжим?"))

def recap_and_continue(uid: int, st: Dict[str, Any]):
    mer = st["data"].get("mer", {})
    parts = []
    if st["data"].get("error_description"): parts.append(f"Ошибка: {st['data']['error_description']}")
    if mer.get(STEP_MER_CTX): parts.append(f"Контекст: {mer.get(STEP_MER_CTX)}")
    if mer.get(STEP_MER_EMO): parts.append(f"Чувства: {mer.get(STEP_MER_EMO)}")
    if mer.get(STEP_MER_THO): parts.append(f"Мысли: {mer.get(STEP_MER_THO)}")
    if mer.get(STEP_MER_BEH): parts.append(f"Действия: {mer.get(STEP_MER_BEH)}")
    if st["data"].get("goal"): parts.append(f"Цель: {st['data']['goal']}")
    if parts:
        bot.send_message(uid, "Коротко где остановились:\n\n" + "\n".join(parts))
    ask_next_humanized(uid, st["step"])

def prefill_then_ask(uid: int, st: Dict[str, Any], step: str):
    """Показываем, что уже знаем, просим только поправить/дополнить; иначе задаём вопрос."""
    mer = st["data"].get("mer", {})
    if step in MER_ORDER:
        val = mer.get(step)
        if val:
            bot.send_message(uid, f"У меня уже записано:\n{val}\n\nПоправим/дополним?")
            return
    elif step == STEP_ERR_DESCR and st["data"].get("error_description"):
        bot.send_message(uid, f"Записал так:\n{st['data']['error_description']}\n\nЧто поправим?")
        return
    elif step == STEP_GOAL and st["data"].get("goal"):
        bot.send_message(uid, f"Цель записана:\n{st['data']['goal']}\n\nОставляем так?")
        return
    elif step in (STEP_TOTE_OPS, STEP_TOTE_TEST, STEP_TOTE_EXIT):
        tote = st["data"].get("tote", {})
        key = {"ops": STEP_TOTE_OPS, "test": STEP_TOTE_TEST, "exit": STEP_TOTE_EXIT}
        rev = {v:k for k,v in key.items()}
        store_key = rev.get(step)
        if store_key and tote.get(store_key):
            bot.send_message(uid, f"Есть такая запись:\n{tote.get(store_key)}\n\nНужно обновить?")
            return
    # если нечего префилить — задаём вопрос
    ask_next_humanized(uid, step)

# ========= OpenAI =========
oai_client, openai_status = None, "disabled"
if OPENAI_API_KEY and OFFSCRIPT_ENABLED:
    try:
        oai_client = OpenAI(api_key=OPENAI_API_KEY)
        # sanity ping
        _ = oai_client.chat.completions.create(
            model=OPENAI_MODEL, messages=[{"role":"user","content":"ok"}], max_tokens=1)
        openai_status = "active"
        log.info("OpenAI client initialized successfully")
    except Exception as e:
        log.error(f"OpenAI init error: {e}")
        openai_status = f"error: {e}"

def gpt_coach_reply(user_text: str, style="ты", history: List[Dict]=[]):
    """Коуч-тон, без сухих советов/шаблонов, без упоминаний техник."""
    if not oai_client: return "Понял. Продолжим."
    sys = f"""
Ты — тёплый, конкретный наставник по трейдингу по имени Алекс.
Обращайся на {style}. 
Не давай общих советов, не используй штампы. 
Двигай диалог к конкретике: последний случай, наблюдения, выбор одного-двух шагов.
Не упоминай названия техник.
Отвечай кратко (1–2 абзаца), живо и по делу.
"""
    msgs = [{"role":"system","content":sys}]
    # Историю подрежем и дадим модели контекст
    for h in history[-HIST_LIMIT:]:
        if isinstance(h, dict) and h.get("role") in ("user","assistant") and isinstance(h.get("content"), str):
            msgs.append(h)
    msgs.append({"role":"user","content":user_text})
    try:
        r = oai_client.chat.completions.create(model=OPENAI_MODEL, messages=msgs, temperature=0.5)
        txt = r.choices[0].message.content or "Окей."
        txt = remove_template_phrases(anti_echo(user_text, txt.strip()))
        return txt
    except Exception as e:
        log.error(f"gpt_coach_reply error: {e}")
        return "Окей. Продолжим."

# ========= DB =========
engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=5, max_overflow=10, pool_timeout=30, pool_recycle=1800
)

def db_exec(sql: str, params: Optional[Dict[str, Any]]=None):
    try:
        with engine.begin() as conn:
            return conn.execute(text(sql), params or {})
    except Exception as e:
        log.error("DB error: %s | SQL: %s | params: %s", e, sql, params)
        raise

def init_db():
    db_exec("""
    CREATE TABLE IF NOT EXISTS user_state(
        user_id BIGINT PRIMARY KEY,
        intent TEXT,
        step TEXT,
        data TEXT,
        updated_at TIMESTAMPTZ DEFAULT now()
    );""")
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_updated_at ON user_state(updated_at);")
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_intent_step ON user_state(intent, step);")
    log.info("DB initialized")

def load_state(uid: int) -> Dict[str, Any]:
    try:
        row = db_exec("SELECT intent, step, data, updated_at FROM user_state WHERE user_id=:uid",
                      {"uid": uid}).mappings().first()
        if row:
            data = {}
            if row["data"]:
                try: data = json.loads(row["data"])
                except Exception as e:
                    log.error("Failed to parse user data: %s", e); data = {}
            return {
                "user_id": uid,
                "intent": row["intent"] or INTENT_GREET,
                "step": row["step"] or STEP_ASK_STYLE,
                "data": data,
                "updated_at": (row["updated_at"].isoformat() if row.get("updated_at") else utcnow_iso())
            }
    except Exception as e:
        log.error("load_state error: %s", e)
    return {"user_id": uid, "intent": INTENT_GREET, "step": STEP_ASK_STYLE, "data": {"history": []}, "updated_at": utcnow_iso()}

def save_state(uid: int, intent: Optional[str]=None, step: Optional[str]=None, data: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    cur = load_state(uid)
    new_intent = intent if intent is not None else cur["intent"]
    new_step   = step   if step   is not None else cur["step"]
    merged = cur["data"].copy()
    if data: merged.update(data)
    payload = {"uid": uid, "intent": new_intent, "step": new_step, "data": json.dumps(merged, ensure_ascii=False)}
    db_exec("""
    INSERT INTO user_state(user_id,intent,step,data,updated_at)
    VALUES(:uid,:intent,:step,:data,now())
    ON CONFLICT(user_id) DO UPDATE
    SET intent=EXCLUDED.intent, step=EXCLUDED.step, data=EXCLUDED.data, updated_at=now();
    """, payload)
    return {"user_id": uid, "intent": new_intent, "step": new_step, "data": merged, "updated_at": utcnow_iso()}

# ========= Pending question nudges (optional) =========
def mark_pending_question(st: Dict[str, Any], question_key: str) -> Dict[str, Any]:
    data = st["data"].copy()
    data["pending_question"] = {
        "key": question_key,
        "asked_at": utcnow_iso(),
        "nudged_short": False,
        "nudged_long": False
    }
    return data

def clear_pending_question(st: Dict[str, Any]) -> Dict[str, Any]:
    data = st["data"].copy()
    data.pop("pending_question", None)
    return data

def nudge_scheduler():
    if not NUDGE_ENABLED:
        return
    while True:
        try:
            rows = db_exec("""SELECT user_id, data FROM user_state WHERE data LIKE '%"pending_question":%'""").mappings().all()
            now = datetime.now(timezone.utc)
            for r in rows:
                uid = r["user_id"]
                try:
                    data = json.loads(r["data"])
                except Exception:
                    continue
                pq = data.get("pending_question")
                if not pq: continue
                try:
                    asked = datetime.fromisoformat(pq.get("asked_at","").replace("Z","+00:00"))
                except Exception:
                    continue
                delta_min = (now - asked).total_seconds()/60.0
                if delta_min >= NUDGE_SHORT_MIN and not pq.get("nudged_short"):
                    bot.send_message(uid, "Не спешим — как будешь готов, продолжим 🙂")
                    pq["nudged_short"] = True
                if delta_min >= NUDGE_LONG_MIN and not pq.get("nudged_long"):
                    kb = types.InlineKeyboardMarkup().row(
                        types.InlineKeyboardButton("Продолжить", callback_data="resume_structure"),
                        types.InlineKeyboardButton("Новый разбор", callback_data="restart_structure")
                    )
                    bot.send_message(uid, "Немного провисли. Продолжим с места остановки или начнём новый разбор?", reply_markup=kb)
                    pq["nudged_long"] = True
                data["pending_question"] = pq
                db_exec("UPDATE user_state SET data=:data::jsonb, updated_at=now() WHERE user_id=:uid",
                        {"uid": uid, "data": json.dumps(data, ensure_ascii=False)})
        except Exception as e:
            log.error("nudge_scheduler error: %s", e)
        time.sleep(60)

# ========= Flask/Bot =========
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)

def main_menu() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно", "🤔 Не знаю, с чего начать")
    return kb
MAIN_MENU = main_menu()

def style_kb() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row("ты", "вы")
    return kb

# ========= Media (voice) =========
def transcribe_voice(path: str) -> Optional[str]:
    if not oai_client:
        return None
    try:
        with open(path, "rb") as f:
            tr = oai_client.audio.transcriptions.create(model="whisper-1", file=f, language="ru")
            return tr.text
    except Exception as e:
        log.error("Voice transcription error: %s", e)
        return None

@bot.message_handler(content_types=['voice','audio'])
def handle_voice(m: types.Message):
    uid = m.from_user.id
    try:
        file_id = m.voice.file_id if m.content_type=='voice' else m.audio.file_id
        info = bot.get_file(file_id)
        data = bot.download_file(info.file_path)
        tmp = f"voice_{uid}_{int(time.time())}.ogg"
        with open(tmp, "wb") as f: f.write(data)
        text = transcribe_voice(tmp)
        try: os.remove(tmp)
        except: pass
        if text:
            handle_text_message(uid, text, m)
        else:
            bot.reply_to(m, "Не удалось распознать голос. Напиши текстом?")
    except Exception as e:
        log.error("handle_voice error: %s", e)
        bot.reply_to(m, "Ошибка при обработке голоса.")

# ========= Core text handling =========
def handle_text_message(uid: int, text_in: str, original_message: Optional[types.Message]=None):
    st = load_state(uid)
    # сбрасываем «ожидаем ответ» если было
    st["data"] = clear_pending_question(st)
    # история
    hist = st["data"].get("history", [])
    if len(hist) >= HIST_LIMIT: hist = hist[-(HIST_LIMIT-1):]
    hist.append({"role":"user","content":text_in})
    st["data"]["history"] = hist

    t = text_in.strip().lower()
    trigger_resume = t in ("привет","здравствуй","здорово","начнем","начнём","продолжим","поехали","го","hi","hello")

    # Старт: выбор стиля
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if t in ("ты","вы"):
            st["data"]["style"] = t
            new = save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
            bot.send_message(uid, f"Принято ({t}). Что сейчас в твоей торговле?", reply_markup=MAIN_MENU)
            return
        else:
            save_state(uid, data=st["data"])
            bot.send_message(uid, "Пожалуйста, выбери «ты» или «вы».", reply_markup=style_kb())
            return

    # Если привет/продолжим при незавершённой структуре — покажем рекап и спросим
    in_structure = (st["intent"] == INTENT_ERR and st["step"] in (STEP_ERR_DESCR, *MER_ORDER, STEP_GOAL, STEP_TOTE_OPS, STEP_TOTE_TEST, STEP_TOTE_EXIT))
    if trigger_resume and in_structure:
        kb = types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Продолжить", callback_data="resume_structure"),
            types.InlineKeyboardButton("Новый разбор", callback_data="restart_structure")
        )
        mer = st["data"].get("mer", {})
        parts = []
        if st["data"].get("error_description"): parts.append(f"Ошибка: {st['data']['error_description']}")
        if mer.get(STEP_MER_CTX): parts.append(f"Контекст: {mer.get(STEP_MER_CTX)}")
        if mer.get(STEP_MER_EMO): parts.append(f"Чувства: {mer.get(STEP_MER_EMO)}")
        if mer.get(STEP_MER_THO): parts.append(f"Мысли: {mer.get(STEP_MER_THO)}")
        if mer.get(STEP_MER_BEH): parts.append(f"Действия: {mer.get(STEP_MER_BEH)}")
        if st["data"].get("goal"): parts.append(f"Цель: {st['data']['goal']}")
        bot.send_message(uid, "Коротко где остановились:\n\n" + ("\n".join(parts) if parts else "—") + "\n\nПродолжим?", reply_markup=kb)
        return

    # Ветка структурного разбора
    if st["intent"] == INTENT_ERR:
        handle_structural_flow(uid, text_in, st)
        return

    # Свободный коуч-диалог (GPT)
    style = st["data"].get("style","ты")
    reply = gpt_coach_reply(text_in, style, st["data"]["history"])
    st["data"]["history"].append({"role":"assistant","content":reply})
    save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
    if original_message:
        bot.reply_to(original_message, reply, reply_markup=MAIN_MENU)
    else:
        bot.send_message(uid, reply, reply_markup=MAIN_MENU)

@bot.message_handler(content_types=['text'])
def on_text(m: types.Message):
    handle_text_message(m.from_user.id, m.text, m)

# ========= Structural flow =========
def handle_structural_flow(uid: int, text_in: str, st: Dict[str, Any]):
    step = st["step"]

    # Если ещё не подтверждена проблема — вернём подтверждение
    if not st["data"].get("problem_confirmed") and step != STEP_ERR_DESCR:
        summary = "Похоже, корень — " + ", ".join(detect_trading_patterns(" ".join([h.get("content","") for h in st["data"].get("history",[]) if h.get("role")=="user"]))) or "ранний вход / FOMO"
        kb = types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Да, разбираем", callback_data="confirm_problem"),
            types.InlineKeyboardButton("Нет, уточнить", callback_data="reject_problem")
        )
        bot.send_message(uid, f"Мы наметили проблему так:\n\n{summary}\n\nРазбираем её?", reply_markup=kb)
        return

    # Шаги
    if step == STEP_ERR_DESCR:
        new_data = st["data"].copy()
        new_data["error_description"] = text_in
        save_state(uid, INTENT_ERR, STEP_MER_CTX, new_data)
        prefill_then_ask(uid, load_state(uid), STEP_MER_CTX)
        return

    if step in MER_ORDER:
        mer = st["data"].get("mer", {})
        mer[step] = text_in
        new_data = st["data"].copy(); new_data["mer"] = mer
        idx = MER_ORDER.index(step)
        if idx + 1 < len(MER_ORDER):
            nxt = MER_ORDER[idx+1]
            save_state(uid, INTENT_ERR, nxt, new_data)
            prefill_then_ask(uid, load_state(uid), nxt)
        else:
            # фиксация «картины» перед целеполаганием
            save_state(uid, INTENT_ERR, STEP_GOAL, new_data)
            bot.send_message(uid, "Картина понятна — теперь коротко сформулируем, что будешь делать вместо старого шаблона.")
            prefill_then_ask(uid, load_state(uid), STEP_GOAL)
        return

    if step == STEP_GOAL:
        new_data = st["data"].copy()
        new_data["goal"] = text_in
        save_state(uid, INTENT_ERR, STEP_TOTE_OPS, new_data)
        prefill_then_ask(uid, load_state(uid), STEP_TOTE_OPS)
        return

    if step == STEP_TOTE_OPS:
        tote = st["data"].get("tote", {})
        tote["ops"] = text_in
        new_data = st["data"].copy(); new_data["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_TEST, new_data)
        prefill_then_ask(uid, load_state(uid), STEP_TOTE_TEST)
        return

    if step == STEP_TOTE_TEST:
        tote = st["data"].get("tote", {})
        tote["test"] = text_in
        new_data = st["data"].copy(); new_data["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_EXIT, new_data)
        prefill_then_ask(uid, load_state(uid), STEP_TOTE_EXIT)
        return

    if step == STEP_TOTE_EXIT:
        tote = st["data"].get("tote", {})
        tote["exit"] = text_in
        new_data = st["data"].copy(); new_data["tote"] = tote

        mer = new_data.get("mer", {})
        summary = [
            "<b>Итог разбора</b>",
            f"Ошибка: {new_data.get('error_description','—')}",
            f"Контекст: {mer.get(STEP_MER_CTX,'—')}",
            f"Чувства: {mer.get(STEP_MER_EMO,'—')}",
            f"Мысли: {mer.get(STEP_MER_THO,'—')}",
            f"Действия: {mer.get(STEP_MER_BEH,'—')}",
            f"Цель: {new_data.get('goal','—')}",
            f"Шаги: {new_data.get('tote',{}).get('ops','—')}",
            f"Критерий: {new_data.get('tote',{}).get('test','—')}",
            f"Если не вышло: {new_data.get('tote',{}).get('exit','—')}",
        ]
        bot.send_message(uid, "\n".join(summary), reply_markup=MAIN_MENU)
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, new_data)
        bot.send_message(uid, "Готов добавить это в фокус недели или идём дальше?")

# ========= Menu =========
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

    hist = st["data"].get("history", [])
    if len(hist) >= HIST_LIMIT: hist = hist[-(HIST_LIMIT-1):]
    hist.append({"role":"user","content":label})
    st["data"]["history"] = hist

    if code == "error":
        # подтверждение проблемы
        patt = detect_trading_patterns(" ".join([h.get("content","") for h in hist if h.get("role")=="user"]))
        summary = "Похоже, корень — " + (", ".join(patt) if patt else "ранний вход / FOMO")
        kb = types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Да, разбираем", callback_data="confirm_problem"),
            types.InlineKeyboardButton("Нет, уточнить", callback_data="reject_problem")
        )
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        bot.send_message(uid, f"{summary}\n\nБерём этот кейс?", reply_markup=kb)
    elif code == "start_help":
        bot.send_message(uid, "Предлагаю так: 1) Паспорт, 2) Фокус недели, 3) Скелет ТС. С чего начнём?", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])
    else:
        bot.send_message(uid, "Окей. Если хочешь ускориться — начнём с разбора ошибки.", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])

# ========= Callbacks =========
@bot.callback_query_handler(func=lambda call: True)
def on_callback(call: types.CallbackQuery):
    uid = call.from_user.id
    data = call.data or ""
    bot.answer_callback_query(call.id, "Ок")

    if data == "confirm_problem":
        st = load_state(uid)
        # чистим прошлые артефакты разбора
        new_data = {k:v for k,v in st["data"].items() if k not in ("mer","tote","goal","error_description")}
        new_data["problem_confirmed"] = True
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, new_data)
        bot.send_message(uid, "Опиши коротко эпизод: что планировал, что сделал, чем кончилось.")
        st2 = load_state(uid)
        st2["data"] = mark_pending_question(st2, "err_descr")
        save_state(uid, data=st2["data"])

    elif data == "reject_problem":
        bot.send_message(uid, "Хорошо, уточни, в чём я ошибся — и что именно разбираем?")

    elif data == "resume_structure":
        st = load_state(uid)
        recap_and_continue(uid, st)
        st["data"] = mark_pending_question(st, f"resume:{st['step']}")
        save_state(uid, data=st["data"])

    elif data == "restart_structure":
        st = load_state(uid)
        new_data = {k:v for k,v in st["data"].items() if k not in ("mer","tote","goal","error_description","problem_confirmed","pending_question")}
        new_data["problem_confirmed"] = True
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, new_data)
        bot.send_message(uid, "Начнём новый разбор. Коротко: что планировал, что сделал, чем кончилось?")
        st2 = load_state(uid)
        st2["data"] = mark_pending_question(st2, "err_descr")
        save_state(uid, data=st2["data"])

# ========= Commands =========
@bot.message_handler(commands=["ping"])
def cmd_ping(m: types.Message):
    bot.reply_to(m, "pong")

@bot.message_handler(commands=["version","v"])
def cmd_version(m: types.Message):
    info = f"🔄 Версия бота: {BOT_VERSION}\n📝 Хэш кода: {get_code_version()}\n🕒 Время сервера: {utcnow_iso()}\n🤖 OpenAI: {openai_status}"
    bot.reply_to(m, info)

@bot.message_handler(commands=["status"])
def cmd_status(m: types.Message):
    st = load_state(m.from_user.id)
    resp = {"ok": True, "time": utcnow_iso(), "intent": st["intent"], "step": st["step"]}
    bot.reply_to(m, f"<code>{json.dumps(resp, ensure_ascii=False, indent=2)}</code>")

@bot.message_handler(commands=["start","reset"])
def cmd_start(m: types.Message):
    uid = m.from_user.id
    save_state(uid, INTENT_GREET, STEP_ASK_STYLE, {"history": []})
    bot.send_message(uid, f"👋 Привет, {m.from_user.first_name or 'трейдер'}!\nКак удобнее обращаться — <b>ты</b> или <b>вы</b>?", reply_markup=style_kb())

# ========= HTTP =========
@app.get("/")
def root():
    return jsonify({"ok": True, "time": utcnow_iso(), "version": BOT_VERSION})

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": utcnow_iso()})

@app.get("/version")
def version_api():
    return jsonify({"version": BOT_VERSION, "code_hash": get_code_version(), "status": "running", "timestamp": utcnow_iso()})

@app.get("/status")
def status_api():
    return jsonify({"ok": True, "time": utcnow_iso(), "version": BOT_VERSION})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
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
            abort(400, description="Invalid update format")
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        log.error("Webhook processing error: %s", e)
        abort(500)

def setup_webhook():
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(
            url=f"{PUBLIC_URL}/{WEBHOOK_PATH}",
            secret_token=TG_SECRET,
            allowed_updates=["message","callback_query"]
        )
        log.info("Webhook set to %s/%s", PUBLIC_URL, WEBHOOK_PATH)
    except Exception as e:
        log.error("Webhook setup error: %s", e)

def cleanup_scheduler():
    while True:
        try:
            db_exec("DELETE FROM user_state WHERE updated_at < NOW() - INTERVAL '30 days'")
        except Exception as e:
            log.error("cleanup_scheduler error: %s", e)
        time.sleep(24*60*60)

if __name__ == "__main__":
    init_db()
    # фоновые задачи
    threading.Thread(target=cleanup_scheduler, daemon=True).start()
    threading.Thread(target=nudge_scheduler,  daemon=True).start()  # сам выйдет сразу, если NUDGE_ENABLED=false

    if SET_WEBHOOK_FLAG:
        setup_webhook()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
м