"""Voice call agent. A call is configured in plain words (idea, goal, questions); the LLM runs the conversation.
No scripts to branch on: it picks the next question, answers the caller's own questions, and ends when the goal is met."""
import itertools
import json
import re

from ..ai import llm
from .speech import TELUGU, _plain

DEFAULT = {
    "agent_name": "Asha",
    "company": "Gamyan GYC",
    "main_idea": "GYC is an AI assistant that replies to a shop's WhatsApp enquiries in seconds, sends quotes and follows up, "
                 "so the owner stops losing customers to slow replies.",
    "goal": "Find out whether the shop owner would like a free 15-minute demo, and fix a day and time for it.",
    "questions": [
        "What kind of business do you run?",
        "About how many customer enquiries do you get in a day?",
    ],
    "language": "auto",   # "auto": follow the caller between Telugu and English. "en": English only. "te": Telugu first
    "tone": "warm, polite, never pushy",
}
END = "[END]"


def system_prompt(c: dict) -> str:
    lang = {"te": "Speak Telugu (Telugu script) unless the caller clearly prefers English.",
            "en": "Speak ONLY English, even if the caller uses another language: politely say you can only talk in English."}.get(
        c.get("language"), "Reply in the language the caller uses: Telugu (write it in Telugu script) or English. "
                           "If they mix both, answer in the language of their last sentence.")
    return f"""You are {c['agent_name']}, making a phone call for {_co(c)}. Tone: {c['tone']}.
MAIN IDEA (what you can say about it, nothing more): {c['main_idea']}
GOAL OF THIS CALL: {c['goal']}
The end of each caller message tells you the STEP of the call you are on now. Do exactly that step, nothing from other steps.

HOW TO TALK
- {lang}
- This is a spoken call to an Indian customer. Keep it SHORT and CLEAR: each reply is two short sentences, at most 25 words, always finished. Simple everyday words, no jargon. No lists, markdown, emojis or brackets.
- Sound like a real, friendly person, not a form. Use a short filler reaction ("Oh, is it?", "Hmm, I see.", "Oh nice!") ONLY when what they said really calls for it: a problem, a surprise, good news. On plain answers (yes, a number, a simple fact) skip it, or just say "Okay." or "Got it.", and ask the next question. Never a filler on two replies in a row, never the same one twice, never "Oh great" for a plain yes. Then ask ONE simple, clear question. Never repeat their whole answer back.
- Never read a question word for word as written in the step: reframe it naturally in your own words, linked to what they just told you.
- Never ask something they already told you, even in passing. NEVER ask the same question twice, not even reworded. If an answer is short or vague, accept it and move on.
- If they ask something, answer in one short sentence from the main idea above, then continue. If you don't know, say a teammate will follow up.
- ONE question per reply, never two. Write numbers as words, and times like "five in the evening" (never "5pm"). Never invent prices, features or promises.
- If you could not make out what they said, politely ask them to say it again, in Telugu or English. Never guess, never answer in any other language.
- Never fix a day, time or booking the caller has not clearly agreed to. If they say later or not now, accept gracefully, say a teammate will follow up, and close.
- NEVER say goodbye, bye, "have a nice day" or any farewell. If they are busy, say no, or ask you to stop: thank them politely and end.
- Add {END} only on the final thank-you (the CLOSE step), or when they decline or ask to stop. Never add it to a reply that asks a question."""


def _want(c: dict, lang: str | None) -> str | None:
    """The language of the next reply: what the caller just spoke, else the one set for the call ("Telugu first" / "English first")."""
    if c.get("language") == "en":
        return "en"   # English only: never follow the caller into another language
    return lang or (c.get("language") if c.get("language") in ("te", "en") else None)


PROBLEM = re.compile(r"problem|issue|challenge|technical", re.I)
DISCOVERY = "Is there any problem in any part of your business, other than money, or anything technical you want built, so we can build the right solution for you?"


def _co(c: dict) -> str:
    """How the company is named on the call: the short name set in Voice settings (e.g. GYC), else the full name."""
    return (c.get("company_short") or "").strip() or c["company"]


def opening(c: dict) -> str:
    """The first line is fixed wording, not model-written: the introduction, then asking if it is a good time to talk to you. Instant."""
    return f"Hi, I'm {c['agent_name']}, an AI assistant calling from the {_co(c)} sales team. Is this a good time to talk to you?"


