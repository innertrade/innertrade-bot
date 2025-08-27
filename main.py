import os
import re
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any

from flask import Flask, request, abort, jsonify
from telebot import TeleBot, types
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError, OperationalError

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("innertrade")

# ---------------- ENV ----------------
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
DATABASE_URL      = os.getenv("DATABASE_URL")
PUBLIC_URL        = os.getenv("PUBLIC_URL")   # e.g. https://innertrade-bot.onrender.com
WEBHOOK_PATH      = os.getenv("WEBHOOK_PATH") # e.g. wbhk_9t3x
TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET") # any random string, must match BotFather setWebhook secret_token

if not TELEGRAM_TOKEN:    raise RuntimeError("TELEGRAM_TOKEN missing")
if not OPENAI_API_KEY:    raise RuntimeError("OPENAI_API_KEY missing")
if not PUBLIC_URL:        raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if not WEBHOOK_PATH:      raise RuntimeError("WEBHOOK_PATH missing (random slug)")
if not TG_WEBHOOK_SECRET: raise RuntimeError("TG_WEBHOOK_SECRET missing")

# ---------------- OPENAI ----------------
# SDK v1.x
from openai import OpenAI
oai = OpenAI(api_key=OPENAI_API_KEY)

def gpt_coach_reply(prompt: str, locale: str = "ru") -> str:
    """
    Короткий, тёплый, коуч-ответ. Без «лекций», 1–3 предложения.
    Используем для мягких отклонений от сценария.
    """
    try:
        sys = (
            "Ты коуч-наставник по трейдингу. Отвечай кратко, по-человечески, доброжелательно. "
            "Цель — помочь уточнить мысль и мягко вернуть к вопросу. Максимум 3 коротких предложения."
        )
        msg = [
            {"role": "system", "content": sys},
            {"role": "user", "content": prompt}
        ]
        res = oai.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.4,
            max_tokens=220,
            messages=msg,
        )
        return (res.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning(f"OpenAI fallback: {e}")
        return "Понимаю. Расскажите чуть конкретнее, и я помогу. Если удобно — можем вернуться к шагам из меню."

# ---------------- DB ----------------
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            # создаём минимально необходимое (если отстало)
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
              user_id    BIGINT PRIMARY KEY,
              mode       TEXT NOT NULL DEFAULT 'course',
              created_at TIMESTAMPTZ DEFAULT now(),
              updated_at TIMESTAMPTZ DEFAULT now()
            );
            """))
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_state (
              user_id    BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
              intent     TEXT,
              step       TEXT,
              data       JSONB,
              updated_at TIMESTAMPTZ DEFAULT now()
            );
            """))
        log.info("DB connected & ensured minimal schema")
    except OperationalError as e:
        log.warning(f"DB not ready: {e}")
        engine = None
else:
    log.info("DATABASE_URL not set — running without DB persistence")

def db_exec(sql: str, params: Optional[Dict[str, Any]] = None):
    if not engine: return None
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})

def ensure_user(uid: int):
    if not engine: return
    db_exec("INSERT INTO users(user_id) VALUES (:uid) ON CONFLICT DO NOTHING", {"uid": uid})

def get_state(uid: int) -> Dict[str, Any]:
    if not engine: return {"intent":"idle","step":None,"data":{}}
    ensure_user(uid)
    row = db_exec("SELECT intent, step, COALESCE(data,'{}'::jsonb) as data FROM user_state WHERE user_id=:uid", {"uid":uid}).mappings().first()
    if not row:
        db_exec("INSERT INTO user_state(user_id,intent,step,data) VALUES(:uid,'idle',NULL,'{}'::jsonb) ON CONFLICT DO NOTHING", {"uid":uid})
        return {"intent":"idle","step":None,"data":{}}
    return {"intent":row["intent"], "step":row["step"], "data":row["data"]}

def set_state(uid: int, intent: Optional[str]=None, step: Optional[str]=None, data: Optional[Dict[str,Any]]=None):
    if not engine: return
    ensure_user(uid)
    cur = get_state(uid)
    if intent is None: intent = cur["intent"]
    if step   is None: step   = cur["step"]
    merged = cur["data"]
    if data: merged = {**merged, **data}
    db_exec("""
    INSERT INTO user_state(user_id,intent,step,data,updated_at)
    VALUES(:uid,:intent,:step,CAST(:data AS jsonb), now())
    ON CONFLICT (user_id) DO UPDATE
    SET intent=:intent, step=:step, data=CAST(:data AS jsonb), updated_at=now()
    """, {"uid": uid, "intent": intent, "step": step, "data": json.dumps(merged)})

# ---------------- TELEGRAM ----------------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

