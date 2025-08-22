import os
import logging
from flask import Flask
import telebot
from telebot import types
from openai import OpenAI

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ====== ENV ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")  # строка из Neon

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets")
if not DATABASE_URL:
    raise RuntimeError("Нет DATABASE_URL в Secrets")

# Переходим на psycopg v3: явно указываем драйвер
# Пример: postgresql://user:pass@host/db -> postgresql+psycopg://user:pass@host/db
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://"):]

# ====== OpenAI ======
client = OpenAI(api_key=OPENAI_KEY)

# ====== Логи ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ====== Бот ======
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# Снимаем webhook на всякий случай
try:
    bot.remove_webhook()
    logging.info("Webhook removed (ok)")
except Exception as e:
    logging.warning(f"Webhook remove warn: {e}")

# ====== Flask keepalive ======
app = Flask(__name__)

@app.route("/")
def root():
    return "OK", 200

@app.route("/health")
def health():
    # Проба БД
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db = "db_ok"
    except Exception as e:
        db = f"db_err: {e}"
    return f"pong | {db}", 200

# ====== DB (SQLAlchemy) ======
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

# ====== Память GPT-диалога в ОЗУ (на время процесса) ======
history = {}  # user_id -> list of messages

def ask_gpt(uid, text_in):
    msgs = history.setdefault(uid, [])
    msgs.append({"role": "user", "content": text_in})
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.5,
        messages=msgs
    )
    reply = (resp.choices[0].message.content or "").strip()
    msgs.append({"role": "assistant", "content": reply})
    return reply

def send_long(chat_id, text):
    MAX = 3500
    for i in range(0, len(text), MAX):
        bot.send_message(chat_id, text[i:i+MAX])

# ====== Меню/интенты ======
MAIN_MENU = [
    ["🚑 У меня ошибка", "🧩 Хочу стратегию"],
    ["🗓 Панель недели", "📄 Паспорт"],
    ["📊 Мой прогресс", "📚 Материалы"],
    ["💬 Поговорим", "🔄 Сброс"]
]

def show_menu(chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for row in MAIN_MENU:
        kb.row(*[types.KeyboardButton(x) for x in row])
    bot.send_message(
        chat_id,
        "Выбери направление:",
        reply_markup=kb
    )

# ====== Команды ======
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    history[uid] = []
    show_menu(m.chat.id)

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    history[m.from_user.id] = []
    bot.send_message(m.chat.id, "Контекст очищен.")
    show_menu(m.chat.id)

@bot.message_handler(commands=['menu'])
def cmd_menu(m):
    show_menu(m.chat.id)

# ====== Обработчик кнопок ======
BUTTON_ALIASES = {
    "🚑 У меня ошибка": "интент:ошибка",
    "🧩 Хочу стратегию": "интент:стратегия",
    "🗓 Панель недели": "интент:панель-недели",
    "📄 Паспорт": "интент:паспорт",
    "📊 Мой прогресс": "интент:прогресс",
    "📚 Материалы": "интент:материалы",
    "💬 Поговорим": "интент:свободный-диалог",
    "🔄 Сброс": "интент:сброс"
}

@bot.message_handler(func=lambda m: (m.text or "").strip() in BUTTON_ALIASES.keys())
def on_buttons(m):
    uid = m.from_user.id
    text = (m.text or "").strip()
    if text == "🔄 Сброс":
        history[uid] = []
        bot.send_message(m.chat.id, "Контекст очищен.")
        show_menu(m.chat.id)
        return
    try:
        reply = ask_gpt(uid, BUTTON_ALIASES[text])
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply)

# ====== Любой текст ======
@bot.message_handler(func=lambda _: True)
def on_text(m):
    uid = m.from_user.id
    try:
        reply = ask_gpt(uid, m.text or "")
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply)

if __name__ == "__main__":
    logging.info("Starting keepalive web server…")
    # Render поднимет Flask на $PORT; по умолчанию запустим локально
    port = int(os.getenv("PORT", "10000"))
    # Стартуем Flask в отдельном потоке — TeleBot polling в главном
    import threading
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True).start()

    logging.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
