import os
import logging
from datetime import datetime
from typing import Dict, Any

import telebot
from telebot import types

from flask import Flask, jsonify
from openai import OpenAI

# ====== ENV ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets")

# ====== GPT client ======
client = OpenAI(api_key=OPENAI_KEY)

# ====== LOGS ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ====== BOT ======
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# Снимаем возможный webhook
try:
    bot.remove_webhook()
    logging.info("Webhook removed (ok)")
except Exception as e:
    logging.warning(f"Webhook remove warn: {e}")

# ====== STATE (память в ОЗУ; БД подключим позже) ======
user_state: Dict[int, Dict[str, Any]] = {}  # {uid: {mode: ..., data:{...}, step:int}}

# ====== Клавиатура ======
def main_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("Ошибка"), types.KeyboardButton("Хочу стратегию"), types.KeyboardButton("Поговорим"))
    kb.row(types.KeyboardButton("Паспорт"), types.KeyboardButton("Панель недели"))
    kb.row(types.KeyboardButton("Мой прогресс"), types.KeyboardButton("Материалы"), types.KeyboardButton("Сброс"))
    return kb

# ====== Общий "курсовый" системный контекст для GPT ======
COURSE_SYSTEM = (
    "Ты — ИИ-наставник Innertrade. Работай строго по курсу: психология трейдинга (MERCEDES, TOTE), "
    "архетипы/роли, чек-листы, ритуалы, конструктор ТС. Отвечай кратко и предметно, давай шаги и мини-чек-листы. "
    "Если спрашивают про «ошибку» — запускай мини-разбор через MERCEDES+TOTE. Если «хочу стратегию» — веди по "
    "конструктору ТС: цели, стиль, рынок, вход/выход, риски, правила сопровождения, тестирование. Если «поговорим» — "
    "держи беседу в русле психологии и системности трейдинга."
)

def ask_gpt_course(user_text: str, history: list = None) -> str:
    # Простой одноходовый вызов с "жёстким" системным сообщением.
    msgs = [{"role": "system", "content": COURSE_SYSTEM}]
    if history:
        msgs.extend(history)
    msgs.append({"role": "user", "content": user_text})
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.4,
        messages=msgs
    )
    return (resp.choices[0].message.content or "").strip()

def send_long(chat_id, text):
    MAX = 3500
    for i in range(0, len(text), MAX):
        bot.send_message(chat_id, text[i:i+MAX])

# ====== /start ======
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    user_state[uid] = {"mode": None, "data": {}, "step": 0}
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник Innertrade.\nВыбери кнопку или напиши текст.\nКоманды: /ping /reset",
        reply_markup=main_kb()
    )

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.reply_to(m, "pong ✅")

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    uid = m.from_user.id
    user_state[uid] = {"mode": None, "data": {}, "step": 0}
    bot.reply_to(m, "Контекст очищен.", reply_markup=main_kb())

# ====== Вспомогательные мастера ======

# ---- Паспорт трейдера (wizard) ----
PASSPORT_QUESTIONS = [
    "1/6) На каком рынке/инструментах торгуешь? (пример: акции США, EURUSD, BTC, фьючерсы…)",
    "2/6) Твой стиль: скальпинг / интрадей / свинг / позиционный?",
    "3/6) Таймфреймы (основные и вспомогательные)?",
    "4/6) Риск-менеджмент: риск на сделку (%), дневные/недельные лимиты?",
    "5/6) Правила входа/выхода в общих чертах (уровни, паттерны, новости…)?",
    "6/6) Ритуалы и правила психогигиены (настройка, тайм-аут, завершение дня)?"
]

def start_passport(uid, chat_id):
    user_state[uid] = {"mode": "passport", "step": 0, "data": {}}
    bot.send_message(chat_id, "🪪 Паспорт трейдера. Ответь на 6 коротких вопросов.\n" + PASSPORT_QUESTIONS[0])

def handle_passport(uid, chat_id, text):
    st = user_state.get(uid, {})
    step = st.get("step", 0)
    data = st.get("data", {})

    data[f"q{step+1}"] = text
    step += 1

    if step >= len(PASSPORT_QUESTIONS):
        # финал
        user_state[uid] = {"mode": None, "step": 0, "data": data}
        summary = (
            "✅ Паспорт сохранён (локально).\n\n"
            f"Рынки: {data.get('q1','-')}\n"
            f"Стиль: {data.get('q2','-')}\n"
            f"ТФ: {data.get('q3','-')}\n"
            f"Риски/лимиты: {data.get('q4','-')}\n"
            f"Вход/выход: {data.get('q5','-')}\n"
            f"Ритуалы: {data.get('q6','-')}\n\n"
            "Дальше можно: «Хочу стратегию» или «Панель недели»."
        )
        send_long(chat_id, summary)
    else:
        user_state[uid]["step"] = step
        user_state[uid]["data"] = data
        bot.send_message(chat_id, PASSPORT_QUESTIONS[step])

