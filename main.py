import os
import logging
from flask import Flask
import telebot
from telebot import types
from openai import OpenAI

# ========= Env =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY     = os.getenv("OPENAI_API_KEY")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Environment")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Environment")

# ========= OpenAI клиент =========
client = OpenAI(api_key=OPENAI_KEY)

# ========= Логи =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ========= Telegram =========
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# На всякий случай снимаем webhook
try:
    bot.remove_webhook()
    logging.info("Webhook removed (ok)")
except Exception as e:
    logging.warning(f"Webhook remove warn: {e}")

# ========= Память диалога =========
# простая in-memory история: {user_id: [ {role, content}, ... ]}
history = {}

def ask_gpt(uid: int, user_text: str) -> str:
    """
    Вызов Chat Completions (новый SDK).
    """
    msgs = history.setdefault(uid, [])
    msgs.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.5,
        messages=msgs
    )
    reply = (resp.choices[0].message.content or "").strip()
    msgs.append({"role": "assistant", "content": reply})
    return reply

def send_long(chat_id: int, text: str):
    """
    Режем длинные ответы, И БЕЗ reply_to (чтобы не было «ответа на сообщение»).
    """
    MAX = 3500
    for i in range(0, len(text), MAX):
        bot.send_message(chat_id, text[i:i+MAX])

# ========= Клавиатура =========
def main_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(
        types.KeyboardButton("У меня ошибка"),
        types.KeyboardButton("Хочу стратегию"),
        types.KeyboardButton("Поговорим")
    )
    kb.row(
        types.KeyboardButton("Чек-лист"),
        types.KeyboardButton("Сброс")
    )
    return kb

# ========= Команды =========
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    history[uid] = []
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник Innertrade.\n"
        "Выбери кнопку или напиши текст.\n"
        "Команды: /ping /reset",
        reply_markup=main_kb()
    )

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    history[m.from_user.id] = []
    bot.send_message(m.chat.id, "Контекст очищен.", reply_markup=main_kb())

# ========= Кнопки-интенты =========
INTENT_MAP = {
    "У меня ошибка":  "Давай разберём мою торговую ошибку.",
    "Хочу стратегию": "Помоги собрать торговую систему/стратегию под меня.",
    "Поговорим":      "Просто поболтаем про трейдинг и мои вопросы.",
    "Чек-лист":       "Дай короткий чек-лист на сегодня по трейдингу."
}

@bot.message_handler(func=lambda x: x.text in INTENT_MAP.keys() or x.text == "Сброс")
def on_buttons(m):
    uid = m.from_user.id
    t = (m.text or "").strip()
    if t == "Сброс":
        history[uid] = []
        bot.send_message(m.chat.id, "Контекст очищен. Начнём заново.", reply_markup=main_kb())
        return

    try:
        reply = ask_gpt(uid, INTENT_MAP[t])
    except Exception as e:
        reply = f"Ошибка GPT: {e}"

    send_long(m.chat.id, reply)

# ========= Обычный текст =========
@bot.message_handler(func=lambda _: True)
def on_text(m):
    uid = m.from_user.id
    try:
        reply = ask_gpt(uid, m.text or "")
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply)

# ========= Keepalive (Render/UptimeRobot) =========
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return "pong"

if __name__ == "__main__":
    logging.info("Starting keepalive web server…")
    logging.info("Starting polling…")
    # Запускаем Flask в отдельном порте, чтобы UptimeRobot/Render могли пинговать
    from threading import Thread
    def run_flask():
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
    Thread(target=run_flask, daemon=True).start()

    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
