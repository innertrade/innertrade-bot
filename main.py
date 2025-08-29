import os, json, time
from datetime import datetime
from flask import Flask, request, jsonify, abort
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import create_engine, text
from openai import OpenAI

# === ENV ===
TOKEN            = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL     = os.getenv("DATABASE_URL")
PUBLIC_URL       = os.getenv("PUBLIC_URL")                    # https://innertrade-bot.onrender.com
WEBHOOK_PATH     = os.getenv("WEBHOOK_PATH", "webhook")       # например: wbhk_9t3x
TG_SECRET        = os.getenv("TG_WEBHOOK_SECRET")             # длинная случайная строка
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OFFSCRIPT        = os.getenv("OFFSCRIPT_ENABLED", "true").lower() == "true"
APP_VERSION      = os.getenv("APP_VERSION", "2025-08-29")

if not TOKEN or not DATABASE_URL or not PUBLIC_URL or not TG_SECRET:
    raise RuntimeError("Missing required env: TELEGRAM_TOKEN, DATABASE_URL, PUBLIC_URL, TG_WEBHOOK_SECRET")

# === CORE ===
app   = Flask(__name__)
bot   = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=False)
eng   = create_engine(DATABASE_URL, pool_pre_ping=True)
oai   = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

MENU_BUTTONS = [
    ["🚑 У меня ошибка", "🧩 Хочу стратегию"],
    ["📄 Паспорт", "🗒 Панель недели"],
    ["🆘 Экстренно", "🤔 Не знаю, с чего начать"],
]

def main_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    for row in MENU_BUTTONS:
        kb.add(*[KeyboardButton(x) for x in row])
    return kb

def db_exec(sql, params=None, fetch=False, one=False):
    with eng.begin() as conn:
        cur = conn.execute(text(sql), params or {})
        if fetch:
            rows = [dict(r._mapping) for r in cur.fetchall()]
            return rows[0] if (one and rows) else rows
        return None

# --- state helpers ---
def ensure_user(uid: int):
    db_exec("INSERT INTO users(user_id) VALUES(:u) ON CONFLICT(user_id) DO NOTHING", {"u": uid})

def get_state(uid: int):
    row = db_exec("SELECT intent, step, data FROM user_state WHERE user_id=:u", {"u": uid}, fetch=True, one=True)
    if not row:
        return {"intent": "idle", "step": "greet", "data": {}}
    return {"intent": row["intent"] or "idle", "step": row["step"] or "greet", "data": row["data"] or {}}

def set_state(uid: int, intent=None, step=None, data=None):
    cur = get_state(uid)
    if intent is None: intent = cur["intent"]
    if step   is None: step   = cur["step"]
    if data   is None: data   = cur["data"]
    db_exec("""
        INSERT INTO user_state(user_id,intent,step,data,updated_at)
        VALUES(:u,:i,:s,CAST(:d AS jsonb), now())
        ON CONFLICT(user_id) DO UPDATE SET intent=:i, step=:s, data=CAST(:d AS jsonb), updated_at=now()
    """, {"u": uid, "i": intent, "s": step, "d": json.dumps(data)})

# --- GPT helpers ---
SYS_FREE = (
"Ты коуч по трейдингу. Общайся кратко, по-доброму, естественно. "
"Никогда не цитируй пользователя дословно — делай короткий перефраз 1–2 строками. "
"Задай 1 уточняющий вопрос. Если уже слышна конкретная проблема на уровне поведения/навыка "
"(например: «вхожу до сигнала», «двигаю стоп», «закрываю на коррекции»), предложи перейти к короткому разбору. "
"Не называй техники («Мерседес», TOTE) вслух — просто задавай нужные вопросы. "
"Если спрашивают «что такое Паспорт/Панель», дай простое объяснение в 2–3 строки."
)

def gpt_reply(messages):
    if not oai:  # если GPT отключен, отвечаем вручную-минимумом
        return "Понял. Расскажи ещё чуть-чуть, что именно тебя сейчас больше всего цепляет?"
    r = oai.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.3,
        max_tokens=300,
    )
    return r.choices[0].message.content.strip()

# --- tiny heuristics ---
BEHAVIOR_MARKERS = ["вхожу", "вход", "двигаю стоп", "переношу стоп", "закрываю", "усредня", "ставлю безубыток", "фиксирую"]
def looks_like_behavior(text: str) -> bool:
    t = text.lower()
    return any(m in t for m in BEHAVIOR_MARKERS)

def summarize_problem(text: str) -> str:
    # очень короткий «перефраз без цитаты»
    if oai:
        msg = [
            {"role":"system","content":"Кратко перефразируй проблему 1 предложением, без цитирования, по сути."},
            {"role":"user","content":text}
        ]
        try:
            r = oai.chat.completions.create(model=OPENAI_MODEL, messages=msg, temperature=0.1, max_tokens=60)
            return r.choices[0].message.content.strip()
        except Exception:
            pass
    # fallback
    return "Нарушаю правила сопровождения сделки (дергаю стоп/выход раньше времени)."

