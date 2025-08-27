# main.py
import os
import json
import time
import logging
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify, abort
from telebot import TeleBot, types
from telebot.types import Update

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError, OperationalError

# ============ ЛОГИ ============
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("innertrade")

STARTED_AT = time.time()

# ============ ENV ============
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")  # опционально; без него будет «ручной» коучинг
DATABASE_URL       = os.getenv("DATABASE_URL")
PUBLIC_URL         = os.getenv("PUBLIC_URL")      # например, https://innertrade-bot.onrender.com
WEBHOOK_PATH       = os.getenv("WEBHOOK_PATH")    # например, "wbhk_abcd123"
TG_WEBHOOK_SECRET  = os.getenv("TG_WEBHOOK_SECRET")  # секрет заголовка X-Telegram-Bot-Api-Secret-Token

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if not WEBHOOK_PATH:
    raise RuntimeError("WEBHOOK_PATH missing (e.g., wbhk_xxx)")
if not TG_WEBHOOK_SECRET:
    raise RuntimeError("TG_WEBHOOK_SECRET missing")

# ============ OPENAI (опционально) ============
# Мягкая обёртка — если ключа нет/ошибка, работаем без LLM.
try:
    from openai import OpenAI
    oa_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception as e:
    log.warning(f"OpenAI SDK not available: {e}")
    oa_client = None

def coach_reply(prompt: str, sys_hint: str) -> Optional[str]:
    """
    Короткий, эмпатичный ответ. Если OpenAI недоступен — вернём None.
    """
    if not oa_client:
        return None
    try:
        # компактная подсказка, без лишних токенов
        resp = oa_client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system", "content": sys_hint},
                {"role": "user", "content": prompt},
            ],
            max_output_tokens=180,
            temperature=0.5,
        )
        txt = resp.output_text.strip()
        return txt[:800]
    except Exception as e:
        log.warning(f"LLM fallback: {e}")
        return None

# ============ БД ============
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        with engine.begin() as conn:
            # Только необходимые таблицы; без «DROP» — безопасная инициализация.
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
              user_id   BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
              intent    TEXT,
              step      TEXT,
              data      JSONB,
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
        log.info("DB connected & tables ready")
    except OperationalError as e:
        log.warning(f"DB not available yet: {e}")
        engine = None
else:
    log.info("DATABASE_URL not set — running without DB")

def db_exec(sql: str, params: Optional[dict] = None):
    if not engine:
        return None
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})

def upsert_user(uid: int):
    if not engine: return
    db_exec("INSERT INTO users(user_id) VALUES (:uid) ON CONFLICT (user_id) DO NOTHING", {"uid": uid})

def save_state(uid: int, intent: str, step: Optional[str] = None, data: Optional[dict] = None):
    if not engine: return
    upsert_user(uid)
    db_exec("""
    INSERT INTO user_state(user_id, intent, step, data)
    VALUES (:uid, :intent, :step, CAST(:data AS JSONB))
    ON CONFLICT (user_id) DO UPDATE
    SET intent = EXCLUDED.intent,
        step   = EXCLUDED.step,
        data   = EXCLUDED.data,
        updated_at = now()
    """, {"uid": uid, "intent": intent, "step": step, "data": json.dumps(data or {})})

def load_state(uid: int) -> dict:
    if not engine:
        return {}
    res = db_exec("SELECT intent, step, data FROM user_state WHERE user_id = :uid", {"uid": uid})
    row = res.fetchone() if res else None
    if not row:
        return {}
    return {"intent": row[0], "step": row[1], "data": row[2] or {}}

