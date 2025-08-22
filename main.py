import os
import logging
import threading
from datetime import datetime
from typing import Optional

import telebot
from telebot import types
from openai import OpenAI
from flask import Flask, jsonify

# === ENV ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets")
if not DATABASE_URL:
    raise RuntimeError("Нет DATABASE_URL в Secrets")

# === LOGS ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# === OpenAI client ===
client = OpenAI(api_key=OPENAI_KEY)

# === Telegram bot ===
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# === Keepalive Flask ===
app = Flask(__name__)

@app.get("/")
def root():
    return "Innertrade bot: OK"

@app.get("/health")
def health():
    return jsonify({"status": "pong", "service": "Innertrade"})

def run_keepalive():
    port = int(os.getenv("PORT", "10000"))
    logging.info("Starting keepalive web server…")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# === DB (SQLAlchemy) ===
from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Text, DateTime, ForeignKey, JSON, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, scoped_session

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String(255))
    first_name = Column(String(255))
    last_name = Column(String(255))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now(), server_default=func.now())

    passport = relationship("Passport", back_populates="user", uselist=False, cascade="all, delete-orphan")

class Passport(Base):
    __tablename__ = "passports"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Базовое ядро полей «паспорт данных» — пока компактно, дальше расширим
    risk_profile = Column(String(255))          # пример: "консервативный/умеренный/агрессивный"
    style = Column(String(255))                 # пример: "интрадей/свинг/позиционный"
    timeframes = Column(String(255))            # пример: "M15,H1" (через запятую)
    instruments = Column(String(255))           # пример: "BTCUSDT, ES"
    triggers_blocklist = Column(Text)           # «фразы-запреты» (через точку с запятой)
    rituals = Column(Text)                      # короткий список привычек/ритуалов
    notes = Column(Text)                        # любые заметки
    version = Column(Integer, default=1)
    updated_at = Column(DateTime, onupdate=func.now(), server_default=func.now())

    user = relationship("User", back_populates="passport")

def create_tables():
    Base.metadata.create_all(engine)
    logging.info("DB: tables ensured")

def db_session():
    return SessionLocal()

def get_or_create_user(tg_id: int, username: Optional[str], first: Optional[str], last: Optional[str]) -> User:
    session = db_session()
    try:
        u: Optional[User] = session.query(User).filter_by(tg_id=tg_id).one_or_none()
        if u is None:
            u = User(tg_id=tg_id, username=username, first_name=first, last_name=last)
            session.add(u)
            session.commit()
            session.refresh(u)
            logging.info(f"DB: created user tg_id={tg_id}")
        else:
            # Мягкое обновление имени/ника, если изменилось
            changed = False
            if u.username != username:
                u.username = username; changed = True
            if u.first_name != first:
                u.first_name = first; changed = True
            if u.last_name != last:
                u.last_name = last; changed = True
            if changed:
                session.commit()
        # Убедимся, что у юзера есть паспорт (пока пустой)
        if u.passport is None:
            p = Passport(user_id=u.id)
            session.add(p)
            session.commit()
            session.refresh(u)
        return u
    finally:
        session.close()

# === Simple GPT wrapper (контекст пока держим в памяти, позже перенесем в БД) ===
history = {}  # uid -> [{"role": "...", "content": "..."}]

def ask_gpt(uid: int, text: str) -> str:
    msgs = history.setdefault(uid, [])
    msgs.append({"role": "user", "content": text})
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.4,
        messages=msgs
    )
    reply = (resp.choices[0].message.content or "").strip()
    msgs.append({"role": "assistant", "content": reply})
    return reply

def send_long(chat_id: int, text: str):
    MAX = 3500
    for i in range(0, len(text), MAX):
        bot.send_message(chat_id, text[i:i+MAX])

# === UI: клавиатура с интентами (минимальный набор) ===
def main_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(
        types.KeyboardButton("🪪 Профиль"),
        types.KeyboardButton("📈 Мой прогресс")
    )
    kb.row(
        types.KeyboardButton("🚨 У меня ошибка"),
        types.KeyboardButton("🧱 Собрать ТС")
    )
    kb.row(
        types.KeyboardButton("🧭 Не знаю с чего начать"),
        types.KeyboardButton("🆘 Экстренно: поплыл")
    )
    kb.row(
        types.KeyboardButton("🗂 Материалы"),
        types.KeyboardButton("Сброс")
    )
    return kb

