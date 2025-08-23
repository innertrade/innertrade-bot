import os
import logging
from flask import Flask
import telebot
from telebot import types
from openai import OpenAI

# ====== ENV ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets")

client = OpenAI(api_key=OPENAI_KEY)

# ====== LOGS ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# снять вебхук на всякий случай
try:
    bot.remove_webhook()
    logging.info("Webhook removed (ok)")
except Exception as e:
    logging.warning(f"Webhook remove warn: {e}")

# ====== SIMPLE KEEPALIVE WEB ======
app = Flask(__name__)

@app.route("/")
def home():
    return "Innertrade bot is alive"

@app.route("/health")
def health():
    return "pong"

# ====== MEMORY ======
history = {}  # uid -> list of messages
week_flow_stage = {}  # uid -> current step of weekly panel [1..5]
week_flow_data = {}   # uid -> dict collected answers

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

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("Ошибка"), types.KeyboardButton("Стратегия"), types.KeyboardButton("Поговорим"))
    kb.row(types.KeyboardButton("Паспорт"), types.KeyboardButton("Панель недели"))
    kb.row(types.KeyboardButton("Мой прогресс"), types.KeyboardButton("Материалы"))
    kb.row(types.KeyboardButton("Сброс"))
    return kb

# ====== START/PING/RESET ======
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    history[uid] = []
    week_flow_stage.pop(uid, None)
    week_flow_data.pop(uid, None)
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник Innertrade.\nВыбери кнопку или напиши текст.\nКоманды: /ping /reset",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.reply_to(m, "pong")

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    uid = m.from_user.id
    history[uid] = []
    week_flow_stage.pop(uid, None)
    week_flow_data.pop(uid, None)
    bot.reply_to(m, "Контекст очищен.", reply_markup=main_menu())

# ====== INTENTS: ПАСПОРТ (старт) ======
@bot.message_handler(func=lambda x: (x.text or "").strip().lower() == "паспорт")
def on_passport(m):
    bot.send_message(
        m.chat.id,
        "📇 Паспорт трейдера.\n1/6) На каком рынке/инструментах торгуешь? (пример: акции США, EURUSD, BTC, фьючерсы…)"
    )

# ====== INTENTS: ПАНЕЛЬ НЕДЕЛИ ======
PANEL_ALIASES = {"панель недели", "панель", "неделя", "панельный деви"}  # опечатку тоже ловим

@bot.message_handler(func=lambda x: (x.text or "").strip().lower() in PANEL_ALIASES)
def on_week_panel(m):
    uid = m.from_user.id
    week_flow_stage[uid] = 1
    week_flow_data[uid] = {}
    bot.send_message(
        m.chat.id,
        "🗓️ Панель недели.\nДавай быстро зафиксируем план.\n\n1/5) <b>Фокус недели</b>: какой один главный результат ты хочешь получить?",
        reply_markup=types.ReplyKeyboardRemove()
    )

# Продолжение диалога по Панели недели — ловим любой текст, пока идёт сценарий
def proceed_week_panel(uid, chat_id, text):
    stage = week_flow_stage.get(uid, 0)
    data = week_flow_data.setdefault(uid, {})

    if stage == 1:
        data["focus"] = text
        week_flow_stage[uid] = 2
        bot.send_message(chat_id, "2/5) <b>Узел/боль</b>: что мешало раньше? (одна формулировка)")

    elif stage == 2:
        data["knot"] = text
        week_flow_stage[uid] = 3
        bot.send_message(chat_id, "3/5) <b>План</b>: 3 конкретных шага на этой неделе (в виде списка).")

    elif stage == 3:
        data["plan"] = text
        week_flow_stage[uid] = 4
        bot.send_message(chat_id, "4/5) <b>Лимиты</b>: риск/лимит просадки/время на торговлю? (кратко)")

    elif stage == 4:
        data["limits"] = text
        week_flow_stage[uid] = 5
        bot.send_message(chat_id, "5/5) <b>Ретро прошлого периода</b>: что сработало/не сработало (1-2 предложения).")

    elif stage == 5:
        data["retro"] = text
        week_flow_stage.pop(uid, None)
        summary = (
            "✅ <b>Панель недели зафиксирована</b>\n\n"
            f"• Фокус: {data.get('focus','')}\n"
            f"• Узел/боль: {data.get('knot','')}\n"
            f"• План: {data.get('plan','')}\n"
            f"• Лимиты: {data.get('limits','')}\n"
            f"• Ретро: {data.get('retro','')}\n\n"
            "Если хочешь сохранить это в профиль — напиши: «сохрани панель» (подключим запись в БД на следующем шаге)."
        )
        bot.send_message(chat_id, summary, reply_markup=main_menu())

# ====== ДРУГИЕ КНОПКИ (заглушки-подсказки) ======
@bot.message_handler(func=lambda x: (x.text or "").strip().lower() == "ошибка")
def on_error(m):
    bot.send_message(
        m.chat.id,
        "⚠️ Окей, разберём ошибку по MERCEDES+TOTE.\n"
        "1) Опиши коротко, что произошло и какое было действие.",
    )

@bot.message_handler(func=lambda x: (x.text or "").strip().lower() == "стратегия")
def on_strategy(m):
    bot.send_message(
        m.chat.id,
        "🧩 «Стратегия». Могу помочь собрать/пересобрать ТС.\n"
        "Напиши: на каком рынке и таймфрейме хочешь работать, и какой стиль ближе (интрадей/свинг/позиционная)."
    )

@bot.message_handler(func=lambda x: (x.text or "").strip().lower() == "поговорим")
def on_talk(m):
    bot.send_message(
        m.chat.id,
        "💬 О чём хочешь поговорить сейчас: о рынке, о дисциплине, о рисках или о конкретной сделке?"
    )

@bot.message_handler(func=lambda x: (x.text or "").strip().lower() == "мой прогресс")
def on_progress(m):
    bot.send_message(
        m.chat.id,
        "📈 Здесь будет сводка твоих отметок (ошибки, ритуалы, панель недели).\n"
        "Скоро подключим сохранение и выдачу отчётов из базы.",
    )

@bot.message_handler(func=lambda x: (x.text or "").strip().lower() == "материалы")
def on_materials(m):
    bot.send_message(
        m.chat.id,
        "📚 Материалы:\n— MERCEDES, TOTE\n— Архетипы и роли\n— Риск-менеджмент, конструктор ТС\n(Подключим выдачу по кнопкам.)",
    )

@bot.message_handler(func=lambda x: (x.text or "").strip().lower() == "сброс")
def on_clear(m):
    uid = m.from_user.id
    history[uid] = []
    week_flow_stage.pop(uid, None)
    week_flow_data.pop(uid, None)
    bot.reply_to(m, "Контекст очищен. Выбери пункт меню.", reply_markup=main_menu())

# ====== CATCH-ALL (оставляем В САМОМ НИЗУ) ======
@bot.message_handler(func=lambda _: True)
def on_text(m):
    uid = m.from_user.id
    txt = (m.text or "").strip()

    # если внутри сценария «Панель недели» — ведём дальше
    if week_flow_stage.get(uid):
        proceed_week_panel(uid, m.chat.id, txt)
        return

    # иначе — обычный ответ через GPT
    try:
        reply = ask_gpt(uid, txt)
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply)

if __name__ == "__main__":
    logging.info("Starting keepalive web server…")
    import threading
    def run_web():
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
    threading.Thread(target=run_web, daemon=True).start()

    logging.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
