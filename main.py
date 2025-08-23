import os
import logging
from datetime import date, timedelta
from typing import Dict, Any

import telebot
from telebot import types

from openai import OpenAI
from flask import Flask
from sqlalchemy import (
    create_engine, Integer, String, Date, Text, JSON,
    UniqueConstraint, select
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

# ========= ENV =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY     = os.getenv("OPENAI_API_KEY")
DATABASE_URL   = os.getenv("DATABASE_URL")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets")
if not DATABASE_URL:
    raise RuntimeError("Нет DATABASE_URL в Secrets")

# ========= LOGS =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ========= BOT / GPT =========
bot    = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
client = OpenAI(api_key=OPENAI_KEY)

# На всякий случай снимем webhook
try:
    bot.remove_webhook()
    logging.info("Webhook removed (ok)")
except Exception as e:
    logging.warning(f"Webhook remove warn: {e}")

# ========= KEEPALIVE (Render / UptimeRobot) =========
app = Flask(__name__)

@app.route("/")
def home():
    return "Innertrade bot is alive"

@app.route("/health")
def health():
    return "pong"

# ========= DB (SQLAlchemy 2.0) =========
class Base(DeclarativeBase):
    pass

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

class UserProfile(Base):
    __tablename__ = "user_profile"
    id: Mapped[int]        = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int]     = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str]      = mapped_column(String(128), default="")
    created: Mapped[str]   = mapped_column(String(32), default="")

class Passport(Base):
    """
    Паспорт трейдера: храним всё в JSON для простоты.
    """
    __tablename__ = "passport"
    id: Mapped[int]    = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(Integer, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("tg_id", name="uq_passport_tg_id"),)

class WeeklyPanel(Base):
    """
    Панель недели: один актуальный срез на неделю (week_start - понедельник).
    """
    __tablename__ = "weekly_panel"
    id: Mapped[int]          = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int]       = mapped_column(Integer, index=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)
    focus: Mapped[str]       = mapped_column(Text, default="")
    plan: Mapped[str]        = mapped_column(Text, default="")
    limits: Mapped[str]      = mapped_column(Text, default="")
    retro: Mapped[str]       = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("tg_id", "week_start", name="uq_week_tg"),)

Base.metadata.create_all(engine)

# ========= MEMORY (в рамках процесса) =========
history: Dict[int, list] = {}  # диалог с GPT
state: Dict[int, Dict[str, Any]] = {}  # простая FSM для «паспорт», «панель», «ошибка»

def gpt_reply(uid: int, text: str) -> str:
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

def main_menu() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(
        types.KeyboardButton("🧩 Ошибка"),
        types.KeyboardButton("🧠 Стратегия"),
        types.KeyboardButton("💬 Поговорим"),
    )
    kb.row(
        types.KeyboardButton("📇 Паспорт"),
        types.KeyboardButton("📆 Панель недели"),
        types.KeyboardButton("📚 Материалы"),
    )
    kb.row(
        types.KeyboardButton("♻️ Сброс"),
        types.KeyboardButton("/ping"),
    )
    return kb

def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())  # понедельник текущей недели

# ========= HANDLERS =========
@bot.message_handler(commands=["start"])
def cmd_start(m):
    uid = m.from_user.id
    history[uid] = []
    state.pop(uid, None)
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
    uid = m.from_user.id
    history[uid] = []
    state.pop(uid, None)
    bot.send_message(m.chat.id, "Контекст очищен.", reply_markup=main_menu())

