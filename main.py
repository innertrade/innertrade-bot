# main.py
import os
import logging
from datetime import datetime, date
from typing import Dict, Any, Optional

from flask import Flask
import telebot
from telebot import types

from openai import OpenAI

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Text, Date, UniqueConstraint
)
from sqlalchemy.orm import sessionmaker, declarative_base

# ========= ENV =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets/Environment")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets/Environment")
if not DATABASE_URL:
    raise RuntimeError("Нет DATABASE_URL в Secrets/Environment")

# ========= LOGGING =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ========= OPENAI =========
client = OpenAI(api_key=OPENAI_KEY)

# ========= DB =========
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class UserState(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, nullable=False, index=True)
    first_name = Column(String(128))
    username = Column(String(128))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

class Passport(Base):
    __tablename__ = "passports"
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, index=True, nullable=False)
    market = Column(String(256))        # 1
    style = Column(String(256))         # 2
    tf = Column(String(256))            # 3
    setup = Column(Text)                # 4
    risk = Column(String(256))          # 5
    rituals = Column(Text)              # 6
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint('tg_id', name='uq_passport_tg'),)

class WeekPanel(Base):
    __tablename__ = "week_panels"
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, index=True, nullable=False)
    week_start = Column(Date, index=True, nullable=False)
    focus_node = Column(Text)     # 1
    plan_actions = Column(Text)   # 2
    limits = Column(Text)         # 3
    rituals = Column(Text)        # 4
    retro = Column(Text)          # 5
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('tg_id', 'week_start', name='uq_panel_user_week'),)

Base.metadata.create_all(bind=engine)

# ========= BOT =========
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# Снимаем webhook (на всякий случай)
try:
    bot.remove_webhook()
    logging.info("Webhook removed (ok)")
except Exception as e:
    logging.warning(f"Webhook remove warn: {e}")

# Память сессии (RAM) — для пошаговых форм
user_flow: Dict[int, Dict[str, Any]] = {}   # uid -> dict(flow=..., step=..., buffer={})

# ========= FLASK KEEPALIVE =========
app = Flask(__name__)

@app.route("/")
def index():
    return "OK: Innertrade bot alive"

@app.route("/health")
def health():
    return "pong"

def start_keepalive_server():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)

# ========= UI / HELPERS =========
def main_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("Ошибка"), types.KeyboardButton("Стратегия"))
    kb.row(types.KeyboardButton("Паспорт"), types.KeyboardButton("Панель недели"))
    kb.row(types.KeyboardButton("Материалы"), types.KeyboardButton("Сброс"))
    return kb

def send_long(chat_id: int, text: str):
    MAX = 3500
    for i in range(0, len(text), MAX):
        bot.send_message(chat_id, text[i:i+MAX])

# ========= GPT =========
SYSTEM_PROMPT = (
    "Ты — ИИ-наставник Innertrade. Твоя роль: помогать трейдеру с психо-основой (MERCEDES, TOTE, архетипы) "
    "и с конструктором торговой системы (правила входа/выхода, риск, план). "
    "Если пользователь запустил сценарий 'Паспорт' или 'Панель недели', не перехватывай диалог — "
    "эти сценарии ведутся ботом пошагово. В остальных случаях отвечай по делу, кратко и структурно: "
    "1) мысль/рамка, 2) что сделать, 3) мини-чеклист."
)

def ask_gpt(messages: list[dict]) -> str:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.4,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages
    )
    return (resp.choices[0].message.content or "").strip()

# ========= FLOWS =========
# ---- Паспорт трейдера ----
PASSPORT_QUESTS = [
    "1/6) На каком рынке/инструментах торгуешь? (акции США, EURUSD, BTC, фьючерсы…)",
    "2/6) Твой стиль: скальпинг, дейтрейдинг, свинг, позиционный?",
    "3/6) Основные таймфреймы (например: M5/M15 для входа, H1/H4 для контекста)?",
    "4/6) Базовые сетапы/паттерны для входа (кратко)?",
    "5/6) Риск-параметры: риск на сделку/день, допустимая просадка?",
    "6/6) Ритуалы до/после сессии (кратко)?"
]