# --- flows ---
def ask_intro(uid):
    set_state(uid, intent="greet", step="ask_style", data={"address":"ты"})
    kb = main_menu()
    return "👋 Привет! Можем просто поговорить — напиши, что болит в торговле. Или выбери пункт ниже.", kb

def confirm_error(uid, problem_text):
    paraphrase = summarize_problem(problem_text)
    set_state(uid, intent="error_flow", step="confirm_error", data={"error_text": paraphrase})
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Да", callback_data="err_ok"),
           InlineKeyboardButton("✍️ Править", callback_data="err_edit"))
    bot_msg = f"Зафиксирую так: <b>{paraphrase}</b>\nПодходит?"
    return bot_msg, kb

def proceed_error_questions(uid):
    set_state(uid, intent="error_flow", step="ask_context")
    return "Ок, коротко по шагам.\n1) В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)", None

def answer_passport_question():
    return ("<b>Паспорт трейдера</b> — это твой рабочий профиль: цели, рынки/ТФ, стиль, риск-лимиты, "
            "топ-ошибки, архетип/роли и рабочие ритуалы. Его собираем и обновляем по мере работы.")

def answer_week_panel():
    return ("<b>Панель недели</b> — твой недельный фокус: 1 узел внимания, 1–2 цели, лимиты, короткие чек-ины "
            "утром/вечером и мини-ретро в конце недели.")

# --- routing helpers ---
def route_free(uid, text):
    # Q&A по словарю, чтобы не сбивать поток
    low = text.lower().strip()
    if "что такое паспорт" in low or low == "паспорт?":
        return answer_passport_question()
    if "что такое панел" in low or "панель недели" in low:
        return answer_week_panel()

    # OFFSCRIPT: живой ответ + мягкая подсветка шага
    msgs = [{"role":"system","content":SYS_FREE},
            {"role":"user","content":text}]
    reply = gpt_reply(msgs)

    # если уже поведенческий уровень — сразу подтверждаем формулировку
    if looks_like_behavior(text):
        m, kb = confirm_error(uid, text)
        bot.send_message(uid, reply)
        bot.send_message(uid, m, reply_markup=kb)
        return None
    return reply

# === COMMANDS ===
@bot.message_handler(commands=["ping"])
def ping(m):
    bot.reply_to(m, "pong")

@bot.message_handler(commands=["status"])
def status(m):
    s = get_state(m.from_user.id)
    bot.reply_to(m, json.dumps({
        "ok": True,
        "time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "intent": s["intent"],
        "step": s["step"],
        "db": "ok"
    }, ensure_ascii=False, indent=2))

@bot.message_handler(commands=["reset"])
def reset(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, intent="idle", step="greet", data={})
    txt, kb = ask_intro(uid)
    bot.send_message(uid, txt, reply_markup=kb)

# === BUTTONS (main menu) ===
@bot.message_handler(func=lambda msg: msg.text in [row for rows in MENU_BUTTONS for row in rows])
def menu_click(m):
    uid = m.from_user.id
    ensure_user(uid)
    txt = m.text

    if txt == "🚑 У меня ошибка":
        # если уже есть сформулированная ошибка в data — не просим заново
        st = get_state(uid)
        err = (st["data"] or {}).get("error_text")
        if err:
            bot.send_message(uid, f"Возьмём текущую формулировку: <b>{err}</b>")
            msg, _ = proceed_error_questions(uid)
            bot.send_message(uid, msg)
        else:
            set_state(uid, intent="error_flow", step="confirm_or_collect", data={})
            bot.send_message(uid, "Коротко: опиши основную трудность 1–2 предложениями (на уровне поведения/навыка).")

    elif txt == "🧩 Хочу стратегию":
        set_state(uid, intent="strategy_flow", step="start")
        bot.send_message(uid, "Соберём скелет: рынок/ТФ → вход → стоп/сопровождение → риск. Готов?")
    elif txt == "📄 Паспорт":
        set_state(uid, intent="passport_flow", step="start")
        bot.send_message(uid, answer_passport_question())
    elif txt == "🗒 Панель недели":
        set_state(uid, intent="week_panel", step="start")
        bot.send_message(uid, answer_week_panel())
    elif txt == "🆘 Экстренно":
        set_state(uid, intent="emergency", step="stop")
        bot.send_message(uid,
            "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку\n3) 10 медленных вдохов\n"
            "4) Запиши триггер\n5) Вернись к плану сделки или закрой позицию по правилу")
    elif txt == "🤔 Не знаю, с чего начать":
        set_state(uid, intent="idle", step="suggest_route")
        bot.send_message(uid, "Предлагаю так: 1) Паспорт, 2) Фокус недели, 3) Скелет ТС.\nС чего начнём?")
    else:
        bot.send_message(uid, "Принял.")

