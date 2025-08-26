import os, logging, json, time
from datetime import datetime
from typing import Optional

from flask import Flask, request, jsonify, abort
from telebot import TeleBot, types
from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ---------- ЛОГИ ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("innertrade")

# ---------- ENV ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL   = os.getenv("DATABASE_URL")
PUBLIC_URL     = os.getenv("PUBLIC_URL")            # https://innertrade-bot.onrender.com
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH")          # например: wbhk_9t3x
TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET")  # любой длинный секрет
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")        # опционально

for k, v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "PUBLIC_URL": PUBLIC_URL,
    "WEBHOOK_PATH": WEBHOOK_PATH,
    "TG_WEBHOOK_SECRET": TG_WEBHOOK_SECRET,
}.items():
    if not v:
        raise RuntimeError(f"{k} missing")

# ---------- OPENAI (опционально) ----------
client_oa: Optional[OpenAI] = None
if OPENAI_API_KEY:
    try:
        client_oa = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client ready")
    except Exception as e:
        log.warning(f"OpenAI init failed: {e}")

# ---------- DB ----------
engine = None
if DATABASE_URL:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.begin() as conn:
        # Мини-схема (как согласовано)
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS users (
          user_id    BIGINT PRIMARY KEY,
          mode       TEXT NOT NULL DEFAULT 'course',
          created_at TIMESTAMPTZ DEFAULT now(),
          updated_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS user_state (
          user_id    BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
          intent     TEXT,
          step       TEXT,
          data       JSONB,
          updated_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS errors (
          id BIGSERIAL PRIMARY KEY,
          user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
          error_text TEXT NOT NULL,
          pattern_behavior TEXT,
          pattern_emotion  TEXT,
          pattern_thought  TEXT,
          positive_goal    TEXT,
          tote_goal  TEXT, tote_ops TEXT, tote_check TEXT, tote_exit TEXT,
          checklist_pre TEXT, checklist_post TEXT,
          created_at TIMESTAMPTZ DEFAULT now()
        );
        """))
        log.info("DB connected & migrated")
else:
    log.info("DATABASE_URL not set — running stateless")

def ensure_user(uid: int):
    if not engine: return
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO users(user_id) VALUES(:u)
                ON CONFLICT (user_id) DO UPDATE SET updated_at = now()
            """), {"u": uid})
            conn.execute(text("""
                INSERT INTO user_state(user_id, intent, step, data)
                VALUES(:u, 'idle', NULL, '{}'::jsonb)
                ON CONFLICT (user_id) DO NOTHING
            """), {"u": uid})
    except SQLAlchemyError as e:
        log.error(f"ensure_user DB error: {e}")

def save_state(uid: int, intent: Optional[str]=None, step: Optional[str]=None, data: Optional[dict]=None):
    if not engine: return
    try:
        with engine.begin() as conn:
            if intent is not None:
                conn.execute(text("""
                    UPDATE user_state SET intent=:i, updated_at=now() WHERE user_id=:u
                """), {"i": intent, "u": uid})
            if step is not None:
                conn.execute(text("""
                    UPDATE user_state SET step=:s, updated_at=now() WHERE user_id=:u
                """), {"s": step, "u": uid})
            if data is not None:
                conn.execute(text("""
                    UPDATE user_state SET data=:d, updated_at=now() WHERE user_id=:u
                """), {"d": json.dumps(data), "u": uid})
    except SQLAlchemyError as e:
        log.error(f"save_state DB error: {e}")

def get_state(uid: int) -> dict:
    if not engine: return {"intent":"idle","step":None,"data":{}}
    try:
        with engine.begin() as conn:
            row = conn.execute(text("""
                SELECT intent, step, COALESCE(data,'{}'::jsonb) AS data FROM user_state WHERE user_id=:u
            """), {"u": uid}).mappings().first()
            return row or {"intent":"idle","step":None,"data":{}}
    except SQLAlchemyError as e:
        log.error(f"get_state DB error: {e}")
        return {"intent":"idle","step":None,"data":{}}

