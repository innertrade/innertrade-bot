import os
import json
import logging
from datetime import datetime
from typing import Optional

from flask import Flask, request, jsonify, abort
from telebot import TeleBot, types
from telebot.types import Update
from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

# ----------------- ЛОГИ -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("innertrade")

# ----------------- ENV ------------------
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")
DATABASE_URL       = os.getenv("DATABASE_URL")
PUBLIC_URL         = os.getenv("PUBLIC_URL")  # https://innertrade-bot.onrender.com
WEBHOOK_PATH       = os.getenv("WEBHOOK_PATH", "wbhk_9t3x")
TG_WEBHOOK_SECRET  = os.getenv("TG_WEBHOOK_SECRET")
MODE               = os.getenv("MODE", "webhook")  # webhook | polling

if not TELEGRAM_TOKEN: raise RuntimeError("TELEGRAM_TOKEN missing")
if not OPENAI_API_KEY: raise RuntimeError("OPENAI_API_KEY missing")
if not PUBLIC_URL:     raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")

# ----------------- OPENAI ---------------
client = OpenAI(api_key=OPENAI_API_KEY)

def coach_llm(prompt: str) -> str:
    """
    Короткая «мягкая» поддержка вне сценария.
    Безопасный короткий ответ, 1–2 фразы, на ТЫ.
    """
    try:
        msg = [
            {"role": "system", "content":
             "Ты — спокойный коуч по трейдингу. Пиши коротко, дружелюбно, на «ты». "
             "Если собеседник спрашивает не по сценарию, поддержи и мягко предложи следующий шаг."},
            {"role": "user", "content": prompt}
        ]
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=msg,
            temperature=0.5,
            max_tokens=120
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"LLM error: {e}")
        return "Понимаю. Расскажи ещё чуть-чуть — я помогу это собрать в конкретику."

# ----------------- DB -------------------
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        with engine.begin() as conn:
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
              user_id BIGINT PRIMARY KEY,
              mode TEXT NOT NULL DEFAULT 'course',
              created_at TIMESTAMPTZ DEFAULT now(),
              updated_at TIMESTAMPTZ DEFAULT now()
            );
            """))
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_state (
              user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
              intent  TEXT,
              step    TEXT,
              data    JSONB,
              updated_at TIMESTAMPTZ DEFAULT now()
            );
            """))
        log.info("DB connected & basic tables exist")
    except OperationalError as e:
        log.warning(f"DB not available yet: {e}")
        engine = None
else:
    log.info("DATABASE_URL not set — working without DB")

def db_exec(sql: str, params: dict | None = None):
    if not engine: return
    with engine.begin() as conn:
        conn.execute(text(sql), params or {})

def db_fetchone(sql: str, params: dict | None = None) -> Optional[dict]:
    if not engine: return None
    with engine.begin() as conn:
        row = conn.execute(text(sql), params or {}).mappings().first()
        return dict(row) if row else None

def ensure_user(uid: int):
    if not engine: return
    db_exec("INSERT INTO users(user_id) VALUES (:u) ON CONFLICT DO NOTHING", {"u": uid})

def save_state(uid: int, intent: str, step: Optional[str] = None, data: Optional[dict] = None):
    if not engine: return
    ensure_user(uid)
    db_exec("""
      INSERT INTO user_state(user_id, intent, step, data)
      VALUES (:u, :i, :s, COALESCE(:d, '{}'::jsonb))
      ON CONFLICT (user_id) DO UPDATE
      SET intent = EXCLUDED.intent,
          step   = EXCLUDED.step,
          data   = COALESCE(EXCLUDED.data, user_state.data),
          updated_at = now()
    """, {"u": uid, "i": intent, "s": step, "d": json.dumps(data or {})})