# ---- Панель недели (wizard) ----
WEEK_PANEL_QUESTIONS = [
    "1/4) Главный фокус недели (одна формулировка — например: «не пересиживать убытки»):",
    "2/4) План из 3 мини-шагов на неделю (кратко, через запятую):",
    "3/4) Лимиты риска на день/неделю (в процентах или деньгах):",
    "4/4) Как поймёшь, что неделя удалась? (1-2 проверяемых критерия):"
]

def start_week_panel(uid, chat_id):
    user_state[uid] = {"mode": "week_panel", "step": 0, "data": {}, "week_start": datetime.now().strftime("%Y-%m-%d")}
    bot.send_message(chat_id, "📅 Панель недели. 4 шага — и готово.\n" + WEEK_PANEL_QUESTIONS[0])

def handle_week_panel(uid, chat_id, text):
    st = user_state.get(uid, {})
    step = st.get("step", 0)
    data = st.get("data", {})
    data[f"q{step+1}"] = text
    step += 1

    if step >= len(WEEK_PANEL_QUESTIONS):
        user_state[uid] = {"mode": None, "step": 0, "data": {}}
        summary = (
            "✅ Панель недели зафиксирована.\n\n"
            f"Фокус: {data.get('q1','-')}\n"
            f"План(3): {data.get('q2','-')}\n"
            f"Лимиты: {data.get('q3','-')}\n"
            f"Критерии успеха: {data.get('q4','-')}\n\n"
            "Совет: закрепи это в заметках/планере. Можем ежедневно напоминать — скажи «Ежедневный чек-ин»."
        )
        send_long(chat_id, summary)
    else:
        user_state[uid]["step"] = step
        user_state[uid]["data"] = data
        bot.send_message(chat_id, WEEK_PANEL_QUESTIONS[step])

# ====== Обработчик кнопок ======
@bot.message_handler(func=lambda m: m.text in {
    "Ошибка","Хочу стратегию","Поговорим","Паспорт","Панель недели","Мой прогресс","Материалы","Сброс"
})
def on_buttons(m):
    uid = m.from_user.id
    t = (m.text or "").strip()

    if t == "Сброс":
        user_state[uid] = {"mode": None, "step": 0, "data": {}}
        bot.send_message(m.chat.id, "Контекст очищен.", reply_markup=main_kb())
        return

    if t == "Паспорт":
        start_passport(uid, m.chat.id)
        return

    if t == "Панель недели":
        start_week_panel(uid, m.chat.id)
        return

    if t == "Мой прогресс":
        bot.send_message(
            m.chat.id,
            "📈 Прогресс (демо):\n— Кол-во заполненных паспортов: 1\n— Активный фокус недели: установлен\n— Следующий шаг: «Ежедневный чек-ин» или «Хочу стратегию»"
        )
        return

    if t == "Материалы":
        bot.send_message(
            m.chat.id,
            "📚 Материалы Innertrade:\n— MERCEDES / TOTE (теория)\n— Архетипы и роли\n— Конструктор ТС\n— Риск-менеджмент\nСпроси: «дай конструктор ТС» или «напомни MERCEDES»."
        )
        return

    # Кнопки, которые идут в GPT, но с курс-контекстом
    alias_prompt = {
        "Ошибка": "У меня ошибка в трейдинге. Запусти мини-разбор через MERCEDES+TOTE. Дай краткий чек-лист.",
        "Хочу стратегию": "Хочу собрать свою торговую стратегию. Веди по конструктору: цели, стиль, рынок, вход/выход, риски, сопровождение, тестирование.",
        "Поговорим": "Поговорим о психологии трейдинга в рамках Innertrade. Помоги найти главное узкое место и предложи 1-2 шага."
    }
    try:
        reply = ask_gpt_course(alias_prompt[t])
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply)

# ====== Любой текст (состояния мастеров + свободный диалог) ======
@bot.message_handler(func=lambda _: True)
def on_text(m):
    uid = m.from_user.id
    text = (m.text or "").strip()

    st = user_state.get(uid, {"mode": None})
    mode = st.get("mode")

    if mode == "passport":
        handle_passport(uid, m.chat.id, text)
        return

    if mode == "week_panel":
        handle_week_panel(uid, m.chat.id, text)
        return

    # Свободный диалог по курсу
    try:
        reply = ask_gpt_course(text)
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply)

# ====== keepalive ======
app = Flask(__name__)

@app.route("/")
def root():
    return jsonify(ok=True, service="innertrade-bot", ts=datetime.utcnow().isoformat())

@app.route("/health")
def health():
    return "pong"

if __name__ == "__main__":
    logging.info("Starting keepalive web server…")
    logging.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
