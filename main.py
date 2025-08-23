import os
import logging
from datetime import datetime
from flask import Flask
import telebot
from telebot import types
from openai import OpenAI

# ====== ENV ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY     = os.getenv("OPENAI_API_KEY")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets")

# ====== OpenAI (новый SDK) ======
client = OpenAI(api_key=OPENAI_KEY)

# ====== Логи ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("innertrade")

# ====== Flask keepalive ======
app = Flask(__name__)

@app.get("/")
def root():
    return "Innertrade bot OK"

@app.get("/health")
def health():
    return "pong"

# ====== Telegram bot ======
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# На всякий случай снимем вебхук (мы работаем в polling)
try:
    bot.remove_webhook()
    log.info("Webhook removed (ok)")
except Exception as e:
    log.warning(f"Webhook remove warn: {e}")

# Память диалога в RAM (персист при необходимости прикрутим к БД)
history = {}  # uid -> [{"role": "...", "content": "..."}]

def ask_gpt(uid: int, text: str) -> str:
    msgs = history.setdefault(uid, [])
    msgs.append({"role": "user", "content": text})

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.4,
        messages=[
            # Лёгкий системный каркас, чтобы GPT держал контекст проекта
            {"role": "system", "content":
             "Ты ИИ-наставник Innertrade. Коротко, по делу. Если пользователь нажимает кнопки меню, \
возвращай структурированные ответы. Если запрос общий, помогай, но не выдумывай факты про его сделки."}
        ] + msgs
    )
    reply = (resp.choices[0].message.content or "").strip()
    msgs.append({"role": "assistant", "content": reply})
    return reply

def send_long(chat_id, text, reply_to=None):
    MAX = 3500
    for i in range(0, len(text), MAX):
        bot.send_message(chat_id, text[i:i+MAX], reply_to_message_id=reply_to if i == 0 else None)

def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("Ошибка"), types.KeyboardButton("Стратегия"), types.KeyboardButton("Поговорим"))
    kb.row(types.KeyboardButton("Паспорт"), types.KeyboardButton("Панель недели"))
    kb.row(types.KeyboardButton("Материалы"), types.KeyboardButton("Прогресс"), types.KeyboardButton("Сброс"))
    return kb

# ====== /start ======
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    history[uid] = []
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник Innertrade.\nВыбери кнопку или напиши текст.\nКоманды: /ping /reset",
        reply_markup=main_keyboard()
    )

# ====== /ping ======
@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.reply_to(m, "pong")

# ====== /reset ======
@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    history[m.from_user.id] = []
    bot.reply_to(m, "Контекст очищен.", reply_markup=main_keyboard())

# ====== Хелперы-ответы на жёсткие кнопки ======
def reply_passport_intro() -> str:
    return (
        "<b>Паспорт трейдера</b>\n"
        "Ответь по пунктам, можно списком 1–6:\n\n"
        "1) На каких рынках/инструментах торгуешь?\n"
        "2) Таймфреймы (рабочий / старший контекст)?\n"
        "3) Базовый подход (тренд/средний/контртренд), стиль (дейтрейд/свинг)?\n"
        "4) Риск-профиль (риски на сделку/день, просадка-лимиты)?\n"
        "5) Ключевые триггеры стресса (твои «сигналы тревоги»)?\n"
        "6) Ритуалы до/во время/после сессии (коротко)."
    )

def reply_week_panel_template() -> str:
    today = datetime.utcnow().strftime('%d.%m')
    return (
        f"<b>Панель недели</b> (неделя от {today})\n"
        "Ответь кратко 1–5:\n\n"
        "1) <b>Фокус недели (узел)</b>: одна тема/ошибка/навык (например: «не пересиживать стоп»).\n"
        "2) <b>Лимиты</b>: риск на день/неделю, дневной стоп, макс. число сделок.\n"
        "3) <b>Ритуалы</b>: чек входа, дыхание, пауза после стопа, финальный разбор.\n"
        "4) <b>План</b>: 2–4 конкретных шага на неделю (что и когда делаешь).\n"
        "5) <b>Мини-ретро</b> (в конце недели): 3 факта → 1 вывод → 1 улучшение.\n\n"
        "Готов? Напиши ответы подряд (1–5)."
    )