def send(chat_id: int, text: str, kb=None):
    try:
        bot.send_message(chat_id, text, reply_markup=kb or main_menu())
    except Exception as e:
        log.error(f"send err: {e}")

# ---------------- HELPERS: small NLP ----------------
_greet_re = re.compile(r"^(привет|здрав|доброе|доброго|hi|hello)\b", re.I)
_passport_q_re = re.compile(r"(что такое|что за|зачем.*)паспорт", re.I)
_vague_re = re.compile(r"\b(иногда|часто|бывает|по[- ]разному|как получится|определенн(ые|ых)|не знаю|сложно сказать)\b", re.I)

def is_greeting(txt: str) -> bool:
    return bool(_greet_re.search(txt.strip()))

def is_passport_question(txt: str) -> bool:
    return bool(_passport_q_re.search(txt))

def is_vague(txt: str) -> bool:
    # коротко + маркеры «воды»
    return len(txt.strip()) < 8 or bool(_vague_re.search(txt))

# ---------------- SCENES: ERROR (MERCEDES) ----------------
MER_STEPS = [
    ("ask_error",     "Опиши основную ошибку **1–2 предложениями** на уровне *поведения/навыка*.\nПримеры: «вхожу до формирования сигнала», «двигаю стоп после входа»."),
    ("ask_ctx",       "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует ошибке? *(1–2 предложения)*"),
    ("ask_emotions",  "ЭМОЦИИ. Что чувствуешь в момент ошибки? Как ощущается в теле? *(несколько слов)*"),
    ("ask_thoughts",  "МЫСЛИ. Что говоришь себе в этот момент? *(1–2 короткие цитаты)*"),
    ("ask_behavior",  "ПОВЕДЕНИЕ. Что ты конкретно делаешь? Опиши действия глаголами. *(1–2 предложения)*"),
    ("ask_beliefs",   "УБЕЖДЕНИЯ/ЦЕННОСТИ. Почему кажется, что так «надо»? *(1 мысль/установка)*"),
    ("sum_up",        "Супер. Я соберу паттерн и предложу цель по TOTE."),
    ("tote_goal",     "TOTE — ЦЕЛЬ (Test). Сформулируй **желательное поведение** в будущем.\nПример: «Входить только после 100% условий и не двигать стоп/тейк до развязки»."),
    ("tote_ops",      "TOTE — ОПЕРАЦИИ (Operate). 2–3 шага, которые помогут удержать цель.\nПример: чек-лист перед входом; таймер/дыхание после входа."),
    ("tote_check",    "TOTE — ПРОВЕРКА (Test). Как поймёшь, что цель удержана? 1–2 критерия."),
    ("tote_exit",     "TOTE — ВЫХОД (Exit). Если получилось — что закрепим? Если нет — что изменим на следующий цикл?")
]

def to_next_step(current: str) -> Optional[str]:
    ids = [s[0] for s in MER_STEPS]
    try:
        idx = ids.index(current)
        return ids[idx+1] if idx+1 < len(ids) else None
    except ValueError:
        return "ask_error"

def prompt_for(step: str) -> str:
    mp = {k:v for k,v in MER_STEPS}
    return mp.get(step, MER_STEPS[0][1])

def ensure_concrete_or_ask(chat_id: int, txt: str, retry_prompt: str, examples: str) -> bool:
    """
    Проверка на «воду». Если расплывчато — мягко просим конкретику.
    return True — всё ок, можно двигаться дальше; False — остаёмся на шаге.
    """
    if is_vague(txt):
        send(chat_id,
             f"Чуть конкретнее, пожалуйста. Сейчас звучит общо.\n"
             f"🔎 Пример конкретики: {examples}\n\n{retry_prompt}")
        return False
    return True

