import os, logging, json, threading
from datetime import datetime, timezone
from flask import Flask, request, abort, jsonify
from telebot import TeleBot, types
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ---------- ENV ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # пока не используем, но оставляем
DATABASE_URL   = os.getenv("DATABASE_URL")
PUBLIC_URL     = os.getenv("PUBLIC_URL")  # https://innertrade-bot.onrender.com
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "wbhk_9t3x")
TG_SECRET      = os.getenv("TG_WEBHOOK_SECRET")

if not TELEGRAM_TOKEN: raise RuntimeError("TELEGRAM_TOKEN missing")
if not PUBLIC_URL:     raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if not TG_SECRET:      raise RuntimeError("TG_WEBHOOK_SECRET missing")

# ---------- DB ----------
engine = create_engine(DATABASE_URL, pool_pre_ping=True) if DATABASE_URL else None

def db_exec(sql: str, params: dict | None = None, fetch: bool = False):
    if not engine:
        return None
    try:
        with engine.begin() as conn:
            res = conn.execute(text(sql), params or {})
            return res.fetchall() if fetch else None
    except SQLAlchemyError as e:
        logging.exception("DB error")
        return None

def ensure_user(uid: int):
    db_exec("""
        INSERT INTO users(user_id) VALUES (:uid)
        ON CONFLICT (user_id) DO NOTHING
    """, {"uid": uid})

def clear_state(uid: int):
    ensure_user(uid)
    db_exec("""
        INSERT INTO user_state(user_id, intent, step, data, updated_at)
        VALUES (:uid, 'greet', NULL, '{}'::jsonb, now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent='greet', step=NULL, data='{}'::jsonb, updated_at=now()
    """, {"uid": uid})

def set_state(uid: int, intent: str, step: str | None = None, data: dict | None = None):
    ensure_user(uid)
    db_exec("""
        INSERT INTO user_state(user_id, intent, step, data, updated_at)
        VALUES (:uid, :intent, :step, COALESCE(:data, '{}'::jsonb), now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent=:intent, step=:step, data=COALESCE(:data, '{}'::jsonb), updated_at=now()
    """, {"uid": uid, "intent": intent, "step": step, "data": json.dumps(data or {})})

def get_state(uid: int):
    rows = db_exec("SELECT intent, step, data FROM user_state WHERE user_id=:uid", {"uid": uid}, fetch=True)
    if rows:
        intent, step, data = rows[0]
        return intent, step, data or {}
    return "greet", None, {}

# ---------- TELEGRAM ----------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

def safe_handler(fn):
    def wrap(message):
        try:
            return fn(message)
        except Exception:
            logging.exception("Handler error")
            try:
                bot.send_message(message.chat.id, "⚠️ Ошибка на моей стороне. Попробуйте ещё раз /reset")
            except Exception:
                pass
    return wrap

