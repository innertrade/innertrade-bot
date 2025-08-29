import os, json, time, logging
from datetime import datetime, timezone
from flask import Flask, request, abort, jsonify
import telebot
from telebot import types

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

# ---------- CONFIG ----------
APP_VERSION   = os.getenv("APP_VERSION", "chat-first-2025-08-29")
PUBLIC_URL    = os.getenv("PUBLIC_URL", "")
DB_URL        = os.getenv("DATABASE_URL", "")
TG_TOKEN      = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_PATH  = os.getenv("WEBHOOK_PATH", "webhook")
WEBHOOK_SECRET= os.getenv("TG_WEBHOOK_SECRET", "")
OFFSCRIPT     = os.getenv("OFFSCRIPT_ENABLED", "true").lower() in ("1","true","yes","on")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")

if not PUBLIC_URL:   raise RuntimeError("PUBLIC_URL missing")
if not TG_TOKEN:     raise RuntimeError("TELEGRAM_TOKEN missing")
if not DB_URL:       raise RuntimeError("DATABASE_URL missing")
if not WEBHOOK_PATH: raise RuntimeError("WEBHOOK_PATH missing")
if not WEBHOOK_SECRET: logging.warning("TG_WEBHOOK_SECRET is empty — webhook auth disabled")

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))

# ---------- DB ----------
engine = create_engine(DB_URL, poolclass=NullPool, future=True)

DDL = """
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
"""
with engine.begin() as conn:
    conn.exec_driver_sql(DDL)

def ensure_user(uid:int):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO users(user_id) VALUES(:uid)
            ON CONFLICT (user_id) DO UPDATE SET updated_at = now();
        """), {"uid": uid})

def save_state(uid:int, intent:str|None, step:str|None, data:dict|None):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO user_state (user_id, intent, step, data, updated_at)
            VALUES (:uid, COALESCE(:intent,'greet'), COALESCE(:step,'ask_form'),
                    COALESCE(CAST(:data AS jsonb), '{}'::jsonb), now())
            ON CONFLICT (user_id) DO UPDATE
            SET intent = COALESCE(EXCLUDED.intent, user_state.intent),
                step   = COALESCE(EXCLUDED.step,   user_state.step),
                data   = COALESCE(EXCLUDED.data,   user_state.data, '{}'::jsonb),
                updated_at = now();
        """), {"uid": uid, "intent": intent, "step": step, "data": json.dumps(data) if isinstance(data, dict) else data})

def load_state(uid:int):
    with engine.begin() as conn:
        row = conn.execute(text("SELECT intent, step, data FROM user_state WHERE user_id=:uid"), {"uid": uid}).first()
        if not row: return {"intent":"greet","step":"ask_form","data":{}}
        return {"intent": row[0] or "greet", "step": row[1] or "ask_form", "data": row[2] or {}}

# ---------- AI (optional) ----------
def ai_reply(history: list[dict], fallback:str) -> str:
    """
    history: [{"role":"system"/"user"/"assistant","content":"..."}]
    Returns assistant text. If OpenAI disabled or fails — fallback.
    """
    if not (OFFSCRIPT and OPENAI_KEY):
        return fallback
    try:
        # Lazy import to avoid hard dependency if no key
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_KEY)
        sys = {
            "role": "system",
            "content": (
                "Ты — тёплый коуч по трейдингу. Общайся естественно, коротко, без названий методик, "
                "но внутренне держи структуру. Не цитируй пользователя дословно. "
                "Главная цель: мягко вывести проблему на уровень поведения/навыка, согласовать формулировку, "
                "и только после согласования предложить разобрать шагами (без названий)."
            )
        }
        msgs = [sys] + history
        # Use chat.completions for compatibility
        res = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL","gpt-4o-mini"),
            messages=msgs,
            temperature=0.4,
            max_tokens=300
        )
        return (res.choices[0].message.content or "").strip() or fallback
    except Exception as e:
        logging.warning(f"AI fallback due to: {e}")
        return fallback

# ---------- BOT ----------
bot = telebot.TeleBot(TG_TOKEN, parse_mode="HTML")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно", "🤔 Не знаю, с чего начать")
    return kb

