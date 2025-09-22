# main.py — Innertrade Kai Mentor Bot
# Версия: 2025-09-22 (coach-mode)

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

# Validation
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

# Logging
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
        log.info("OpenAI client initialized successfully")
    except Exception as e:
        log.error(f"OpenAI init error: {e}")
        oai_client = None
        openai_status = f"error: {e}"
else:
    log.warning("OpenAI disabled — missing API key or OFFSCRIPT_ENABLED=false")
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

def init_db(silent: bool = False):
    try:
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
        if not silent:
            log.info("DB initialized")
    except Exception as e:
        log.error("init_db error (soft): %s", e)

# ========= State =========
def load_state(uid: int) -> Dict[str, Any]:
    try:
        row = db_exec(
            "SELECT intent, step, data FROM user_state WHERE user_id = :uid",
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
                "data": data
            }
    except Exception as e:
        log.error("load_state error: %s", e)
    return {"user_id": uid, "intent": INTENT_GREET, "step": STEP_ASK_STYLE, "data": {"history": []}}

def save_state(uid: int, intent: Optional[str] = None,
               step: Optional[str] = None, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cur = load_state(uid)
    new_intent = cur["intent"] if intent is None else intent
    new_step = cur["step"] if step is None else step
    new_data = cur["data"].copy()
    if data:
        new_data.update(data)
    payload = {
        "uid": uid,
        "intent": new_intent,
        "step": new_step,
        "data": json.dumps(new_data, ensure_ascii=False),
    }
    db_exec("""
        INSERT INTO user_state (user_id, intent, step, data, updated_at)
        VALUES (:uid, :intent, :step, :data, now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent = EXCLUDED.intent,
            step   = EXCLUDED.step,
            data   = EXCLUDED.data,
            updated_at = now();
    """, payload)
    return {"user_id": uid, "intent": new_intent, "step": new_step, "data": new_data}

# ========= App & Bot =========
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)

# ========= Keyboards =========
def main_menu() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно", "🤔 Не зна́ю, с чего начать")
    return kb

MAIN_MENU = main_menu()

def style_kb() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row("ты", "вы")
    return kb

# ========= Pattern Detection =========
def detect_trading_patterns(text: str) -> List[str]:
    patterns = {
        "remove_stop": ["убираю стоп", "убрал стоп", "убрала стоп", "снимаю стоп", "без стопа"],
        "move_stop": ["двигаю стоп", "передвигаю стоп", "переставляю стоп", "отодвигал стоп", "перенёс стоп"],
        "early_close": ["раньше времени закрыл", "закрыл рано", "маленький плюс закрыл", "выход раньше"],
        "averaging": ["усреднение", "добавлялся", "докупал против", "доливался"],
        "break_even": ["в безубыток", "перевёл в ноль", "перевод в безубыток"],
        "small_profit": ["мизерный плюс", "мелкий профит", "быстрая фиксация"],
        "self_doubt": ["сомневаюсь", "не уверен", "стрессую", "паника", "волнение"],
        "fear_of_loss": ["страх потерь", "боюсь потерять", "боюсь убытка"],
        "fomo": ["упустил", "поезд уедет", "без меня пойдёт", "уходит без меня"],
        "chaos": ["хаос", "топчусь", "не знаю с чего начать", "смущало"],
        "rule_breaking": ["нарушаю правила", "отошёл от плана", "игнорирую план"]
    }
    tl = (text or "").lower()
    detected: List[str] = []
    for name, keys in patterns.items():
        if any(k in tl for k in keys):
            detected.append(name)
    return detected

def risky_patterns(pats: List[str]) -> bool:
    risk = {"remove_stop", "move_stop", "averaging", "early_close", "fomo"}
    return any(p in risk for p in pats) or len(pats) >= 2

# ========= Helpers =========
TEMPLATE_CHUNKS = [
    "понимаю", "это может быть", "важно понять", "сложности с", "давай разбер",
    "распространённая проблема", "можешь рассказать", "как ты обычно",
    "что именно вызывает", "какие конкретно", "как долго", "в каких ситуациях",
    "это поможет", "давай рассмотрим", "было бы полезно", "постараемся", "сфокусируйся"
]

def remove_template_phrases(text_in: str) -> str:
    text = text_in or ""
    for ph in TEMPLATE_CHUNKS:
        text = re.sub(rf"(?i)\b{re.escape(ph)}[^.!?]*[.!?]", " ", text)
    text = re.sub(r'\s+', ' ', text).strip(" ,.!?")
    return text

