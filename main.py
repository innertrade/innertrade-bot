import os
import logging
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

# ---------- OPENAI ----------
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- DB (опционально) ----------
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_state (
                user_id BIGINT PRIMARY KEY,
                intent TEXT,
                data JSONB
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

# Клавиатура главного меню
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

# /start и меню
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

# ---------- ХЕНДЛЕРЫ ИНТЕНТОВ ----------
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def intent_error(m):
    save_state(m.from_user.id, "error")
    bot.send_message(
        m.chat.id,
        "Давай разберём через *MERCEDES + TOTE*.\n\n"
        "*M* Мотивация?\n*E* Эмоции?\n*R* Результат?\n*C* Контекст?\n*E* Эффект?\n*D* Действия?\n*S* Стратегия?\n\n"
        "*T* Test — что пошло не так?\n*O* Operate — что сделал?\n*T* Test — результат?\n*E* Evolve — что изменишь?",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
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

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def intent_passport(m):
    save_state(m.from_user.id, "passport")
    bot.send_message(
        m.chat.id,
        "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def intent_week_panel(m):
    save_state(m.from_user.id, "week_panel")
    bot.send_message(
        m.chat.id,
        "Панель недели:\n• Фокус недели\n• План (3 шага)\n• Лимиты\n• Ритуалы\n• Короткая ретро в конце недели",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def intent_panic(m):
    save_state(m.from_user.id, "panic")
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку с графиком\n3) Сделай 10 медленных вдохов\n"
        "4) Запиши триггер (что именно выбило)\n5) Вернись к плану сделки или закрой позицию по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def intent_start_help(m):
    save_state(m.from_user.id, "start_help")
    bot.send_message(
        m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\n"
        "С чего начнём — паспорт или фокус недели?",
        reply_markup=main_menu()
    )

# Фолбэк: если пользователь пишет текст мимо кнопок
@bot.message_handler(content_types=["text"])
def fallback(m):
    # Короткий, «закрытый» ответ, без ухода в общий чат-бот
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
    # Удалим вебхук на всякий случай, чтобы polling не конфликтовал
    try:
        bot.remove_webhook()
        logging.info("Webhook removed (ok)")
    except Exception as e:
        logging.warning(f"Webhook remove warn: {e}")
    logging.info("Starting polling…")
    bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)

if __name__ == "__main__":
    # Запускаем polling в отдельном потоке + Flask для Render
    import threading
    t = threading.Thread(target=start_polling, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "10000"))
    logging.info("Starting keepalive web server…")
    app.run(host="0.0.0.0", port=port)