# ===== КНОПКИ (интенты) =====
@bot.message_handler(func=lambda x: (x.text or "").strip() in {
    "🧩 Ошибка","🧠 Стратегия","💬 Поговорим",
    "📇 Паспорт","📆 Панель недели","📚 Материалы","♻️ Сброс"
})
def on_menu(m):
    uid = m.from_user.id
    t = (m.text or "").strip()

    # Сброс локальной FSM
    if t == "♻️ Сброс":
        history[uid] = []
        state.pop(uid, None)
        bot.send_message(m.chat.id, "Ок, начинаем заново.", reply_markup=main_menu())
        return

    if t == "📇 Паспорт":
        # старт паспорта (6 вопросов)
        state[uid] = {"mode": "passport", "step": 1, "data": {}}
        bot.send_message(m.chat.id, "Паспорт трейдера.\n1/6) На каком рынке/инструментах торгуешь? (акции США, EURUSD, BTC, фьючерсы…)")
        return

    if t == "📆 Панель недели":
        # заведём/отредактируем текущую неделю
        state[uid] = {"mode": "weekly", "step": 1, "buf": {}}
        bot.send_message(m.chat.id,
            "Панель недели:\n1/4) Главный фокус недели (одним предложением).")
        return

    if t == "🧩 Ошибка":
        state[uid] = {"mode": "error", "step": 1, "buf": {}}
        bot.send_message(m.chat.id, "Разбор ошибки (mini MERCEDES+TOTE).\n1) Опиши последнюю ошибку в 1–2 предложениях.")
        return

    if t == "🧠 Стратегия":
        # лёгкий вход в М2: спросим, чего именно хочет
        state[uid] = {"mode": "ts", "step": 1, "buf": {}}
        bot.send_message(m.chat.id,
            "Ок, стратегию. Что нужно сейчас?\n"
            "1) Собрать с нуля\n2) Пересобрать/подправить текущую\n3) Не знаю с чего начать")
        return

    if t == "💬 Поговорим":
        # свободный чат
        state.pop(uid, None)
        bot.send_message(m.chat.id, "О чём поговорим в контексте трейдинга? Можешь задать вопрос.")
        return

    if t == "📚 Материалы":
        bot.send_message(m.chat.id,
            "Материалы:\n"
            "• Теория MERCEDES, TOTE\n"
            "• Архетипы/роли\n"
            "• База по ТС, риск-менеджмент\n"
            "Скажи, что открыть текстом: например «MERCEDES» или «риск-менеджмент».")
        return

# ===== FSM: ПАСПОРТ =====
@bot.message_handler(func=lambda m: state.get(m.from_user.id, {}).get("mode") == "passport")
def passport_flow(m):
    uid = m.from_user.id
    st = state[uid]
    step = st["step"]
    data = st["data"]

    if step == 1:
        data["market"] = m.text.strip()
        st["step"] = 2
        bot.send_message(m.chat.id, "2/6) Твой стиль: скальп/интрадей/свинг/позиционно?")
        return

    if step == 2:
        data["style"] = m.text.strip()
        st["step"] = 3
        bot.send_message(m.chat.id, "3/6) Время торговли (сессии/часы)?")
        return

    if step == 3:
        data["time"] = m.text.strip()
        st["step"] = 4
        bot.send_message(m.chat.id, "4/6) Риск-профиль: риск на сделку (% депозита)?")
        return

    if step == 4:
        data["risk"] = m.text.strip()
        st["step"] = 5
        bot.send_message(m.chat.id, "5/6) Типичные ошибки (3 шт. через запятую)?")
        return

    if step == 5:
        data["errors"] = m.text.strip()
        st["step"] = 6
        bot.send_message(m.chat.id, "6/6) Ритуалы/привычки (до/во время/после сессии)?")
        return

    if step == 6:
        data["rituals"] = m.text.strip()
        # Сохраним в БД (upsert)
        with Session(engine) as s:
            row = s.scalar(select(Passport).where(Passport.tg_id == uid))
            if not row:
                row = Passport(tg_id=uid, data=data)
                s.add(row)
            else:
                row.data = data
            s.commit()
        state.pop(uid, None)
        bot.send_message(m.chat.id, "Готово ✅ Паспорт сохранён.", reply_markup=main_menu())
        return

# ===== FSM: ПАНЕЛЬ НЕДЕЛИ =====
@bot.message_handler(func=lambda m: state.get(m.from_user.id, {}).get("mode") == "weekly")
def weekly_flow(m):
    uid = m.from_user.id
    st = state[uid]
    step = st["step"]
    buf = st["buf"]

    if step == 1:
        buf["focus"] = m.text.strip()
        st["step"] = 2
        bot.send_message(m.chat.id, "2/4) План недели (3–5 пунктов, можно в одну строку через «;»).")
        return

    if step == 2:
        buf["plan"] = m.text.strip()
        st["step"] = 3
        bot.send_message(m.chat.id, "3/4) Лимиты/ограничения (вне рынка, риск, время).")
        return

    if step == 3:
        buf["limits"] = m.text.strip()
        st["step"] = 4
        bot.send_message(m.chat.id, "4/4) Короткая ретроспектива прошлой недели (1–2 предложения).")
        return

    if step == 4:
        buf["retro"] = m.text.strip()
        wk = week_monday(date.today())
        with Session(engine) as s:
            row = s.scalar(select(WeeklyPanel).where(
                (WeeklyPanel.tg_id == uid) & (WeeklyPanel.week_start == wk)
            ))
            if not row:
                row = WeeklyPanel(
                    tg_id=uid, week_start=wk,
                    focus=buf.get("focus",""), plan=buf.get("plan",""),
                    limits=buf.get("limits",""), retro=buf.get("retro","")
                )
                s.add(row)
            else:
                row.focus  = buf.get("focus","")
                row.plan   = buf.get("plan","")
                row.limits = buf.get("limits","")
                row.retro  = buf.get("retro","")
            s.commit()
        state.pop(uid, None)
        bot.send_message(
            m.chat.id,
            "Панель недели сохранена ✅\n"
            f"• Фокус: {buf['focus']}\n"
            f"• План: {buf['plan']}\n"
            f"• Лимиты: {buf['limits']}\n"
            f"• Ретро: {buf['retro']}",
            reply_markup=main_menu()
        )
        return

