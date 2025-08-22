# main.py (v5) — Innertrade bot with DB memory (Passport / Errors / WeeklyPanel / Progress)

import os
import logging
from datetime import datetime, date
from typing import Optional, List

from flask import Flask
import telebot
from telebot import types

from openai import OpenAI

from sqlalchemy import (
    create_engine, Integer, String, DateTime, Date, Text, ForeignKey, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

# ==== ENV & Clients ====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в Secrets")
if not OPENAI_KEY:
    raise RuntimeError("Нет OPENAI_API_KEY в Secrets")
if not DATABASE_URL:
    raise RuntimeError("Нет DATABASE_URL в Secrets")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
client = OpenAI(api_key=OPENAI_KEY)

# ==== DB setup ====
class Base(DeclarativeBase): pass

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    passport: Mapped["Passport"] = relationship(back_populates="user", uselist=False)
    errors: Mapped[List["ErrorLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    weeks: Mapped[List["WeeklyPanel"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    progress: Mapped[List["Progress"]] = relationship(back_populates="user", cascade="all, delete-orphan")

class Passport(Base):
    __tablename__ = "passport"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # ключевые поля из курса
    trading_style: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)   # скальп/интрадей/свинг
    timeframe_pref: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)  # M1/M5/M15/H1 и т.п.
    instruments: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)    # тикеры/рынки
    risk_profile: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)   # консерват/умеренный/агрессивный
    archetypes: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)     # текстом
    subparts: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)       # субличности/роли (кратко)
    triggers: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)       # личные триггеры
    rituals: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)        # ключевые ритуалы
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship(back_populates="passport")

class ErrorLog(Base):
    __tablename__ = "error_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # MERCEDES краткая фиксация
    error_text: Mapped[str] = mapped_column(Text)             # формулировка ошибки
    emotions: Mapped[Optional[str]] = mapped_column(Text)     # E
    thoughts: Mapped[Optional[str]] = mapped_column(Text)     # M
    behavior: Mapped[Optional[str]] = mapped_column(Text)     # B
    beliefs: Mapped[Optional[str]] = mapped_column(Text)      # Убеждения/ценности
    context: Mapped[Optional[str]] = mapped_column(Text)      # Контекст
    pattern: Mapped[Optional[str]] = mapped_column(Text)      # повторяющийся паттерн
    goal: Mapped[Optional[str]] = mapped_column(Text)         # позитивная цель
    tote_steps: Mapped[Optional[str]] = mapped_column(Text)   # шаги по TOTE (кратко)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    user: Mapped["User"] = relationship(back_populates="errors")

class WeeklyPanel(Base):
    __tablename__ = "weekly_panel"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    week_start: Mapped[date] = mapped_column(Date)
    focus_node: Mapped[Optional[str]] = mapped_column(String(120))  # узел ТС недели (напр., «Выход», «Риск»)
    plan: Mapped[Optional[str]] = mapped_column(Text)               # краткий план
    limits: Mapped[Optional[str]] = mapped_column(Text)             # дневные/недельные лимиты
    retro: Mapped[Optional[str]] = mapped_column(Text)              # ретроспектива
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship(back_populates="weeks")

class Progress(Base):
    __tablename__ = "progress"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    module: Mapped[Optional[str]] = mapped_column(String(40))  # M1/M2/Generic
    lesson: Mapped[Optional[str]] = mapped_column(String(40))  # L1/L2/L3/L4
    status: Mapped[Optional[str]] = mapped_column(String(40))  # started/done/paused
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    user: Mapped["User"] = relationship(back_populates="progress")

Base.metadata.create_all(bind=engine)

# ==== Helpers ====
def db() -> SessionLocal:
    return SessionLocal()

def get_or_create_user(tg_id: int, username: Optional[str], first_name: Optional[str]) -> User:
    s = db()
    try:
        u = s.query(User).filter(User.tg_id == tg_id).one_or_none()
        if u is None:
            u = User(tg_id=tg_id, username=username, first_name=first_name)
            s.add(u)
            s.commit()
            s.refresh(u)
        return u
    finally:
        s.close()

def get_passport(u: User) -> Optional[Passport]:
    s = db()
    try:
        return s.query(Passport).filter(Passport.user_id == u.id).one_or_none()
    finally:
        s.close()

def upsert_passport(u: User, **kwargs):
    s = db()
    try:
        p = s.query(Passport).filter(Passport.user_id == u.id).one_or_none()
        if p is None:
            p = Passport(user_id=u.id, **kwargs)
            s.add(p)
        else:
            for k, v in kwargs.items():
                setattr(p, k, v)
        s.commit()
    finally:
        s.close()

