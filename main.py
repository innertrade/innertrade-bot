# main.py
import os, json, logging
from datetime import datetime, timezone

from flask import Flask, request, jsonify, abort
from telebot import TeleBot, types
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("innertrade")

# ---------- ENV ----------
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")
DATABASE_URL       = os.getenv("DATABASE_URL")
PUBLIC_URL         = os.getenv("PUBLIC_URL")
WEBHOOK_PATH       = os.getenv("WEBHOOK_PATH")  # например: wbhk_9t3x
TG_WEBHOOK_SECRET  = os.getenv("TG_WEBHOOK_SECRET")  # должен совпадать с secret_token в setWebhook

if not TELEGRAM_TOKEN:    raise RuntimeError("TELEGRAM_TOKEN missing")
if not OPENAI_API_KEY:    raise RuntimeError("OPENAI_API_KEY missing")
if not PUBLIC_URL:        raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if not WEBHOOK_PATH:      raise RuntimeError("WEBHOOK_PATH missing (e.g., wbhk_XXXX)")
if not TG_WEBHOOK_SECRET: raise RuntimeError("TG_WEBHOOK_SECRET missing (use same as setWebhook&secret_token=)")

# ---------- OPENAI ----------
# Лёгкая обёртка: вызываем Chat Completions только при «свободной беседе».
from openai import OpenAI
oai = OpenAI(api_key=OPENAI_API_KEY)
GPT_MODEL = "gpt-4o-mini"

def coach_reply(user_name: str | None, prompt: str) -> str:
    """Ненавязчивый ответ-коуч: 1 короткий вопрос + эмпатия. Без ухода из сценария."""
    sys = (
        "Ты коуч-трейдинг ассистент Innertrade. Общайся тепло, кратко, по делу. "
        "Позволь человеку выговориться 1–2 реплики, затем мягко подведи к формулировке "
        "конкретной проблемы на уровне поведения/навыка (без терминов MERCEDES/TOTE). "
        "Не давай длинных лекций и не требуй немедленно «вернуться к курсу». "
        "В конце задай один уточняющий вопрос."
    )
    name = user_name or "друг"
    msgs = [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"{name}: {prompt}"}
    ]
    try:
        r = oai.chat.completions.create(model=GPT_MODEL, messages=msgs, temperature=0.4, max_tokens=180)
        return r.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"OpenAI fallback error: {e}")
        return "Понимаю. Расскажи ещё чуть подробнее, что конкретно тебя выбивает в моменте?"