def anti_echo(user_text: str, model_text: str) -> str:
    u = (user_text or "").strip().lower()
    m = (model_text or "").strip()
    if len(u) < 15 or len(m) < 15:
        return m
    if SequenceMatcher(None, u, m.lower()).ratio() > 0.7:
        return "Скажу по-своему: " + m
    return m

def mer_prompt_for(step: str) -> str:
    prompts = {
        STEP_MER_CTX: "Где/когда это случилось? Коротко контекст.",
        STEP_MER_EMO: "Что в моменте чувствуешь (2–3 слова)?",
        STEP_MER_THO: "Какие мысли мелькали (2–3 короткие фразы)?",
        STEP_MER_BEH: "Что сделал фактически? Опиши действия.",
    }
    return prompts.get(step, "Продолжим.")

def extract_problem_summary(history: List[Dict]) -> str:
    user_msgs = [m["content"] for m in history if m.get("role") == "user"]
    pats: List[str] = []
    for m in user_msgs:
        pats.extend(detect_trading_patterns(m))
    up = sorted(set(pats))
    parts = []
    if "self_doubt" in up: parts.append("неуверенность после входа")
    if "fear_of_loss" in up: parts.append("страх потерь")
    if "fomo" in up: parts.append("FOMO / страх упустить ход")
    if "remove_stop" in up or "move_stop" in up: parts.append("трогание/снятие стопа")
    if "early_close" in up: parts.append("ранний выход")
    if "averaging" in up: parts.append("усреднение против позиции")
    if "chaos" in up: parts.append("хаос/сомнения")
    if "rule_breaking" in up: parts.append("нарушение плана/ТС")
    return "Основные триггеры: " + (", ".join(parts) if parts else "нужно уточнить на примере")

# ========= Voice =========
def transcribe_voice(audio_file_path: str) -> Optional[str]:
    if not oai_client:
        return None
    try:
        with open(audio_file_path, "rb") as audio_file:
            tr = oai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru"
            )
            return tr.text
    except Exception as e:
        log.error("Voice transcription error: %s", e)
        return None

# ========= GPT (Coach-mode) =========
def gpt_decide(uid: int, text_in: str, st: Dict[str, Any]) -> Dict[str, Any]:
    """Строго коучинговый режим: никаких советов/диагнозов; 1 конкретный вопрос или мостик в структуру."""
    fallback = {
        "next_step": st["step"],
        "intent": st["intent"],
        "response_text": "Возьмём этот пример. Что именно было сигналом к входу и что было планом по стопу/выходу?",
        "store": {},
        "is_structural": False
    }
    if not oai_client or not OFFSCRIPT_ENABLED:
        return fallback

    try:
        history = st["data"].get("history", [])
        style = st["data"].get("style", "ты")
        patterns = detect_trading_patterns(text_in)
        patterns_text = ", ".join(patterns) if patterns else "нет"

        system_prompt = f"""
Ты — коуч-наставник по трейдингу Алекс. НЕ консультант и НЕ аналитик.
Запрещено: давать советы, диагнозы, инструкции («сделай X», «используй Y», «записывай Z»), обобщения и морализаторство.
Цель: помочь пользователю понять свой паттерн через вопросы и провести его через MERCEDES → Goal → TOTE.

Форма ответа: кратко (1–2 абзаца), по делу, разговорно на «{style}», без шаблонных фраз.
Если информации достаточно — задай ровно ОДИН чёткий уточняющий вопрос, который продвигает разбор вперёд.
Если явно пора идти в структуру — поставь is_structural=true и сформулируй мостик к разбору на примере последнего кейса (без советов).

Обнаруженные паттерны: {patterns_text}

Верни JSON:
{{
  "next_step": "<оставь текущее значение шага, если не уверен>",
  "intent": "<оставь текущее значение интента, если не уверен>",
  "response_text": "<краткий вопрос или мостик в структуру, без советов>",
  "store": {{}},
  "is_structural": <true|false>
}}
""".strip()

        msgs = [{"role": "system", "content": system_prompt}]
        for h in history[-HIST_LIMIT:]:
            if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str):
                msgs.append({"role": h["role"], "content": h["content"]})
        msgs.append({"role": "user", "content": text_in})

        res = oai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=msgs,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        raw = res.choices[0].message.content or "{}"
        dec = json.loads(raw)

        if not isinstance(dec, dict):
            return fallback
        for k in ["next_step", "intent", "response_text", "store", "is_structural"]:
            if k not in dec:
                return fallback
        if not isinstance(dec.get("store"), dict):
            dec["store"] = {}
        if not isinstance(dec.get("is_structural"), bool):
            dec["is_structural"] = False

        dec["response_text"] = remove_template_phrases(anti_echo(text_in, dec["response_text"]))

        ban_words = ["попробуй", "используй", "придерживайся", "записывай", "установи", "следуй", "пересмотри", "сфокусируйся"]
        if any(bw in dec["response_text"].lower() for bw in ban_words) or len(dec["response_text"]) < 10:
            dec["response_text"] = "Возьмём этот пример. Что было планом по входу/стопу и в какой момент ты от него отступил?"

        return dec

    except Exception as e:
        log.error("GPT decision error: %s", e)
        return fallback

