import os
import logging
from threading import Thread

import telebot
from telebot import types
from openai import OpenAI  # новый SDK
from flask import Flask

# ====== Ключи из Secrets/Env ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets/Env")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets/Env")

# ====== OpenAI client ======
client = OpenAI(api_key=OPENAI_KEY)

# ====== Логи ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ====== Снять webhook на всякий случай (если перезапускали) ======
try:
    bot.remove_webhook()
    logging.info("Webhook removed (ok)")
except Exception as e:
    logging.warning(f"Webhook remove warn: {e}")

# ====== Память диалога ======
history = {}          # uid -> [{"role":"user"/"assistant","content":"..."}]
HARD_LIMIT_MSGS = 24  # ограничим длину контекста

def _trim(msgs):
    if len(msgs) > HARD_LIMIT_MSGS:
        del msgs[:-HARD_LIMIT_MSGS]

def ask_gpt(uid: int, text: str) -> str:
    msgs = history.setdefault(uid, [])
    msgs.append({"role": "user", "content": text})
    _trim(msgs)

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",   # можно gpt-4o / gpt-4.1-mini и т.п.
            temperature=0.5,
            messages=msgs
        )
        reply = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logging.exception("OpenAI error")
        reply = f"Ошибка GPT: {e}"

    msgs.append({"role": "assistant", "content": reply})
    _trim(msgs)
    return reply

def send_long(chat_id: int, text: str):
    """Отправка длинных сообщений БЕЗ reply_to (без «в ответ на…»)."""
    MAX = 3500
    for i in range(0, len(text), MAX):
        bot.send_message(chat_id, text[i:i+MAX])

# ====== Команды ======
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    history[uid] = []
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
    return  # не пропускаем дальше

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong ✅")
    return  # <--- важно: после /ping ничего больше не делаем

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    history[m.from_user.id] = []
    bot.send_message(m.chat.id, "Контекст очищен.")
    return

# ====== Кнопки ======
@bot.message_handler(func=lambda x: x.text in {"Модуль 1","Модуль 2","Чек-лист","Фиксация","Сброс"})
def on_buttons(m):
    uid = m.from_user.id
    t = (m.text or "").strip()

    if t == "Сброс":
        history[uid] = []
        bot.send_message(m.chat.id, "Контекст очищен. Нажми «Модуль 1» или «Модуль 2».")
        return

    alias = {
        "Модуль 1": "Готов",
        "Модуль 2": "ТС",
        "Чек-лист": "чеклист",
        "Фиксация": "фиксация",
    }
    reply = ask_gpt(uid, alias.get(t, t))
    send_long(m.chat.id, reply)
    return

# ====== Любой текст (кроме команд) ======
@bot.message_handler(func=lambda m: True)
def on_text(m):
    # Игнорируем команды (чтобы /ping и др. не попадали сюда)
    if m.text and m.text.startswith("/"):
        return
    uid = m.from_user.id
    reply = ask_gpt(uid, m.text or "")
    send_long(m.chat.id, reply)

# ====== Мини-веб-сервер для keep-alive ======
app = Flask(__name__)

@app.get("/")
def root():
    return "OK", 200

@app.get("/health")
def health():
    return "pong", 200

def run_server():
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

# ====== Запуск ======
if __name__ == "__main__":
    logging.info("Starting keepalive web server…")
    Thread(target=run_server, daemon=True).start()

    logging.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