def load_state(uid: int) -> dict:
    row = db_fetchone("SELECT intent, step, data FROM user_state WHERE user_id = :u", {"u": uid}) or {}
    return {
        "intent": row.get("intent"),
        "step": row.get("step"),
        "data": (row.get("data") or {}) if isinstance(row.get("data"), dict) else {}
    }

# ----------------- TELEGRAM -------------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

WELCOME = (
    "👋 Привет! Я наставник *Innertrade*.\n"
    "Можем просто поговорить — или пойдём по шагам.\n"
    "Команды: /status /ping"
)

# ------------ ВСПОМОГАТЕЛЬНОЕ NLU -------
def is_problem_text(t: str) -> bool:
    t = t.lower()
    keys = ["ошиб", "слива", "просад", "теря", "вхожу", "выхожу", "двигаю стоп", "сетап"]
    return any(k in t for k in keys)

def is_fix_or_average(t: str) -> bool:
    t = t.lower()
    return ("усред" in t) or ("фиксир" in t and "убыт" in t)

def vague_phrase(t: str) -> bool:
    t = t.lower()
    return any(k in t for k in ["определённые дни", "иногда", "бывает", "часто", "когда как"])

def summarize_error_for_user(data: dict) -> str:
    err = data.get("error_text") or "—"
    ctx = data.get("ctx") or "—"
    beh = data.get("beh") or "—"
    emo = data.get("emo") or "—"
    th  = data.get("th")  or "—"
    return (
        f"Так я понял твою ситуацию:\n\n"
        f"• Ошибка: *{err}*\n"
        f"• Контекст: {ctx}\n"
        f"• Поведение: {beh}\n"
        f"• Эмоции/ощущения: {emo}\n"
        f"• Мысли: {th}\n\n"
        f"Ок ли такое резюме? Если да — пойдём к позитивной цели (TOTE). Если нет — поправь меня в одном-двух предложениях."
    )

# ------------- ПОТОК: СВОБОДНЫЙ ЧАТ -----
# Дадим выговориться 2–3 реплики, затем предложим конкретику/подтверждение
FREE_TURNS = {}

def freeflow_next(uid: int) -> int:
    FREE_TURNS[uid] = FREE_TURNS.get(uid, 0) + 1
    return FREE_TURNS[uid]

def freeflow_reset(uid: int):
    FREE_TURNS[uid] = 0

# ------------- ПОТОК: MERCEDES ----------
M_STEPS = ["error", "ctx", "emo", "th", "beh", "val", "state", "confirm"]

def ask_next_mercedes(uid: int, chat_id: int, data: dict):
    step = data.get("m_step") or "error"

    if step == "error":
        bot.send_message(chat_id,
            "Опиши основную ошибку 1–2 предложениями *на уровне поведения/навыка*.\n"
            "Примеры: «вхожу до формирования сигнала», «двигаю стоп», «закрываю по первой коррекции».",
            reply_markup=main_menu())
        return
    if step == "ctx":
        bot.send_message(chat_id, "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)")
        return
    if step == "emo":
        bot.send_message(chat_id, "ЭМОЦИИ. Что чувствуешь в момент ошибки? Как в теле? (несколько слов)")
        return
    if step == "th":
        bot.send_message(chat_id, "МЫСЛИ. Что говоришь себе в этот момент? (1–2 короткие цитаты)")
        return
    if step == "beh":
        bot.send_message(chat_id, "ПОВЕДЕНИЕ. Что именно делаешь? Опиши действия глаголами (1–2 предложения).")
        return
    if step == "val":
        bot.send_message(chat_id, "УБЕЖДЕНИЯ/ЦЕННОСТИ. Почему *кажется*, что надо именно так? (1–2 мысли)")
        return
    if step == "state":
        bot.send_message(chat_id, "БАЗОВОЕ СОСТОЯНИЕ. В каком состоянии ты обычно входишь? (тревога, спешка, контроль и т.п.)")
        return
    if step == "confirm":
        bot.send_message(chat_id, summarize_error_for_user(data))
        return

