# main.py
import os, json, logging, time
from datetime import datetime
from typing import Optional, Tuple, Dict, Any

from flask import Flask, request, jsonify, abort
from telebot import TeleBot, types
from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ---------- ENV ----------
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
DATABASE_URL        = os.getenv("DATABASE_URL")
PUBLIC_URL          = os.getenv("PUBLIC_URL")           # https://innertrade-bot.onrender.com
WEBHOOK_PATH        = os.getenv("WEBHOOK_PATH", "wbhk")
TG_WEBHOOK_SECRET   = os.getenv("TG_WEBHOOK_SECRET")

if not TELEGRAM_TOKEN:    raise RuntimeError("TELEGRAM_TOKEN missing")
if not OPENAI_API_KEY:    raise RuntimeError("OPENAI_API_KEY missing")
if not PUBLIC_URL:        raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if not TG_WEBHOOK_SECRET: raise RuntimeError("TG_WEBHOOK_SECRET missing")

WEBHOOK_URL = f"{PUBLIC_URL.rstrip('/')}/{WEBHOOK_PATH}"

# ---------- OPENAI ----------
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- DB ----------
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, isolation_level="AUTOCOMMIT")
        with engine.begin() as conn:
            # users + user_state + errors (минимум, остальное миграциями)
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
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS errors (
              id                BIGSERIAL PRIMARY KEY,
              user_id           BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
              error_text        TEXT NOT NULL,
              pattern_behavior  TEXT,
              pattern_emotion   TEXT,
              pattern_thought   TEXT,
              positive_goal     TEXT,
              tote_goal         TEXT,
              tote_ops          TEXT,
              tote_check        TEXT,
              tote_exit         TEXT,
              checklist_pre     TEXT,
              checklist_post    TEXT,
              created_at        TIMESTAMPTZ DEFAULT now()
            );
            """))
        logging.info("DB connected & migrated")
    except OperationalError as e:
        logging.warning(f"DB not available yet: {e}")
        engine = None
else:
    logging.info("DATABASE_URL not set — running without DB")

def db_exec(sql: str, params: dict | None = None):
    if not engine: return None
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})

def ensure_user(uid: int):
    db_exec("INSERT INTO users(user_id) VALUES (:uid) ON CONFLICT (user_id) DO NOTHING", {"uid": uid})

def get_state(uid: int) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
    if not engine:
        return None, None, {}
    row = db_exec("SELECT intent, step, COALESCE(data, '{}'::jsonb) AS data FROM user_state WHERE user_id=:uid", {"uid": uid}).fetchone()
    if not row:
        return None, None, {}
    intent, step, data = row[0], row[1], row[2]
    if isinstance(data, str):
        try: data = json.loads(data)
        except: data = {}
    return intent, step, data or {}

def set_state(uid: int, intent: Optional[str] = None, step: Optional[str] = None,
              data_update: Optional[Dict[str, Any]] = None, replace: bool=False):
    if not engine: return
    ensure_user(uid)
    cur_intent, cur_step, cur_data = get_state(uid)
    new_intent = intent if intent is not None else cur_intent
    new_step   = step   if step   is not None else cur_step
    if replace:
        new_data = data_update or {}
    else:
        new_data = {**(cur_data or {}), **(data_update or {})}
    db_exec("""
        INSERT INTO user_state(user_id, intent, step, data)
        VALUES (:uid, :intent, :step, CAST(:data AS jsonb))
        ON CONFLICT (user_id) DO UPDATE
           SET intent=:intent, step=:step, data=CAST(:data AS jsonb), updated_at=now()
    """, {"uid": uid, "intent": new_intent, "step": new_step, "data": json.dumps(new_data)})

# ---------- TELEGRAM ----------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

# Главное меню
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

# Утилиты NATURAL LLM
def llm_short_reply(user_text: str) -> str:
    """Натуральный короткий ответ на уточняющий/нетиповой вопрос — без ухода из сценария."""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"Отвечай кратко, по-доброму, в стиле коуча. 2–3 предложения максимум."},
                {"role":"user","content": user_text}
            ],
            temperature=0.3,
            max_tokens=120,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.warning(f"LLM short reply error: {e}")
        return "Понимаю. Давайте я помогу сузить тему и двинемся по шагам."

BEHAVIOR_VERBS = [
    "вхожу","захожу","выход","выхожу","двигаю","переношу","фиксирую",
    "усредняю","добавляю","закрываю","пересиживаю","пересиживаю убыток",
    "перезаход","усиливаю","обнуляю","прибираю","шортю","лонгую"
]

def looks_behavioral(text_in: str) -> bool:
    t = text_in.lower()
    return any(v in t for v in BEHAVIOR_VERBS) or len(t) >= 120

def llm_summarize_problem(vent: list[str]) -> str:
    """Сформулировать проблему на уровне поведения/навыка, избегая уровня убеждений на старте."""
    joined = "\n".join(vent[-6:])  # последние реплики
    prompt = f"""
