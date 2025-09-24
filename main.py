# main.py — Innertrade Kai Mentor Bot
# Версия: 2025-09-24 (coach-struct v7.2 - calibrated-first)

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

BOT_VERSION = f"2025-09-24-{get_code_version()}"

# ========= ENV =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "webhook").strip()
TG_SECRET = os.getenv("TG_WEBHOOK_SECRET", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()  # ожидаем postgresql+psycopg://... sslmode=require
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OFFSCRIPT_ENABLED = os.getenv("OFFSCRIPT_ENABLED", "true").lower() == "true"
SET_WEBHOOK_FLAG = os.getenv("SET_WEBHOOK", "true").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_BODY = int(os.getenv("MAX_BODY", "1000000"))
HIST_LIMIT = 12

# напоминалки (диалог продолжить/начать заново)
IDLE_REMINDER_MIN = int(os.getenv("IDLE_REMINDER_MIN", "60"))   # через сколько минут бездействия напомнить
SESSION_CONTINUE_THRESHOLD_MIN = int(os.getenv("SESSION_CONTINUE_THRESHOLD_MIN", "60"))  # если спустя N минут пришло новое сообщение — спросить «продолжить?»

# критичные ENV
for var in ("TELEGRAM_TOKEN", "PUBLIC_URL", "WEBHOOK_PATH", "TG_WEBHOOK_SECRET", "DATABASE_URL"):
    if not globals().get(var):
        raise RuntimeError(f"{var} is required")

# ========= Logging =========
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("kai-mentor")
log.info(f"Запуск бота версия: {BOT_VERSION}")

# ========= Intents/Steps =========
INTENT_GREET = "greet"
INTENT_FREE = "free"
INTENT_ERR  = "error"

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
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=3
        )
        openai_status = "active"
        log.info("OpenAI готов")
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
        log.info("DB initialized")
    except Exception as e:
        log.error("Инициализация базы данных не удалась: %s", e)
        raise

# ========= State =========
def default_data():
    return {
        "history": [],
        "style": "ты",
        "calibrated": {
            "problem_text": "",
            "signals": {"trigger": "", "action": "", "cost": ""},
            "rounds": 0
        },
        "problem_confirmed": False,
        "awaiting_reply": False,
        "last_user_msg_at": datetime.now(timezone.utc).isoformat()
    }

def load_state(uid: int) -> Dict[str, Any]:
    row = db_exec("SELECT intent, step, data FROM user_state WHERE user_id=:uid", {"uid": uid}).mappings().first()
    if row:
        data = default_data()
        if row["data"]:
            try:
                payload = json.loads(row["data"])
                # мягкое слияние с дефолтами на случай старых записей
                data.update({k: payload.get(k, data[k]) for k in data.keys()})
                # глубже — calibrated/signals
                if "calibrated" in payload:
                    data["calibrated"].update(payload.get("calibrated", {}))
                    if "signals" in payload.get("calibrated", {}):
                        data["calibrated"]["signals"].update(payload["calibrated"]["signals"])
            except Exception as e:
                log.error("Failed to parse user data: %s", e)
        return {"user_id": uid, "intent": row["intent"] or INTENT_GREET, "step": row["step"] or STEP_ASK_STYLE, "data": data}
    return {"user_id": uid, "intent": INTENT_GREET, "step": STEP_ASK_STYLE, "data": default_data()}

def save_state(uid: int, intent=None, step=None, data=None) -> Dict[str, Any]:
    cur = load_state(uid)
    intent = intent or cur["intent"]
    step = step or cur["step"]
    new_data = cur["data"].copy()
    if data:
        # глубокое слияние для calibrated/signals
        if "calibrated" in data:
            new_cal = new_data.get("calibrated", {}).copy()
            inc = data["calibrated"]
            if "signals" in inc:
                sig = new_cal.get("signals", {}).copy()
                sig.update({k: v for k, v in inc["signals"].items() if v})
                inc = {**inc, "signals": sig}
            new_cal.update({k: v for k, v in inc.items() if k != "signals"})
            new_data["calibrated"] = new_cal
            data = {**data, "calibrated": new_cal}
        new_data.update({k: v for k, v in data.items() if k != "calibrated"})
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

def detect_trading_patterns(text: str) -> List[str]:
    tl = (text or "").lower()
    hits = []
    for name, keys in {**RISK_PATTERNS, **EMO_PATTERNS}.items():
        if any(k in tl for k in keys):
            hits.append(name)
    return hits