def reply_error_intro() -> str:
    return (
        "<b>Разбор ошибки (мини-MERCEDES + TOTE)</b>\n"
        "Отправь 1–6:\n"
        "1) Ситуация/контекст (что произошло?)\n"
        "2) Мысли/интерпретации (M)\n"
        "3) Эмоции/физиология (E)\n"
        "4) Реакции/действия (R/C)\n"
        "5) Результат (S)\n"
        "6) TOTE: цель → тест → операция → выход (что меняем в следующий раз?)"
    )

def reply_strategy_intro() -> str:
    return (
        "<b>Стратегия/ТС</b>\n"
        "Давай зафиксируем основу:\n"
        "1) Подход и рынок (что, где, когда)\n"
        "2) Вход: условия/сигналы\n"
        "3) Стоп и сопровождение\n"
        "4) Выход/таргеты\n"
        "5) Риск (на сделку/день)\n"
        "Ответь 1–5 — сделаем черновик ТС."
    )

def reply_materials_hint() -> str:
    return (
        "<b>Материалы Innertrade</b>\n"
        "• MERCEDES и TOTE — краткая теория\n"
        "• Архетипы/роли трейдера — таблица\n"
        "• Сборка ТС: конструктор + чек-листы\n"
        "• Риск-менеджмент: уровни и лимиты\n"
        "Напиши, что открыть: «mercedes», «tote», «архетипы», «конструктор ТС», «риск»."
    )

def reply_progress_hint() -> str:
    return (
        "<b>Мой прогресс</b>\n"
        "Могу подсказать, что уже зафиксировано за сессию (ошибки/шаблоны/планы).\n"
        "Напиши: «показать прогресс» или уточни, что именно вывести."
    )

# ====== Кнопки (строгое соответствие тексту) ======
BUTTONS = {
    "Ошибка": "ERROR",
    "Стратегия": "STRAT",
    "Поговорим": "CHAT",
    "Паспорт": "PASSPORT",
    "Панель недели": "WEEK",
    "Материалы": "MATS",
    "Прогресс": "PROGRESS",
    "Сброс": "RESET_BTN",
}

@bot.message_handler(func=lambda m: (m.text or "").strip() in BUTTONS.keys())
def on_buttons(m):
    uid = m.from_user.id
    text = (m.text or "").strip()
    kind = BUTTONS[text]
    log.info(f"Button pressed: {text} → {kind}")

    if kind == "RESET_BTN":
        history[uid] = []
        bot.reply_to(m, "Контекст очищен. Выбери действие.", reply_markup=main_keyboard())
        return

    if kind == "PASSPORT":
        send_long(m.chat.id, reply_passport_intro(), reply_to=m.message_id)
        return

    if kind == "WEEK":
        send_long(m.chat.id, reply_week_panel_template(), reply_to=m.message_id)
        return

    if kind == "ERROR":
        send_long(m.chat.id, reply_error_intro(), reply_to=m.message_id)
        return

    if kind == "STRAT":
        send_long(m.chat.id, reply_strategy_intro(), reply_to=m.message_id)
        return

    if kind == "MATS":
        send_long(m.chat.id, reply_materials_hint(), reply_to=m.message_id)
        return

    if kind == "PROGRESS":
        send_long(m.chat.id, reply_progress_hint(), reply_to=m.message_id)
        return

    # Fallback – если вдруг что-то новое
    try:
        reply = ask_gpt(uid, text)
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply, reply_to=m.message_id)

# ====== Любой другой текст → GPT ======
@bot.message_handler(func=lambda _: True)
def on_text(m):
    uid = m.from_user.id
    try:
        reply = ask_gpt(uid, m.text or "")
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    # Без «reply_to», чтобы не было «ответа на сообщение пользователя»
    send_long(m.chat.id, reply)

if __name__ == "__main__":
    log.info("Starting keepalive web server…")
    # Render будет звать /health периодически; Flask слушает параллельно
    import threading
    def run_flask():
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
    threading.Thread(target=run_flask, daemon=True).start()

    log.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