def mercedes_store_and_advance(uid: int, chat_id: int, text_answer: str):
    st = load_state(uid)
    d  = st.get("data", {}) or {}
    step = d.get("m_step") or "error"

    # Если ответ расплывчатый — попросим конкретику
    if vague_phrase(text_answer) and step in {"error","ctx"}:
        bot.send_message(chat_id, "Чуть конкретнее, пожалуйста: *какие именно дни/события*? 1–2 примера.")
        return

    # Сохраняем по шагам
    if step == "error":
        d["error_text"] = text_answer.strip()
        d["m_step"] = "ctx"
    elif step == "ctx":
        d["ctx"] = text_answer.strip()
        d["m_step"] = "emo"
    elif step == "emo":
        d["emo"] = text_answer.strip()
        d["m_step"] = "th"
    elif step == "th":
        d["th"] = text_answer.strip()
        d["m_step"] = "beh"
    elif step == "beh":
        d["beh"] = text_answer.strip()
        d["m_step"] = "val"
    elif step == "val":
        d["val"] = text_answer.strip()
        d["m_step"] = "state"
    elif step == "state":
        d["state"] = text_answer.strip()
        d["m_step"] = "confirm"
    elif step == "confirm":
        # Пользователь поправил резюме — принимаем правку как уточнение error_text
        d["error_text"] = text_answer.strip()
        d["m_step"] = "confirm"

    save_state(uid, intent="mercedes", step=d["m_step"], data=d)
    ask_next_mercedes(uid, chat_id, d)

# ------------- ПОТОК: TOTE --------------
def start_tote(uid: int, chat_id: int):
    st = load_state(uid)
    d  = st.get("data", {}) or {}
    d["t_step"] = "t1"
    save_state(uid, intent="tote", step="t1", data=d)
    bot.send_message(chat_id,
        "TOTE — цель и проверка.\n\n"
        "*T1 — Цель (будущее поведение)*: сформулируй в 1 предложении.\n"
        "Пример: «В 3 ближайших сделках не двигаю стоп/тейк после входа».")
def tote_store_and_advance(uid: int, chat_id: int, text_answer: str):
    st = load_state(uid); d = st.get("data", {}) or {}; step = d.get("t_step") or "t1"
    if step == "t1":
        d["t_goal"] = text_answer.strip()
        d["t_step"] = "o"
        bot.send_message(chat_id, "*O — Операции (шаги)*: перечисли 2–4 действия, которые помогут держать цель (чек-лист, таймер, пауза).")
    elif step == "o":
        d["t_ops"] = text_answer.strip()
        d["t_step"] = "t2"
        bot.send_message(chat_id, "*T2 — Проверка*: как поймёшь, что соблюдаешь цель? (критерий/счётчик)")
    elif step == "t2":
        d["t_check"] = text_answer.strip()
        d["t_step"] = "e"
        bot.send_message(chat_id, "*E — Выход*: если получилось — что закрепляем; если нет — к какому шагу вернёшься?")
    elif step == "e":
        d["t_exit"] = text_answer.strip()
        save_state(uid, intent="done_l1", step=None, data=d)
        bot.send_message(chat_id,
            "Готово! Мы сформировали цель и шаги. Хочешь добавить это в недельный фокус или перейти к архетипам?",
            reply_markup=main_menu())
        return
    save_state(uid, intent="tote", step=d["t_step"], data=d)

# ---- ПОТОК: Фиксировать vs Усреднять ---
def start_fix_vs_avg(uid: int, chat_id: int):
    st = load_state(uid); d = st.get("data", {}) or {}
    d["fv_step"] = "q1"
    save_state(uid, intent="fix_or_avg", step="q1", data=d)
    bot.send_message(chat_id,
        "Понял про просадку. Быстро пробежимся по 5 пунктам (да/нет/коротко):\n\n"
        "1) *Усреднение прописано* в твоей ТС (правила, лимит, условия входа)?")

