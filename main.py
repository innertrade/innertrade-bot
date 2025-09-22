# main.py — Innertrade Kai Mentor Bot
# Версия: 2025-09-22 (session-continue + idle-nudge)
# Запуск (Render): gunicorn -w 1 -b 0.0.0.0:$PORT main:app

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
        with open(__file__, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except Exception:
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

# Сессии/простои
IDLE_MINUTES = int(os.getenv("IDLE_MINUTES", "60"))  # когда считаем, что пользователь «пропал»
CODEWORD_NEW = os.getenv("CODEWORD_NEW", "новый разбор").strip().lower()
NUDGE_TEXT = os.getenv("NUDGE_TEXT", "Прошло время. Продолжим прошлый разбор или начнём заново?").strip()

# ========= Validation =========
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is required")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL is required")
if not WEBHOOK_PATH:
    raise RuntimeError("WEBHOOK_PATH is required")
if not TG_SECRET:
    raise RuntimeError("TG_WEBHOOK_SECRET is required")

# ========= Logging =========
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("innertrade")
log.info(f"Starting bot version: {BOT_VERSION}")

# ========= INTENTS/STEPS =========
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
STEP_DONE = "done"

MER_ORDER = [STEP_MER_CTX, STEP_MER_EMO, STEP_MER_THO, STEP_MER_BEH]

# ========= OpenAI =========
oai_client = None
openai_status = "disabled"

if OPENAI_API_KEY and OFFSCRIPT_ENABLED:
    try:
        oai_client = OpenAI(api_key=OPENAI_API_KEY)
        # ping
        _ = oai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=4
        )
        openai_status = "active"
        log.info("OpenAI client initialized successfully")
    except Exception as e:
        log.error(f"OpenAI init error: {e}")
        oai_client = None
        openai_status = f"error: {str(e)}"