def add_error(u: User, **kwargs):
    s = db()
    try:
        e = ErrorLog(user_id=u.id, **kwargs)
        s.add(e)
        s.commit()
    finally:
        s.close()

def list_errors(u: User, limit=5) -> List[ErrorLog]:
    s = db()
    try:
        return (
            s.query(ErrorLog)
            .filter(ErrorLog.user_id == u.id)
            .order_by(ErrorLog.created_at.desc())
            .limit(limit)
            .all()
        )
    finally:
        s.close()

def upsert_week(u: User, week_start: date, **kwargs):
    s = db()
    try:
        w = (
            s.query(WeeklyPanel)
            .filter(WeeklyPanel.user_id == u.id, WeeklyPanel.week_start == week_start)
            .one_or_none()
        )
        if w is None:
            w = WeeklyPanel(user_id=u.id, week_start=week_start, **kwargs)
            s.add(w)
        else:
            for k, v in kwargs.items():
                setattr(w, k, v)
        s.commit()
    finally:
        s.close()

def get_latest_week(u: User) -> Optional[WeeklyPanel]:
    s = db()
    try:
        return (
            s.query(WeeklyPanel)
            .filter(WeeklyPanel.user_id == u.id)
            .order_by(WeeklyPanel.week_start.desc())
            .first()
        )
    finally:
        s.close()

def add_progress(u: User, module: str, lesson: str, status: str, note: Optional[str] = None):
    s = db()
    try:
        p = Progress(user_id=u.id, module=module, lesson=lesson, status=status, note=note)
        s.add(p)
        s.commit()
    finally:
        s.close()

# ==== System Prompt (динамический) ====
def build_system_prompt(u: User) -> str:
    p = get_passport(u)
    last_errors = list_errors(u, limit=3)

    # Базовая «прошивка» курса для бота (кратко, хватает для ориентиров и терминов)
    core = (
        "Ты — ИИ-наставник Innertrade. Помогаешь трейдеру через:\n"
        "- Модуль 1 (психология): Mercedes (эмоции/мысли/поведение/убеждения/контекст) и TOTE; архетипы/роли/субличности; убеждения/ценности; интеграционная карта.\n"
        "- Модуль 2 (ТС): стиль, ТФ, вход, сопровождение, выход, риск, аварийный план, торговый план.\n"
        "Правило ответов: кратко, по шагам, с чек-листами. Всегда персонализируй под паспорт и последние ошибки.\n"
    )

    # Вставка персональных данных
    passport_txt = ""
    if p:
        passport_txt = (
            f"[ПАСПОРТ]\n"
            f"Стиль: {p.trading_style or '-'}; ТФ: {p.timeframe_pref or '-'}; Инструменты: {p.instruments or '-'};\n"
            f"Риск-профиль: {p.risk_profile or '-'}; Архетипы: {p.archetypes or '-'};\n"
            f"Субличности/роли: {p.subparts or '-'}; Триггеры: {p.triggers or '-'}; Ритуалы: {p.rituals or '-'}.\n"
        )
    errors_txt = ""
    if last_errors:
        bullets = []
        for e in last_errors:
            bullets.append(f"• {e.error_text} | паттерн: {e.pattern or '-'} | цель: {e.goal or '-'}")
        errors_txt = "[ПОСЛЕДНИЕ ОШИБКИ]\n" + "\n".join(bullets) + "\n"

    return core + passport_txt + errors_txt

def ask_gpt_with_context(u: User, user_text: str) -> str:
    system_prompt = build_system_prompt(u)
    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text}
    ]
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.4,
        messages=msgs
    )
    return (resp.choices[0].message.content or "").strip()

# ==== Flask keepalive ====
app = Flask(__name__)

@app.route("/", methods=["GET"])
def root():
    return "Innertrade bot OK"

@app.route("/health", methods=["GET"])
def health():
    return "pong"

# ==== Telegram Handlers ====
def main_menu() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("🧭 У меня ошибка"), types.KeyboardButton("🧩 Хочу стратегию"))
    kb.row(types.KeyboardButton("🗓 Панель недели"), types.KeyboardButton("🪪 Паспорт"))
    kb.row(types.KeyboardButton("📈 Мой прогресс"), types.KeyboardButton("🧰 Материалы"))
    kb.row(types.KeyboardButton("🔁 Сброс"))
    return kb

def send(msg, text):
    # Без reply_to (чтобы не цеплялся к сообщению пользователя)
    bot.send_message(msg.chat.id, text)

