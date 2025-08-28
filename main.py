# main.py
import os, json, logging, time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify, abort
from telebot import TeleBot, types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from openai import OpenAI

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("innertrade")

# ---------- ENV ----------
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
DATABASE_URL    = os.getenv("DATABASE_URL")
PUBLIC_URL      = os.getenv("PUBLIC_URL")         # например: https://innertrade-bot.onrender.com
WEBHOOK_PATH    = os.getenv("WEBHOOK_PATH", "webhook")
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET")      # X-Telegram-Bot-Api-Secret-Token

if not TELEGRAM_TOKEN: raise RuntimeError("TELEGRAM_TOKEN missing")
if not OPENAI_API_KEY: raise RuntimeError("OPENAI_API_KEY missing")
if not PUBLIC_URL:     raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if not WEBHOOK_SECRET: log.warning("WEBHOOK_SECRET not set (dev only). Set it in production.")

# ---------- OPENAI ----------
oa = OpenAI(api_key=OPENAI_API_KEY)

# ---------- DB ----------
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        with engine.begin() as conn:
            # Минимум того, что нужно здесь. Остальные таблицы мы уже накатывали миграцией.
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'course',
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            );
            """))
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_state (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                intent  TEXT,
                step    TEXT,
                data    JSONB,
                updated_at TIMESTAMPTZ DEFAULT now()
            );
            """))
        log.info("DB connected & minimal schema ensured")
    except OperationalError as e:
        log.warning(f"DB not available yet: {e}")
        engine = None
else:
    log.info("DATABASE_URL not set — running without DB")

def db_exec(sql: str, params: Optional[Dict[str, Any]] = None):
    if not engine: return None
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})

def ensure_user(uid: int):
    if not engine: return
    db_exec("INSERT INTO users(user_id) VALUES (:u) ON CONFLICT (user_id) DO NOTHING", {"u": uid})

def load_state(uid: int) -> Dict[str, Any]:
    if not engine:
        return {"intent": "greet", "step": None, "data": {}}
    row = db_exec("SELECT intent, step, data FROM user_state WHERE user_id = :u", {"u": uid}).fetchone()
    if not row:
        db_exec("""INSERT INTO user_state(user_id, intent, step, data)
                   VALUES (:u, 'greet', NULL, '{}'::jsonb)
                   ON CONFLICT (user_id) DO NOTHING""", {"u": uid})
        return {"intent": "greet", "step": None, "data": {}}
    intent, step, data = row
    return {"intent": intent, "step": step, "data": data or {}}

def save_state(uid: int, intent: Optional[str] = None, step: Optional[str] = None, patch: Optional[Dict[str, Any]] = None):
    if not engine: return
    st = load_state(uid)
    if intent is not None: st["intent"] = intent
    if step   is not None: st["step"]   = step
    if patch:
        base = st.get("data") or {}
        base.update(patch)
        st["data"] = base
    db_exec("""
        INSERT INTO user_state (user_id, intent, step, data, updated_at)
        VALUES (:u, :i, :s, COALESCE(:d, '{}'::jsonb), now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent=EXCLUDED.intent, step=EXCLUDED.step, data=EXCLUDED.data, updated_at=now()
    """, {"u": uid, "i": st["intent"], "s": st["step"], "d": json.dumps(st.get("data") or {})})

# ---------- БОТ ----------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Собрать ТС")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

# --- Утилиты диалога ---
START_ERROR_WORDS = {
    "разбор ошибки", "давай разберем", "давай разберём", "разберем", "разберём",
    "у меня ошибка", "ошибка", "поплыл", "экстренно"
}

def looks_like_error_free_text(t: str) -> bool:
    t = (t or "").lower()
    keys = ["ошиб", "просад", "наруша", "сует", "стоп", "тейк", "усредн", "раньше", "поздно", "слива"]
    return any(k in t for k in keys)

def build_inline_yesno(cb_yes: str, cb_no: str, text_yes="Да", text_no="Пока поговорим"):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(text_yes, callback_data=cb_yes),
           InlineKeyboardButton(text_no,  callback_data=cb_no))
    return kb

def summarize_for_user(texts: list[str]) -> str:
    # Мягкая краткая выжимка (одно предложение)
    joined = " ".join(texts)[-2000:]
    try:
        rsp = oa.responses.create(
            model="gpt-4.1-mini",
            input=f"Суммаризуй проблему трейдера в одном предложении, без советов, без клише. Текст: {joined}"
        )
        return rsp.output_text.strip()
    except Exception:
        return "Сформулирую так: есть трудности со следованием правилам и управлением эмоциями в сделке."

# --- Команды ---
@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m: types.Message):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="greet", step=None, patch={"chat_buf": [], "buf_turns": 0})
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я наставник *Innertrade*. Можем просто поговорить — напиши, что болит в торговле.\n"
        "Или выбери пункт ниже, если удобнее идти по шагам.",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["ping"])