else:
    log.warning("OpenAI disabled - missing API key or OFFSCRIPT_ENABLED=false")
    openai_status = "disabled"

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
        updated_at TIMESTAMPTZ DEFAULT now(),
        nudge_sent BOOLEAN DEFAULT FALSE
    );
    """)
    # На случай старой схемы — добавляем недостающие колонки
    db_exec("ALTER TABLE user_state ADD COLUMN IF NOT EXISTS nudge_sent BOOLEAN DEFAULT FALSE;")
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_updated_at ON user_state(updated_at)")
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_intent_step ON user_state(intent, step)")
    log.info("DB initialized")

# ========= State =========
def load_state(uid: int) -> Dict[str, Any]:
    try:
        row = db_exec(
            "SELECT intent, step, data, updated_at, nudge_sent FROM user_state WHERE user_id = :uid",
            {"uid": uid}
        ).mappings().first()
        if row:
            data = {}
            if row["data"]:
                try:
                    data = json.loads(row["data"])
                except Exception as e:
                    log.error("Failed to parse user data: %s", e)
                    data = {}
            return {
                "user_id": uid,
                "intent": row["intent"] or INTENT_GREET,
                "step": row["step"] or STEP_ASK_STYLE,
                "data": data,
                "updated_at": row["updated_at"],
                "nudge_sent": bool(row.get("nudge_sent", False))
            }
    except Exception as e:
        log.error("load_state error: %s", e)

    return {
        "user_id": uid,
        "intent": INTENT_GREET,
        "step": STEP_ASK_STYLE,
        "data": {"history": []},
        "updated_at": datetime.now(timezone.utc),
        "nudge_sent": False
    }

def save_state(uid: int, intent: Optional[str] = None,
               step: Optional[str] = None, data: Optional[Dict[str, Any]] = None,
               nudge_sent: Optional[bool] = None) -> Dict[str, Any]:
    cur = load_state(uid)
    new_intent = intent if intent is not None else cur["intent"]
    new_step = step if step is not None else cur["step"]
    new_data = cur["data"].copy()
    if data:
        new_data.update(data)
    if "history" not in new_data:
        new_data["history"] = []

    payload = {
        "uid": uid,
        "intent": new_intent,
        "step": new_step,
        "data": json.dumps(new_data, ensure_ascii=False),
        "nudge_sent": nudge_sent if nudge_sent is not None else cur.get("nudge_sent", False)
    }
    db_exec("""
        INSERT INTO user_state (user_id, intent, step, data, updated_at, nudge_sent)
        VALUES (:uid, :intent, :step, :data, now(), :nudge_sent)
        ON CONFLICT (user_id) DO UPDATE
        SET intent = EXCLUDED.intent,
            step   = EXCLUDED.step,
            data   = EXCLUDED.data,
            updated_at = now(),
            nudge_sent = EXCLUDED.nudge_sent;
    """, payload)

    return {"user_id": uid, "intent": new_intent, "step": new_step,
            "data": new_data, "updated_at": datetime.now(timezone.utc),
            "nudge_sent": payload["nudge_sent"]}

# ========= Bot & Flask =========
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)

# ========= Keyboards =========
def main_menu() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно", "🤔 Не знаю, с чего начать")
    kb.row("🔄 Новый разбор")
    return kb

MAIN_MENU = main_menu()

def style_kb() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row("ты", "вы")
    return kb

def continue_or_new_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("▶️ Продолжаем", callback_data="cont_session"),
        types.InlineKeyboardButton("🔄 Начать заново", callback_data="start_new")
    )
    return kb

# ========= Pattern Detection =========
def detect_trading_patterns(text: str) -> List[str]:
    patterns = {
        "remove_stop": ["убираю стоп", "убираю стоп-лосс", "снимаю стоп"],
        "move_stop": ["двигаю стоп", "передвигаю стоп", "переставляю стоп"],
        "early_close": ["закрыть позицию", "раньше времени закрыть"],
        "averaging": ["усреднение", "докупать", "добавляться"],
        "break_even": ["безубыток", "в ноль", "без убытка"],
        "small_profit": ["мелкий профит", "небольшую прибыль", "скорее зафиксировать"],
        "self_doubt": ["не уверен", "сомневаюсь", "неуверенность"],
        "fear_of_loss": ["страх потерять", "боюсь потерять", "страх убытка"],
        "chaos": ["хаос", "топчусь на месте", "не знаю с чего начать"],
        "rule_breaking": ["нарушаю правила", "не соблюдаю правила", "игнорирую правила"]
    }
    detected = []
    text_lower = (text or "").lower()
    for pattern, keywords in patterns.items():
        if any(k in text_lower for k in keywords):
            detected.append(pattern)
    return detected

def should_suggest_deep_analysis(text: str, patterns: List[str]) -> bool:
    crisis_words = ["систематически", "давно", "не могу", "не получается", "постоянно", "регулярно"]
    return any(w in (text or "").lower() for w in crisis_words) or len(patterns) >= 2

# ========= Helpers =========
def anti_echo(user_text: str, model_text: str) -> str:
    u = (user_text or "").strip().lower()
    m = (model_text or "").strip()
    if len(u) < 15 or len(m) < 15:
        return m
    if SequenceMatcher(None, u, m.lower()).ratio() > 0.7:
        return "Скажу по-своему: " + m
    return m

def remove_template_phrases(text: str) -> str:
    template_phrases = [
        "понимаю, это", "я понимаю, что", "это может быть", "важно понять",
        "сложности с", "давай разберем", "это распространённая проблема",
        "можешь рассказать", "как ты обычно", "что именно вызывает",
        "какие конкретно", "как долго", "в каких ситуациях", "понимаю, как",
        "скажи,", "расскажи,", "важно", "обычно", "часто", "это поможет",
        "давай рассмотрим", "можешь описать", "было бы полезно"
    ]
    s = text
    for phrase in template_phrases:
        s = re.sub(rf"{phrase}[^.!?]*?[.!?]", "", s, flags=re.IGNORECASE)
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'^[,\s\.!?]+', '', s)
    return s

def mer_prompt_for(step: str) -> str:
    prompts = {
        STEP_MER_CTX: "Коротко контекст: где/когда это было?",
        STEP_MER_EMO: "Что чувствовал(а) в момент? 2–3 слова.",
        STEP_MER_THO: "Какие фразы мелькали в голове? 2–3 короткие.",
        STEP_MER_BEH: "Что сделал(а) фактически? Действия."
    }
    return prompts.get(step, "Продолжим.")

def extract_problem_summary(history: List[Dict]) -> str:
    user_messages = [m["content"] for m in history if m.get("role") == "user"]
    patterns = []
    for msg in user_messages:
        patterns.extend(detect_trading_patterns(msg))
    uniq = set(patterns)
    parts = []
    if "self_doubt" in uniq: parts.append("неуверенность в решениях")
    if "fear_of_loss" in uniq: parts.append("страх потерь/стопа")
    if {"remove_stop","move_stop"} & uniq: parts.append("нарушение управления риском (стоп)")
    if "early_close" in uniq: parts.append("преждевременное закрытие")
    if "averaging" in uniq: parts.append("усреднение убыточных")
    if "chaos" in uniq: parts.append("хаос/нет прогресса")
    if "rule_breaking" in uniq: parts.append("системные нарушения правил")
    return "Основные риски: " + ", ".join(parts) if parts else "Нужно уточнение проблемы"

# ========= Voice =========
def transcribe_voice(audio_file_path: str) -> Optional[str]:
    if not oai_client:
        return None
    try:
        with open(audio_file_path, "rb") as f:
            tr = oai_client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="ru"
            )
        return tr.text
    except Exception as e:
        log.error("Voice transcription error: %s", e)
        return None

# ========= GPT Decision =========
def gpt_decide(uid: int, text_in: str, st: Dict[str, Any]) -> Dict[str, Any]:
    fallback = {
        "next_step": st["step"],
        "intent": st["intent"],
        "response_text": "Ок, продолжим.",
        "store": {},
        "is_structural": False
    }
    if not oai_client or not OFFSCRIPT_ENABLED:
        log.warning(f"OpenAI not available: oai_client={oai_client}, OFFSCRIPT_ENABLED={OFFSCRIPT_ENABLED}")
        return fallback

    try:
        history = st["data"].get("history", [])
        style = st["data"].get("style", "ты")
        patterns = detect_trading_patterns(text_in)
        patterns_text = ", ".join(patterns) if patterns else "нет"

        system_prompt = f"""
