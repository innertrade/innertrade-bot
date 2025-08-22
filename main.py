# ===== main.py (Innertrade mentor bot) =====
import os
import logging
from flask import Flask
import telebot
from telebot import types
from openai import OpenAI

# ---------- ENV ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", "10000"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Env")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Env")

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ---------- GPT ----------
client = OpenAI(api_key=OPENAI_KEY)

# Храним контексты по пользователю
history = {}  # uid -> [{"role":"system"/"user"/"assistant","content":"..."}]

SYSTEM_PROMPT = (
    "Ты — ИИ-наставник проекта Innertrade. "
    "Твоя задача: быстро определить запрос пользователя и вести его по коротким шагам. "
    "Отвечай структурировано, короткими блоками, с буллетами и мини-чеклистами. "
    "Если пользователь жмёт кнопку-интент, продолжай как сценарий: задай 1–2 уточняющих вопроса, "
    "дай готовый шаг и микрорезультат для фиксации. Не уходи в длинные лекции."
)

def get_msgs(uid):
    msgs = history.setdefault(uid, [])
    # добавим System один раз
    if not msgs or msgs[0].get("role") != "system":
        msgs.insert(0, {"role": "system", "content": SYSTEM_PROMPT})
    return msgs

def ask_gpt(uid, text):
    msgs = get_msgs(uid)
    msgs.append({"role": "user", "content": text})
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.5,
        messages=msgs
    )
    reply = (resp.choices[0].message.content or "").strip()
    msgs.append({"role": "assistant", "content": reply})
    return reply

# ---------- BOT ----------
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# Снять вебхук (на всякий)
try:
    bot.remove_webhook()
    logging.info("Webhook removed (ok)")
except Exception as e:
    logging.warning(f"Webhook remove warn: {e}")

# ---- КЛАВИАТУРЫ ----
USE_EXTENDED_MENU = True  # False = 3 кнопки, True = 8 кнопок

INTENTS_MIN = [
    "🆘 У меня ошибка",
    "🧩 Хочу стратегию",
    "🗣 Поговорим",
]

INTENTS_EXTENDED = [
    "🆘 У меня ошибка",
    "🛠 Мини-разбор (Mercedes)",
    "🏗 Собрать/пересобрать ТС",
    "❓ Не знаю, с чего начать",
    "🚨 Экстренно: «поплыл»",
    "📈 Мой прогресс (неделя)",
    "🪪 Паспорт/профиль",
    "📚 Материалы",
]

def build_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    intents = INTENTS_EXTENDED if USE_EXTENDED_MENU else INTENTS_MIN
    # раскладываем по 2–3 кнопки в ряд
    row = []
    for i, label in enumerate(intents, 1):
        row.append(types.KeyboardButton(label))
        if len(row) == 3:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    # нижний ряд: сервис
    kb.row(types.KeyboardButton("🔄 Сброс"), types.KeyboardButton("🧭 Меню"))
    return kb

def send_long(chat_id, text):
    MAX = 3500
    for i in range(0, len(text), MAX):
        bot.send_message(chat_id, text[i:i+MAX])