@bot.message_handler(commands=['start'])
def cmd_start(m):
    bot.remove_webhook()
    u = get_or_create_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник <b>Innertrade</b>.\nВыбирай кнопку или пиши текст.\nКоманды: /ping /reset",
        reply_markup=main_menu()
    )
    add_progress(u, module="Generic", lesson="start", status="done")

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    send(m, "pong")

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    # Контекст теперь в БД, reset = отметка прогресса
    u = get_or_create_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    add_progress(u, module="Generic", lesson="reset", status="done", note="User requested reset")
    bot.send_message(m.chat.id, "Ок, обновили фокус. Выбирай раздел.", reply_markup=main_menu())

# ===== Кнопки-интенты =====
@bot.message_handler(func=lambda x: x.text in {
    "🔁 Сброс","🧭 У меня ошибка","🧩 Хочу стратегию","🗓 Панель недели",
    "🪪 Паспорт","📈 Мой прогресс","🧰 Материалы"
})
def on_menu(m):
    u = get_or_create_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    t = m.text or ""
    if t == "🔁 Сброс":
        bot.send_message(m.chat.id, "Контекст обновлён. Чем займёмся?", reply_markup=main_menu())
        return

    if t == "🧭 У меня ошибка":
        bot.send_message(m.chat.id,
            "Опиши кратко ошибку (1–2 предложения). Я помогу прогнать через Mercedes и зафиксировать.")
        add_progress(u, module="M1", lesson="L1", status="started", note="error intake")
        return

    if t == "🧩 Хочу стратегию":
        bot.send_message(m.chat.id,
            "Отлично! Начнём с основы ТС. Напиши: стиль/ТФ/инструменты (через запятую). Пример:\n"
            "<i>интрадей, M15, фьючерс на индекс</i>")
        add_progress(u, module="M2", lesson="L1", status="started")
        return

    if t == "🗓 Панель недели":
        w = get_latest_week(u)
        if w:
            bot.send_message(m.chat.id,
                f"Текущая панель:\n• Узел: {w.focus_node or '-'}\n• План: {w.plan or '-'}\n"
                f"• Лимиты: {w.limits or '-'}\n• Ретро: {w.retro or '-'}\n\n"
                "Напиши в формате:\nузел: ...\nплан: ...\nлимиты: ...")
        else:
            bot.send_message(m.chat.id,
                "Панели пока нет. Напиши в формате:\nузел: ...\nплан: ...\нлимиты: ...")
        return

    if t == "🪪 Паспорт":
        p = get_passport(u)
        if p:
            bot.send_message(m.chat.id,
                f"<b>Паспорт трейдера</b>\nСтиль: {p.trading_style or '-'}\nТФ: {p.timeframe_pref or '-'}\n"
                f"Инструменты: {p.instruments or '-'}\nРиск-профиль: {p.risk_profile or '-'}\n"
                f"Архетипы: {p.archetypes or '-'}\nСубличности/роли: {p.subparts or '-'}\n"
                f"Триггеры: {p.triggers or '-'}\nРитуалы: {p.rituals or '-'}\n\n"
                "Чтобы обновить, напиши, например:\nстиль: свинг\nтф: H1\nинструменты: SPY, NQ")
        else:
            bot.send_message(m.chat.id,
                "Паспорт пуст. Напиши в формате:\nстиль: ...\nтф: ...\nинструменты: ...\nриск: ...")
        return

    if t == "📈 Мой прогресс":
        bot.send_message(m.chat.id,
            "Прогресс фиксируется автоматически. Напиши, что сделал, и я отмечу. Пример:\n"
            "M1-L2 done — «архетипы/роли разобрал»")
        return

    if t == "🧰 Материалы":
        bot.send_message(m.chat.id,
            "Материалы по курсу:\n• Mercedes/TOTE — краткий конспект\n• Архетипы/роли — памятка\n"
            "• Чек-лист входа, выхода, риска\n• Сценарий «что делать, если поплыл»\n\n"
            "Попроси: «пришли чек-лист входа» или «дай памятку по TOTE».")
        return

# ===== Текст: маршрутизация простым парсером =====
def parse_kv(lines: List[str]) -> dict:
    out = {}
    for ln in lines:
        if ":" in ln:
            k, v = ln.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out