Ты — тёплый наставник по трейдингу по имени Алекс. Говори на {style}. Будь конкретным и эмпатичным, не занудствуй.
Обнаруженные паттерны: {patterns_text}

ПРАВИЛА:
1) Не используй шаблонные фразы и клише.
2) На проблемные сообщения — предлагай разобрать последний случай.
3) Если пользователь уже дал кейс — продвигай к подтверждению проблемы и запуску разбора.
4) В разборе не называй «MERCEDES»/«TOTE» — просто задавай вопросы по шагам.
5) Будь человеческим: короткие, тёплые фразы + конкретика.
6) Избегай общих советов без привязки к кейсу.

Верни JSON: next_step, intent, response_text, store(object), is_structural(boolean).
"""
        msgs = [{"role": "system", "content": system_prompt}]
        for h in history[-HIST_LIMIT:]:
            if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str):
                msgs.append({"role": h["role"], "content": h["content"]})
        msgs.append({"role": "user", "content": text_in})

        res = oai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=msgs,
            temperature=0.4,
            response_format={"type": "json_object"},
        )
        raw = res.choices[0].message.content or "{}"
        dec = json.loads(raw)
        for k in ["next_step", "intent", "response_text"]:
            if k not in dec:
                return fallback
        if not isinstance(dec.get("store"), dict):
            dec["store"] = {}
        if not isinstance(dec.get("is_structural"), bool):
            dec["is_structural"] = False

        dec["response_text"] = anti_echo(text_in, remove_template_phrases(dec["response_text"]))
        if len(dec["response_text"]) < 8:
            dec["response_text"] = "Хочу понять точнее. Можешь привести короткий пример из последней сделки?"
        return dec
    except Exception as e:
        log.error("GPT decision error: %s", e)
        return fallback

# ========= Greetings / Idle logic =========
GREET_WORDS = {"привет","здравствуй","добрый день","добрый вечер","доброе утро","hi","hello","hey","hai"}

def is_greeting(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(t.startswith(w) for w in GREET_WORDS) or t in GREET_WORDS

def too_idle(updated_at: Optional[datetime]) -> bool:
    if not updated_at:
        return False
    try:
        now = datetime.now(timezone.utc)
        return (now - updated_at) > timedelta(minutes=IDLE_MINUTES)
    except Exception:
        return False

# ========= Message Handlers =========
@bot.message_handler(commands=["ping"])
def cmd_ping(m: types.Message):
    bot.reply_to(m, "pong")

@bot.message_handler(commands=["version", "v"])
def cmd_version(m: types.Message):
    info = f"""🔄 Версия бота: {BOT_VERSION}