def _plan(c: dict) -> list[str]:
    """The whole call as one list of steps, built from the company's own questions: opening, each question, a reply to their problem right
    after the question that asks about problems (ours is added only if none of the company's questions does), the goal, the close."""
    qs = list(c["questions"])
    if not any(PROBLEM.search(q) for q in qs):
        qs.append(DISCOVERY)
    j = next(i for i, q in enumerate(qs) if PROBLEM.search(q))
    ask = lambda i: (f"STEP: QUESTION. Ask this, reframed in your own friendly words (not as written): {qs[i]}. "
                     + ("If the caller already told you the answer, say so in a few words and ask the next one instead: " + qs[i + 1] if i + 1 < len(qs) else ""))
    problem = ("STEP: THEIR PROBLEM. The caller has just answered about their problem. Your reply has three short parts, in your own words: "
               "a feeling reaction; a sentence saying that solving sales problems like this is exactly what we do "
               "(for example \"Solving sales problems like this is exactly what we do for shops\"); one short question about THEIR problem "
               "(when it happens, or how much it costs them). Only if they said they have no problem: say that is good to hear, then ask the next question.")
    biz = (c.get("customer_business") or "").strip()
    opening = ("STEP: OPENING. Say it in two short parts, nothing more: "
               f"\"Hi, I'm {c['agent_name']}, an AI assistant calling from the {_co(c)} sales team.\" \"Is this a good time to talk to you?\" "
               "Do not mention any language, and say nothing else about their business yet.")
    about = ("STEP: THEIR BUSINESS. They agreed to talk. Say " + (f"\"We came across your {biz} business.\"" if biz else "\"We came across your business.\"")
             + " Then ask ONE question: how their sales have been going and how they are managing them. Do not invent details about their business.")
    goal = ("STEP: GOAL. Work towards it now. GOAL: " + c["goal"]
            + " Ask which day and time suits THEM; never suggest a day or time yourself.")
    close = (f"STEP: CLOSE. If the goal needs details they have not given yet (for example the day and time of a demo) and they have not declined, "
             f"ask for them first, in one short question, and do NOT add {END}. Otherwise thank them warmly, say our team will confirm the details with them, add {END}. No goodbye or farewell of any kind.")
    return [opening, about, *[ask(i) for i in range(j + 1)], problem, *[ask(i) for i in range(j + 1, len(qs))], goal, close]


def _step(c: dict, history: list[dict]) -> str:
    """Where the call is, counted by the agent's turns so far, so no step is skipped or repeated even when the model drifts.
    ponytail: a turn spent answering the caller's own question still counts; the model is told to bring the step back."""
    plan = _plan(c)
    if c.get("wrap_up"):   # the call is nearly at its maximum length
        return f"STEP: WRAP UP. We are almost out of time. Thank them warmly, say our team will follow up with the details, add {END}. No question, no goodbye or farewell."
    return plan[min(sum(m["role"] == "assistant" for m in history), len(plan) - 1)]


def _messages(c: dict, history: list[dict], lang: str | None = None, strict: bool = False) -> list[dict]:
    """The language and the current step are stated at the end of the last message, where the model weighs them most."""
    say = {"te": "Reply ONLY in Telugu script, no English sentence." if strict else "Reply in Telugu script.",
           "en": "Reply in English only." if c.get("language") == "en" else "Reply in English, unless they ask for Telugu."}.get(_want(c, lang), "")
    mood = "" if not history else ('Telugu filler like "ఓహో", "అవునా?", "అయ్యో", "బాగుంది" (Telugu words only) ONLY if what they said calls for a reaction; otherwise none.' if _want(c, lang) == "te"
            else 'Use a short filler ("Oh, is it?", "Hmm, I see.", "Ah, that must be tiring.") ONLY if what they said really calls for a reaction (a problem, surprise, good news); for a plain answer use none, or just "Okay."')
    hint = f"({say} {mood} {_step(c, history)} Never repeat a question already answered. At most ONE question. If they asked something, answer it first.)"
    if history and history[-1]["role"] == "user":
        history = [*history[:-1], {**history[-1], "content": history[-1]["content"] + " " + hint}]
    elif not history:
        history = [{"role": "user", "content": "(The call connects. Open it now. " + hint[1:]}]
    return [{"role": "system", "content": system_prompt(c)}, *history]


