# main.py — Innertrade Kai Mentor Bot
# Версия: 2025-10-18 (coach-struct v9.0, "тонкий дирижёр")
# Ключевые идеи:
#  - Ведущая роль у ChatGPT: калибровка, живой диалог, глубина.
#  - Оркестратор (код) следит за UX: один ход = один вопрос/действие;
#    никакого "двойного" сообщения (вопрос + кнопки + резюме в одном).
#  - Техника (MERCEDES/TOTE) только после подтверждения проблемы
#    или явных риск-паттернов, и только отдельным ходом.
#  - НЕТ жёсткого счётчика "после 3-го шага" — модель сама ведёт,
#    код лишь страхует от слишком раннего перехода.
#  - Совместимость: Flask 3.x, TeleBot, SQLAlchemy 2.x, psycopg3, OpenAI 1.108.x

import os
import json
import time
import threading
import logging
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

# ========= Version / Hash =========
def _code_hash() -> str:
    try:
        with open(__file__, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except Exception:
        return "unknown"

BOT_VERSION = f"2025-10-18-{_code_hash()}"

# ========= ENV =========
def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

TELEGRAM_TOKEN = _env("TELEGRAM_TOKEN")
PUBLIC_URL     = _env("PUBLIC_URL")
WEBHOOK_PATH   = _env("WEBHOOK_PATH", "webhook")
TG_SECRET      = _env("TG_WEBHOOK_SECRET")
DATABASE_URL   = _env("DATABASE_URL")
OPENAI_API_KEY = _env("OPENAI_API_KEY")
OPENAI_MODEL   = _env("OPENAI_MODEL", "gpt-4o-mini")

OFFSCRIPT_ENABLED = _env("OFFSCRIPT_ENABLED", "true").lower() == "true"
SET_WEBHOOK_FLAG  = _env("SET_WEBHOOK", "true").lower() == "true"
LOG_LEVEL         = _env("LOG_LEVEL", "INFO").upper()
MAX_BODY          = int(_env("MAX_BODY", "1000000"))

# Напоминания и idle
REMINDERS_ENABLED   = _env("REMINDERS_ENABLED", "true").lower() == "true"
IDLE_MINUTES_REMIND = int(_env("IDLE_MINUTES_REMIND", "60"))
IDLE_MINUTES_RESET  = int(_env("IDLE_MINUTES_RESET", "240"))

# Храним последние N реплик (короткий контекст)
HIST_LIMIT = 18

# ========= Guards =========
_missing = [k for k in
            ["TELEGRAM_TOKEN", "PUBLIC_URL", "WEBHOOK_PATH", "TG_WEBHOOK_SECRET", "DATABASE_URL", "OPENAI_API_KEY"]
            if not globals()[k]]
if _missing:
    raise RuntimeError(f"ENV variables missing: {', '.join(_missing)}")

# ========= Logging =========
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("kai-mentor")
log.info(f"Starting bot version: {BOT_VERSION}")

# ========= Intents/Steps =========
INTENT_GREET = "greet"
INTENT_FREE  = "free"       # коуч-слой, живой диалог
INTENT_ERR   = "error"      # структурный разбор
INTENT_DONE  = "done"

STEP_ASK_STYLE  = "ask_style"
STEP_FREE_CHAT  = "free_chat"       # живая калибровка
STEP_ERR_DESCR  = "err_describe"    # «опиши последний кейс»
STEP_MER_CTX    = "mer_context"
STEP_MER_EMO    = "mer_emotions"
STEP_MER_THO    = "mer_thoughts"
STEP_MER_BEH    = "mer_behavior"
STEP_GOAL       = "goal_positive"
STEP_TOTE_OPS   = "tote_ops"
STEP_TOTE_TEST  = "tote_test"
STEP_TOTE_EXIT  = "tote_exit"

MER_ORDER = [STEP_MER_CTX, STEP_MER_EMO, STEP_MER_THO, STEP_MER_BEH]

# ========= Risk/Emotion patterns (для страховки раннего перехода) =========
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
}

def detect_patterns(text_in: str) -> List[str]:
    tl = (text_in or "").lower()
    hits = []
    for name, keys in {**RISK_PATTERNS, **EMO_PATTERNS}.items():
        if any(k in tl for k in keys):
            hits.append(name)
    return hits

def risky(text_in: str) -> bool:
    pats = set(detect_patterns(text_in))
    return bool(pats & set(RISK_PATTERNS.keys())) or ("fear_of_loss" in pats) or ("self_doubt" in pats)