# ========= Commands =========
@bot.message_handler(commands=["ping"])
def cmd_ping(m: types.Message):
    bot.reply_to(m, "pong")

@bot.message_handler(commands=["menu"])
def cmd_menu(m: types.Message):
    bot.send_message(m.chat.id, "Меню:", reply_markup=MAIN_MENU)

@bot.message_handler(commands=["help"])
def cmd_help(m: types.Message):
    bot.reply_to(m, "Я коуч по торговле. Через вопросы помогаю увидеть паттерн и провести разбор: MERCEDES → цель → TOTE. Начни с «🚑 У меня ошибка» или опиши ситуацию.", reply_markup=MAIN_MENU)

@bot.message_handler(commands=["version", "v"])
def cmd_version(m: types.Message):
    version_info = f"""🔄 Версия бота: {BOT_VERSION}
📝 Хэш кода: {get_code_version()}
🕒 Время сервера: {datetime.now(timezone.utc).isoformat()}
🤖 OpenAI: {openai_status}"""
    bot.reply_to(m, version_info)

@bot.message_handler(commands=["debug"])
def cmd_debug(m: types.Message):
    dbg = {
        "openai_available": bool(oai_client),
        "offscript_enabled": OFFSCRIPT_ENABLED,
        "has_api_key": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL,
        "openai_status": openai_status
    }
    bot.reply_to(m, f"<code>{json.dumps(dbg, ensure_ascii=False, indent=2)}</code>")

@bot.message_handler(commands=["status"])
def cmd_status(m: types.Message):
    uid = m.from_user.id
    st = load_state(uid)
    resp = {
        "ok": True,
        "time": datetime.now(timezone.utc).isoformat(),
        "intent": st["intent"],
        "step": st["step"],
        "db": "ok"
    }
    bot.reply_to(m, f"<code>{json.dumps(resp, ensure_ascii=False, indent=2)}</code>")

@bot.message_handler(commands=["reset", "start"])
def cmd_reset(m: types.Message):
    uid = m.from_user.id
    save_state(uid, intent=INTENT_GREET, step=STEP_ASK_STYLE, data={"history": [], "style_set": False})
    bot.send_message(uid, f"👋 Привет, {m.from_user.first_name or 'трейдер'}!\nКак удобнее обращаться — <b>ты</b> или <b>вы</b>? (напиши одно слово)", reply_markup=style_kb())

# ========= Structural Offer =========
def offer_structural(uid: int, st: Dict[str, Any]):
    if st["data"].get("struct_offer_shown"):
        return
    st["data"]["struct_offer_shown"] = True
    save_state(uid, data=st["data"])
    summary = extract_problem_summary(st["data"].get("history", []))
    kb = types.InlineKeyboardMarkup().row(
        types.InlineKeyboardButton("Разобрать по шагам", callback_data="start_error_flow"),
        types.InlineKeyboardButton("Пока нет", callback_data="skip_error_flow")
    )
    bot.send_message(uid, f"{summary}\n\nПредлагаю короткий разбор: MERCEDES → TOTE. Начинаем?", reply_markup=kb)

# ========= Text =========
@bot.message_handler(content_types=['text'])
def all_text(m: types.Message):
    handle_text_message(m.from_user.id, m.text, m)