# === START / PING / RESET ===
@bot.message_handler(commands=['start'])
def cmd_start(m):
    bot.remove_webhook()  # на всякий случай
    uid = m.from_user.id
    get_or_create_user(
        tg_id=uid,
        username=m.from_user.username,
        first=m.from_user.first_name,
        last=m.from_user.last_name
    )
    history[uid] = []
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник Innertrade.\nВыбери кнопку или напиши текст.\nКоманды: /ping /reset",
        reply_markup=main_kb()
    )

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    history[m.from_user.id] = []
    bot.send_message(m.chat.id, "Контекст очищен.", reply_markup=main_kb())

# === ПРОФИЛЬ: вывод текущего состояния паспорта ===
def render_passport_text(p: Passport) -> str:
    def dash(x): return x if (x and x.strip()) else "—"
    return (
        "<b>🪪 Паспорт трейдера</b>\n"
        f"• Риск-профиль: {dash(p.risk_profile)}\n"
        f"• Стиль: {dash(p.style)}\n"
        f"• Таймфреймы: {dash(p.timeframes)}\n"
        f"• Инструменты: {dash(p.instruments)}\n"
        f"• Запретные триггеры: {dash(p.triggers_blocklist)}\n"
        f"• Ритуалы: {dash(p.rituals)}\n"
        f"• Заметки: {dash(p.notes)}\n"
        f"• Версия: {p.version or 1}\n"
        f"• Обновлён: {p.updated_at.strftime('%Y-%m-%d %H:%M') if p.updated_at else '—'}"
    )

@bot.message_handler(func=lambda m: (m.text or "").strip() == "🪪 Профиль")
def handle_profile(m):
    session = db_session()
    try:
        u = session.query(User).filter_by(tg_id=m.from_user.id).one_or_none()
        if not u or not u.passport:
            bot.send_message(m.chat.id, "Паспорт пока не найден. Нажми /start и попробуй ещё раз.")
            return
        text = render_passport_text(u.passport)
        bot.send_message(m.chat.id, text)
    finally:
        session.close()

# === Заглушки-интенты (пока без логики, просто подтверждаем кнопки) ===
@bot.message_handler(func=lambda m: (m.text or "").strip() == "📈 Мой прогресс")
def handle_progress(m):
    bot.send_message(m.chat.id, "Здесь будет прогресс по курсу/боту (после следующего этапа).")

@bot.message_handler(func=lambda m: (m.text or "").strip() == "🚨 У меня ошибка")
def handle_error_intent(m):
    bot.send_message(m.chat.id, "OK. На следующем этапе включим мини-разбор ошибки (Mercedes+TOTE) с сохранением в БД.")

@bot.message_handler(func=lambda m: (m.text or "").strip() == "🧱 Собрать ТС")
def handle_build_ts(m):
    bot.send_message(m.chat.id, "Включим конструктор ТС после подключения паспорт/ошибок. Следующий этап.")

@bot.message_handler(func=lambda m: (m.text or "").strip() == "🧭 Не знаю с чего начать")
def handle_dont_know(m):
    bot.send_message(m.chat.id, "Сделаем навигатор (вопросы → подсказанный маршрут) на одном из следующих этапов.")

@bot.message_handler(func=lambda m: (m.text or "").strip() == "🆘 Экстренно: поплыл")
def handle_emergency(m):
    bot.send_message(m.chat.id, "Подключим аварийный план (быстрые действия + лог) на следующем этапе.")

@bot.message_handler(func=lambda m: (m.text or "").strip() == "🗂 Материалы")
def handle_materials(m):
    bot.send_message(m.chat.id, "Материалы/подсказки добавим чуть позже (после основной логики сохранений).")

@bot.message_handler(func=lambda m: (m.text or "").strip() == "Сброс")
def handle_clear(m):
    history[m.from_user.id] = []
    bot.send_message(m.chat.id, "Контекст очищен. Что дальше?", reply_markup=main_kb())

# === Fallback: любой другой текст пока уходит в GPT (позже перехватим интентами) ===
@bot.message_handler(func=lambda _m: True)
def on_text(m):
    uid = m.from_user.id
    reply = ask_gpt(uid, m.text or "")
    send_long(m.chat.id, reply)

if __name__ == "__main__":
    # DB
    create_tables()

    # снять webhook и стартануть Flask + polling
    try:
        bot.remove_webhook()
        logging.info("Webhook removed (ok)")
    except Exception as e:
        logging.warning(f"Webhook remove warn: {e}")

    threading.Thread(target=run_keepalive, daemon=True).start()
    logging.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