def fix_vs_avg_store(uid: int, chat_id: int, txt: str):
    st = load_state(uid); d = st.get("data", {}) or {}; step = d.get("fv_step","q1")
    ans = txt.strip().lower()

    def next_q(s: str, q: str):
        d["fv_step"] = s
        save_state(uid, intent="fix_or_avg", step=s, data=d)
        bot.send_message(chat_id, q)

    if step == "q1":
        d["fv_has_rule"] = ans
        return next_q("q2", "2) Текущий риск *вписывается* в твои лимиты (риск/сделку, дневной/недельный)?")
    if step == "q2":
        d["fv_risk_ok"] = ans
        return next_q("q3", "3) Не нарушается *макс. просадка* по счёту?")
    if step == "q3":
        d["fv_dd_ok"] = ans
        return next_q("q4", "4) Есть *рыночные причины* усредняться (плановый уровень, сигнал, ликвидность), а не просто «страх»?")
    if step == "q4":
        d["fv_market_ok"] = ans
        return next_q("q5", "5) Если усреднишься и не пойдёт — *план выхода* понятен? (где стоп, что считаем ошибкой)")
    if step == "q5":
        d["fv_exit_plan"] = ans

        # Решение
        has_rule  = d.get("fv_has_rule","нет").startswith("д")
        risk_ok   = d.get("fv_risk_ok","нет").startswith("д")
        dd_ok     = d.get("fv_dd_ok","нет").startswith("д")
        market_ok = d.get("fv_market_ok","нет").startswith("д")
        exit_ok   = d.get("fv_exit_plan","нет").startswith("д")

        if has_rule and risk_ok and dd_ok and market_ok and exit_ok:
            msg = ("Судя по ответам, усреднение *в рамках ТС* допустимо.\n"
                   "👉 Действуем по плану: *малой долей*, по сигналу, стоп и лимиты — жёстко.")
        else:
            msg = ("Рекомендация — *фиксировать* (или сокращать позицию).\n"
                   "Причина: не выполнены условия безопасного усреднения (правила/риск/просадка/сигнал/план выхода).")

        save_state(uid, intent="fix_or_avg_done", step=None, data=d)
        bot.send_message(chat_id, msg, reply_markup=main_menu())
        return

# ------------- КНОПКИ / КОМАНДЫ ---------
@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m):
    freeflow_reset(m.from_user.id)
    save_state(m.from_user.id, intent="idle", step=None, data={"free":0})
    ensure_user(m.from_user.id)
    bot.send_message(m.chat.id, WELCOME, reply_markup=main_menu())

@bot.message_handler(commands=["ping"])
def cmd_ping(m): bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    st = load_state(m.from_user.id)
    bot.send_message(m.chat.id,
        f"Статус: бот живой ✅\n"
        f"intent: {st.get('intent')}\nstep: {st.get('step')}\nupdated: {datetime.utcnow().isoformat()}Z",
        reply_markup=main_menu())