def start_passport(uid: int, chat_id: int):
    user_flow[uid] = {"flow": "passport", "step": 0, "buffer": {}}
    bot.send_message(chat_id, "Запускаем «Паспорт трейдера». Ответы коротко. Можно отменить: «Сброс».")
    bot.send_message(chat_id, PASSPORT_QUESTS[0])

def handle_passport(uid: int, chat_id: int, text: str):
    st = user_flow.get(uid, {})
    step = st.get("step", 0)
    buf = st.get("buffer", {})
    # Сохраняем ответ на тек. вопрос
    if step == 0: buf["market"] = text.strip()
    elif step == 1: buf["style"] = text.strip()
    elif step == 2: buf["tf"] = text.strip()
    elif step == 3: buf["setup"] = text.strip()
    elif step == 4: buf["risk"] = text.strip()
    elif step == 5: buf["rituals"] = text.strip()
    else:
        bot.send_message(chat_id, "Неожиданный шаг паспорта. Попробуй «Паспорт» заново.")
        user_flow.pop(uid, None)
        return

    step += 1
    if step < len(PASSPORT_QUESTS):
        # следующий вопрос
        user_flow[uid]["step"] = step
        user_flow[uid]["buffer"] = buf
        bot.send_message(chat_id, PASSPORT_QUESTS[step])
    else:
        # финал — сохраняем в БД
        sess = SessionLocal()
        try:
            p: Optional[Passport] = sess.query(Passport).filter_by(tg_id=uid).one_or_none()
            if not p:
                p = Passport(tg_id=uid)
                sess.add(p)
            p.market = buf.get("market", "")
            p.style = buf.get("style", "")
            p.tf = buf.get("tf", "")
            p.setup = buf.get("setup", "")
            p.risk = buf.get("risk", "")
            p.rituals = buf.get("rituals", "")
            p.updated_at = datetime.utcnow()
            sess.commit()
            bot.send_message(chat_id, "Паспорт сохранён ✅", reply_markup=main_keyboard())
            # Короткая сводка
            summary = (
                f"<b>Паспорт трейдера</b>\n"
                f"• Рынок/инструменты: {p.market}\n"
                f"• Стиль: {p.style}\n"
                f"• Таймфреймы: {p.tf}\n"
                f"• Сетапы: {p.setup}\n"
                f"• Риск: {p.risk}\n"
                f"• Ритуалы: {p.rituals}"
            )
            send_long(chat_id, summary)
        finally:
            sess.close()
            user_flow.pop(uid, None)

# ---- Панель недели ----
WEEK_QUESTS = [
    "1/5) Фокус-нода недели (один главный узел: «вход по плану», «стоп дисциплина», «не гнаться» и т.п.)?",
    "2/5) План: 2–3 конкретных действия на неделю?",
    "3/5) Лимиты: риск/день, лимит сделок, условия остановки?",
    "4/5) Ритуалы недели (короткие чек-ритуалы до/после сессии)?",
    "5/5) Ретроспектива прошлой недели (1–2 предложения): что сработало/не сработало?"
]

def week_monday(d: date) -> date:
    # ISO: Monday=1..Sunday=7 -> хотим понедельник
    return d if d.isoweekday() == 1 else (d.fromordinal(d.toordinal() - (d.isoweekday() - 1)))

def start_week_panel(uid: int, chat_id: int):
    user_flow[uid] = {"flow": "week", "step": 0, "buffer": {}}
    bot.send_message(chat_id, "Панель недели: отвечай коротко. Можно отменить: «Сброс».")
    bot.send_message(chat_id, WEEK_QUESTS[0])

