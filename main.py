# main.py — Innertrade (интенты + БД + сценарии)
import os
import re
import logging
from datetime import datetime, date, timedelta

import telebot
from telebot import types
from openai import OpenAI

from flask import Flask
from threading import Thread

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Text,
    Date, DateTime, func
)
from sqlalchemy.orm import declarative_base, sessionmaker

# ========= ENV =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY     = os.getenv("OPENAI_API_KEY")
DATABASE_URL   = os.getenv("DATABASE_URL")

if not TELEGRAM_TOKEN: raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets")
if not OPENAI_KEY:     raise RuntimeError("Нет OPENAI_API_KEY в Secrets")
if not DATABASE_URL:   raise RuntimeError("Нет DATABASE_URL в Secrets")

# ========= LOG =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ========= OPENAI =========
client = OpenAI(api_key=OPENAI_KEY)

SYSTEM_PROMPT = (
    "Ты Kai — ИИ-наставник проекта Innertrade. "
    "Всегда трактуй слова 'паспорт', 'панель недели', 'ошибка' в контексте трейдинга. "
    "Если пользователь выбрал интент из меню — не спорь и не уточняй вне сценария. "
    "Коротко и по делу, русским языком."
)

def gpt_reply(history_msgs):
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.4,
        messages=[{"role":"system","content":SYSTEM_PROMPT}] + history_msgs
    )
    return (resp.choices[0].message.content or "").strip()

# ========= BOT =========
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ========= KEEPALIVE =========
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return "pong"

def run_server():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))

Thread(target=run_server, daemon=True).start()

# ========= DB =========
Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

class UserProfile(Base):
    __tablename__ = "user_profile"
    tg_id      = Column(BigInteger, primary_key=True)
    full_name  = Column(String(200))
    market     = Column(String(200))   # рынок/инструменты
    timeframe  = Column(String(100))
    style      = Column(String(200))   # стиль торговли
    risk       = Column(String(100))   # риск в % на сделку
    mistakes   = Column(Text)          # частые ошибки (свободный текст)
    goal_month = Column(Text)          # цель на месяц
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class WeeklyPanel(Base):
    __tablename__ = "weekly_panel"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    tg_id      = Column(BigInteger, index=True)
    week_start = Column(Date)          # понедельник недели
    focus      = Column(Text)
    plan       = Column(Text)
    limits     = Column(Text)
    retro      = Column(Text)          # короткая ретро по итогам недели
    created_at = Column(DateTime, server_default=func.now())

class ErrorReport(Base):
    __tablename__ = "error_report"
    id        = Column(Integer, primary_key=True, autoincrement=True)
    tg_id     = Column(BigInteger, index=True)
    created_at= Column(DateTime, server_default=func.now())
    context   = Column(Text)   # что случилось
    mercedes  = Column(Text)   # мысли/эмоции/реакции (упрощённо)
    tote      = Column(Text)   # как изменим цикл TOTE

Base.metadata.create_all(engine)

# ========= STATE (простая FSM в памяти) =========
state = {}  # uid -> dict(flow=..., step=..., data={})

def set_state(uid, flow=None, step=0, data=None):
    state[uid] = {"flow": flow, "step": step, "data": data or {}}

def get_state(uid):
    return state.get(uid, {"flow": None, "step": 0, "data": {}})

# ========= UI =========
BTN = {
    "err": "🚑 У меня ошибка",
    "strat": "🧩 Хочу стратегию",
    "week": "🗒 Панель недели",
    "pass": "📄 Паспорт",
    "prog": "📊 Мой прогресс",
    "mats": "📚 Материалы",
    "talk": "💬 Поговорим",
    "reset": "🔄 Сброс",
}

def menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN["err"], BTN["strat"])
    kb.row(BTN["week"], BTN["pass"])
    kb.row(BTN["prog"], BTN["mats"])
    kb.row(BTN["talk"], BTN["reset"])
    return kb

def send(chat_id, text):
    bot.send_message(chat_id, text, reply_markup=menu_kb())

# ========= HELPERS =========
def norm(text: str) -> str:
    t = (text or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t

INTENTS = {
    "🚑 у меня ошибка":"error", "у меня ошибка":"error", "ошибка":"error",
    "🧩 хочу стратегию":"strategy", "хочу стратегию":"strategy",
    "💬 поговорим":"talk", "поговорим":"talk",
    "📄 паспорт":"passport", "паспорт":"passport",
    "🗒 панель недели":"weekpanel", "панель недели":"weekpanel", "панель":"weekpanel",
    "📚 материалы":"materials", "материалы":"materials",
    "📊 мой прогресс":"progress", "мой прогресс":"progress",
    "/menu":"menu", "меню":"menu",
    "/reset":"reset", "сброс":"reset", "🔄 сброс":"reset",
    "/ping":"ping", "ping":"ping"
}

def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())