def handle_text_message(uid: int, text: str, original_message=None):
    st = load_state(uid)
    log.info("User %s: intent=%s step=%s text='%s'", uid, st["intent"], st["step"], text[:150])

    # history (user)
    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "user", "content": text})
    st["data"]["history"] = history

    # guard: если стиль уже выбран, но по ошибке мы в GREET/ASK_STYLE — починим стейт на лету
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE and st["data"].get("style"):
        st["data"]["style_set"] = True
        st = save_state(uid, intent=INTENT_FREE, step=STEP_FREE_INTRO, data=st["data"])

    # Greeting: выбор стиля
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if text.lower() in ("ты", "вы"):
            st["data"]["style"] = text.lower()
            st["data"]["style_set"] = True
            save_state(uid, intent=INTENT_FREE, step=STEP_FREE_INTRO, data=st["data"])
            bot.send_message(uid, f"Принято ({text}). Что сейчас хочется поправить в трейдинге?", reply_markup=MAIN_MENU)
        else:
            save_state(uid, data=st["data"])
            bot.send_message(uid, "Пожалуйста, выбери «ты» или «вы».", reply_markup=style_kb())
        return

    # Structural flow
    if st["intent"] == INTENT_ERR:
        handle_structural_flow(uid, text, st)
        return

    # Free flow — GPT (коуч)
    patterns = detect_trading_patterns(text)
    decision = gpt_decide(uid, text, st)
    resp = decision.get("response_text") or "Давай на примере: что было планом по входу/стопу и где ты от него отошёл?"

    # history (assistant)
    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "assistant", "content": resp})

    merged = st["data"].copy()
    store = decision.get("store", {})
    if isinstance(store, dict):
        merged.update(store)
    merged["history"] = history

    new_intent = decision.get("intent") or st["intent"]
    new_step = decision.get("next_step") or st["step"]

    st_after = save_state(uid, intent=new_intent, step=new_step, data=merged)

    if original_message:
        bot.reply_to(original_message, resp, reply_markup=MAIN_MENU)
    else:
        bot.send_message(uid, resp, reply_markup=MAIN_MENU)

    if decision.get("is_structural", False) or risky_patterns(patterns):
        offer_structural(uid, st_after)

