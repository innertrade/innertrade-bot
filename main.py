# main.py
import os, logging, json, time, re
from datetime import datetime
from contextlib import contextmanager

from flask import Flask, request, abort, jsonify
from telebot import TeleBot, types
from telebot.types import Update
from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ──────────────────────────────────────────────────────────────────────────────
# ЛОГИ
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("innertrade")

# ──────────────────────────────────────────────────────────────────────────────
# ENV
# ──────────────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL   = os.getenv("DATABASE_URL")
PUBLIC_URL     = os.getenv("PUBLIC_URL")  # например: https://innertrade-bot.onrender.com
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "webhook")
TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET")

if not TELEGRAM_TOKEN:   raise RuntimeError("TELEGRAM_TOKEN missing")
if not OPENAI_API_KEY:   raise RuntimeError("OPENAI_API_KEY missing")
if not PUBLIC_URL:       raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if not TG_WEBHOOK_SECRET:raise RuntimeError("TG_WEBHOOK_SECRET missing")

# ──────────────────────────────────────────────────────────────────────────────
# OPENAI (для «умного» ответа вне сценария)
# ──────────────────────────────────────────────────────────────────────────────
oa = OpenAI(api_key=OPENAI_API_KEY)

def gpt_reply(system_prompt: str, user_prompt: str, max_tokens: int = 400) -> str:
    try:
        rsp = oa.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":system_prompt},
                {"role":"user","content":user_prompt}
            ],
            temperature=0.3,
            max_tokens=max_tokens,
        )
        return rsp.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"OpenAI fallback error: {e}")
        return ""  # молча, чтобы бот продолжал сценарий

# ──────────────────────────────────────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────────────────────────────────────
engine = None
if DATABASE_URL:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    log.info("DB engine ready")
else:
    log.warning("DATABASE_URL not set — running without DB")

@contextmanager
def db_conn(user_id: int | None = None):
    """
    Соединение с БД + попытка проставить RLS-контекст (если настроен).
    """
    if not engine:
        yield None
        return
    conn = engine.connect()
    try:
        if user_id is not None:
            try:
                conn.exec_driver_sql("SET app.user_id = :uid", {"uid": str(user_id)})
            except SQLAlchemyError:
                # RLS может быть ещё не включен — это ок
                pass
        yield conn
    finally:
        conn.close()

def db_exec(conn, sql: str, params: dict | None = None):
    if not conn:
        return None
    return conn.execute(text(sql), params or {})

def save_state(user_id: int, intent: str, step: str | None, data: dict | None = None):
    with db_conn(user_id) as conn:
        if not conn: 
            return
        db_exec(conn, """
            INSERT INTO user_state (user_id, intent, step, data, updated_at)
            VALUES (:uid, :intent, :step, COALESCE(:data, '{}'::jsonb), now())
            ON CONFLICT (user_id) DO UPDATE
              SET intent=:intent, step=:step, data=COALESCE(:data, user_state.data), updated_at=now()
        """, {"uid": user_id, "intent": intent, "step": step, "data": json.dumps(data or {})})

def get_state(user_id: int) -> dict:
    with db_conn(user_id) as conn:
        if not conn: 
            return {"intent":"idle","step":None}
        row = db_exec(conn, "SELECT intent, step, data FROM user_state WHERE user_id=:uid", {"uid": user_id}).fetchone()
        if not row:
            return {"intent":"idle","step":None}
        return {"intent": row[0], "step": row[1], "data": row[2] or {}}

def ensure_error_record(user_id: int) -> int:
    """
    Берём последний errors.id для юзера (или создаём пустую строку).
    Возвращает id.
    """
    with db_conn(user_id) as conn:
        if not conn:
            return -1
        row = db_exec(conn, "SELECT id FROM errors WHERE user_id=:uid ORDER BY id DESC LIMIT 1", {"uid": user_id}).fetchone()
        if row:
            return row[0]
        # создаём пустой каркас
        row = db_exec(conn, """
            INSERT INTO errors(user_id, error_text, created_at)
            VALUES (:uid, '', now())
            RETURNING id
        """, {"uid": user_id}).fetchone()
        return row[0]

def upd_error(user_id: int, fields: dict):
    """
    Обновляет текущую запись errors по последнему id.
    """
    if not fields:
        return
    err_id = ensure_error_record(user_id)
    if err_id < 0:
        return
    sets = []
    params = {"id": err_id}
    for k, v in fields.items():
        sets.append(f"{k} = :{k}")
        params[k] = v
    sql = f"UPDATE errors SET {', '.join(sets)} WHERE id=:id"
    with db_conn(user_id) as conn:
        if conn:
            db_exec(conn, sql, params)

