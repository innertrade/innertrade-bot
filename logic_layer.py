# logic_layer.py — Innertrade Kai Mentor Bot (coach engine)
# v8.0 — медленная калибровка → резюме → подтверждение → предложение структуры

from __future__ import annotations
from typing import Dict, Any, List
from difflib import SequenceMatcher
import re, json

RISK_PATTERNS = {
    "remove_stop": ["убираю стоп", "снял стоп", "без стопа"],
    "move_stop": ["двигаю стоп", "отодвинул стоп", "переставил стоп"],
    "early_close": ["закрыл рано", "вышел в ноль", "мизерный плюс", "ранний выход"],
    "averaging": ["усреднение", "доливался против", "докупал против"],
    "fomo": ["поезд уедет", "упустил", "уйдёт без меня", "страх упустить"],
    "rule_breaking": ["нарушил план", "отошёл от плана", "игнорировал план"],
}
EMO_PATTERNS = {
    "self_doubt": ["сомневаюсь", "не уверен", "стресс", "паника", "волнение"],
    "fear_of_loss": ["страх потерь", "боюсь стопа", "не хочу быть обманутым"],
    "chaos": ["хаос", "суета", "путаюсь"],
}

BAN_TEMPLATES = [
    "понимаю", "это может быть", "важно понять", "давай рассмотрим", "было бы полезно",
    "попробуй", "используй", "придерживайся", "установи", "сфокусируйся", "следуй", "пересмотри"
]

def strip_templates(text_in: str) -> str:
    t = text_in or ""
    for ph in BAN_TEMPLATES:
        t = re.sub(rf"(?i)\b{re.escape(ph)}[^.!?]*[.!?]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.!?")[:1200]
    return t

def detect_trading_patterns(text: str) -> List[str]:
    tl = (text or "").lower()
    hits = []
    for name, keys in {**RISK_PATTERNS, **EMO_PATTERNS}.items():
        if any(k in tl for k in keys):
            hits.append(name)
    return hits

def measure_clarity(history: List[Dict[str, str]]) -> float:
    txt = " ".join([m.get("content", "") for m in history if m.get("role") == "user"])[-1200:].lower()
    signals = 0
    for kw in ["вчера","сегодня","на днях","на прошлой неделе","на выходных",
               "когда","тогда","в момент","после входа","после открытия","в сделке",
               "стоп","тейк","объём","позиция","вошёл","закрыл","открыл","план","сетап",
               "лонг","шорт","перенёс","изменил","поставил","снял"]:
        if kw in txt:
            signals += 1
    return max(0.0, min(1.0, signals / 12.0))

def should_force_structural(text: str) -> bool:
    pats = detect_trading_patterns(text)
    risk = set(pats) & set(RISK_PATTERNS.keys())
    return bool(risk) or ("fear_of_loss" in pats) or ("self_doubt" in pats)

def extract_problem_summary(history: List[Dict]) -> str:
    user_msgs = [m["content"] for m in history if m.get("role") == "user"]
    pats: List[str] = []
    for m in user_msgs:
        pats.extend(detect_trading_patterns(m))
    up = sorted(set(pats))
    parts = []
    if "fomo" in up: parts.append("FOMO (страх упустить)")
    if "remove_stop" in up or "move_stop" in up: parts.append("трогаешь/снимаешь стоп")
    if "early_close" in up: parts.append("ранний выход/«в ноль»")
    if "averaging" in up: parts.append("усреднение против позиции")
    if "fear_of_loss" in up: parts.append("страх стопа/потерь")
    if "self_doubt" in up: parts.append("сомнения после входа")
    return "Триггеры: " + (", ".join(parts) if parts else "нужен пример")