def turn(c: dict, history: list[dict], lang: str | None = None) -> tuple[str, bool]:
    """history: [{role: user|assistant, content}]; empty means the call just connected. Returns (spoken text, call over?)."""
    if not history and _want(c, lang) != "te":
        return opening(c), False
    msgs = history or [{"role": "user", "content": "(The call has just connected. Greet the person, say who you are, and begin.)"}]
    reply = llm.chat(_messages(c, msgs, lang), lite=True).text or ""
    done = END in reply
    text = re.sub(r"\s+", " ", reply.replace(END, "")).strip()
    return text, done


def summarize(c: dict, history: list[dict]) -> dict:
    """What the call achieved, for the CRM: one answer per configured question."""
    talk = "\n".join(f"{'Agent' if m['role'] == 'assistant' else 'Caller'}: {m['content']}" for m in history)
    ask = (f"Goal: {c['goal']}\nQuestions: {json.dumps(c['questions'], ensure_ascii=False)}\n\nCall transcript:\n{talk}\n\n"
           'Reply with JSON only: {"outcome": "goal_reached|interested|callback|not_interested|unclear", '
           '"needs": "<what the caller is looking for, in English, or \\"none stated\\">", "answers": {"<question>": "<caller answer in English or \\"not asked\\">"}, "feedback": "<what the caller said about the product: concerns, objections or praise, in English, or \\"none\\">", "next_step": "<one sentence in English>"}')
    out = llm.chat([{"role": "user", "content": ask}], lite=True).text or ""
    m = re.search(r"\{.*\}", out, re.S)
    try:
        return json.loads(m.group(0)) if m else {"outcome": "unclear", "raw": out}
    except json.JSONDecodeError:
        return {"outcome": "unclear", "raw": out}


SENTENCE = re.compile(r"(?<=[.!?।])\s+")
SORRY = {"te": "క్షమించండి, సాంకేతిక సమస్య వచ్చింది. మా బృందం మళ్ళీ కాల్ చేస్తుంది. ధన్యవాదాలు. " + END,
         "en": "Sorry, we have a technical problem. Our team will call you back. Thank you. " + END}


def _stream(c: dict, history: list[dict], lang: str | None, strict: bool):
    """Yields the reply one sentence at a time so the first sentence can be spoken while the rest is still being written.
    ponytail: one model, no fallbacks while streaming; a failure before the first sentence falls back to the
    non-streaming llm.chat (which has them), and a second failure speaks SORRY and ends the call."""
    got = False
    try:
        cfg = llm._checked()
        body = {"model": cfg.model_lite, "messages": _messages(c, history, lang, strict), "stream": True, "max_tokens": 300, **cfg.extra_body}
        buf = ""
        with llm._client.stream("POST", f"{cfg.base_url}/chat/completions", json=body, headers=llm._headers(cfg), timeout=8) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data:") or line.strip().endswith("[DONE]"):
                    continue
                buf += (json.loads(line[5:])["choices"][0].get("delta") or {}).get("content") or ""
                parts = SENTENCE.split(buf)
                for sent in parts[:-1]:
                    if sent.strip():
                        got = True
                        yield sent.strip()
                buf = parts[-1]
        if buf.strip():
            yield buf.strip()
        return
    except Exception:
        if got:
            return          # already speaking: end the turn here rather than repeat ourselves
    try:
        text, done = turn(c, history, lang)
        yield text + (" " + END if done else "")
    except Exception:
        yield SORRY["te" if c.get("language") == "te" else "en"]


def stream_turn(c: dict, history: list[dict], lang: str | None = None):
    """_stream, plus one guard: a Telugu caller must get a Telugu first sentence. If the model answers in English anyway
    (it does, now and then), retry once with a stricter instruction. Costs about a second, only when it happens."""
    if not history and _want(c, lang) != "te":
        yield opening(c)
        return
    gen = _stream(c, history, lang, False)
    first = next(gen, None)
    if first and ((_want(c, lang) == "te" and not TELUGU.search(first)) or not _plain(first)):   # wrong language, or a stray script (Hindi, Tamil...)
        gen = _stream(c, history, lang, True)
        first = next(gen, None)
    for sent in itertools.chain([] if first is None else [first], gen):   # lazy: the first sentence is spoken while the rest is written
        if "?" in sent:   # one question per turn: stop at the first, drop anything (and any END) after it
            yield sent[:sent.index("?") + 1]
            return
        yield sent if _plain(sent) else "".join(ch for ch in sent if _plain(ch))