# ---------- TELEGRAM ----------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

# Небольшой «умный» ответ на уточнение с вопросом
def sidecar_or_hint(user_text: str, last_prompt: str) -> Optional[str]:
    txt = user_text.strip()
    if not txt.endswith("?"):
        return None
    # Если есть OpenAI, коротко отвечаем и возвращаем к вопросу
    if client_oa:
        try:
            msg = [
                {"role":"system","content":"Ответь кратко (1-2 фразы), по делу, дружелюбно."},
                {"role":"user","content": f"Пользователь уточняет: «{txt}». Текущий вопрос бота был: «{last_prompt}». Помоги прояснить и мягко верни к вопросу."}
            ]
            r = client_oa.chat.completions.create(
                model="gpt-4o-mini",
                messages=msg,
                temperature=0.2,
                max_tokens=120,
            )
            return r.choices[0].message.content.strip()
        except Exception as e:
            log.warning(f"OpenAI sidecar failed: {e}")
    # Фолбэк: статичный краткий ответ
    return f"Хороший вопрос! Здесь имею в виду *до входа*, в момент выбора. А теперь ответь, пожалуйста: {last_prompt}"

# ---------- ХЕНДЛЕРЫ ----------
@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m: types.Message):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="idle", step=None, data={})
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
    bot.send_message(m.chat.id, f"status: intent={st.get('intent')} step={st.get('step')}")

