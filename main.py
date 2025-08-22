# main.py
import os
import logging
from collections import deque

from flask import Flask
import telebot
from telebot import types
from openai import OpenAI

# ====== Env ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", "10000"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в переменных окружения")
if not OPENAI_API_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в переменных окружения")

# ====== Logs ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

# ====== OpenAI client (новый SDK) ======
client = OpenAI(api_key=OPENAI_API_KEY)

# ====== Telegram bot ======
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# на всякий случай снимем вебхук (мы используем polling)
try:
    bot.remove_webhook()
    logging.info("Webhook removed (ok)")
except Exception as e:
    logging.warning(f"Webhook remove warn: {e}")

# ====== Простая «память» диалога (per-user)
# deque с ограничением, чтобы не рос бесконечно
history = {}  # uid -> deque of messages (dict role/content)
MAX_TURNS = 20

def ensure_history(uid: int):
    if uid not in history:
        history[uid] = deque(maxlen=MAX_TURNS)

def ask_gpt(uid: int, text: str) -> str:
    """Вызов Chat Completions через новый SDK."""
    ensure_history(uid)
    msgs = list(history[uid])  # скопируем в список
    msgs.append({"role": "user", "content": text})

    # вызов модели
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.5,
        messages=msgs
    )
    reply = (resp.choices[0].message.content or "").strip()

    # обновим историю
    history[uid].append({"role": "user", "content": text})
    history[uid].append({"role": "assistant", "content": reply})
    return reply

def send_long(chat_id: int, text: str):
    """Отправка длинных сообщений чанками. Без reply_to!"""
    MAX = 3500
    for i in range(0, len(text), MAX):
        bot.send_message(chat_id, text[i:i+MAX])

# ====== Команды ======
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    history[uid] = deque(maxlen=MAX_TURNS)

    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("Модуль 1"), types.KeyboardButton("Модуль 2"))
    kb.row(types.KeyboardButton("Чек-лист"), types.KeyboardButton("Фиксация"), types.KeyboardButton("Сброс"))

    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник Innertrade.\n"
        "Выбери кнопку или напиши текст.\n"
        "Команды: /ping /reset",
        reply_markup=kb
    )

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    # только "pong" — без лишнего текста
    bot.reply_to(m, "pong")

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    history[m.from_user.id] = deque(maxlen=MAX_TURNS)
    bot.reply_to(m, "Контекст очищен.")

# ====== Кнопки ======
BUTTON_SET = {"Модуль 1", "Модуль 2", "Чек-лист", "Фиксация", "Сброс"}
ALIASES = {
    "Модуль 1": "Готов",          # якорь под наши сценарии
    "Модуль 2": "ТС",             # якорь под наши сценарии
    "Чек-лист": "чеклист",
    "Фиксация": "фиксация",
}

@bot.message_handler(func=lambda x: isinstance(x.text, str) and x.text.strip() in BUTTON_SET)
def on_buttons(m):
    uid = m.from_user.id
    t = m.text.strip()

    if t == "Сброс":
        history[uid] = deque(maxlen=MAX_TURNS)
        bot.send_message(m.chat.id, "Контекст очищен. Нажми «Модуль 1» или «Модуль 2».")
        return

    prompt = ALIASES.get(t, t)
    try:
        reply = ask_gpt(uid, prompt)
    except Exception as e:
        logging.exception("ask_gpt error (buttons)")
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply)

# ====== Любой текст ======
@bot.message_handler(func=lambda _: True)
def on_text(m):
    uid = m.from_user.id
    text = (m.text or "").strip()
    if not text:
        return
    try:
        reply = ask_gpt(uid, text)
    except Exception as e:
        logging.exception("ask_gpt error (text)")
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply)

# ====== Keepalive HTTP (для Render) ======
app = Flask(__name__)

@app.route("/", methods=["GET", "HEAD"])
def home():
    return "OK", 200

@app.route("/health", methods=["GET"])
def health():
    # UptimeRobot будет бить сюда и ожидать "pong"
    return "pong", 200

if __name__ == "__main__":
    logging.info("Starting keepalive web server…")
    # Flask в отдельном треде поднимать не нужно — Render запускает один процесс.
    # Он слушает порт, а бот крутится параллельно через polling в другом треде.
    # Поэтому запустим Flask в отдельном треде руками:
    import threading
    def run_flask():
        app.run(host="0.0.0.0", port=PORT, debug=False)

    threading.Thread(target=run_flask, daemon=True).start()

    logging.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