# ========= FLOWS =========
def start_passport(uid, chat_id, username):
    set_state(uid, flow="passport", step=1, data={"full_name": username})
    send(chat_id, "Паспорт трейдера.\n1/6) На каком рынке/инструментах торгуешь? (пример: акции США, EURUSD, BTC, фьючерсы…)")

def handle_passport(uid, chat_id, msg):
    s = get_state(uid)
    step = s["step"]
    data = s["data"]

    if step == 1:
        data["market"] = msg
        set_state(uid, "passport", 2, data)
        send(chat_id, "2/6) Рабочий таймфрейм(ы)? (пример: M5/H1/D1)")
    elif step == 2:
        data["timeframe"] = msg
        set_state(uid, "passport", 3, data)
        send(chat_id, "3/6) Твой стиль? (скальп/дейтрейд/свинг/позиционный)")
    elif step == 3:
        data["style"] = msg
        set_state(uid, "passport", 4, data)
        send(chat_id, "4/6) Риск на сделку в %? (пример: 0.5%)")
    elif step == 4:
        data["risk"] = msg
        set_state(uid, "passport", 5, data)
        send(chat_id, "5/6) Топ-3 частых ошибки (коротко списком).")
    elif step == 5:
        data["mistakes"] = msg
        set_state(uid, "passport", 6, data)
        send(chat_id, "6/6) Главная цель на месяц (конкретно и измеримо).")
    else:
        # save
        data["goal_month"] = msg
        with SessionLocal() as db:
            prof = db.get(UserProfile, uid) or UserProfile(tg_id=uid)
            prof.full_name  = data.get("full_name")
            prof.market     = data.get("market")
            prof.timeframe  = data.get("timeframe")
            prof.style      = data.get("style")
            prof.risk       = data.get("risk")
            prof.mistakes   = data.get("mistakes")
            prof.goal_month = data.get("goal_month")
            db.merge(prof)
            db.commit()
        set_state(uid, None, 0, {})
        send(chat_id, "✅ Паспорт сохранён.\n"
                      f"Рынок: {data['market']}\nTF: {data['timeframe']}\nСтиль: {data['style']}\n"
                      f"Риск: {data['risk']}\nОшибки: {data['mistakes']}\nЦель: {data['goal_month']}")

def start_weekpanel(uid, chat_id):
    set_state(uid, flow="weekpanel", step=1, data={})
    send(chat_id, "Панель недели.\n1/4) Главный фокус недели? (одна формулировка).")

def handle_weekpanel(uid, chat_id, msg):
    s = get_state(uid)
    step = s["step"]
    data = s["data"]

    if step == 1:
        data["focus"] = msg
        set_state(uid, "weekpanel", 2, data)
        send(chat_id, "2/4) План в 3–5 пунктов (коротким списком).")
    elif step == 2:
        data["plan"] = msg
        set_state(uid, "weekpanel", 3, data)
        send(chat_id, "3/4) Лимиты и правила на неделю (риск/число сделок/стоп-день).")
    elif step == 3:
        data["limits"] = msg
        set_state(uid, "weekpanel", 4, data)
        send(chat_id, "4/4) Короткая ретро прошлой недели (что сработало/нет).")
    else:
        data["retro"] = msg
        with SessionLocal() as db:
            w = WeeklyPanel(
                tg_id=uid,
                week_start=week_monday(date.today()),
                focus=data.get("focus"),
                plan=data.get("plan"),
                limits=data.get("limits"),
                retro=data.get("retro"),
            )
            db.add(w); db.commit()
        set_state(uid, None, 0, {})
        send(chat_id, "✅ Панель недели сохранена.\nФокус: {f}\nПлан: {p}\nЛимиты: {l}\nРетро: {r}"
             .format(f=data["focus"], p=data["plan"], l=data["limits"], r=data["retro"]))

def start_error(uid, chat_id):
    set_state(uid, "error", 1, {})
    send(chat_id, "Ошибка (MERCEDES + TOTE).\n1/3) Коротко опиши, что случилось (контекст сделки).")