def gently_paraphrase(text_in:str) -> str:
    t = (text_in or "").strip()
    if not t: return "Понял, нужно уточнить проблему."
    # примитивная мягкая перефраза (без буквального повторения)
    lead = t.split(".")[0][:120]
    return f"Понимаю: это про срывы дисциплины вокруг сделок («{lead}…»). Верно уловил?"

# ---------- FLASK ----------
app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify({"status":"ok","time": datetime.now(timezone.utc).isoformat()})

@app.get("/status")
def status():
    return jsonify({"ok": True,
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "version": APP_VERSION})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    try:
        update = telebot.types.Update.de_json(request.get_data(as_text=True))
        bot.process_new_updates([update])
    except Exception as e:
        logging.exception("Webhook error")
        abort(500)
    return "OK", 200

# ---------- COMMANDS ----------
@bot.message_handler(commands=["ping"])
def cmd_ping(m: telebot.types.Message):
    bot.reply_to(m, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m: telebot.types.Message):
    st = load_state(m.from_user.id)
    bot.reply_to(m, f"<code>{json.dumps({'ok':True,'time': datetime.utcnow().isoformat(timespec='seconds'),'intent': st['intent'],'step': st['step'],'db':'ok'}, ensure_ascii=False, indent=2)}</code>")

@bot.message_handler(commands=["reset","start"])
def cmd_reset(m: telebot.types.Message):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, "greet", "ask_address", {"address": None})
    greet = f"👋 Привет, {m.from_user.first_name or 'друг'}!\nМожем просто поговорить — напиши, что болит в торговле.\n\nКак удобнее обращаться — <b>ты</b> или <b>вы</b>? (напиши одно слово)"
    bot.send_message(uid, greet, reply_markup=main_menu())