# ---- МАППИНГ ИНТЕНТОВ -> ПОДСКАЗОК ДЛЯ GPT ----
def intent_seed(user_text):
    mapping = {
        "🆘 У меня ошибка":
            "INTENT=ERROR_START. Спроси кратко об ошибке в 1–2 вопросах. "
            "Дай мини-чеклист фиксации: 'что делаю/что думаю/что чувствую'.",

        "🛠 Мини-разбор (Mercedes)":
            "INTENT=MERCEDES_MICRO. Проведи короткий прогон через MERCEDES (контекст, мысли, эмоции, поведение, убеждения). "
            "Заверши 1 фразой-осознанием и 1 шагом TOTE на ближайшую сессию.",

        "🏗 Собрать/пересобрать ТС":
            "INTENT=BUILD_TS. Уточни стиль/таймфрейм/рынок. Дай каркас: вход-сопровождение-выход-риск. "
            "Попроси заполнить 3 поля сейчас и предложи сохранить черновик.",

        "❓ Не знаю, с чего начать":
            "INTENT=START_HELP. Предложи 3 пути: (а) быстрый мини-разбор ошибки, (б) экспресс-каркас ТС, (в) карта целей на неделю. "
            "Помоги выбрать 1 путь, затем задай 1 уточнение и дай 1 маленькое действие.",

        "🚨 Экстренно: «поплыл»":
            "INTENT=CRISIS. Дай протокол остановки: тайм-аут 3 мин, закрыть терминал, дыхание 4-7-8, проверить лимиты дня. "
            "После стабилизации — один вопрос на осознание и решение по позиции по сценарию.",

        "📈 Мой прогресс (неделя)":
            "INTENT=WEEKLY_PANEL. Попроси 3 факта: что получилось/что не получилось/1 причина. "
            "Сформируй фокус-узел на неделю и 2 ритуала поддержки. Итог — мини-план в 3 шагах.",

        "🪪 Паспорт/профиль":
            "INTENT=PASSPORT. Спроси кратко: рынок, стиль, ТФ, риск на сделку, лимит дня, главная ошибка. "
            "Верни аккуратную карточку-паспорт и предложи обновить при необходимости.",

        "📚 Материалы":
            "INTENT=MATERIALS. Предложи навигацию: М1-урок1 (Mercedes+TOTE), М1-урок2 (архетипы), М1-урок3 (убеждения), "
            "М2-урок1 (что такое ТС), М2-урок2 (входы), М2-урок3 (риск/выход), М2-урок4 (финализация). "
            "Спроси, что открыть кратко."
    }
    # если нажали «Меню»/«Сброс»
    if user_text in ("🧭 Меню", "🔄 Сброс"):
        return None
    # иначе — либо intent, либо свободный текст
    return mapping.get(user_text, f"FREE_CHAT. Пользователь пишет: {user_text}")

# ---------- HANDLERS ----------
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    # сброс контекста + system
    history[uid] = [{"role":"system","content":SYSTEM_PROMPT}]
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник <b>Innertrade</b>.\n"
        "Выбери кнопку-интент или напиши свой запрос.\nКоманды: /menu /reset /ping",
        reply_markup=build_kb()
    )

@bot.message_handler(commands=['menu'])
def cmd_menu(m):
    bot.send_message(m.chat.id, "🧭 Меню обновлено. Выбери интент:", reply_markup=build_kb())

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    uid = m.from_user.id
    history[uid] = [{"role":"system","content":SYSTEM_PROMPT}]
    bot.send_message(m.chat.id, "Контекст очищен. Готов продолжать.", reply_markup=build_kb())

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

# Кнопки/текст
@bot.message_handler(func=lambda x: True)
def on_text(m):
    uid = m.from_user.id
    incoming = (m.text or "").strip()

    if incoming == "🧭 Меню":
        bot.send_message(m.chat.id, "🧭 Меню:", reply_markup=build_kb())
        return
    if incoming == "🔄 Сброс":
        history[uid] = [{"role":"system","content":SYSTEM_PROMPT}]
        bot.send_message(m.chat.id, "Контекст очищен.", reply_markup=build_kb())
        return

    seed = intent_seed(incoming)
    try:
        reply = ask_gpt(uid, seed if seed else incoming)
    except Exception as e:
        reply = f"Ошибка GPT: {e}"

    send_long(m.chat.id, reply)

# ---------- KEEPALIVE (Render/health) ----------
app = Flask(__name__)

@app.route("/")
def home():
    return "Innertrade mentor is alive."

@app.route("/health")
def health():
    return "pong"

if __name__ == "__main__":
    logging.info("Starting keepalive web server…")
    # запуск Flask + polling в отдельных потоках не нужен — telebot сам вThread; Flask просто держит порт
    import threading
    def run_bot():
        logging.info("Starting polling…")
        bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