# ---------------- COMMANDS ----------------
@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, intent="idle", step=None, data={})
    send(m.chat.id, "👋 Привет! Я наставник *Innertrade*.\nВыбери пункт или напиши текст.\nКоманды: /status /ping", main_menu())

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    send(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    # простая диагностика
    db_ok = False
    try:
        if engine:
            db_exec("SELECT 1")
            db_ok = True
    except SQLAlchemyError:
        db_ok = False
    st = get_state(m.from_user.id)
    send(m.chat.id,
         f"✅ Бот живой\n"
         f"DB: {'ok' if db_ok else '—'}\n"
         f"Intent: {st.get('intent') or 'idle'} / Step: {st.get('step') or '-'}\n"
         f"Time: {datetime.utcnow().isoformat(timespec='seconds')}Z")

# ---------------- INTENT BUTTONS ----------------
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def btn_error(m):
    uid = m.from_user.id
    set_state(uid, intent="error", step="ask_error", data={"mer":{}})
    send(m.chat.id, prompt_for("ask_error"))

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def btn_strategy(m):
    uid = m.from_user.id
    set_state(uid, intent="strategy", step=None, data={})
    send(m.chat.id,
         "Соберём ТС по конструктору:\n"
         "1) Подход/рынки/ТФ\n2) Условия входа\n3) Стоп/сопровождение/выход\n4) Риск/лимиты\n"
         "Готов начать с **подхода/рынков/ТФ**?", main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def btn_passport(m):
    uid = m.from_user.id
    set_state(uid, intent="passport", step=None)
    send(m.chat.id,
         "«Паспорт трейдера» — это карточка настроек и психопрофиля:\n"
         "цели, рынки/ТФ, стиль, риски, топ-ошибки, архетип/роли, триггеры, ритуалы.\n"
         "Готов заполнить базу: рынки/ТФ/стиль?", main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def btn_week(m):
    uid = m.from_user.id
    set_state(uid, intent="week_panel", step=None)
    send(m.chat.id,
         "Панель недели:\n• Фокус недели (1 узел)\n• 1–2 цели\n• Лимиты\n• Утро/вечер мини-чек-ин\n• Ретро в конце недели", main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def btn_panic(m):
    uid = m.from_user.id
    set_state(uid, intent="panic", step=None)
    send(m.chat.id,
         "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал\n3) 10 медленных вдохов\n"
         "4) Запиши триггер\n5) Дальше по плану: сократить/закрыть/оставить по правилу", main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def btn_start_help(m):
    uid = m.from_user.id
    set_state(uid, intent="start_help", step=None)
    send(m.chat.id,
         "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\n"
         "С чего начнём — **паспорт** или **фокус недели**?", main_menu())

# ---------------- ERROR FLOW HANDLER ----------------
@bot.message_handler(func=lambda msg: get_state(msg.from_user.id).get("intent") == "error")
def handle_error_flow(m):
    uid = m.from_user.id
    st = get_state(uid)
    step = st.get("step") or "ask_error"
    data = st.get("data") or {}
    mer = data.get("mer", {})

    txt = (m.text or "").strip()

    # Доп. мягкая ветка: если человек явно просит уточнить
    if re.search(r"\b(уточн(ю|ить)|переформулировать|можно доп(в|.)нить)\b", txt, re.I):
        send(m.chat.id, "Да, конечно. Сформулируй, пожалуйста, конкретнее — *на уровне поведения/навыка*. Что именно делаешь?")
        return

    if step == "ask_error":
        # Требуем поведенческую формулировку
        if is_vague(txt) or len(txt) < 8:
            send(m.chat.id,
                 "Сформулируй конкретнее *на уровне поведения/навыка*.\n"
                 "Примеры: «вхожу до формирования сигнала», «двигаю стоп после входа».")
            return
        mer["error"] = txt
        set_state(uid, intent="error", step="ask_ctx", data={"mer":mer})
        send(m.chat.id, prompt_for("ask_ctx"))
        return

    if step == "ask_ctx":
        ok = ensure_concrete_or_ask(
            m.chat.id, txt, "Опиши контекст чуть точнее — когда именно это возникает?",
            "«после серии лосей», «когда весь день без сетапа», «на новостях по ставке», «в понедельник на открытии»"
        )
        if not ok: return
        mer["context"] = txt
        set_state(uid, intent="error", step="ask_emotions", data={"mer":mer})
        send(m.chat.id, prompt_for("ask_emotions"))
        return

    if step == "ask_emotions":
        if is_vague(txt):
            send(m.chat.id, "Пара слов достаточно (например: тревога, спешка, давление в груди).")
            return
        mer["emotions"] = txt
        set_state(uid, intent="error", step="ask_thoughts", data={"mer":mer})
        send(m.chat.id, prompt_for("ask_thoughts"))
        return

    if step == "ask_thoughts":
        if len(txt) < 4:
            send(m.chat.id, "Дай 1–2 короткие цитаты мыслей в момент ошибки.")
            return
        mer["thoughts"] = txt
        set_state(uid, intent="error", step="ask_behavior", data={"mer":mer})
        send(m.chat.id, prompt_for("ask_behavior"))
        return

    if step == "ask_behavior":
        if is_vague(txt):
            send(m.chat.id, "Опиши действия глаголами: что делаешь до/во время/после входа?")
            return
        mer["behavior"] = txt
        set_state(uid, intent="error", step="ask_beliefs", data={"mer":mer})
        send(m.chat.id, prompt_for("ask_beliefs"))
        return

    if step == "ask_beliefs":
        if is_vague(txt):
            send(m.chat.id, "Одно простое убеждение/установка. Пример: «нельзя упускать шанс».")
            return
        mer["beliefs"] = txt
        set_state(uid, intent="error", step="sum_up", data={"mer":mer})
        # Короткое резюме
        summary = (
            f"Понял.\n\n*Ошибка*: {mer.get('error','—')}\n"
            f"*Контекст*: {mer.get('context','—')}\n"
            f"*Эмоции*: {mer.get('emotions','—')}\n"
            f"*Мысли*: {mer.get('thoughts','—')}\n"
            f"*Поведение*: {mer.get('behavior','—')}\n"
            f"*Убеждения*: {mer.get('beliefs','—')}\n"
        )
        send(m.chat.id, summary)
        send(m.chat.id, prompt_for("tote_goal"))
        set_state(uid, intent="error", step="tote_goal", data={"mer":mer})
        return

    if step == "tote_goal":
        if is_vague(txt):
            send(m.chat.id, "Цель должна быть наблюдаемой. Пример: «жду 100% условий входа и не двигаю стоп/тейк до развязки».")
            return
        mer["tote_goal"] = txt
        set_state(uid, intent="error", step="tote_ops", data={"mer":mer})
        send(m.chat.id, prompt_for("tote_ops"))
        return

    if step == "tote_ops":
        if len(txt) < 8:
            send(m.chat.id, "Дай 2–3 шага (чек-лист, дыхание/таймер, напоминание на мониторе и т.п.).")
            return
        mer["tote_ops"] = txt
        set_state(uid, intent="error", step="tote_check", data={"mer":mer})
        send(m.chat.id, prompt_for("tote_check"))
        return

    if step == "tote_check":
        if is_vague(txt):
            send(m.chat.id, "Нужны 1–2 критерия. Пример: «3 сделки подряд без сдвига стопа» или «все пункты чек-листа выполнены».")
            return
        mer["tote_check"] = txt
        set_state(uid, intent="error", step="tote_exit", data={"mer":mer})
        send(m.chat.id, prompt_for("tote_exit"))
        return

    if step == "tote_exit":
        if len(txt) < 4:
            send(m.chat.id, "Коротко: что закрепим при успехе / что изменим при неуспехе?")
            return
        mer["tote_exit"] = txt
        # Здесь можно сохранить в таблицу errors (если нужно). Пока складируем в state.
        set_state(uid, intent="idle", step=None, data={"mer":mer})
        send(m.chat.id,
             "Готово. Сохранил разбор.\nХочешь добавить цель в *Панель недели* или перейти к *ТС*?",
             main_menu())
        return

# ---------------- SMART FALLBACK ----------------
@bot.message_handler(content_types=["text"])
def fallback(m):
    uid = m.from_user.id
    txt = (m.text or "").strip()

    # 1) Приветствия
    if is_greeting(txt):
        send(m.chat.id, "Привет! Готов помочь. Можем коротко поговорить или пойти по шагам. Что сейчас важнее?")
        return

    # 2) Вопрос «что такое паспорт»
    if is_passport_question(txt):
        send(m.chat.id,
             "Паспорт трейдера — это ваша карточка настроек/психопрофиля: цели, рынки/ТФ, стиль, риски, топ-ошибки, архетип/роли, триггеры, ритуалы.\n"
             "Нужен, чтобы все решения были в одном месте и не «плавали». Готовы заполнить базу?")
        return

    # 3) Если сейчас идёт сцена «ошибка», но пришёл уточняющий вопрос — мягкий коуч-ответ через GPT
    st = get_state(uid)
    if (st.get("intent") == "error") and ("?" in txt or len(txt) > 80):
        coach = gpt_coach_reply(f"Пользователь в процессе разбора ошибки. Сообщение: {txt}")
        send(m.chat.id, f"{coach}\n\n(Когда будете готовы — ответьте на мой последний вопрос.)")
        return

    # 4) Иначе — короткий коуч-ответ + меню
    coach = gpt_coach_reply(txt)
    send(m.chat.id, coach, main_menu())

# ---------------- FLASK / WEBHOOK ----------------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK", 200

@app.get("/health")
def health():
    return jsonify({"status":"ok","time": datetime.utcnow().isoformat() + "Z"})

# Telegram webhook endpoint (secret header required)
@app.post(f"/{WEBHOOK_PATH}")
def tg_webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    try:
        update = request.get_json(force=True, silent=False)
        bot.process_new_updates([types.Update.de_json(update)])
    except Exception as e:
        log.exception(f"webhook err: {e}")
    return "OK", 200

# ---------------- MAIN ----------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    log.info(f"Starting Flask on :{port}, webhook path /{WEBHOOK_PATH}")
    app.run(host="0.0.0.0", port=port)
