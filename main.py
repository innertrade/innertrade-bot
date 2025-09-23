# main.py — Innertrade Kai Mentor Bot (coach-struct v7.1)
# Версия: 2025-09-23

import os, json, time, logging, threading, hashlib, re
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

BOT_VERSION = f"2025-09-23-{get_code_version()}"

# ========= ENV =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PUBLIC_URL     = os.getenv("PUBLIC_URL", "").strip()
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "webhook").strip()
TG_SECRET      = os.getenv("TG_WEBHOOK_SECRET", "").strip()
DATABASE_URL   = os.getenv("DATABASE_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OFFSCRIPT_ENABLED = os.getenv("OFFSCRIPT_ENABLED", "true").lower() == "true"
SET_WEBHOOK_FLAG  = os.getenv("SET_WEBHOOK", "false").lower() == "true"
LOG_LEVEL      = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_BODY       = int(os.getenv("MAX_BODY", "1000000"))
HIST_LIMIT     = 12

# ========= Sanity =========
for var in ("TELEGRAM_TOKEN","PUBLIC_URL","WEBHOOK_PATH","TG_SECRET","DATABASE_URL"):
    if not globals()[var]:
        raise RuntimeError(f"{var} is required")

# ========= Logging =========
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("kai-mentor")
log.info(f"Запуск бота версия: {BOT_VERSION}")

# ========= Intents/Steps =========
INTENT_GREET = "greet"
INTENT_FREE  = "free"
INTENT_ERR   = "error"

STEP_ASK_STYLE     = "ask_style"
STEP_FREE_INTRO    = "free_intro"

STEP_CONFIRM_PROBLEM = "confirm_problem"  # <-- калибровка/подтверждение

STEP_ERR_DESCR   = "err_describe"
STEP_MER_CTX     = "mer_context"
STEP_MER_EMO     = "mer_emotions"
STEP_MER_THO     = "mer_thoughts"
STEP_MER_BEH     = "mer_behavior"
STEP_GOAL        = "goal_positive"
STEP_TOTE_OPS    = "tote_ops"
STEP_TOTE_TEST   = "tote_test"
STEP_TOTE_EXIT   = "tote_exit"

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
        log.info("OpenAI готов")
    except Exception as e:
        log.error(f"OpenAI init error: {e}")
        oai_client = None
        openai_status = f"error: {e}"

# ========= DB =========
# ВАЖНО: для SQLAlchemy URL используй dialect+driver (postgresql+psycopg://)
# Пример ENV: postgresql+psycopg://user:pass@host/db?sslmode=require&channel_binding=require
engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=5, max_overflow=10, pool_timeout=30, pool_recycle=1800,
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
        data  TEXT,
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    """)
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_updated_at ON user_state(updated_at)")
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_intent_step ON user_state(intent, step)")
    log.info("БД инициализирована")

# ========= State =========
def load_state(uid: int) -> Dict[str, Any]:
    row = db_exec("SELECT intent, step, data FROM user_state WHERE user_id=:uid", {"uid": uid}).mappings().first()
    if row:
        data = {}
        if row["data"]:
            try:
                data = json.loads(row["data"])
            except Exception as e:
                log.error("parse user data error: %s", e)
        return {"user_id": uid, "intent": row["intent"] or INTENT_GREET, "step": row["step"] or STEP_ASK_STYLE, "data": data}
    return {"user_id": uid, "intent": INTENT_GREET, "step": STEP_ASK_STYLE, "data": {"history": []}}

def save_state(uid: int, intent=None, step=None, data=None) -> Dict[str, Any]:
    cur = load_state(uid)
    intent = intent or cur["intent"]
    step   = step   or cur["step"]
    new_data = cur["data"].copy()
    if data:
        new_data.update(data)
    db_exec("""
        INSERT INTO user_state (user_id,intent,step,data,updated_at)
        VALUES (:uid,:intent,:step,:data,now())
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

# ========= Patterns =========
RISK_PATTERNS = {
    "remove_stop": ["убираю стоп", "снял стоп", "без стопа"],
    "move_stop": ["двигаю стоп", "отодвинул стоп", "переставил стоп"],
    "early_close": ["ранний выход", "вышел в ноль", "мизерный плюс", "закрыл рано"],
    "averaging": ["усреднение", "доливался против", "докупал против"],
    "fomo": ["поезд уедет", "упущу", "уйдёт без меня", "страх упустить"],
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

def should_force_structural(text: str) -> bool:
    pats = detect_trading_patterns(text)
    risk = set(pats) & set(RISK_PATTERNS.keys())
    return bool(risk) or ("fear_of_loss" in pats) or ("self_doubt" in pats)

# ========= Helpers (tone & clean) =========
BAN_TEMPLATES = ["понимаю", "это может быть", "важно понять", "давай рассмотрим", "было бы полезно",
                 "попробуй", "используй", "придерживайся", "установи", "сфокусируйся", "следуй", "пересмотри"]

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

def mer_prompt_for(step: str) -> str:
    return {
        STEP_MER_CTX: "Зафиксируем картинку коротко. Где/когда это было?",
        STEP_MER_EMO: "Что чувствовал в моменте (2–3 слова)?",
        STEP_MER_THO: "Какие мысли мелькали (2–3 короткие фразы)?",
        STEP_MER_BEH: "Что сделал фактически? (действия)",
    }.get(step, "Продолжим.")

def summarize_issue(text_in: str) -> str:
    """Короткое резюме проблемы на основе триггеров/языка."""
    pats = set(detect_trading_patterns(text_in))
    parts = []
    if "fomo" in pats: parts.append("страх упустить вход (FOMO)")
    if "remove_stop" in pats or "move_stop" in pats: parts.append("трогаешь/снимаешь стоп")
    if "early_close" in pats: parts.append("ранний выход/«в ноль»")
    if "averaging" in pats: parts.append("усреднение против позиции")
    if "fear_of_loss" in pats: parts.append("страх стопа/потерь")
    if "self_doubt" in pats: parts.append("сомнения после входа")
    core = " и ".join(parts) if parts else "нарушение плана после открытия сделки"
    return f"Похоже, ключевая трудность — {core}. Так сформулируем?"

def mer_snapshot(data: Dict[str, Any]) -> str:
    mer = data.get("mer", {})
    return (
        f"Контекст: {mer.get(STEP_MER_CTX, '—')}\n"
        f"Эмоции: {mer.get(STEP_MER_EMO, '—')}\n"
        f"Мысли: {mer.get(STEP_MER_THO, '—')}\n"
        f"Действия: {mer.get(STEP_MER_BEH, '—')}"
    )

# ========= Voice (optional) =========
def transcribe_voice(audio_file_path: str) -> Optional[str]:
    if not oai_client:
        return None
    try:
        with open(audio_file_path, "rb") as audio_file:
            tr = oai_client.audio.transcriptions.create(
                model="whisper-1", file=audio_file, language="ru"
            )
        return getattr(tr, "text", None)
    except Exception as e:
        log.error("Whisper error: %s", e)
        return None

# ========= GPT (one-step coach) =========
def gpt_decide(uid: int, text_in: str, st: Dict[str, Any]) -> Dict[str, Any]:
    """Коуч задаёт один точный шаг и при наличии триггеров наводит к подтверждению проблемы/разбору."""
    fallback = {
        "next_step": st["step"],
        "intent": st["intent"],
        "response_text": "Окей, двигаемся короткими шагами — я рядом. Расскажи на примере: где именно ты отступил от плана (вход/стоп/выход)?",
        "store": {},
        "is_structural": False
    }
    if not oai_client or not OFFSCRIPT_ENABLED:
        return fallback

    style = st["data"].get("style", "ты")
    patterns = detect_trading_patterns(text_in)
    system_prompt = f"""
Ты — тёплый и точный коуч по трейдингу Алекс. Без списков советов и общих рассуждений.
Твоя задача — 1) коротко отразить суть, 2) задать один точный практичный вопрос
или мягко подвести к подтверждению формулировки проблемы. Пиши разговорно на «{style}».

Формат ответа JSON:
- next_step
- intent
- response_text (1–2 абзаца, без клише)
- store (object)
- is_structural (true/false, если пора идти в структурный разбор)
""".strip()

    msgs = [{"role": "system", "content": system_prompt}]
    for h in st["data"].get("history", [])[-HIST_LIMIT:]:
        if h.get("role") in ("user","assistant") and isinstance(h.get("content"), str):
            msgs.append(h)
    msgs.append({"role":"user","content": text_in})

    try:
        res = oai_client.chat.completions.create(
            model=OPENAI_MODEL, messages=msgs, temperature=0.3,
            response_format={"type":"json_object"},
        )
        raw = res.choices[0].message.content or "{}"
        dec = json.loads(raw) if isinstance(raw, str) else fallback
        for k in ["next_step","intent","response_text","store","is_structural"]:
            if k not in dec: return fallback

        resp = strip_templates(anti_echo(text_in, dec.get("response_text","").strip()))
        if len(resp) < 12:
            resp = "Окей. На этом кейсе: где конкретно ты отошёл от плана (вход/стоп/выход)?"
        dec["response_text"] = resp

        # Хард-триггеры — мягко просим подтвердить формулировку
        if should_force_structural(text_in):
            dec["is_structural"] = True

        return dec
    except Exception as e:
        log.error("GPT decision error: %s", e)
        return fallback

# ========= UI bits =========
def kb_continue_or_new():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("Продолжить", callback_data="resume_flow"),
        types.InlineKeyboardButton("Начать заново", callback_data="restart_flow"),
    )
    return kb

def kb_yes_no():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("Да, верно", callback_data="confirm_problem_yes"),
        types.InlineKeyboardButton("Поправить", callback_data="confirm_problem_no"),
    )
    return kb