# ========= Helpers =========
BAN_TEMPLATES = [
    "понимаю", "это может быть", "важно понять", "давай рассмотрим", "было бы полезно",
    "попробуй", "используй", "придерживайся", "установи", "сфокусируйся", "следуй", "пересмотри"
]

def strip_templates(text_in: str) -> str:
    t = text_in or ""
    for ph in BAN_TEMPLATES:
        t = re.sub(rf"(?i)\b{re.escape(ph)}[^.!?]*[.!?]", " ", t)
    t = re.sub(r'\s+', ' ', t).strip(" ,.!?") or text_in
    return t

def anti_echo(user_text: str, model_text: str) -> str:
    u = (user_text or "").strip().lower()
    m = (model_text or "").strip()
    if len(u) >= 15 and len(m) >= 15 and SequenceMatcher(None, u, m.lower()).ratio() > 0.7:
        return "Скажу иначе: " + m
    return m

def is_calibration_complete(d: Dict[str, Any]) -> bool:
    cal = (d or {}).get("calibrated", {})
    s = (cal.get("signals") or {})
    return bool(cal.get("problem_text")) \
        and all(s.get(k) for k in ("trigger", "action", "cost")) \
        and cal.get("rounds", 0) >= 2 \
        and bool(d.get("problem_confirmed"))

def soft_bridge(text: str) -> str:
    return f"{text}\n\nЕсли захочешь начать с чистого листа — напиши: <b>новый разбор</b>."

# ========= Summaries =========
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
    return "Вижу такие сигналы: " + (", ".join(parts) if parts else "нужно ещё пару деталей")

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
        return getattr(tr, "text", None)
    except Exception as e:
        log.error("Whisper error: %s", e)
        return None