# ========= OpenAI =========
oai_client: Optional[OpenAI] = None
openai_status = "disabled"
if OPENAI_API_KEY and OFFSCRIPT_ENABLED:
    try:
        oai_client = OpenAI(api_key=OPENAI_API_KEY)
        # Лёгкая проверка
        oai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=4,
        )
        openai_status = "active"
        log.info("OpenAI ready")
    except Exception as e:
        log.error(f"OpenAI init error: {e}")
        openai_status = f"error: {e}"
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

# ========= State helpers =========
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_state(uid: int) -> Dict[str, Any]:
    row = db_exec("SELECT intent, step, data FROM user_state WHERE user_id=:uid", {"uid": uid}).mappings().first()
    if row:
        data = {}
        if row["data"]:
            try:
                data = json.loads(row["data"])
            except Exception as e:
                log.error("parse user data error: %s", e)
                data = {}
        data.setdefault("history", [])
        return {"user_id": uid, "intent": row["intent"] or INTENT_GREET, "step": row["step"] or STEP_ASK_STYLE, "data": data}
    return {"user_id": uid, "intent": INTENT_GREET, "step": STEP_ASK_STYLE, "data": {"history": []}}

def save_state(uid: int, intent: Optional[str] = None, step: Optional[str] = None, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cur = load_state(uid)
    intent = intent or cur["intent"]
    step   = step   or cur["step"]
    new_data = cur["data"].copy()
    if data:
        new_data.update(data)
    new_data["last_state_write_at"] = _now_iso()
    db_exec("""
        INSERT INTO user_state (user_id, intent, step, data, updated_at)
        VALUES (:uid, :intent, :step, :data, now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent=EXCLUDED.intent, step=EXCLUDED.step, data=EXCLUDED.data, updated_at=now()
    """, {"uid": uid, "intent": intent, "step": step, "data": json.dumps(new_data, ensure_ascii=False)})
    return {"user_id": uid, "intent": intent, "step": step, "data": new_data}

def _append_history(data: Dict[str, Any], role: str, content: str) -> Dict[str, Any]:
    hist = data.get("history", [])
    if len(hist) >= HIST_LIMIT:
        hist = hist[-(HIST_LIMIT - 1):]
    hist.append({"role": role, "content": content})
    data["history"] = hist
    return data

# ========= Flask/TeleBot =========
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)

MAIN_MENU = types.ReplyKeyboardMarkup(resize_keyboard=True)
MAIN_MENU.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
MAIN_MENU.row("📄 Паспорт", "🗒 Панель недели")
MAIN_MENU.row("🆘 Экстренно", "🤔 Не знаю, с чего начать")

STYLE_KB = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
STYLE_KB.row("ты", "вы")

# ========= GPT: коуч-слой (единый вопрос за ход) =========
def gpt_calibrate(uid: int, text_in: str, st: Dict[str, Any]) -> Dict[str, Any]:
    """
    Возвращает JSON:
      response_text: короткий, человечный, 1 вопрос, без советов
      store: dict (что положить в память)
      summary_draft: короткое резюме проблемы ИЛИ ""
      readiness_score: float 0..1 (готовность к структуре)
      ask_confirm: bool (попросить подтверждение резюме)
    """
    fallback = {
        "response_text": "Окей. Чтобы не спешить, скажи коротко: где именно начинает уводить от плана — вход, стоп или выход?",
        "store": {},
        "summary_draft": "",
        "readiness_score": 0.0,
        "ask_confirm": False,
    }
    if not oai_client or not OFFSCRIPT_ENABLED:
        return fallback

    style = st["data"].get("style", "ты")
    history = st["data"].get("history", [])

    system = f"""
Ты — Алекс, коуч-наставник. Говоришь на «{style}», просто и по-человечески.
Задача: углубляться в контекст короткими вопросами (ОДИН вопрос за ход), аккуратно
подводить к чёткому резюме проблемы. Никаких советов и списков техник на этапе калибровки.
Техники (и вообще слово "техника") не упоминай. Сначала: калибровка → резюме → подтверждение.
Когда уверен, что человек четко назвал проблему — readiness_score ближе к 1.0.
Если уже можно — дай краткое summary_draft (1–2 строки) и ask_confirm=true.
Ответ — строго JSON: response_text, store, summary_draft, readiness_score, ask_confirm.
""".strip()

    msgs = [{"role": "system", "content": system}]
    for h in history[-HIST_LIMIT:]:
        if h.get("role") in ("user", "assistant"):
            msgs.append(h)
    msgs.append({"role": "user", "content": text_in})

    try:
        res = oai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=msgs,
            temperature=0.3,
            response_format={"type":"json_object"},
        )
        raw = res.choices[0].message.content or "{}"
        js = json.loads(raw)
        # sanity
        for k in ["response_text","store","summary_draft","readiness_score","ask_confirm"]:
            if k not in js:
                return fallback
        # safety: ровно один вопрос (не более 1 '?')
        rt = (js.get("response_text") or "").strip()
        if rt.count("?") > 1:
            # оставим первую вопросительную часть
            first = rt.split("?")[0].strip()
            rt = first + "?"
        if len(rt) < 6:
            rt = fallback["response_text"]
        js["response_text"] = rt[:900]  # коротко
        # калибровочный "store"
        if not isinstance(js.get("store"), dict):
            js["store"] = {}
        # readiness в 0..1
        try:
            r = float(js.get("readiness_score", 0))
            js["readiness_score"] = max(0.0, min(1.0, r))
        except Exception:
            js["readiness_score"] = 0.0
        return js
    except Exception as e:
        log.error("gpt_calibrate error: %s", e)
        return fallback