# ===== FSM: ОШИБКА (мини MERCEDES+TOTE) =====
@bot.message_handler(func=lambda m: state.get(m.from_user.id, {}).get("mode") == "error")
def error_flow(m):
    uid = m.from_user.id
    st = state[uid]
    step = st["step"]
    buf  = st["buf"]

    if step == 1:
        buf["desc"] = m.text.strip()
        st["step"] = 2
        bot.send_message(m.chat.id, "2) Что предшествовало (контекст/триггер)?")
        return

    if step == 2:
        buf["trigger"] = m.text.strip()
        st["step"] = 3
        bot.send_message(m.chat.id, "3) Мысль/эмоция/реакция в моменте (коротко).")
        return

    if step == 3:
        buf["mercedes"] = m.text.strip()
        st["step"] = 4
        bot.send_message(m.chat.id, "4) Желаемый новый шаг (что сделаешь в следующий раз иначе)?")
        return

    if step == 4:
        buf["next"] = m.text.strip()
        # здесь можно сохранить в БД как часть паспорта (errors_log) — опционально
        try:
            with Session(engine) as s:
                row = s.scalar(select(Passport).where(Passport.tg_id == uid))
                if not row:
                    row = Passport(tg_id=uid, data={})
                    s.add(row)
                    s.flush()
                data = row.data or {}
                log = data.get("errors_log", [])
                log.append(buf)
                data["errors_log"] = log
                row.data = data
                s.commit()
        except Exception as e:
            logging.warning(f"Save error log warn: {e}")

        state.pop(uid, None)
        bot.send_message(
            m.chat.id,
            "Готово ✅ Короткий разбор сохранён.\n"
            "Если хочешь — скажи «ещё ошибка» или вернись в меню.",
            reply_markup=main_menu()
        )
        return

# ===== FSM: СТРАТЕГИЯ (вход в Модуль 2) =====
@bot.message_handler(func=lambda m: state.get(m.from_user.id, {}).get("mode") == "ts")
def ts_flow(m):
    uid = m.from_user.id
    st  = state[uid]
    step = st["step"]
    buf  = st["buf"]

    if step == 1:
        choice = (m.text or "").strip()
        buf["choice"] = choice
        st["step"] = 2
        bot.send_message(
            m.chat.id,
            "Ок. Для старта назови:\n"
            "• рынок/инструменты\n• таймфрейм\n• базовый подход (например, пробой/откат/диапазон)\n\n"
            "Можно одной строкой."
        )
        return

    if step == 2:
        buf["seed"] = m.text.strip()
        state.pop(uid, None)
        # На этом этапе пока отдадим в GPT — позже подменим ответ на шаблон М2.
        answer = gpt_reply(uid,
            f"Пользователь хочет стратегию. Исходные данные: {buf}. "
            "Собери минимальную версию ТС: вход/стоп/сопровождение/выход/риск (пулеверс). "
            "Выведи списком кратко и структурировано."
        )
        bot.send_message(m.chat.id, answer, reply_markup=main_menu())
        return

# ===== ФОЛБЭК: свободный текст =====
@bot.message_handler(func=lambda _: True)
def any_text(m):
    uid = m.from_user.id
    # если есть активный режим — обработку уже перехватят FSM-хэндлеры выше
    # сюда попадёт только свободный текст без активной FSM
    try:
        reply = gpt_reply(uid, m.text or "")
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    # отвечаем без reply_to (чтобы не было «ссылки» на сообщение пользователя)
    bot.send_message(m.chat.id, reply)

if __name__ == "__main__":
    logging.info("Starting keepalive web server…")
    # На Render порт задаётся переменной PORT
    port = int(os.getenv("PORT", "10000"))
    # Запускаем Flask в отдельном потоке через встроенный сервер
    from threading import Thread
    Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False), daemon=True).start()

    logging.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
