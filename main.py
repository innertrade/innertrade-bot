# main.py
import os
import logging
from datetime import datetime, date

from flask import Flask
from telebot import TeleBot, types

from openai import OpenAI

from sqlalchemy import (
    create_engine, Integer, String, Date, DateTime, Text,
    Boolean, ForeignKey, select
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, relationship

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

# ========= OPENAI =========
client = OpenAI(api_key=OPENAI_KEY)

# ========= TELEGRAM =========
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ========= DB =========
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int]             = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int]          = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None]= mapped_column(String(64), nullable=True)
    first_seen: Mapped[datetime]= mapped_column(DateTime, default=datetime.utcnow)

    passport: Mapped["Passport | None"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")
    weekly:   Mapped[list["WeeklyPanel"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    progress: Mapped[list["ProgressLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")

class Passport(Base):
    __tablename__ = "passport"
    id: Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # поля паспорта (минимальный набор; расширим позже)
    market: Mapped[str | None]     = mapped_column(String(120))
    style: Mapped[str | None]      = mapped_column(String(120))
    timeframe: Mapped[str | None]  = mapped_column(String(120))
    risk_rules: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None]      = mapped_column(Text)

    user: Mapped["User"] = relationship(back_populates="passport")

class WeeklyPanel(Base):
    __tablename__ = "weekly_panel"
    id: Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    week_start: Mapped[date] = mapped_column(Date)  # понедельник текущей недели
    focus_node: Mapped[str]  = mapped_column(String(200))     # фокус недели (узел/тема)
    plan: Mapped[str | None] = mapped_column(Text)            # ключевые шаги
    limits: Mapped[str | None]= mapped_column(Text)           # лимиты/ограничения
    retrospective: Mapped[str | None]= mapped_column(Text)    # короткая ретро

    user: Mapped["User"] = relationship(back_populates="weekly")

class ProgressLog(Base):
    __tablename__ = "progress_log"
    id: Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    day: Mapped[date]    = mapped_column(Date, index=True, default=date.today)
    done: Mapped[str | None]   = mapped_column(Text)        # что сделал
    changes: Mapped[str | None]= mapped_column(Text)        # какие изменения внёс
    blockers: Mapped[str | None]= mapped_column(Text)       # что блокирует
    created_at: Mapped[datetime]= mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="progress")

class State(Base):
    """
    Таблица «состояние диалога» — хранит, в каком шаге сейчас пользователь.
    """
    __tablename__ = "state"
    id: Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    flow: Mapped[str]    = mapped_column(String(40))   # 'passport' | 'weekly' | 'progress' | 'none'
    step: Mapped[int]    = mapped_column(Integer, default=0)
    payload: Mapped[str | None] = mapped_column(Text)  # временные ответы между шагами

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Base.metadata.create_all(engine)

# ========= HELPERS =========
def get_or_create_user(sess: Session, tg_id: int, username: str | None) -> User:
    user = sess.scalar(select(User).where(User.tg_id == tg_id))
    if not user:
        user = User(tg_id=tg_id, username=username)
        sess.add(user)
        sess.commit()
    return user

def set_state(sess: Session, user_id: int, flow: str, step: int = 0, payload: str | None = None):
    st = sess.scalar(select(State).where(State.user_id == user_id))
    if not st:
        st = State(user_id=user_id, flow=flow, step=step, payload=payload)
        sess.add(st)
    else:
        st.flow = flow
        st.step = step
        st.payload = payload
    sess.commit()

def clear_state(sess: Session, user_id: int):
    st = sess.scalar(select(State).where(State.user_id == user_id))
    if st:
        sess.delete(st)
        sess.commit()

def get_state(sess: Session, user_id: int) -> State | None:
    return sess.scalar(select(State).where(State.user_id == user_id))

def main_menu() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("🚑 У меня ошибка"), types.KeyboardButton("🧩 Хочу стратегию"))
    kb.row(types.KeyboardButton("🗓 Панель недели"), types.KeyboardButton("🧾 Паспорт"))
    kb.row(types.KeyboardButton("📊 Мой прогресс"), types.KeyboardButton("📚 Материалы"))
    kb.row(types.KeyboardButton("💬 Поговорим"), types.KeyboardButton("🔄 Сброс"))
    return kb

def send(chat_id: int, text: str):
    bot.send_message(chat_id, text, reply_markup=main_menu())

# ========= START / PING / RESET =========
@bot.message_handler(commands=['start'])
def cmd_start(m):
    with Session(engine) as sess:
        user = get_or_create_user(sess, m.from_user.id, m.from_user.username)
        clear_state(sess, user.id)
    send(m.chat.id,
         "👋 Привет! Я ИИ-наставник Innertrade.\nВыбери кнопку или напиши текст.\nКоманды: /ping /reset")

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    with Session(engine) as sess:
        user = get_or_create_user(sess, m.from_user.id, m.from_user.username)
        clear_state(sess, user.id)
    send(m.chat.id, "Контекст очищен.")

# ========= INTENTS (кнопки) =========
@bot.message_handler(func=lambda x: x.text in {
    "🧾 Паспорт", "🗓 Панель недели", "📊 Мой прогресс",
    "🚑 У меня ошибка", "🧩 Хочу стратегию", "📚 Материалы",
    "💬 Поговорим", "🔄 Сброс"
})
def on_menu(m):
    txt = m.text or ""
    uid = m.from_user.id

    if txt == "🔄 Сброс":
        return cmd_reset(m)

    if txt == "🧾 Паспорт":
            # стартуем паспорт: задаём 1-й вопрос
            with Session(engine) as sess:
                user = get_or_create_user(sess, uid, m.from_user.username)
                set_state(sess, user.id, flow="passport", step=1)
            bot.send_message(m.chat.id, "Паспорт трейдера.\n1/6) На каком рынке/инструментах торгуешь? (пример: акции США, EURUSD, BTC, фьючерсы…)",
                             reply_markup=types.ReplyKeyboardRemove())
            return

    if txt == "🗓 Панель недели":
        with Session(engine) as sess:
            user = get_or_create_user(sess, uid, m.from_user.username)
            set_state(sess, user.id, flow="weekly", step=1)
        bot.send_message(m.chat.id,
                         "Панель недели. 1/4) Какой фокус недели? (узел/тема, напр.: «входы по плану», «сопровождение», «MERCEDES при ошибке»)",
                         reply_markup=types.ReplyKeyboardRemove())
        return

    if txt == "📊 Мой прогресс":
        with Session(engine) as sess:
            user = get_or_create_user(sess, uid, m.from_user.username)
            set_state(sess, user.id, flow="progress", step=1)
        bot.send_message(m.chat.id,
                         "Дневной чек-ин. 1/3) Что сегодня сделал по Innertrade?",
                         reply_markup=types.ReplyKeyboardRemove())
        return

    # Остальное пока просто перекидываем в GPT (системные интенты допилим позже)
    if txt == "🚑 У меня ошибка":
        bot.send_message(m.chat.id,
                         "Опиши кратко ошибку: что произошло, на каком инструменте/таймфрейме и чем закончилось. Я помогу развернуть разбор по MERCEDES.")
        return
    if txt == "🧩 Хочу стратегию":
        bot.send_message(m.chat.id,
                         "Ок, начнём набросок ТС. Напиши: твой рынок, рабочие таймфреймы и что считаешь своим «краем» (edge).")
        return
    if txt == "📚 Материалы":
        bot.send_message(m.chat.id,
                         "Материалы скоро подшью в меню (теория MERCEDES/TOTE, чек-листы, шаблоны).")
        return
    if txt == "💬 Поговорим":
        bot.send_message(m.chat.id, "Я здесь. С чем именно помочь?")
        return

# ========= TEXT FLOW HANDLER =========
@bot.message_handler(func=lambda _: True)
def on_text(m):
    text = (m.text or "").strip()
    uid  = m.from_user.id

    with Session(engine) as sess:
        user = get_or_create_user(sess, uid, m.from_user.username)
        st = get_state(sess, user.id)

        if st and st.flow == "passport":
            return handle_passport_step(m, sess, user, st, text)
        if st and st.flow == "weekly":
            return handle_weekly_step(m, sess, user, st, text)
        if st and st.flow == "progress":
            return handle_progress_step(m, sess, user, st, text)

    # Если нет активного сценария — короткий GPT-ответ (общий помощник)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.4,
            messages=[
                {"role":"system","content":"Ты дружелюбный наставник по трейдингу Innertrade. Отвечай кратко и по делу. Если пользователь упоминает ошибки трейдинга, мягко направляй к разбору MERCEDES/TOTE."},
                {"role":"user","content": text}
            ]
        )
        reply = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        reply = f"Ошибка GPT: {e}"
    send(m.chat.id, reply)