def extract_summary_from_memory(data: Dict[str, Any]) -> str:
    # Небольшой авто-резюм на случай, если модель ничего не дала
    user_msgs = [m["content"] for m in data.get("history", []) if m.get("role") == "user"]
    pats = []
    for m in user_msgs:
        pats.extend(detect_patterns(m))
    parts = []
    s = set(pats)
    if "fomo" in s: parts.append("FOMO / страх упустить")
    if "remove_stop" in s or "move_stop" in s: parts.append("трогаешь/снимаешь стоп")
    if "early_close" in s: parts.append("ранний выход")
    if "averaging" in s: parts.append("усреднение против позиции")
    if "fear_of_loss" in s: parts.append("страх потерь/стопа")
    if "self_doubt" in s: parts.append("сомнения после входа")
    if not parts:
        return ""
    return "Похоже на: " + ", ".join(parts)

# ========= Structural prompts =========
def mer_prompt_for(step: str) -> str:
    return {
        STEP_MER_CTX: "Зафиксируем картинку. Где и когда это было? Коротко.",
        STEP_MER_EMO: "Что почувствовал в моменте (2–3 слова)?",
        STEP_MER_THO: "Какие мысли мелькали (2–3 коротких фразы)?",
        STEP_MER_BEH: "Что сделал фактически? Действия.",
    }.get(step, "Продолжим.")

# ========= Handlers =========
@bot.message_handler(commands=["start", "reset"])
def cmd_start(m: types.Message):
    uid = m.from_user.id
    st = load_state(uid)
    # Начинаем свежо
    st = save_state(uid, INTENT_GREET, STEP_ASK_STYLE, {"history": []})
    bot.send_message(uid,
        "👋 Привет! Как удобнее — <b>ты</b> или <b>вы</b>?\n\nЕсли захочешь начать с чистого листа — напиши: <b>новый разбор</b>.",
        reply_markup=STYLE_KB
    )

@bot.message_handler(commands=["version","v"])
def cmd_version(m: types.Message):
    bot.reply_to(m, (
        f"🔄 Версия бота: {BOT_VERSION}\n"
        f"📝 Хэш кода: {_code_hash()}\n"
        f"🕒 Время сервера: {datetime.now(timezone.utc).isoformat()}\n"
        f"🤖 OpenAI: {openai_status}"
    ))

@bot.message_handler(commands=["menu"])
def cmd_menu(m: types.Message):
    bot.send_message(m.chat.id, "Меню:", reply_markup=MAIN_MENU)

@bot.message_handler(content_types=["text"])
def on_text(m: types.Message):
    uid = m.from_user.id
    text_in = (m.text or "").strip()
    handle_text(uid, text_in, m)

