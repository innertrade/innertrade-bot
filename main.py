# main.py — Innertrade Kai Mentor Bot
# Версия: 2025-09-22 (coach-struct v3)

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

STEP_CONFIRM_PROBLEM = "confirm_problem"     # новая стадия: подтвердить гипотезу проблемы

STEP_ERR_DESCR = "err_describe"
STEP_MER_CTX   = "mer_context"
STEP_MER_EMO   = "mer_emotions"
STEP_MER_THO   = "mer_thoughts"
STEP_MER_BEH   = "mer_behavior"

STEP_MER_RECAP = "mer_recap"                 # чек-пойнт между блоками

STEP_GOAL      = "goal_positive"
STEP_TOTE_OPS  = "tote_ops"
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

# ========= Bot / Flask =========
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)

MAIN_MENU = types.ReplyKeyboardMarkup(resize_keyboard=True)
MAIN_MENU.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
MAIN_MENU.row("📄 Паспорт", "🗒 Панель недели")
MAIN_MENU.row("🆘 Экстренно", "🤔 Не знаю, с чего начать")

STYLE_KB = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
STYLE_KB.row("ты", "вы")

# ========= Pattern Detection & Hypothesis =========
RISK_PATTERNS = {
    "remove_stop": ["убираю стоп", "снял стоп", "без стопа"],
    "move_stop": ["двигаю стоп", "отодвинул стоп", "переставил стоп"],
    "early_close": ["закрыл рано", "вышел в ноль", "мизерный плюс", "ранний выход"],
    "averaging": ["усреднение", "доливался против", "докупал против"],
    "fomo": ["поезд уедет", "упустил", "уйдёт без меня", "страх упустить", "не вернётся"],
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

def make_problem_hypothesis(text: str) -> str:
    tl = text.lower()
    pats = detect_trading_patterns(tl)
    if "fomo" in pats and "early_close" in pats:
        return "ранний вход/выход из-за FOMO и страха потерь"
    if "fomo" in pats:
        return "ранний вход из-за FOMO (страх упустить)"
    if "early_close" in pats:
        return "ранний выход «в ноль» при колебаниях против позиции"
    if "remove_stop" in pats or "move_stop" in pats:
        return "трогаешь/снимаешь стоп после входа"
    if "averaging" in pats:
        return "усреднение против позиции"
    if "rule_breaking" in pats:
        return "отклонение от торгового плана"
    return "конкретизируем основную ошибку в последнем кейсе"

# ========= Helpers (tone & sanitizing) =========
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
        return "Скажу по-своему: " + m
    return m

def short_reflection(st: Dict[str, Any]) -> str:
    # микро-отражение из последнего юзер-сообщения
    hist = st["data"].get("history", [])
    msg = ""
    for it in reversed(hist):
        if it.get("role") == "user":
            msg = it.get("content", "")
            break
    if not msg:
        return ""
    # возьмём первое предложение, укоротим до 140
    sent = re.split(r'[.!?\n]', msg.strip())[0][:140]
    return f"Окей. Если коротко: «{sent}». Верно?"

# ========= Voice (Whisper) =========
def transcribe_voice(audio_file_path: str) -> Optional[str]:
    if not oai_client:
        log.warning("Whisper: client not available")
        return None
    try:
        log.info("Whisper: uploading %s", audio_file_path)
        with open(audio_file_path, "rb") as audio_file:
            tr = oai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru"
            )
        text = getattr(tr, "text", None)
        log.info("Whisper: ok, len=%s", len(text or ""))
        return text
    except Exception as e:
        log.error("Whisper error: %s", e)
        return None