# ========= PASSPORT FLOW =========
def handle_passport_step(m, sess: Session, user: User, st: State, text: str):
    # собираем по шагам 6 ответов
    if st.step == 1:
        set_state(sess, user.id, "passport", 2, payload=text)
        return bot.send_message(m.chat.id, "2/6) Твой стиль/подход (скальпинг, дейтрейдинг, свинг…)?")
    if st.step == 2:
        payload = st.payload or ""
        payload += f"\nmarket: {text}"
        set_state(sess, user.id, "passport", 3, payload=payload)
        return bot.send_message(m.chat.id, "3/6) Рабочие таймфреймы?")
    if st.step == 3:
        payload = st.payload or ""
        payload += f"\nstyle: {text}"
        set_state(sess, user.id, "passport", 4, payload=payload)
        return bot.send_message(m.chat.id, "4/6) Базовые правила риска (стоп, % на сделку…)?")
    if st.step == 4:
        payload = st.payload or ""
        payload += f"\ntimeframe: {text}"
        set_state(sess, user.id, "passport", 5, payload=payload)
        return bot.send_message(m.chat.id, "5/6) Особые примечания (паттерны, запреты, сигналы тревоги)?")
    if st.step == 5:
        payload = (st.payload or "") + f"\nrisk_rules: {text}"
        set_state(sess, user.id, "passport", 6, payload=payload)
        return bot.send_message(m.chat.id, "6/6) Любые дополнительные заметки?")
    if st.step == 6:
        payload = (st.payload or "") + f"\nnotes: {text}"

        # разбор payload в dict
        data = {"market":None,"style":None,"timeframe":None,"risk_rules":None,"notes":None}
        for line in payload.splitlines():
            if ":" in line:
                k,v = line.split(":",1)
                k=k.strip(); v=v.strip()
                if k in data: data[k]=v

        # сохранить/обновить
        pas = sess.scalar(select(Passport).where(Passport.user_id == user.id))
        if not pas:
            pas = Passport(user_id=user.id, **data)
            sess.add(pas)
        else:
            pas.market = data["market"]
            pas.style  = data["style"]
            pas.timeframe = data["timeframe"]
            pas.risk_rules= data["risk_rules"]
            pas.notes = data["notes"]
        sess.commit()
        clear_state(sess, user.id)
        return send(m.chat.id, "✅ Паспорт обновлён.\nВернулся в меню.")