def handle_text(uid: int, text_in: str, original_message: Optional[types.Message] = None):
    st = load_state(uid)
    log.info("User %s: intent=%s step=%s text='%s'", uid, st["intent"], st["step"], text_in[:200])

    # быстрый reset
    if text_in.lower() in ("новый разбор","новый","с чистого листа","start over"):
        st = save_state(uid, INTENT_FREE, STEP_FREE_CHAT, {"history": [], "coach_turns": 0, "struct_offer_shown": False})
        bot.send_message(uid, "Окей, чистый лист. Что сейчас хочется поправить в трейдинге?", reply_markup=MAIN_MENU)
        return

    # история (user)
    st["data"] = _append_history(st["data"], "user", text_in)
    st["data"]["last_user_msg_at"] = _now_iso()
    st["data"]["awaiting_reply"] = True

    # выбор стиля
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if text_in.lower() in ("ты","вы"):
            st["data"]["style"] = text_in.lower()
            st = save_state(uid, INTENT_FREE, STEP_FREE_CHAT, st["data"])
            bot.send_message(uid, f"Принято ({text_in}). Начнём спокойно и без спешки. Что сейчас больше всего мешает?", reply_markup=MAIN_MENU)
        else:
            save_state(uid, data=st["data"])
            bot.send_message(uid, "Выбери «ты» или «вы».", reply_markup=STYLE_KB)
        return

    # Если уже в структурном потоке — обрабатываем его
    if st["intent"] == INTENT_ERR:
        proceed_struct(uid, text_in, st)
        return

    # ===== Живой коуч-слой (ведёт ChatGPT) =====
    turns = int(st["data"].get("coach_turns", 0))
    decision = gpt_calibrate(uid, text_in, st)
    resp = decision["response_text"]
    # обновим память
    mem = st["data"]
    mem = _append_history(mem, "assistant", resp)
    if decision.get("store"):
        try:
            mem.update(decision["store"])
        except Exception:
            pass
    if decision.get("summary_draft"):
        mem["problem_draft"] = decision["summary_draft"]

    readiness = float(decision.get("readiness_score", 0.0))
    turns += 1
    mem["coach_turns"] = turns
    st = save_state(uid, INTENT_FREE, STEP_FREE_CHAT, mem)

    # Отвечаем (один ход = одно сообщение)
    if original_message:
        bot.reply_to(original_message, resp, reply_markup=MAIN_MENU)
    else:
        bot.send_message(uid, resp, reply_markup=MAIN_MENU)

    # Если модель просит подтверждение — выносим ЭТО отдельным ходом
    if decision.get("ask_confirm") and mem.get("problem_draft"):
        kb = types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Да, верно", callback_data="confirm_problem"),
            types.InlineKeyboardButton("Чуть иначе", callback_data="refine_problem")
        )
        bot.send_message(uid, f"Суммирую коротко:\n\n<b>{mem['problem_draft']}</b>\n\nПодходит?", reply_markup=kb)
        return

    # Если человек уже подтвердил раньше — можно в любой момент предложить структуру
    if mem.get("problem_confirmed"):
        offer_structure(uid, st)
        return

    # Страховка от слишком раннего старта: нужна готовность И контекст
    if readiness >= 0.85 and (turns >= 3 or risky(text_in)):
        # Попросим короткое подтверждение резюме (если его нет — сгенерим из памяти)
        if not mem.get("problem_draft"):
            auto = extract_summary_from_memory(mem)
            if auto:
                mem["problem_draft"] = auto
                save_state(uid, data=mem)
        if mem.get("problem_draft"):
            kb = types.InlineKeyboardMarkup().row(
                types.InlineKeyboardButton("Да, верно", callback_data="confirm_problem"),
                types.InlineKeyboardButton("Чуть иначе", callback_data="refine_problem")
            )
            bot.send_message(uid, f"Суммирую:\n\n<b>{mem['problem_draft']}</b>\n\nПодходит?", reply_markup=kb)

# ========= Предложение структуры (отдельным ходом) =========
def offer_structure(uid: int, st: Dict[str, Any]):
    data = st["data"]
    if data.get("struct_offer_shown"):
        return
    data["struct_offer_shown"] = True
    save_state(uid, data=data)
    kb = types.InlineKeyboardMarkup().row(
        types.InlineKeyboardButton("Разобрать по шагам", callback_data="start_error_flow"),
        types.InlineKeyboardButton("Пока нет", callback_data="skip_error_flow")
    )
    bot.send_message(uid, "Готов разобрать это по шагам (коротко и без спешки)?", reply_markup=kb)