def insert_error_record(uid: int, payload: dict):
    if not engine: return
    fields = {
        "user_id": uid,
        "error_text": payload.get("error_text", ""),
        "pattern_behavior": payload.get("pattern_behavior"),
        "pattern_emotion": payload.get("pattern_emotion"),
        "pattern_thought": payload.get("pattern_thought"),
        "positive_goal": payload.get("positive_goal"),
        "tote_goal": payload.get("tote_goal"),
        "tote_ops": payload.get("tote_ops"),
        "tote_check": payload.get("tote_check"),
        "tote_exit": payload.get("tote_exit"),
        "checklist_pre": payload.get("checklist_pre"),
        "checklist_post": payload.get("checklist_post"),
    }
    placeholders = ", ".join(fields.keys())
    values = ", ".join([f":{k}" for k in fields.keys()])
    sql = f"INSERT INTO errors({placeholders}) VALUES ({values})"
    db_exec(sql, fields)

# ============ БОТ ============
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

WELCOME = "👋 Привет! Я наставник *Innertrade*.\nВыбери пункт или напиши текст.\nКоманды: /status /ping"

# ====== МЯГКИЙ «ПРЕ-ШАГ» ПЕРЕД MERCEDES ======
@dataclass
class ProbeState:
    """храним временно до MERCEDES"""
    raw_issue: Optional[str] = None           # как сформулировал пользователь
    probes: int = 0                           # сколько уточняющих уже было
    summary_ready: bool = False               # бот сформулировал гипотезу и спросил «Ок?»
    agreed: bool = False                      # пользователь согласился
    # «слоты» для мягкой конкретизации
    when: Optional[str] = None                # когда это случается / в какие дни/условия
    behavior: Optional[str] = None            # что конкретно делает (глаголы)
    feelings: Optional[str] = None            # эмоции/ощущения
    thoughts: Optional[str] = None            # мысли/саморазговор

    def to_dict(self): return asdict(self)

def get_probe(state: dict) -> ProbeState:
    return ProbeState(**(state.get("probe") or {}))

def set_probe(uid: int, p: ProbeState, intent="error", step="probe"):
    data = load_state(uid).get("data", {})
    data["probe"] = p.to_dict()
    save_state(uid, intent=intent, step=step, data=data)

def clear_probe(uid: int):
    data = load_state(uid).get("data", {})
    data.pop("probe", None)
    save_state(uid, intent="error", step="start", data=data)

def propose_summary(p: ProbeState) -> str:
    parts = []
    if p.raw_issue: parts.append(p.raw_issue.strip())
    if p.when: parts.append(f"особенно часто — {p.when.strip()}")
    if p.behavior: parts.append(f"действие: {p.behavior.strip()}")
    if p.feelings: parts.append(f"эмоции: {p.feelings.strip()}")
    if p.thoughts: parts.append(f"мысли: {p.thoughts.strip()}")
    text = "; ".join(parts)
    if not text:
        text = "Ошибка сформулирована не до конца."
    return f"Так сформулирую проблему: *{text}*\n\nПодходит? Если да — напиши «да». Если нужно поправить — напиши, что поменять."

# ====== ХЕНДЛЕРЫ ======
@bot.message_handler(commands=["start", "menu", "reset"])
def cmd_start(m):
    upsert_user(m.from_user.id)
    save_state(m.from_user.id, intent="idle", step=None, data={})
    bot.send_message(m.chat.id, WELCOME, reply_markup=main_menu())

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    uptime = int(time.time() - STARTED_AT)
    mins = uptime // 60
    db_ok = False
    try:
        if engine:
            db_exec("SELECT 1")
            db_ok = True
    except SQLAlchemyError:
        db_ok = False
    bot.send_message(
        m.chat.id,
        f"✅ Бот жив.\nUptime: {mins} мин\nБД: {'ok' if db_ok else 'нет'}\nWebhook: {PUBLIC_URL}/***",
        reply_markup=main_menu()
    )