# ========= WEEKLY PANEL FLOW =========
def monday_of(dt: date) -> date:
    return dt if dt.weekday()==0 else (dt.fromisocalendar(dt.isocalendar().year, dt.isocalendar().week, 1))

def handle_weekly_step(m, sess: Session, user: User, st: State, text: str):
    if st.step == 1:
        set_state(sess, user.id, "weekly", 2, payload=f"focus:{text}")
        return bot.send_message(m.chat.id, "2/4) План на неделю (3–5 коротких шагов):")
    if st.step == 2:
        payload = (st.payload or "") + f"\nplan:{text}"
        set_state(sess, user.id, "weekly", 3, payload=payload)
        return bot.send_message(m.chat.id, "3/4) Лимиты/ограничения (время, риск, количество сделок):")
    if st.step == 3:
        payload = (st.payload or "") + f"\nlimits:{text}"
        set_state(sess, user.id, "weekly", 4, payload=payload)
        return bot.send_message(m.chat.id, "4/4) Короткая ретроспектива прошлого спринта (что сработало/нет):")
    if st.step == 4:
        payload = (st.payload or "") + f"\nretro:{text}"
        # распарсить
        data = {"focus_node":"","plan":"","limits":"","retrospective":""}
        for line in payload.splitlines():
            if ":" in line:
                k,v = line.split(":",1)
                k=k.strip(); v=v.strip()
                if k=="focus": data["focus_node"]=v
                if k=="plan":  data["plan"]=v
                if k=="limits":data["limits"]=v
                if k=="retro": data["retrospective"]=v

        week = monday_of(date.today())
        # перезаписываем на текущую неделю
        wp = sess.scalar(select(WeeklyPanel).where(WeeklyPanel.user_id==user.id, WeeklyPanel.week_start==week))
        if not wp:
            wp = WeeklyPanel(user_id=user.id, week_start=week, **data)
            sess.add(wp)
        else:
            wp.focus_node = data["focus_node"]
            wp.plan = data["plan"]
            wp.limits = data["limits"]
            wp.retrospective = data["retrospective"]
        sess.commit()
        clear_state(sess, user.id)
        return send(m.chat.id, "✅ Панель недели сохранена. Удачной недели!")

# ========= PROGRESS FLOW =========
def handle_progress_step(m, sess: Session, user: User, st: State, text: str):
    if st.step == 1:
        set_state(sess, user.id, "progress", 2, payload=f"done:{text}")
        return bot.send_message(m.chat.id, "2/3) Какие изменения внёс (в структуру курса/бота/ТС)?")
    if st.step == 2:
        payload = (st.payload or "") + f"\nchanges:{text}"
        set_state(sess, user.id, "progress", 3, payload=payload)
        return bot.send_message(m.chat.id, "3/3) Что блокирует/мешает?")
    if st.step == 3:
        payload = (st.payload or "") + f"\nblockers:{text}"
        data = {"done":"","changes":"","blockers":""}
        for line in payload.splitlines():
            if ":" in line:
                k,v = line.split(":",1)
                k=k.strip(); v=v.strip()
                if k in data: data[k]=v

        # одна запись в день — обновляем если уже есть
        today = date.today()
        pl = sess.scalar(select(ProgressLog).where(ProgressLog.user_id==user.id, ProgressLog.day==today))
        if not pl:
            pl = ProgressLog(user_id=user.id, day=today, **data)
            sess.add(pl)
        else:
            pl.done = data["done"]; pl.changes = data["changes"]; pl.blockers = data["blockers"]
        sess.commit()
        clear_state(sess, user.id)
        return send(m.chat.id, "✅ Дневной чек-ин сохранён. Спасибо!")

# ========= KEEPALIVE =========
app = Flask(__name__)

@app.get("/")
def home():
    return "Innertrade bot is up"

@app.get("/health")
def health():
    return "pong"

if __name__ == "__main__":
    # важно: не использовать reply_to_message_id, чтобы не было «ссылки на запрос»
    logging.info("Starting keepalive web server…")
    from threading import Thread
    def run_app():
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
    Thread(target=run_app, daemon=True).start()

    logging.info("Starting polling…")
    bot.remove_webhook()
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)