Сформулируй одну конкретную проблему трейдера на уровне поведения/навыка (не убеждений),
одной фразой, без воды и общих слов. Примеры формата: «вхожу до формирования сигнала»,
«двигаю стоп после входа», «закрываю по первой коррекции». Ввод:
{joined}
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"Ты помощник-коуч. Выдавай только одну фразу-конкретику про поведение/навык."},
                {"role":"user","content": prompt}
            ],
            temperature=0.2,
            max_tokens=60
        )
        return resp.choices[0].message.content.strip().strip("—- ")
    except Exception as e:
        logging.warning(f"LLM summarize error: {e}")
        # запасной вариант
        return "вхожу до формирования сигнала"

# --------- Команды ---------
@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m):
    ensure_user(m.from_user.id)
    set_state(m.from_user.id, intent="idle", step=None, data_update={"vent":[], "draft_problem":None}, replace=True)
    bot.send_message(m.chat.id,
        "👋 Привет! Я наставник *Innertrade*.\nВыбери пункт или напиши текст.\nКоманды: /status /ping",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["ping"])
def cmd_ping(m): bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    intent, step, data = get_state(m.from_user.id)
    bot.send_message(m.chat.id, f"ℹ️ Статус: intent=`{intent}` step=`{step}` vent={len((data or {}).get('vent',[]))}")