# ========= Calibration / Confirmation =========
def maybe_confirm_problem(uid: int, last_text: str, st: Dict[str, Any]):
    """Если видим явные триггеры и нет подтверждённой проблемы — предлагаем формулировку и спрашиваем согласие."""
    data = st["data"]
    if st["intent"] == INTENT_ERR:
        return  # уже в разборе
    if data.get("problem_confirmed"):
        return
    if should_force_structural(last_text):
        draft = summarize_issue(last_text)
        data["draft_problem"] = draft
        data["awaiting_confirm"] = True
        save_state(uid, INTENT_FREE, STEP_CONFIRM_PROBLEM, data)
        bot.send_message(uid, f"Скажу коротко, как я тебя понял:\n\n{draft}\n\nВерно?", reply_markup=kb_yes_no())

def continue_prompt_if_needed(uid: int, text_in: str, st: Dict[str, Any]):
    hello = text_in.lower().strip()
    if st["intent"] == INTENT_ERR and any(w in hello for w in ("привет","/start","/начать","/continue")):
        bot.send_message(uid, "Похоже, мы не закончили прошлый разбор. Продолжим или начнём заново?", reply_markup=kb_continue_or_new())

# ========= Handlers =========
@bot.message_handler(commands=["start","reset"])
def cmd_start(m: types.Message):
    st = save_state(m.from_user.id, INTENT_GREET, STEP_ASK_STYLE, {"history": []})
    bot.send_message(
        m.chat.id,
        "👋 Привет! Как удобнее — <b>ты</b> или <b>вы</b>?\n\nЕсли захочешь начать чистый лист, просто напиши: <b>новый разбор</b>.",
        reply_markup=STYLE_KB
    )

