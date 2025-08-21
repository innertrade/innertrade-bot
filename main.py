import os
import logging
import telebot
from telebot import types
from openai import OpenAI
from flask import Flask

# ====== Ключи из Secrets ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets")

# OpenAI client (новый SDK)
client = OpenAI(api_key=OPENAI_KEY)

# ====== Логи ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ====== Снять webhook на всякий случай ======
try:
    bot.remove_webhook()
    logging.info("Webhook removed (ok)")
except Exception as e:
    logging.warning(f"Webhook remove warn: {e}")

# ====== Память диалога ======
history = {}  # uid -> [{"role":"user"/"assistant","content":"..."}]

def ask_gpt(uid, text):
    """Вызов Chat Completions через новый SDK."""
    msgs = history.setdefault(uid, [])
    msgs.append({"role": "user", "content": text})

    resp = client.chat.completions.create(
        model="gpt-4o-mini",    # можно gpt-4o / gpt-4.1-mini и т.п.
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

# ====== /start ======
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    history[uid] = []
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("Модуль 1"), types.KeyboardButton("Модуль 2"))
    kb.row(types.KeyboardButton("Чек-лист"), types.KeyboardButton("Фиксация"), types.KeyboardButton("Сброс"))
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник Innertrade.\nВыбери кнопку или напиши текст.\nКоманды: /ping /reset",
        reply_markup=kb
    )

# ====== /ping (диагностика) ======
@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.reply_to(m, "pong ✅")

# ====== /reset ======
@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    history[m.from_user.id] = []
    bot.reply_to(m, "Контекст очищен.")

# ====== Кнопки ======
@bot.message_handler(func=lambda x: x.text in {"Модуль 1","Модуль 2","Чек-лист","Фиксация","Сброс"})
def on_buttons(m):
    uid = m.from_user.id
    t = (m.text or "").strip()
    if t == "Сброс":
        history[uid] = []
        bot.reply_to(m, "Контекст очищен. Нажми «Модуль 1» или «Модуль 2».")
        return
    alias = {"Модуль 1":"Готов", "Модуль 2":"ТС", "Чек-лист":"чеклист", "Фиксация":"фиксация"}
    try:
        reply = ask_gpt(uid, alias.get(t, t))
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

# ====== Keep-alive сервер для Render/UptimeRobot ======
app = Flask(__name__)

@app.route('/')
def index():
    return "OK"

@app.route('/health')
def health():
    return "pong"

if __name__ == "__main__":
    logging.info("Starting keepalive web server…")
    import threading
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8000)).start()

    logging.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