# --------- Интенты (кнопки) ---------
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def btn_error(m):
    set_state(m.from_user.id, intent="error", step="free_talk", data_update={"vent":[], "draft_problem":None}, replace=True)
    bot.send_message(m.chat.id,
        "Опиши основную ошибку 1–2 предложениями **на уровне поведения/навыка**.\n"
        "Примеры: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю по первой коррекции».",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def btn_strategy(m):
    set_state(m.from_user.id, intent="strategy", step=None)
    bot.send_message(m.chat.id,
        "Ок, собираем ТС по конструктору:\n"
        "1) Цели\n2) Стиль (дневной/свинг/позиционный)\n3) Рынки/инструменты\n"
        "4) Правила входа/выхода\n5) Риск (%, стоп)\n6) Сопровождение\n7) Тестирование",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def btn_passport(m):
    set_state(m.from_user.id, intent="passport", step=None)
    bot.send_message(m.chat.id, "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def btn_week_panel(m):
    set_state(m.from_user.id, intent="week_panel", step=None)
    bot.send_message(m.chat.id,
        "Панель недели:\n• Фокус недели\n• План (3 шага)\n• Лимиты\n• Ритуалы\n• Короткая ретро в конце недели",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def btn_panic(m):
    set_state(m.from_user.id, intent="panic", step=None)
    bot.send_message(m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку с графиком\n3) 10 медленных вдохов\n"
        "4) Запиши триггер (что именно выбило)\n5) Вернись к плану сделки или закрой позицию по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def btn_start_help(m):
    set_state(m.from_user.id, intent="start_help", step=None)
    bot.send_message(m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\n"
        "С чего начнём — паспорт или фокус недели?",
        reply_markup=main_menu()
    )

# --------- ВЕТКА «ОШИБКА»: выговор → синтез → подтверждение → MERCEDES ----------
def ask_mercedes_first(chat_id: int, uid: int):
    set_state(uid, step="m_context")
    bot.send_message(chat_id, "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует ошибке? (1–2 предложения)")

def propose_problem(chat_id: int, uid: int, problem: str):
    # inline confirm/refine
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Да, верно", callback_data="pr_confirm"))
    kb.add(types.InlineKeyboardButton("✍️ Дополнить/исправить", callback_data="pr_refine"))
    bot.send_message(chat_id,
        f"Я услышал так:\n\n*Рабочая формулировка ошибки*: _{problem}_\n\nПодходит?\n"
        "Это нужно, чтобы двигаться дальше по разборам.",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data in ["pr_confirm","pr_refine"])
def cb_problem_confirm(c):
    uid = c.from_user.id
    intent, step, data = get_state(uid)
    if intent != "error":
        bot.answer_callback_query(c.id, "Ок")
        return
    if c.data == "pr_confirm":
        bot.answer_callback_query(c.id, "Зафиксировали")
        # фиксируем в errors (минимум — текст ошибки)
        if engine and data.get("draft_problem"):
            db_exec("""
                INSERT INTO errors(user_id, error_text) VALUES (:uid, :txt)
            """, {"uid": uid, "txt": data["draft_problem"]})
        ask_mercedes_first(c.message.chat.id, uid)
    else:
        bot.answer_callback_query(c.id, "Ок, давай уточним")
        set_state(uid, step="refine_problem")
        bot.send_message(c.message.chat.id, "Как бы ты это сформулировал(а) точнее? 1–2 коротких предложения.")

@bot.message_handler(func=lambda m: get_state(m.from_user.id)[0] == "error")
def flow_error(m):
    uid, chat_id, txt = m.from_user.id, m.chat.id, (m.text or "").strip()
    intent, step, data = get_state(uid)
    data = data or {}

    # Вариант: уже в MERCEDES
    if step and step.startswith("m_"):
        if step == "m_context":
            set_state(uid, step="m_emotions", data_update={"mer_context": txt})
            bot.send_message(chat_id, "ЭМОЦИИ. Что чувствуешь в момент ошибки? Как ощущается в теле? (несколько слов)")
        elif step == "m_emotions":
            set_state(uid, step="m_thoughts", data_update={"mer_emotions": txt})
            bot.send_message(chat_id, "МЫСЛИ. Что говоришь себе в этот момент? (1–2 фразы)")
        elif step == "m_thoughts":
            set_state(uid, step="m_behavior", data_update={"mer_thoughts": txt})
            bot.send_message(chat_id, "ПОВЕДЕНИЕ. Что ты конкретно делаешь? (глаголами, 1–2 предложения)")
        elif step == "m_behavior":
            set_state(uid, step="m_values", data_update={"mer_behavior": txt})
            bot.send_message(chat_id, "ЦЕННОСТИ/УБЕЖДЕНИЯ. Почему кажется, что «так и надо»? (коротко)")
        elif step == "m_values":
            set_state(uid, step="m_state", data_update={"mer_values": txt})
            bot.send_message(chat_id, "СОСТОЯНИЕ. В каком состоянии входил? Что доминировало: тревога/азарт/контроль?")
        elif step == "m_state":
            # Резюме → переход к TOTE (минимальная заглушка)
            set_state(uid, step="tote_goal", data_update={"mer_state": txt})
            bot.send_message(chat_id,
                "Понял. Сформируем цель по TOTE.\nЧто будет *позитивной целью* в следующей сделке? (1 фраза, наблюдаемо)")
        elif step == "tote_goal":
            set_state(uid, step="tote_ops", data_update={"tote_goal": txt})
            bot.send_message(chat_id, "Какие шаги помогут удержать цель? (3 пункта: чек-лист/ритуал/таймер и т.п.)")
        elif step == "tote_ops":
            set_state(uid, step="tote_check", data_update={"tote_ops": txt})
            bot.send_message(chat_id, "Как проверим, что цель удержана? (критерий, например: 3 сделки без сдвига стопа)")
        elif step == "tote_check":
            set_state(uid, step="tote_exit", data_update={"tote_check": txt})
            bot.send_message(chat_id, "Финальный шаг: если критерий выполнен — что фиксируем? Если нет — что меняем?")
        elif step == "tote_exit":
            # финал ветки
            set_state(uid, intent="idle", step=None, data_update={"tote_exit": txt})
            bot.send_message(chat_id,
                "Отлично. Зафиксировал. Если хочешь — добавим это в Панель недели позже. Готов двигаться дальше.",
                reply_markup=main_menu()
            )
        return

    # Режим уточнения формулировки
    if step == "refine_problem":
        # Обновляем «черновик» и снова предлагаем подтвердить
        draft = txt
        set_state(uid, step="confirm_problem", data_update={"draft_problem": draft})
        propose_problem(chat_id, uid, draft)
        return

    # FREE TALK: выговор → авто-синтез → подтверждение
    vent = data.get("vent", [])
    # Если это вопрос/уточнение — ответим коротко в коуч-стиле и попросим продолжить
    if "?" in txt and (len(txt) <= 140 or not looks_behavioral(txt)):
        reply = llm_short_reply(txt)
        bot.send_message(chat_id, reply)
        bot.send_message(chat_id, "Добавь ещё подробностей: когда это чаще случается и что ты в этот момент делаешь?")
        vent.append(txt)
        set_state(uid, step="free_talk", data_update={"vent": vent})
        return

    vent.append(txt)
    set_state(uid, step="free_talk", data_update={"vent": vent})

    # Done-условие: (а) ≥2 реплик ИЛИ (б) явная поведенческая конкретика
    if len(vent) >= 2 or looks_behavioral(txt):
        problem = llm_summarize_problem(vent)
        set_state(uid, step="confirm_problem", data_update={"draft_problem": problem})
        propose_problem(chat_id, uid, problem)
        return

    # Иначе — мягко продолжаем выговор
    bot.send_message(chat_id,
        "Понимаю. Расскажи ещё чуть-чуть: *в какой ситуации* это чаще происходит и *что ты делаешь* дальше?",
    )

# --------- Fallback: прочий текст вне веток ----------
@bot.message_handler(content_types=["text"])
def fallback(m):
    bot.send_message(m.chat.id, "Принял. Чтобы было быстрее, выбери пункт в меню или напиши /menu.", reply_markup=main_menu())

# ---------- Flask / Webhook ----------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK", 200

@app.get("/health")
def health():
    return jsonify({"status":"ok","time":datetime.utcnow().isoformat()+"Z"})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    update = request.get_data().decode("utf-8")
    bot.process_new_updates([types.Update.de_json(update)])
    return "ok"

def install_webhook():
    try:
        bot.remove_webhook()
    except Exception:
        pass
    time.sleep(0.5)
    ok = bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=TG_WEBHOOK_SECRET,
        allowed_updates=["message","callback_query"]
    )
    logging.info(f"Webhook set -> {ok} @ {WEBHOOK_URL}")

if __name__ == "__main__":
    install_webhook()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