# ──────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────────────────────────────────────
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown", threaded=True)

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

# ──────────────────────────────────────────────────────────────────────────────
# УМНЫЙ ОТВЕТ ВНЕ СЦЕНАРИЯ (GPT)
# ──────────────────────────────────────────────────────────────────────────────
SCENARIO_SYSTEM = (
    "Ты — наставник Innertrade. Отвечай кратко и по делу, дружелюбно. "
    "Если ученик задаёт уточняющий вопрос вне текущего шага сценария, "
    "сначала ответь по сути (1–3 предложения), затем мягко верни к текущему шагу, "
    "коротко повторив вопрос шага в одной строке. Не уходи в длинные лекции."
)

def clarify_then_return(step_name: str, user_text: str, question_for_step: str) -> str:
    prompt = (
        f"Текущий шаг: {step_name}\n"
        f"Вопрос шага: {question_for_step}\n"
        f"Сообщение ученика: {user_text}\n"
        "Сначала дай краткий ответ на его уточнение, затем одной строкой повтори вопрос шага."
    )
    msg = gpt_reply(SCENARIO_SYSTEM, prompt, max_tokens=300)
    if not msg:
        # fallback: только повторить вопрос шага
        return question_for_step
    return msg

def looks_like_clarifying_q(text: str) -> bool:
    t = text.lower().strip()
    if "?" in t: return True
    if any(w in t for w in ["что значит", "не понимаю", "когда", "как именно", "правильно ли", "не пойму"]):
        return True
    return False

# ──────────────────────────────────────────────────────────────────────────────
# СЦЕНАРИЙ: М1/Урок 1 (Ошибка → MERCEDES → TOTE)
# ──────────────────────────────────────────────────────────────────────────────
M1Q = {
    "ask_error": "Опиши основную ошибку 1–2 предложениями на уровне *поведения/навыка*.\n"
                 "Примеры: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю по первой коррекции».",
    "mer_context":  "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует ошибке? (1–2 предложения)",
    "mer_emotions": "ЭМОЦИИ. Что чувствуешь в момент ошибки? Как ощущается в теле? (несколько слов)",
    "mer_thoughts": "МЫСЛИ. Что говоришь себе в этот момент? (цитатами, 1–2 короткие фразы)",
    "mer_behavior": "ПОВЕДЕНИЕ. Что конкретно делаешь? Опиши действия глаголами (1–2 предложения).",
    "mer_beliefs":  "УБЕЖДЕНИЯ/ЦЕННОСТИ. Почему «надо так»? Какие установки стоят за этим? (1–2 тезиса)",
    "mer_state":    "СОСТОЯНИЕ. В каком состоянии был перед/во время сделки (тревога/азарт/контроль и т.п.)?",
    "new_goal":     "Теперь сформулируй *позитивную цель/новое поведение* (наблюдаемо): что будешь делать вместо ошибки?",
    "tote_goal":    "TOTE — ЦЕЛЬ (Test 1). Сформулируй цель в формате ближайших 3 сделок.\n"
                    "Напр.: «в 3 ближайших сделках не двигаю стоп и тейк после входа».",
    "tote_ops":     "TOTE — ОПЕРАЦИИ (Operate). Перечисли 2–4 шага, которые помогут удержать цель.\n"
                    "Напр.: чек-лист входа; пауза/дыхание после входа; таймер 5 минут; записка на мониторе.",
    "tote_check":   "TOTE — ПРОВЕРКА (Test 2). Как поймёшь, что цель удержана? Критерий «да/нет».",
    "tote_exit":    "TOTE — ВЫХОД (Exit). Если *да* — чем закрепишь успех; если *нет* — что исправишь в следующий раз?"
}

NEXT = {
    "ask_error": "mer_context",
    "mer_context": "mer_emotions",
    "mer_emotions": "mer_thoughts",
    "mer_thoughts": "mer_behavior",
    "mer_behavior": "mer_beliefs",
    "mer_beliefs":  "mer_state",
    "mer_state":    "new_goal",
    "new_goal":     "tote_goal",
    "tote_goal":    "tote_ops",
    "tote_ops":     "tote_check",
    "tote_check":   "tote_exit",
    "tote_exit":    None
}

def set_intent_and_step(uid: int, intent: str, step: str):
    save_state(uid, intent=intent, step=step, data=None)

def send_step(uid: int, chat_id: int, step: str):
    save_state(uid, intent="lesson1", step=step, data=None)
    bot.send_message(chat_id, M1Q[step], reply_markup=main_menu())