# ---------- GENERAL TEXT ----------
@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(m: telebot.types.Message):
    uid = m.from_user.id
    ensure_user(uid)
    st = load_state(uid)
    txt = (m.text or "").strip()

    # быстрые кнопки:
    if txt == "🚑 У меня ошибка":
        save_state(uid, "error", "ask_error", st["data"])
        bot.send_message(uid, "Опиши, пожалуйста, основную ошибку (1–2 предложения).", reply_markup=main_menu())
        return
    if txt == "🧩 Хочу стратегию":
        save_state(uid, "strategy", "intro", st["data"])
        bot.send_message(uid, "Соберём по шагам: рынок/ТФ → вход → стоп/сопровождение → риск. Поехали?", reply_markup=main_menu())
        return
    if txt == "📄 Паспорт":
        save_state(uid, "passport", "q1", st["data"])
        bot.send_message(uid, "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?", reply_markup=main_menu())
        return
    if txt == "🗒 Панель недели":
        save_state(uid, "week_panel", "start", st["data"])
        bot.send_message(uid, "Панель недели: фокус, 1–2 цели, лимиты, ритуалы. Готов выбрать фокус?", reply_markup=main_menu())
        return
    if txt == "🆘 Экстренно":
        save_state(uid, "sos", "stop_protocol", st["data"])
        bot.send_message(uid, "Стоп-протокол: 1) Пауза 2 мин 2) Убери график 3) 10 вдохов 4) Назови триггер 5) Вернись к плану или закрой по правилу.", reply_markup=main_menu())
        return
    if txt == "🤔 Не знаю, с чего начать":
        save_state(uid, "help", "route", st["data"])
        bot.send_message(uid, "Предлагаю так: 1) Паспорт 2) Фокус недели 3) Скелет ТС. С чего начнём?", reply_markup=main_menu())
        return

    # настройка обращения
    if st["intent"] == "greet" and st["step"] in ("ask_address","ask_form"):
        t = txt.lower()
        if t in ("ты","вы"):
            st["data"]["address"] = t
            save_state(uid, "greet", "free_talk", st["data"])
            reply = "Принято. Можем просто поговорить — расскажи, что сейчас болит, или выбери пункт ниже."
            bot.send_message(uid, reply, reply_markup=main_menu())
            return
        else:
            # оффскрипт: позволяем поговорить свободно перед формальным выбором
            fallback = "Понял. Напиши, пожалуйста, одно слово: «ты» или «вы» — так будет проще общаться."
            out = ai_reply([{"role":"user","content":txt}], fallback)
            bot.send_message(uid, out, reply_markup=main_menu())
            return

    # естественное общение перед структурой
    if st["intent"] in ("greet","help") and st["step"] in ("free_talk","route","ask_address","ask_form"):
        # Пытаемся мягко выявить проблему на уровне поведения, без цитирования
        paraphrase = gently_paraphrase(txt)
        # Если пользователь сам явно просит разбор — перейдём сразу
        if any(k in txt.lower() for k in ("ошибка","просадк","наруша","стоп","усредн", "не знаю что делать")):
            save_state(uid, "error", "confirm_problem", {"candidate": txt})
            bot.send_message(uid, f"{paraphrase}\n\nЕсли верно — скажи «да», и разберём по шагам. Если нет — поправь одним предложением.", reply_markup=main_menu())
            return
        # Иначе поддержим диалог оффскриптом
        fallback = "Понимаю. Расскажи ещё немного — в чём именно сложность сейчас?"
        out = ai_reply([{"role":"user","content":txt}], fallback)
        bot.send_message(uid, out, reply_markup=main_menu())
        return

    # Ветка разбора ошибки: согласование формулировки → вопросы
    if st["intent"] == "error":
        step = st["step"]
        data = st["data"] or {}

        if step == "ask_error":
            # пользователь выдал исходное описание
            data["raw_error"] = txt
            save_state(uid, "error", "confirm_problem", data)
            paraphrase = gently_paraphrase(txt)
            bot.send_message(uid, f"{paraphrase}\n\nЕсли верно — скажи «да». Если нет — поправь одним предложением.")
            return

        if step == "confirm_problem":
            if txt.strip().lower() in ("да","ок","верно","угу","ага","правильно"):
                save_state(uid, "error", "mer_context", data)
                bot.send_message(uid, "Ок. В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)")
                return
            # пользователь корректирует формулировку
            data["raw_error"] = txt
            save_state(uid, "error", "confirm_problem", data)
            paraphrase = gently_paraphrase(txt)
            bot.send_message(uid, f"{paraphrase}\n\nВерно? Скажи «да» или поправь коротко.")
            return

        if step == "mer_context":
            data["context"] = txt
            save_state(uid, "error", "mer_emotions", data)
            bot.send_message(uid, "Что чувствуешь в этот момент? (несколько слов)")
            return

        if step == "mer_emotions":
            data["emotions"] = txt
            save_state(uid, "error", "mer_thoughts", data)
            bot.send_message(uid, "Какие мысли/фразы крутятся в голове в этот момент? (1–2 коротких)")
            return

        if step == "mer_thoughts":
            data["thoughts"] = txt
            save_state(uid, "error", "mer_behavior", data)
            bot.send_message(uid, "Что конкретно делаешь? Опиши действия глаголами (1–2 предложения).")
            return

        if step == "mer_behavior":
            data["behavior"] = txt
            # Резюме без дословных цитат
            save_state(uid, "error", "new_goal", data)
            bot.send_message(uid, "Ок, вижу картину. Сформулируй новую цель одним предложением — что хочешь делать вместо прежнего поведения?")
            return

        if step == "new_goal":
            data["new_goal"] = txt
            save_state(uid, "error", "tote_ops", data)
            bot.send_message(uid, "Назови 2–3 шага, которые помогут держаться этой цели в ближайших 3 сделках.")
            return

        if step == "tote_ops":
            data["tote_ops"] = txt
            save_state(uid, "error", "tote_check", data)
            bot.send_message(uid, "Как поймёшь, что получилось? Один простой критерий проверки.")
            return

        if step == "tote_check":
            data["tote_check"] = txt
            save_state(uid, "error", "tote_exit", data)
            bot.send_message(uid, "И последнее: что сделаешь, если в проверке выйдет «не получилось»? (1 действие)")
            return

        if step == "tote_exit":
            data["tote_exit"] = txt
            # финал
            save_state(uid, "error", "done", data)
            bot.send_message(uid, "Готово. Я сохранил разбор. Можем добавить это в фокус недели или перейти к следующей задаче.", reply_markup=main_menu())
            return

    # Если ничего не совпало — мягкий оффскрипт
    fallback = "Понял. Можем поговорить подробнее или перейти к разбору — нажми «🚑 У меня ошибка»."
    out = ai_reply([{"role":"user","content":txt}], fallback)
    bot.send_message(uid, out, reply_markup=main_menu())

# ---------- LOCAL DEV (optional) ----------
if __name__ == "__main__":
    # Для локального запуска (Render запускает просто python main.py тоже)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