def handle_week_panel(uid: int, chat_id: int, text: str):
    st = user_flow.get(uid, {})
    step = st.get("step", 0)
    buf = st.get("buffer", {})
    if step == 0: buf["focus"] = text.strip()
    elif step == 1: buf["plan"] = text.strip()
    elif step == 2: buf["limits"] = text.strip()
    elif step == 3: buf["rituals"] = text.strip()
    elif step == 4: buf["retro"] = text.strip()
    else:
        bot.send_message(chat_id, "Неожиданный шаг панели. Запусти «Панель недели» заново.")
        user_flow.pop(uid, None)
        return

    step += 1
    if step < len(WEEK_QUESTS):
        user_flow[uid]["step"] = step
        user_flow[uid]["buffer"] = buf
        bot.send_message(chat_id, WEEK_QUESTS[step])
    else:
        # Сохраняем
        sess = SessionLocal()
        try:
            ws = week_monday(date.today())
            panel: Optional[WeekPanel] = (
                sess.query(WeekPanel).filter_by(tg_id=uid, week_start=ws).one_or_none()
            )
            if not panel:
                panel = WeekPanel(tg_id=uid, week_start=ws)
                sess.add(panel)
            panel.focus_node = buf.get("focus", "")
            panel.plan_actions = buf.get("plan", "")
            panel.limits = buf.get("limits", "")
            panel.rituals = buf.get("rituals", "")
            panel.retro = buf.get("retro", "")
            sess.commit()

            bot.send_message(chat_id, "Панель недели сохранена ✅", reply_markup=main_keyboard())
            summary = (
                f"<b>Неделя {ws.isoformat()}</b>\n"
                f"• Фокус-нода: {panel.focus_node}\n"
                f"• План: {panel.plan_actions}\n"
                f"• Лимиты: {panel.limits}\n"
                f"• Ритуалы: {panel.rituals}\n"
                f"• Ретроспектива: {panel.retro}"
            )
            send_long(chat_id, summary)
        finally:
            sess.close()
            user_flow.pop(uid, None)

# ========= COMMANDS =========
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    # регистрируем/обновляем юзера
    sess = SessionLocal()
    try:
        u: Optional[UserState] = sess.query(UserState).filter_by(tg_id=uid).one_or_none()
        if not u:
            u = UserState(
                tg_id=uid,
                first_name=m.from_user.first_name or "",
                username=m.from_user.username or ""
            )
            sess.add(u)
        u.updated_at = datetime.utcnow()
        sess.commit()
    finally:
        sess.close()

    user_flow.pop(uid, None)
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник Innertrade.\nВыбери кнопку или напиши текст.\nКоманды: /ping /reset",
        reply_markup=main_keyboard()
    )

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")  # без «How can I assist…»

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    uid = m.from_user.id
    user_flow.pop(uid, None)
    bot.send_message(m.chat.id, "Контекст форм очищен. Готов работать.", reply_markup=main_keyboard())

# ========= BUTTONS =========
@bot.message_handler(func=lambda x: x.text in {"Сброс","Паспорт","Панель недели","Ошибка","Стратегия","Материалы"})
def on_buttons(m):
    uid = m.from_user.id
    t = (m.text or "").strip()

    if t == "Сброс":
        user_flow.pop(uid, None)
        bot.send_message(m.chat.id, "Сброшено. Выбирай раздел.", reply_markup=main_keyboard())
        return

    if t == "Паспорт":
        start_passport(uid, m.chat.id)
        return

    if t == "Панель недели":
        start_week_panel(uid, m.chat.id)
        return

    if t == "Ошибка":
        # мини-вступление под MERCEDES+TOTE
        msg = (
            "<b>Мини-разбор ошибки</b>\n"
            "Напиши кратко, что произошло (1–2 предложения).\n"
            "Дальше я спрошу по схеме MERCEDES → TOTE."
        )
        user_flow[uid] = {"flow": "error", "step": 0, "buffer": {}}
        bot.send_message(m.chat.id, msg)
        return

    if t == "Стратегия":
        bot.send_message(
            m.chat.id,
            "Ок. Готов собрать/пересобрать ТС: пришли кратко твой подход (рынок, стиль) и цель. "
            "Дальше пойдем по шагам: вход/стоп/сопровождение/риск/план."
        )
        user_flow[uid] = {"flow": "strategy", "step": 0, "buffer": {}}
        return

    if t == "Материалы":
        bot.send_message(
            m.chat.id,
            "Материалы:\n• MERCEDES, TOTE, архетипы\n• Чек-листы входа/риска\n• Конструктор ТС и план дня\n\n"
            "Скажи, что открыть: «MERCEDES», «TOTE», «чек-лист входа», «риск», «план дня».",
            reply_markup=main_keyboard()
        )
        return