@bot.message_handler(commands=["version","v"])
def cmd_version(m: types.Message):
    info = f"🔄 Версия бота: {BOT_VERSION}\n📝 Хэш кода: {get_code_version()}\n🕒 Время сервера: {datetime.now(timezone.utc).isoformat()}\n🤖 OpenAI: {openai_status}"
    bot.reply_to(m, info)

@bot.message_handler(content_types=['voice','audio'])
def handle_voice(message: types.Message):
    uid = message.from_user.id
    try:
        file_id = message.voice.file_id if message.content_type == 'voice' else message.audio.file_id
        file_info = bot.get_file(file_id)
        data = bot.download_file(file_info.file_path)
        tmp = f"voice_{uid}_{int(time.time())}.ogg"
        with open(tmp,"wb") as f: f.write(data)
        txt = transcribe_voice(tmp)
        try: os.remove(tmp)
        except: pass
        if not txt:
            bot.reply_to(message, "Не удалось распознать голос. Скажи ещё раз или напиши текстом.")
            return
        handle_text_message(uid, txt, message)
    except Exception as e:
        log.error("Voice processing error: %s", e)
        bot.reply_to(message, "Произошла ошибка при обработке голоса. Напиши текстом, пожалуйста.")

@bot.message_handler(content_types=["text"])
def on_text(m: types.Message):
    handle_text_message(m.from_user.id, m.text.strip(), m)

