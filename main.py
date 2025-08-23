import os
import logging
import re
from flask import Flask, jsonify
from telebot import TeleBot, types
from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ---------- ENV ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL   = os.getenv("DATABASE_URL")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets")
if not OPENAI_API_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets")

# ---------- OPENAI (на будущее) ----------
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- DB ----------
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        with engine.begin() as conn:
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_state (
                user_id BIGINT PRIMARY KEY,
                intent  TEXT,
                data    JSONB
            );
            """))
        logging.info("DB connected & migrated")
    except OperationalError as e:
        logging.warning(f"DB not available yet: {e}")
        engine = None
else:
    logging.info("DATABASE_URL не задан — работаем без БД")

def save_state(user_id: int, intent: str, data: dict | None = None):
    if not engine:
        return
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO user_state (user_id, intent, data)
            VALUES (:uid, :intent, COALESCE(:data, '{}'::jsonb))
            ON CONFLICT (user_id) DO UPDATE
            SET intent = EXCLUDED.intent,
                data   = EXCLUDED.data
        """), {"uid": user_id, "intent": intent, "data": data})

# ---------- TELEGRAM ----------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

# Главное меню
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

# /start, /menu, /reset
@bot.message_handler(commands=["start", "menu", "reset"])
def cmd_start(m):
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник *Innertrade*.\nВыбери кнопку или напиши текст.\nКоманды: /ping /reset",
        reply_markup=main_menu()
    )
    save_state(m.from_user.id, intent="idle")

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

# ---------- ИНТЕНТЫ ----------
def intent_error(m):
    save_state(m.from_user.id, "error")
    bot.send_message(
        m.chat.id,
        "Разберём через *MERCEDES + TOTE*.\n\n"
        "*M* Мотивация?\n*E* Эмоции?\n*R* Результат?\n*C* Контекст?\n*E* Эффект?\n*D* Действия?\n*S* Стратегия?\n\n"
        "*T* Test — что пошло не так?\n*O* Operate — что сделал?\n*T* Test — результат?\n*E* Evolve — что изменишь?",
        reply_markup=main_menu()
    )

def intent_strategy(m):
    save_state(m.from_user.id, "strategy")
    bot.send_message(
        m.chat.id,
        "Ок, собираем ТС по конструктору:\n"
        "1) Цели\n2) Стиль (дневной/свинг/позиционный)\n"
        "3) Рынки/инструменты\n4) Правила входа/выхода\n"
        "5) Риск (%, стоп)\n6) Сопровождение\n7) Тестирование (история/демо)",
        reply_markup=main_menu()
    )

def intent_passport(m):
    save_state(m.from_user.id, "passport")
    bot.send_message(
        m.chat.id,
        "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?",
        reply_markup=main_menu()
    )

def intent_week_panel(m):
    save_state(m.from_user.id, "week_panel")
    bot.send_message(
        m.chat.id,
        "Панель недели:\n• Фокус недели\n• План (3 шага)\n• Лимиты\n• Ритуалы\n• Короткая ретро в конце недели",
        reply_markup=main_menu()
    )

def intent_panic(m):
    save_state(m.from_user.id, "panic")
    bot.send_message(
        m.chat.id,
        "🛑 Стоп-протокол «поплыл»:\n"
        "1) Пауза 2 мин\n"
        "2) Закрой график/терминал\n"
        "3) 10 медленных вдохов\n"
        "4) Запиши триггер: *что* выбило\n"
        "5) Действуй по плану сделки или закрой по правилу\n"
        "_Вернись к лимитам и чек-листу перед следующим входом_",
        reply_markup=main_menu()
    )

def intent_start_help(m):
    save_state(m.from_user.id, "start_help")
    bot.send_message(
        m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\n"
        "С чего начнём — паспорт или фокус недели?",
        reply_markup=main_menu()
    )

# ---------- ХЕНДЛЕРЫ ДЛЯ КНОПОК ----------
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def intent_error_btn(m): intent_error(m)

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def intent_strategy_btn(m): intent_strategy(m)

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def intent_passport_btn(m): intent_passport(m)

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def intent_week_panel_btn(m): intent_week_panel(m)

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def intent_panic_btn(m): intent_panic(m)

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def intent_start_help_btn(m): intent_start_help(m)

# ---------- УМНЫЙ РОУТЕР ДЛЯ СВОБОДНОГО ТЕКСТА ----------
def detect_intent(text: str) -> str | None:
    t = text.lower()
    # убрать лишние пробелы и базовую пунктуацию
    t = re.sub(r"[^\w\sёа-я-]", " ", t)  # простая очистка
    t = re.sub(r"\s+", " ", t).strip()

    if any(k in t for k in ["поплыл", "паника", "panic", "экстренн"]):
        return "panic"
    if any(k in t for k in ["ошибк"]):  # ошибка/ошибку/ошибки
        return "error"
    if any(k in t for k in ["стратег"]):
        return "strategy"
    if any(k in t for k in ["паспорт"]):
        return "passport"
    if any(k in t for k in ["панел", "недел"]):
        return "week_panel"
    if any(k in t for k in ["не знаю", "с чего начать", "начать не знаю"]):
        return "start_help"
    return None

@bot.message_handler(content_types=["text"])
def router_or_fallback(m):
    intent = detect_intent(m.text or "")
    if intent == "panic":
        return intent_panic(m)
    if intent == "error":
        return intent_error(m)
    if intent == "strategy":
        return intent_strategy(m)
    if intent == "passport":
        return intent_passport(m)
    if intent == "week_panel":
        return intent_week_panel(m)
    if intent == "start_help":
        return intent_start_help(m)

    # Фолбэк — если ничего не распознали
    bot.send_message(
        m.chat.id,
        "Принял. Чтобы было быстрее, выбери пункт в меню ниже или напиши /menu.",
        reply_markup=main_menu()
    )

# ---------- KEEPALIVE для Render ----------
app = Flask(__name__)

@app.route("/")
def root():
    return "OK v5"

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

def start_polling():
    try:
        bot.remove_webhook()
        logging.info("Webhook removed (ok)")
    except Exception as e:
        logging.warning(f"Webhook remove warn: {e}")
    logging.info("Starting polling…")
    bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)

if __name__ == "__main__":
    import threading
    t = threading.Thread(target=start_polling, daemon=True)
    t.start()
    port = int(os.getenv("PORT", "10000"))
    logging.info("Starting keepalive web server…")
    app.run(host="0.0.0.0", port=port)
