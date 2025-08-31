# main.py — Innertrade Kai Mentor Bot (Production Ready)
# Версия: 2025-09-02-mentor-v3

import os
import json
import time
import logging
import threading
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List, Tuple
from difflib import SequenceMatcher

import requests
from flask import Flask, request, abort, jsonify
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool
import telebot
from telebot import types
from openai import OpenAI

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
if OPENAI_API_KEY and OFFSCRIPT_ENABLED:
    try:
        oai_client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client initialized")
    except Exception as e:
        log.error("OpenAI init error: %s", e)
        oai_client = None

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
        updated_at TIMESTAMPTZ DEFAULT now()
    );""")
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_updated_at ON user_state(updated_at)")
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_intent_step ON user_state(intent, step)")
    log.info("DB initialized")

# ========= State Management =========
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
    new_intent = intent if intent is not None else cur["intent"]
    new_step = step if step is not None else cur["step"]
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

# ========= Bot & Flask =========
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)

# ========= Keyboards =========
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

# ========= Pattern Detection =========
def detect_trading_patterns(text: str) -> List[str]:
    """Детекция паттернов нарушения правил"""
    patterns = {
        "remove_stop": ["убираю стоп", "убираю стоп-лосс", "снимаю стоп"],
        "move_stop": ["двигаю стоп", "передвигаю стоп", "переставляю стоп"],
        "early_close": ["закрыть позицию", "раньше времени закрыть"],
        "averaging": ["усреднение", "докупать", "добавляться"],
        "break_even": ["безубыток", "в ноль", "без убытка"],
        "small_profit": ["мелкий профит", "небольшую прибыль", "скорее зафиксировать"],
        "self_doubt": ["не уверен", "сомневаюсь", "неуверенность"],
        "fear_of_loss": ["страх потерять", "боюсь потерять", "страх убытка"]
    }
    
    detected = []
    text_lower = text.lower()
    for pattern, keywords in patterns.items():
        if any(keyword in text_lower for keyword in keywords):
            detected.append(pattern)
    
    return detected

def should_suggest_deep_analysis(text: str, patterns: List[str]) -> bool:
    """Когда предлагать глубокий разбор"""
    crisis_words = ["систематически", "давно", "не могу", "не получается", "постоянно", "регулярно"]
    has_crisis = any(word in text.lower() for word in crisis_words)
    has_patterns = len(patterns) >= 2
    
    return has_crisis or has_patterns

# ========= Helpers =========
def anti_echo(user_text: str, model_text: str) -> str:
    u = (user_text or "").strip().lower()
    m = (model_text or "").strip()
    if len(u) < 15 or len(m) < 15:
        return m
    similarity = SequenceMatcher(None, u, m.lower()).ratio()
    if similarity > 0.7:
        return "Понял. Скажу по-своему: " + m
    return m

def mer_prompt_for(step: str) -> str:
    prompts = {
        STEP_MER_CTX: "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует? (1–2 коротких предложения)",
        STEP_MER_EMO: "ЭМОЦИИ. Что чувствуешь в момент ошибки? (несколько слов)",
        STEP_MER_THO: "МЫСЛИ. Какие фразы крутятся в голове? (1–2 коротких)",
        STEP_MER_BEH: "ПОВЕДЕНИЕ. Что именно ты делаешь? Опиши действия глаголами.",
    }
    return prompts.get(step, "Продолжим.")

# ========= Voice Handling =========
def transcribe_voice(audio_file_path: str) -> Optional[str]:
    """Транскрибация голосового сообщения через Whisper"""
    if not oai_client:
        return None
        
    try:
        with open(audio_file_path, "rb") as audio_file:
            transcript = oai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru"
            )
            return transcript.text
    except Exception as e:
        log.error("Voice transcription error: %s", e)
        return None

# ========= GPT Decision Maker =========
def gpt_decide(uid: int, text_in: str, st: Dict[str, Any]) -> Dict[str, Any]:
    fallback = {
        "next_step": st["step"],
        "intent": st["intent"],
        "response_text": "Понял. Продолжим.",
        "store": {},
        "is_structural": False
    }
    if not oai_client or not OFFSCRIPT_ENABLED:
        return fallback

    try:
        history = st["data"].get("history", [])
        
        # Добавляем контекст о паттернах и стиле общения
        style = st["data"].get("style", "ты")
        patterns = detect_trading_patterns(text_in)
        patterns_text = ", ".join(patterns) if patterns else "не обнаружено"
        
        system_prompt = f"""
        Ты — тёплый наставник по трейдингу по имени Алекс. Веди естественный диалог, но сохраняй траекторию.
        Всегда обращайся на {style}, как выбрал пользователь.
        
        Обнаруженные паттерны: {patterns_text}
        
        ВАЖНЫЕ ПРАВИЛА:
        1. НИКОГДА не начинай ответ с шаблонных фраз: "Понимаю, это...", "Я понимаю, что...", "Это может быть..."
        2. НИКОГДА не ссылайся на предыдущее сообщение пользователя явно
        3. НИКОГДА не задавай циклические вопросы (про "как долго" или "в каких ситуациях")
        4. Всегда предлагай конкретику вместо абстракций
        5. При обнаружении проблемы сразу предлагай разобрать последний случай
        6. Будь проактивным - предлагай глубокий разбор при системных проблемах
        7. Сохраняй empathetic тон, но без шаблонных фраз
        8. Будь кратким в повседневном общении (1-2 абзаца)
        9. При детекции кризиса переходи в режим поддержки
        
        Ответ отдавай строка JSON с ключами: next_step, intent, response_text, store(объект), is_structural(true/false).
        """

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

        required = ["next_step", "intent", "response_text", "store", "is_structural"]
        if not all(k in dec for k in required):
            return fallback
        if not isinstance(dec.get("store"), dict):
            dec["store"] = {}
        if not isinstance(dec.get("is_structural"), bool):
            dec["is_structural"] = False

        dec["response_text"] = anti_echo(text_in, dec["response_text"])
        return dec

    except Exception as e:
        log.error("GPT decision error: %s", e)
        return fallback

# ========= Commands =========
@bot.message_handler(commands=["ping"])
def cmd_ping(m: types.Message):
    bot.reply_to(m, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m: types.Message):
    uid = m.from_user.id
    st = load_state(uid)
    response = {
        "ok": True,
        "time": datetime.now(timezone.utc).isoformat(),
        "intent": st["intent"],
        "step": st["step"],
        "db": "ok"
    }
    bot.reply_to(m, f"<code>{json.dumps(response, ensure_ascii=False, indent=2)}</code>")

@bot.message_handler(commands=["reset", "start"])
def cmd_reset(m: types.Message):
    uid = m.from_user.id
    save_state(uid, intent=INTENT_GREET, step=STEP_ASK_STYLE, data={"history": []})
    bot.send_message(
        uid,
        f"👋 Привет, {m.from_user.first_name or 'трейдер'}!\nКак удобнее обращаться — <b>ты</b> или <b>вы</b>? (напиши одно слово)",
        reply_markup=style_kb()
    )

# ========= Media Handler =========
@bot.message_handler(content_types=['voice', 'audio'])
def handle_voice(message: types.Message):
    """Обработка голосовых сообщений"""
    try:
        uid = message.from_user.id
        bot.send_chat_action(uid, 'typing')
        
        # Скачиваем голосовое сообщение
        file_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Сохраняем временный файл
        voice_path = f"temp_voice_{uid}.ogg"
        with open(voice_path, 'wb') as f:
            f.write(downloaded_file)
        
        # Транскрибируем
        text = transcribe_voice(voice_path)
        
        # Удаляем временный файл
        try:
            os.remove(voice_path)
        except:
            pass
        
        if text:
            # Обрабатываем как текстовое сообщение
            handle_text_message(uid, text, message)
        else:
            bot.reply_to(message, "Не удалось распознать голосовое сообщение. Попробуй еще раз или напиши текстом.")
            
    except Exception as e:
        log.error("Voice processing error: %s", e)
        bot.reply_to(message, "Произошла ошибка при обработке голоса. Попробуй написать текстом.")

def handle_text_message(uid: int, text: str, original_message=None):
    """Обработка текстовых сообщений (общая функция)"""
    st = load_state(uid)
    log.info("User %s: intent=%s step=%s text='%s'", uid, st["intent"], st["step"], text[:80])

    # Update history (user)
    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "user", "content": text})
    st["data"]["history"] = history

    # Greeting: style selection
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if text.lower() in ("ты", "вы"):
            st["data"]["style"] = text.lower()
            save_state(uid, intent=INTENT_FREE, step=STEP_FREE_INTRO, data=st["data"])
            bot.send_message(uid, f"Принято ({text}). Расскажи, что сейчас происходит в твоей торговле?", reply_markup=MAIN_MENU)
        else:
            save_state(uid, data=st["data"])
            bot.send_message(uid, "Пожалуйста, выбери «ты» или «вы».", reply_markup=style_kb())
        return

    # Structural flow
    if st["intent"] == INTENT_ERR:
        handle_structural_flow(uid, text, st)
        return

    # Free flow — GPT
    patterns = detect_trading_patterns(text)
    suggest_analysis = should_suggest_deep_analysis(text, patterns)
    
    decision = gpt_decide(uid, text, st)
    resp = decision.get("response_text", "Понял.")

    # Update history (assistant)
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

    save_state(uid, intent=new_intent, step=new_step, data=merged)
    
    # Отправляем ответ
    if original_message:
        bot.reply_to(original_message, resp, reply_markup=MAIN_MENU)
    else:
        bot.send_message(uid, resp, reply_markup=MAIN_MENU)
    
    # Проактивное предложение помощи
    if suggest_analysis and new_intent != INTENT_ERR:
        bot.send_message(
            uid, 
            "Похоже, это системная проблема. Хочешь разберем её подробно?",
            reply_markup=types.InlineKeyboardMarkup().row(
                types.InlineKeyboardButton("Да, давай разберем", callback_data="deep_analysis_yes"),
                types.InlineKeyboardButton("Пока нет", callback_data="deep_analysis_no")
            )
        )

@bot.message_handler(content_types=['text'])
def all_text(m: types.Message):
    """Обработка текстовых сообщений"""
    handle_text_message(m.from_user.id, m.text, m)

# ========= Structural Flow =========
def handle_structural_flow(uid: int, text_in: str, st: Dict[str, Any]):
    # a) Error description
    if st["step"] == STEP_ERR_DESCR:
        new_data = st["data"].copy()
        new_data["error_description"] = text_in
        save_state(uid, intent=INTENT_ERR, step=STEP_MER_CTX, data=new_data)
        bot.send_message(uid, mer_prompt_for(STEP_MER_CTX))
        return

    # b) MERCEDES (4 шага)
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
            save_state(uid, intent=INTENT_ERR, step=STEP_GOAL, data=new_data)
            bot.send_message(uid, "Теперь сформулируй <b>позитивную цель</b>: что хочешь делать вместо прежнего поведения? (одно предложение)")
        return

    # c) Goal
    if st["step"] == STEP_GOAL:
        new_data = st["data"].copy()
        new_data["goal"] = text_in
        save_state(uid, intent=INTENT_ERR, step=STEP_TOTE_OPS, data=new_data)
        bot.send_message(uid, "Отлично. Назови 2–3 конкретных шага (операции), которые помогут удержать цель в ближайших 3 сделках.")
        return

    # d) TOTE - operations
    if st["step"] == STEP_TOTE_OPS:
        tote = st["data"].get("tote", {})
        tote["ops"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote
        save_state(uid, intent=INTENT_ERR, step=STEP_TOTE_TEST, data=new_data)
        bot.send_message(uid, "Как поймёшь, что получилось? Дай один простой критерий проверки (например: «3 сделки подряд без сдвига стопа»).")
        return

    # e) TOTE - test
    if st["step"] == STEP_TOTE_TEST:
        tote = st["data"].get("tote", {})
        tote["test"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote
        save_state(uid, intent=INTENT_ERR, step=STEP_TOTE_EXIT, data=new_data)
        bot.send_message(uid, "Что сделаешь, если проверка покажет «не получилось»? (например, «стоп-процедура и пауза»)")
        return

    # f) TOTE - exit (final)
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
            f"Поведение: {new_data.get('mer', {}).get(STEP_MER_BEH, '—')}",
            f"Цель: {new_data.get('goal', '—')}",
            f"Шаги (OPS): {new_data.get('tote', {}).get('ops', '—')}",
            f"Проверка: {new_data.get('tote', {}).get('test', '—')}",
            f"Если не вышло (EXIT): {new_data.get('tote', {}).get('exit', '—')}",
        ]
        save_state(uid, intent=INTENT_FREE, step=STEP_FREE_INTRO, data=new_data)
        bot.send_message(uid, "\n".join(summary), reply_markup=MAIN_MENU)
        bot.send_message(uid, "Готов добавить это в фокус недели или идём дальше?")

# ========= Callback =========
@bot.callback_query_handler(func=lambda call: True)
def on_callback(call: types.CallbackQuery):
    uid = call.from_user.id
    data = call.data or ""
    bot.answer_callback_query(call.id, "Ок")
    
    if data == "deep_analysis_yes":
        save_state(uid, intent=INTENT_ERR, step=STEP_ERR_DESCR)
        bot.send_message(uid, "Отлично! Опиши последний случай, когда это произошло:")
    elif data == "deep_analysis_no":
        bot.send_message(uid, "Хорошо, как скажешь. Если передумаешь — просто напиши об этом.", reply_markup=MAIN_MENU)

# ========= Menu Handlers =========
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

    # history
    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "user", "content": label})
    st["data"]["history"] = history

    if code == "error":
        save_state(uid, intent=INTENT_ERR, step=STEP_ERR_DESCR, data=st["data"])
        bot.send_message(uid, "Опиши последний случай, когда произошла ошибка:")
    elif code == "start_help":
        bot.send_message(uid, "Предлагаю так: 1) Паспорт, 2) Фокус недели, 3) Скелет ТС. С чего начнём?", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])
    else:
        bot.send_message(uid, "Окей. Если хочешь ускориться — начнём с разбора ошибки.", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])

# ========= Webhook / Health =========
@app.get("/")
def root():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat()})

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

@app.get("/status")
def status():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat(), "version": "2025-09-02-mentor-v3"})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    # Security checks
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_SECRET:
        abort(401)
    if request.content_length and request.content_length > MAX_BODY:
        abort(413, description="Body too large")

    body = request.get_data()
    if not body:
        abort(400, description="Empty body")

    try:
        json_str = body.decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        if update is None:
            log.error("Failed to parse update: %s", json_str)
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
            allowed_updates=["message", "callback_query"]
        )
        log.info("Webhook set to %s/%s", PUBLIC_URL, WEBHOOK_PATH)
    except Exception as e:
        log.error("Webhook setup error: %s", e)

def cleanup_old_states(days: int = 30):
    """Cleans up old user states"""
    try:
        result = db_exec(
            "DELETE FROM user_state WHERE updated_at < NOW() - INTERVAL ':days days'",
            {"days": days}
        )
        log.info("Cleaned up %s old user states", result.rowcount)
    except Exception as e:
        log.error("Cleanup error: %s", e)

def cleanup_scheduler():
    """Runs cleanup daily"""
    while True:
        time.sleep(24 * 60 * 60)  # 24 hours
        cleanup_old_states(30)

if __name__ == "__main__":
    init_db()
    
    # Start cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
    cleanup_thread.start()
    
    if SET_WEBHOOK_FLAG:
        setup_webhook()
        
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)