def accept_or_clarify(step: str, user_text: str, chat_id: int, uid: int) -> bool:
    """
    Возвращает True, если приняли ответ и идём дальше.
    Если это уточняющий вопрос — даём «умный» ответ и остаёмся на шаге.
    """
    if looks_like_clarifying_q(user_text):
        bot.send_message(chat_id, clarify_then_return(step, user_text, M1Q[step]), reply_markup=main_menu())
        return False
    # простая валидация (не пусто, не одно слово)
    if len(user_text.strip()) < 3:
        bot.send_message(chat_id, "Дай, пожалуйста, хотя бы одно короткое предложение по сути.", reply_markup=main_menu())
        return False
    return True

def handle_step(uid: int, chat_id: int, step: str, text_in: str):
    # сохраняем в errors соответствующие поля
    mapping = {
        "ask_error":   {"error_text": text_in},
        "mer_context": {"pattern_behavior": None, "pattern_emotion": None, "pattern_thought": None},  # заполним ниже частично
        "mer_emotions":{"pattern_emotion": text_in},
        "mer_thoughts":{"pattern_thought": text_in},
        "mer_behavior":{"pattern_behavior": text_in},
        "mer_beliefs": {},  # можно сохранять в свободное поле, если есть: positive_goal позже
        "mer_state":   {},  # состояние — в summary не храним отдельным полем; можно дописать в pattern_* при желании
        "new_goal":    {"positive_goal": text_in},
        "tote_goal":   {"tote_goal": text_in},
        "tote_ops":    {"tote_ops": text_in},
        "tote_check":  {"tote_check": text_in},
        "tote_exit":   {"tote_exit": text_in}
    }

    # Для контекста/убеждений/состояния — не теряем текст: добавим к соответствующим полям, если уместно
    if step == "mer_context":
        # контекст влияет в первую очередь на поведение — сохраним как префикс к behavior если уже есть
        pass
    elif step == "mer_beliefs":
        # сохраним убеждения в positive_goal? нет; создадим лёгкий конкат в pattern_thought (как «установка»)
        pass
    elif step == "mer_state":
        # для простоты — допишем к pattern_emotion в скобках, если оно уже есть
        pass

    # принятие/уточнение
    if not accept_or_clarify(step, text_in, chat_id, uid):
        return

    # тонкая склейка некоторых полей:
    if step == "mer_beliefs":
        # подтянем и допишем к pattern_thought (как установки)
        with db_conn(uid) as conn:
            if conn:
                row = db_exec(conn, "SELECT id, pattern_thought FROM errors WHERE user_id=:uid ORDER BY id DESC LIMIT 1", {"uid": uid}).fetchone()
                if row:
                    base = row[1] or ""
                    new_val = (base + ("\nУстановки: " if base else "Установки: ") + text_in).strip()
                    upd_error(uid, {"pattern_thought": new_val})
    elif step == "mer_state":
        with db_conn(uid) as conn:
            if conn:
                row = db_exec(conn, "SELECT id, pattern_emotion FROM errors WHERE user_id=:uid ORDER BY id DESC LIMIT 1", {"uid": uid}).fetchone()
                if row:
                    base = row[1] or ""
                    new_val = (base + ("; " if base else "") + f"Состояние: {text_in}").strip()
                    upd_error(uid, {"pattern_emotion": new_val})

    # стандартное сохранение по mapping
    fields = mapping.get(step)
    if fields is not None:
        # удалим пустые None
        clean_fields = {k:v for k,v in fields.items() if v is not None}
        if clean_fields:
            upd_error(uid, clean_fields)

    nxt = NEXT[step]
    if nxt:
        send_step(uid, chat_id, nxt)
        return

    # финал урока — выдаём чек-листы и короткую сводку
    checklist_pre = "Чек-лист *перед входом*: 1) сетап 100% есть; 2) ресурс ок; 3) план сопровождения; 4) объём и риск подтверждены."
    checklist_post = "Чек-лист *после входа*: 1) не трогаю стоп/тейк; 2) сверка по плану; 3) фиксирую исход по сценарию; 4) короткая заметка."
    upd_error(uid, {"checklist_pre": checklist_pre, "checklist_post": checklist_post})

    bot.send_message(chat_id,
        "Готово! Мы зафиксировали ошибку, паттерн, цель и TOTE.\n"
        "Я добавил два чек-листа — их можно копипастить в заметки.\n"
        "Продолжим Модуль 1 или перейти к «🧩 Хочу стратегию»?", reply_markup=main_menu())
    save_state(uid, intent="idle", step=None)

