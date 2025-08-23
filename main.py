import os
import logging
from openai import OpenAI
import telebot
from telebot import types
from flask import Flask

# ====== Ключи ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")  # на будущее: использование БД уже настроено в окружении

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets")

client = OpenAI(api_key=OPENAI_KEY)

# ====== Логи ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ====== Keepalive (для Render / UptimeRobot) ======
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return "OK: Innertrade bot"

@app.route("/health", methods=["GET"])
def health():
    return "pong"

# ====== Память (RAM) ======
history = {}       # uid -> [{"role":"user"/"assistant","content":"..."}]
user_state = {}    # uid -> {"flow": "passport|weekly|error|...", "step": int, "data": dict}

# ====== GPT ======
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

# ====== Меню ======
def build_main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("Ошибка"), types.KeyboardButton("ТС / Стратегия"))
    kb.row(types.KeyboardButton("Паспорт"), types.KeyboardButton("Панель недели"))
    kb.row(types.KeyboardButton("Материалы"), types.KeyboardButton("Прогресс"))
    kb.row(types.KeyboardButton("Профиль"), types.KeyboardButton("Сброс"))
    return kb

# ====== Хелперы сценариев ======
def start_passport(uid):
    user_state[uid] = {"flow": "passport", "step": 1, "data": {}}
    return ("Паспорт трейдера.\n"
            "1/6) На каком рынке/инструментах торгуешь? (пример: акции США, EURUSD, BTC, фьючерсы…)")

def continue_passport(uid, text):
    st = user_state.get(uid, {})
    step = st.get("step", 1)
    data = st.get("data", {})

    if step == 1:
        data["рынок"] = text.strip()
        user_state[uid] = {"flow": "passport", "step": 2, "data": data}
        return "2/6) Твой стиль: скальпинг, интрадей, свинг, позиционный?"
    elif step == 2:
        data["стиль"] = text.strip()
        user_state[uid] = {"flow": "passport", "step": 3, "data": data}
        return "3/6) Рабочие таймфреймы?"
    elif step == 3:
        data["таймфреймы"] = text.strip()
        user_state[uid] = {"flow": "passport", "step": 4, "data": data}
        return "4/6) Средняя дневная сессия (время): когда торгуешь?"
    elif step == 4:
        data["время"] = text.strip()
        user_state[uid] = {"flow": "passport", "step": 5, "data": data}
        return "5/6) Риск на сделку (% от депо)?"
    elif step == 5:
        data["риск_на_сделку"] = text.strip()
        user_state[uid] = {"flow": "passport", "step": 6, "data": data}
        return "6/6) Главная слабость/ошибка (одно предложение)?"
    elif step == 6:
        data["главная_ошибка"] = text.strip()
        user_state[uid] = {"flow": None, "step": 0, "data": data}
        # TODO: здесь можно сохранить data в БД
        return ("Готово ✅ Паспорт сохранён (локально)."
                "\nКратко:\n"
                f"- Рынок: {data.get('рынок')}\n"
                f"- Стиль: {data.get('стиль')}\n"
                f"- ТФ: {data.get('таймфреймы')}\n"
                f"- Время: {data.get('время')}\n"
                f"- Риск: {data.get('риск_на_сделку')}\n"
                f"- Ошибка: {data.get('главная_ошибка')}")
    else:
        return start_passport(uid)

def start_weekly(uid):
    user_state[uid] = {"flow": "weekly", "step": 1, "data": {}}
    return ("Панель недели 🗓️\n"
            "1/4) Один фокус на эту неделю (узел/навык). Пример: «Не пересиживаю убытки» "
            "или «Только один сетап A+ в день». Напиши свой фокус.")

def continue_weekly(uid, text):
    st = user_state.get(uid, {})
    step = st.get("step", 1)
    data = st.get("data", {})

    if step == 1:
        data["фокус"] = text.strip()
        user_state[uid] = {"flow": "weekly", "step": 2, "data": data}
        return ("2/4) План на 5 торговых дней: коротко — что делаешь ежедневно, чтобы двигаться к фокусу?"
                "\nПример: «Перед сессией — чек-лист входа; после — 2 строки в журнал»")
    elif step == 2:
        data["план"] = text.strip()
        user_state[uid] = {"flow": "weekly", "step": 3, "data": data}
        return ("3/4) Лимиты и рамки: max риск/день, stop-trading триггеры?"
                "\nПример: «Макс. -2R/день, после 2 подряд стопов — пауза 30 мин»")
    elif step == 3:
        data["лимиты"] = text.strip()
        user_state[uid] = {"flow": "weekly", "step": 4, "data": data}
        return ("4/4) Короткая ретроспектива недели (позже): как поймёшь, что неделя удалась?"
                "\nПример: «Выполнил 5/5 ритуалов, 0 нарушений по лимитам»")
    elif step == 4:
        data["критерий_успеха"] = text.strip()
        user_state[uid] = {"flow": None, "step": 0, "data": data}
        # TODO: здесь можно сохранить data в БД
        return ("Панель недели сохранена ✅\n"
                f"Фокус: {data.get('фокус')}\n"
                f"План: {data.get('план')}\n"
                f"Лимиты: {data.get('лимиты')}\n"
                f"Критерий успеха: {data.get('критерий_успеха')}")
    else:
        return start_weekly(uid)

