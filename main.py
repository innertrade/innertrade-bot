# main.py
import os
import logging
from datetime import datetime

from flask import Flask
from telebot import TeleBot, types

from openai import OpenAI
from sqlalchemy import (
    create_engine, text, String, Integer, DateTime, JSON
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

# ========= ENV =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")  # Neon/Render

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets")
if not OPENAI_API_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets")
if not DATABASE_URL:
    raise RuntimeError("Нет DATABASE_URL в Secrets")

# Приведение URL БД к драйверу psycopg3
# Neon даёт postgresql://...; для SQLAlchemy + psycopg нужен postgresql+psycopg://...
def normalize_db_url(url: str) -> str:
    u = url.strip()
    if u.startswith("postgres://"):
        u = "postgresql://" + u[len("postgres://"):]
    if u.startswith("postgresql://"):
        u = "postgresql+psycopg://" + u[len("postgresql://"):]
    return u

DATABASE_URL = normalize_db_url(DATABASE_URL)

# ========= LOGS =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("innertrade")

# ========= OPENAI =========
client = OpenAI(api_key=OPENAI_API_KEY)

# ========= DB (SQLAlchemy 2.x + psycopg3) =========
class Base(DeclarativeBase):
    pass

class UserPassport(Base):
    __tablename__ = "user_passport"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(index=True)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class WeekPanel(Base):
    __tablename__ = "week_panel"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(index=True)
    week_key: Mapped[str] = mapped_column(String(16))  # например 2025-W34
    focus: Mapped[str] = mapped_column(String(255), default="")
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    retro: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class ErrorLog(Base):
    __tablename__ = "error_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    future=True,
)

with engine.begin() as conn:
    Base.metadata.create_all(conn)
    log.info("DB: tables ensured")

# ========= TELEGRAM BOT =========
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# на всякий — снять webhook
try:
    bot.remove_webhook()
except Exception as e:
    log.warning(f"Webhook remove warn: {e}")

# Память диалога в ОЗУ (поверх БД; БД пойдёт под паспорт/панель/ошибки)
history: dict[int, list[dict]] = {}

SYSTEM_PROMPT = (
    "Ты ИИ-наставник трейдера Innertrade. Отвечай кратко, по делу, "
    "используй шаги и чек-листы, когда уместно. "
    "Если пользователь выбирает один из пунктов меню — веди по сценарию."
)

def ask_gpt(uid: int, text: str) -> str:
    msgs = history.setdefault(uid, [])
    # фиксируем системный промпт в начале диалога
    if not msgs or msgs[0].get("role") != "system":
        msgs.insert(0, {"role": "system", "content": SYSTEM_PROMPT})
    msgs.append({"role": "user", "content": text})

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.5,
        messages=msgs,
    )
    reply = (resp.choices[0].message.content or "").strip()
    msgs.append({"role": "assistant", "content": reply})
    return reply

def send_long(chat_id: int, text: str):
    MAX = 3500
    for i in range(0, len(text), MAX):
        bot.send_message(chat_id, text[i:i+MAX])

# ===== КНОПКИ (интенты) =====
INTENT_BUTTONS = [
    "❗ У меня ошибка",
    "🧠 Хочу стратегию",
    "🧭 Не знаю, с чего начать",
    "🆘 Экстренно: я поплыл",
    "📈 Мой прогресс",
    "🪪 Паспорт/Профиль",
    "📚 Материалы",
    "💬 Поговорим",
    "🔄 Сброс",
]

def main_menu() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(INTENT_BUTTONS[0], INTENT_BUTTONS[1])
    kb.row(INTENT_BUTTONS[2], INTENT_BUTTONS[3])
    kb.row(INTENT_BUTTONS[4], INTENT_BUTTONS[5])
    kb.row(INTENT_BUTTONS[6], INTENT_BUTTONS[7])
    kb.row(INTENT_BUTTONS[8])
    return kb

# ===== START =====
@bot.message_handler(commands=["start"])
def cmd_start(m):
    uid = m.from_user.id
    history[uid] = []  # сброс контекста
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник <b>Innertrade</b>.\n"
        "Выбери кнопку или напиши текст.\n"
        "Команды: /ping /reset",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["reset"])
def cmd_reset(m):
    history[m.from_user.id] = []
    bot.send_message(m.chat.id, "Контекст очищен. Меню обновлено.", reply_markup=main_menu())

# ===== Обработка кнопок =====
@bot.message_handler(func=lambda x: (x.text or "") in INTENT_BUTTONS)
def on_buttons(m):
    uid = m.from_user.id
    t = (m.text or "").strip()

    mapping = {
        "❗ У меня ошибка": "Давай сделаем мини-разбор по MERCEDES+TOTE. Опиши ситуацию: вход/стоп/эмоции/мысли.",
        "🧠 Хочу стратегию": "Соберём твою ТС. Расскажи: рынок, стиль (скальп/день/свинг), таймфрейм, входы/стоп/сопровождение.",
        "🧭 Не знаю, с чего начать": "Давай определим профиль. Какие цели? Опыт? Ресурс/ограничения? Затем предложу дорожную карту.",
        "🆘 Экстренно: я поплыл": "Экстренный протокол: 1) Стоп-торговля на 20 минут. 2) Дыхание 4-7-8. 3) Что произошло? 4) Что делаем дальше?",
        "📈 Мой прогресс": "Покажу, что зафиксировано в паспорте и панели недели. Чем дополним на этой неделе?",
        "🪪 Паспорт/Профиль": "Паспорт трейдера: цели, ограничения, антириски, триггеры. Что обновим?",
        "📚 Материалы": "Материалы: MERCEDES, TOTE, архетипы, чек-листы входа/риска, шаблон ТС. Что открыть?",
        "💬 Поговорим": "Окей. Какая тема — рынок, психология, риск, дисциплина?",
        "🔄 Сброс": "Контекст очищен. Готов продолжать.",
    }

    # спец-кейс "Сброс"
    if t == "🔄 Сброс":
        history[uid] = []
        bot.send_message(m.chat.id, "Контекст очищен. Меню обновлено.", reply_markup=main_menu())
        return

    try:
        reply = ask_gpt(uid, mapping.get(t, t))
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply)

# ===== Любой текст (кроме кнопок/команд) =====
@bot.message_handler(func=lambda m: True)
def on_text(m):
    uid = m.from_user.id
    txt = (m.text or "").strip()

    # чтобы /ping текстом не уводил в GPT потоки
    if txt.lower() == "ping":
        bot.send_message(m.chat.id, "pong")
        return

    try:
        reply = ask_gpt(uid, txt)
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    send_long(m.chat.id, reply)

# ========= KEEPALIVE (Flask) =========
app = Flask(__name__)

@app.get("/")
def root():
    return "Innertrade bot is alive"

@app.get("/health")
def health():
    return "pong"

# ========= RUN =========
if __name__ == "__main__":
    log.info("Starting keepalive web server…")
    # Flask в фоновом потоке поднимет UptimeRobot health-endpoint
    from threading import Thread
    Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000"))), daemon=True).start()

    log.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