# ----- Кнопки-интенты -----
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def intent_error_btn(m):
    # старт «мягкого раппорта»
    p = ProbeState()
    set_probe(m.from_user.id, p, intent="error", step="probe")
    bot.send_message(
        m.chat.id,
        "Опиши основную ошибку 1–2 предложениями на уровне *поведения/навыка*.\n"
        "Примеры: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю по первой коррекции».",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def intent_strategy_btn(m):
    save_state(m.from_user.id, intent="strategy", step="start", data={})
    bot.send_message(
        m.chat.id,
        "Ок, собираем ТС по конструктору:\n"
        "1) Цели\n2) Стиль (дневной/свинг/позиционный)\n3) Рынки/инструменты\n"
        "4) Правила входа/выхода\n5) Риск (%, стоп)\n6) Сопровождение\n7) Тестирование",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def intent_passport_btn(m):
    save_state(m.from_user.id, intent="passport", step="start", data={})
    bot.send_message(
        m.chat.id,
        "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def intent_week_panel_btn(m):
    save_state(m.from_user.id, intent="week_panel", step="start", data={})
    bot.send_message(
        m.chat.id,
        "Панель недели:\n• Фокус недели\n• План (3 шага)\n• Лимиты\n• Ритуалы\n• Короткая ретро в конце недели",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def intent_panic_btn(m):
    save_state(m.from_user.id, intent="panic", step="start", data={})
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку с графиком\n3) 10 медленных вдохов\n"
        "4) Запиши триггер (что выбило)\n5) Вернись к плану сделки или закрой позицию по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def intent_start_help_btn(m):
    save_state(m.from_user.id, intent="start_help", step="start", data={})
    bot.send_message(
        m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\n"
        "С чего начнём — паспорт или фокус недели?",
        reply_markup=main_menu()
    )

# ====== СЦЕНАРИЙ: «МЯГКАЯ КОНКРЕТИЗАЦИЯ» → MERCEDES ======
def handle_probe(uid: int, chat_id: int, text_msg: str) -> bool:
    """
    Возвращает True, если сообщение обработано как пролог ошибки (до MERCEDES).
    """
    state = load_state(uid)
    if state.get("intent") != "error" or state.get("step") not in (None, "probe", "start"):
        return False

    p = get_probe(state)

    # 1) если ещё нет исходной формулировки — берём её
    if not p.raw_issue:
        p.raw_issue = text_msg.strip()
        p.probes = 1
        set_probe(uid, p)
        # Первый мягкий уточняющий: «когда/в какие дни/в каких условиях?»
        bot.send_message(
            chat_id,
            "Понял. *Когда* это чаще случается? (например: «в дни без сетапов», «после убытков», «на сильных новостях»)"
        )
        return True

    # 2) собираем уточнения
    low = text_msg.lower().strip()
    if any(x in low for x in ["когда", "день", "дни", "бывает", "часто", "обычно"]) and not p.when:
        p.when = text_msg.strip()
        p.probes += 1
        set_probe(uid, p)
        bot.send_message(chat_id, "А что *конкретно делаешь* в этот момент? (глаголами: «вхожу раньше», «двигаю стоп», «фиксируюсь по первой коррекции»)")
        return True

    # эвристика: если поведение ещё пусто — примем нынешний ответ за поведение
    if not p.behavior:
        p.behavior = text_msg.strip()
        p.probes += 1
        set_probe(uid, p)
        bot.send_message(chat_id, "Какие *эмоции/ощущения* в момент ошибки? (несколько слов)")
        return True

    # если эмоции пусты — примем нынешний ответ за эмоции
    if not p.feelings:
        p.feelings = text_msg.strip()
        p.probes += 1
        set_probe(uid, p)
        bot.send_message(chat_id, "Что говоришь себе в этот момент? 1–2 короткие мысли/фразы.")
        return True

    # если мысли пусты — примем нынешний ответ за мысли
    if not p.thoughts:
        p.thoughts = text_msg.strip()
        p.probes += 1
        set_probe(uid, p)

    # 3) после 2–3 уточнений — сформулировать гипотезу и спросить согласие
    if p.probes >= 2 and not p.summary_ready:
        p.summary_ready = True
        set_probe(uid, p)
        bot.send_message(chat_id, propose_summary(p))
        return True

    # 4) согласование
    if p.summary_ready and not p.agreed:
        if low in ("да", "ок", "да, ок", "согласен", "подходит", "да подходит"):
            p.agreed = True
            set_probe(uid, p)
            # Переход к MERCEDES
            # Сохраним «ошибку» в user_state.data, чтобы потом собрать запись errors
            data = load_state(uid).get("data", {})
            data["mercedes"] = {
                "context": None,
                "emotions": None,
                "thoughts": None,
                "behavior": None,
                "beliefs_values": None,
                "state": None,
                "raw_issue": p.raw_issue,
                "when": p.when,
            }
            save_state(uid, intent="error", step="mercedes_context", data=data)
            bot.send_message(chat_id, "Идём дальше — MERCEDES.\n\n*КОНТЕКСТ.* В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)")
            return True
        else:
            # попросим поправить формулировку и повторим гипотезу
            p.summary_ready = False
            set_probe(uid, p)
            bot.send_message(chat_id, "Хорошо, уточни, что поменять в формулировке — и я перефразирую.")
            return True

    return True

def mercedes_step(uid: int, chat_id: int, text_msg: str) -> bool:
    """
    Обработка шагов MERCEDES + переход к TOTE. Возвращает True, если обработали.
    """
    st = load_state(uid)
    if st.get("intent") != "error":
        return False
    step = st.get("step")
    data = st.get("data") or {}
    mer = data.get("mercedes") or {}

    if step == "mercedes_context":
        mer["context"] = text_msg.strip()
        data["mercedes"] = mer
        save_state(uid, "error", "mercedes_emotions", data)
        bot.send_message(chat_id, "*ЭМОЦИИ.* Что чувствуешь в момент ошибки? Как это ощущается в теле? (несколько слов)")
        return True

    if step == "mercedes_emotions":
        mer["emotions"] = text_msg.strip()
        data["mercedes"] = mer
        save_state(uid, "error", "mercedes_thoughts", data)
        bot.send_message(chat_id, "*МЫСЛИ.* Что говоришь себе в этот момент? (1–2 короткие фразы)")
        return True

    if step == "mercedes_thoughts":
        mer["thoughts"] = text_msg.strip()
        data["mercedes"] = mer
        save_state(uid, "error", "mercedes_behavior", data)
        bot.send_message(chat_id, "*ПОВЕДЕНИЕ.* Что делаешь конкретно? (глаголами, 1–2 предложения)")
        return True

    if step == "mercedes_behavior":
        mer["behavior"] = text_msg.strip()
        data["mercedes"] = mer
        save_state(uid, "error", "mercedes_beliefs", data)
        bot.send_message(chat_id, "*УБЕЖДЕНИЯ/ЦЕННОСТИ.* Почему «надо именно так»? Какие убеждения/ценности за этим?")
        return True

    if step == "mercedes_beliefs":
        mer["beliefs_values"] = text_msg.strip()
        data["mercedes"] = mer
        save_state(uid, "error", "mercedes_state", data)
        bot.send_message(chat_id, "*СОСТОЯНИЕ.* В каком состоянии входил? Что доминировало: тревога, азарт, контроль?")
        return True

    if step == "mercedes_state":
        mer["state"] = text_msg.strip()
        data["mercedes"] = mer
        # короткое резюме паттерна
        pattern = f"Поведение: {mer.get('behavior') or '-'}; эмоции: {mer.get('emotions') or '-'}; мысли: {mer.get('thoughts') or '-'}."
        data["pattern_behavior"] = mer.get("behavior")
        data["pattern_emotion"]  = mer.get("emotions")
        data["pattern_thought"]  = mer.get("thoughts")
        save_state(uid, "error", "tote_goal", data)
        bot.send_message(
            chat_id,
            f"Резюме паттерна: {pattern}\n\nТеперь *TOTE*.\n*T (цель)* — сформулируй позитивно и наблюдаемо. Пример: «В ближайшие 3 сделки не двигаю стоп/тейк после входа»."
        )
        return True

    # TOTE
    if step == "tote_goal":
        data["tote_goal"] = text_msg.strip()
        save_state(uid, "error", "tote_ops", data)
        bot.send_message(chat_id, "*O (операции):* Какие шаги помогут удержать цель? (чек-лист/таймер/дыхание/заметки)")
        return True

    if step == "tote_ops":
        data["tote_ops"] = text_msg.strip()
        save_state(uid, "error", "tote_check", data)
        bot.send_message(chat_id, "*T (проверка):* Как поймёшь, что цель удержана? (критерии) Если нет — что сделаешь?")
        return True

    if step == "tote_check":
        data["tote_check"] = text_msg.strip()
        save_state(uid, "error", "tote_exit", data)
        bot.send_message(chat_id, "*E (выход):* Подведение итога. Что усилим в следующий раз?")
        return True

    if step == "tote_exit":
        data["tote_exit"] = text_msg.strip()
        # Done-условие Урока 1: сохраняем запись
        payload = {
            "error_text": (get_probe(st.get("data", {})).raw_issue if st.get("data") else None)
                          or data.get("mercedes", {}).get("raw_issue") or "ошибка (не указана)",
            "pattern_behavior": data.get("pattern_behavior"),
            "pattern_emotion":  data.get("pattern_emotion"),
            "pattern_thought":  data.get("pattern_thought"),
            "positive_goal":    data.get("tote_goal"),
            "tote_goal":        data.get("tote_goal"),
            "tote_ops":         data.get("tote_ops"),
            "tote_check":       data.get("tote_check"),
            "tote_exit":        data.get("tote_exit"),
            "checklist_pre":    "Чек-лист перед входом: сетап 100%, состояние ок, план сопровождения.",
            "checklist_post":   "После входа: дыхание, таймер, не трогаю стоп/тейк до сценария.",
        }
        try:
            insert_error_record(uid, payload)
        except SQLAlchemyError as e:
            log.warning(f"save error record failed: {e}")

        # очистим probe и переведём в idle
        save_state(uid, "idle", None, {})
        bot.send_message(
            chat_id,
            "Готово ✅\nСохранил разбор.\nХочешь добавить это в фокус недели или перейти к ТС?"
        )
        return True

    return False

# ====== Fallback / свободный диалог с мягким возвратом ======
@bot.message_handler(content_types=["text"])
def on_text(m):
    uid = m.from_user.id
    txt = (m.text or "").strip()

    # 1) если мы в прологе «ошибки» — обработать
    if handle_probe(uid, m.chat.id, txt):
        return

    # 2) если мы в MERCEDES/TOTE — обработать
    if mercedes_step(uid, m.chat.id, txt):
        return

    # 3) свободный диалог + мягкий возврат к сценарию
    sys_hint = (
        "Ты коуч по трейдингу. Отвечай кратко, по-доброму, на «ты». "
        "Пара уточняющих вопросов и предложи вернуться к шагам курса/бота. "
        "Без лекций, максимум 2–3 предложения."
    )
    reply = coach_reply(txt, sys_hint) or "Понимаю. Хочешь поговорить свободно или пойдём по шагам (например, «🚑 У меня ошибка»)?"
    bot.send_message(m.chat.id, reply, reply_markup=main_menu())

# ============ FLASK / WEBHOOK ============
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

@app.post(f"/{WEBHOOK_PATH}")
def tg_webhook():
    # Периметр: проверяем секрет и размер
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    try:
        j = request.get_json(force=True, silent=False)
        update = Update.de_json(j)
        bot.process_new_updates([update])
    except Exception as e:
        log.error(f"webhook error: {e}")
        return "error", 500
    return "ok"

# Установка вебхука при старте контейнера (без гонок)
def ensure_webhook():
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    payload = {
        "url": f"{PUBLIC_URL}/{WEBHOOK_PATH}",
        "secret_token": TG_WEBHOOK_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        log.info(f"setWebhook -> {r.status_code} {r.text}")
    except Exception as e:
        log.warning(f"setWebhook failed: {e}")

if __name__ == "__main__":
    # Ставим вебхук (без removeWebhook — Telegram сам заменит).
    ensure_webhook()
    port = int(os.getenv("PORT", "10000"))
    log.info("Starting web server…")
    app.run(host="0.0.0.0", port=port)