# ========= GPT (strict coach, warm tone) =========
def gpt_decide(uid: int, text_in: str, st: Dict[str, Any]) -> Dict[str, Any]:
    fallback = {
        "next_step": st["step"],
        "intent": st["intent"],
        "response_text": "Чтобы двинуться по делу: где именно ты отступил от плана — вход, стоп или выход?",
        "store": {},
        "is_structural": False
    }
    if not oai_client or not OFFSCRIPT_ENABLED:
        return fallback

    style = st["data"].get("style", "ты")
    patterns = detect_trading_patterns(text_in)
    patterns_text = ", ".join(patterns) if patterns else "нет"

    system_prompt = f"""
Ты — коуч-наставник Алекс. Один тёплый уточняющий вопрос или мостик к структурному разбору. Никаких советов и списков техник.
Всегда разговорно, коротко, без штампов. Не повторяй то, что уже сказал пользователь — лучше сделай короткое отражение и один точный вопрос.

Формат ответа JSON:
- next_step
- intent
- response_text  (1–2 абзаца, на «{style}»)
- store          (объект)
- is_structural  (true/false — пора ли перейти к разбору)

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
        resp = strip_templates(anti_echo(text_in, dec.get("response_text", "")))
        if any(b in resp.lower() for b in ["попробуй", "используй", "придерживайся", "установи", "сфокусируйся", "следуй", "пересмотри"]) or len(resp) < 12:
            resp = "Возьмём этот кейс. Где именно ты отступил от плана — вход, стоп или выход?"
        dec["response_text"] = resp
        # Подсказка на переход в структуру по триггерам
        if should_force_structural(text_in):
            dec["is_structural"] = True
        return dec
    except Exception as e:
        log.error("GPT decision error: %s", e)
        return fallback

# ========= Prefill from history (to avoid repeats) =========
EMO_LEX = ["страх", "досада", "обида", "напряжение", "волнение", "паника", "раздражение", "фрустрация"]
def prefill_from_history(st: Dict[str, Any]) -> Dict[str, Any]:
    """Грубое извлечение контекста/мыслей/действий/эмоций из последних сообщений пользователя."""
    hist = st.get("data", {}).get("history", [])
    last_user_texts = [h["content"] for h in hist if h.get("role") == "user"][-3:]
    blob = " ".join(last_user_texts).strip()

    mer = st["data"].get("mer", {})
    flags = st["data"].get("mer_filled", {})

    # Контекст: биржа/инструмент/когда — по ключевым словам
    if not mer.get(STEP_MER_CTX):
        ctx = ""
        m1 = re.search(r"(на\s+байбит[е]?)|(bybit)", blob, re.I)
        m2 = re.search(r"(AI\w+|BTC|ETH|SOL|BONK|[A-Z]{2,10}\d*)", blob)
        m3 = re.search(r"(сегодня|вчера|на \w+-фрейме|таймфрейм\s*\w+)", blob, re.I)
        parts = []
        if m1: parts.append("биржа: Bybit")
        if m2: parts.append(f"инструмент: {m2.group(0)}")
        if m3: parts.append(m3.group(0))
        if parts:
            ctx = ", ".join(parts)
        if ctx:
            mer[STEP_MER_CTX] = ctx
            flags[STEP_MER_CTX] = "auto"

    # Эмоции
    if not mer.get(STEP_MER_EMO):
        emos = [w for w in EMO_LEX if re.search(rf"\b{re.escape(w)}\b", blob, re.I)]
        if not emos:
            # эвристика по паттернам
            pats = detect_trading_patterns(blob)
            if "fomo" in pats: emos.append("страх упустить")
            if "fear_of_loss" in pats: emos.append("страх стопа")
            if "chaos" in pats: emos.append("суета")
        if emos:
            mer[STEP_MER_EMO] = ", ".join(sorted(set(emos))[:3])
            flags[STEP_MER_EMO] = "auto"

    # Мысли
    if not mer.get(STEP_MER_THO):
        # вытащим пронумерованные пункты или цитаты
        thoughts = []
        for line in blob.splitlines():
            line = line.strip("-• \t")
            if re.match(r"^\d+[).]\s", line):
                thoughts.append(re.sub(r"^\d+[).]\s*", "", line)[:120])
        if not thoughts:
            # fallback: пару ключевых фраз по шаблонам
            if "не упущу" in blob or "поезд" in blob or "уйдёт без меня" in blob:
                thoughts.append("если не войду сейчас — упущу движение")
            if "правильно просчитал" in blob:
                thoughts.append("я прав в анализе — можно ускориться")
        if thoughts:
            mer[STEP_MER_THO] = "; ".join(thoughts[:3])
            flags[STEP_MER_THO] = "auto"

    # Действия
    if not mer.get(STEP_MER_BEH):
        beh = ""
        if re.search(r"заш[её]л руками|вош[её]л руками|открыл сделку руками", blob, re.I):
            beh = "вошёл руками раньше лимитки"
        if not beh and re.search(r"двигал стоп|отодвинул стоп|переставил стоп", blob, re.I):
            beh = "переставлял/отодвигал стоп"
        if beh:
            mer[STEP_MER_BEH] = beh
            flags[STEP_MER_BEH] = "auto"

    st["data"]["mer"] = mer
    st["data"]["mer_filled"] = flags
    return st

def field_ok_edit_kb(field_code: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("Ок", callback_data=f"field_ok:{field_code}"),
        types.InlineKeyboardButton("Изменить", callback_data=f"field_edit:{field_code}")
    )
    return kb

def ask_or_confirm_field(uid: int, st: Dict[str, Any], field: str, prompt: str, label: str):
    mer = st["data"].get("mer", {})
    flags = st["data"].get("mer_filled", {})
    if mer.get(field):
        text = f"{label}: {mer[field]}\nОставляем так?"
        bot.send_message(uid, text, reply_markup=field_ok_edit_kb(field))
    else:
        bot.send_message(uid, prompt)

# ========= Menus / Offer structural =========
def offer_problem_confirmation(uid: int, hypothesis: str):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("Да, это и есть ошибка", callback_data="confirm_problem_yes"),
        types.InlineKeyboardButton("Не совсем, уточню", callback_data="confirm_problem_no")
    )
    bot.send_message(uid, f"Похоже, ключевая ошибка — <b>{hypothesis}</b>. Согласен?", reply_markup=kb)

def offer_mer_to_tote_checkpoint(uid: int, st: Dict[str, Any]):
    mer = st["data"].get("mer", {})
    recap = [
        "Зафиксирую коротко:",
        f"• Контекст — {mer.get(STEP_MER_CTX, '—')}",
        f"• Чувства — {mer.get(STEP_MER_EMO, '—')}",
        f"• Мысли — {mer.get(STEP_MER_THO, '—')}",
        f"• Действия — {mer.get(STEP_MER_BEH, '—')}",
        "",
        "Идём к плану действий?"
    ]
    kb = types.InlineKeyboardMarkup().row(
        types.InlineKeyboardButton("Да", callback_data="mer_recap_yes"),
        types.InlineKeyboardButton("Назад, поправлю", callback_data="mer_recap_no"),
    )
    bot.send_message(uid, "\n".join(recap), reply_markup=kb)

# ========= Handlers =========
@bot.message_handler(commands=["start", "reset"])
def cmd_start(m: types.Message):
    save_state(m.from_user.id, INTENT_GREET, STEP_ASK_STYLE, {"history": []})
    bot.send_message(m.from_user.id, "👋 Привет! Как удобнее — <b>ты</b> или <b>вы</b>?", reply_markup=STYLE_KB)

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
        file_path = file_info.file_path
        log.info("Voice: file_id=%s path=%s", file_id, file_path)
        data = bot.download_file(file_path)
        tmp_name = f"voice_{uid}_{int(time.time())}.ogg"
        with open(tmp_name, "wb") as f:
            f.write(data)
        txt = transcribe_voice(tmp_name)
        try:
            os.remove(tmp_name)
        except Exception:
            pass
        if not txt:
            bot.reply_to(message, "Не получилось распознать голос. Скажи ещё раз или набери текстом.")
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

    # Greeting: выбор стиля
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if text_in.lower() in ("ты", "вы"):
            st["data"]["style"] = text_in.lower()
            save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
            ref = short_reflection(st)
            bot.send_message(uid, f"Принято ({text_in}). {ref}\nЧто сейчас в трейдинге хочешь поправить?", reply_markup=MAIN_MENU)
        else:
            save_state(uid, data=st["data"])
            bot.send_message(uid, "Выбери «ты» или «вы».", reply_markup=STYLE_KB)
        return

    # Structural flow
    if st["intent"] == INTENT_ERR:
        handle_structural(uid, text_in, st)
        return

    # --- FREE FLOW ---
    decision = gpt_decide(uid, text_in, st)
    resp = decision.get("response_text") or "Возьмём этот кейс. Где именно ты отступил от плана — вход, стоп или выход?"

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

    st_after = save_state(uid, decision.get("intent") or st["intent"], decision.get("next_step") or st["step"], merged)

    if original_message:
        bot.reply_to(original_message, resp, reply_markup=MAIN_MENU)
    else:
        bot.send_message(uid, resp, reply_markup=MAIN_MENU)

    # Если пора — получаем согласие на разбор (подтверждение проблемы)
    if decision.get("is_structural", False) or should_force_structural(text_in):
        hypothesis = make_problem_hypothesis(text_in)
        st_after["data"]["problem_hypothesis"] = hypothesis
        save_state(uid, INTENT_ERR, STEP_CONFIRM_PROBLEM, st_after["data"])
        offer_problem_confirmation(uid, hypothesis)

# ========= Structural Flow =========
def handle_structural(uid: int, text_in: str, st: Dict[str, Any]):
    step = st["step"]

    # 0) Подтверждение проблемы
    if step == STEP_CONFIRM_PROBLEM:
        # Пользователь уточняет текстом — обновим гипотезу и спросим подтверждение снова
        if text_in and text_in.lower() not in ("да", "ок", "ага"):
            st["data"]["problem_hypothesis"] = text_in.strip()[:200]
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        # Микро-рамка и переход: «опиши последний случай…»
        hyp = st["data"].get("problem_hypothesis", "ошибка в последнем кейсе")
        bot.send_message(uid, f"Окей. Берём в работу: <b>{hyp}</b>.\nОпиши последний случай: где/когда, вход/план, где отступил, чем закончилось.")
        return

    # 1) Описание кейса
    if step == STEP_ERR_DESCR:
        new_data = st["data"].copy()
        new_data["error_description"] = text_in
        # Предзаполним поля из истории
        st_prefilled = {"user_id": st["user_id"], "intent": st["intent"], "step": st["step"], "data": new_data}
        st_prefilled = prefill_from_history(st_prefilled)
        save_state(uid, INTENT_ERR, STEP_MER_CTX, st_prefilled["data"])
        # Переходим к полям с подтверждением/вопросами
        ask_or_confirm_field(uid, st_prefilled, STEP_MER_CTX,
                             "Где и когда это было? Один-два штриха: инструмент/биржа/таймфрейм.",
                             "Контекст")
        return

    # 2) MER: поля по очереди, с «Ок/Изменить»
    if step in (STEP_MER_CTX, STEP_MER_EMO, STEP_MER_THO, STEP_MER_BEH):
        # Пользователь вводит правку или значение
        mer = st["data"].get("mer", {})
        mer[step] = text_in.strip()
        st["data"]["mer"] = mer
        # Следующее поле или чек-пойнт
        idx = MER_ORDER.index(step)
        if idx + 1 < len(MER_ORDER):
            nxt = MER_ORDER[idx + 1]
            save_state(uid, INTENT_ERR, nxt, st["data"])
            # Спросим/подтвердим следующее поле
            prompts = {
                STEP_MER_EMO: ("Два-три слова — что всплыло внутри (досада/страх упустить/напряжение)?", "Чувства"),
                STEP_MER_THO: ("Какие фразы звучали в голове? 2–3 коротких пункта.", "Мысли"),
                STEP_MER_BEH: ("Что сделал руками? Одно предложение.", "Действия"),
            }
            pr, lbl = prompts.get(nxt, ("Продолжим.", "Поле"))
            ask_or_confirm_field(uid, st, nxt, pr, lbl)
        else:
            # Все четыре поля заполнены → чек-пойнт
            save_state(uid, INTENT_ERR, STEP_MER_RECAP, st["data"])
            offer_mer_to_tote_checkpoint(uid, st)
        return

    # 2.5) MER recap (чек-пойнт)
    if step == STEP_MER_RECAP:
        # Если пришёл текстом ответ — игнорируем, ждём кнопки, но на всякий отреагируем мягко
        bot.send_message(uid, "Если готов, нажми «Да» ниже — перейдём к плану действий.", reply_markup=types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Да", callback_data="mer_recap_yes"),
            types.InlineKeyboardButton("Назад, поправлю", callback_data="mer_recap_no"),
        ))
        return

    # 3) Goal
    if step == STEP_GOAL:
        new_data = st["data"].copy()
        new_data["goal"] = text_in.strip()
        save_state(uid, INTENT_ERR, STEP_TOTE_OPS, new_data)
        bot.send_message(uid, "Дай 2–3 микро-шага для ближайших трёх сделок (списком, коротко).")
        return

    # 4) TOTE - ops
    if step == STEP_TOTE_OPS:
        tote = st["data"].get("tote", {})
        tote["ops"] = text_in.strip()
        st["data"]["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_TEST, st["data"])
        bot.send_message(uid, "Как поймёшь, что получилось? Один простой критерий.")
        return

    # 5) TOTE - test
    if step == STEP_TOTE_TEST:
        tote = st["data"].get("tote", {})
        tote["test"] = text_in.strip()
        st["data"]["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_EXIT, st["data"])
        bot.send_message(uid, "Если критерий не выполнился — что сделаешь по шагам?")
        return

    # 6) TOTE - exit + финальная сводка
    if step == STEP_TOTE_EXIT:
        tote = st["data"].get("tote", {})
        tote["exit"] = text_in.strip()
        st["data"]["tote"] = tote

        mer = st["data"].get('mer', {})
        summary = [
            "<b>Сводка по кейсу</b>",
            f"Ошибка: {st['data'].get('problem_hypothesis', st['data'].get('error_description', '—'))}",
            f"Контекст: {mer.get(STEP_MER_CTX, '—')}",
            f"Чувства: {mer.get(STEP_MER_EMO, '—')}",
            f"Мысли: {mer.get(STEP_MER_THO, '—')}",
            f"Действия: {mer.get(STEP_MER_BEH, '—')}",
            f"Цель: {st['data'].get('goal', '—')}",
            f"Шаги: {st['data'].get('tote', {}).get('ops', '—')}",
            f"Проверка: {st['data'].get('tote', {}).get('test', '—')}",
            f"Если не вышло: {st['data'].get('tote', {}).get('exit', '—')}",
        ]
        st["data"]["last_structural_summary"] = "\n".join(summary)
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
        kb = types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Добавить в фокус недели", callback_data="save_focus_week"),
            types.InlineKeyboardButton("Дальше", callback_data="exit_to_free"),
        )
        bot.send_message(uid, "\n".join(summary), reply_markup=kb)
        return

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
        # сначала сформулируем гипотезу и спросим согласие
        last_user_texts = [h["content"] for h in history if h.get("role") == "user"][-2:]
        hypothesis = make_problem_hypothesis(" ".join(last_user_texts))
        st["data"]["problem_hypothesis"] = hypothesis
        save_state(uid, INTENT_ERR, STEP_CONFIRM_PROBLEM, st["data"])
        offer_problem_confirmation(uid, hypothesis)
    elif code == "start_help":
        bot.send_message(uid, "План: 1) быстрый разбор ошибки, 2) фокус недели, 3) скелет ТС. С чего начнём?", reply_markup=MAIN_MENU)
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

    # Подтверждение проблемы
    if data == "confirm_problem_yes":
        save_state(uid, INTENT_ERR, STEP_ERR_DESCR, st["data"])
        hyp = st["data"].get("problem_hypothesis", "ошибка в последнем кейсе")
        bot.send_message(uid, f"Берём в работу: <b>{hyp}</b>.\nОпиши последний случай: где/когда, вход/план, где отступил, чем закончилось.")
        return
    if data == "confirm_problem_no":
        bot.send_message(uid, "Окей. Сформулируй, как ты это видишь, одной фразой — и начнём разбирать.")
        return

    # Ок/Изменить для MER полей
    if data.startswith("field_ok:"):
        field = data.split(":", 1)[1]
        # перейти к следующему шагу как будто пользователь ответил
        next_step_map = {
            STEP_MER_CTX: STEP_MER_EMO,
            STEP_MER_EMO: STEP_MER_THO,
            STEP_MER_THO: STEP_MER_BEH,
            STEP_MER_BEH: STEP_MER_RECAP
        }
        nxt = next_step_map.get(field, STEP_MER_RECAP)
        save_state(uid, INTENT_ERR, nxt, st["data"])
        if nxt in (STEP_MER_EMO, STEP_MER_THO, STEP_MER_BEH):
            prompts = {
                STEP_MER_EMO: ("Два-три слова — что всплыло внутри (досада/страх упустить/напряжение)?", "Чувства"),
                STEP_MER_THO: ("Какие фразы звучали в голове? 2–3 коротких пункта.", "Мысли"),
                STEP_MER_BEH: ("Что сделал руками? Одно предложение.", "Действия"),
            }
            pr, lbl = prompts[nxt]
            ask_or_confirm_field(uid, st, nxt, pr, lbl)
        else:
            offer_mer_to_tote_checkpoint(uid, st)
        return
    if data.startswith("field_edit:"):
        field = data.split(":", 1)[1]
        # очистим поле и попросим ввести заново
        st["data"].setdefault("mer", {}).pop(field, None)
        save_state(uid, INTENT_ERR, field, st["data"])
        prompts = {
            STEP_MER_CTX: ("Где и когда это было? Один-два штриха: инструмент/биржа/таймфрейм.", "Контекст"),
            STEP_MER_EMO: ("Два-три слова — что всплыло внутри (досада/страх упустить/напряжение)?", "Чувства"),
            STEP_MER_THO: ("Какие фразы звучали в голове? 2–3 коротких пункта.", "Мысли"),
            STEP_MER_BEH: ("Что сделал руками? Одно предложение.", "Действия"),
        }
        pr, _ = prompts[field]
        bot.send_message(uid, pr)
        return

    # Recap MER → перейти к плану
    if data == "mer_recap_yes":
        save_state(uid, INTENT_ERR, STEP_GOAL, st["data"])
        bot.send_message(uid, "Как хочешь действовать вместо прежнего (одна фраза, утвердительно)?")
        return
    if data == "mer_recap_no":
        # вернёмся к первому полю на правку
        save_state(uid, INTENT_ERR, STEP_MER_CTX, st["data"])
        ask_or_confirm_field(uid, st, STEP_MER_CTX,
                             "Где и когда это было? Один-два штриха: инструмент/биржа/таймфрейм.",
                             "Контекст")
        return

    # Финал после TOTE
    if data == "save_focus_week":
        # тут просто подтверждаем — фактического отдельного хранилища «фокус недели» пока нет
        bot.send_message(uid, "Сохранил как фокус недели. Вернёмся к этому в конце недели и проверим прогресс.", reply_markup=MAIN_MENU)
        return
    if data == "exit_to_free":
        bot.send_message(uid, "Окей. Готов идти дальше.", reply_markup=MAIN_MENU)
        return

# ========= HTTP =========
def _now_iso():
    return datetime.now(timezone.utc).isoformat()

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

try:
    th = threading.Thread(target=cleanup_scheduler, daemon=True)
    th.start()
except Exception as e:
    log.error("Can't start cleanup thread: %s", e)

# ========= Dev run =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