# ========= Structural Flow =========
def handle_structural_flow(uid: int, text_in: str, st: Dict[str, Any]):
    # a) описание ошибки
    if st["step"] == STEP_ERR_DESCR:
        new_data = st["data"].copy()
        new_data["error_description"] = text_in
        save_state(uid, intent=INTENT_ERR, step=STEP_MER_CTX, data=new_data)
        bot.send_message(uid, mer_prompt_for(STEP_MER_CTX))
        return

    # b) MERCEDES
    if st["step"] in MER_ORDER:
        mer = st["data"].get("mer", {})
        mer[st["step"]] = text_in
        new_data = st["data"].copy()
        new_data["mer"] = mer

        idx = MER_ORDER.index(st["step"])
        if idx + 1 < len(MER_ORDER):
            nxt = MER_ORDER[idx + 1]
            save_state(uid, intent=INTENT_ERR, step=nxt, data=new_data)
            bot.send_message(uid, mer_prompt_for(nxt))
        else:
            save_state(uid, intent=INTENT_ERR, step=STEP_GOAL, data=new_data)
            bot.send_message(uid, "Сформулируй позитивную цель: что делаешь вместо прежнего поведения?")
        return

    # c) Goal
    if st["step"] == STEP_GOAL:
        new_data = st["data"].copy()
        new_data["goal"] = text_in
        save_state(uid, intent=INTENT_ERR, step=STEP_TOTE_OPS, data=new_data)
        bot.send_message(uid, "Назови 2–3 конкретных шага для ближайших 3 сделок (коротко, списком).")
        return

    # d) TOTE - ops
    if st["step"] == STEP_TOTE_OPS:
        tote = st["data"].get("tote", {})
        tote["ops"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote
        save_state(uid, intent=INTENT_ERR, step=STEP_TOTE_TEST, data=new_data)
        bot.send_message(uid, "Как поймёшь, что получилось? Один простой критерий.")
        return

    # e) TOTE - test
    if st["step"] == STEP_TOTE_TEST:
        tote = st["data"].get("tote", {})
        tote["test"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote
        save_state(uid, intent=INTENT_ERR, step=STEP_TOTE_EXIT, data=new_data)
        bot.send_message(uid, "Что сделаешь, если проверка покажет «не получилось»?")
        return

    # f) TOTE - exit
    if st["step"] == STEP_TOTE_EXIT:
        tote = st["data"].get("tote", {})
        tote["exit"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote

        mer = new_data.get('mer', {})
        summary = [
            "<b>Итог разбора</b>",
            f"Ошибка: {new_data.get('error_description', '—')}",
            f"Контекст: {mer.get(STEP_MER_CTX, '—')}",
            f"Эмоции: {mer.get(STEP_MER_EMO, '—')}",
            f"Мысли: {mer.get(STEP_MER_THO, '—')}",
            f"Поведение: {mer.get(STEP_MER_BEH, '—')}",
            f"Цель: {new_data.get('goal', '—')}",
            f"Шаги: {new_data.get('tote', {}).get('ops', '—')}",
            f"Проверка: {new_data.get('tote', {}).get('test', '—')}",
            f"Если не вышло: {new_data.get('tote', {}).get('exit', '—')}",
        ]
        save_state(uid, intent=INTENT_FREE, step=STEP_FREE_INTRO, data=new_data)
        bot.send_message(uid, "\n".join(summary), reply_markup=MAIN_MENU)
        bot.send_message(uid, "Добавим это в фокус недели или идём дальше?")

# ========= Callbacks =========
@bot.callback_query_handler(func=lambda call: True)
def on_callback(call: types.CallbackQuery):
    uid = call.from_user.id
    data = call.data or ""
    bot.answer_callback_query(call.id, "Ок")

    if data == "start_error_flow":
        st = load_state(uid)
        st["data"]["problem_confirmed"] = True
        save_state(uid, intent=INTENT_ERR, step=STEP_ERR_DESCR, data=st["data"])
        bot.send_message(uid, "Начинаем разбор. Опиши последний случай: вход/план, где отступил, что получилось в итоге.")
    elif data == "skip_error_flow":
        bot.send_message(uid, "Хорошо. Вернёмся к этому, когда будешь готов.", reply_markup=MAIN_MENU)

# ========= Menu =========
MENU_BTNS = {
    "🚑 У меня ошибка": "error",
    "🧩 Хочу стратегию": "strategy",
    "📄 Паспорт": "passport",
    "🗒 Панель недели": "weekpanel",
    "🆘 Экстренно": "panic",
    "🤔 Не зна́ю, с чего начать": "start_help",
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

    if code == "error":
        save_state(uid, intent=INTENT_ERR, step=STEP_ERR_DESCR, data=st["data"])
        bot.send_message(uid, "Опиши последний кейс ошибки: где/когда, вход/стоп/план, что пошло не так.")
    elif code == "start_help":
        bot.send_message(uid, "План: 1) быстрый разбор ошибки, 2) фокус недели, 3) скелет ТС. С чего начнём?", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])
    else:
        bot.send_message(uid, "Ок. Если хочешь ускориться — нажми «🚑 У меня ошибка».", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])

# ========= HTTP =========
def _now_iso():
    return datetime.now(timezone.utc).isoformat()

@app.get("/")
def root():
    return jsonify({"ok": True, "time": _now_iso()})

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": _now_iso()})

@app.get("/version")
def version_api():
    return jsonify({
        "version": BOT_VERSION,
        "code_hash": get_code_version(),
        "status": "running",
        "timestamp": _now_iso()
    })

@app.get("/status")
def status():
    return jsonify({"ok": True, "time": _now_iso(), "version": BOT_VERSION})

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

# ========= Maintenance =========
def cleanup_old_states(days: int = 30):
    try:
        db_exec("DELETE FROM user_state WHERE updated_at < NOW() - make_interval(days => :days)", {"days": days})
        log.info("Old user states cleanup done (> %s days).", days)
    except Exception as e:
        log.error("Cleanup error: %s", e)

def cleanup_scheduler():
    while True:
        time.sleep(24 * 60 * 60)
        cleanup_old_states(30)

# ========= Boot (Flask 3.x) =========
_boot_done = False

@app.before_serving
def boot():
    global _boot_done
    if _boot_done:
        return
    _boot_done = True

    init_db(silent=False)
    try:
        th = threading.Thread(target=cleanup_scheduler, daemon=True)
        th.start()
    except Exception as e:
        log.error("Can't start cleanup thread: %s", e)

    if SET_WEBHOOK_FLAG:
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

# ========= Dev run =========
if __name__ == "__main__":
    init_db(silent=False)
    try:
        th = threading.Thread(target=cleanup_scheduler, daemon=True)
        th.start()
    except Exception as e:
        log.error("Can't start cleanup thread: %s", e)
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