# ========= Структурный поток =========
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

        mer = data.get("mer", {})
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
        save_state(uid, INTENT_DONE, STEP_FREE_CHAT, data)
        bot.send_message(uid, "\n".join(summary), reply_markup=MAIN_MENU)
        bot.send_message(uid, "Готов вынести это в «фокус недели» или идём дальше?", reply_markup=MAIN_MENU)
        return

    # fallback — вернёмся в коуч-слой
    save_state(uid, INTENT_FREE, STEP_FREE_CHAT, data)
    bot.send_message(uid, "Окей, вернёмся на шаг назад и уточним ещё чуть-чуть.", reply_markup=MAIN_MENU)

# ========= Меню =========
MENU_BTNS = {
    "🚑 У меня ошибка": "error",
    "🧩 Хочу стратегию": "strategy",
    "📄 Паспорт": "passport",
    "🗒 Панель недели": "weekpanel",
    "🆘 Экстренно": "panic",
    "🤔 Не знаю, с чего начать": "start_help",
}

@bot.message_handler(func=lambda m: m.text in MENU_BTNS)
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
            save_state(uid, INTENT_FREE, STEP_FREE_CHAT, st["data"])
            bot.send_message(uid, "Коротко — что именно сейчас мешает? Сформулируй в одном-двух предложениях.", reply_markup=MAIN_MENU)
    elif code == "start_help":
        bot.send_message(uid, "План: 1) быстрый разбор проблемы, 2) фокус недели, 3) скелет ТС. С чего начнём?", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])
    else:
        bot.send_message(uid, "Ок. Если хочешь ускориться — нажми «🚑 У меня ошибка».", reply_markup=MAIN_MENU)
        save_state(uid, data=st["data"])

# ========= Callbacks =========
@bot.callback_query_handler(func=lambda c: True)
def on_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    data = call.data or ""
    bot.answer_callback_query(call.id, "Ок")
    st = load_state(uid)

    if data == "confirm_problem":
        st["data"]["problem"] = st["data"].get("problem_draft", "—")
        st["data"]["problem_confirmed"] = True
        st["data"]["struct_offer_shown"] = False
        save_state(uid, INTENT_FREE, STEP_FREE_CHAT, st["data"])
        # отдельным ходом — предложение структуры
        offer_structure(uid, st)
        return

    if data == "refine_problem":
        st["data"]["problem_confirmed"] = False
        save_state(uid, INTENT_FREE, STEP_FREE_CHAT, st["data"])
        bot.send_message(uid, "Хорошо. Сформулируй тогда поконкретнее, что именно разбирать.", reply_markup=MAIN_MENU)
        return

    if data == "start_error_flow":
        st["data"]["problem_confirmed"] = True
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        bot.send_message(uid, "Начинаем разбор. Опиши последний случай: вход/план, где отступил, результат.")
        return

    if data == "skip_error_flow":
        bot.send_message(uid, "Окей, вернёмся к этому позже.", reply_markup=MAIN_MENU)
        return

    if data == "continue_session":
        st["data"]["awaiting_reply"] = False
        st["data"]["last_nag_at"] = _now_iso()
        save_state(uid, data=st["data"])
        bot.send_message(uid, "Продолжаем. На чём остановились?", reply_markup=MAIN_MENU)
        return

    if data == "restart_session":
        st = save_state(uid, INTENT_FREE, STEP_FREE_CHAT, {"history": [], "coach_turns": 0, "struct_offer_shown": False})
        bot.send_message(uid, "Окей, начнём заново. Что сейчас хочется поправить?", reply_markup=MAIN_MENU)
        return

# ========= HTTP =========
@app.get("/")
def root():
    return jsonify({"ok": True, "time": _now_iso(), "version": BOT_VERSION, "openai": openai_status})

@app.get("/version")
def version_api():
    return jsonify({"version": BOT_VERSION, "code_hash": _code_hash(), "status": "running", "timestamp": _now_iso(), "openai": openai_status})

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
            abort(400, description="Invalid update")
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        log.error("Webhook processing error: %s", e)
        abort(500)

# ========= Housekeeping / Reminders =========
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
        mins = IDLE_MINUTES_REMIND
        reset_mins = IDLE_MINUTES_RESET
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
            nag_ok = True
            last_nag_at = data.get("last_nag_at")
            if last_nag_at:
                try:
                    if (now - datetime.fromisoformat(last_nag_at)) < timedelta(minutes=max(1, mins // 2)):
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

# ========= Init on import =========
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

try:
    th = threading.Thread(target=background_housekeeping, daemon=True)
    th.start()
except Exception as e:
    log.error("housekeeping thread error: %s", e)

# ========= Gunicorn entry =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