def cmd_ping(m): bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    uid = m.from_user.id
    st = load_state(uid)
    bot.send_message(
        m.chat.id,
        "```\n" + json.dumps({
            "ok": True,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "intent": st.get("intent"),
            "step": st.get("step"),
            "db": "ok" if engine else "no-db"
        }, ensure_ascii=False, indent=2) + "\n```",
        parse_mode="Markdown"
    )

# --- INTENT КНОПКИ ---
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def btn_error(m):
    start_error_flow(m, from_button=True)

@bot.message_handler(func=lambda msg: msg.text == "🧩 Собрать ТС")
def btn_ts(m):
    uid = m.from_user.id
    ensure_user(uid); save_state(uid, intent="ts", step="intro")
    bot.send_message(m.chat.id, "Начнём конструктор ТС чуть позже — сейчас фокус на корректном разборе ошибки. 🤝", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def btn_passport(m):
    uid = m.from_user.id
    ensure_user(uid); save_state(uid, intent="passport", step="intro")
    bot.send_message(m.chat.id, "Открою паспорт позже. Пока улучшаем базовый диалог. 👌", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def btn_week(m):
    uid = m.from_user.id
    ensure_user(uid); save_state(uid, intent="week_panel", step="intro")
    bot.send_message(m.chat.id, "Панель недели подключим после логики разбора. 👍", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def btn_panic(m):
    uid = m.from_user.id
    ensure_user(uid)
    # мгновенный сценарий
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку\n3) 10 медленных вдохов\n"
        "4) Запиши триггер (что выбило)\n5) Вернись к плану или закрой по правилу",
        reply_markup=main_menu()
    )
    # и предложить разбор
    kb = build_inline_yesno("go_error", "stay_chat")
    bot.send_message(m.chat.id, "Хочешь потом коротко разобрать это по шагам?", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def btn_dontknow(m):
    uid = m.from_user.id
    ensure_user(uid); save_state(uid, intent="greet", step=None)
    bot.send_message(
        m.chat.id,
        "Предлагаю так: 1) коротко поговорим — что болит; 2) я предложу, с чего начать; 3) по желанию перейдём к разбору.\n"
        "Напиши, что мешает больше всего. ",
        reply_markup=main_menu()
    )

# --- CALLBACKS (inline «Да/Нет») ---
@bot.callback_query_handler(func=lambda call: call.data in {"go_error","stay_chat"})
def cb_switch(call):
    if call.data == "go_error":
        m = call.message
        start_error_flow(m, from_button=True)
    else:
        bot.answer_callback_query(call.id, "Ок, продолжим разговор свободно.")
        # ничего не меняем

# --- ОСНОВНОЙ СВОБОДНЫЙ ДИАЛОГ + МЯГКИЙ ПЕРЕХОД ---
@bot.message_handler(content_types=["text"])
def free_text(m: types.Message):
    uid = m.from_user.id
    ensure_user(uid)
    st = load_state(uid)
    text_in = (m.text or "").strip()

    # явные триггеры «разбор ошибки»
    low = text_in.lower()
    if low in START_ERROR_WORDS or any(w in low for w in START_ERROR_WORDS):
        return start_error_flow(m, from_button=False)

    # если уже в error-потоке — передать в обработчик шагов
    if st.get("intent") == "error":
        return error_flow_router(m, st)

    # свободный разговор: аккумулируем первые 2–3 реплики, затем предлагаем разбор
    data = st.get("data") or {}
    buf = data.get("chat_buf", [])
    turns = int(data.get("buf_turns", 0))
    buf.append(text_in)
    turns += 1
    save_state(uid, patch={"chat_buf": buf[-6:], "buf_turns": turns})

    # если уже звучит ошибка — можем предложить перейти
    if looks_like_error_free_text(text_in) and turns >= 2:
        summary = summarize_for_user(buf)
        kb = build_inline_yesno("go_error", "stay_chat", text_yes="Да, разберём", text_no="Пока поговорим")
        bot.send_message(
            m.chat.id,
            f"Сформулирую так: *{summary}*\nПерейдём к короткому разбору по шагам?",
            reply_markup=kb
        )
        return

    # иначе — поддерживаем диалог коротко
    try:
        rsp = oa.responses.create(
            model="gpt-4.1-mini",
            input=(
                "Отвечай кратко, эмпатично, 1–2 предложения, без общих поучений. "
                "Задай 1 уточняющий вопрос. Если человек описывает трудность в сделках, не навязывай методы; "
                "мягко спроси, что болит конкретнее."
                f"\nПользователь: {text_in}"
            )
        )
        out = rsp.output_text.strip()
    except Exception:
        out = "Понимаю. Можешь описать, в чём именно сейчас сложность в сделках?"

    bot.send_message(m.chat.id, out, reply_markup=main_menu())

# ---------- РАЗБОР ОШИБКИ (MERCEDES краткий) ----------
def start_error_flow(m: types.Message, from_button: bool):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="error", step="ask_error", patch={"mer": {}, "chat_buf": [], "buf_turns": 0})
    bot.send_message(
        m.chat.id,
        "Опиши *основную ошибку* 1–2 предложениями (естественно, как есть).",
        reply_markup=main_menu()
    )

def error_flow_router(m: types.Message, st: Dict[str, Any]):
    uid = m.from_user.id
    step = st.get("step")
    mer = (st.get("data") or {}).get("mer") or {}

    def ask(next_step: str, prompt: str):
        save_state(uid, step=next_step)
        bot.send_message(m.chat.id, prompt, reply_markup=main_menu())

    if step == "ask_error":
        mer["error_text"] = m.text.strip()
        save_state(uid, patch={"mer": mer})
        # согласование формулировки (одно подтверждение)
        bot.send_message(m.chat.id, f"Зафиксирую так: _{mer['error_text']}_\nОк?", reply_markup=build_inline_yesno("mer_ok","mer_edit","Да","Уточнить"))
        return

    # коллбеки подтверждения формулировки
@bot.callback_query_handler(func=lambda call: call.data in {"mer_ok","mer_edit"})
def cb_mer_ok(call):
    uid = call.from_user.id
    st = load_state(uid)
    mer = (st.get("data") or {}).get("mer") or {}
    if call.data == "mer_edit":
        save_state(uid, step="ask_error")
        bot.edit_message_text("Хорошо, поправь формулировку ошибки (1–2 предложения).", call.message.chat.id, call.message.message_id)
        return
    # ok → к блокам MERCEDES
    save_state(uid, step="ask_ctx", patch={"mer": mer})
    bot.edit_message_text("КОНТЕКСТ. Когда это обычно происходит? Что предшествует? (1–2 предложения)", call.message.chat.id, call.message.message_id)

@bot.message_handler(func=lambda m: load_state(m.from_user.id).get("intent")=="error")
def error_steps(m: types.Message):
    uid = m.from_user.id
    st = load_state(uid)
    step = st.get("step")
    mer = (st.get("data") or {}).get("mer") or {}

    def set_and_ask(next_step: str, field: str, value: str, prompt: str):
        mer[field] = value.strip()
        save_state(uid, step=next_step, patch={"mer": mer})
        bot.send_message(m.chat.id, prompt, reply_markup=main_menu())

    if step == "ask_ctx":
        return set_and_ask("ask_em", "context", m.text, "ЭМОЦИИ. Что чувствуешь в момент ошибки? (пара слов)")

    if step == "ask_em":
        return set_and_ask("ask_th", "emotions", m.text, "МЫСЛИ. Что говоришь себе в этот момент? (1–2 фразы)")

    if step == "ask_th":
        return set_and_ask("ask_bhv", "thoughts", m.text, "ПОВЕДЕНИЕ. Что именно делаешь? Опиши действия глаголами.")

    if step == "ask_bhv":
        mer["behavior"] = m.text.strip()
        # резюме + новый вектор
        summary = (
            f"*Резюме:*\n"
            f"Ошибка: _{mer.get('error_text','')}_\n"
            f"Контекст: {mer.get('context','')}\n"
            f"Эмоции: {mer.get('emotions','')}\n"
            f"Мысли: {mer.get('thoughts','')}\n"
            f"Поведение: {mer.get('behavior','')}\n\n"
            f"Сформулируем *новую цель* одним предложением (что хочешь делать вместо прежнего поведения)?"
        )
        save_state(uid, step="ask_goal", patch={"mer": mer})
        return bot.send_message(m.chat.id, summary, reply_markup=main_menu())

    if step == "ask_goal":
        mer["new_goal"] = m.text.strip()
        save_state(uid, step="ask_ops", patch={"mer": mer})
        return bot.send_message(m.chat.id, "Какие *2–3 шага* помогут держаться этой цели в ближайших 3 сделках?", reply_markup=main_menu())

    if step == "ask_ops":
        mer["ops"] = m.text.strip()
        # done урока 1 (мягкая версия). В проде тут — insert в errors
        save_state(uid, intent="greet", step=None, patch={"last_mercedes": mer})
        bot.send_message(
            m.chat.id,
            "Готово. Сохранил краткий разбор. При желании добавим это в недельный фокус позже. "
            "Можем продолжить разговор или выбрать пункт ниже.",
            reply_markup=main_menu()
        )
        return

# ---------- FLASK / WEBHOOK ----------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return jsonify({"status":"ok","time": datetime.now(timezone.utc).isoformat()})

MAX_BODY = 1_000_000

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            abort(401)
    if request.content_length and request.content_length > MAX_BODY:
        abort(413)
    try:
        upd = Update.de_json(request.get_json(force=True), bot)
        bot.process_new_updates([upd])
    except Exception as e:
        log.exception("webhook error: %s", e)
        abort(500)
    return "OK"

# ---------- ЛОКАЛЬНЫЙ СТАРТ (polling) ----------
if __name__ == "__main__":
    # Для Render используем вебхук; локально можно включить polling (раскомментить).
    # bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