def handle_error(uid, chat_id, msg):
    s = get_state(uid)
    step = s["step"]
    data = s["data"]

    if step == 1:
        data["context"] = msg
        set_state(uid, "error", 2, data)
        send(chat_id, "2/3) MERCEDES (кратко): мысли/эмоции/реакция в моменте?")
    elif step == 2:
        data["mercedes"] = msg
        set_state(uid, "error", 3, data)
        send(chat_id, "3/3) TOTE: что изменим в триггерах/проверках/действиях в следующий раз?")
    else:
        data["tote"] = msg
        with SessionLocal() as db:
            er = ErrorReport(
                tg_id=uid,
                context=data.get("context"),
                mercedes=data.get("mercedes"),
                tote=data.get("tote"),
            )
            db.add(er); db.commit()
        set_state(uid, None, 0, {})
        send(chat_id, "✅ Разбор сохранён.\nПодсказка: добавь правило в чек-лист входа/выхода.")

def show_materials(chat_id):
    text = (
        "📚 Материалы Innertrade:\n"
        "• Теория: MERCEDES, TOTE, архетипы, риск-менеджмент\n"
        "• Инструменты: шаблоны MER+TOTE, чек-листы входа/риска, карта трейдера\n"
        "• Сценарии: «поплыл», «зона просадки», «как вернуться в ресурс»\n\n"
        "Попроси: «дай шаблон MER+TOTE» или «чек-лист входа» — пришлю."
    )
    send(chat_id, text)

def show_progress(uid, chat_id):
    with SessionLocal() as db:
        n_errors = db.query(ErrorReport).filter_by(tg_id=uid).count()
        last_week = db.query(WeeklyPanel).filter_by(tg_id=uid)\
                        .order_by(WeeklyPanel.id.desc()).first()
    lines = [f"📊 Разборов ошибок: {n_errors}"]
    if last_week:
        lines.append(f"Последняя панель недели: {last_week.week_start} — фокус: {last_week.focus}")
    send(chat_id, "\n".join(lines))

# ========= COMMANDS =========
@bot.message_handler(commands=["start"])
def cmd_start(m):
    uid = m.from_user.id
    set_state(uid, None, 0, {})
    send(m.chat.id,
         "👋 Привет! Я ИИ-наставник Innertrade.\n"
         "Выбери кнопку или напиши текст.\nКоманды: /ping /reset /menu")

@bot.message_handler(commands=["menu"])
def cmd_menu(m):
    send(m.chat.id, "Меню обновлено.")

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["reset"])
def cmd_reset(m):
    set_state(m.from_user.id, None, 0, {})
    bot.send_message(m.chat.id, "Контекст очищен.", reply_markup=menu_kb())

# ========= MAIN HANDLER =========
@bot.message_handler(func=lambda _: True)
def on_text(m):
    uid = m.from_user.id
    txt_raw = m.text or ""
    txt = norm(txt_raw)

    # если в процессе сценария — ведём дальше
    st = get_state(uid)
    if st["flow"] == "passport":
        return handle_passport(uid, m.chat.id, txt_raw)
    if st["flow"] == "weekpanel":
        return handle_weekpanel(uid, m.chat.id, txt_raw)
    if st["flow"] == "error":
        return handle_error(uid, m.chat.id, txt_raw)

    # интенты
    intent = INTENTS.get(txt)
    if intent == "menu":
        return cmd_menu(m)
    if intent == "reset":
        return cmd_reset(m)
    if intent == "ping":
        return cmd_ping(m)
    if intent == "passport":
        return start_passport(uid, m.chat.id, m.from_user.full_name or "")
    if intent == "weekpanel":
        return start_weekpanel(uid, m.chat.id)
    if intent == "error":
        return start_error(uid, m.chat.id)
    if intent == "materials":
        return show_materials(m.chat.id)
    if intent == "progress":
        return show_progress(uid, m.chat.id)
    if intent == "strategy":
        return send(m.chat.id, "Окей, начнём со стратегии. Напиши: рынок/TF/сигнал(ы), что пробовал и где застреваешь.")
    if intent == "talk":
        return send(m.chat.id, "О чём поговорим в трейдинге? Задай тему или вопрос.")

    # фолбэк в GPT — но уже с системным контекстом
    reply = gpt_reply([{"role":"user","content":txt_raw}])
    bot.send_message(m.chat.id, reply, reply_markup=menu_kb())

# ========= START =========
if __name__ == "__main__":
    logging.info("Starting polling…")
    # На всякий случай: убрать webhook (если был)
    try:
        bot.remove_webhook()
    except Exception:
        pass
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
