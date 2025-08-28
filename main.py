import os
import json
import logging
from datetime import datetime, timezone

from flask import Flask, request, abort, jsonify
from telebot import TeleBot, types
from telebot.types import Update

# ----------------- ЛОГИ -----------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("innertrade")

# ----------------- ENV ------------------
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TG_WEBHOOK_SECRET  = os.getenv("TG_WEBHOOK_SECRET")   # любой ваш секрет (тот же, что устанавливали в setWebhook&secret_token=)
PUBLIC_URL         = os.getenv("PUBLIC_URL")          # https://innertrade-bot.onrender.com
WEBHOOK_PATH       = os.getenv("WEBHOOK_PATH")        # например: wbhk_9t3x
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
DATABASE_URL       = os.getenv("DATABASE_URL", "")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing")
if not TG_WEBHOOK_SECRET:
    raise RuntimeError("TG_WEBHOOK_SECRET missing")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if not WEBHOOK_PATH:
    raise RuntimeError("WEBHOOK_PATH missing (random safe path)")

# ----------------- BOT ------------------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

def greet(chat_id, first_name: str | None = None):
    name = first_name or "друг"
    bot.send_message(
        chat_id,
        f"👋 Привет, {name}! Можем просто поговорить — напиши, что болит в торговле.\n"
        f"Или выбери пункт ниже.",
        reply_markup=main_menu()
    )

# ----------------- /health ------------------
app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})

# ----------------- WEBHOOK ------------------
@app.post(f"/{WEBHOOK_PATH}")
def telegram_webhook():
    # Верификация секрета от Telegram (важно, иначе будут 401)
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        log.warning("Webhook secret mismatch")
        abort(401)

    if not request.is_json:
        abort(415)

    try:
        update = Update.de_json(request.get_json(force=True))
        bot.process_new_updates([update])
    except Exception as e:
        # Никогда не отдаём 500 Телеге — иначе getWebhookInfo будет показывать last_error_message: 500
        log.exception("Error while processing update: %s", e)
        return "OK", 200

    return "OK", 200

# ----------------- БАЗОВЫЕ КОМАНДЫ ------------------
@bot.message_handler(commands=["start", "menu"])
def cmd_start(m):
    greet(m.chat.id, m.from_user.first_name)

@bot.message_handler(commands=["reset"])
def cmd_reset(m):
    greet(m.chat.id, m.from_user.first_name)

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    # без похода в БД — лаконичная диагностика
    payload = {
        "ok": True,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "intent": "greet",
        "step": "idle",
        "db": "ok" if DATABASE_URL else "none",
    }
    bot.send_message(m.chat.id, f"```\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```", parse_mode="Markdown")

# ----------------- ИНТЕНТЫ-КНОПКИ ------------------
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def intent_error(m):
    bot.send_message(
        m.chat.id,
        "Опиши основную ошибку 1–2 предложениями.\n"
        "_Например:_ «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю на первой коррекции».",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def intent_strategy(m):
    bot.send_message(
        m.chat.id,
        "Соберём скелет ТС:\n"
        "1) Цели\n2) Стиль (дневной/свинг)\n3) Рынки/ТФ\n"
        "4) Вход/выход\n5) Риск (стоп/лимиты)\n6) Сопровождение\n7) Тест на истории/демо",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def intent_passport(m):
    bot.send_message(
        m.chat.id,
        "Паспорт трейдера — 1/6. На каких рынках/инструментах ты торгуешь?",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def intent_week(m):
    bot.send_message(
        m.chat.id,
        "Панель недели:\n• Фокус недели\n• 1–2 цели\n• Лимиты\n• Ритуалы\n• Короткая ретро в конце недели",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def intent_panic(m):
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку\n3) 10 медленных вдохов\n"
        "4) Запиши триггер (что выбило)\n5) Вернись к плану сделки или закрой по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def intent_start_help(m):
    bot.send_message(
        m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\n"
        "С чего начнём: паспорт или фокус недели?",
        reply_markup=main_menu()
    )

# ----------------- СВОБОДНЫЙ ТЕКСТ ------------------
@bot.message_handler(content_types=["text"])
def freestyle(m):
    text = (m.text or "").strip().lower()

    # Очень короткая «естественная» реакция + мягкое направление в меню
    if any(greet_word in text for greet_word in ["привет", "здрав", "hi", "hello"]):
        bot.send_message(
            m.chat.id,
            "Привет! Можем поговорить свободно — расскажи, что болит в торговле. "
            "Или ткни в кнопку ниже.",
            reply_markup=main_menu()
        )
        return

    # Если пользователь сразу пишет о проблеме — ответ короткий и по делу
    if any(w in text for w in ["ошиб", "правил", "просад", "сует", "стоп", "тейк"]):
        bot.send_message(
            m.chat.id,
            "Понимаю. Чтобы двигаться по шагам и не потерять мысль — нажми «🚑 У меня ошибка», "
            "и я проведу тебя через короткий разбор.",
            reply_markup=main_menu()
        )
        return

    # Фолбэк по умолчанию
    bot.send_message(
        m.chat.id,
        "Принял. Чтобы было быстрее, выбери пункт в меню ниже или напиши /menu.",
        reply_markup=main_menu()
    )

# ----------------- APP RUN ------------------
if __name__ == "__main__":
    # Никаких polling — только webhook через Flask
    port = int(os.getenv("PORT", "10000"))
    log.info("Starting Flask on 0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port)