def start_error(uid):
    user_state[uid] = {"flow": "error", "step": 1, "data": {}}
    return ("Разбор ошибки (MER+TOTE).\n"
            "Коротко опиши, что произошло (ситуация).")

def continue_error(uid, text):
    st = user_state.get(uid, {})
    step = st.get("step", 1)
    data = st.get("data", {})

    if step == 1:
        data["ситуация"] = text.strip()
        user_state[uid] = {"flow": "error", "step": 2, "data": data}
        return "Какое было <b>эмоциональное состояние</b> (M из MERCEDES)?"
    elif step == 2:
        data["emotion"] = text.strip()
        user_state[uid] = {"flow": "error", "step": 3, "data": data}
        return "Какие были <b>убеждения/мысли</b> (E)?"
    elif step == 3:
        data["beliefs"] = text.strip()
        user_state[uid] = {"flow": "error", "step": 4, "data": data}
        return "Что ты <b>сделал</b> (R — реакция/поведение)?"
    elif step == 4:
        data["reaction"] = text.strip()
        user_state[uid] = {"flow": "error", "step": 5, "data": data}
        return "Результат и вывод (S). Что менять в TOTE-петле в следующий раз?"
    elif step == 5:
        data["result"] = text.strip()
        user_state[uid] = {"flow": None, "step": 0, "data": data}
        # TODO: сохранить в БД
        return ("Готово ✅ Разбор сохранён (локально).\n"
                "Напомнить о ритуале «тайм-аут после ошибки» перед следующей сессией?")
    else:
        return start_error(uid)

# ====== Команды ======
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    history[uid] = []
    user_state[uid] = {"flow": None, "step": 0, "data": {}}
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник Innertrade.\nВыбери кнопку или напиши текст.\nКоманды: /ping /reset",
        reply_markup=build_main_menu()
    )

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    history[m.from_user.id] = []
    user_state[m.from_user.id] = {"flow": None, "step": 0, "data": {}}
    bot.send_message(m.chat.id, "Контекст очищен.", reply_markup=build_main_menu())

# ====== Кнопки (интенты) ======
INTENT_ALIASES = {
    "Ошибка": "error",
    "ТС / Стратегия": "ts",
    "Паспорт": "passport",
    "Панель недели": "weekly",
    "Материалы": "materials",
    "Прогресс": "progress",
    "Профиль": "profile",
    "Сброс": "reset_btn",
}

@bot.message_handler(func=lambda m: (m.text or "").strip() in INTENT_ALIASES.keys())
def on_intent_button(m):
    uid = m.from_user.id
    t = (m.text or "").strip()
    intent = INTENT_ALIASES[t]

    if intent == "reset_btn":
        history[uid] = []
        user_state[uid] = {"flow": None, "step": 0, "data": {}}
        bot.send_message(m.chat.id, "Контекст очищен. Выбери пункт меню.", reply_markup=build_main_menu())
        return

    if intent == "passport":
        reply = start_passport(uid)
        send_long(m.chat.id, reply)
        return

    if intent == "weekly":
        reply = start_weekly(uid)
        send_long(m.chat.id, reply)
        return

    if intent == "error":
        reply = start_error(uid)
        send_long(m.chat.id, reply)
        return

    if intent == "ts":
        # лёгкий вход в сценарий ТС — пока просто подсказка (будет расширяться)
        send_long(m.chat.id, "Хочешь собрать/пересобрать ТС. С чего начнём: подход/таймфреймы, вход/выход или риск?")
        return

    if intent == "materials":
        send_long(m.chat.id, "Материалы: MERCEDES, TOTE, архетипы, ограничивающие убеждения, базовая ТС, риск-менеджмент и пр. (каталог скоро будет в меню).")
        return

    if intent == "progress":
        send_long(m.chat.id, "Раздел «Прогресс»: скоро покажу % выполнения ритуалов, число нарушений лимитов и активные узлы недели.")
        return

    if intent == "profile":
        send_long(m.chat.id, "Профиль: паспорт, стиль, часы торговли, ограничения. (Пока храню локально, БД подключим — буду помнить между сессиями.)")
        return

# ====== Текущие пошаговые сценарии ======
@bot.message_handler(func=lambda m: True)
def on_text(m):
    uid = m.from_user.id
    text = (m.text or "").strip()
    st = user_state.get(uid, {"flow": None, "step": 0, "data": {}})

    # если пользователь в сценарии — продолжаем его
    if st.get("flow") == "passport":
        reply = continue_passport(uid, text)
        send_long(m.chat.id, reply)
        return

    if st.get("flow") == "weekly":
        reply = continue_weekly(uid, text)
        send_long(m.chat.id, reply)
        return

    if st.get("flow") == "error":
        reply = continue_error(uid, text)
        send_long(m.chat.id, reply)
        return

    # иначе — обычный GPT
    try:
        reply = ask_gpt(uid, text)
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply)

if __name__ == "__main__":
    # снимаем вебхук и запускаем polling + веб-сервер для /health
    try:
        bot.remove_webhook()
        logging.info("Webhook removed (ok)")
    except Exception as e:
        logging.warning(f"Webhook remove warn: {e}")

    import threading
    def run_flask():
        logging.info("Starting keepalive web server…")
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

    threading.Thread(target=run_flask, daemon=True).start()
    logging.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