# ---------- DB ----------
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        # создадим таблицу user_state, если вдруг её нет
        with engine.begin() as conn:
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_state (
                user_id BIGINT PRIMARY KEY,
                intent  TEXT,
                step    TEXT,
                data    JSONB,
                updated_at TIMESTAMPTZ DEFAULT now()
            );
            """))
        log.info("DB connected")
    except OperationalError as e:
        log.warning(f"DB not reachable: {e}")
        engine = None
else:
    log.info("DATABASE_URL not set — running without DB")

def get_state(uid: int) -> dict:
    st = {"intent": "idle", "step": None, "data": {}}
    if not engine: return st
    row = None
    with engine.begin() as conn:
        row = conn.execute(text("SELECT intent, step, data FROM user_state WHERE user_id=:u"),
                           {"u": uid}).mappings().first()
    if row:
        st["intent"] = row["intent"]
        st["step"]   = row["step"]
        st["data"]   = row["data"] or {}
    return st

def save_state(uid: int, intent: str | None = None, step: str | None = None, data_patch: dict | None = None):
    if not engine: return
    cur = get_state(uid)
    if intent is not None: cur["intent"] = intent
    if step   is not None: cur["step"]   = step
    if data_patch:
        cur["data"] = (cur["data"] or {}) | data_patch
    with engine.begin() as conn:
        conn.execute(text("""
        INSERT INTO user_state(user_id, intent, step, data, updated_at)
        VALUES (:u, :i, :s, COALESCE(:d, '{}'::jsonb), now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent = EXCLUDED.intent,
            step   = EXCLUDED.step,
            data   = EXCLUDED.data,
            updated_at = now()
        """), {"u": uid, "i": cur["intent"], "s": cur["step"], "d": json.dumps(cur["data"])})

# ---------- TELEGRAM ----------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

BOT_PUBLIC_NAME = "Innertrade"

def ask_name(chat_id: int):
    bot.send_message(chat_id, "Как тебя зовут? (можно ник)", reply_markup=types.ReplyKeyboardRemove())

def ask_addressing(chat_id: int):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row("ты", "вы")
    bot.send_message(chat_id, "Как удобнее обращаться — на *ты* или на *вы*?", reply_markup=kb)

# ---------- Команды ----------
@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    st = get_state(m.from_user.id)
    bot.send_message(
        m.chat.id,
        "```\n" + json.dumps({
            "ok": True,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "intent": st["intent"],
            "step": st["step"],
            "db": "ok" if engine else "none"
        }, ensure_ascii=False, indent=2) + "\n```",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=["start", "reset", "menu"])
def cmd_start(m):
    uid = m.from_user.id
    # Сбросим «мягко»: имя и обращение сохраним, если были.
    st = get_state(uid)
    name = (st["data"] or {}).get("name")
    address = (st["data"] or {}).get("address")
    save_state(uid, intent="greet", step="ask_name" if not name else ("ask_address" if not address else None))
    greet = f"👋 Привет{', ' + name if name else ''}! "
    if not name:
        bot.send_message(m.chat.id, greet + "Давай познакомимся.", reply_markup=types.ReplyKeyboardRemove())
        ask_name(m.chat.id)
    elif not address:
        bot.send_message(m.chat.id, greet + "Как удобнее общаться?", reply_markup=types.ReplyKeyboardRemove())
        ask_addressing(m.chat.id)
    else:
        bot.send_message(m.chat.id, greet + "Выбери пункт или просто расскажи, что болит сейчас.",
                         reply_markup=main_menu())

# ---------- Интенты-кнопки ----------
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def btn_error(m):
    uid = m.from_user.id
    save_state(uid, intent="error", step="ask_error")
    bot.send_message(
        m.chat.id,
        "Опиши основную проблему в 1–2 предложениях (как ты *действуешь* в моменте).",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def btn_strategy(m):
    uid = m.from_user.id
    save_state(uid, intent="strategy", step="intro")
    bot.send_message(
        m.chat.id,
        "Соберём каркас ТС:\n1) подход/рынки/ТФ\n2) вход\n3) стоп/сопровождение/выход\n4) риск/лимиты.",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def btn_passport(m):
    uid = m.from_user.id
    save_state(uid, intent="passport", step="q_markets")
    bot.send_message(m.chat.id, "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?",
                     reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def btn_weekpanel(m):
    uid = m.from_user.id
    save_state(uid, intent="week_panel", step="focus")
    bot.send_message(m.chat.id, "Панель недели: выбери фокус на ближайшие 5 торговых дней (коротко).",
                     reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def btn_panic(m):
    uid = m.from_user.id
    save_state(uid, intent="panic", step=None)
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой график\n3) 10 медленных вдохов\n4) Запиши триггер\n5) Вернись к плану/закрой по правилу.",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def btn_start_help(m):
    uid = m.from_user.id
    save_state(uid, intent="start_help", step=None)
    bot.send_message(
        m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС.\nС чего начнём?",
        reply_markup=main_menu()
    )

# ---------- Естественный диалог + сценарии ----------
def looks_like_greeting(text: str) -> bool:
    t = text.lower().strip()
    return any(w in t for w in ["привет", "здрав", "добрый", "hi", "hello"])

def asked_bot_name(text: str) -> bool:
    t = text.lower()
    return ("как" in t and "зовут" in t) or ("твое имя" in t) or ("твоё имя" in t)

def maybe_problem_sentence(text: str) -> bool:
    # примитивный детектор «ошибки»: глаголы действия + торговые термины
    t = text.lower()
    verbs = ["вхожу","захожу","выход","двигаю","переношу","фиксир","усредня","добавля","закрыва","открыва"]
    market = ["сделк","стоп","тейк","сетап","позици","просад","рынок","торгов"]
    return any(v in t for v in verbs) and any(m in t for m in market)

@bot.message_handler(content_types=["text"])
def on_text(m):
    uid = m.from_user.id
    st = get_state(uid)
    text = (m.text or "").strip()

    # 0) «как тебя зовут?»
    if asked_bot_name(text):
        bot.send_message(m.chat.id, f"Я — {BOT_PUBLIC_NAME}. Рад знакомству!")
        return

    # 1) Диалог знакомства: имя → «ты/вы»
    if st["intent"] == "greet" and (st["step"] in (None, "ask_name", "ask_address")):
        data = st["data"] or {}
        if st["step"] in (None, "ask_name"):
            # Сохраним имя (очистим эмодзи/лишние кавычки по-простому)
            name = text.strip().strip('«»"\'🙂🥲😀😅🤝').split()[0][:24]
            if len(name) < 1: name = None
            if name:
                save_state(uid, step="ask_address", data_patch={"name": name})
                ask_addressing(m.chat.id)
            else:
                bot.send_message(m.chat.id, "Не расслышал имя. Можно просто коротко — как к тебе обращаться?")
            return
        if st["step"] == "ask_address":
            t = text.lower()
            if t in ("ты","вы"):
                save_state(uid, intent="idle", step=None, data_patch={"address": t})
                greet = f"Принято, { (st['data'] or {}).get('name') or '' }."
                bot.send_message(m.chat.id, f"{greet} Можем просто поговорить или выбрать пункт в меню.", reply_markup=main_menu())
            else:
                ask_addressing(m.chat.id)
            return

    # 2) Кнопка «ошибка» (первый шаг): пользователь написал ошибку?
    if st["intent"] == "error":
        if st["step"] == "ask_error":
            # мягкая конкретизация: если абстрактно — один уточняющий вопрос; если достаточно — резюме и подтверждение
            if not maybe_problem_sentence(text):
                save_state(uid, step="nudge_error")
                bot.send_message(
                    m.chat.id,
                    "Понял. Чтобы точнее помочь, уточни, пожалуйста: *что именно ты делаешь* (глаголами) и *в какой момент*?\nНапример: «вхожу до формирования сигнала», «двигаю стоп после входа».",
                    reply_markup=main_menu()
                )
            else:
                # Короткое резюме и подтверждение
                save_state(uid, step="confirm_error", data_patch={"error_text": text})
                bot.send_message(
                    m.chat.id,
                    f"Зафиксировал так:\n> {text}\n\nПодходит формулировка? Напиши *да* или скорректируй своими словами."
                )
            return
        if st["step"] == "nudge_error":
            # второе приближение: примем как есть
            save_state(uid, step="confirm_error", data_patch={"error_text": text})
            bot.send_message(
                m.chat.id,
                f"Ок, записал:\n> {text}\n\nПодходит формулировка? Напиши *да* или поправь."
            )
            return
        if st["step"] == "confirm_error":
            if text.lower() in ("да","ок","подходит","верно","ага"):
                # переход к MERCEDES (без терминов)
                save_state(uid, step="mer_context")
                bot.send_message(m.chat.id, "Начнём разбор. *Ситуация*: в какой момент это обычно случается? Что предшествует? (1–2 предложения)")
                return
            else:
                # пользователь уточнил формулировку — примем и пойдём дальше
                save_state(uid, step="mer_context", data_patch={"error_text": text})
                bot.send_message(m.chat.id, "Принято. *Ситуация*: когда это обычно случается? Что предшествует?")
                return
        # Короткая «MERCEDES» без терминов
        if st["step"] == "mer_context":
            save_state(uid, step="mer_emotions", data_patch={"mer_context": text})
            bot.send_message(m.chat.id, "Эмоции: что чувствуешь в этот момент? (несколько слов)")
            return
        if st["step"] == "mer_emotions":
            save_state(uid, step="mer_thoughts", data_patch={"mer_emotions": text})
            bot.send_message(m.chat.id, "Мысли: что говоришь себе? (1–2 короткие фразы)")
            return
        if st["step"] == "mer_thoughts":
            save_state(uid, step="mer_behavior", data_patch={"mer_thoughts": text})
            bot.send_message(m.chat.id, "Действия: что конкретно делаешь? (глаголами, 1–2 предложения)")
            return
        if st["step"] == "mer_behavior":
            # резюме — *интерпретация*, а не копипаст
            st2 = get_state(uid)
            d = st2["data"] or {}
            error_text = d.get("error_text","ошибка")
            resume = (
                f"Вижу паттерн: при «{d.get('mer_context','…')}» "
                f"возникают «{d.get('mer_emotions','…')}», мысли «{d.get('mer_thoughts','…')}», "
                f"и ты делаешь «{text}», что приводит к ошибке «{error_text}»."
            )
            save_state(uid, step="goal_new", data_patch={"mer_behavior": text, "pattern_resume": resume})
            bot.send_message(m.chat.id, f"{resume}\n\nСформулируем новую цель как *поведение*: как ты хочешь действовать в следующий раз? (1–2 предложения, наблюдаемо)")
            return
        if st["step"] == "goal_new":
            # переход к «TOTE» (без терминов)
            save_state(uid, step="tote_ops", data_patch={"positive_goal": text})
            bot.send_message(m.chat.id, "Ок. Какие *конкретные шаги* помогут удерживать новое поведение? (чек-лист 2–4 пункта)")
            return
        if st["step"] == "tote_ops":
            save_state(uid, step="tote_check", data_patch={"tote_ops": text})
            bot.send_message(m.chat.id, "Как проверишь, что получилось? (критерий на 1–3 сделки)")
            return
        if st["step"] == "tote_check":
            save_state(uid, step=None, intent="idle", data_patch={"tote_check": text})
            bot.send_message(
                m.chat.id,
                "Готово! Мы зафиксировали:\n— проблему\n— паттерн\n— новую цель\n— шаги и проверку.\n"
                "Можем добавить это в фокус недели или вернуться в меню.",
                reply_markup=main_menu()
            )
            return

    # 3) Свободный диалог: GPT помогает «мягко» и удерживает рамку
    #    (но если распознан «ошибочный» текст — переведём в сценарий «ошибка»).
    if maybe_problem_sentence(text) and st["intent"] not in ("error",):
        save_state(uid, intent="error", step="confirm_error", data_patch={"error_text": text})
        bot.send_message(
            m.chat.id,
            f"Понял тебя так:\n> {text}\n\nПодходит формулировка? Напиши *да* или поправь.",
            reply_markup=main_menu()
        )
        return

    # иначе — одна короткая коуч-реплика от GPT
    name = (st["data"] or {}).get("name")
    reply = coach_reply(name, text)
    bot.send_message(m.chat.id, reply, reply_markup=main_menu())

# ---------- FLASK / WEBHOOK ----------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK", 200

@app.get("/health")
def health():
    return jsonify({"status":"ok","time": datetime.now(timezone.utc).isoformat()})

@app.post(f"/{WEBHOOK_PATH}")
def tg_webhook():
    # проверка секрета
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if not request.is_json:
        abort(400)
    upd = request.get_json(force=True, silent=True)
    try:
        update = types.Update.de_json(upd)
        bot.process_new_updates([update])
    except Exception as e:
        log.exception(f"Webhook update error: {e}")
    return "OK", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT","10000"))
    log.info("Starting Flask webhook server…")
    app.run(host="0.0.0.0", port=port)