@bot.message_handler(func=lambda _: True)
def on_text(m):
    u = get_or_create_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    text = (m.text or "").strip()

    # 1) Паспорт — ключ: "стиль:", "тф:", "инструменты:", "риск:", "архетипы:", "субличности:", "триггеры:", "ритуалы:"
    if any(key in text.lower() for key in ["стиль:", "тф:", "инструменты:", "риск:", "архетип", "сублич", "триггер", "ритуал"]):
        kv = parse_kv([ln for ln in text.splitlines() if ":" in ln])
        upsert_passport(
            u,
            trading_style=kv.get("стиль"),
            timeframe_pref=kv.get("тф"),
            instruments=kv.get("инструменты"),
            risk_profile=kv.get("риск"),
            archetypes=kv.get("архетипы"),
            subparts=kv.get("субличности"),
            triggers=kv.get("триггеры"),
            rituals=kv.get("ритуалы"),
        )
        bot.send_message(m.chat.id, "Паспорт обновлён ✅", reply_markup=main_menu())
        return

    # 2) Панель недели — ключ: "узел:", "план:", "лимиты:", "ретро:"
    if any(k in text.lower() for k in ["узел:", "план:", "лимиты:", "ретро:"]):
        kv = parse_kv([ln for ln in text.splitlines() if ":" in ln])
        week_start = date.today()  # упрощённо: неделя начинается сегодня
        upsert_week(
            u,
            week_start=week_start,
            focus_node=kv.get("узел"),
            plan=kv.get("план"),
            limits=kv.get("лимиты"),
            retro=kv.get("ретро"),
        )
        bot.send_message(m.chat.id, "Панель недели сохранена ✅", reply_markup=main_menu())
        return

    # 3) Ошибка — эвристика: если пользователь начинал «У меня ошибка», просим Mercedes
    if text.lower().startswith("ошибка:") or "ошибка" in text.lower():
        # примем это как формулировку ошибки, попросим добить Mercedes
        add_error(u, error_text=text, emotions=None, thoughts=None, behavior=None, beliefs=None, context=None, pattern=None, goal=None, tote_steps=None)
        bot.send_message(m.chat.id,
            "Принял формулировку ошибки. Теперь по Mercedes одним сообщением:\n"
            "эмоции: ...\nмысли: ...\nповедение: ...\nубеждения: ...\nконтекст: ...")
        return
    if any(h in text.lower() for h in ["эмоции:", "мысли:", "поведение:", "убеждения:", "контекст:", "паттерн:", "цель:", "tote", "шаги:"]):
        kv = parse_kv([ln for ln in text.splitlines() if ":" in ln])
        # обновим последнюю ошибку
        s = db()
        try:
            e = (
                s.query(ErrorLog)
                .filter(ErrorLog.user_id == u.id)
                .order_by(ErrorLog.created_at.desc())
                .first()
            )
            if e:
                e.emotions = kv.get("эмоции", e.emotions)
                e.thoughts = kv.get("мысли", e.thoughts)
                e.behavior = kv.get("поведение", e.behavior)
                e.beliefs  = kv.get("убеждения", e.beliefs)
                e.context  = kv.get("контекст", e.context)
                e.pattern  = kv.get("паттерн", e.pattern)
                e.goal     = kv.get("цель", e.goal)
                # допускаем "шаги:" или "tote:"
                e.tote_steps = kv.get("шаги", kv.get("tote", e.tote_steps))
                s.commit()
                bot.send_message(m.chat.id, "Ошибка зафиксирована по Mercedes/TOTE ✅", reply_markup=main_menu())
            else:
                bot.send_message(m.chat.id, "Не нашёл последнюю ошибку. Напиши сначала «Ошибка: ...»")
        finally:
            s.close()
        return

    # 4) Прогресс — например "M1-L2 done ..."
    if text.lower().startswith(("m1","m2","generic")):
        parts = text.split()
        mod_lsn = parts[0] if parts else "Generic"
        status  = (parts[1] if len(parts)>1 else "done").lower()
        note    = " ".join(parts[2:]) if len(parts)>2 else None
        module, lesson = "Generic", "-"
        if "-" in mod_lsn:
            module, lesson = mod_lsn.split("-", 1)
        add_progress(u, module=module, lesson=lesson, status=status, note=note)
        bot.send_message(m.chat.id, "Прогресс обновлён ✅", reply_markup=main_menu())
        return

    # 5) Иначе — идём в GPT с персональным контекстом
    try:
        reply = ask_gpt_with_context(u, text)
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    bot.send_message(m.chat.id, reply)

# ==== Boot ====
try:
    bot.remove_webhook()
    logging.info("Webhook removed (ok)")
except Exception as e:
    logging.warning(f"Webhook remove warn: {e}")

if __name__ == "__main__":
    logging.info("Starting keepalive web server…")
    logging.info("Starting polling…")
    # Flask keepalive на 0.0.0.0:10000 (Render сам проксирует)
    app.run(host="0.0.0.0", port=10000, debug=False)
    # Примечание: telebot.infinity_polling обычно блокирующий;
    # В проде лучше разнести воркер/веб на разные процессы.