# ========= TEXT ROUTER =========
@bot.message_handler(func=lambda _: True)
def on_text(m):
    uid = m.from_user.id
    text = (m.text or "").strip()

    # активные сценарии
    st = user_flow.get(uid)
    if st:
        flow = st.get("flow")
        if flow == "passport":
            handle_passport(uid, m.chat.id, text)
            return
        if flow == "week":
            handle_week_panel(uid, m.chat.id, text)
            return
        if flow == "error":
            # Простая «ступенька» мерседес+тоте (усечённо). Храним в памяти, но не пишем в БД (MVP).
            step = st.get("step", 0)
            buf = st.get("buffer", {})
            if step == 0:
                buf["story"] = text
                user_flow[uid]["step"] = 1
                user_flow[uid]["buffer"] = buf
                bot.send_message(m.chat.id, "Что ты <b>думал</b> в момент ошибки? (M — мысли)")
                return
            if step == 1:
                buf["M"] = text
                user_flow[uid]["step"] = 2
                bot.send_message(m.chat.id, "Что ты <b>чувствовал</b>? (E — эмоции)")
                return
            if step == 2:
                buf["E"] = text
                user_flow[uid]["step"] = 3
                bot.send_message(m.chat.id, "Как повёл себя? (R — реакция/действие)")
                return
            if step == 3:
                buf["R"] = text
                user_flow[uid]["step"] = 4
                bot.send_message(m.chat.id, "К чему привело? (S — состояние/результат)")
                return
            if step == 4:
                buf["S"] = text
                # Итог и короткий TOTE
                recap = (
                    "<b>Итог по MERCEDES</b>\n"
                    f"История: {buf.get('story','')}\n"
                    f"M: {buf.get('M','')}\nE: {buf.get('E','')}\n"
                    f"R: {buf.get('R','')}\nS: {buf.get('S','')}\n\n"
                    "<b>TOTE →</b> Тест: что было критерием входа/выхода?\n"
                    "Операция: что нужно сделать в следующий раз?\n"
                    "Тест: как поймёшь, что идёшь по плану?\n"
                    "Выход: где остановишься, если снова плывёшь?\n\n"
                    "Можем оформить это в чек-лист. Напиши: «сделай чек-лист TOTE»."
                )
                send_long(m.chat.id, recap)
                user_flow.pop(uid, None)
                return

        if flow == "strategy":
            # Пока даём GPT-помощь под системным промптом
            reply = ask_gpt([{"role": "user", "content": f"Стратегия: {text}"}])
            send_long(m.chat.id, reply)
            return

    # Если не в сценарии — подхватывает GPT (с нашим системным промптом)
    reply = ask_gpt([{"role": "user", "content": text}])
    send_long(m.chat.id, reply)

# ========= MAIN =========
if __name__ == "__main__":
    logging.info("Starting keepalive web server…")
    import threading
    threading.Thread(target=start_keepalive_server, daemon=True).start()

    logging.info("Starting polling…")
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