def handle_text_message(uid: int, text_in: str, original_message=None):
    st = load_state(uid)
    log.info("User %s: intent=%s step=%s text='%s'", uid, st["intent"], st["step"], text_in[:200])

    # Спец-триггер: "новый разбор"
    if text_in.lower() == "новый разбор":
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, {"history": []})
        bot.send_message(uid, "Окей, начнём с чистого листа. Опиши последний случай: что планировал и где отступил?")
        return

    # history (user)
    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT: history = history[-(HIST_LIMIT-1):]
    history.append({"role":"user","content": text_in})
    st["data"]["history"] = history

    # Выбор стиля
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if text_in.lower() in ("ты","вы"):
            st["data"]["style"] = text_in.lower()
            save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
            bot.send_message(uid, f"Принято ({text_in}). Что сейчас в трейдинге хочется поправить?\nЕсли захочешь начать с чистого листа — напиши «новый разбор».", reply_markup=MAIN_MENU)
        else:
            save_state(uid, data=st["data"])
            bot.send_message(uid, "Выбери «ты» или «вы».", reply_markup=STYLE_KB)
        return

    # Если пользователь написал «привет» во время незавершённого разбора — предложим продолжить/сбросить
    continue_prompt_if_needed(uid, text_in, st)

    # Структурный разбор
    if st["intent"] == INTENT_ERR:
        handle_structural(uid, text_in, st)
        return

    # Свободный коучинг (1 шаг) + возможная калибровка
    decision = gpt_decide(uid, text_in, st)
    resp = decision.get("response_text") or "Окей. Где именно ты отступил от плана (вход/стоп/выход)?"

    # history (assistant)
    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT: history = history[-(HIST_LIMIT-1):]
    history.append({"role":"assistant","content": resp})

    merged = st["data"].copy()
    store = decision.get("store", {})
    if isinstance(store, dict): merged.update(store)
    merged["history"] = history

    new_intent = decision.get("intent") or st["intent"]
    new_step   = decision.get("next_step") or st["step"]
    st_after = save_state(uid, new_intent, new_step, merged)

    if original_message:
        bot.reply_to(original_message, resp, reply_markup=MAIN_MENU)
    else:
        bot.send_message(uid, resp, reply_markup=MAIN_MENU)

    # Калибровка: подтверждение формулировки перед входом в разбор
    if decision.get("is_structural", False) or should_force_structural(text_in):
        maybe_confirm_problem(uid, text_in, st_after)