# ---------- COMMANDS ----------
@bot.message_handler(commands=["ping"])
@safe_handler
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
@safe_handler
def cmd_status(m):
    uid = m.from_user.id
    intent, step, _ = get_state(uid)
    db_ok = "ok" if engine else "no-db"
    payload = {
        "ok": True,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "intent": intent,
        "step": step,
        "db": db_ok
    }
    bot.send_message(m.chat.id, f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```", parse_mode="Markdown")

@bot.message_handler(commands=["start", "menu"])
@safe_handler
def cmd_start(m):
    uid = m.from_user.id
    clear_state(uid)
    first_name = (m.from_user.first_name or "").strip() or "друг"
    bot.send_message(
        m.chat.id,
        f"👋 Привет, {first_name}! Можем просто поговорить — что болит в торговле — или выбери пункт ниже.",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["reset"])
@safe_handler
def cmd_reset(m):
    uid = m.from_user.id
    clear_state(uid)
    first_name = (m.from_user.first_name or "").strip() or "друг"
    bot.send_message(
        m.chat.id,
        f"🔄 Сбросил контекст.\nПривет, {first_name}! Можем просто поговорить — что болит в торговле — или выбери пункт ниже.",
        reply_markup=main_menu()
    )

# ---------- INTENTS (кнопки) ----------
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
@safe_handler
def intent_error_btn(m):
    set_state(m.from_user.id, "error", "ask_error")
    bot.send_message(
        m.chat.id,
        "Опиши основную ошибку 1–2 предложениями (как ты её делаешь)."
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
@safe_handler
def intent_strategy_btn(m):
    set_state(m.from_user.id, "strategy", None)
    bot.send_message(
        m.chat.id,
        "Ок, соберём конструктор ТС:\n1) стиль и рынки\n2) вход\n3) стоп/сопровождение/выход\n4) риск\nНачнём со стиля и рынков.",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
@safe_handler
def intent_passport_btn(m):
    set_state(m.from_user.id, "passport", "start")
    bot.send_message(m.chat.id, "Паспорт трейдера: какие рынки/инструменты торгуешь?")

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
@safe_handler
def intent_week_btn(m):
    set_state(m.from_user.id, "week_panel", "focus")
    bot.send_message(m.chat.id, "Панель недели: какой фокус на ближайшие 5–7 дней? (одно короткое предложение)")

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
@safe_handler
def intent_panic_btn(m):
    set_state(m.from_user.id, "panic", None)
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку\n3) 10 медленных вдохов\n4) Запиши триггер\n5) Вернись к плану сделки или закрой по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
@safe_handler
def intent_start_help_btn(m):
    set_state(m.from_user.id, "start_help", None)
    bot.send_message(
        m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\nС чего начнём — паспорт или фокус недели?",
        reply_markup=main_menu()
    )

# ---------- TEXT FLOW ----------
@bot.message_handler(content_types=["text"])
@safe_handler
def text_router(m):
    uid = m.from_user.id
    intent, step, data = get_state(uid)

    # Простейшая логика «ошибка»
    if intent == "error":
        if step == "ask_error":
            txt = (m.text or "").strip()
            if len(txt) < 5:
                bot.send_message(m.chat.id, "Дай, пожалуйста, чуток конкретики (1–2 предложения).")
                return
            # здесь можно сохранить в errors.error_text
            set_state(uid, "error", "mercedes_context", {"error": txt})
            bot.send_message(m.chat.id, "Принял. КОНТЕКСТ: когда это чаще случается? (коротко)")
            return
        elif step == "mercedes_context":
            data["context"] = (m.text or "").strip()
            set_state(uid, "error", "mercedes_emotions", data)
            bot.send_message(m.chat.id, "ЭМОЦИИ: что чувствуешь в момент ошибки? (несколько слов)")
            return
        elif step == "mercedes_emotions":
            data["emotions"] = (m.text or "").strip()
            set_state(uid, "error", "mercedes_thoughts", data)
            bot.send_message(m.chat.id, "МЫСЛИ: что говоришь себе в этот момент? (1–2 фразы)")
            return
        elif step == "mercedes_thoughts":
            data["thoughts"] = (m.text or "").strip()
            set_state(uid, "error", "mercedes_behavior", data)
            bot.send_message(m.chat.id, "ПОВЕДЕНИЕ: что конкретно делаешь? (1–2 предложения)")
            return
        elif step == "mercedes_behavior":
            data["behavior"] = (m.text or "").strip()
            # краткое резюме
            summary = (
                f"Резюме:\n• Ошибка: {data.get('error')}\n"
                f"• Контекст: {data.get('context')}\n"
                f"• Эмоции: {data.get('emotions')}\n"
                f"• Мысли: {data.get('thoughts')}\n"
                f"• Поведение: {data.get('behavior')}"
            )
            set_state(uid, "error", "ask_goal", data)
            bot.send_message(m.chat.id, summary + "\n\nСформулируем новую цель одним предложением (что хочешь делать вместо прежнего поведения)?")
            return
        elif step == "ask_goal":
            data["goal"] = (m.text or "").strip()
            set_state(uid, "error", "ask_ops", data)
            bot.send_message(m.chat.id, "Какие 2–3 шага помогут держаться этой цели в ближайших 3 сделках?")
            return
        elif step == "ask_ops":
            data["ops"] = (m.text or "").strip()
            set_state(uid, "error", None, data)
            bot.send_message(m.chat.id, "Готово. Можем добавить это в недельный фокус позже.", reply_markup=main_menu())
            return

    # Если ни один сценарий не активен — мягкий ответ + меню
    bot.send_message(m.chat.id, "Принял. Можем поговорить свободно или выбрать пункт в меню ниже.", reply_markup=main_menu())

# ---------- FLASK / WEBHOOK ----------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_SECRET:
        abort(401)
    cl = request.content_length or 0
    if cl > 1_000_000:
        abort(413)
    update = request.get_data(cache=False, as_text=True)
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return "OK"

def setup_webhook():
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    payload = {
        "url": f"{PUBLIC_URL}/{WEBHOOK_PATH}",
        "secret_token": TG_SECRET,
        "allowed_updates": json.dumps(["message","callback_query"]),
        "drop_pending_updates": True
    }
    r = requests.post(url, data=payload, timeout=10)
    logging.info("setWebhook: %s", r.text)

if __name__ == "__main__":
    # Настраиваем вебхук при старте
    try:
        setup_webhook()
    except Exception:
        logging.exception("setWebhook failed")

    port = int(os.getenv("PORT", "10000"))
    logging.info("Starting web server on %s …", port)
    app.run(host="0.0.0.0", port=port)