# ========= GPT — коуч-калибровщик =========
def gpt_decide(uid: int, text_in: str, st: Dict[str, Any]) -> Dict[str, Any]:
    """
    Задача: НЕ давать советы. Вытянуть 3 сигнала (trigger/action/cost),
    собрать короткую формулировку проблемы и дойти до подтверждения.
    """
    fallback = {
        "mode": "calibration",
        "next_step": st["step"],
        "intent": st["intent"],
        "response_text": "Схвачу суть точнее. Что было триггером: конкретный момент/сигнал рынка?",
        "store": {},
        "is_structural": False,
        "ready_to_confirm": False
    }
    if not oai_client or not OFFSCRIPT_ENABLED:
        return fallback

    style = st["data"].get("style", "ты")
    cal = st["data"].get("calibrated", {})
    cur_problem = cal.get("problem_text", "")

    system_prompt = f"""
Ты — коуч Алекс. Говоришь коротко и по-человечески, без советов.
Цель сейчас — калибровка (а не техника):
1) вытащить три сигнала: trigger (что запускает), action (что делаешь), cost (чем это обходится);
2) собрать одну короткую формулировку проблемы (1 предложение, без общих слов);
3) если картина ясна — мягко предложить подтвердить формулировку.

Запрещено: списки советов, клише, метод-термины “Мерседес/ТOTE”.
Формат ответа (JSON):
{{
  "mode": "calibration|structure",
  "response_text": "короткий ответ на «{style}»",
  "store": {{
     "calibrated_delta": {{
        "problem_text": "<или пусто, если рано>",
        "signals": {{"trigger":"...", "action":"...", "cost":"..."}},
        "rounds_inc": 1
     }},
     "ready_to_confirm": true|false
  }},
  "is_structural": false
}}
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
        resp = strip_templates(anti_echo(text_in, dec.get("response_text", ""))) or fallback["response_text"]

        # Строгий фильтр на «советы»
        if any(b in resp.lower() for b in ["попробуй", "придерживайся", "используй", "установи", "следуй", "пересмотри"]):
            resp = "Хочу точнее зафиксировать момент. Что именно тебя обычно толкает к этому действию?"

        dec["response_text"] = resp
        dec.setdefault("store", {})
        return dec
    except Exception as e:
        log.error("GPT decision error: %s", e)
        return fallback

# ========= UI helpers =========
def kb_confirm_problem() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("Да, об этом", callback_data="confirm_problem"),
        types.InlineKeyboardButton("Не совсем", callback_data="refine_problem")
    )
    return kb

def kb_start_error_flow() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("Разобрать по шагам", callback_data="start_error_flow"),
        types.InlineKeyboardButton("Пока нет", callback_data="skip_error_flow")
    )
    return kb

# ========= Handlers =========
@bot.message_handler(commands=["start", "reset"])
def cmd_start(m: types.Message):
    st = save_state(m.from_user.id, INTENT_GREET, STEP_ASK_STYLE, {"history": [], "problem_confirmed": False, "calibrated": default_data()["calibrated"]})
    bot.send_message(
        m.from_user.id,
        "👋 Привет! Как удобнее — <b>ты</b> или <b>вы</b>?\n\nЕсли захочешь начать чистый лист, просто напиши: <b>новый разбор</b>.",
        reply_markup=STYLE_KB
    )

@bot.message_handler(commands=["version", "v"])
def cmd_version(m: types.Message):
    info = f"🔄 Версия бота: {BOT_VERSION}\n📝 Хэш кода: {get_code_version()}\n🕒 Время сервера: {datetime.now(timezone.utc).isoformat()}\n🤖 OpenAI: {openai_status}"
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
    handle_text_message(m.from_user.id, (m.text or "").strip(), m)

def handle_text_message(uid: int, text_in: str, original_message=None):
    st = load_state(uid)

    # сброс по кодовой фразе
    if text_in.lower() in ("новый разбор", "новый разбор.", "новый разбор!", "начать заново"):
        st["data"]["problem_confirmed"] = False
        st["data"]["calibrated"] = default_data()["calibrated"]
        st["data"]["history"] = st["data"].get("history", [])[-(HIST_LIMIT-2):]
        st["intent"], st["step"] = INTENT_FREE, STEP_FREE_INTRO
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
        bot.send_message(uid, "Окей, начнём с чистого листа. Опиши последний случай: что планировал и где отступил?")
        return

    # history (user)
    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "user", "content": text_in})
    st["data"]["history"] = history
    st["data"]["last_user_msg_at"] = datetime.now(timezone.utc).isoformat()

    # Greeting: выбор стиля
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if text_in.lower() in ("ты", "вы"):
            st["data"]["style"] = text_in.lower()
            save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
            bot.send_message(uid, f"Принято ({text_in}). Что сейчас в трейдинге хочешь поправить?", reply_markup=MAIN_MENU)
        else:
            save_state(uid, data=st["data"])
            bot.send_message(uid, "Выбери «ты» или «вы».", reply_markup=STYLE_KB)
        return

    # Если на паузе пришёл новый месседж спустя X минут — уточняем продолжение
    try:
        last_at = datetime.fromisoformat(st["data"].get("last_user_msg_at"))
    except Exception:
        last_at = datetime.now(timezone.utc)
    delta_min = (datetime.now(timezone.utc) - last_at).total_seconds() / 60.0
    if delta_min >= SESSION_CONTINUE_THRESHOLD_MIN and st["intent"] != INTENT_ERR:
        kb = types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Продолжим", callback_data="continue_session"),
            types.InlineKeyboardButton("Новый разбор", callback_data="new_session")
        )
        save_state(uid, data=st["data"])
        bot.send_message(uid, "Похоже, мы прерывались. Продолжим или начнём заново?", reply_markup=kb)

    # Structural flow (если уже внутри техники)
    if st["intent"] == INTENT_ERR:
        handle_structural(uid, text_in, st)
        return

    # ===== Свободный режим — КАЛИБРОВКА через GPT =====
    decision = gpt_decide(uid, text_in, st)
    resp = decision.get("response_text") or "Давай зафиксируем триггер — что именно запускает это поведение?"
    resp = soft_bridge(resp)  # добавляем мягкий хвост «про новый разбор»

    # применяем deltas
    merged = st["data"].copy()
    store = decision.get("store", {})
    cal_delta = (store or {}).get("calibrated_delta") or {}
    if cal_delta:
        cal = merged.get("calibrated", {})
        sig = cal.get("signals", {})
        # обновляем сигналы точечно
        for k in ("trigger", "action", "cost"):
            v = (cal_delta.get("signals") or {}).get(k)
            if v:
                sig[k] = v
        cal["signals"] = sig
        # проблем-текст, если предложен
        if cal_delta.get("problem_text"):
            cal["problem_text"] = cal_delta["problem_text"]
        # инкремент раундов
        cal["rounds"] = int(cal.get("rounds", 0)) + int(cal_delta.get("rounds_inc", 0) or 0)
        merged["calibrated"] = cal

    # история (assistant)
    history = merged.get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "assistant", "content": decision.get("response_text", resp)})
    merged["history"] = history

    # фиксируем
    st_after = save_state(uid, INTENT_FREE, STEP_FREE_INTRO, merged)

    # показываем резюме на согласование, если готово
    cal = st_after["data"].get("calibrated", {})
    signals = cal.get("signals", {})
    ready_to_confirm = store.get("ready_to_confirm", False)
    if ready_to_confirm and all(signals.get(k) for k in ("trigger", "action", "cost")) and cal.get("problem_text"):
        bot.send_message(
            uid,
            f"Зафиксирую так:\n\n<b>{cal['problem_text']}</b>\n\nТриггер: {signals.get('trigger')}\nДействие: {signals.get('action')}\nЦена: {signals.get('cost')}\n\nВерно?",
            reply_markup=kb_confirm_problem()
        )
        return

    # иначе обычный ответ
    if original_message:
        bot.reply_to(original_message, resp, reply_markup=MAIN_MENU)
    else:
        bot.send_message(uid, resp, reply_markup=MAIN_MENU)

# ========= Structural Flow =========
def mer_prompt_for(step: str) -> str:
    return {
        STEP_MER_CTX: "Окей, коротко контекст: где/когда это было?",
        STEP_MER_EMO: "Поймаю тоньше. Что чувствовал в моменте (2–3 слова)?",
        STEP_MER_THO: "Какие фразы мелькали в голове (2–3 коротких)?",
        STEP_MER_BEH: "Что сделал фактически? Пошагово, но коротко."
    }.get(step, "Продолжим.")

def handle_structural(uid: int, text_in: str, st: Dict[str, Any]):
    # мягкий перехват, если внезапно стало ясно, что проблема другая
    if any(k in text_in.lower() for k in ["на самом деле", "скорее проблема", "понял, что дело"]):
        d = st["data"]
        d["problem_confirmed"] = False
        d["calibrated"]["problem_text"] = ""
        d["calibrated"]["signals"] = {"trigger": "", "action": "", "cost": ""}
        d["calibrated"]["rounds"] = 0
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, d)
        bot.send_message(uid, "Вижу, картина поменялась. Давай уточним формулировку и согласуем её.")
        return

    # a) описание ошибки (последний кейс)
    if st["step"] == STEP_ERR_DESCR:
        new_data = st["data"].copy()
        new_data["error_description"] = text_in
        save_state(uid, INTENT_ERR, STEP_MER_CTX, new_data)
        bot.send_message(uid, "Понял. Перейдём коротко по шагам, я рядом.", reply_markup=MAIN_MENU)
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
            save_state(uid, INTENT_ERR, nxt, new_data)
            bot.send_message(uid, mer_prompt_for(nxt))
        else:
            save_state(uid, INTENT_ERR, STEP_GOAL, new_data)
            bot.send_message(uid, "Окей. Теперь сформулируй позитивно: что делаешь вместо прежнего поведения?")
        return

    # c) Goal
    if st["step"] == STEP_GOAL:
        new_data = st["data"].copy()
        new_data["goal"] = text_in
        save_state(uid, INTENT_ERR, STEP_TOTE_OPS, new_data)
        bot.send_message(uid, "Супер. Назови 2–3 конкретных шага для ближайших 3 сделок (коротко, списком).")
        return

    # d) TOTE - ops
    if st["step"] == STEP_TOTE_OPS:
        tote = st["data"].get("tote", {})
        tote["ops"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_TEST, new_data)
        bot.send_message(uid, "Как поймёшь, что получилось? Один простой критерий.")
        return

    # e) TOTE - test
    if st["step"] == STEP_TOTE_TEST:
        tote = st["data"].get("tote", {})
        tote["test"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_EXIT, new_data)
        bot.send_message(uid, "И последнее: что сделаешь, если проверка покажет «не получилось»?")
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
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, new_data)
        bot.send_message(uid, "\n".join(summary), reply_markup=MAIN_MENU)
        bot.send_message(uid, "Сохранить это в фокус недели или двигаемся дальше?")

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
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        bot.send_message(uid, "Начнём с конкретики. Опиши последний случай: что планировал и где отступил?")
    elif code == "start_help":
        bot.send_message(uid, "План: 1) короткая калибровка, 2) фокус недели, 3) скелет ТС. С чего начнём?", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])
    else:
        bot.send_message(uid, "Ок. Если хочешь ускориться — нажми «🚑 У меня ошибка».", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])

# ========= Callbacks =========
@bot.callback_query_handler(func=lambda call: True)
def on_callback(call: types.CallbackQuery):
    uid = call.from_user.id
    data = call.data or ""
    bot.answer_callback_query(call.id, "Ок")

    if data == "confirm_problem":
        st = load_state(uid)
        st["data"]["problem_confirmed"] = True
        save_state(uid, data=st["data"])
        # Мягкий мостик + кнопка начать разбор
        bot.send_message(uid, "Окей, возьмём это как рабочую формулировку. Перейдём к разбору пошагово?", reply_markup=kb_start_error_flow())

    elif data == "refine_problem":
        bot.send_message(uid, "Где поправим формулировку? Что бы ты заменил или убрал?")
        st = load_state(uid)
        st["data"]["problem_confirmed"] = False
        st["data"]["calibrated"]["rounds"] = max(0, int(st["data"]["calibrated"].get("rounds", 0)) - 1)
        save_state(uid, data=st["data"])

    elif data == "start_error_flow":
        st = load_state(uid)
        # если уже есть последнее описание — можно сразу в MER_CTX, иначе начнём с ERR_DESCR
        step = STEP_ERR_DESCR if not st["data"].get("error_description") else STEP_MER_CTX
        save_state(uid, INTENT_ERR, step, st["data"])
        if step == STEP_ERR_DESCR:
            bot.send_message(uid, "Опиши последний случай: вход/план, где отступил, чем закончилось.")
        else:
            bot.send_message(uid, "Перейдём по шагам. Коротко и по делу.")
            bot.send_message(uid, mer_prompt_for(STEP_MER_CTX))

    elif data == "skip_error_flow":
        bot.send_message(uid, "Хорошо. Вернёмся, когда захочешь.", reply_markup=MAIN_MENU)

    elif data == "continue_session":
        bot.send_message(uid, "Окей, продолжаем. Где остановились?")
    elif data == "new_session":
        st = load_state(uid)
        st["data"]["problem_confirmed"] = False
        st["data"]["calibrated"] = default_data()["calibrated"]
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
        bot.send_message(uid, "Начнём с чистого листа. Опиши последний случай: что планировал и где отступил?")

# ========= HTTP =========
def _now_iso():
    return datetime.now(timezone.utc).isoformat()

@app.get("/")
def root():
    return jsonify({"ok": True, "time": _now_iso()})

@app.get("/version")
def version_api():
    return jsonify({"version": BOT_VERSION, "code_hash": get_code_version(), "status": "running", "timestamp": _now_iso(), "openai": openai_status})

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

# ========= Maintenance (optional reminders) =========
def cleanup_old_states(days: int = 30):
    try:
        # безопасная интервал-подстановка
        db_exec("DELETE FROM user_state WHERE updated_at < NOW() - (:ival)::interval",
                {"ival": f"{int(days)} days"})
        log.info("Old user states cleanup done (> %s days).", days)
    except Exception as e:
        log.error("Cleanup error: %s", e)

def reminder_loop():
    """Напоминание в чате, если пользователь «завис» во время свободного диалога."""
    while True:
        try:
            # выбираем тех, кто ждет ответа и «старше» X минут
            rows = db_exec("""
                SELECT user_id, intent, step, data
                FROM user_state
                WHERE (data LIKE '%%"awaiting_reply": true%%' OR intent = :intent)
                  AND updated_at < NOW() - (:ival)::interval
                LIMIT 50
            """, {"ival": f"{IDLE_REMINDER_MIN} minutes", "intent": INTENT_FREE}).mappings().all()
            for r in rows:
                try:
                    d = json.loads(r["data"])
                except Exception:
                    d = {}
                # мягкое напоминание
                try:
                    kb = types.InlineKeyboardMarkup().row(
                        types.InlineKeyboardButton("Продолжим", callback_data="continue_session"),
                        types.InlineKeyboardButton("Новый разбор", callback_data="new_session")
                    )
                    bot.send_message(r["user_id"], "Как будешь готов — продолжим. Продолжим сейчас или начнём заново?", reply_markup=kb)
                    # сбросим awaiting_reply
                    d["awaiting_reply"] = False
                    save_state(r["user_id"], data=d)
                except Exception as e:
                    log.error("Reminder send error: %s", e)
        except Exception as e:
            log.error("Reminder query error: %s", e)
        time.sleep(60)

# ========= Init on import =========
try:
    init_db()
    log.info("DB initialized (import)")
except Exception as e:
    log.error("DB init (import) сбой: %s", e)

if SET_WEBHOOK_FLAG:
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(
            url=f"{PUBLIC_URL}/{WEBHOOK_PATH}",
            secret_token=TG_SECRET,
            allowed_updates=["message", "callback_query"]
        )
        log.info("Webhook установлен на %s/%s", PUBLIC_URL, WEBHOOK_PATH)
    except Exception as e:
        log.error("Webhook setup error: %s", e)

try:
    threading.Thread(target=cleanup_old_states, args=(30,), daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()
except Exception as e:
    log.error("Не удалось запустить фоновые задачи: %s", e)

# ========= Dev run =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