# ========= Structural Flow =========
def handle_structural(uid: int, text_in: str, st: Dict[str, Any]):
    # Подтверждение проблемы (если пришли текстом после "Поправить")
    if st["step"] == STEP_CONFIRM_PROBLEM and st["data"].get("awaiting_confirm"):
        # Пользователь дал свою формулировку — считаем подтверждением и двигаемся
        st["data"]["draft_problem"] = text_in
        st["data"]["problem_confirmed"] = True
        st["data"]["awaiting_confirm"] = False
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        bot.send_message(uid, "Принял формулировку. Возьмём свежий пример. Опиши последний случай: что планировал и где отступил?")
        return

    # Описание ошибки
    if st["step"] == STEP_ERR_DESCR:
        new_data = st["data"].copy()
        new_data["error_description"] = text_in
        # Мягкий мостик к шагам
        save_state(uid, INTENT_ERR, STEP_MER_CTX, new_data)
        bot.send_message(uid, "Окей, пойдём по шагам, коротко и по делу. Сначала контекст.")
        bot.send_message(uid, mer_prompt_for(STEP_MER_CTX))
        return

    # MER: контекст → эмоции → мысли → поведение
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
            # Фиксируем картину перед целями
            snap = mer_snapshot(new_data)
            save_state(uid, INTENT_ERR, STEP_GOAL, new_data)
            bot.send_message(uid, f"Картину собрали:\n\n{snap}\n\nТеперь — позитивная цель. Что делаешь вместо прежнего поведения?")
        return

    # Цель
    if st["step"] == STEP_GOAL:
        new_data = st["data"].copy()
        new_data["goal"] = text_in
        save_state(uid, INTENT_ERR, STEP_TOTE_OPS, new_data)
        bot.send_message(uid, "Два-три конкретных шага для ближайших 3 сделок (коротко, списком).")
        return

    # TOTE - ops
    if st["step"] == STEP_TOTE_OPS:
        tote = st["data"].get("tote", {})
        tote["ops"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_TEST, new_data)
        bot.send_message(uid, "Как поймёшь, что получилось? Один простой критерий.")
        return

    # TOTE - test
    if st["step"] == STEP_TOTE_TEST:
        tote = st["data"].get("tote", {})
        tote["test"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_EXIT, new_data)
        bot.send_message(uid, "Если проверка покажет «не получилось» — что сделаешь?")
        return

    # TOTE - exit (финал)
    if st["step"] == STEP_TOTE_EXIT:
        tote = st["data"].get("tote", {})
        tote["exit"] = text_in
        new_data = st["data"].copy()
        new_data["tote"] = tote

        mer = new_data.get('mer', {})
        summary = [
            "<b>Итог разбора</b>",
            f"Проблема: {new_data.get('draft_problem','—')}",
            f"Ошибка: {new_data.get('error_description','—')}",
            f"Контекст: {mer.get(STEP_MER_CTX,'—')}",
            f"Эмоции: {mer.get(STEP_MER_EMO,'—')}",
            f"Мысли: {mer.get(STEP_MER_THO,'—')}",
            f"Действия: {mer.get(STEP_MER_BEH,'—')}",
            f"Цель: {new_data.get('goal','—')}",
            f"Шаги: {new_data.get('tote',{}).get('ops','—')}",
            f"Проверка: {new_data.get('tote',{}).get('test','—')}",
            f"Если не вышло: {new_data.get('tote',{}).get('exit','—')}",
        ]
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, new_data)
        bot.send_message(uid, "\n".join(summary), reply_markup=MAIN_MENU)
        bot.send_message(uid, "Готов вынести это в фокус недели или идём дальше?")
        return