📝 Хэш кода: {get_code_version()}
🕒 Время сервера: {datetime.now(timezone.utc).isoformat()}
🤖 OpenAI: {openai_status}"""
    bot.reply_to(m, info)

@bot.message_handler(commands=["status"])
def cmd_status(m: types.Message):
    uid = m.from_user.id
    st = load_state(uid)
    response = {
        "ok": True,
        "time": datetime.now(timezone.utc).isoformat(),
        "intent": st["intent"],
        "step": st["step"],
        "nudge_sent": st.get("nudge_sent", False),
        "idle_minutes": IDLE_MINUTES
    }
    bot.reply_to(m, f"<code>{json.dumps(response, ensure_ascii=False, indent=2)}</code>")

@bot.message_handler(commands=["reset", "start"])
def cmd_reset(m: types.Message):
    uid = m.from_user.id
    save_state(uid, intent=INTENT_GREET, step=STEP_ASK_STYLE, data={"history": []}, nudge_sent=False)
    bot.send_message(uid,
        f"👋 Привет, {m.from_user.first_name or 'трейдер'}!\nКак удобнее обращаться — <b>ты</b> или <b>вы</b>? (напиши одно слово)\n"
        f"Если захочешь начать заново в любой момент, напиши: «{CODEWORD_NEW}».",
        reply_markup=style_kb()
    )

# ===== Voice =====
@bot.message_handler(content_types=['voice', 'audio'])
def handle_voice(message: types.Message):
    try:
        uid = message.from_user.id
        bot.send_chat_action(uid, 'typing')
        file_id = message.voice.file_id if message.content_type == 'voice' else message.audio.file_id
        file_info = bot.get_file(file_id)
        data = bot.download_file(file_info.file_path)
        path = f"temp_voice_{uid}.ogg"
        with open(path, "wb") as f:
            f.write(data)
        text = transcribe_voice(path)
        try:
            os.remove(path)
        except Exception:
            pass
        if text:
            handle_text_message(uid, text, message)
        else:
            bot.reply_to(message, "Не удалось распознать голос. Напиши текстом или пришли ещё раз.")
    except Exception as e:
        log.error("Voice processing error: %s", e)
        bot.reply_to(message, "Произошла ошибка при обработке голоса. Напиши текстом, пожалуйста.")

# ===== Core text =====
def handle_text_message(uid: int, text: str, original_message=None):
    st = load_state(uid)
    txt = (text or "").strip()
    log.info("User %s: intent=%s step=%s text='%s'", uid, st["intent"], st["step"], txt[:120])

    # Кодовое слово «новый разбор»
    if txt.lower() == CODEWORD_NEW or txt == "🔄 Новый разбор":
        save_state(uid, intent=INTENT_ERR, step=STEP_ERR_DESCR,
                   data={"history": []}, nudge_sent=False)
        bot.send_message(uid, "Ок, начнём новый разбор. Опиши последний случай ошибки: что планировал и где отступил?")
        return

    # История (user)
    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "user", "content": txt})
    st["data"]["history"] = history

    # Приветствие + незавершенная сессия или долгий простой
    if is_greeting(txt) and st["intent"] != INTENT_GREET and st["step"] not in (STEP_FREE_INTRO, STEP_DONE):
        # Вежливое уточнение
        bot.send_message(
            uid,
            "Привет! Похоже, мы не закончили прошлый разбор. Продолжаем или начнём заново?",
            reply_markup=continue_or_new_kb()
        )
        save_state(uid, data=st["data"], nudge_sent=False)  # сбрасываем флаг нуджа
        return

    # Если был долгий простой — спросим
    if too_idle(st.get("updated_at")):
        bot.send_message(uid, NUDGE_TEXT, reply_markup=continue_or_new_kb())
        save_state(uid, data=st["data"], nudge_sent=True)
        return

    # Выбор стиля в приветствии
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if txt.lower() in ("ты", "вы"):
            st["data"]["style"] = txt.lower()
            save_state(uid, intent=INTENT_FREE, step=STEP_FREE_INTRO, data=st["data"])
            bot.send_message(uid,
                f"Принято ({txt}). Если захочешь начать заново — напиши «{CODEWORD_NEW}».\n"
                f"Что сейчас происходит в твоей торговле?",
                reply_markup=MAIN_MENU)
        else:
            save_state(uid, data=st["data"])
            bot.send_message(uid, "Пожалуйста, выбери «ты» или «вы».", reply_markup=style_kb())
        return

    # Если мы сейчас в структурном режиме — ведём по шагам
    if st["intent"] == INTENT_ERR and st["step"] != STEP_FREE_INTRO:
        handle_structural_flow(uid, txt, st)
        return

    # Свободный диалог — GPT
    patterns = detect_trading_patterns(txt)
    suggest_analysis = should_suggest_deep_analysis(txt, patterns)
    decision = gpt_decide(uid, txt, st)
    resp = decision.get("response_text", "Понял.")

    # История (assistant)
    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "assistant", "content": resp})

    merged = st["data"].copy()
    store = decision.get("store", {})
    if isinstance(store, dict):
        merged.update(store)
    merged["history"] = history

    new_intent = decision.get("intent", st["intent"])
    new_step = decision.get("next_step", st["step"])

    save_state(uid, intent=new_intent, step=new_step, data=merged, nudge_sent=False)

    # Ответ
    if original_message:
        bot.reply_to(original_message, resp, reply_markup=MAIN_MENU)
    else:
        bot.send_message(uid, resp, reply_markup=MAIN_MENU)

    # Мягкий переход к разбору при необходимости
    if suggest_analysis and new_intent != INTENT_ERR and not st["data"].get("problem_confirmed"):
        problem_summary = extract_problem_summary(history)
        bot.send_message(
            uid,
            f"Похоже, ключевая тема такова:\n\n{problem_summary}\n\nРазберём на конкретном кейсе?",
            reply_markup=types.InlineKeyboardMarkup().row(
                types.InlineKeyboardButton("Да, пойдём в разбор", callback_data="go_struct"),
                types.InlineKeyboardButton("Пока нет", callback_data="no_struct")
            )
        )

@bot.message_handler(content_types=['text'])
def all_text(m: types.Message):
    handle_text_message(m.from_user.id, m.text, m)

# ========= Structural Flow (без названий техник в тексте) =========
def handle_structural_flow(uid: int, text_in: str, st: Dict[str, Any]):
    # a) Описание ошибки
    if st["step"] == STEP_ERR_DESCR:
        new_data = st["data"].copy()
        new_data["error_description"] = text_in
        save_state(uid, intent=INTENT_ERR, step=STEP_MER_CTX, data=new_data)
        bot.send_message(uid, mer_prompt_for(STEP_MER_CTX))
        return

    # b) MER (4 шага)
    if st["step"] in MER_ORDER:
        mer = st["data"].get("mer", {})
        mer[st["step"]] = text_in
        new_data = st["data"].copy()
        new_data["mer"] = mer

        idx = MER_ORDER.index(st["step"])
        if idx + 1 < len(MER_ORDER):
            next_step = MER_ORDER[idx + 1]
            save_state(uid, intent=INTENT_ERR, step=next_step, data=new_data)
            bot.send_message(uid, mer_prompt_for(next_step))
        else:
            # Короткая фиксация MER перед переходом
            fix = [
                "Зафиксируем:",
                f"• Контекст — {mer.get(STEP_MER_CTX, '—')}",
                f"• Эмоции — {mer.get(STEP_MER_EMO, '—')}",
                f"• Мысли — {mer.get(STEP_MER_THO, '—')}",
                f"• Действия — {mer.get(STEP_MER_BEH, '—')}"
            ]
            bot.send_message(uid, "\n".join(fix))
            save_state(uid, intent=INTENT_ERR, step=STEP_GOAL, data=new_data)
            bot.send_message(uid, "Теперь сформулируй позитивную цель: что делаешь вместо прежнего поведения?")
        return

    # c) Цель
    if st["step"] == STEP_GOAL:
        new_data = st["data"].copy()
        new_data["goal"] = text_in
        save_state(uid, intent=INTENT_ERR, step=STEP_TOTE_OPS, data=new_data)
        bot.send_message(uid, "Назови 2–3 конкретных шага для ближайших 3 сделок (коротко, списком).")
        return

    # d) Операции
    if st["step"] == STEP_TOTE_OPS:
        tote = st["data"].get("tote", {})
        tote["ops"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote
        save_state(uid, intent=INTENT_ERR, step=STEP_TOTE_TEST, data=new_data)
        bot.send_message(uid, "Как поймёшь, что получилось? Один простой критерий.")
        return

    # e) Проверка
    if st["step"] == STEP_TOTE_TEST:
        tote = st["data"].get("tote", {})
        tote["test"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote
        save_state(uid, intent=INTENT_ERR, step=STEP_TOTE_EXIT, data=new_data)
        bot.send_message(uid, "Если проверка покажет «не получилось», что сделаешь дальше?")
        return

    # f) Выход (итог)
    if st["step"] == STEP_TOTE_EXIT:
        tote = st["data"].get("tote", {})
        tote["exit"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote

        summary = [
            "<b>Итог разбора</b>",
            f"Ошибка: {new_data.get('error_description', '—')}",
            f"Контекст: {new_data.get('mer', {}).get(STEP_MER_CTX, '—')}",
            f"Эмоции: {new_data.get('mer', {}).get(STEP_MER_EMO, '—')}",
            f"Мысли: {new_data.get('mer', {}).get(STEP_MER_THO, '—')}",
            f"Действия: {new_data.get('mer', {}).get(STEP_MER_BEH, '—')}",
            f"Цель: {new_data.get('goal', '—')}",
            f"Шаги: {new_data.get('tote', {}).get('ops', '—')}",
            f"Критерий: {new_data.get('tote', {}).get('test', '—')}",
            f"Если не вышло: {new_data.get('tote', {}).get('exit', '—')}"
        ]
        save_state(uid, intent=INTENT_FREE, step=STEP_FREE_INTRO, data=new_data, nudge_sent=False)
        bot.send_message(uid, "\n".join(summary), reply_markup=MAIN_MENU)
        bot.send_message(uid, "Готов добавить это в фокус недели или идём дальше?")
        return

# ========= Callbacks =========
@bot.callback_query_handler(func=lambda call: True)
def on_callback(call: types.CallbackQuery):
    uid = call.from_user.id
    data = call.data or ""
    bot.answer_callback_query(call.id)
    st = load_state(uid)

    if data == "confirm_problem" or data == "go_struct":
        save_state(uid, intent=INTENT_ERR, step=STEP_ERR_DESCR, data={"problem_confirmed": True}, nudge_sent=False)
        bot.send_message(uid, "Отлично. Опиши последний случай: вход/план, где отступил, результат.")
        return
    if data == "reject_problem" or data == "no_struct":
        bot.send_message(uid, "Хорошо. Тогда с чего начнём сейчас?", reply_markup=MAIN_MENU)
        return
    if data == "cont_session":
        # Просто сообщим и продолжим текущий шаг
        bot.send_message(uid, "Продолжаем с того места, где остановились.", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"], nudge_sent=False)
        # Нежно «подтолкнём» вопросом по текущему шагу
        if st["intent"] == INTENT_ERR:
            step = st["step"]
            if step in MER_ORDER:
                bot.send_message(uid, mer_prompt_for(step))
            elif step == STEP_ERR_DESCR:
                bot.send_message(uid, "Напомни коротко: что планировал и где отступил?")
            elif step == STEP_GOAL:
                bot.send_message(uid, "Сформулируй позитивную цель: что делаешь вместо прежнего поведения?")
            elif step == STEP_TOTE_OPS:
                bot.send_message(uid, "Назови 2–3 конкретных шага (коротко, списком).")
            elif step == STEP_TOTE_TEST:
                bot.send_message(uid, "Как поймёшь, что получилось? Один простой критерий.")
            elif step == STEP_TOTE_EXIT:
                bot.send_message(uid, "Если «не получилось», что сделаешь дальше?")
        return
    if data == "start_new":
        save_state(uid, intent=INTENT_ERR, step=STEP_ERR_DESCR, data={"history": []}, nudge_sent=False)
        bot.send_message(uid, "Ок, начнём заново. Опиши последний случай ошибки: что планировал и где отступил?")
        return

# ========= Menu =========
MENU_BTNS = {
    "🚑 У меня ошибка": "error",
    "🧩 Хочу стратегию": "strategy",
    "📄 Паспорт": "passport",
    "🗒 Панель недели": "weekpanel",
    "🆘 Экстренно": "panic",
    "🤔 Не знаю, с чего начать": "start_help",
    "🔄 Новый разбор": "new_case",
}

@bot.message_handler(func=lambda m: m.text in MENU_BTNS.keys())
def handle_menu(m: types.Message):
    uid = m.from_user.id
    st = load_state(uid)
    label = m.text
    code = MENU_BTNS[label]

    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "user", "content": label})
    st["data"]["history"] = history

    if code in ("error", "new_case"):
        save_state(uid, intent=INTENT_ERR, step=STEP_ERR_DESCR, data=st["data"], nudge_sent=False)
        bot.send_message(uid, "Опиши последний случай, когда произошла ошибка: что планировал и где отступил?")
    elif code == "start_help":
        bot.send_message(uid, "Предлагаю так: 1) Паспорт, 2) Фокус недели, 3) Скелет ТС. С чего начнём?", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])
    else:
        bot.send_message(uid, "Окей. Если хочешь ускориться — начнём с разбора ошибки.", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])

# ========= Webhook / Health =========
@app.get("/")
def root():
    return jsonify({"ok": True, "time": datetime.now(timezone.utc).isoformat()})

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})

@app.get("/version")
def version_api():
    return jsonify({
        "version": BOT_VERSION,
        "code_hash": get_code_version(),
        "status": "running",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

@app.get("/status")
def status():
    return jsonify({"ok": True, "time": datetime.now(timezone.utc).isoformat(), "version": BOT_VERSION})

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
            log.error("Failed to parse update")
            abort(400, description="Invalid update")
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
            allowed_updates=["message", "callback_query"]
        )
        log.info("Webhook set to %s/%s", PUBLIC_URL, WEBHOOK_PATH)
    except Exception as e:
        log.error("Webhook setup error: %s", e)

def cleanup_old_states(days: int = 60):
    try:
        result = db_exec(
            "DELETE FROM user_state WHERE updated_at < NOW() - (INTERVAL '1 day' * :days)",
            {"days": days}
        )
        log.info("Cleaned up %s old user states", result.rowcount)
    except Exception as e:
        log.error("Cleanup error: %s", e)

def nudge_scheduler():
    """Раз в 5 минут: если пользователь «завис» дольше IDLE_MINUTES и нудж не отправляли — отправляем «Продолжим?»"""
    while True:
        try:
            rows = db_exec("""
                SELECT user_id, step, intent, data, updated_at, nudge_sent
                FROM user_state
                WHERE updated_at < NOW() - (INTERVAL '1 minute' * :idle)
                  AND nudge_sent = FALSE
                  AND intent = :intent_err
                  AND step NOT IN (:free_intro, :done)
            """, {
                "idle": IDLE_MINUTES,
                "intent_err": INTENT_ERR,
                "free_intro": STEP_FREE_INTRO,
                "done": STEP_DONE
            }).mappings().all()
            for r in rows:
                uid = r["user_id"]
                try:
                    bot.send_message(uid, NUDGE_TEXT, reply_markup=continue_or_new_kb())
                    save_state(uid, nudge_sent=True)
                    time.sleep(0.3)
                except Exception as e:
                    log.error("Nudge send error to %s: %s", uid, e)
        except Exception as e:
            log.error("Nudge scheduler error: %s", e)
        time.sleep(300)  # 5 минут

def cleanup_scheduler():
    while True:
        time.sleep(24 * 60 * 60)
        cleanup_old_states(60)

# ========= Entry =========
if __name__ == "__main__":
    init_db()

    # фоновые потоки
    threading.Thread(target=cleanup_scheduler, daemon=True).start()
    threading.Thread(target=nudge_scheduler, daemon=True).start()

    if SET_WEBHOOK_FLAG:
        setup_webhook()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
