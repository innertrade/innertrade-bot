# main.py — Innertrade Kai Mentor Bot
# Версия: 2025-09-22 (coach-struct v4-fixed)

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
from functools import lru_cache

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

# Поведение сессии / напоминаний
RESUME_THRESHOLD_MIN = int(os.getenv("RESUME_THRESHOLD_MIN", "60"))       # если прошло > N мин молчания — при следующем сообщении спросим «продолжим?»
REMIND_AFTER_MIN = int(os.getenv("REMIND_AFTER_MIN", "5"))                # тихий пинг если молчат N минут после вопроса
REMIND_REPEAT_MIN = int(os.getenv("REMIND_REPEAT_MIN", "60"))             # не пинговать чаще чем раз в N минут

if not TELEGRAM_TOKEN or not DATABASE_URL or not PUBLIC_URL or not TG_SECRET:
    raise RuntimeError("ENV variables missing")

# ========= Logging =========
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("kai-mentor")
log.info(f"Starting bot version: {BOT_VERSION}")

# ========= Intents/Steps =========
INTENT_GREET = "greet"
INTENT_FREE = "free"
INTENT_ERR = "error"

STEP_ASK_STYLE = "ask_style"
STEP_FREE_INTRO = "free_intro"

STEP_ERR_CONFIRM = "err_confirm"   # новое: подтверждение формулировки проблемы перед разбором
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
    );
    """)
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_updated_at ON user_state(updated_at)")
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_intent_step ON user_state(intent, step)")
    log.info("DB initialized")

# ========= State =========
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
        return {"user_id": uid, "intent": row["intent"] or INTENT_GREET,
                "step": row["step"] or STEP_ASK_STYLE, "data": data}
    # дефолтные поля для менеджмента сессии
    return {"user_id": uid, "intent": INTENT_GREET, "step": STEP_ASK_STYLE,
            "data": {"history": [], "last_activity_at": _now_iso(), "awaiting_reply": False}}

def save_state(uid: int, intent=None, step=None, data=None) -> Dict[str, Any]:
    cur = load_state(uid)
    intent = intent or cur["intent"]
    step = step or cur["step"]
    new_data = cur["data"].copy()
    if data:
        new_data.update(data)
    # авто-метки времени
    new_data["last_activity_at"] = _now_iso()
    db_exec("""
        INSERT INTO user_state (user_id, intent, step, data, updated_at)
        VALUES (:uid, :intent, :step, :data, now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent=EXCLUDED.intent, step=EXCLUDED.step, data=EXCLUDED.data, updated_at=now()
    """, {"uid": uid, "intent": intent, "step": step, "data": json.dumps(new_data, ensure_ascii=False)})
    return {"user_id": uid, "intent": intent, "step": step, "data": new_data}

# ========= Bot / Flask =========
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)

MAIN_MENU = types.ReplyKeyboardMarkup(resize_keyboard=True)
MAIN_MENU.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
MAIN_MENU.row("📄 Паспорт", "🗒 Панель недели")
MAIN_MENU.row("🆘 Экстренно", "🤔 Не знаю, с чего начать")

STYLE_KB = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
STYLE_KB.row("ты", "вы")

RESUME_KB = types.InlineKeyboardMarkup()
RESUME_KB.row(
    types.InlineKeyboardButton("🔁 Продолжаем", callback_data="resume_flow"),
    types.InlineKeyboardButton("🆕 Новый разбор", callback_data="new_flow")
)

CONFIRM_KB = types.InlineKeyboardMarkup()
CONFIRM_KB.row(
    types.InlineKeyboardButton("Да, верно", callback_data="confirm_problem"),
    types.InlineKeyboardButton("Нет, скорректировать", callback_data="refine_problem"),
)

# ========= Pattern Detection =========
RISK_PATTERNS = {
    "remove_stop": ["убираю стоп", "снял стоп", "без стопа"],
    "move_stop": ["двигаю стоп", "отодвинул стоп", "переставил стоп"],
    "early_close": ["закрыл рано", "вышел в ноль", "мизерный плюс", "ранний выход"],
    "averaging": ["усреднение", "доливался против", "докупал против"],
    "fomo": ["поезд уедет", "упустил", "уйдёт без меня", "страх упустить"],
    "rule_breaking": ["нарушил план", "отошёл от плана", "игнорировал план"],
}
EMO_PATTERNS = {
    "self_doubt": ["сомневаюсь", "не уверен", "стресс", "паника", "волнение"],
    "fear_of_loss": ["страх потерь", "боюсь стопа", "не хочу быть обманутым"],
    "chaos": ["хаос", "суета", "путаюсь"],
}

@lru_cache(maxsize=1000)
def detect_trading_patterns_cached(text: str) -> List[str]:
    """Кэшированная версия детекции паттернов"""
    tl = (text or "").lower()
    hits = []
    for name, keys in {**RISK_PATTERNS, **EMO_PATTERNS}.items():
        if any(k in tl for k in keys):
            hits.append(name)
    return hits

def detect_trading_patterns(text: str) -> List[str]:
    return detect_trading_patterns_cached(text)

def should_force_structural(text: str) -> bool:
    pats = detect_trading_patterns(text)
    risk = set(pats) & set(RISK_PATTERNS.keys())
    return bool(risk) or ("fear_of_loss" in pats) or ("self_doubt" in pats)

# ========= Helpers =========
BAN_TEMPLATES = [
    "понимаю", "это может быть", "важно понять", "давай рассмотрим", "было бы полезно",
    "попробуй", "используй", "придерживайся", "установи", "сфокусируйся", "следуй", "пересмотри"
]

def strip_templates(text_in: str) -> str:
    t = text_in or ""
    for ph in BAN_TEMPLATES:
        t = re.sub(rf"(?i)\b{re.escape(ph)}[^.!?]*[.!?]", " ", t)
    t = re.sub(r'\s+', ' ', t).strip(" ,.!?")
    return t

def anti_echo(user_text: str, model_text: str) -> str:
    u = (user_text or "").strip().lower()
    m = (model_text or "").strip()
    if len(u) >= 15 and len(m) >= 15 and SequenceMatcher(None, u, m.lower()).ratio() > 0.7:
        return "Скажу иначе: " + m
    return m

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _iso_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s: return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def mer_prompt_for(step: str) -> str:
    # мягкие формулировки
    return {
        STEP_MER_CTX: "Зафиксируем контекст: где и когда это было? Пару слов.",
        STEP_MER_EMO: "Чтобы мне точнее подстроиться: какие чувства всплыли в тот момент? 2–3 слова.",
        STEP_MER_THO: "Какие короткие мысли мелькали? 2–3 фразы.",
        STEP_MER_BEH: "Что ты сделал(а) фактически? Пошагово и коротко.",
    }.get(step, "Продолжим.")

def extract_problem_summary(history: List[Dict]) -> str:
    user_msgs = [m["content"] for m in history if m.get("role") == "user"]
    pats: List[str] = []
    for m in user_msgs:
        pats.extend(detect_trading_patterns(m))
    up = sorted(set(pats))
    parts = []
    if "fomo" in up: parts.append("FOMO (страх упустить)")
    if "remove_stop" in up or "move_stop" in up: parts.append("трогаешь/снимаешь стоп")
    if "early_close" in up: parts.append("ранний выход/«в ноль»")
    if "averaging" in up: parts.append("усреднение против позиции")
    if "fear_of_loss" in up: parts.append("страх стопа/потерь")
    if "self_doubt" in up: parts.append("сомнения после входа")
    return "Похоже, ключевая тема: " + (", ".join(parts) if parts else "нужно уточнение")

# ========= Voice (Whisper) =========
def transcribe_voice(audio_file_path: str) -> Optional[str]:
    if not oai_client:
        log.warning("Whisper: client not available")
        return None
    try:
        with open(audio_file_path, "rb") as audio_file:
            tr = oai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru"
            )
        return getattr(tr, "text", None)
    except Exception as e:
        log.error("Whisper error: %s", e)
        return None

# ========= GPT (strict coach) =========
def gpt_decide(uid: int, text_in: str, st: Dict[str, Any]) -> Dict[str, Any]:
    """Один точный вопрос/мостик. Без советов и списков техник."""
    fallback = {
        "next_step": st["step"],
        "intent": st["intent"],
        "response_text": "Извини, произошла техническая ошибка. Попробуй переформулировать вопрос.",
        "store": {},
        "is_structural": False
    }
    
    if not oai_client or not OFFSCRIPT_ENABLED:
        log.warning("OpenAI not available")
        return fallback

    style = st["data"].get("style", "ты")
    patterns = detect_trading_patterns(text_in)
    patterns_text = ", ".join(patterns) if patterns else "нет"

    system_prompt = f"""
Ты — коуч-наставник по трейдингу Алекс (дружелюбно и кратко, разговорно на «{style}»).
Не консультируешь и не даёшь советов. Двигаешь разбор вопросом или коротким мостиком.
Если явный паттерн — предлагай перейти к чёткой структуре (без упоминания названий техник).

Формат ответа: JSON с полями:
- next_step
- intent
- response_text  (1–2 коротких абзаца, без общих советов)
- store          (объект)
- is_structural  (true/false — пора ли в структурный разбор)
Обнаруженные паттерны: {patterns_text}
""".strip()

    msgs = [{"role": "system", "content": system_prompt}]
    for h in st["data"].get("history", [])[-HIST_LIMIT:]:
        if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str):
            msgs.append(h)
    msgs.append({"role": "user", "content": text_in})

    try:
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

        resp = strip_templates(anti_echo(text_in, dec.get("response_text", ""))).strip()
        if any(b in resp.lower() for b in ["попробуй", "используй", "придерживайся", "установи", "сфокусируйся", "следуй", "пересмотри"]) or len(resp) < 12:
            resp = "Окей. На этом кейсе: где именно ты отступил от плана (вход/стоп/выход)?"
        dec["response_text"] = resp

        if should_force_structural(text_in):
            dec["is_structural"] = True

        return dec
    except Exception as e:
        log.error("GPT decision error: %s", e)
        # Возвращаем понятное сообщение об ошибке пользователю
        fallback["response_text"] = "Извини, произошла техническая ошибка при обработке запроса. Попробуй переформулировать вопрос или повторить позже."
        return fallback

# ========= High-level UX helpers =========
def offer_structural(uid: int, st: Dict[str, Any]):
    """Предложить перейти к структурному разбору (без названий техник)."""
    if st["data"].get("struct_offer_shown"):
        return
    st["data"]["struct_offer_shown"] = True
    summary = extract_problem_summary(st["data"].get("history", []))
    save_state(uid, data=st["data"])
    kb = types.InlineKeyboardMarkup().row(
        types.InlineKeyboardButton("🔁 Разобрать по шагам", callback_data="start_error_flow"),
        types.InlineKeyboardButton("Позже", callback_data="skip_error_flow")
    )
    bot.send_message(uid, f"{summary}\n\nПредлагаю короткий пошаговый разбор. Поехали?", reply_markup=kb)

def set_awaiting(uid: int, st: Dict[str, Any], awaiting: bool):
    st["data"]["awaiting_reply"] = awaiting
    if awaiting:
        st["data"]["last_prompt_at"] = _now_iso()
    save_state(uid, data=st["data"])

def greet_or_resume(uid: int, st: Dict[str, Any], text_in: str) -> bool:
    """Если приветствие/долгая пауза — спросить «продолжаем?»; возвращает True если отправили резюм-карточку."""
    tl = (text_in or "").strip().lower()
    is_greeting = tl in ("привет", "hi", "hello", "здравствуй", "добрый день", "добрый вечер", "йо", "ку", "хай") or \
                  tl.startswith("привет ")
    
    # если «новый разбор» — мгновенно в чистый разбор
    if any(key in tl for key in ["новый разбор", "с нуля", "начать заново", "start over"]):
        st["data"].pop("mer", None)
        st["data"].pop("tote", None)
        st["data"].pop("error_description", None)
        st["data"]["problem_confirmed"] = False
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        bot.send_message(uid, "Окей, начнём с чистого листа. Опиши последний случай: что планировал и где отступил?")
        set_awaiting(uid, st, True)
        return True

    # если было молчание дольше порога — мягко уточнить
    last = _iso_to_dt(st["data"].get("last_activity_at"))
    if is_greeting or (last and datetime.now(timezone.utc) - last > timedelta(minutes=RESUME_THRESHOLD_MIN)):
        # если есть незавершённый разбор — спросим
        if st["intent"] == INTENT_ERR and st["step"] not in (STEP_FREE_INTRO, STEP_ASK_STYLE):
            bot.send_message(uid, "Привет! Похоже, мы не завершили прошлый разбор. Продолжим или начнём заново?", reply_markup=RESUME_KB)
            set_awaiting(uid, st, False)
            return True
    return False

# ========= Handlers =========
@bot.message_handler(commands=["start", "reset"])
def cmd_start(m: types.Message):
    data = {"history": [], "last_activity_at": _now_iso(), "awaiting_reply": False}
    bot.send_message(m.from_user.id,
        "👋 Привет! Как удобнее — <b>ты</b> или <b>вы</b>?\n\n"
        "Если захочешь начать чистый лист, просто напиши: <b>новый разбор</b>.",
        reply_markup=STYLE_KB)
    save_state(m.from_user.id, INTENT_GREET, STEP_ASK_STYLE, data)

@bot.message_handler(commands=["version", "v"])
def cmd_version(m: types.Message):
    info = (
        f"🔄 Версия бота: {BOT_VERSION}\n"
        f"📝 Хэш кода: {get_code_version()}\n"
        f"🕒 Время сервера: {datetime.now(timezone.utc).isoformat()}\n"
        f"🤖 OpenAI: {openai_status}"
    )
    bot.reply_to(m, info)

@bot.message_handler(commands=["menu"])
def cmd_menu(m: types.Message):
    bot.send_message(m.chat.id, "Меню:", reply_markup=MAIN_MENU)

@bot.message_handler(content_types=['voice', 'audio'])
def handle_voice(message: types.Message):
    uid = message.from_user.id
    try:
        file_id = message.voice.file_id if message.content_type == 'voice' else message.audio.file_id
        file_info = bot.get_file(file_id)
        data = bot.download_file(file_info.file_path)
        tmp_name = f"voice_{uid}_{int(time.time())}.ogg"
        with open(tmp_name, "wb") as f:
            f.write(data)
        txt = transcribe_voice(tmp_name)
        try:
            os.remove(tmp_name)
        except Exception:
            pass
        if not txt:
            bot.reply_to(message, "Не удалось распознать голос. Скажи ещё раз или напиши текстом.")
            return
        handle_text_message(uid, txt, original_message=message)
    except Exception as e:
        log.error("Voice processing error: %s", e)
        bot.reply_to(message, "Произошла ошибка при обработке голоса. Напиши текстом, пожалуйста.")

@bot.message_handler(content_types=["text"])
def on_text(m: types.Message):
    handle_text_message(m.from_user.id, m.text.strip(), m)

def handle_text_message(uid: int, text_in: str, original_message=None):
    st = load_state(uid)
    log.info("User %s: intent=%s step=%s text='%s'", uid, st["intent"], st["step"], text_in[:200])

    # history (user)
    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "user", "content": text_in})
    st["data"]["history"] = history

    # приветствие/резюм - ВАЖНО: если вернули True, выходим из функции
    if greet_or_resume(uid, st, text_in):
        return  # ← КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: выходим если обработали приветствие

    # выбор стиля
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if text_in.lower() in ("ты", "вы"):
            st["data"]["style"] = text_in.lower()
            save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
            bot.send_message(uid,
                f"Принято ({text_in}). Что сейчас в трейдинге хочется поправить? "
                "Если появится желание начать с чистого листа — напиши «новый разбор».",
                reply_markup=MAIN_MENU)
            set_awaiting(uid, st, False)
        else:
            save_state(uid, data=st["data"])
            bot.send_message(uid, "Выбери «ты» или «вы».", reply_markup=STYLE_KB)
        return

    # явный запрос «новый разбор» - ВАЖНО: добавляем return после обработки
    tl = text_in.lower()
    if any(key in tl for key in ["новый разбор", "с нуля", "начать заново", "start over"]):
        st["data"].pop("mer", None)
        st["data"].pop("tote", None)
        st["data"].pop("error_description", None)
        st["data"]["problem_confirmed"] = False
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        bot.send_message(uid, "Окей, начнём заново. Опиши последний случай: что планировал и где отступил?")
        set_awaiting(uid, st, True)
        return  # ← КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: выходим после обработки

    # меню/интенты
    if st["intent"] == INTENT_ERR:
        handle_structural(uid, text_in, st)
        return

    # свободный коуч-поток
    decision = gpt_decide(uid, text_in, st)
    resp = decision.get("response_text") or "Окей. На этом кейсе: где именно ты отступил от плана (вход/стоп/выход)?"

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
    st_after = save_state(uid, new_intent, new_step, merged)

    if original_message:
        bot.reply_to(original_message, resp, reply_markup=MAIN_MENU)
    else:
        bot.send_message(uid, resp, reply_markup=MAIN_MENU)

    # отметим «ждём ответ»
    set_awaiting(uid, st_after, True)

    # предложение «пойти по шагам»
    if decision.get("is_structural", False) or should_force_structural(text_in):
        offer_structural(uid, st_after)

# ========= Structural Flow =========
def handle_structural(uid: int, text_in: str, st: Dict[str, Any]):
    # если ещё не подтверждена проблема — попросим согласовать формулировку
    if st["step"] == STEP_FREE_INTRO or st["step"] == STEP_ERR_CONFIRM:
        # возьмём короткое резюме из последних сообщений
        summary = extract_problem_summary(st["data"].get("history", []))
        bot.send_message(uid, f"Зафиксирую проблему, чтобы мы говорили об одном и том же:\n\n<b>{summary}</b>\n\nВерно сформулировал?", reply_markup=CONFIRM_KB)
        save_state(uid, INTENT_ERR, STEP_ERR_CONFIRM, st["data"])
        set_awaiting(uid, st, True)
        return

    # a) описание ошибки (ввод)
    if st["step"] == STEP_ERR_DESCR:
        new_data = st["data"].copy()
        new_data["error_description"] = text_in
        # мягкий мостик к шагам
        bot.send_message(uid, "Окей, двигаемся короткими шагами — я рядом, темп задаёшь ты.")
        save_state(uid, INTENT_ERR, STEP_MER_CTX, new_data)
        bot.send_message(uid, mer_prompt_for(STEP_MER_CTX))
        set_awaiting(uid, st, True)
        return

    # b) шаги (без названий техник)
    if st["step"] in MER_ORDER:
        mer = st["data"].get("mer", {})
        mer[st["step"]] = text_in
        new_data = st["data"].copy()
        new_data["mer"] = mer

        idx = MER_ORDER.index(st["step"])
        if idx + 1 < len(MER_ORDER):
            nxt = MER_ORDER[idx + 1]
            save_state(uid, INTENT_ERR, nxt, new_data)
            # короткий мостик между вопросами
            bot.send_message(uid, "Принял. Следующий маленький штрих.")
            bot.send_message(uid, mer_prompt_for(nxt))
        else:
            # мини-фиксация «итога части»
            m = new_data.get("mer", {})
            bot.send_message(uid,
                "Собрали картинку: контекст, эмоции, мысли и действия. Теперь — куда хочешь прийти вместо прежней реакции.")
            save_state(uid, INTENT_ERR, STEP_GOAL, new_data)
            bot.send_message(uid, "Сформулируй позитивную цель: что делаешь вместо прежнего поведения?")
        set_awaiting(uid, st, True)
        return

    # c) Goal
    if st["step"] == STEP_GOAL:
        new_data = st["data"].copy()
        new_data["goal"] = text_in
        bot.send_message(uid, "Окей. Закрепим простыми шагами на ближайшие 3 сделки.")
        save_state(uid, INTENT_ERR, STEP_TOTE_OPS, new_data)
        bot.send_message(uid, "Назови 2–3 конкретных шага (коротко, списком).")
        set_awaiting(uid, st, True)
        return

    # d) Проверка
    if st["step"] == STEP_TOTE_OPS:
        tote = st["data"].get("tote", {})
        tote["ops"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_TEST, new_data)
        bot.send_message(uid, "Как поймёшь, что получилось? Один простой критерий.")
        set_awaiting(uid, st, True)
        return

    # e) Что делаем, если «не получилось»
    if st["step"] == STEP_TOTE_TEST:
        tote = st["data"].get("tote", {})
        tote["test"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_EXIT, new_data)
        bot.send_message(uid, "И последний штрих: что сделаешь, если проверка покажет «не получилось»?")
        set_awaiting(uid, st, True)
        return

    # f) Итог
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
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, new_data)
        bot.send_message(uid, "\n".join(summary), reply_markup=MAIN_MENU)
        bot.send_message(uid, "Готов вынести это в фокус недели или идём дальше?")
        set_awaiting(uid, st, False)

# ========= Menu handlers =========
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

    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "user", "content": label})
    st["data"]["history"] = history

    if code == "error":
        st["data"]["problem_confirmed"] = False
        save_state(uid, INTENT_ERR, STEP_ERR_CONFIRM, st["data"])
        # Сразу попросим подтвердить формулировку по истории
        summary = extract_problem_summary(st["data"].get("history", []))
        bot.send_message(uid, f"Хочу убедиться, что верно понял:\n\n<b>{summary}</b>\n\nПодходит такая формулировка?", reply_markup=CONFIRM_KB)
        set_awaiting(uid, st, True)
    elif code == "start_help":
        bot.send_message(uid, "План: 1) короткий разбор ошибки, 2) фокус недели, 3) скелет ТС. С чего начнём?", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])
        set_awaiting(uid, st, False)
    else:
        bot.send_message(uid, "Ок. Если хочешь ускориться — нажми «🚑 У меня ошибка».", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])
        set_awaiting(uid, st, False)

# ========= Callbacks =========
@bot.callback_query_handler(func=lambda call: True)
def on_callback(call: types.CallbackQuery):
    uid = call.from_user.id
    data = call.data or ""
    bot.answer_callback_query(call.id, "Ок")

    st = load_state(uid)

    if data == "start_error_flow":
        st["data"]["problem_confirmed"] = True
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        bot.send_message(uid, "Начнём разбор. Опиши последний случай: что планировал и где отступил?")
        set_awaiting(uid, st, True)

    elif data == "skip_error_flow":
        bot.send_message(uid, "Хорошо. Вернёмся когда будет удобно.", reply_markup=MAIN_MENU)
        set_awaiting(uid, st, False)

    elif data == "confirm_problem":
        st["data"]["problem_confirmed"] = True
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        bot.send_message(uid, "Отлично. Тогда берём последний случай. Что планировал и где отступил?")
        set_awaiting(uid, st, True)

    elif data == "refine_problem":
        save_state(uid, INTENT_ERR, STEP_ERR_CONFIRM, st["data"])
        bot.send_message(uid, "Сформулируй своими словами коротко — как это звучит для тебя?")
        set_awaiting(uid, st, True)

    elif data == "resume_flow":
        # просто продолжаем с текущего шага
        bot.send_message(uid, "Продолжаем с того места, где остановились.")
        step = st["step"]
        # подсказка исходя из шага
        if step in MER_ORDER:
            bot.send_message(uid, mer_prompt_for(step))
        elif step == STEP_ERR_DESCR:
            bot.send_message(uid, "Опиши последний случай: что планировал и где отступил?")
        elif step == STEP_GOAL:
            bot.send_message(uid, "Сформулируй позитивную цель: что делаешь вместо прежнего поведения?")
        elif step == STEP_TOTE_OPS:
            bot.send_message(uid, "Назови 2–3 конкретных шага (коротко, списком).")
        elif step == STEP_TOTE_TEST:
            bot.send_message(uid, "Как поймёшь, что получилось? Один простой критерий.")
        elif step == STEP_TOTE_EXIT:
            bot.send_message(uid, "Что сделаешь, если проверка покажет «не получилось»?")
        set_awaiting(uid, st, True)

    elif data == "new_flow":
        st["data"].pop("mer", None)
        st["data"].pop("tote", None)
        st["data"].pop("error_description", None)
        st["data"]["problem_confirmed"] = False
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        bot.send_message(uid, "Окей, начнём заново. Опиши последний случай: что планировал и где отступил?")
        set_awaiting(uid, st, True)

# ========= HTTP =========
@app.get("/")
def root():
    return jsonify({"ok": True, "time": _now_iso()})

@app.get("/version")
def version_api():
    return jsonify({"version": BOT_VERSION, "code_hash": get_code_version(), "status": "running", "timestamp": _now_iso()})

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

# ========= Maintenance & gentle reminders =========
def cleanup_old_states(days: int = 30):
    try:
        # ИСПРАВЛЕННЫЙ ЗАПРОС - убрал make_interval
        result = db_exec(
            "DELETE FROM user_state WHERE updated_at < NOW() - INTERVAL '1 day' * :days", 
            {"days": days}
        )
        log.info("Old user states cleanup done (> %s days). Deleted: %s", days, result.rowcount)
    except Exception as e:
        log.error("Cleanup error: %s", e)

def reminder_tick():
    """Каждую минуту: если ждём ответ и тишина > REMIND_AFTER_MIN — мягкий пинг (не чаще REMIND_REPEAT_MIN)."""
    while True:
        try:
            # ИСПРАВЛЕННЫЙ ЗАПРОС - убрал make_interval, добавил безопасный LIKE
            rows = db_exec("""
                SELECT user_id, intent, step, data
                FROM user_state
                WHERE data::text LIKE '%' || :search_term || '%'
                  AND updated_at < NOW() - INTERVAL '1 minute' * :mins
            """, {
                "search_term": '"awaiting_reply": true', 
                "mins": REMIND_AFTER_MIN
            }).mappings().all()
            
            now = datetime.now(timezone.utc)
            reminder_count = 0
            
            for r in rows:
                try:
                    data = json.loads(r["data"]) if r["data"] else {}
                except Exception:
                    data = {}
                    
                last_ping = _iso_to_dt(data.get("reminder_sent_at"))
                if last_ping and (now - last_ping) < timedelta(minutes=REMIND_REPEAT_MIN):
                    continue
                    
                # отправим мягкий пинг
                try:
                    bot.send_message(r["user_id"], "Как будешь готов — продолжим. Могу повторить вопрос.")
                    data["reminder_sent_at"] = _now_iso()
                    save_state(r["user_id"], data=data)
                    reminder_count += 1
                except Exception as e:
                    log.error(f"Failed to send reminder to {r['user_id']}: {e}")
                    
            if reminder_count > 0:
                log.info(f"Sent {reminder_count} reminders")
                
        except Exception as e:
            log.error(f"Reminder error: {e}")
        time.sleep(60)

def cleanup_scheduler():
    while True:
        time.sleep(24 * 60 * 60)  # 24 hours
        cleanup_old_states(30)

# ========= Init on import (for gunicorn) =========
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
            secret_token=TG_SECRET,
            allowed_updates=["message", "callback_query"]
        )
        log.info("Webhook set to %s/%s", PUBLIC_URL, WEBHOOK_PATH)
    except Exception as e:
        log.error("Webhook setup error: %s", e)

# фоновая чистка и напоминатель
try:
    threading.Thread(target=cleanup_scheduler, daemon=True).start()
    threading.Thread(target=reminder_tick, daemon=True).start()
    log.info("Background threads started")
except Exception as e:
    log.error("Can't start background threads: %s", e)

# ========= Dev run =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)