# ──────────────────────────────────────────────────────────────────────────────
# ХЕНДЛЕРЫ
# ──────────────────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m):
    uid = m.from_user.id
    save_state(uid, "idle", None)
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я наставник *Innertrade*.\nВыбери пункт или напиши текст.\nКоманды: /status /ping",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    st = get_state(m.from_user.id)
    bot.send_message(m.chat.id, f"intent: `{st.get('intent')}`\nstep: `{st.get('step')}`", reply_markup=main_menu())

# Кнопки главного меню
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def btn_error(m):
    uid = m.from_user.id
    set_intent_and_step(uid, "lesson1", "ask_error")
    bot.send_message(m.chat.id, M1Q["ask_error"], reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def btn_strategy(m):
    uid = m.from_user.id
    save_state(uid, "strategy", None)
    bot.send_message(
        m.chat.id,
        "Ок, соберём ТС по конструктору (М2):\n"
        "1) подход/ТФ/вход → 2) стоп/сопровождение/выход/риск → выпуск v0.1.\n"
        "Готов перейти после завершения М1/Урок 1.",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def btn_passport(m):
    uid = m.from_user.id
    save_state(uid, "passport", None)
    bot.send_message(
        m.chat.id,
        "Паспорт трейдера — обновим позже (после М1/Урок 1): рынки, ТФ, стиль, риск, архетип/роли, топ-ошибки.",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def btn_week(m):
    uid = m.from_user.id
    save_state(uid, "week_panel", None)
    bot.send_message(
        m.chat.id,
        "Панель недели: фокус-узел, 1–2 цели, лимиты, короткие чек-ины, ретроспектива. Подключим после М1.",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def btn_panic(m):
    uid = m.from_user.id
    save_state(uid, "panic", None)
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) пауза 2 мин\n2) закрой терминал/вкладку\n3) 10 медленных вдохов\n"
        "4) запиши триггер\n5) вернись к плану или закрой по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def btn_starthelp(m):
    uid = m.from_user.id
    save_state(uid, "start_help", None)
    bot.send_message(
        m.chat.id,
        "Предлагаю так: 1) разберём одну ошибку (М1/Урок 1), 2) выберем фокус недели, 3) соберём каркас ТС (М2).\n"
        "Готов начать с ошибки?", reply_markup=main_menu()
    )

# Текст по сценарию (и офф-скрипт)
@bot.message_handler(content_types=["text"])
def on_text(m):
    uid = m.from_user.id
    st = get_state(uid)
    intent = st.get("intent") or "idle"
    step = st.get("step")

    # если мы в сценарии М1 — обрабатываем шаг
    if intent == "lesson1" and step in M1Q:
        handle_step(uid, m.chat.id, step, m.text.strip())
        return

    # вне сценария — лёгкий GPT-ответ (коротко) + напоминание про меню
    # (чтобы бот выглядел естественнее)
    reply = gpt_reply(
        "Ты — наставник Innertrade. Отвечай кратко (до 2–4 предложений), дружелюбно.",
        m.text.strip(), max_tokens=180
    )
    if not reply:
        reply = "Принял. Выбери пункт в меню ниже или напиши /start."
    bot.send_message(m.chat.id, reply, reply_markup=main_menu())

# ──────────────────────────────────────────────────────────────────────────────
# FLASK (вебхук + health)
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
MAX_BODY = 1_000_000  # 1 MB

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return jsonify({"status":"ok","time":datetime.utcnow().isoformat()+"Z"})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    # Безопасность: секретный хедер и лимит тела
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > MAX_BODY:
        abort(413)

    try:
        json_str = request.get_data(as_text=True)
        update = Update.de_json(json.loads(json_str))
        bot.process_new_updates([update])
    except Exception as e:
        log.error(f"webhook error: {e}")
        abort(500)
    return "OK"

def install_webhook():
    # Устанавливаем вебхук с секретом. Отключаем polling-конкурентов и старые апдейты.
    url = f"{PUBLIC_URL}/{WEBHOOK_PATH}"
    ok = bot.set_webhook(
        url=url,
        secret_token=TG_WEBHOOK_SECRET,
        drop_pending_updates=True,
        max_connections=40,
        allowed_updates=["message","callback_query"]
    )
    if ok:
        log.info(f"Webhook set: {url}")
    else:
        log.error("Failed to set webhook")

# ──────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    install_webhook()
    port = int(os.getenv("PORT", "10000"))
    log.info(f"Starting Flask on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