# === CALLBACKS (inline) ===
@bot.callback_query_handler(func=lambda c: c.data in ["err_ok","err_edit"])
def cb_err_confirm(c):
    uid = c.from_user.id
    st = get_state(uid)
    if c.data == "err_ok":
        msg, _ = proceed_error_questions(uid)
        bot.edit_message_text("Подтвердили. Погнали по шагам.", chat_id=uid, message_id=c.message.message_id)
        bot.send_message(uid, msg)
    else:
        set_state(uid, intent="error_flow", step="confirm_or_collect")
        bot.edit_message_text("Окей, поправь формулировку 1–2 предложениями.", chat_id=uid, message_id=c.message.message_id)

# === TEXT HANDLER ===
@bot.message_handler(content_types=["text"])
def on_text(m):
    uid = m.from_user.id
    txt = (m.text or "").strip()
    ensure_user(uid)
    st = get_state(uid)
    intent, step, data = st["intent"], st["step"], st["data"]

    # быстрые FAQ фразы
    low = txt.lower()
    if low in ["что такое паспорт","паспорт что это","что за паспорт"]:
        bot.reply_to(m, answer_passport_question()); return
    if "панел" in low and "недел" in low and "что" in low:
        bot.reply_to(m, answer_week_panel()); return

    # --- ERROR FLOW ---
    if intent == "error_flow":
        if step in ["confirm_or_collect", "confirm_error"]:
            # если это новая формулировка — попробуем сразу подтвердить
            if looks_like_behavior(txt):
                msg, kb = confirm_error(uid, txt)
                bot.send_message(uid, msg, reply_markup=kb)
            else:
                # мягко подталкиваем к поведению
                bot.send_message(uid, "Попробуй сформулировать через действие (глагол): что точнее происходит?")
            return

        if step == "ask_context":
            data["context"] = txt
            set_state(uid, step="ask_emotions", data=data)
            bot.send_message(uid, "2) Что ты чувствуешь в этот момент? (несколько слов)")
            return
        if step == "ask_emotions":
            data["emotions"] = txt
            set_state(uid, step="ask_thoughts", data=data)
            bot.send_message(uid, "3) Какие мысли приходят? (1–2 фразы)")
            return
        if step == "ask_thoughts":
            data["thoughts"] = txt
            set_state(uid, step="ask_behavior", data=data)
            bot.send_message(uid, "4) Что ты конкретно делаешь? (глаголами, 1–2 предложения)")
            return
        if step == "ask_behavior":
            data["behavior"] = txt
            # резюме
            err = data.get("error_text") or summarize_problem(
                f"{data.get('context','')}. {data.get('emotions','')}. {data.get('thoughts','')}. {txt}"
            )
            data["error_text"] = err
            set_state(uid, step="goal", data=data)
            bot.send_message(uid, f"Вижу картину.\n<b>Формулировка:</b> {err}\nСформулируем новую цель одним предложением — что будешь делать вместо прежнего поведения?")
            return
        if step == "goal":
            data["goal"] = txt
            set_state(uid, step="ops", data=data)
            bot.send_message(uid, "Какие 2–3 шага помогут держаться этой цели в ближайших 3 сделках?")
            return
        if step == "ops":
            data["ops"] = txt
            set_state(uid, step="done", data=data)
            bot.send_message(uid, "Принято. Сохранил. Готово ✅\nЕсли хочешь — добавим это в недельный фокус позже.")
            return

    # --- STRATEGY FLOW (краткий каркас) ---
    if intent == "strategy_flow":
        if step == "start":
            set_state(uid, step="markets")
            bot.send_message(uid, "Шаг 1. Рынок/ТФ: что торгуешь и на каких ТФ?")
            return
        # (дальше аналогично наращивай шаги при необходимости)
    
    # --- FREE DIALOG / OFFSCRIPT ---
    # если не в активном шаге — живой ответ и мягкая подсветка
    if OFFSCRIPT:
        reply = route_free(uid, txt)
        if reply:
            bot.send_message(uid, reply, reply_markup=main_menu())
    else:
        bot.send_message(uid, "Принял. Чтобы было быстрее, выбери пункт в меню ниже или напиши /menu.", reply_markup=main_menu())

# === FLASK (webhook) ===
MAX_BODY = 1_000_000

@app.get("/health")
def health():
    return jsonify({"status":"ok","time": datetime.utcnow().isoformat()})

@app.get("/status")
def http_status():
    return jsonify({"ok":True,"version":APP_VERSION,"time": datetime.utcnow().isoformat()})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_SECRET:
        abort(401)
    if request.content_length and request.content_length > MAX_BODY:
        abort(413)
    update = request.get_data().decode("utf-8")
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