# ========= Menu handlers =========
MENU_BTNS = {
    "🚑 У меня ошибка":"error",
    "🧩 Хочу стратегию":"strategy",
    "📄 Паспорт":"passport",
    "🗒 Панель недели":"weekpanel",
    "🆘 Экстренно":"panic",
    "🤔 Не знаю, с чего начать":"start_help",
}
@bot.message_handler(func=lambda m: m.text in MENU_BTNS.keys())
def handle_menu(m: types.Message):
    uid = m.from_user.id
    st = load_state(uid)
    label = m.text; code = MENU_BTNS[label]

    hist = st["data"].get("history", [])
    if len(hist) >= HIST_LIMIT: hist = hist[-(HIST_LIMIT-1):]
    hist.append({"role":"user","content": label})
    st["data"]["history"] = hist

    if code == "error":
        # Сброс в разбор ошибки через подтверждение, если нет подтверждения
        if not st["data"].get("problem_confirmed"):
            draft = "Разбираем недавнюю ошибку в сделке — ок?"
            st["data"]["draft_problem"] = draft
            st["data"]["awaiting_confirm"] = True
            save_state(uid, INTENT_FREE, STEP_CONFIRM_PROBLEM, st["data"])
            bot.send_message(uid, f"{draft}\n\nВерно?", reply_markup=kb_yes_no())
        else:
            save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
            bot.send_message(uid, "Опиши последний кейс ошибки: что планировал и где отступил?")
    elif code == "start_help":
        bot.send_message(uid, "План: 1) короткий разбор ошибки, 2) фокус недели, 3) скелет ТС. С чего начнём?", reply_markup=MAIN_MENU)
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
    st = load_state(uid)

    if data == "confirm_problem_yes":
        st["data"]["problem_confirmed"] = True
        st["data"]["awaiting_confirm"] = False
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        bot.send_message(uid, "Отлично, берём этот фокус. Опиши последний случай: что планировал и где отступил?")
    elif data == "confirm_problem_no":
        st["data"]["awaiting_confirm"] = True
        save_state(uid, INTENT_FREE, STEP_CONFIRM_PROBLEM, st["data"])
        bot.send_message(uid, "Поправь формулировку в 1–2 предложения — как ты бы её назвал?")
    elif data == "resume_flow":
        # Продолжаем с текущего шага
        save_state(uid, st["intent"], st["step"], st["data"])
        next_prompt = {
            STEP_ERR_DESCR: "Опиши последний случай: что планировал и где отступил?",
            STEP_MER_CTX: mer_prompt_for(STEP_MER_CTX),
            STEP_MER_EMO: mer_prompt_for(STEP_MER_EMO),
            STEP_MER_THO: mer_prompt_for(STEP_MER_THO),
            STEP_MER_BEH: mer_prompt_for(STEP_MER_BEH),
            STEP_GOAL: "Позитивная цель: что делаешь вместо прежнего поведения?",
            STEP_TOTE_OPS: "Два-три конкретных шага для ближайших 3 сделок (коротко, списком).",
            STEP_TOTE_TEST: "Как поймёшь, что получилось? Один простой критерий.",
            STEP_TOTE_EXIT: "Если проверка покажет «не получилось» — что сделаешь?"
        }.get(st["step"], "Продолжим.")
        bot.send_message(uid, next_prompt)
    elif data == "restart_flow":
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, {"history": []})
        bot.send_message(uid, "Окей, начнём заново. Опиши последний случай: что планировал и где отступил?")

# ========= HTTP =========
def _now_iso(): return datetime.now(timezone.utc).isoformat()

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

# ========= Init =========
try:
    init_db()
except Exception as e:
    log.error("Инициализация базы данных не удалась: %s", e)

if SET_WEBHOOK_FLAG:
    try:
        bot.remove_webhook(); time.sleep(1)
        bot.set_webhook(
            url=f"{PUBLIC_URL}/{WEBHOOK_PATH}",
            secret_token=TG_SECRET,
            allowed_updates=["message","callback_query"]
        )
        log.info("Webhook установлен на %s/%s", PUBLIC_URL, WEBHOOK_PATH)
    except Exception as e:
        log.error("Webhook setup error: %s", e)

if __name__ == "__main__":
    port = int(os.getenv("PORT","10000"))
    app.run(host="0.0.0.0", port=port)