# Кнопки меню
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def btn_error(m):
    freeflow_reset(m.from_user.id)
    d = {"m_step":"error"}
    save_state(m.from_user.id, intent="mercedes", step="error", data=d)
    ask_next_mercedes(m.from_user.id, m.chat.id, d)

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def btn_panic(m):
    save_state(m.from_user.id, intent="panic", step=None)
    bot.send_message(m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку с графиком\n3) 10 медленных вдохов\n"
        "4) Запиши триггер (что выбило)\n5) Вернись к плану сделки или закрой по правилу",
        reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def btn_strategy(m):
    save_state(m.from_user.id, intent="strategy", step=None)
    bot.send_message(m.chat.id,
        "Ок, собираем ТС по конструктору:\n1) Цели\n2) Стиль\n3) Рынки/инструменты\n4) Вход/Выход\n5) Риск\n6) Сопровождение\n7) Тестирование",
        reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def btn_passport(m):
    save_state(m.from_user.id, intent="passport", step=None)
    bot.send_message(m.chat.id, "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def btn_week(m):
    save_state(m.from_user.id, intent="week_panel", step=None)
    bot.send_message(m.chat.id,
        "Панель недели:\n• Фокус недели\n• План (3 шага)\n• Лимиты\n• Ритуалы\n• Короткая ретро в конце недели",
        reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def btn_start_help(m):
    save_state(m.from_user.id, intent="start_help", step=None)
    bot.send_message(m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\nС чего начнём?",
        reply_markup=main_menu())

# ------------- РОУТЕР ТЕКСТА ------------
@bot.message_handler(content_types=["text"])
def on_text(m):
    uid = m.from_user.id
    txt = (m.text or "").strip()

    st = load_state(uid)
    intent = st.get("intent")
    step   = st.get("step")
    data   = st.get("data") or {}

    # Если мы в MERCEDES-потоке
    if intent == "mercedes":
        return mercedes_store_and_advance(uid, m.chat.id, txt)

    # Подтверждение резюме → старт TOTE
    if intent == "mercedes" and step == "confirm":
        # (по логике mercedes_store_and_advance)
        pass

    # Если мы в TOTE
    if intent == "tote":
        return tote_store_and_advance(uid, m.chat.id, txt)

    # Если мы в fix vs average
    if intent == "fix_or_avg":
        return fix_vs_avg_store(uid, m.chat.id, txt)

    # Спец-детектор «фиксировать или усреднять»
    if is_fix_or_average(txt):
        start_fix_vs_avg(uid, m.chat.id)
        return

    # Детектор проблемного входа → MERCEDES
    if "🚑" in txt or "ошиб" in txt.lower():
        d = {"m_step":"error"}
        save_state(uid, intent="mercedes", step="error", data=d)
        ask_next_mercedes(uid, m.chat.id, d)
        return

    # Свободный чат (мягкая поддержка + done-условие)
    turns = freeflow_next(uid)
    if turns <= 2 and not is_problem_text(txt):
        # просто поддержим
        reply = coach_llm(txt)
        bot.send_message(m.chat.id, reply, reply_markup=main_menu())
        return

    # После пары реплик — предложим конкретику
    if is_problem_text(txt):
        # заякорим как ошибку и сразу спросим формулировку
        d = {"m_step":"error", "error_text": txt}
        save_state(uid, intent="mercedes", step="ctx", data=d)
        bot.send_message(m.chat.id, "Понял. Зафиксировал так: *{}*".format(txt))
        ask_next_mercedes(uid, m.chat.id, {"m_step":"ctx"})
        return

    # Иначе — короткая поддержка и лёгкий «возврат» к делу
    reply = coach_llm(txt) + "\n\nЕсли хочешь — напиши, *что именно* сейчас болит в трейдинге, и разберём по шагам."
    bot.send_message(m.chat.id, reply, reply_markup=main_menu())

# ------------- FLASK (WEBHOOK) ----------
app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify({"status":"ok","time": datetime.utcnow().isoformat()+"Z"})

# Webhook endpoint
@app.post(f"/{WEBHOOK_PATH}")
def tg_webhook():
    if TG_WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
            abort(401)
    # ограничение размера тела
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    try:
        upd = Update.de_json(request.get_data(as_text=True))
        bot.process_new_updates([upd])
    except Exception as e:
        log.exception(f"process update error: {e}")
    return "ok"

# ------------- ENTRYPOINT ----------------
def start_polling():
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log.info("Starting polling…")
    bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)

if __name__ == "__main__":
    if MODE == "polling":
        import threading
        t = threading.Thread(target=start_polling, daemon=True)
        t.start()
    port = int(os.getenv("PORT", "10000"))
    log.info(f"Web server on :{port}, mode={MODE}, webhook=/{WEBHOOK_PATH}")
    app.run(host="0.0.0.0", port=port)
