import os
import logging
from flask import Flask
import telebot
from telebot import types
from openai import OpenAI

# ====== Ключи из переменных окружения ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")  # уже есть в окружении, пока не используем

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets/Environment")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets/Environment")

# ====== Логи ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ====== Инициализация ======
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
client = OpenAI(api_key=OPENAI_KEY)

# Память простая (RAM) — для демо; позже переведём в БД
history = {}  # uid -> [{"role":"user"/"assistant","content":"..."}]

# ====== Тексты кнопок (интенты) ======
BTN_ERROR         = "🧩 У меня ошибка"
BTN_STRATEGY      = "🧭 Хочу стратегию"
BTN_DONT_KNOW     = "🤷 Не знаю, с чего начать"
BTN_PANIC         = "⛑ Экстренно: «поплыл»"
BTN_PROGRESS      = "📈 Мой прогресс"
BTN_PROFILE       = "🗂 Паспорт / профиль"
BTN_MATERIALS     = "📚 Материалы"

INTENT_BUTTONS = [
    BTN_ERROR, BTN_STRATEGY, BTN_DONT_KNOW,
    BTN_PANIC, BTN_PROGRESS, BTN_PROFILE, BTN_MATERIALS
]

# ====== Вспомогательные ======
def make_menu_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN_ERROR, BTN_STRATEGY)
    kb.row(BTN_DONT_KNOW, BTN_PANIC)
    kb.row(BTN_PROGRESS, BTN_PROFILE, BTN_MATERIALS)
    return kb

def remove_keyboard(chat_id):
    bot.send_message(chat_id, "Обновляю меню…", reply_markup=types.ReplyKeyboardRemove())

def send_menu(chat_id):
    bot.send_message(
        chat_id,
        "Выбери пункт меню или напиши свой вопрос.",
        reply_markup=make_menu_keyboard()
    )

def ask_gpt(uid, text):
    msgs = history.setdefault(uid, [])
    msgs.append({"role": "user", "content": text})
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

# ====== Команды ======
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    history[uid] = []  # чистим контекст
    remove_keyboard(m.chat.id)    # убираем старую клавиатуру (Телеграм её кэширует)
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник Innertrade.\n"
        "Умею разбирать ошибки, собирать ТС и вести тебя по шагам.\n"
        "Команды: /ping /reset /menu"
    )
    send_menu(m.chat.id)

@bot.message_handler(commands=['menu'])
def cmd_menu(m):
    remove_keyboard(m.chat.id)
    send_menu(m.chat.id)

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.reply_to(m, "pong ✅")

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    history[m.from_user.id] = []
    remove_keyboard(m.chat.id)
    bot.send_message(m.chat.id, "Контекст очищен.")
    send_menu(m.chat.id)

# ====== Обработка кнопок (НОВЫЕ тексты) ======
@bot.message_handler(func=lambda x: (x.text or "") in INTENT_BUTTONS)
def on_buttons(m):
    uid = m.from_user.id
    t = (m.text or "").strip()

    # Здесь маппим кнопку -> «семантический запрос» в GPT (пока без БД)
    intent_map = {
        BTN_ERROR:     "Начать разбор ошибки по модели MERCEDES + TOTE. Спроси, что болит, и веди по шагам.",
        BTN_STRATEGY:  "Помоги собрать торговую стратегию: стиль, рынок, TF, вход/выход, риск. Веди чеклистом.",
        BTN_DONT_KNOW:"Диагностика: задай 5-7 вопросов, чтобы понять, с чего начать (ошибка/ТС/психология).",
        BTN_PANIC:     "Экстренный протокол: что делать когда «поплыл». Короткий сценарий с действиями и тайм-аутом.",
        BTN_PROGRESS:  "Запросить короткий отчёт: что сделал за неделю/день, что улучшить, 1 следующий шаг.",
        BTN_PROFILE:   "Паспорт трейдера: что это, какие поля, чем заполнить. Дай шаблон и как вести.",
        BTN_MATERIALS: "Список материалов курса по модулям (без воды), что смотреть/делать в первую очередь."
    }

    try:
        reply = ask_gpt(uid, intent_map.get(t, t))
    except Exception as e:
        reply = f"Ошибка GPT: {e}"

    send_long(m.chat.id, reply)

# ====== Любой другой текст ======
@bot.message_handler(func=lambda _: True)
def on_text(m):
    uid = m.from_user.id
    try:
        reply = ask_gpt(uid, m.text or "")
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply)

# ====== Keepalive HTTP для Render/UptimeRobot ======
app = Flask(__name__)

@app.route("/")
def root():
    return "Innertrade bot alive"

@app.route("/health")
def health():
    return "pong"

if __name__ == "__main__":
    # TeleBot: снимаем webhook на всякий случай
    try:
        bot.remove_webhook()
        logging.info("Webhook removed (ok)")
    except Exception as e:
        logging.warning(f"Webhook remove warn: {e}")

    # Запускаем Flask (keepalive) и polling параллельно
    from threading import Thread
    def run_flask():
        logging.info("Starting keepalive web server…")
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
    Thread(target=run_flask, daemon=True).start()

    logging.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