# Интенты-кнопки
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def intent_error(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="error", step="ask_error", data={})
    bot.send_message(
        m.chat.id,
        "Опиши основную ошибку 1–2 предложениями *на уровне поведения/навыка*.\nПримеры: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю по первой коррекции».",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def intent_strategy(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="strategy", step="intro")
    bot.send_message(
        m.chat.id,
        "Ок, собираем ТС по конструктору:\n1) Цели\n2) Стиль (дневной/свинг/позиционный)\n3) Рынки/инструменты\n4) Правила входа/выхода\n5) Риск (%, стоп)\n6) Сопровождение\n7) Тестирование (история/демо)",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def intent_passport(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="passport", step="q1")
    bot.send_message(m.chat.id, "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def intent_week(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="week_panel", step="intro")
    bot.send_message(m.chat.id, "Панель недели:\n• Фокус недели\n• План (3 шага)\n• Лимиты\n• Ритуалы\n• Ретроспектива", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def intent_panic(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="panic", step=None)
    bot.send_message(m.chat.id, "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку\n3) 10 медленных вдохов\n4) Запиши триггер\n5) Вернись к плану/закрой по правилу", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def intent_start_help(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="start_help", step=None)
    bot.send_message(m.chat.id, "Предлагаю так:\n1) Заполним паспорт\n2) Выберем фокус недели\n3) Соберём скелет ТС\nС чего начнём — паспорт или фокус недели?", reply_markup=main_menu())

# Диалог Урок 1 (короткий MERCEDES-flow MVP)
MER_QUESTIONS = [
    ("ask_context",  "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует ошибке? (1–2 предложения)"),
    ("ask_emotions", "ЭМОЦИИ. Что чувствуешь в момент ошибки? Как ощущается в теле? (несколько слов)"),
    ("ask_thoughts", "МЫСЛИ. Что говоришь себе в этот момент? (1–2 фразы цитатами)"),
    ("ask_behavior", "ПОВЕДЕНИЕ. Что ты конкретно делаешь? Опиши действия глаголами (1–2 предложения)"),
    ("ask_goal",     "Сформулируй новое желаемое поведение позитивно: *что будешь делать вместо прежнего?*")
]

@bot.message_handler(content_types=["text"])
def router(m: types.Message):
    uid = m.from_user.id
    txt = (m.text or "").strip()
    st = get_state(uid)
    intent = st.get("intent")
    step = st.get("step")
    data = st.get("data") or {}
    last_prompt = None

    # Общий «умный» ответ на уточнение в рамках вопроса
    if intent == "error" and step:
        for key, prompt in MER_QUESTIONS:
            if step == key:
                last_prompt = prompt
                break
        if last_prompt:
            hint = sidecar_or_hint(txt, last_prompt)
            if hint:
                bot.send_message(m.chat.id, hint, reply_markup=main_menu())
                return

    # Ветка "Ошибка"
    if intent == "error":
        if step == "ask_error" or step is None:
            # Берём формулировку ошибки (минимум 3 символа)
            if len(txt) < 3:
                bot.send_message(m.chat.id, "Нужна короткая формулировка ошибки (1–2 предложения).")
                return
            data["error_text"] = txt
            save_state(uid, step="ask_context", data=data)
            bot.send_message(m.chat.id, MER_QUESTIONS[0][1], reply_markup=main_menu())
            return

        # Последовательно задаём MER-вопросы
        for idx, (key, prompt) in enumerate(MER_QUESTIONS):
            if step == key:
                field_map = {
                    "ask_context":  "mer_context",
                    "ask_emotions": "mer_emotions",
                    "ask_thoughts": "mer_thoughts",
                    "ask_behavior": "mer_behavior",
                    "ask_goal":     "positive_goal",
                }
                data[field_map[key]] = txt
                if idx + 1 < len(MER_QUESTIONS):
                    next_key, next_prompt = MER_QUESTIONS[idx+1]
                    save_state(uid, step=next_key, data=data)
                    bot.send_message(m.chat.id, next_prompt, reply_markup=main_menu())
                else:
                    # Финал MER — сохраним в таблицу errors (если есть БД)
                    if engine:
                        try:
                            with engine.begin() as conn:
                                conn.execute(text("""
                                    INSERT INTO errors(user_id, error_text, pattern_behavior, pattern_emotion, pattern_thought, positive_goal)
                                    VALUES (:u, :err, :beh, :emo, :th, :pg)
                                """), {
                                    "u": uid,
                                    "err": data.get("error_text",""),
                                    "beh": data.get("mer_behavior",""),
                                    "emo": data.get("mer_emotions",""),
                                    "th":  data.get("mer_thoughts",""),
                                    "pg":  data.get("positive_goal",""),
                                })
                        except SQLAlchemyError as e:
                            log.error(f"INSERT errors failed: {e}")
                    save_state(uid, intent="idle", step=None, data={})
                    bot.send_message(
                        m.chat.id,
                        "Готово. Зафиксировал паттерн и цель. Хочешь сразу оформить TOTE под эту цель?",
                        reply_markup=main_menu()
                    )
                return

    # Фолбэк — если текст вне сценария
    bot.send_message(
        m.chat.id,
        "Принял. Чтобы двигаться быстрее — выбери пункт в меню ниже или напиши /menu.",
        reply_markup=main_menu()
    )

# ---------- WEB ----------
app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify({"status":"ok","time": datetime.utcnow().isoformat() + "Z"})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    # Безопасность периметра
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    try:
        upd = types.Update.de_json(request.get_data().decode("utf-8"))
        # Диагностика: короткий лог
        who = None
        try:
            if upd.message and upd.message.from_user:
                who = upd.message.from_user.id
            elif upd.callback_query and upd.callback_query.from_user:
                who = upd.callback_query.from_user.id
        except Exception:
            pass
        log.info(f"update <- {who} type={('callback' if upd.callback_query else 'message')}")
        bot.process_new_updates([upd])
        return "OK", 200
    except Exception as e:
        log.exception(f"webhook exception: {e}")
        return "ERR", 500

@app.get("/")
def root():
    return "OK"

if __name__ == "__main__":
    # НИКАКОГО polling — только вебхук
    port = int(os.getenv("PORT", "10000"))
    log.info(f"Starting Flask on 0.0.0.0:{port}, webhook /{WEBHOOK_PATH}")
    app.run(host="0.0.0.0", port=port)