def gpt_coach_explore(oai_client, model: str, style: str, history, user_text: str) -> Dict[str, Any]:
    sys = f"""
Ты — Алекс, коуч-наставник по трейдингу. Общайся живо на «{style}».
Фаза — калибровка: уточнять контекст/эмоции/мысли, без советов и техник.
Короткие вопросы, отражение смысла. Формат — JSON: response_text, store.
""".strip()
    msgs = [{"role": "system", "content": sys}]
    for h in history[-16:]:
        if h.get("role") in ("user","assistant"):
            msgs.append(h)
    msgs.append({"role":"user","content":user_text})
    try:
        res = oai_client.chat.completions.create(
            model=model, messages=msgs, temperature=0.4,
            response_format={"type":"json_object"},
        )
        raw = res.choices[0].message.content or "{}"
        data = json.loads(raw)
        text = strip_templates(data.get("response_text","")) or "Давай на примере: где/когда это было и что именно сделал?"
        data["response_text"] = text
        if "store" not in data or not isinstance(data["store"], dict):
            data["store"] = {}
        return data
    except Exception:
        return {"response_text":"Окей, уточню: когда именно в последний раз это случилось и что сделал?", "store":{}}

def gpt_coach_summarize(oai_client, model: str, style: str, history, user_text: str) -> Dict[str, Any]:
    sys = f"""
Ты — Алекс. Сформулируй проблему словами пользователя и попроси подтвердить.
Без советов, без техник. JSON: response_text, propose_summary, ask_confirm (bool).
""".strip()
    msgs = [{"role": "system", "content": sys}]
    for h in history[-16:]:
        if h.get("role") in ("user","assistant"):
            msgs.append(h)
    msgs.append({"role":"user","content":user_text})
    try:
        res = oai_client.chat.completions.create(
            model=model, messages=msgs, temperature=0.3,
            response_format={"type":"json_object"},
        )
        raw = res.choices[0].message.content or "{}"
        data = json.loads(raw)
        rt = strip_templates(data.get("response_text",""))
        pr = data.get("propose_summary","")
        ac = bool(data.get("ask_confirm", False)) if pr else False
        return {
            "response_text": rt or "Похоже, мы близко — сформулирую одной строкой и сверимся.",
            "propose_summary": pr,
            "ask_confirm": ac
        }
    except Exception:
        return {"response_text":"Соберу в одну строку и сверимся, окей?", "propose_summary":"", "ask_confirm":False}

def process_turn(oai_client, model: str, state: Dict[str,Any], user_text: str) -> Dict[str,Any]:
    updates: Dict[str,Any] = {}
    history = state.get("history", [])
    style = state.get("style", "ты")

    clarity = measure_clarity(history)
    prev = state.get("coach", {})
    turn = int(prev.get("turns", 0)) + 1
    loop = prev.get("loop") or "explore"  # explore -> summarize -> structure

    force_struct = should_force_structural(user_text)
    ask_confirm = False
    propose_summary = ""
    suggest_struct = False

    updates["coach"] = {"clarity": round(clarity,2), "turns": turn, "loop": loop}

    if state.get("problem_confirmed"):
        loop = "structure"
        updates["coach"]["loop"] = "structure"

    if loop == "explore":
        data = gpt_coach_explore(oai_client, model, style, history, user_text)
        reply = data["response_text"]
        for k, v in data.get("store", {}).items():
            updates[k] = v
        if force_struct or (turn >= 3 and clarity >= 0.55):
            updates["coach"]["loop"] = "summarize"

    elif loop == "summarize":
        data = gpt_coach_summarize(oai_client, model, style, history, user_text)
        reply = data["response_text"]
        propose_summary = data["propose_summary"]
        ask_confirm = data["ask_confirm"]
        if not ask_confirm or not propose_summary:
            updates["coach"]["loop"] = "explore"

    else:  # structure
        suggest_struct = True
        reply = "Готов пройтись по шагам и собрать краткий план изменений?"

    return {
        "reply": reply,
        "state_updates": updates,
        "ask_confirm": ask_confirm,
        "propose_summary": propose_summary,
        "suggest_struct": suggest_struct,
    }
