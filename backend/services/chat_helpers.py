from datetime import date, datetime, time, timedelta
from typing import Any, Callable, List
import re
import json
import uuid

from sqlalchemy.orm import Session

from app.models import ConversationMessage, ConversationThread, Mood, Task
from app.schemas import (
    ChatMeta,
    ChatResponse,
    ClarificationOption,
    ClarificationPrompt,
    ConflictPrompt,
    ConflictTaskRef,
    SlotSuggestion,
)
from services.planner import get_historical_avg_duration, infer_task_type, rebalance_day
from services.scheduling import extract_date_from_text, infer_duration_minutes, parse_time
from services.tools import create_task, get_schedule, infer_priority

STOP_WORDS = {
    "the", "a", "an", "to", "for", "and", "or", "is", "at", "on", "in", "of", "my",
    "our", "your", "with", "from", "by", "it", "this", "that", "please", "plan", "day",
    "move", "moved", "earlier", "later", "before", "after", "around", "about",
}

FOLLOWUP_VERBS = ("move", "change", "shift", "reschedul", "push", "postpone", "swap", "keep", "remove", "delete", "drop", "cancel")

GENERIC_GREETING_PATTERNS = [
    r"hi",
    r"hiya",
    r"hello",
    r"hey",
    r"hey there",
    r"good morning",
    r"good afternoon",
    r"good evening",
    r"yo",
]

GENERIC_WELLBEING_PATTERNS = [
    r"how(?:\s+are)?\s+you(?:\s+doing)?(?:\s+today)?",
    r"how(?:\s+have)?\s+you\s+been(?:\s+lately)?",
    r"how\s+you\s+been(?:\s+lately)?",
    r"how'?s it going",
    r"whats up",
    r"what'?s up",
    r"how are things",
    r"how'?s your day going",
    r"how is your day going",
    r"how are you feeling",
    r"how you feeling",
    r"how are things going",
]

GENERIC_THANKS_PATTERNS = [
    r"thanks",
    r"thank you",
    r"thanks!",
    r"thank you!",
    r"thx",
    r"ty",
]

GENERIC_ACK_PATTERNS = [
    r"okay",
    r"ok",
    r"cool",
    r"sounds good",
    r"got it",
    r"alright",
    r"all right",
    r"nice",
    r"great",
    r"awesome",
    r"sure",
]

GENERIC_POSITIVE_PATTERNS = [
    r"you make me happy",
    r"youre the best",
    r"you are the best",
    r"i like talking to you",
    r"i love talking to you",
    r"youre amazing",
    r"you are amazing",
    r"youre great",
    r"you are great",
    r"youre so helpful",
    r"you are so helpful",
]

POSITIVE_USER_TO_ASSISTANT_WORDS = {
    "nice", "kind", "sweet", "amazing", "great", "helpful", "awesome", "lovely",
    "wonderful", "cool", "smart", "thoughtful", "funny", "gentle", "patient",
    "supportive", "good", "brilliant", "fantastic",
}

POSITIVE_USER_FEELING_WORDS = {
    "happy", "better", "calm", "safe", "lighter", "good", "great", "okay", "ok", "seen",
}

GENERIC_STATUS_PATTERNS = [
    r"(?:i am|im|ive been|i have been|feeling|i feel)\s+(?:really\s+|so\s+|kinda\s+|kind of\s+|a bit\s+|not\s+)?(?:bored|tired|sleepy|stressed|overwhelmed|drained|burnt out|burned out|anxious|sad|down|lonely|unmotivated|demotivated|restless|frazzled|busy|good|great|okay|ok|well)(?:\s+(?:today|lately|right now|these days|you know|honestly|tbh|though|man|at the moment))?",
    r"i\s+do(?:\s*not|nt)\s+feel\s+(?:so\s+|very\s+|that\s+)?(?:good|well|great|okay|ok)",
    r"(?:its|it is)\s+been\s+(?:a\s+)?(?:long|rough|busy|stressful|exhausting)\s+day(?:\s+(?:today|honestly|you know))?",
    r"life\s+is\s+(?:a\s+bit\s+|kind of\s+|kinda\s+|so\s+)?(?:much|heavy|a lot)(?:\s+today)?",
    r"(?:this|today)\s+(?:day\s+is|is)\s+(?:weird|strange|off|rough)",
    r"i\s+feel\s+(?:meh|off|weird)",
    r"i(?:m| am)\s+(?:really\s+|so\s+|very\s+)?(?:angry|mad|upset|frustrated|annoyed)(?:\s+(?:today|lately|right now|honestly|you know))?",
    r"i\s+am\s+(?:really\s+|so\s+|very\s+)?(?:angry|mad|upset|frustrated|annoyed)(?:\s+(?:today|lately|right now|honestly|you know))?",
    r"i(?:m| am)\s+(?:really\s+|so\s+|very\s+)?(?:sad|hurt|lonely|down)(?:\s+(?:today|lately|right now|honestly|you know))?",
    r"i\s+am\s+(?:really\s+|so\s+|very\s+)?(?:sad|hurt|lonely|down)(?:\s+(?:today|lately|right now|honestly|you know))?",
]

GENERIC_CASUAL_CHAT_PATTERNS = [
    r"tell me something interesting",
    r"say something nice",
    r"give me a quick pep talk",
    r"im bored(?:\s+you know)?",
    r"what are you up to",
    r"whatre you up to",
    r"you there",
    r"can we chat",
    r"lets chat",
    r"let us chat",
    r"can we just talk normally",
    r"can we talk normally",
    r"can we just talk",
    r"can we talk",
    r"talk to me normally",
    r"talk normally",
]


def _looks_like_replan_mutation_request(text: str) -> bool:
    lower = (text or "").lower().strip()
    if not lower:
        return False
    has_mutation = bool(
        re.search(
            r"\b(move|change|shift|reschedul|push|postpone|swap|keep|remove|delete|drop|cancel)\b",
            lower,
        )
    )
    if not has_mutation:
        return False
    has_existing_ref = bool(
        re.search(r"\b(this|that|it|these|those|other tasks?|existing tasks?|task-\d+)\b", lower)
    )
    # "move X to Y" / "change X to Y" generally implies replan.
    has_direct_transform = bool(re.search(r"\b(move|change|shift|reschedul)\b.*\bto\b", lower))
    has_implicit_adjustment = bool(
        re.search(r"\b(make|do|apply)\s+(the\s+)?change\b", lower)
        or re.search(r"\b(adjust|update|modify)\b.*\b(accordingly|as discussed|as above)\b", lower)
        or "accordingly" in lower
    )
    return has_existing_ref or has_direct_transform or has_implicit_adjustment


def _looks_like_followup_instruction(text: str) -> bool:
    lower = (text or "").lower().strip()
    if not lower:
        return False
    if _looks_like_replan_mutation_request(lower):
        return True
    if re.search(r"\b(make|apply)\s+(the\s+)?change\b", lower):
        return True
    if re.search(r"\b(change|adjust|update|modify)\b.*\b(accordingly|as discussed|as above)\b", lower):
        return True
    if "on second thought" in lower or lower.startswith("no change"):
        return True
    return False


def _has_thread_task_context(thread_state: dict | None) -> bool:
    if not thread_state:
        return False
    return bool(
        thread_state.get("last_referenced_task_ids")
        or thread_state.get("last_created_task_ids")
        or thread_state.get("last_updated_task_ids")
    )


def _looks_like_fresh_plan_request(text: str, fallback_multi: list[dict]) -> bool:
    lower = (text or "").lower().strip()
    if not lower:
        return False
    if _looks_like_followup_instruction(lower):
        return False

    has_planning_phrase = any(
        p in lower
        for p in [
            "plan my day",
            "block my calendar",
            "fit in",
            "i have",
            "i need",
            "i want",
            "i'll",
            "i will",
        ]
    )
    has_time_span = bool(
        re.search(
            r"\bfrom\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s+(?:to|-)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
            lower,
        )
    )
    return len(fallback_multi) >= 2 and (has_planning_phrase or has_time_span)


def _looks_like_schedule_request(text: str) -> bool:
    lower = (text or "").lower()
    if not lower.strip():
        return False
    return bool(
        re.search(
            r"\b(schedule|calendar|plan for today|show (?:my|the) plan|what(?:'s| is) (?:my|the) schedule)\b",
            lower,
        )
    )


def _looks_like_delete_request(text: str, thread_state: dict | None) -> bool:
    lower = (text or "").lower().strip()
    if not lower:
        return False
    if not re.search(r"\b(delete|remove|drop)\b", lower):
        return False
    if re.search(r"\b(task|event|meeting|class|reminder)\b", lower):
        return True
    return _has_thread_task_context(thread_state)


def _infer_fallback_action(message: str, thread_state: dict | None) -> str | None:
    text = message or ""
    fallback_multi = split_tasks_from_message(text)
    if _looks_like_schedule_request(text):
        return "get_schedule"
    replan_request = _parse_replan_request(text)
    has_named_task_target = bool(
        re.search(
            r"\b(dinner|lunch|breakfast|sleep|class|meeting|study|revision|gym|walk|call|mom|dad|workout|task|event|reminder)\b",
            text.lower(),
        )
    )
    if _looks_like_followup_instruction(text) and (
        _has_thread_task_context(thread_state)
        or replan_request.get("target_phrase")
        or has_named_task_target
    ):
        return "replan_day"
    if _looks_like_delete_request(text, thread_state):
        return "delete_task"
    if _looks_like_fresh_plan_request(text, fallback_multi) or len(fallback_multi) >= 2:
        return "create_task"
    return None


def _normalize_generic_text(text: str) -> str:
    lower = (text or "").lower().strip()
    lower = re.sub(r"['’]", "", lower)
    lower = re.sub(r"\bi m\b", "im", lower)
    lower = re.sub(r"\byou re\b", "youre", lower)
    lower = re.sub(r"\bwhat s\b", "whats", lower)
    lower = re.sub(r"\bit s\b", "its", lower)
    lower = re.sub(r"[^a-z0-9\s]", " ", lower)
    lower = re.sub(r"\s+", " ", lower)
    return lower


def _normalize_generic_token(token: str) -> str:
    if not token:
        return token
    normalized = re.sub(r"(.)\1{2,}", r"\1", token)
    if re.fullmatch(r"he+y+", normalized):
        return "hey"
    if re.fullmatch(r"hi+i+", normalized):
        return "hi"
    if re.fullmatch(r"hello+o*", normalized):
        return "hello"
    return normalized


def _normalize_generic_variants(text: str) -> list[str]:
    normalized = _normalize_generic_text(text)
    if not normalized:
        return [""]
    tokens = normalized.split()
    collapsed = " ".join(_normalize_generic_token(token) for token in tokens)
    variants = [normalized]
    if collapsed != normalized:
        variants.append(collapsed)
    return variants


def _looks_like_positive_message(normalized: str) -> bool:
    if not normalized:
        return False
    tokens = set(normalized.split())
    if any(_matches_any_pattern(normalized, [pattern]) for pattern in GENERIC_POSITIVE_PATTERNS):
        return True
    if ("youre" in tokens or ("you" in tokens and "are" in tokens)) and tokens.intersection(POSITIVE_USER_TO_ASSISTANT_WORDS):
        return True
    if normalized.startswith("you make me ") and tokens.intersection(POSITIVE_USER_FEELING_WORDS):
        return True
    if re.search(r"\bi\s+(?:really\s+|truly\s+|honestly\s+)?(?:like|love|appreciate|adore)\s+you\b", normalized):
        return True
    return False


def _looks_like_single_task_request(text: str) -> bool:
    lower = (text or "").lower().strip()
    if not lower:
        return False
    if re.search(r"\b(can|could|would)\s+we\s+(just\s+)?talk\b", lower):
        return False
    if re.search(r"\btalk(?:\s+to\s+me)?\s+normally\b", lower):
        return False
    if re.search(r"\b(can|could|would)\s+we\s+chat\b", lower):
        return False
    if _extract_time_range(lower)[0] or extract_time_from_text(lower):
        return True
    if re.search(r"\b\d+\s*(hour|hours|hr|hrs|min|mins|minute|minutes)\b", lower):
        return True
    return bool(
        re.search(
            r"\b(go|eat|cook|study|work|rest|sleep|talk|call|meet|attend|exercise|walk|code|coding|practice|read|write|buy|shop|clean|review|organize|plan|schedule)\b",
            lower,
        )
    )


def _matches_any_pattern(text: str, patterns: list[str]) -> bool:
    return any(re.fullmatch(pattern, text) for pattern in patterns)


def _pick_generic_reply(options: list[str], variant_seed: int) -> str:
    if not options:
        return ""
    return options[variant_seed % len(options)]


def _generic_reply_variant_seed(db: Session, thread_key: str) -> int:
    return (
        db.query(ConversationMessage)
        .filter(ConversationMessage.thread_key == thread_key, ConversationMessage.role == "assistant")
        .count()
    )


def _classify_generic_conversation(text: str, thread_state: dict | None) -> str | None:
    normalized_variants = _normalize_generic_variants(text)
    normalized = normalized_variants[0]
    if not normalized:
        return "fallback"

    # Let clearly actionable or follow-up scheduling requests continue through normal intent handling.
    if _infer_fallback_action(text, thread_state):
        return None
    if _looks_like_followup_instruction(text):
        return None
    if _looks_like_delete_request(text, thread_state):
        return None
    if _looks_like_schedule_request(text):
        return None
    if _looks_like_single_task_request(text):
        return None

    if any(_matches_any_pattern(variant, GENERIC_GREETING_PATTERNS) for variant in normalized_variants):
        return "greeting"
    if any(_matches_any_pattern(variant, GENERIC_WELLBEING_PATTERNS) for variant in normalized_variants):
        return "wellbeing"
    if any(_matches_any_pattern(variant, GENERIC_THANKS_PATTERNS) for variant in normalized_variants):
        return "thanks"
    if any(_looks_like_positive_message(variant) for variant in normalized_variants):
        return "positive"
    if any(_matches_any_pattern(variant, GENERIC_ACK_PATTERNS) for variant in normalized_variants):
        return "ack"
    if any(_matches_any_pattern(variant, GENERIC_STATUS_PATTERNS) for variant in normalized_variants):
        return "status"
    if any(_matches_any_pattern(variant, GENERIC_CASUAL_CHAT_PATTERNS) for variant in normalized_variants):
        return "casual"
    if len(normalized.split()) <= 7 and normalized.endswith("?"):
        return "casual"
    if len(normalized.split()) <= 5:
        return "fallback"
    return None


def _build_generic_conversation_reply(category: str | None, text: str | None = None, variant_seed: int = 0) -> str:
    normalized = _normalize_generic_text(text or "")
    if category == "greeting":
        if "good morning" in normalized:
            return _pick_generic_reply([
                "Good morning. I'm glad to hear from you, and I'm ready to help. What would you like to get organized first today?",
                "Good morning. I'm here with you and ready to help you get settled into the day. What would you like to plan or sort out first?",
            ], variant_seed)
        if "good evening" in normalized:
            return _pick_generic_reply([
                "Good evening. I'm here with you and ready to help however I can. What would you like to sort out or plan next?",
                "Good evening. We can keep this simple and make the rest of the day easier. What would you like to organize next?",
            ], variant_seed)
        return _pick_generic_reply([
            "Hi! It's good to hear from you. I'm here and ready to help you get things organized. What would you like to plan or rearrange today?",
            "Hey. I'm here and ready to help however you want to use me. What would you like to organize or sort out today?",
            "Hi there. Happy to help. If you'd like, we can turn whatever is on your mind into a simple plan for today.",
        ], variant_seed)
    if category == "wellbeing":
        if "day" in normalized:
            return _pick_generic_reply([
                "My day is going smoothly, and I'm ready to help. I'm here and focused on whatever would make things easier for you next. Want me to help organize your schedule or sort out a task?",
                "Things are going well on my side, and I'm ready to help. If you want, we can use that energy to organize what's next in your day.",
            ], variant_seed)
        if "feeling" in normalized:
            return _pick_generic_reply([
                "I'm doing well and staying focused. I'm here, present, and ready to help with whatever you need next. If you want, we can turn that into a simple plan for the rest of your day.",
                "I'm feeling good and ready to help. I'm here with you, and we can keep things practical from here. Want to organize the next part of your day together?",
            ], variant_seed)
        return _pick_generic_reply([
            "I'm doing well and ready to help. I'm focused and here for whatever you need next. Want me to help organize your schedule or adjust a task?",
            "I'm doing well, and I'm ready when you are. If you'd like, I can help organize your schedule or make the next task feel clearer.",
        ], variant_seed)
    if category == "thanks":
        return _pick_generic_reply([
            "You're welcome. Happy to help. If you'd like, we can keep going and plan, reschedule, or review the rest of your day.",
            "Anytime. I'm glad that helped. If you want, we can keep the momentum going and organize what comes next.",
        ], variant_seed)
    if category == "positive":
        if "happy" in normalized:
            return _pick_generic_reply([
                "That’s really kind of you to say. I’m glad this is helping and that you feel good talking with me. If you want, we can carry that forward and organize the next part of your day together.",
                "That means a lot. I’m really glad I can make things feel a little lighter for you. If you'd like, we can use that energy and turn it into a simple plan for what comes next.",
            ], variant_seed)
        if "feel better" in normalized or "appreciate" in normalized:
            return _pick_generic_reply([
                "That means a lot to hear. I'm really glad this feels helpful to you. If you'd like, we can build on that and organize the next part of your day together.",
                "Thank you, I really appreciate that. I'm glad I can make things feel a little easier. If you want, I can help turn that into a simple plan for what comes next.",
            ], variant_seed)
        if "nice" in normalized or "kind" in normalized:
            return _pick_generic_reply([
                "That’s really kind of you to say. I’m glad this feels supportive. If you want, we can keep going and organize whatever comes next.",
                "Thank you, that’s very kind. I’m happy to be here with you. If you'd like, I can help you sort out the next part of your day.",
            ], variant_seed)
        if "best" in normalized or "amazing" in normalized or "great" in normalized:
            return _pick_generic_reply([
                "That’s really sweet of you. I’m glad I’ve been helpful. If you want, I can keep that going and help you organize the next step in your day.",
                "Thank you, that’s very kind. I’m happy to be useful to you. If you'd like, we can keep going and plan or sort out what comes next.",
            ], variant_seed)
        return _pick_generic_reply([
            "That’s really kind of you to say. I’m glad this feels helpful. If you want, we can keep going and organize whatever comes next.",
            "Thank you, that means a lot. I’m happy to be here with you. If you'd like, I can help turn that good momentum into a simple plan for the rest of the day.",
        ], variant_seed)
    if category == "ack":
        return _pick_generic_reply([
            "Sounds good. We can keep things simple from here. What would you like to organize next?",
            "Got it. We can take it one step at a time from here. What would you like to plan or sort out next?",
        ], variant_seed)
    if category == "status":
        if "bored" in normalized:
            return _pick_generic_reply([
                "That sounds dull, and it can make the day drag. Sometimes one small change helps more than trying to fix everything at once. If you'd like, I can help you line up one or two useful things to do next and turn them into a simple plan.",
                "That kind of boredom can make the whole day feel sticky. We don't need to force a big reset. If you want, I can help you pick one or two small things and organize them into a simple next step.",
            ], variant_seed)
        if "dont feel so good" in normalized or "do not feel so good" in normalized:
            return _pick_generic_reply([
                "I'm sorry you're not feeling so good. Let's keep things light and manageable for now instead of piling more on. If you'd like, I can help you simplify the rest of your day and make a gentle plan from here.",
                "I'm sorry you're feeling off. We can keep this gentle and not ask too much of you right now. If you'd like, I can help you scale the rest of the day down into something more manageable.",
            ], variant_seed)
        if any(word in normalized for word in ["overwhelmed", "stressed", "drained", "burnt out", "burned out", "frazzled"]):
            return _pick_generic_reply([
                "That sounds like a lot to carry right now. We don't need to solve everything at once. If you'd like, I can help you break the rest of the day into a couple of smaller, calmer next steps and organize them into a simple plan.",
                "That sounds heavy. Let's not make the whole day your problem all at once. If you want, I can help you sort the next few hours into smaller, steadier steps.",
            ], variant_seed)
        if any(word in normalized for word in ["angry", "mad", "upset", "frustrated", "annoyed"]):
            return _pick_generic_reply([
                "That sounds really frustrating. It makes sense that you're feeling that way. If you'd like, I can help you slow things down and organize the next step without adding more pressure.",
                "I'm sorry it's hitting like that. When you're angry or frustrated, even the next small step can feel harder than it should. If you want, I can help you sort out one calm, practical next move and turn it into a simple plan.",
            ], variant_seed)
        if any(word in normalized for word in ["sad", "hurt", "lonely", "down"]):
            return _pick_generic_reply([
                "I'm sorry you're feeling that way. That can make everything feel heavier than usual. If you'd like, I can help you make the rest of the day feel a little gentler and more manageable with a simple plan.",
                "That sounds really hard. We can keep things soft and simple from here. If you want, I can help you organize one or two easy next steps for the rest of the day.",
            ], variant_seed)
        if any(word in normalized for word in ["meh", "weird", "off", "rough", "long day", "life is"]):
            return _pick_generic_reply([
                "That kind of day can throw everything a little off balance. We can keep it simple and steady from here. If you'd like, I can help you shape the next part of your day into something more manageable and organized.",
                "That sounds like one of those off days. We can keep the bar low and just get you to the next useful step. If you'd like, I can help you organize what comes next.",
            ], variant_seed)
        return _pick_generic_reply([
            "I'm sorry you're feeling that way. Let's keep things gentle and manageable from here. If you'd like, I can help you turn the rest of your day into a simple plan with just one or two easy next steps.",
            "That sounds tough. We can slow things down and make the next step easier. If you'd like, I can help you organize the rest of the day in a lighter way.",
        ], variant_seed)
    if category == "casual":
        if "talk" in normalized or "chat" in normalized:
            return _pick_generic_reply([
                "Yes, we can keep this more natural. I'm happy to talk in a normal way and still help when you want to shift back into planning. If you'd like, tell me what's on your mind, or I can help you organize the next part of your day.",
                "Absolutely. We can talk more normally and keep this relaxed. And whenever you want, I can help turn the conversation into a plan or help organize what comes next.",
            ], variant_seed)
        if "you there" in normalized:
            return _pick_generic_reply([
                "Yep, I'm here. We can keep things light or make them practical, whichever helps more right now. If you want, I can help you sort out what to do next and organize the next part of your day.",
                "I'm here. We can just talk for a moment, or we can make things useful and organized from here. Whatever feels more helpful right now works for me.",
            ], variant_seed)
        if "what are you up to" in normalized:
            return _pick_generic_reply([
                "I'm here and ready to help. Mostly I'm focused on whatever would make your day feel clearer or easier. If you want, we can chat for a moment and then organize the next step together.",
                "I'm here, paying attention, and ready to help with whatever would make the day smoother. If you'd like, we can talk for a minute and then organize what comes next.",
            ], variant_seed)
        if "interesting" in normalized:
            return _pick_generic_reply([
                "Sure. One interesting thing is that even a tiny concrete plan can lower the mental load of a messy day more than people expect. If you'd like, I can help you turn that into one useful next step and build from there.",
                "Sure. One interesting thing is that clarity usually comes after a small action, not before it. If you want, I can help you organize one next step and see where that leads.",
            ], variant_seed)
        return _pick_generic_reply([
            "Absolutely. We can chat for a moment and still make things useful. Want me to help you organize the next part of your day or line up one small task?",
            "Of course. We can keep this casual and still make it helpful. If you want, I can help you organize what comes next without making it feel heavy.",
        ], variant_seed)
    return _pick_generic_reply([
        "I’m here with you. I can respond to quick questions like that and also help turn things into a plan. Want me to show your schedule or help rearrange something?",
        "I’m here, and I can do both: talk things through a little and help you get organized. If you'd like, we can figure out one useful next step together.",
    ], variant_seed)


def _infer_replan_summary(message: str) -> tuple[str | None, str | None]:
    """
    Try to extract a target phrase and time for replan confirmation prompts.
    Returns (target_phrase, time_str).
    """
    text = (message or "").strip()
    lower = text.lower()
    if not text:
        return None, None
    m = re.search(
        r"\b(?:move|change|shift|reschedul|update|adjust)\b\s+(?:the\s+)?(.+?)\s+(?:to|till|until|by)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
        lower,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # Fallback: detect a time mention without a clear target phrase.
    time_match = re.search(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", lower)
    return None, time_match.group(1).strip() if time_match else None


def _parse_replan_request(message: str) -> dict:
    text = (message or "").strip()
    lower = text.lower()
    if not text:
        return {
            "target_phrase": None,
            "requested_start_time": None,
            "requested_end_time": None,
            "requested_date": None,
            "direction": None,
            "time_str": None,
        }

    requested_date = extract_date_from_text(text) if _has_explicit_date_cue(text) else None
    direction = None
    if any(k in lower for k in ["earlier", "before", "ahead"]):
        direction = "earlier"
    elif any(k in lower for k in ["later", "after", "postpone", "push"]):
        direction = "later"

    target_phrase = None
    target_match = re.search(
        r"\b(?:move|change|shift|reschedul|update|adjust|set)\b\s+(?:the\s+)?(.+?)\s+(?:to|till|until|by|from)\b",
        lower,
        flags=re.IGNORECASE,
    )
    if target_match:
        target_phrase = target_match.group(1).strip()

    end_match = re.search(
        r"\b(.+?)\s+(?:end|ends|ending|finish|finishes)\s+(?:at|by)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
        lower,
        flags=re.IGNORECASE,
    )
    if end_match and not target_phrase:
        target_phrase = end_match.group(1).strip()
    if end_match:
        requested_end = _parse_clock_token(end_match.group(2))

    end_direct = re.search(
        r"\bend\s+(?:the\s+)?(.+?)\s+(?:at|by)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
        lower,
        flags=re.IGNORECASE,
    )
    if end_direct:
        target_phrase = end_direct.group(1).strip()
        requested_end = _parse_clock_token(end_direct.group(2))

    range_start, range_end = _extract_time_range(lower)
    requested_start = None
    requested_end = None
    if range_start and range_end and re.search(r"\b(move|change|shift|reschedul|update|adjust|set)\b", lower):
        requested_start = range_start
        requested_end = range_end

    explicit_end_time = None
    if not requested_end:
        end_time_match = re.search(
            r"\b(?:till|until|end|ends?|ending|finish|finishes)\b.*?\b(?:at|by|to)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
            lower,
            flags=re.IGNORECASE,
        )
        if end_time_match:
            explicit_end_time = _parse_clock_token(end_time_match.group(1))
            requested_end = explicit_end_time

    if not requested_start and not requested_end:
        change_match = re.search(
            r"\b(?:to|at)\b\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
            lower,
            flags=re.IGNORECASE,
        )
        if change_match:
            prefer_pm = any(k in lower for k in ["dinner", "party", "event", "meeting", "class", "evening", "night"])
            requested_start = _parse_clock_token(
                f"{change_match.group(1)}:{change_match.group(2) or '00'}{change_match.group(3) or ''}",
                prefer_pm=prefer_pm,
            )

    time_str = None
    if requested_end and not requested_start:
        time_str = requested_end.strftime("%I:%M %p").lstrip("0")
    elif requested_start:
        time_str = requested_start.strftime("%I:%M %p").lstrip("0")

    return {
        "target_phrase": target_phrase,
        "requested_start_time": requested_start,
        "requested_end_time": requested_end,
        "requested_date": requested_date,
        "direction": direction,
        "time_str": time_str,
    }

def _parse_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            out = []
            for item in data:
                try:
                    out.append(int(item))
                except (TypeError, ValueError):
                    continue
            return out
    except Exception:
        return []
    return []


def _dump_ids(ids: list[int]) -> str:
    return json.dumps([int(i) for i in ids])


def _parse_json_obj(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _dump_json_obj(obj: dict | None) -> str | None:
    if not obj:
        return None
    return json.dumps(obj)


def _time_to_str(value: time | None) -> str | None:
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    return None


def resolve_thread_key(chat_thread_id: str | None, thread_date: date) -> str:
    base = (chat_thread_id or "").strip()
    if base:
        return f"{base}:{thread_date.isoformat()}"
    return f"auto:{thread_date.isoformat()}"


def load_thread_state(db: Session, thread_key: str) -> dict:
    rec = db.query(ConversationThread).filter(ConversationThread.thread_key == thread_key).first()
    if not rec:
        return {
            "thread_key": thread_key,
            "chat_thread_id": None,
            "thread_date": None,
            "last_intent_type": None,
            "last_user_message": None,
            "last_created_task_ids": [],
            "last_updated_task_ids": [],
            "last_referenced_task_ids": [],
            "pending_intent_id": None,
            "pending_state_type": None,
            "pending_state": {},
        }
    return {
        "thread_key": rec.thread_key,
        "chat_thread_id": rec.chat_thread_id,
        "thread_date": rec.thread_date,
        "last_intent_type": rec.last_intent_type,
        "last_user_message": rec.last_user_message,
        "last_created_task_ids": _parse_ids(rec.last_created_task_ids),
        "last_updated_task_ids": _parse_ids(rec.last_updated_task_ids),
        "last_referenced_task_ids": _parse_ids(rec.last_referenced_task_ids),
        "pending_intent_id": rec.pending_intent_id,
        "pending_state_type": rec.pending_state_type,
        "pending_state": _parse_json_obj(rec.pending_state_json),
    }


def save_thread_state(db: Session, thread_key: str, payload: dict) -> None:
    rec = db.query(ConversationThread).filter(ConversationThread.thread_key == thread_key).first()
    if not rec:
        rec = ConversationThread(
            thread_key=thread_key,
            thread_date=payload.get("thread_date") or date.today(),
            chat_thread_id=payload.get("chat_thread_id"),
        )
        db.add(rec)

    if payload.get("thread_date"):
        rec.thread_date = payload["thread_date"]
    rec.chat_thread_id = payload.get("chat_thread_id")
    rec.last_intent_type = payload.get("last_intent_type")
    rec.last_user_message = payload.get("last_user_message")
    rec.last_created_task_ids = _dump_ids(payload.get("last_created_task_ids", []))
    rec.last_updated_task_ids = _dump_ids(payload.get("last_updated_task_ids", []))
    rec.last_referenced_task_ids = _dump_ids(payload.get("last_referenced_task_ids", []))
    rec.pending_intent_id = payload.get("pending_intent_id")
    rec.pending_state_type = payload.get("pending_state_type")
    rec.pending_state_json = _dump_json_obj(payload.get("pending_state"))
    rec.updated_at = datetime.utcnow()
    db.commit()


def persist_chat_turn(
    db: Session,
    thread_key: str,
    thread_date: date,
    chat_thread_id: str | None,
    user_message: str,
    response: ChatResponse,
) -> None:
    if not user_message.strip():
        return

    user_row = ConversationMessage(
        thread_key=thread_key,
        thread_date=thread_date,
        chat_thread_id=chat_thread_id,
        role="user",
        message_text=user_message,
        meta_text=None,
        payload_json=None,
    )
    assistant_row = ConversationMessage(
        thread_key=thread_key,
        thread_date=thread_date,
        chat_thread_id=chat_thread_id,
        role="assistant",
        message_text=response.message,
        meta_text=_summarize_response_for_log(response),
        payload_json=json.dumps(response.model_dump(mode="json")),
    )
    db.add(user_row)
    db.add(assistant_row)
    db.commit()


def _summarize_response_for_log(response: ChatResponse) -> str | None:
    parts = []
    if response.created_tasks:
        parts.append(f"{len(response.created_tasks)} created")
    if response.updated_tasks:
        parts.append(f"{len(response.updated_tasks)} updated")
    if response.unscheduled_tasks:
        parts.append(f"{len(response.unscheduled_tasks)} unscheduled")
    return " | ".join(parts) or None


def _thread_summary_for_prompt(db: Session, thread_key: str, thread_state: dict) -> str:
    def _titles_for(ids: list[int]) -> str:
        if not ids:
            return "none"
        limited = ids[:3]
        tasks = db.query(Task).filter(Task.id.in_(limited)).all()
        title_map = {t.id: t.title for t in tasks}
        parts = []
        for i in limited:
            title = title_map.get(i, "missing")
            parts.append(f"{i}:{title}")
        return ", ".join(parts) if parts else "none"

    return (
        f"Thread key: {thread_key}\n"
        f"Last intent: {thread_state.get('last_intent_type') or 'none'}\n"
        f"Last created: {_titles_for(thread_state.get('last_created_task_ids', []))}\n"
        f"Last updated: {_titles_for(thread_state.get('last_updated_task_ids', []))}\n"
        f"Last referenced: {_titles_for(thread_state.get('last_referenced_task_ids', []))}"
    )


def _has_explicit_date_cue(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.search(
            r"\b(today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
            r"next\s+\w+|on\s+\d{1,2}(?:st|nd|rd|th)?|"
            r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _parse_clock_token(token: str, prefer_pm: bool = False) -> time | None:
    if not token:
        return None
    token = token.strip().lower()
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", token)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridian = m.group(3)

    if meridian:
        if meridian == "pm" and hour != 12:
            hour += 12
        elif meridian == "am" and hour == 12:
            hour = 0
    else:
        if prefer_pm and 1 <= hour <= 11:
            hour += 12

    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return time(hour, minute)
    return None


def _extract_time_range(segment: str):
    if not segment:
        return None, None
    text = segment.lower()

    range_match = re.search(
        r"\bfrom\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(?:to|-)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not range_match:
        range_match = re.search(
            r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:to|-)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
            text,
            flags=re.IGNORECASE,
        )
    if not range_match:
        return None, None

    start_raw = range_match.group(1).strip()
    end_raw = range_match.group(2).strip()
    explicit_meridian = ("am" in start_raw or "pm" in start_raw or "am" in end_raw or "pm" in end_raw)

    prefer_pm = False
    if not explicit_meridian:
        if any(k in text for k in ["class", "meeting", "evening", "night", "pm"]):
            prefer_pm = True

    start_t = _parse_clock_token(start_raw, prefer_pm=prefer_pm)
    end_t = _parse_clock_token(end_raw, prefer_pm=prefer_pm)
    if not start_t or not end_t:
        return None, None

    # If ambiguous and end <= start, push end to later by assuming same day progression.
    if datetime.combine(date.today(), end_t) <= datetime.combine(date.today(), start_t):
        end_dt = datetime.combine(date.today(), end_t) + timedelta(hours=12)
        end_t = end_dt.time()
    return start_t, end_t


def title_from_text(text: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip(" .,\n\t"))
    if not clean:
        return "Untitled task"
    words = clean.split(" ")
    short = " ".join(words[:8])
    return short[:1].upper() + short[1:]


def description_from_text(text: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return "Task generated from user request."
    return clean if clean.endswith((".", "!", "?")) else f"{clean}."


def split_tasks_from_message(message: str):
    if not message or not message.strip():
        return []

    def _is_actionable_clause(text: str) -> bool:
        if not text:
            return False
        lower = text.lower()
        # Explicit time/duration cues imply an actionable task.
        if _extract_time_range(lower)[0] or extract_time_from_text(lower):
            return True
        if re.search(r"\b\d+\s*(hour|hours|hr|hrs|min|mins|minute|minutes)\b", lower):
            return True
        # Simple verb heuristics for tasks.
        if re.search(r"\b(go|eat|cook|study|work|rest|sleep|talk|call|meet|attend|exercise|walk|code|coding|practice|read|write|buy|shop|clean|do)\b", lower):
            return True
        return False

    parts = re.split(
        r"(?:\n|;|,|\band then\b|\bthen\b|\bplus\b|\balso\b|\s+and\s+)",
        message,
        flags=re.IGNORECASE,
    )
    candidates = [p.strip(" .\t") for p in parts if p and p.strip(" .\t")]
    candidates = [c for c in candidates if _is_actionable_clause(c)]

    if len(candidates) < 2:
        return []

    tasks = []
    default_date = date.today()
    for segment in candidates:
        start_t, end_t = _extract_time_range(segment)
        if _has_explicit_date_cue(segment):
            task_date = extract_date_from_text(segment)
        else:
            task_date = default_date

        duration_min = None
        if start_t and end_t:
            duration_min = int(
                (
                    datetime.combine(task_date, end_t) -
                    datetime.combine(task_date, start_t)
                ).total_seconds() // 60
            )
            if duration_min <= 0:
                duration_min = 60

        tasks.append(
            {
                "title": title_from_text(segment),
                "description": description_from_text(segment),
                "date": task_date.isoformat(),
                "start_time": start_t.strftime("%H:%M:%S") if start_t else None,
                "end_time": end_t.strftime("%H:%M:%S") if end_t else None,
                "priority": infer_priority(segment),
                "task_type": infer_task_type(segment),
                "total_effort_minutes": duration_min,
                "deadline": None,
                "spread": False,
            }
        )
    return tasks


def extract_time_from_text(text: str):
    if not text:
        return None
    lower = text.lower()

    ampm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, flags=re.IGNORECASE)
    if ampm:
        hour = int(ampm.group(1))
        minute = int(ampm.group(2) or 0)
        meridian = ampm.group(3).lower()
        if meridian == "pm" and hour != 12:
            hour += 12
        if meridian == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)

    hhmm = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if hhmm:
        hour = int(hhmm.group(1))
        minute = int(hhmm.group(2))
        # Ambiguous HH:MM without meridian: prefer PM for common evening/night cues.
        if 1 <= hour <= 7:
            prefer_pm = any(
                cue in lower
                for cue in [
                    "evening",
                    "night",
                    "rest of the night",
                    "after class",
                    "after work",
                    "after",
                    "party",
                    "dinner",
                    "class",
                ]
            )
            if "morning" in lower:
                prefer_pm = False
            if prefer_pm:
                hour += 12
        return time(hour, minute)

    # Bare-hour phrases like "at 7" or "by 9".
    bare = re.search(r"\b(?:at|by|around)\s+(\d{1,2})\b", lower)
    if bare:
        hour = int(bare.group(1))
        if 1 <= hour <= 12:
            # Heuristic: if no AM/PM, prefer PM for common evening/event cues.
            prefer_pm = any(
                k in lower
                for k in ["party", "dinner", "event", "class", "meeting", "evening", "night"]
            ) or (1 <= hour <= 7)
            if "morning" in lower:
                prefer_pm = False
            if "afternoon" in lower:
                prefer_pm = hour != 12

            if prefer_pm and hour != 12:
                hour += 12
            elif not prefer_pm and hour == 12:
                hour = 0
            return time(hour, 0)

    return None


def task_duration_minutes(task: Task) -> int:
    if task.duration_minutes and task.duration_minutes > 0:
        return int(task.duration_minutes)
    if task.start_time and task.end_time:
        return max(
            15,
            int(
                (
                    datetime.combine(task.date, task.end_time)
                    - datetime.combine(task.date, task.start_time)
                ).total_seconds()
                // 60
            ),
        )
    return 60


def build_schedule_context(db: Session, today: date, days: int = 7) -> str:
    end_date = today + timedelta(days=days)
    tasks = (
        db.query(Task)
        .filter(Task.date >= today, Task.date <= end_date, Task.completed == False)
        .order_by(Task.date.asc(), Task.start_time.asc(), Task.priority.desc())
        .limit(30)
        .all()
    )
    if not tasks:
        return "No upcoming tasks."

    lines = []
    for t in tasks:
        st = t.start_time.strftime("%H:%M") if t.start_time else "unscheduled"
        et = t.end_time.strftime("%H:%M") if t.end_time else "unscheduled"
        lines.append(f"- {t.date.isoformat()} | {st}-{et} | {t.title}")
    return "\n".join(lines)


def _is_hard_commitment(task: Task) -> bool:
    text = f"{(task.title or '').lower()} {(task.description or '').lower()}"
    hard_keywords = [
        "class",
        "lecture",
        "meeting",
        "appointment",
        "interview",
        "party",
        "event",
        "wedding",
        "doctor",
        "hospital",
        "flight",
        "train",
        "travel",
        "bus",
        "exam",
        "quiz",
        "test",
        "mandatory",
        "breakfast",
        "lunch",
        "dinner",
        "sleep",
        "bedtime",
        "go to bed",
    ]
    return any(k in text for k in hard_keywords)


def _is_event_like(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in ["quiz", "exam", "test", "class", "meeting", "interview", "presentation"])


def _slot_overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    if end_a == start_b or end_b == start_a:
        return False
    return start_a < end_b and end_a > start_b


def _task_datetime_range(task: Task) -> tuple[datetime | None, datetime | None]:
    if not task.start_time:
        return None, None
    start_dt = datetime.combine(task.date, task.start_time)
    if task.end_time:
        end_dt = datetime.combine(task.date, task.end_time)
    else:
        end_dt = start_dt + timedelta(minutes=task_duration_minutes(task))
    return start_dt, end_dt


def _profile_from_text(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ["leetcode", "coding", "study", "revision", "read", "learn", "project"]):
        return "cognitive"
    if any(k in t for k in ["walk", "workout", "gym", "exercise"]):
        return "physical"
    if any(k in t for k in ["relax", "rest", "break", "meditate", "nap", "call", "talk"]):
        return "recovery"
    return "admin"


def _score_slot(
    slot_start: datetime,
    slot_end: datetime,
    occupied: list[tuple[datetime, datetime]],
    energy: int,
    profile: str,
) -> tuple[float, str]:
    # Distance from nearest busy interval (prefer less fragmentation)
    min_gap = 9999
    for occ_s, occ_e in occupied:
        gap = min(abs(int((slot_start - occ_e).total_seconds() // 60)), abs(int((occ_s - slot_end).total_seconds() // 60)))
        min_gap = min(min_gap, gap)
    if min_gap == 9999:
        min_gap = 120

    hour = slot_start.time().hour
    energy_bonus = 0.0
    reason = "Balanced slot."
    if profile == "cognitive":
        if energy >= 4 and 9 <= hour <= 14:
            energy_bonus = 1.2
            reason = "High-energy hours suit focused work."
        elif energy <= 2 and hour >= 15:
            energy_bonus = 0.8
            reason = "Later slot fits lower-energy pacing."
    elif profile == "recovery":
        if energy <= 2 and hour >= 12:
            energy_bonus = 1.1
            reason = "Recovery task placed in low-energy friendly period."
        elif energy >= 4 and hour >= 18:
            energy_bonus = 0.9
            reason = "Evening slot keeps peak hours free."
    else:
        if 10 <= hour <= 18:
            energy_bonus = 0.7
            reason = "Midday slot fits general tasks."

    fit_bonus = min(1.0, max(0.0, min_gap / 60.0))
    score = round(energy_bonus + fit_bonus, 3)
    return score, reason


def suggest_alternative_slots(
    db: Session,
    task_date: date,
    duration_min: int,
    task_text: str,
    limit: int = 3,
) -> list[dict]:
    day_start = datetime.combine(task_date, time(9, 0))
    day_end = datetime.combine(task_date, time(23, 0))
    now = datetime.now()
    today = now.date()
    if task_date < today:
        return []
    if task_date == today:
        # Start from the next 15-minute boundary so we only suggest future times.
        minute_bucket = ((now.minute // 15) + 1) * 15
        aligned_now = now.replace(second=0, microsecond=0)
        if minute_bucket >= 60:
            aligned_now = aligned_now.replace(minute=0) + timedelta(hours=1)
        else:
            aligned_now = aligned_now.replace(minute=minute_bucket)
        day_start = max(day_start, aligned_now)
    mood = db.query(Mood).filter(Mood.date == task_date).first()
    energy = mood.energy if mood else 3

    day_tasks = (
        db.query(Task)
        .filter(Task.date == task_date, Task.completed == False)
        .all()
    )
    occupied = []
    for t in day_tasks:
        s, e = _task_datetime_range(t)
        if s and e:
            occupied.append((s, e))

    profile = _profile_from_text(task_text)
    suggestions = []
    step = timedelta(minutes=15)
    cur = day_start
    while cur + timedelta(minutes=duration_min) <= day_end:
        slot_start = cur
        slot_end = cur + timedelta(minutes=duration_min)
        conflict = any(_slot_overlaps(slot_start, slot_end, o_s, o_e) for o_s, o_e in occupied)
        if not conflict:
            score, reason = _score_slot(slot_start, slot_end, occupied, energy, profile)
            suggestions.append(
                {
                    "slot_id": f"{task_date.isoformat()}@{slot_start.time().strftime('%H:%M:%S')}",
                    "date": task_date,
                    "start_time": slot_start.time(),
                    "end_time": slot_end.time(),
                    "score": score,
                    "reason": reason,
                }
            )
        cur += step

    suggestions.sort(key=lambda s: (-s["score"], s["start_time"]))
    return suggestions[:limit]


def _suggest_slots_same_day_first(
    db: Session,
    task_date: date | None,
    duration_min: int,
    task_text: str,
    limit: int = 3,
    lookahead_days: int = 7,
) -> list[dict]:
    if not task_date or limit <= 0:
        return []

    suggestions = suggest_alternative_slots(
        db=db,
        task_date=task_date,
        duration_min=duration_min,
        task_text=task_text,
        limit=limit,
    )
    if len(suggestions) >= limit:
        return suggestions[:limit]

    remaining = limit - len(suggestions)
    today = date.today()
    if task_date >= today:
        base_day = task_date
        start_offset = 1
    else:
        base_day = today
        start_offset = 0

    for offset in range(start_offset, start_offset + lookahead_days):
        if remaining <= 0:
            break
        day = base_day + timedelta(days=offset)
        day_suggestions = suggest_alternative_slots(
            db=db,
            task_date=day,
            duration_min=duration_min,
            task_text=task_text,
            limit=remaining,
        )
        if day_suggestions:
            suggestions.extend(day_suggestions)
            remaining = limit - len(suggestions)

    return suggestions[:limit]


def detect_conflicts_for_draft(db: Session, draft: dict) -> tuple[list[Task], list[dict]]:
    task_date = draft.get("date")
    start_t = draft.get("start_time")
    end_t = draft.get("end_time")
    duration_min = int(draft.get("duration_minutes") or 60)
    if not task_date or not start_t:
        return [], []
    if not end_t:
        end_t = (datetime.combine(task_date, start_t) + timedelta(minutes=duration_min)).time()

    start_dt = datetime.combine(task_date, start_t)
    end_dt = datetime.combine(task_date, end_t)
    day_tasks = (
        db.query(Task)
        .filter(Task.date == task_date, Task.completed == False)
        .all()
    )
    conflicts = []
    for task in day_tasks:
        t_start, t_end = _task_datetime_range(task)
        if not t_start or not t_end:
            continue
        if _slot_overlaps(start_dt, end_dt, t_start, t_end):
            conflicts.append(task)
    suggestions = suggest_alternative_slots(
        db,
        task_date=task_date,
        duration_min=duration_min,
        task_text=f"{draft.get('title', '')} {draft.get('description', '')}",
        limit=3,
    ) if conflicts else []
    return conflicts, suggestions


def _build_clarification_prompt(
    intent_id: str,
    field: str,
    question: str,
    options: list[dict] | None = None,
    allow_free_text: bool = True,
) -> ClarificationPrompt:
    prompt_options = []
    for idx, item in enumerate(options or []):
        prompt_options.append(
            ClarificationOption(
                id=str(item.get("id") or f"opt-{idx+1}"),
                label=str(item.get("label") or item.get("value") or f"Option {idx+1}"),
                value=str(item.get("value") or item.get("label") or ""),
            )
        )
    return ClarificationPrompt(
        intent_id=intent_id,
        question=question,
        field=field,
        options=prompt_options,
        allow_free_text=allow_free_text,
    )


def _dynamic_time_options_for_draft(db: Session, draft: dict, limit: int = 3) -> list[dict]:
    task_date = draft.get("date")
    duration = int(draft.get("duration_minutes") or 60)
    text = f"{draft.get('title', '')} {draft.get('description', '')}".strip()
    suggestions = suggest_alternative_slots(
        db=db,
        task_date=task_date,
        duration_min=duration,
        task_text=text,
        limit=limit,
    )
    if suggestions:
        return [
            {
                "id": s["slot_id"],
                "label": s["start_time"].strftime("%I:%M %p").lstrip("0"),
                "value": s["start_time"].strftime("%H:%M"),
            }
            for s in suggestions
        ]

    # Fallback: propose near-future times on the same day at 15-min grid.
    now = datetime.now()
    base = datetime.combine(task_date, time(9, 0))
    if task_date == now.date():
        minute_bucket = ((now.minute // 15) + 1) * 15
        aligned_now = now.replace(second=0, microsecond=0)
        if minute_bucket >= 60:
            aligned_now = aligned_now.replace(minute=0) + timedelta(hours=1)
        else:
            aligned_now = aligned_now.replace(minute=minute_bucket)
        base = max(base, aligned_now)

    out = []
    cur = base
    while len(out) < limit and cur.time() <= time(23, 0):
        out.append(
            {
                "id": f"fallback-{cur.strftime('%H%M')}",
                "label": cur.strftime("%I:%M %p").lstrip("0"),
                "value": cur.strftime("%H:%M"),
            }
        )
        cur += timedelta(minutes=15)
    return out


def handle_followup_replan(
    message: str,
    db: Session,
    thread_state: dict | None = None,
    reference_date: date | None = None,
    return_metadata: bool = False,
    dry_run: bool = False,
):
    today = reference_date or date.today()
    text = (message or "").lower()
    if not text.strip():
        return ([], {"memory_used": False, "referenced_task_ids": []}) if return_metadata else []
    parsed = _parse_replan_request(message)
    requested_start = parsed.get("requested_start_time")
    requested_end = parsed.get("requested_end_time")
    requested_date = parsed.get("requested_date") or today
    target_phrase = parsed.get("target_phrase")
    direction = parsed.get("direction")
    time_str = parsed.get("time_str")
    has_explicit_date_in_message = _has_explicit_date_cue(message)

    memory_order = []
    if thread_state:
        memory_order = (
            thread_state.get("last_referenced_task_ids", [])
            + thread_state.get("last_updated_task_ids", [])
            + thread_state.get("last_created_task_ids", [])
        )
    seen_memory = set()
    ordered_memory_ids = []
    for tid in memory_order:
        try:
            tid_int = int(tid)
        except (TypeError, ValueError):
            continue
        if tid_int in seen_memory:
            continue
        seen_memory.add(tid_int)
        ordered_memory_ids.append(tid_int)

    upcoming = (
        db.query(Task)
        .filter(Task.date >= today, Task.completed == False)
        .order_by(Task.date.asc(), Task.start_time.asc(), Task.priority.desc())
        .all()
    )
    memory_tasks = []
    if ordered_memory_ids:
        memory_rows = (
            db.query(Task)
            .filter(Task.id.in_(ordered_memory_ids), Task.completed == False)
            .all()
        )
        by_id = {t.id: t for t in memory_rows}
        memory_tasks = [by_id[i] for i in ordered_memory_ids if i in by_id]

    pool_by_id = {}
    for t in upcoming + memory_tasks:
        pool_by_id[t.id] = t
    candidate_tasks = list(pool_by_id.values())

    if not candidate_tasks:
        return ([], {"memory_used": False, "referenced_task_ids": []}) if return_metadata else []

    meta = {
        "memory_used": False,
        "referenced_task_ids": [],
        "needs_clarification": False,
        "candidate_task_ids": [],
        "target_phrase": target_phrase,
        "time_str": time_str,
        "requested_start_time": requested_start,
        "requested_end_time": requested_end,
        "requested_date": requested_date,
        "ready_to_apply": False,
    }

    def _tokenize(s: str) -> set[str]:
        tokens = set()
        for w in re.findall(r"[a-z0-9]+", (s or "").lower()):
            if len(w) <= 1 or w in STOP_WORDS:
                continue
            tokens.add(w)
            # lightweight normalization for gerunds/plurals to improve matching.
            if len(w) > 4 and w.endswith("ing"):
                tokens.add(w[:-3])
            if len(w) > 3 and w.endswith("s"):
                tokens.add(w[:-1])
        return tokens

    def _task_tokens(task: Task) -> set[str]:
        return _tokenize(f"{task.title or ''} {task.description or ''}")

    def _title_tokens(task: Task) -> set[str]:
        return _tokenize(task.title or "")

    def _score_task(task: Task, query_tokens: set[str]) -> float:
        if not query_tokens:
            return 0.0
        tt = _task_tokens(task)
        overlap = query_tokens.intersection(tt)
        if not overlap:
            return 0.0
        score = float(len(overlap) * 2) + (len(overlap) / max(1, len(query_tokens)))
        if task.start_time:
            score += 0.2
        if "session" in tt and "session" not in query_tokens:
            score -= 0.25
        return score

    def _pick_best_task(tasks: List[Task], query_tokens: set[str]):
        scored = []
        for t in tasks:
            s = _score_task(t, query_tokens)
            if s > 0:
                scored.append((s, t))
        if not scored:
            return None
        scored.sort(
            key=lambda x: (
                -x[0],
                x[1].date,
                x[1].start_time or time(23, 59),
                -x[1].priority,
                x[1].id,
            )
        )
        return scored[0][1]

    def _pick_best_task_for_explicit_target(tasks: List[Task], query_tokens: set[str]):
        if not query_tokens:
            return None
        scored = []
        for t in tasks:
            title_overlap = len(query_tokens.intersection(_title_tokens(t)))
            all_overlap = len(query_tokens.intersection(_task_tokens(t)))
            if title_overlap == 0 and all_overlap == 0:
                continue
            # Prefer title matches strongly over description-only overlaps.
            score = float(title_overlap * 4 + all_overlap)
            scored.append((score, t))
        if not scored:
            return None
        scored.sort(
            key=lambda x: (
                -x[0],
                x[1].date,
                x[1].start_time or time(23, 59),
                -x[1].priority,
                x[1].id,
            )
        )
        return scored[0][1]

    def _extract_change_to_time(raw_text: str, anchor_task: Task | None):
        """
        Parse phrases like "change that to 12" or "move it to 14:30".
        """
        if not raw_text:
            return None
        m = re.search(
            r"\b(?:change|move|set|shift)\b.*?\bto\b\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
            raw_text,
            flags=re.IGNORECASE,
        )
        if not m:
            return None

        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        meridian = (m.group(3) or "").lower() or None

        if not (0 <= minute <= 59):
            return None

        if meridian:
            if meridian == "pm" and hour != 12:
                hour += 12
            elif meridian == "am" and hour == 12:
                hour = 0
        else:
            # Inherit AM/PM tendency from anchor if possible.
            if anchor_task and anchor_task.start_time:
                if anchor_task.start_time.hour >= 12 and 1 <= hour <= 11:
                    hour += 12
            else:
                # Event-like cues default to evening when ambiguous.
                if any(k in raw_text.lower() for k in ["party", "dinner", "event", "class", "meeting"]) and 1 <= hour <= 11:
                    hour += 12

        if 0 <= hour <= 23:
            return time(hour, minute)
        return None

    explicit_target_tokens = _tokenize(target_phrase) if target_phrase else set()
    message_tokens = _tokenize(text)

    def _substring_match(tasks: List[Task], phrase: str | None):
        if not phrase:
            return None, []
        phrase_l = phrase.lower().strip()
        if not phrase_l:
            return None, []
        matches = []
        for t in tasks:
            title_l = (t.title or "").lower()
            if not title_l:
                continue
            if phrase_l in title_l or title_l in text:
                matches.append(t)
        if len(matches) == 1:
            return matches[0], matches
        return None, matches

    def _best_match(tasks: List[Task], tokens: set[str]) -> tuple[Task | None, list[Task]]:
        if not tokens:
            return None, []
        scored = []
        for t in tasks:
            title_overlap = len(tokens.intersection(_title_tokens(t)))
            all_overlap = len(tokens.intersection(_task_tokens(t)))
            if title_overlap == 0 and all_overlap == 0:
                continue
            score = float(title_overlap * 4 + all_overlap)
            scored.append((score, t))
        if not scored:
            return None, []
        scored.sort(key=lambda x: (-x[0], x[1].date, x[1].start_time or time(23, 59), x[1].id))
        best_score = scored[0][0]
        best = [t for s, t in scored if s == best_score]
        if len(best) == 1:
            return best[0], best
        return None, best

    anchor = None
    ambiguous_candidates = []
    if target_phrase:
        anchor, ambiguous_candidates = _substring_match(candidate_tasks, target_phrase)
        if not anchor and not ambiguous_candidates and explicit_target_tokens:
            anchor, ambiguous_candidates = _best_match(candidate_tasks, explicit_target_tokens)
    elif message_tokens:
        # Fallback: see if any title tokens appear verbatim in the message.
        anchor, ambiguous_candidates = _best_match(candidate_tasks, message_tokens)

    if not anchor and thread_state:
        id_to_task = {t.id: t for t in candidate_tasks}
        for tid in memory_order:
            candidate = id_to_task.get(tid)
            if candidate:
                anchor = candidate
                meta["memory_used"] = True
                break

    if not anchor and requested_start:
        candidates = [
            t for t in candidate_tasks
            if t.start_time and t.start_time == requested_start and t.date == requested_date
        ]
        if len(candidates) == 1:
            anchor = candidates[0]
        elif len(candidates) > 1:
            ambiguous_candidates = candidates

    if not anchor and re.search(r"\b(it|that|this|one)\b", text):
        anchor = max(candidate_tasks, key=lambda t: t.id)

    if ambiguous_candidates and not anchor:
        meta["needs_clarification"] = True
        meta["candidate_task_ids"] = [t.id for t in ambiguous_candidates][:5]
        return ([], meta) if return_metadata else []

    if not anchor:
        meta["needs_clarification"] = True
        return ([], meta) if return_metadata else []

    # Safety: for follow-up edits without explicit date, avoid mutating a different day by accident.
    if anchor and (not has_explicit_date_in_message) and anchor.date != today:
        meta["needs_clarification"] = True
        return ([], meta) if return_metadata else []

    if not requested_start and not requested_end:
        meta["needs_clarification"] = True
        return ([], meta) if return_metadata else []

    if direction and not (requested_start or requested_end):
        meta["needs_clarification"] = True
        return ([], meta) if return_metadata else []

    meta["ready_to_apply"] = True
    if dry_run:
        return ([], meta) if return_metadata else []

    target_date = anchor.date
    # Snapshot day state so we can return only changed tasks.
    before_day_tasks = (
        db.query(Task)
        .filter(Task.date == target_date, Task.completed == False)
        .all()
    )
    before_state = {t.id: (t.date, t.start_time, t.end_time) for t in before_day_tasks}

    changed_ids = set()

    duration = task_duration_minutes(anchor)
    if requested_start and requested_end:
        anchor.start_time = requested_start
        anchor.end_time = requested_end
    elif requested_end and anchor.start_time:
        candidate_end = requested_end
        if datetime.combine(anchor.date, candidate_end) <= datetime.combine(anchor.date, anchor.start_time):
            candidate_end = (
                datetime.combine(anchor.date, candidate_end) + timedelta(hours=12)
            ).time()
        if datetime.combine(anchor.date, candidate_end) <= datetime.combine(anchor.date, anchor.start_time):
            candidate_end = (
                datetime.combine(anchor.date, anchor.start_time) + timedelta(minutes=15)
            ).time()
        anchor.end_time = candidate_end
    elif requested_start:
        anchor.start_time = requested_start
        anchor.end_time = (
            datetime.combine(anchor.date, requested_start) + timedelta(minutes=duration)
        ).time()
    changed_ids.add(anchor.id)
    meta["referenced_task_ids"].append(anchor.id)

    def _dt_range(task: Task):
        if not task.start_time:
            return None, None
        s = datetime.combine(task.date, task.start_time)
        if task.end_time:
            e = datetime.combine(task.date, task.end_time)
        else:
            e = s + timedelta(minutes=task_duration_minutes(task))
        if e <= s:
            e = s + timedelta(minutes=max(15, task_duration_minutes(task)))
        return s, e

    changed_tasks = [t for t in before_day_tasks if t.id in changed_ids]
    conflicts = set()
    for changed_task in changed_tasks:
        c_start, c_end = _dt_range(changed_task)
        if not c_start or not c_end:
            continue
        for other in before_day_tasks:
            if other.id == changed_task.id:
                continue
            o_start, o_end = _dt_range(other)
            if not o_start or not o_end:
                continue
            if _slot_overlaps(c_start, c_end, o_start, o_end):
                conflicts.add(other.id)

    if conflicts:
        proposed_start = anchor.start_time
        proposed_end = anchor.end_time
        db.rollback()
        meta.update(
            {
                "conflict_task_id": anchor.id,
                "conflicting_ids": sorted(list(conflicts)),
                "proposed_start_time": proposed_start,
                "proposed_end_time": proposed_end,
                "duration_minutes": task_duration_minutes(anchor),
            }
        )
        if return_metadata:
            return [], meta
        return []

    db.commit()

    refreshed = (
        db.query(Task)
        .filter(Task.date == target_date, Task.completed == False)
        .order_by(Task.start_time.asc(), Task.priority.desc())
        .all()
    )
    changed = []
    for task in refreshed:
        after = (task.date, task.start_time, task.end_time)
        before = before_state.get(task.id)
        if before != after:
            changed.append(task)

    if not meta["referenced_task_ids"]:
        meta["referenced_task_ids"] = [t.id for t in changed][:10]
    if return_metadata:
        return changed, meta
    return changed


def _build_response(
    mode: str,
    message: str,
    created_tasks=None,
    updated_tasks=None,
    unchanged_tasks=None,
    used_fallback_parser: bool = False,
    used_replan_handler: bool = False,
    resolved_thread_key: str | None = None,
    memory_used: bool = False,
    requires_user_input: bool = False,
    pending_intent_id: str | None = None,
    applied_after_confirmation: bool = False,
    affected_dates=None,
    warnings=None,
    clarification: ClarificationPrompt | None = None,
    conflict_info: ConflictPrompt | None = None,
) -> ChatResponse:
    created_tasks = created_tasks or []
    updated_tasks = updated_tasks or []
    unchanged_tasks = unchanged_tasks or []
    affected_dates = sorted(list(affected_dates or []))
    warnings = warnings or []

    created_ids = {t.id for t in created_tasks}
    updated_ids = {t.id for t in updated_tasks}
    unscheduled = []
    unscheduled_ids = set()
    for task in created_tasks + updated_tasks + unchanged_tasks:
        if task.id in unscheduled_ids:
            continue
        if (not task.start_time) or (not task.end_time):
            unscheduled.append(task)
            unscheduled_ids.add(task.id)

    unchanged_filtered = [t for t in unchanged_tasks if t.id not in created_ids and t.id not in updated_ids]

    return ChatResponse(
        mode=mode,
        message=message,
        created_tasks=created_tasks,
        updated_tasks=updated_tasks,
        unchanged_tasks=unchanged_filtered,
        unscheduled_tasks=unscheduled,
        clarification=clarification,
        conflict_info=conflict_info,
        meta=ChatMeta(
            used_fallback_parser=used_fallback_parser,
            used_replan_handler=used_replan_handler,
            resolved_thread_key=resolved_thread_key,
            memory_used=memory_used,
            requires_user_input=requires_user_input,
            pending_intent_id=pending_intent_id,
            applied_after_confirmation=applied_after_confirmation,
            affected_dates=affected_dates,
            warnings=warnings,
        ),
    )


def _collect_updated_tasks_for_dates(db: Session, before_state_by_date, dates):
    updated = []
    seen = set()
    for d in dates:
        before_state = before_state_by_date.get(d, {})
        refreshed = (
            db.query(Task)
            .filter(Task.date == d, Task.completed == False)
            .order_by(Task.start_time.asc(), Task.priority.desc(), Task.id.asc())
            .all()
        )
        for task in refreshed:
            if task.id not in before_state:
                continue
            before = before_state[task.id]
            after = (task.date, task.start_time, task.end_time)
            if before != after and task.id not in seen:
                updated.append(task)
                seen.add(task.id)
    return updated


def _clear_pending_state(
    db: Session,
    thread_key: str,
    thread_date: date,
    chat_thread_id: str | None,
    thread_state: dict,
    last_intent_type: str,
    last_user_message: str,
) -> None:
    save_thread_state(
        db,
        thread_key,
        {
            "thread_date": thread_date,
            "chat_thread_id": chat_thread_id,
            "last_intent_type": last_intent_type,
            "last_user_message": last_user_message,
            "last_created_task_ids": thread_state.get("last_created_task_ids", []),
            "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
            "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
            "pending_intent_id": None,
            "pending_state_type": None,
            "pending_state": {},
        },
    )


def _normalize_task_draft(
    args: dict,
    message: str,
    effective_date: date,
    db: Session,
) -> dict:
    raw_text = args.get("title") or args.get("description") or message
    title = title_from_text(raw_text)
    description = description_from_text(args.get("description") or raw_text)
    priority = int(args.get("priority", 1))
    task_type = args.get("task_type", "other")
    total_effort = args.get("total_effort_minutes")
    deadline_str = args.get("deadline")

    task_date = None
    if args.get("date"):
        try:
            task_date = datetime.strptime(args.get("date"), "%Y-%m-%d").date()
        except Exception:
            task_date = effective_date
    else:
        task_date = effective_date

    start_time_val = None
    end_time_val = None
    start_time_str = args.get("start_time")
    end_time_str = args.get("end_time")
    if isinstance(start_time_str, str) and start_time_str.strip():
        start_time_val = parse_time(start_time_str.strip())
    if isinstance(end_time_str, str) and end_time_str.strip():
        end_time_val = parse_time(end_time_str.strip())
    # If the text implies an end time (e.g. "until 5pm"), treat it as end_time even if
    # the model supplied it as start_time.
    end_match = re.search(
        r"\b(?:till|until|end|ends?|ending|finish|finishes|by)\b.*?\b(?:at|by|to)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
        raw_text,
        flags=re.IGNORECASE,
    )
    if end_match and not end_time_val:
        end_time_val = _parse_clock_token(end_match.group(1))
        if start_time_val and start_time_val == end_time_val:
            start_time_val = None
    # If the text includes an explicit range ("from 2 to 4"), honor it.
    if not start_time_val and not end_time_val:
        range_start, range_end = _extract_time_range(raw_text.lower())
        if range_start and range_end:
            start_time_val = range_start
            end_time_val = range_end
    if not start_time_val and not end_time_val:
        # Do not use full message for draft-time extraction; it can leak unrelated times.
        start_time_val = extract_time_from_text(
            f"{args.get('title') or ''} {args.get('description') or ''}"
        )

    deadline_date = None
    if deadline_str:
        try:
            deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d").date()
        except Exception:
            deadline_date = None

    if not total_effort:
        mood = db.query(Mood).filter(Mood.date == task_date).first()
        energy = mood.energy if mood else 3
        historical_avg = get_historical_avg_duration(db, task_type, energy)
        total_effort = infer_duration_minutes(
            text=title,
            task_type=task_type,
            energy_level=energy,
            historical_avg=historical_avg,
            deadline_date=deadline_date,
        )
    duration_minutes = int(total_effort or 60)
    if end_time_val and not start_time_val:
        # If only end time is given, back-calc start using inferred duration.
        start_dt = datetime.combine(task_date, end_time_val) - timedelta(minutes=duration_minutes)
        start_time_val = start_dt.time()
    if start_time_val and not end_time_val:
        end_time_val = (datetime.combine(task_date, start_time_val) + timedelta(minutes=duration_minutes)).time()

    return {
        "title": title,
        "description": description,
        "date": task_date,
        "start_time": start_time_val,
        "end_time": end_time_val,
        "priority": priority,
        "duration_minutes": duration_minutes,
        "task_type": task_type,
    }


def _build_conflict_prompt(
    intent_id: str,
    draft: dict,
    conflicts: list[Task],
    suggestions: list[dict],
    actions: list[str] | None = None,
) -> ConflictPrompt:
    conflict_items = [
        ConflictTaskRef(
            task_id=t.id,
            title=t.title,
            date=t.date,
            start_time=t.start_time,
            end_time=t.end_time,
            is_fixed=_is_hard_commitment(t),
        )
        for t in conflicts
    ]
    suggestion_items = [
        SlotSuggestion(
            slot_id=s["slot_id"],
            date=s["date"],
            start_time=s["start_time"],
            end_time=s["end_time"],
            score=float(s["score"]),
            reason=s["reason"],
        )
        for s in suggestions[:3]
    ]
    draft_payload = {
        "title": draft.get("title"),
        "description": draft.get("description"),
        "date": draft.get("date").isoformat() if draft.get("date") else None,
        "start_time": draft.get("start_time").strftime("%H:%M:%S") if draft.get("start_time") else None,
        "end_time": draft.get("end_time").strftime("%H:%M:%S") if draft.get("end_time") else None,
        "duration_minutes": draft.get("duration_minutes"),
        "priority": draft.get("priority"),
    }
    return ConflictPrompt(
        intent_id=intent_id,
        new_task_draft=draft_payload,
        conflicting_tasks=conflict_items,
        suggested_slots=suggestion_items,
        actions=actions or [
            "choose_slot:<slot_id>",
            "keep_original_and_move_conflicts",
            "cancel",
        ],
    )

def _format_task_choice_label(task: Task) -> str:
    start = task.start_time.strftime("%H:%M") if task.start_time else "unscheduled"
    end = task.end_time.strftime("%H:%M") if task.end_time else "unscheduled"
    return f"{task.title} ({task.date.isoformat()} {start}-{end})"


def _format_slot_choice_label(slot: dict) -> str:
    date_str = slot["date"].isoformat() if isinstance(slot.get("date"), date) else str(slot.get("date"))
    start = slot["start_time"].strftime("%H:%M") if slot.get("start_time") else ""
    end = slot["end_time"].strftime("%H:%M") if slot.get("end_time") else ""
    return f"{date_str} {start}-{end}".strip()


def _build_choice_map(choices: list[tuple[str, str]]) -> dict[str, str]:
    return {str(i + 1): action for i, (_label, action) in enumerate(choices)}


def _format_choice_message(prefix: str, choices: list[tuple[str, str]]) -> str:
    lines = [prefix, "Options:"]
    for i, (label, _action) in enumerate(choices, 1):
        lines.append(f"{i}. {label}")
    lines.append("Reply with the option number.")
    return "\n".join(lines)


def _parse_pending_action(message: str, pending_response: dict | None, pending_state: dict | None = None) -> str | None:
    if pending_response and isinstance(pending_response, dict):
        raw_action = pending_response.get("action")
        if isinstance(raw_action, str) and raw_action.strip():
            raw_action = raw_action.strip()
            if raw_action.isdigit() and pending_state:
                choice_map = pending_state.get("choice_map") or {}
                mapped = choice_map.get(raw_action)
                if mapped:
                    return mapped
        raw_value = pending_response.get("value")
        if isinstance(raw_value, str) and raw_value.strip() and pending_state:
            choice_map = pending_state.get("choice_map") or {}
            mapped = choice_map.get(raw_value.strip())
            if mapped:
                return mapped
    text = (message or "").strip().lower()
    if not text:
        return None
    if pending_state:
        choice_map = pending_state.get("choice_map") or {}
        m = re.search(r"\b(\d+)\b", text)
        if m:
            mapped = choice_map.get(m.group(1))
            if mapped:
                return mapped
    return None


def handle_pending_resolution(
    message: str,
    pending_response: dict | None,
    confirm: bool | None,
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
) -> ChatResponse | None:
    pending_type = thread_state.get("pending_state_type")
    pending_state = thread_state.get("pending_state") or {}
    pending_intent_id = thread_state.get("pending_intent_id")
    if not pending_type or not pending_intent_id:
        return None

    # Clarification resolution: only date and duration for now.
    if pending_type == "clarification":
        action_value = None
        action_from_payload = False
        if pending_response and isinstance(pending_response, dict):
            action_value = pending_response.get("value")
            action_from_payload = bool(action_value)
        if not action_value and message:
            action_value = message.strip()
        if isinstance(action_value, str):
            choice_map = pending_state.get("choice_map") or {}
            mapped = choice_map.get(action_value.strip())
            if mapped:
                action_value = mapped
        if not action_value:
            prompt = _build_clarification_prompt(
                intent_id=pending_intent_id,
                field=pending_state.get("field", "date"),
                question=pending_state.get("question", "Please provide the missing value."),
                options=pending_state.get("options", []),
            )
            return _build_response(
                mode="ask_user",
                message="I still need your answer to continue.",
                resolved_thread_key=thread_key,
                requires_user_input=True,
                pending_intent_id=pending_intent_id,
                clarification=prompt,
                warnings=["Pending clarification is unresolved."],
            )
        draft = pending_state.get("draft") or {}
        field = pending_state.get("field")
        if pending_state.get("mode") == "intent_choice":
            selected = str(action_value).strip().lower()
            if selected == "intent:get_schedule":
                requested_schedule_date = extract_date_from_text(message) if _has_explicit_date_cue(message) else effective_thread_date
                tasks = get_schedule(requested_schedule_date, db)
                save_thread_state(
                    db,
                    thread_key,
                    {
                        "thread_date": requested_schedule_date,
                        "chat_thread_id": chat_thread_id,
                        "last_intent_type": "schedule",
                        "last_user_message": message,
                        "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                        "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                        "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                        "pending_intent_id": None,
                        "pending_state_type": None,
                        "pending_state": {},
                    },
                )
                return _build_response(
                    mode="schedule",
                    message="Here is your schedule for the day.",
                    unchanged_tasks=tasks,
                    resolved_thread_key=thread_key,
                    affected_dates={requested_schedule_date},
                )
            if selected == "intent:cancel":
                _clear_pending_state(
                    db,
                    thread_key=thread_key,
                    thread_date=effective_thread_date,
                    chat_thread_id=chat_thread_id,
                    thread_state=thread_state,
                    last_intent_type="cancel",
                    last_user_message=message,
                )
                return _build_response(
                    mode="schedule",
                    message="Okay, canceled.",
                    resolved_thread_key=thread_key,
                )
            prompt = _build_clarification_prompt(
                intent_id=pending_intent_id,
                field=field or "intent",
                question=pending_state.get("question", "Choose an option."),
                options=pending_state.get("options", []),
                allow_free_text=False,
            )
            return _build_response(
                mode="ask_user",
                message="Please choose one of the numbered options.",
                resolved_thread_key=thread_key,
                requires_user_input=True,
                pending_intent_id=pending_intent_id,
                clarification=prompt,
            )
        if pending_state.get("mode") == "multi_create":
            drafts = pending_state.get("drafts") or []
            pending_index = int(pending_state.get("pending_index") or 0)
            awaiting_free_time = bool(pending_state.get("awaiting_free_time"))
            if pending_index < 0 or pending_index >= len(drafts):
                pending_index = 0

            if awaiting_free_time:
                parsed_time = extract_time_from_text(action_value) if isinstance(action_value, str) else None
                if not parsed_time:
                    return _build_multi_create_prompt(
                        db=db,
                        thread_key=thread_key,
                        thread_state=thread_state,
                        effective_thread_date=effective_thread_date,
                        chat_thread_id=chat_thread_id,
                        message=message,
                        drafts=drafts,
                        pending_index=pending_index,
                        awaiting_free_time=True,
                    )
            else:
                if isinstance(action_value, str) and action_value.strip().lower() == "other_time":
                    return _build_multi_create_prompt(
                        db=db,
                        thread_key=thread_key,
                        thread_state=thread_state,
                        effective_thread_date=effective_thread_date,
                        chat_thread_id=chat_thread_id,
                        message=message,
                        drafts=drafts,
                        pending_index=pending_index,
                        awaiting_free_time=True,
                    )
                parsed_time = None
                if isinstance(action_value, str):
                    parsed_time = extract_time_from_text(action_value)
                    if not parsed_time and action_from_payload:
                        try:
                            parsed_time = parse_time(action_value)
                        except Exception:
                            parsed_time = None
                if not parsed_time:
                    return _build_multi_create_prompt(
                        db=db,
                        thread_key=thread_key,
                        thread_state=thread_state,
                        effective_thread_date=effective_thread_date,
                        chat_thread_id=chat_thread_id,
                        message=message,
                        drafts=drafts,
                        pending_index=pending_index,
                        awaiting_free_time=False,
                    )

            current = drafts[pending_index]
            task_date = datetime.strptime(current["date"], "%Y-%m-%d").date()
            duration = int(current.get("duration_minutes") or 60)
            end_time = (datetime.combine(task_date, parsed_time) + timedelta(minutes=duration)).time()
            if current.get("min_start_time"):
                try:
                    min_start = parse_time(current["min_start_time"])
                    if parsed_time < min_start:
                        return _build_multi_create_prompt(
                            db=db,
                            thread_key=thread_key,
                            thread_state=thread_state,
                            effective_thread_date=effective_thread_date,
                            chat_thread_id=chat_thread_id,
                            message=message,
                            drafts=drafts,
                            pending_index=pending_index,
                        )
                except Exception:
                    pass
            current["start_time"] = parsed_time.strftime("%H:%M:%S")
            current["end_time"] = end_time.strftime("%H:%M:%S")
            drafts[pending_index] = current

            # Check conflict for chosen time
            conflicts, suggestions = detect_conflicts_for_draft(
                db,
                {
                    "title": current.get("title"),
                    "description": current.get("description"),
                    "date": task_date,
                    "start_time": parsed_time,
                    "end_time": end_time,
                    "duration_minutes": duration,
                    "priority": int(current.get("priority") or 1),
                },
            )
            if conflicts:
                pending_intent_id = str(uuid.uuid4())
                save_thread_state(
                    db,
                    thread_key,
                    {
                        "thread_date": effective_thread_date,
                        "chat_thread_id": chat_thread_id,
                        "last_intent_type": "conflict",
                        "last_user_message": message,
                        "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                        "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                        "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                        "pending_intent_id": pending_intent_id,
                        "pending_state_type": "conflict_resolution",
                        "pending_state": {
                            "mode": "multi_create_conflict",
                            "drafts": drafts,
                            "draft_index": pending_index,
                            "pending_index": pending_index,
                            "draft": current,
                            "conflict_ids": [t.id for t in conflicts],
                            "suggestions": [
                                {
                                    "slot_id": s["slot_id"],
                                    "date": s["date"].isoformat(),
                                    "start_time": s["start_time"].strftime("%H:%M:%S"),
                                    "end_time": s["end_time"].strftime("%H:%M:%S"),
                                    "score": float(s["score"]),
                                    "reason": s["reason"],
                                }
                                for s in suggestions
                            ],
                        },
                    },
                )
                return _build_response(
                    mode="conflict",
                    message="That time conflicts with an existing task. Choose a different slot.",
                    resolved_thread_key=thread_key,
                    requires_user_input=True,
                    pending_intent_id=pending_intent_id,
                    conflict_info=_build_conflict_prompt(
                        intent_id=pending_intent_id,
                        draft={
                            "title": current.get("title"),
                            "description": current.get("description"),
                            "date": task_date,
                            "start_time": parsed_time,
                            "end_time": end_time,
                            "duration_minutes": duration,
                            "priority": int(current.get("priority") or 1),
                        },
                        conflicts=conflicts,
                        suggestions=suggestions,
                        actions=["choose_slot:<slot_id>", "cancel"],
                    ),
                )

            # Advance to next draft missing time.
            next_index = pending_index + 1
            while next_index < len(drafts):
                if not drafts[next_index].get("start_time"):
                    break
                next_index += 1
            if next_index < len(drafts):
                return _build_multi_create_prompt(
                    db=db,
                    thread_key=thread_key,
                    thread_state=thread_state,
                    effective_thread_date=effective_thread_date,
                    chat_thread_id=chat_thread_id,
                    message=message,
                    drafts=drafts,
                    pending_index=next_index,
                )
            return _finalize_multi_create(
                db=db,
                thread_key=thread_key,
                thread_state=thread_state,
                effective_thread_date=effective_thread_date,
                chat_thread_id=chat_thread_id,
                message=message,
                drafts=drafts,
            )
        if pending_state.get("mode") == "delete":
            candidate_ids = pending_state.get("candidate_task_ids") or []
            if isinstance(action_value, str) and action_value.strip().lower() == "cancel":
                _clear_pending_state(
                    db,
                    thread_key=thread_key,
                    thread_date=effective_thread_date,
                    chat_thread_id=chat_thread_id,
                    thread_state=thread_state,
                    last_intent_type="cancel",
                    last_user_message=message,
                )
                return _build_response(
                    mode="schedule",
                    message="Okay, I canceled the deletion.",
                    resolved_thread_key=thread_key,
                )

            chosen_id = None
            if isinstance(action_value, str):
                m = re.search(r"delete_task:([0-9]+)", action_value, flags=re.IGNORECASE)
                if m:
                    chosen_id = int(m.group(1))
                elif action_value.isdigit():
                    chosen_id = int(action_value)
            if not chosen_id and len(candidate_ids) == 1:
                chosen_id = int(candidate_ids[0])
            if not chosen_id or (candidate_ids and chosen_id not in candidate_ids):
                prompt = _build_clarification_prompt(
                    intent_id=pending_intent_id,
                    field="target_task",
                    question=pending_state.get("question", "Which task should I delete?"),
                    options=pending_state.get("options", []),
                )
                return _build_response(
                    mode="ask_user",
                    message="Please reply with the option number for the task you want to delete.",
                    resolved_thread_key=thread_key,
                    requires_user_input=True,
                    pending_intent_id=pending_intent_id,
                    clarification=prompt,
                )

            target = db.query(Task).filter(Task.id == chosen_id).first()
            if not target:
                _clear_pending_state(
                    db,
                    thread_key=thread_key,
                    thread_date=effective_thread_date,
                    chat_thread_id=chat_thread_id,
                    thread_state=thread_state,
                    last_intent_type="pending_invalid",
                    last_user_message=message,
                )
                return _build_response(
                    mode="schedule",
                    message="That task no longer exists.",
                    resolved_thread_key=thread_key,
                    warnings=["Task chosen for deletion no longer exists."],
                )

            before_state = {
                t.id: (t.date, t.start_time, t.end_time)
                for t in db.query(Task).filter(Task.date == target.date, Task.completed == False).all()
            }
            db.delete(target)
            db.commit()
            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "delete",
                    "last_user_message": message,
                    "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                    "last_updated_task_ids": [],
                    "last_referenced_task_ids": [chosen_id],
                    "pending_intent_id": None,
                    "pending_state_type": None,
                    "pending_state": {},
                },
            )
            updated = _collect_updated_tasks_for_dates(db, {target.date: before_state}, {target.date})
            return _build_response(
                mode="delete",
                message=f"Deleted {target.title}.",
                updated_tasks=updated,
                resolved_thread_key=thread_key,
                applied_after_confirmation=True,
                affected_dates={target.date},
            )
        if pending_state.get("intent") == "replan":
            if isinstance(action_value, str) and action_value.strip().lower() == "cancel":
                _clear_pending_state(
                    db,
                    thread_key=thread_key,
                    thread_date=effective_thread_date,
                    chat_thread_id=chat_thread_id,
                    thread_state=thread_state,
                    last_intent_type="cancel",
                    last_user_message=message,
                )
                return _build_response(
                    mode="replan",
                    message="Okay, canceled the change.",
                    resolved_thread_key=thread_key,
                )
            target_task_id = None
            if isinstance(action_value, str):
                m = re.search(r"task[-\s]*([0-9]+)", action_value, flags=re.IGNORECASE)
                if m:
                    target_task_id = int(m.group(1))
                elif action_value.isdigit():
                    target_task_id = int(action_value)
            if not target_task_id:
                candidate_ids = pending_state.get("candidate_task_ids") or []
                if len(candidate_ids) == 1:
                    target_task_id = int(candidate_ids[0])

            parsed_reply = _parse_replan_request(action_value if isinstance(action_value, str) else "")
            requested_start = parsed_reply.get("requested_start_time")
            requested_end = parsed_reply.get("requested_end_time")
            if not requested_start and not requested_end:
                requested_start = _parse_clock_token(str(action_value)) if isinstance(action_value, str) else None
            if not requested_start and not requested_end and isinstance(action_value, str):
                try:
                    requested_start = parse_time(action_value)
                except Exception:
                    requested_start = None
            if not requested_start and not requested_end:
                req_start_str = pending_state.get("requested_start_time")
                req_end_str = pending_state.get("requested_end_time")
                if req_start_str:
                    requested_start = parse_time(req_start_str)
                if req_end_str:
                    requested_end = parse_time(req_end_str)

            if not target_task_id:
                return _build_replan_clarification_response(
                    message=message,
                    db=db,
                    thread_key=thread_key,
                    thread_state=thread_state,
                    effective_thread_date=effective_thread_date,
                    chat_thread_id=chat_thread_id,
                    meta={
                        "target_phrase": pending_state.get("target_phrase"),
                        "time_str": parsed_reply.get("time_str"),
                        "candidate_task_ids": pending_state.get("candidate_task_ids"),
                        "requested_start_time": requested_start,
                        "requested_end_time": requested_end,
                        "requested_date": extract_date_from_text(message) if _has_explicit_date_cue(message) else None,
                    },
                    used_replan_handler=True,
                )

            target = db.query(Task).filter(Task.id == int(target_task_id)).first()
            if not target:
                _clear_pending_state(
                    db,
                    thread_key=thread_key,
                    thread_date=effective_thread_date,
                    chat_thread_id=chat_thread_id,
                    thread_state=thread_state,
                    last_intent_type="pending_invalid",
                    last_user_message=message,
                )
                return _build_response(
                    mode="replan",
                    message="That task no longer exists. Please resend the change.",
                    resolved_thread_key=thread_key,
                    warnings=["Target task for replan was missing."],
                )

            if not requested_start and not requested_end:
                return _build_replan_clarification_response(
                    message=message,
                    db=db,
                    thread_key=thread_key,
                    thread_state=thread_state,
                    effective_thread_date=effective_thread_date,
                    chat_thread_id=chat_thread_id,
                    meta={
                        "target_phrase": target.title,
                        "time_str": None,
                        "candidate_task_ids": [target.id],
                        "requested_start_time": None,
                        "requested_end_time": None,
                        "requested_date": None,
                    },
                    used_replan_handler=True,
                )

            duration = task_duration_minutes(target)
            if requested_start and not requested_end:
                requested_end = (datetime.combine(target.date, requested_start) + timedelta(minutes=duration)).time()

            # Conflict check
            def _dt_range_for(task: Task):
                if not task.start_time:
                    return None, None
                s = datetime.combine(task.date, task.start_time)
                e = datetime.combine(task.date, task.end_time) if task.end_time else s + timedelta(minutes=task_duration_minutes(task))
                if e <= s:
                    e = s + timedelta(minutes=max(15, task_duration_minutes(task)))
                return s, e

            updated_start = requested_start or target.start_time
            updated_end = requested_end or target.end_time
            if updated_start and updated_end:
                conflicts = []
                for other in db.query(Task).filter(Task.date == target.date, Task.completed == False).all():
                    if other.id == target.id:
                        continue
                    o_s, o_e = _dt_range_for(other)
                    if not o_s or not o_e:
                        continue
                    c_s = datetime.combine(target.date, updated_start)
                    c_e = datetime.combine(target.date, updated_end)
                    if _slot_overlaps(c_s, c_e, o_s, o_e):
                        conflicts.append(other)
                if conflicts:
                    suggestions = suggest_alternative_slots(
                        db=db,
                        task_date=target.date,
                        duration_min=duration,
                        task_text=f"{target.title} {target.description}",
                        limit=3,
                    )
                    pending_intent_id = str(uuid.uuid4())
                    choice_pairs: list[tuple[str, str]] = []
                    for s in suggestions:
                        choice_pairs.append((_format_slot_choice_label(s), f"choose_slot:{s['slot_id']}"))
                    choice_pairs.append(("Cancel", "cancel"))
                    choice_map = _build_choice_map(choice_pairs)
                    save_thread_state(
                        db,
                        thread_key,
                        {
                            "thread_date": effective_thread_date,
                            "chat_thread_id": chat_thread_id,
                            "last_intent_type": "conflict",
                            "last_user_message": message,
                            "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                            "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                            "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                            "pending_intent_id": pending_intent_id,
                            "pending_state_type": "conflict_resolution",
                            "pending_state": {
                                "mode": "replan",
                                "target_task_id": target.id,
                                "draft": {
                                    "title": target.title,
                                    "description": target.description,
                                    "date": target.date.isoformat(),
                                    "start_time": updated_start.strftime("%H:%M:%S") if updated_start else None,
                                    "end_time": updated_end.strftime("%H:%M:%S") if updated_end else None,
                                    "duration_minutes": duration,
                                    "priority": int(target.priority or 1),
                                },
                                "conflict_ids": [t.id for t in conflicts],
                                "suggestions": [
                                    {
                                        "slot_id": s["slot_id"],
                                        "date": s["date"].isoformat(),
                                        "start_time": s["start_time"].strftime("%H:%M:%S"),
                                        "end_time": s["end_time"].strftime("%H:%M:%S"),
                                        "score": float(s["score"]),
                                        "reason": s["reason"],
                                    }
                                    for s in suggestions
                                ],
                                "choice_map": choice_map,
                            },
                        },
                    )
                    return _build_response(
                        mode="conflict",
                        message=_format_choice_message(
                            f"I infer you want to move {target.title} to {updated_start.strftime('%I:%M %p').lstrip('0')}. That conflicts with another task.",
                            choice_pairs,
                        ),
                        resolved_thread_key=thread_key,
                        requires_user_input=True,
                        pending_intent_id=pending_intent_id,
                        conflict_info=_build_conflict_prompt(
                            intent_id=pending_intent_id,
                            draft={
                                "title": target.title,
                                "description": target.description,
                                "date": target.date,
                                "start_time": updated_start,
                                "end_time": updated_end,
                                "duration_minutes": duration,
                                "priority": int(target.priority or 1),
                            },
                            conflicts=conflicts,
                            suggestions=suggestions,
                            actions=["choose_slot:<slot_id>", "cancel"],
                        ),
                    )

            before_state = {
                t.id: (t.date, t.start_time, t.end_time)
                for t in db.query(Task).filter(Task.date == target.date, Task.completed == False).all()
            }
            if requested_start:
                target.start_time = requested_start
            if requested_end:
                target.end_time = requested_end
            db.commit()
            updated = _collect_updated_tasks_for_dates(db, {target.date: before_state}, {target.date})
            updated = [t for t in updated if t.id == target.id]

            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "replan",
                    "last_user_message": message,
                    "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                    "last_updated_task_ids": [target.id],
                    "last_referenced_task_ids": [target.id],
                    "pending_intent_id": None,
                    "pending_state_type": None,
                    "pending_state": {},
                },
            )
            return _build_response(
                mode="replan",
                message="Updated existing task based on your clarification.",
                updated_tasks=updated or [target],
                resolved_thread_key=thread_key,
                applied_after_confirmation=True,
                affected_dates={target.date},
            )
        if draft and field == "start_time":
            parsed_time = None
            if isinstance(action_value, str):
                # For free-text replies, first check if it even looks like a time.
                parsed_time = extract_time_from_text(action_value)
                if not parsed_time and action_from_payload:
                    try:
                        parsed_time = parse_time(action_value)
                    except ValueError:
                        parsed_time = None
            if not parsed_time:
                prompt = _build_clarification_prompt(
                    intent_id=pending_intent_id,
                    field=field,
                    question=pending_state.get("question") or "What time should I schedule this task?",
                    options=pending_state.get("options", []),
                )
                return _build_response(
                    mode="ask_user",
                    message="I still need a time to continue. Please reply with something like 18:00 or 6pm.",
                    resolved_thread_key=thread_key,
                    requires_user_input=True,
                    pending_intent_id=pending_intent_id,
                    clarification=prompt,
                    warnings=["Pending clarification is unresolved."],
                )
            task_date = datetime.strptime(draft["date"], "%Y-%m-%d").date()
            duration = int(draft.get("duration_minutes") or 60)
            end_time = (datetime.combine(task_date, parsed_time) + timedelta(minutes=duration)).time()
            before_state = {
                t.id: (t.date, t.start_time, t.end_time)
                for t in db.query(Task).filter(Task.date == task_date, Task.completed == False).all()
            }
            created = create_task(
                db=db,
                title=draft.get("title") or "Task",
                description=draft.get("description") or "Task created from clarification.",
                date=task_date,
                start_time=parsed_time,
                end_time=end_time,
                priority=int(draft.get("priority") or 1),
                duration_minutes=duration,
            )
            rebalance_day(db, task_date, pinned_task_ids={created.id})
            updated = _collect_updated_tasks_for_dates(db, {task_date: before_state}, {task_date})
            updated = [t for t in updated if t.id != created.id]
            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "create",
                    "last_user_message": message,
                    "last_created_task_ids": [created.id],
                    "last_updated_task_ids": [t.id for t in updated][:10],
                    "last_referenced_task_ids": [created.id] + [t.id for t in updated][:9],
                    "pending_intent_id": None,
                    "pending_state_type": None,
                    "pending_state": {},
                },
            )
            return _build_response(
                mode="create",
                message="Created and scheduled task after clarification.",
                created_tasks=[created],
                updated_tasks=updated,
                resolved_thread_key=thread_key,
                applied_after_confirmation=True,
                affected_dates={task_date},
            )

        _clear_pending_state(
            db,
            thread_key=thread_key,
            thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            thread_state=thread_state,
            last_intent_type="clarification_resolved",
            last_user_message=message,
        )
        return _build_response(
            mode="schedule",
            message="Thanks, clarification captured.",
            resolved_thread_key=thread_key,
            affected_dates={effective_thread_date},
        )

    if pending_type != "conflict_resolution":
        return None

    draft = pending_state.get("draft") or {}
    mode = pending_state.get("mode")
    target_task_id = pending_state.get("target_task_id")
    if not draft:
        _clear_pending_state(
            db,
            thread_key=thread_key,
            thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            thread_state=thread_state,
            last_intent_type="pending_invalid",
            last_user_message=message,
        )
        return _build_response(
            mode="replan",
            message="Pending request expired; please resend the task.",
            resolved_thread_key=thread_key,
            warnings=["Pending conflict state was invalid."],
        )

    action = _parse_pending_action(message, pending_response, pending_state)
    if action is None and confirm is True:
        action = "choose_best"
    if action is None and confirm is False:
        action = "cancel"

    suggestions = pending_state.get("suggestions", [])
    conflicts = (
        db.query(Task)
        .filter(Task.id.in_(pending_state.get("conflict_ids", [])))
        .all()
        if pending_state.get("conflict_ids")
        else []
    )
    conflict_prompt = _build_conflict_prompt(
        intent_id=pending_intent_id,
        draft={
            "title": draft.get("title"),
            "description": draft.get("description"),
            "date": datetime.strptime(draft["date"], "%Y-%m-%d").date() if draft.get("date") else None,
            "start_time": parse_time(draft["start_time"]) if draft.get("start_time") else None,
            "end_time": parse_time(draft["end_time"]) if draft.get("end_time") else None,
            "duration_minutes": int(draft.get("duration_minutes") or 60),
            "priority": int(draft.get("priority") or 1),
        },
        conflicts=conflicts,
        suggestions=[
            {
                "slot_id": s["slot_id"],
                "date": datetime.strptime(s["date"], "%Y-%m-%d").date(),
                "start_time": parse_time(s["start_time"]),
                "end_time": parse_time(s["end_time"]),
                "score": float(s.get("score", 0)),
                "reason": s.get("reason", "Suggested alternative"),
            }
            for s in suggestions
        ],
        actions=["choose_slot:<slot_id>", "cancel"] if mode in ("replan", "multi_create_conflict") else None,
    )

    if action in (None, ""):
        return _build_response(
            mode="conflict",
            message="There is a scheduling conflict. Please choose one of the numbered options.",
            resolved_thread_key=thread_key,
            requires_user_input=True,
            pending_intent_id=pending_intent_id,
            conflict_info=conflict_prompt,
            warnings=["Pending conflict resolution requires your choice."],
        )

    if action == "make_adjustments":
        return _build_response(
            mode="conflict",
            message="Okay, let's adjust it. Please choose one of the numbered slot options.",
            resolved_thread_key=thread_key,
            requires_user_input=True,
            pending_intent_id=pending_intent_id,
            conflict_info=conflict_prompt,
        )

    if action == "cancel":
        _clear_pending_state(
            db,
            thread_key=thread_key,
            thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            thread_state=thread_state,
            last_intent_type="cancel",
            last_user_message=message,
        )
        return _build_response(
            mode="replan",
            message="Okay, I canceled that scheduling change.",
            resolved_thread_key=thread_key,
        )

    if mode in ("unscheduled_conflict", "create_conflict_move_existing"):
        if action == "keep_original_and_move_conflicts":
            conflict_tasks = conflicts or []
            if len(conflict_tasks) == 1:
                chosen_task = conflict_tasks[0]
                duration = task_duration_minutes(chosen_task)
                suggestions = _suggest_slots_same_day_first(
                    db=db,
                    task_date=chosen_task.date,
                    duration_min=duration,
                    task_text=f"{chosen_task.title} {chosen_task.description}",
                    limit=3,
                )
                if not suggestions:
                    return _build_response(
                        mode="conflict",
                        message="I couldn't find any available slots. Please provide a time.",
                        resolved_thread_key=thread_key,
                        requires_user_input=True,
                        pending_intent_id=pending_intent_id,
                        conflict_info=conflict_prompt,
                        warnings=["No available slots found for the conflicting task."],
                    )

                choice_pairs: list[tuple[str, str]] = []
                for s in suggestions:
                    choice_pairs.append((_format_slot_choice_label(s), f"choose_slot:{s['slot_id']}"))
                choice_pairs.append(("Cancel", "cancel"))
                choice_map = _build_choice_map(choice_pairs)
                save_thread_state(
                    db,
                    thread_key,
                    {
                        "thread_date": effective_thread_date,
                        "chat_thread_id": chat_thread_id,
                        "last_intent_type": "conflict",
                        "last_user_message": message,
                        "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                        "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                        "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                        "pending_intent_id": pending_intent_id,
                        "pending_state_type": "conflict_resolution",
                        "pending_state": {
                            "mode": "unscheduled_conflict",
                            "phase": "choose_slot",
                            "target_task_id": chosen_task.id,
                            "unscheduled_task_id": pending_state.get("unscheduled_task_id"),
                            "unscheduled_intended_start": pending_state.get("unscheduled_intended_start"),
                            "unscheduled_intended_end": pending_state.get("unscheduled_intended_end"),
                            "draft": {
                                "title": chosen_task.title,
                                "description": chosen_task.description,
                                "date": chosen_task.date.isoformat(),
                                "start_time": None,
                                "end_time": None,
                                "duration_minutes": duration,
                                "priority": int(chosen_task.priority or 1),
                            },
                            "conflict_ids": pending_state.get("conflict_ids", []),
                            "suggestions": [
                                {
                                    "slot_id": s["slot_id"],
                                    "date": s["date"].isoformat(),
                                    "start_time": s["start_time"].strftime("%H:%M:%S"),
                                    "end_time": s["end_time"].strftime("%H:%M:%S"),
                                    "score": float(s["score"]),
                                    "reason": s["reason"],
                                }
                                for s in suggestions
                            ],
                            "choice_map": choice_map,
                            "candidate_move_task_ids": [chosen_task.id],
                        },
                    },
                )
                return _build_response(
                    mode="conflict",
                    message=_format_choice_message(
                        f"When should I move {chosen_task.title}?",
                        choice_pairs,
                    ),
                    resolved_thread_key=thread_key,
                    requires_user_input=True,
                    pending_intent_id=pending_intent_id,
                    conflict_info=_build_conflict_prompt(
                        intent_id=pending_intent_id,
                        draft={
                            "title": chosen_task.title,
                            "description": chosen_task.description,
                            "date": chosen_task.date,
                            "start_time": None,
                            "end_time": None,
                            "duration_minutes": duration,
                            "priority": int(chosen_task.priority or 1),
                        },
                        conflicts=conflict_tasks,
                        suggestions=suggestions,
                        actions=["choose_slot:<slot_id>", "cancel"],
                    ),
                )

            choice_pairs = [(_format_task_choice_label(task), f"choose_task:{task.id}") for task in conflict_tasks[:3]]
            choice_pairs.append(("Cancel", "cancel"))
            choice_map = _build_choice_map(choice_pairs)
            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "conflict",
                    "last_user_message": message,
                    "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                    "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                    "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                    "pending_intent_id": pending_intent_id,
                    "pending_state_type": "conflict_resolution",
                    "pending_state": {
                        "mode": mode,
                        "phase": "choose_task",
                        "candidate_move_task_ids": [task.id for task in conflict_tasks[:3]],
                        "conflict_ids": pending_state.get("conflict_ids", []),
                        "choice_map": choice_map,
                    },
                },
            )
            return _build_response(
                mode="conflict",
                message=_format_choice_message("Which task should I move?", choice_pairs),
                resolved_thread_key=thread_key,
                requires_user_input=True,
                pending_intent_id=pending_intent_id,
                conflict_info=conflict_prompt,
            )

        phase = pending_state.get("phase") or "choose_task"
        candidate_ids = pending_state.get("candidate_move_task_ids") or []
        if phase == "choose_task":
            chosen_id = None
            if action and action.startswith("choose_task:"):
                try:
                    chosen_id = int(action.split(":", 1)[1])
                except (TypeError, ValueError):
                    chosen_id = None
            if not chosen_id and isinstance(pending_response, dict):
                raw = pending_response.get("value") or pending_response.get("task_id")
                try:
                    chosen_id = int(raw)
                except (TypeError, ValueError):
                    chosen_id = None
            if not chosen_id and isinstance(message, str) and message.strip().isdigit():
                chosen_id = int(message.strip())
            if not chosen_id or (candidate_ids and chosen_id not in candidate_ids):
                return _build_response(
                    mode="conflict",
                    message="Please choose which task to move.",
                    resolved_thread_key=thread_key,
                    requires_user_input=True,
                    pending_intent_id=pending_intent_id,
                    conflict_info=conflict_prompt,
                )

            chosen_task = db.query(Task).filter(Task.id == chosen_id).first()
            if not chosen_task:
                return _build_response(
                    mode="conflict",
                    message="That task no longer exists. Please choose a different one.",
                    resolved_thread_key=thread_key,
                    requires_user_input=True,
                    pending_intent_id=pending_intent_id,
                    conflict_info=conflict_prompt,
                )
            duration = task_duration_minutes(chosen_task)
            suggestions = _suggest_slots_same_day_first(
                db=db,
                task_date=chosen_task.date,
                duration_min=duration,
                task_text=f"{chosen_task.title} {chosen_task.description}",
                limit=3,
            )
            if not suggestions:
                return _build_response(
                    mode="conflict",
                    message="I couldn't find any available slots. Please provide a time.",
                    resolved_thread_key=thread_key,
                    requires_user_input=True,
                    pending_intent_id=pending_intent_id,
                    conflict_info=conflict_prompt,
                    warnings=["No available slots found for chosen task."],
                )
            choice_pairs: list[tuple[str, str]] = []
            for s in suggestions:
                choice_pairs.append((_format_slot_choice_label(s), f"choose_slot:{s['slot_id']}"))
            choice_pairs.append(("Cancel", "cancel"))
            choice_map = _build_choice_map(choice_pairs)
            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "conflict",
                    "last_user_message": message,
                    "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                    "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                    "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                    "pending_intent_id": pending_intent_id,
                    "pending_state_type": "conflict_resolution",
                    "pending_state": {
                        "mode": "unscheduled_conflict",
                        "phase": "choose_slot",
                        "target_task_id": chosen_task.id,
                        "unscheduled_task_id": pending_state.get("unscheduled_task_id"),
                        "unscheduled_intended_start": pending_state.get("unscheduled_intended_start"),
                        "unscheduled_intended_end": pending_state.get("unscheduled_intended_end"),
                        "draft": {
                            "title": chosen_task.title,
                            "description": chosen_task.description,
                            "date": chosen_task.date.isoformat(),
                            "start_time": None,
                            "end_time": None,
                            "duration_minutes": duration,
                            "priority": int(chosen_task.priority or 1),
                        },
                        "conflict_ids": pending_state.get("conflict_ids", []),
                        "suggestions": [
                            {
                                "slot_id": s["slot_id"],
                                "date": s["date"].isoformat(),
                                "start_time": s["start_time"].strftime("%H:%M:%S"),
                                "end_time": s["end_time"].strftime("%H:%M:%S"),
                                "score": float(s["score"]),
                                "reason": s["reason"],
                            }
                            for s in suggestions
                        ],
                        "choice_map": choice_map,
                        "candidate_move_task_ids": candidate_ids,
                    },
                },
            )
            return _build_response(
                mode="conflict",
                message=_format_choice_message(
                    f"When should I move {chosen_task.title}?",
                    choice_pairs,
                ),
                resolved_thread_key=thread_key,
                requires_user_input=True,
                pending_intent_id=pending_intent_id,
                conflict_info=_build_conflict_prompt(
                    intent_id=pending_intent_id,
                    draft={
                        "title": chosen_task.title,
                        "description": chosen_task.description,
                        "date": chosen_task.date,
                        "start_time": None,
                        "end_time": None,
                        "duration_minutes": duration,
                        "priority": int(chosen_task.priority or 1),
                    },
                    conflicts=conflicts,
                    suggestions=suggestions,
                    actions=["choose_slot:<slot_id>", "cancel"],
                ),
            )

    if action == "keep_original_and_move_conflicts" and mode in ("replan", "multi_create_conflict", "create_conflict"):
        conflict_ids = pending_state.get("conflict_ids") or []
        conflict_tasks = conflicts or []

        if len(conflict_tasks) == 1:
            task_to_move = conflict_tasks[0]
            duration = task_duration_minutes(task_to_move)
            suggestions = _suggest_slots_same_day_first(
                db=db,
                task_date=task_to_move.date,
                duration_min=duration,
                task_text=f"{task_to_move.title} {task_to_move.description}",
                limit=3,
            )
            if not suggestions:
                return _build_response(
                    mode="conflict",
                    message="I couldn't find any available slots. Please provide a time.",
                    resolved_thread_key=thread_key,
                    requires_user_input=True,
                    pending_intent_id=pending_intent_id,
                    conflict_info=conflict_prompt,
                    warnings=["No available slots found for the conflicting task."],
                )

            choice_pairs: list[tuple[str, str]] = []
            for s in suggestions:
                choice_pairs.append((_format_slot_choice_label(s), f"choose_slot:{s['slot_id']}"))
            choice_pairs.append(("Cancel", "cancel"))
            choice_map = _build_choice_map(choice_pairs)
            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "conflict",
                    "last_user_message": message,
                    "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                    "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                    "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                    "pending_intent_id": pending_intent_id,
                    "pending_state_type": "conflict_resolution",
                        "pending_state": {
                            "mode": "create_conflict_move_existing" if mode == "create_conflict" else "unscheduled_conflict",
                            "phase": "choose_slot",
                            "target_task_id": task_to_move.id,
                            "conflict_ids": conflict_ids,
                            "candidate_move_task_ids": [task_to_move.id],
                            "draft": {
                            "title": task_to_move.title,
                            "description": task_to_move.description,
                            "date": task_to_move.date.isoformat(),
                            "start_time": None,
                            "end_time": None,
                            "duration_minutes": duration,
                            "priority": int(task_to_move.priority or 1),
                        },
                        "suggestions": [
                            {
                                "slot_id": s["slot_id"],
                                "date": s["date"].isoformat(),
                                "start_time": s["start_time"].strftime("%H:%M:%S"),
                                "end_time": s["end_time"].strftime("%H:%M:%S"),
                                "score": float(s["score"]),
                                "reason": s["reason"],
                            }
                            for s in suggestions
                        ],
                        "choice_map": choice_map,
                        "new_task_draft": pending_state.get("draft") if mode == "create_conflict" else None,
                    },
                },
            )
            return _build_response(
                mode="conflict",
                message=_format_choice_message(
                    f"When should I move {task_to_move.title}?",
                    choice_pairs,
                ),
                resolved_thread_key=thread_key,
                requires_user_input=True,
                pending_intent_id=pending_intent_id,
                conflict_info=_build_conflict_prompt(
                    intent_id=pending_intent_id,
                    draft={
                        "title": task_to_move.title,
                        "description": task_to_move.description,
                        "date": task_to_move.date,
                        "start_time": None,
                        "end_time": None,
                        "duration_minutes": duration,
                        "priority": int(task_to_move.priority or 1),
                    },
                    conflicts=conflict_tasks,
                    suggestions=suggestions,
                    actions=["choose_slot:<slot_id>", "cancel"],
                ),
            )

        choice_pairs = [(_format_task_choice_label(task), f"choose_task:{task.id}") for task in conflict_tasks[:3]]
        choice_pairs.append(("Cancel", "cancel"))
        choice_map = _build_choice_map(choice_pairs)
        save_thread_state(
            db,
            thread_key,
            {
                "thread_date": effective_thread_date,
                "chat_thread_id": chat_thread_id,
                "last_intent_type": "conflict",
                "last_user_message": message,
                "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                "pending_intent_id": pending_intent_id,
                "pending_state_type": "conflict_resolution",
                    "pending_state": {
                        "mode": "create_conflict_move_existing" if mode == "create_conflict" else "unscheduled_conflict",
                        "phase": "choose_task",
                        "candidate_move_task_ids": [task.id for task in conflict_tasks[:3]],
                        "conflict_ids": conflict_ids,
                        "choice_map": choice_map,
                        "new_task_draft": pending_state.get("draft") if mode == "create_conflict" else None,
                    },
                },
            )
        return _build_response(
            mode="conflict",
            message=_format_choice_message("Which task should I move?", choice_pairs),
            resolved_thread_key=thread_key,
            requires_user_input=True,
            pending_intent_id=pending_intent_id,
            conflict_info=conflict_prompt,
        )

    choose_best = action == "choose_best"
    selected_slot = None
    if choose_best and suggestions:
        selected_slot = suggestions[0]
    elif action.startswith("choose_slot:"):
        slot_id = action.split(":", 1)[1]
        selected_slot = next((s for s in suggestions if s.get("slot_id") == slot_id), None)
        if not selected_slot:
            return _build_response(
                mode="conflict",
                message="I couldn't find that slot. Please choose one of the suggested slots.",
                resolved_thread_key=thread_key,
                requires_user_input=True,
                pending_intent_id=pending_intent_id,
                conflict_info=conflict_prompt,
                warnings=["Selected slot was not found in suggestions."],
            )

    if selected_slot:
        draft["date"] = selected_slot["date"]
        draft["start_time"] = selected_slot["start_time"]
        draft["end_time"] = selected_slot["end_time"]
    elif action != "keep_original_and_move_conflicts":
        return _build_response(
            mode="conflict",
            message="Please choose a slot, keep original and move conflicts, or cancel.",
            resolved_thread_key=thread_key,
            requires_user_input=True,
            pending_intent_id=pending_intent_id,
            conflict_info=conflict_prompt,
        )

    if mode == "multi_create_conflict":
        drafts = pending_state.get("drafts") or []
        draft_index = int(pending_state.get("draft_index") or 0)
        if 0 <= draft_index < len(drafts):
            drafts[draft_index] = draft
        return _finalize_multi_create(
            db=db,
            thread_key=thread_key,
            thread_state=thread_state,
            effective_thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            message=message,
            drafts=drafts,
        )

    if mode in ("unscheduled_conflict", "create_conflict_move_existing") and target_task_id:
        target = db.query(Task).filter(Task.id == int(target_task_id)).first()
        if not target:
            _clear_pending_state(
                db,
                thread_key=thread_key,
                thread_date=effective_thread_date,
                chat_thread_id=chat_thread_id,
                thread_state=thread_state,
                last_intent_type="pending_invalid",
                last_user_message=message,
            )
            return _build_response(
                mode="replan",
                message="That task no longer exists. Please resend the change.",
                resolved_thread_key=thread_key,
                warnings=["Target task for conflict move was missing."],
            )
        draft_date = datetime.strptime(draft["date"], "%Y-%m-%d").date()
        draft_start = parse_time(draft["start_time"]) if draft.get("start_time") else None
        draft_end = parse_time(draft["end_time"]) if draft.get("end_time") else None
        duration = int(draft.get("duration_minutes") or task_duration_minutes(target))
        if draft_start and not draft_end:
            draft_end = (datetime.combine(draft_date, draft_start) + timedelta(minutes=duration)).time()
        before_state = {
            t.id: (t.date, t.start_time, t.end_time)
            for t in db.query(Task).filter(Task.date == draft_date, Task.completed == False).all()
        }
        target.date = draft_date
        target.start_time = draft_start
        target.end_time = draft_end
        db.commit()
        updated = _collect_updated_tasks_for_dates(db, {draft_date: before_state}, {draft_date})
        updated = [t for t in updated if t.id == target.id]

        created_tasks = []

        if mode == "create_conflict_move_existing":
            new_task_draft = pending_state.get("new_task_draft") or {}
            new_task_date = datetime.strptime(new_task_draft["date"], "%Y-%m-%d").date()
            new_task_start = parse_time(new_task_draft["start_time"]) if new_task_draft.get("start_time") else None
            new_task_end = parse_time(new_task_draft["end_time"]) if new_task_draft.get("end_time") else None
            if new_task_start and not new_task_end:
                new_task_end = (
                    datetime.combine(new_task_date, new_task_start)
                    + timedelta(minutes=int(new_task_draft.get("duration_minutes") or 60))
                ).time()

            post_move_conflicts = []
            if new_task_start and new_task_end:
                for other in db.query(Task).filter(Task.date == new_task_date, Task.completed == False).all():
                    c_s, c_e = _task_datetime_range(other)
                    if not c_s or not c_e:
                        continue
                    n_s = datetime.combine(new_task_date, new_task_start)
                    n_e = datetime.combine(new_task_date, new_task_end)
                    if _slot_overlaps(n_s, n_e, c_s, c_e):
                        post_move_conflicts.append(other)

            if post_move_conflicts:
                return _build_response(
                    mode="conflict",
                    message="I moved the conflicting task, but the new task still overlaps another task. Please resend this request so I can recompute the options cleanly.",
                    resolved_thread_key=thread_key,
                    warnings=["Post-move conflict remained while applying the new task."],
                )

            created = create_task(
                db=db,
                title=new_task_draft.get("title") or "Task",
                description=new_task_draft.get("description") or "Task created after conflict resolution.",
                date=new_task_date,
                start_time=new_task_start,
                end_time=new_task_end,
                priority=int(new_task_draft.get("priority") or 1),
                duration_minutes=int(new_task_draft.get("duration_minutes") or 60),
            )
            created_tasks = [created]

        unscheduled_id = pending_state.get("unscheduled_task_id")
        intended_start_raw = pending_state.get("unscheduled_intended_start")
        intended_end_raw = pending_state.get("unscheduled_intended_end")
        if unscheduled_id and int(unscheduled_id) != target.id and intended_start_raw and intended_end_raw:
            unscheduled_task = db.query(Task).filter(Task.id == int(unscheduled_id)).first()
            if unscheduled_task:
                intended_start = parse_time(intended_start_raw)
                intended_end = parse_time(intended_end_raw)
                conflicts = []
                for other in db.query(Task).filter(Task.date == unscheduled_task.date, Task.completed == False).all():
                    if other.id == unscheduled_task.id:
                        continue
                    o_s, o_e = _task_datetime_range(other)
                    if not o_s or not o_e:
                        continue
                    c_s = datetime.combine(unscheduled_task.date, intended_start)
                    c_e = datetime.combine(unscheduled_task.date, intended_end)
                    if _slot_overlaps(c_s, c_e, o_s, o_e):
                        conflicts.append(other)
                if not conflicts:
                    unscheduled_task.start_time = intended_start
                    unscheduled_task.end_time = intended_end
                    db.commit()

        save_thread_state(
            db,
            thread_key,
            {
                "thread_date": effective_thread_date,
                "chat_thread_id": chat_thread_id,
                "last_intent_type": "create" if mode == "create_conflict_move_existing" else "replan",
                "last_user_message": message,
                "last_created_task_ids": [t.id for t in created_tasks][:10] if mode == "create_conflict_move_existing" else thread_state.get("last_created_task_ids", []),
                "last_updated_task_ids": [target.id],
                "last_referenced_task_ids": ([target.id] + [t.id for t in created_tasks][:9]) if mode == "create_conflict_move_existing" else [target.id],
                "pending_intent_id": None,
                "pending_state_type": None,
                "pending_state": {},
            },
        )
        return _build_response(
            mode="create" if mode == "create_conflict_move_existing" else "replan",
            message="Updated task after resolving the conflict.",
            created_tasks=created_tasks,
            updated_tasks=updated or [target],
            resolved_thread_key=thread_key,
            applied_after_confirmation=bool(choose_best),
            affected_dates={draft_date} | ({created_tasks[0].date} if created_tasks else set()),
        )

    if mode == "replan" and target_task_id:
        target = db.query(Task).filter(Task.id == int(target_task_id)).first()
        if not target:
            _clear_pending_state(
                db,
                thread_key=thread_key,
                thread_date=effective_thread_date,
                chat_thread_id=chat_thread_id,
                thread_state=thread_state,
                last_intent_type="pending_invalid",
                last_user_message=message,
            )
            return _build_response(
                mode="replan",
                message="That task no longer exists. Please resend the change.",
                resolved_thread_key=thread_key,
                warnings=["Target task for replan was missing."],
            )
        draft_date = datetime.strptime(draft["date"], "%Y-%m-%d").date()
        draft_start = parse_time(draft["start_time"]) if draft.get("start_time") else None
        draft_end = parse_time(draft["end_time"]) if draft.get("end_time") else None
        duration = int(draft.get("duration_minutes") or task_duration_minutes(target))
        if draft_start and not draft_end:
            draft_end = (datetime.combine(draft_date, draft_start) + timedelta(minutes=duration)).time()
        before_state = {
            t.id: (t.date, t.start_time, t.end_time)
            for t in db.query(Task).filter(Task.date == draft_date, Task.completed == False).all()
        }
        target.date = draft_date
        if draft_start:
            target.start_time = draft_start
        if draft_end:
            target.end_time = draft_end
        db.commit()
        updated = _collect_updated_tasks_for_dates(db, {draft_date: before_state}, {draft_date})
        updated = [t for t in updated if t.id == target.id]
        save_thread_state(
            db,
            thread_key,
            {
                "thread_date": effective_thread_date,
                "chat_thread_id": chat_thread_id,
                "last_intent_type": "replan",
                "last_user_message": message,
                "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                "last_updated_task_ids": [target.id],
                "last_referenced_task_ids": [target.id],
                "pending_intent_id": None,
                "pending_state_type": None,
                "pending_state": {},
            },
        )
        return _build_response(
            mode="replan",
            message="Updated existing task based on your choice.",
            updated_tasks=updated or [target],
            resolved_thread_key=thread_key,
            applied_after_confirmation=bool(choose_best),
            affected_dates={draft_date},
        )

    draft_date = datetime.strptime(draft["date"], "%Y-%m-%d").date()
    draft_start = parse_time(draft["start_time"]) if draft.get("start_time") else None
    draft_end = parse_time(draft["end_time"]) if draft.get("end_time") else None
    duration = int(draft.get("duration_minutes") or 60)
    if draft_start and not draft_end:
        draft_end = (datetime.combine(draft_date, draft_start) + timedelta(minutes=duration)).time()

    before_state = {
        t.id: (t.date, t.start_time, t.end_time)
        for t in db.query(Task).filter(Task.date == draft_date, Task.completed == False).all()
    }
    created = create_task(
        db=db,
        title=draft.get("title") or "Task",
        description=draft.get("description") or "Task created from pending conflict resolution.",
        date=draft_date,
        start_time=draft_start,
        end_time=draft_end,
        priority=int(draft.get("priority") or 1),
        duration_minutes=duration,
    )
    pinned_ids = {created.id} if draft_start else set()
    rebalance_day(db, draft_date, pinned_task_ids=pinned_ids)
    updated = _collect_updated_tasks_for_dates(db, {draft_date: before_state}, {draft_date})
    updated = [t for t in updated if t.id != created.id]

    save_thread_state(
        db,
        thread_key,
        {
            "thread_date": effective_thread_date,
            "chat_thread_id": chat_thread_id,
            "last_intent_type": "create",
            "last_user_message": message,
            "last_created_task_ids": [created.id],
            "last_updated_task_ids": [t.id for t in updated][:10],
            "last_referenced_task_ids": [created.id] + [t.id for t in updated][:9],
            "pending_intent_id": None,
            "pending_state_type": None,
            "pending_state": {},
        },
    )
    return _build_response(
        mode="create",
        message="Scheduled after your confirmation.",
        created_tasks=[created],
        updated_tasks=updated,
        resolved_thread_key=thread_key,
        applied_after_confirmation=bool(choose_best),
        affected_dates={draft_date},
    )


def _build_model_input_for_decision(db: Session, thread_key: str, thread_state: dict, effective_thread_date: date, message: str) -> str:
    context = build_schedule_context(db, today=effective_thread_date)
    thread_summary = _thread_summary_for_prompt(db, thread_key, thread_state)
    return (
        "Existing schedule context (use this for follow-up commands):\n"
        f"{context}\n\n"
        "Thread memory summary:\n"
        f"{thread_summary}\n\n"
        f"User message:\n{message}"
    )


def _save_ask_user_pending_state(
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
    message: str,
    field: str,
    question: str,
    options_raw: list[dict],
    extra_pending_state: dict | None = None,
) -> str:
    pending_intent_id = str(uuid.uuid4())
    auto_choice_map = {str(i + 1): str(opt.get("value") or opt.get("id") or "") for i, opt in enumerate(options_raw or [])}
    pending_state = {
        "field": field,
        "question": question,
        "options": options_raw,
    }
    if extra_pending_state:
        pending_state.update(extra_pending_state)
    if auto_choice_map and not pending_state.get("choice_map"):
        pending_state["choice_map"] = auto_choice_map
    save_thread_state(
        db,
        thread_key,
        {
            "thread_date": effective_thread_date,
            "chat_thread_id": chat_thread_id,
            "last_intent_type": "ask_user",
            "last_user_message": message,
            "last_created_task_ids": thread_state.get("last_created_task_ids", []),
            "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
            "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
            "pending_intent_id": pending_intent_id,
            "pending_state_type": "clarification",
            "pending_state": pending_state,
        },
    )
    return pending_intent_id


def _build_ask_user_response(
    thread_key: str,
    pending_intent_id: str,
    field: str,
    question: str,
    options_raw: list[dict],
    used_replan_handler: bool = False,
    allow_free_text: bool = True,
) -> ChatResponse:
    return _build_response(
        mode="ask_user",
        message=question,
        used_replan_handler=used_replan_handler,
        resolved_thread_key=thread_key,
        requires_user_input=True,
        pending_intent_id=pending_intent_id,
        clarification=_build_clarification_prompt(
            intent_id=pending_intent_id,
            field=field,
            question=question,
            options=options_raw,
            allow_free_text=allow_free_text,
        ),
    )


def _build_replan_clarification_response(
    message: str,
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
    meta: dict,
    used_replan_handler: bool = False,
) -> ChatResponse:
    target_phrase = meta.get("target_phrase")
    time_str = meta.get("time_str")
    candidate_ids = meta.get("candidate_task_ids") or thread_state.get("last_referenced_task_ids", [])[:5]
    candidate_tasks = []
    if candidate_ids:
        ref_tasks = db.query(Task).filter(Task.id.in_(candidate_ids)).all()
        by_id = {t.id: t for t in ref_tasks}
        candidate_tasks = [by_id[tid] for tid in candidate_ids if tid in by_id]

    target_task_id = candidate_tasks[0].id if len(candidate_tasks) == 1 else None
    requested_start_time = meta.get("requested_start_time")
    requested_end_time = meta.get("requested_end_time")

    options_raw: list[dict] = []
    choice_pairs: list[tuple[str, str]] = []

    if target_task_id and not requested_start_time and not requested_end_time:
        target_task = candidate_tasks[0]
        question = f"Choose a new time slot for {target_task.title}."
        suggestions = suggest_alternative_slots(
            db=db,
            task_date=target_task.date,
            duration_min=task_duration_minutes(target_task),
            task_text=f"{target_task.title} {target_task.description}",
            limit=3,
        )
        if suggestions:
            for s in suggestions:
                label = _format_slot_choice_label(s)
                value = s["start_time"].strftime("%H:%M:%S")
                choice_pairs.append((label, value))
        else:
            fallback = _dynamic_time_options_for_draft(
                db,
                {
                    "title": target_task.title,
                    "description": target_task.description,
                    "date": target_task.date,
                    "duration_minutes": task_duration_minutes(target_task),
                },
                limit=3,
            )
            for s in fallback:
                choice_pairs.append((s["label"], s["value"]))
        choice_pairs.append(("Cancel", "cancel"))
    else:
        if target_phrase and time_str:
            question = f"Which task should I move to {time_str}?"
        elif target_phrase:
            question = f"Which task should I move?"
        elif time_str:
            question = f"Which task should I move to {time_str}?"
        else:
            question = "Which task should I change?"
        for t in candidate_tasks:
            label = _format_task_choice_label(t)
            value = f"task-{t.id}"
            choice_pairs.append((label, value))
        choice_pairs.append(("Cancel", "cancel"))

    for idx, (label, value) in enumerate(choice_pairs, 1):
        options_raw.append(
            {
                "id": str(idx),
                "label": label,
                "value": value,
            }
        )
    choice_map = _build_choice_map(choice_pairs)

    pending_intent_id = _save_ask_user_pending_state(
        db=db,
        thread_key=thread_key,
        thread_state=thread_state,
        effective_thread_date=effective_thread_date,
        chat_thread_id=chat_thread_id,
        message=message,
        field="target_task",
        question=question,
        options_raw=options_raw,
        extra_pending_state={
            "intent": "replan",
            "target_phrase": target_phrase,
            "requested_start_time": _time_to_str(meta.get("requested_start_time")),
            "requested_end_time": _time_to_str(meta.get("requested_end_time")),
            "requested_date": meta.get("requested_date").isoformat()
            if meta.get("requested_date")
            else None,
            "candidate_task_ids": candidate_ids,
            "choice_map": choice_map,
        },
    )
    return _build_ask_user_response(
        thread_key,
        pending_intent_id,
        "target_task",
        question,
        options_raw,
        used_replan_handler=used_replan_handler,
        allow_free_text=False,
    )


def _build_delete_task_prompt(
    message: str,
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
    candidate_tasks: list[Task],
) -> ChatResponse:
    question = "Which task should I delete?"
    options_raw = [
        {"id": str(idx + 1), "label": _format_task_choice_label(task), "value": f"delete_task:{task.id}"}
        for idx, task in enumerate(candidate_tasks[:3])
    ]
    options_raw.append({"id": str(len(options_raw) + 1), "label": "Cancel", "value": "cancel"})
    choice_map = _build_choice_map([(opt["label"], opt["value"]) for opt in options_raw])
    pending_intent_id = _save_ask_user_pending_state(
        db=db,
        thread_key=thread_key,
        thread_state=thread_state,
        effective_thread_date=effective_thread_date,
        chat_thread_id=chat_thread_id,
        message=message,
        field="target_task",
        question=question,
        options_raw=options_raw,
        extra_pending_state={
            "mode": "delete",
            "candidate_task_ids": [task.id for task in candidate_tasks[:3]],
            "choice_map": choice_map,
        },
    )
    return _build_ask_user_response(
        thread_key,
        pending_intent_id,
        "target_task",
        question,
        options_raw,
        used_replan_handler=False,
        allow_free_text=False,
    )


def _find_delete_candidates(
    db: Session,
    message: str,
    thread_state: dict,
    reference_date: date,
    limit: int = 3,
) -> list[Task]:
    text = (message or "").lower()
    candidate_pool = (
        db.query(Task)
        .filter(Task.date >= reference_date, Task.completed == False)
        .order_by(Task.date.asc(), Task.start_time.asc(), Task.priority.desc(), Task.id.asc())
        .all()
    )
    memory_ids = []
    for tid in (
        thread_state.get("last_referenced_task_ids", [])
        + thread_state.get("last_updated_task_ids", [])
        + thread_state.get("last_created_task_ids", [])
    ):
        try:
            tid_int = int(tid)
        except (TypeError, ValueError):
            continue
        if tid_int not in memory_ids:
            memory_ids.append(tid_int)
    if memory_ids:
        memory_rows = db.query(Task).filter(Task.id.in_(memory_ids), Task.completed == False).all()
        by_id = {t.id: t for t in memory_rows}
        candidate_pool = list({t.id: t for t in candidate_pool + [by_id[i] for i in memory_ids if i in by_id]}.values())

    def _tokenize(s: str) -> set[str]:
        tokens = set()
        for w in re.findall(r"[a-z0-9]+", (s or "").lower()):
            if len(w) <= 1 or w in STOP_WORDS:
                continue
            tokens.add(w)
        return tokens

    query_tokens = _tokenize(text)
    scored = []
    for task in candidate_pool:
        task_tokens = _tokenize(f"{task.title or ''} {task.description or ''}")
        overlap = len(query_tokens.intersection(task_tokens))
        if overlap == 0 and not query_tokens:
            overlap = 1 if task.id in memory_ids else 0
        if overlap == 0:
            continue
        scored.append((overlap, task))
    if not scored:
        return candidate_pool[:limit]
    scored.sort(key=lambda item: (-item[0], item[1].date, item[1].start_time or time(23, 59), -item[1].priority, item[1].id))
    return [task for _score, task in scored[:limit]]


def _build_unscheduled_conflict_prompt(
    unscheduled_task: Task,
    conflicts: list[Task],
    intent_id: str,
) -> ConflictPrompt:
    draft = {
        "title": unscheduled_task.title,
        "description": unscheduled_task.description,
        "date": unscheduled_task.date,
        "start_time": unscheduled_task.start_time,
        "end_time": unscheduled_task.end_time,
        "duration_minutes": task_duration_minutes(unscheduled_task),
        "priority": int(unscheduled_task.priority or 1),
    }
    return _build_conflict_prompt(
        intent_id=intent_id,
        draft=draft,
        conflicts=conflicts,
        suggestions=[],
        actions=["choose_task:<task_id>", "cancel"],
    )


def _build_multi_create_prompt(
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
    message: str,
    drafts: list[dict],
    pending_index: int,
    awaiting_free_time: bool = False,
) -> ChatResponse:
    if pending_index >= len(drafts):
        pending_index = len(drafts) - 1 if drafts else 0
    draft = drafts[pending_index]
    task_title = draft.get("title") or "this task"
    question = f"What time should I schedule {task_title}?"
    options_raw = []
    min_start = None
    if draft.get("min_start_time"):
        try:
            min_start = parse_time(draft["min_start_time"])
        except Exception:
            min_start = None
    if not awaiting_free_time:
        suggestions = _suggest_slots_same_day_first(
            db=db,
            task_date=datetime.strptime(draft["date"], "%Y-%m-%d").date(),
            duration_min=int(draft.get("duration_minutes") or 60),
            task_text=f"{draft.get('title', '')} {draft.get('description', '')}",
            limit=3,
        )
        if min_start:
            suggestions = [s for s in suggestions if s["start_time"] >= min_start]
        if not suggestions and min_start:
            task_date = datetime.strptime(draft["date"], "%Y-%m-%d").date()
            cur = datetime.combine(task_date, min_start)
            end_dt = datetime.combine(task_date, time(23, 0))
            while len(suggestions) < 3 and cur + timedelta(minutes=int(draft.get("duration_minutes") or 60)) <= end_dt:
                suggestions.append(
                    {
                        "slot_id": f"{task_date.isoformat()}@{cur.time().strftime('%H:%M:%S')}",
                        "date": task_date,
                        "start_time": cur.time(),
                        "end_time": (cur + timedelta(minutes=int(draft.get('duration_minutes') or 60))).time(),
                        "score": 0,
                        "reason": "Suggested based on availability.",
                    }
                )
                cur += timedelta(minutes=15)
        for s in suggestions:
            options_raw.append(
                {
                    "id": s["slot_id"],
                    "label": s["start_time"].strftime("%I:%M %p").lstrip("0"),
                    "value": s["start_time"].strftime("%H:%M"),
                }
            )
        options_raw.append({"id": "other-time", "label": "Other time", "value": "other_time"})
    pending_intent_id = _save_ask_user_pending_state(
        db=db,
        thread_key=thread_key,
        thread_state=thread_state,
        effective_thread_date=effective_thread_date,
        chat_thread_id=chat_thread_id,
        message=message,
        field="start_time",
        question=question,
        options_raw=options_raw,
        extra_pending_state={
            "mode": "multi_create",
            "drafts": drafts,
            "pending_index": pending_index,
            "awaiting_free_time": awaiting_free_time,
        },
    )
    return _build_ask_user_response(thread_key, pending_intent_id, "start_time", question, options_raw)


def _finalize_multi_create(
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
    message: str,
    drafts: list[dict],
) -> ChatResponse:
    # Check for conflicts before creating any tasks.
    for idx, draft in enumerate(drafts):
        conflicts, suggestions = detect_conflicts_for_draft(db, {
            "title": draft.get("title"),
            "description": draft.get("description"),
            "date": datetime.strptime(draft["date"], "%Y-%m-%d").date(),
            "start_time": parse_time(draft["start_time"]) if draft.get("start_time") else None,
            "end_time": parse_time(draft["end_time"]) if draft.get("end_time") else None,
            "duration_minutes": int(draft.get("duration_minutes") or 60),
            "priority": int(draft.get("priority") or 1),
        })
        if conflicts:
            pending_intent_id = str(uuid.uuid4())
            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "conflict",
                    "last_user_message": message,
                    "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                    "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                    "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                    "pending_intent_id": pending_intent_id,
                    "pending_state_type": "conflict_resolution",
                    "pending_state": {
                        "mode": "multi_create_conflict",
                        "drafts": drafts,
                        "draft_index": idx,
                        "pending_index": idx,
                        "draft": {
                            "title": draft.get("title"),
                            "description": draft.get("description"),
                            "date": draft.get("date"),
                            "start_time": draft.get("start_time"),
                            "end_time": draft.get("end_time"),
                            "duration_minutes": int(draft.get("duration_minutes") or 60),
                            "priority": int(draft.get("priority") or 1),
                        },
                        "conflict_ids": [t.id for t in conflicts],
                        "suggestions": [
                            {
                                "slot_id": s["slot_id"],
                                "date": s["date"].isoformat(),
                                "start_time": s["start_time"].strftime("%H:%M:%S"),
                                "end_time": s["end_time"].strftime("%H:%M:%S"),
                                "score": float(s["score"]),
                                "reason": s["reason"],
                            }
                            for s in suggestions
                        ],
                    },
                },
            )
            return _build_response(
                mode="conflict",
                message="That time conflicts with an existing task. Choose a different slot.",
                resolved_thread_key=thread_key,
                requires_user_input=True,
                pending_intent_id=pending_intent_id,
                conflict_info=_build_conflict_prompt(
                    intent_id=pending_intent_id,
                    draft={
                        "title": draft.get("title"),
                        "description": draft.get("description"),
                        "date": datetime.strptime(draft["date"], "%Y-%m-%d").date(),
                        "start_time": parse_time(draft["start_time"]) if draft.get("start_time") else None,
                        "end_time": parse_time(draft["end_time"]) if draft.get("end_time") else None,
                        "duration_minutes": int(draft.get("duration_minutes") or 60),
                        "priority": int(draft.get("priority") or 1),
                    },
                    conflicts=conflicts,
                    suggestions=suggestions,
                    actions=["choose_slot:<slot_id>", "cancel"],
                ),
            )

    created = []
    affected_dates = set()
    for draft in drafts:
        task_date = datetime.strptime(draft["date"], "%Y-%m-%d").date()
        start_t = parse_time(draft["start_time"]) if draft.get("start_time") else None
        end_t = parse_time(draft["end_time"]) if draft.get("end_time") else None
        created.append(
            create_task(
                db=db,
                title=draft.get("title") or "Task",
                description=draft.get("description") or "",
                date=task_date,
                start_time=start_t,
                end_time=end_t,
                priority=int(draft.get("priority") or 1),
                duration_minutes=int(draft.get("duration_minutes") or 60),
            )
        )
        affected_dates.add(task_date)

    created_ids = [t.id for t in created][:10]
    save_thread_state(
        db,
        thread_key,
        {
            "thread_date": effective_thread_date,
            "chat_thread_id": chat_thread_id,
            "last_intent_type": "create",
            "last_user_message": message,
            "last_created_task_ids": created_ids,
            "last_updated_task_ids": [],
            "last_referenced_task_ids": created_ids,
            "pending_intent_id": None,
            "pending_state_type": None,
            "pending_state": {},
        },
    )
    return _build_response(
        mode="create",
        message="Created and scheduled tasks.",
        created_tasks=created,
        resolved_thread_key=thread_key,
        affected_dates=affected_dates,
    )

def _normalize_args_list(decision: Any, message: str) -> tuple[list[dict], bool]:
    fallback_multi = split_tasks_from_message(message)
    used_fallback_parser = False
    args_list = decision.arguments if decision.action == "create_task" else []
    if isinstance(args_list, dict):
        args_list = [args_list]
    if not args_list:
        if decision.action == "create_task":
            args_list = fallback_multi
            used_fallback_parser = bool(fallback_multi)
    elif len(args_list) == 1:
        only = args_list[0] or {}
        only_title = (only.get("title") or "").strip().lower()
        input_clean = message.strip().lower()
        if decision.action == "create_task" and len(fallback_multi) > 1 and (not only_title or only_title == input_clean):
            args_list = fallback_multi
            used_fallback_parser = True
    return args_list or [], used_fallback_parser


def _handle_non_create_decision(
    message: str,
    decision: Any,
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
) -> tuple[ChatResponse | None, list[dict], bool]:
    if decision.action == "create_task":
        return None, [], False

    # If message looks like a fresh plan, route to create flow even when model misclassifies.
    fallback_multi = split_tasks_from_message(message)
    inferred_action = _infer_fallback_action(message, thread_state)
    if inferred_action == "create_task":
        return None, fallback_multi, True

    if decision.action == "get_schedule":
        requested_schedule_date = extract_date_from_text(message) if _has_explicit_date_cue(message) else effective_thread_date
        tasks = get_schedule(requested_schedule_date, db)
        save_thread_state(
            db,
            thread_key,
            {
                "thread_date": requested_schedule_date,
                "chat_thread_id": chat_thread_id,
                "last_intent_type": "schedule",
                "last_user_message": message,
                "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
            },
        )
        return (
            _build_response(
                mode="schedule",
                message="Here is your schedule.",
                unchanged_tasks=tasks,
                resolved_thread_key=thread_key,
                memory_used=False,
                affected_dates={requested_schedule_date},
            ),
            [],
            False,
        )

    if decision.action == "replan_day":
        replanned, replan_meta = handle_followup_replan(
            message,
            db,
            thread_state=thread_state,
            reference_date=effective_thread_date,
            return_metadata=True,
        )
        if replanned:
            referenced_ids = [t.id for t in replanned][:10]
            affected_dates = {t.date for t in replanned}
            all_day_tasks = []
            for d in affected_dates:
                all_day_tasks.extend(
                    db.query(Task)
                    .filter(Task.date == d, Task.completed == False)
                    .order_by(Task.start_time.asc(), Task.priority.desc(), Task.id.asc())
                    .all()
                )
            replanned_ids = {t.id for t in replanned}
            unchanged = [t for t in all_day_tasks if t.id not in replanned_ids]
            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "replan",
                    "last_user_message": message,
                    "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                    "last_updated_task_ids": referenced_ids,
                    "last_referenced_task_ids": referenced_ids,
                },
            )
            return (
                _build_response(
                    mode="replan",
                    message="Updated existing tasks based on your follow-up request.",
                    updated_tasks=replanned,
                    unchanged_tasks=unchanged,
                    used_replan_handler=True,
                    resolved_thread_key=thread_key,
                    memory_used=bool(replan_meta.get("memory_used")),
                    affected_dates=affected_dates,
                ),
                [],
                False,
            )

        target_phrase = replan_meta.get("target_phrase")
        time_str = replan_meta.get("time_str")
        if replan_meta.get("conflict_task_id"):
            anchor = db.query(Task).filter(Task.id == int(replan_meta["conflict_task_id"])).first()
            conflict_ids = [int(i) for i in replan_meta.get("conflicting_ids", [])]
            conflicts = db.query(Task).filter(Task.id.in_(conflict_ids)).all() if conflict_ids else []
            draft_date = anchor.date if anchor else effective_thread_date
            draft_start = replan_meta.get("proposed_start_time")
            draft_end = replan_meta.get("proposed_end_time")
            duration = int(replan_meta.get("duration_minutes") or 60)
            suggestions = suggest_alternative_slots(
                db=db,
                task_date=draft_date,
                duration_min=duration,
                task_text=f"{anchor.title if anchor else ''} {anchor.description if anchor else ''}",
                limit=3,
            )
            draft = {
                "title": anchor.title if anchor else "Updated task",
                "description": anchor.description if anchor else "Proposed replan update.",
                "date": draft_date,
                "start_time": draft_start,
                "end_time": draft_end,
                "duration_minutes": duration,
                "priority": int(anchor.priority if anchor else 1),
            }
            conflict_titles = ", ".join([t.title for t in conflicts]) if conflicts else "another task"
            move_time_str = draft_start.strftime("%I:%M %p").lstrip("0") if draft_start else "that time"
            pending_intent_id = str(uuid.uuid4())
            choice_pairs: list[tuple[str, str]] = []
            for s in suggestions:
                choice_pairs.append((_format_slot_choice_label(s), f"choose_slot:{s['slot_id']}"))
            choice_pairs.append(("Cancel", "cancel"))
            choice_map = _build_choice_map(choice_pairs)
            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "conflict",
                    "last_user_message": message,
                    "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                    "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                    "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                    "pending_intent_id": pending_intent_id,
                    "pending_state_type": "conflict_resolution",
                    "pending_state": {
                        "mode": "replan",
                        "target_task_id": anchor.id if anchor else None,
                        "draft": {
                            "title": draft["title"],
                            "description": draft["description"],
                            "date": draft_date.isoformat() if draft_date else None,
                            "start_time": draft_start.strftime("%H:%M:%S") if draft_start else None,
                            "end_time": draft_end.strftime("%H:%M:%S") if draft_end else None,
                            "duration_minutes": duration,
                            "priority": int(anchor.priority if anchor else 1),
                        },
                        "conflict_ids": [t.id for t in conflicts],
                        "suggestions": [
                            {
                                "slot_id": s["slot_id"],
                                "date": s["date"].isoformat(),
                                "start_time": s["start_time"].strftime("%H:%M:%S"),
                                "end_time": s["end_time"].strftime("%H:%M:%S"),
                                "score": float(s["score"]),
                                "reason": s["reason"],
                            }
                            for s in suggestions
                        ],
                        "requested_start_time": _time_to_str(replan_meta.get("requested_start_time")),
                        "requested_end_time": _time_to_str(replan_meta.get("requested_end_time")),
                        "choice_map": choice_map,
                    },
                },
            )
            return (
                _build_response(
                    mode="conflict",
                    message=_format_choice_message(
                        f"I infer you want to move {anchor.title if anchor else 'this task'} to {move_time_str}, but it conflicts with {conflict_titles}. Choose a different slot or cancel.",
                        choice_pairs,
                    ),
                    used_replan_handler=True,
                    resolved_thread_key=thread_key,
                    requires_user_input=True,
                    pending_intent_id=pending_intent_id,
                    conflict_info=_build_conflict_prompt(
                        intent_id=pending_intent_id,
                        draft=draft,
                        conflicts=conflicts,
                        suggestions=suggestions,
                        actions=[
                            "choose_slot:<slot_id>",
                            "cancel",
                        ],
                    ),
                ),
                [],
                False,
            )

        return (
            _build_replan_clarification_response(
                message=message,
                db=db,
                thread_key=thread_key,
                thread_state=thread_state,
                effective_thread_date=effective_thread_date,
                chat_thread_id=chat_thread_id,
                meta=replan_meta,
                used_replan_handler=True,
            ),
            [],
            False,
        )

    if decision.action == "delete_task":
        candidate_tasks = _find_delete_candidates(
            db=db,
            message=message,
            thread_state=thread_state,
            reference_date=effective_thread_date,
            limit=3,
        )
        if not candidate_tasks:
            return (
                _build_response(
                    mode="ask_user",
                    message="I couldn't find a matching task to delete.",
                    resolved_thread_key=thread_key,
                    requires_user_input=True,
                    clarification=_build_clarification_prompt(
                        intent_id=str(uuid.uuid4()),
                        field="target_task",
                        question="Which task should I delete?",
                        options=[],
                    ),
                    warnings=["No task candidates were found for deletion."],
                ),
                [],
                False,
            )
        return (
            _build_delete_task_prompt(
                message=message,
                db=db,
                thread_key=thread_key,
                thread_state=thread_state,
                effective_thread_date=effective_thread_date,
                chat_thread_id=chat_thread_id,
                candidate_tasks=candidate_tasks,
            ),
            [],
            False,
        )

    inferred_action = _infer_fallback_action(message, thread_state)
    if inferred_action == "delete_task":
        candidate_tasks = _find_delete_candidates(
            db=db,
            message=message,
            thread_state=thread_state,
            reference_date=effective_thread_date,
            limit=3,
        )
        if candidate_tasks:
            return (
                _build_delete_task_prompt(
                    message=message,
                    db=db,
                    thread_key=thread_key,
                    thread_state=thread_state,
                    effective_thread_date=effective_thread_date,
                    chat_thread_id=chat_thread_id,
                    candidate_tasks=candidate_tasks,
                ),
                [],
                False,
            )
    inferred_label = {
        "replan_day": "replan",
        "get_schedule": "show your schedule",
    }.get(inferred_action, "help with scheduling")
    if inferred_action == "replan_day":
        replanned, replan_meta = handle_followup_replan(
            message,
            db,
            thread_state=thread_state,
            reference_date=effective_thread_date,
            return_metadata=True,
        )
        if replanned:
            referenced_ids = [t.id for t in replanned][:10]
            affected_dates = {t.date for t in replanned}
            all_day_tasks = []
            for d in affected_dates:
                all_day_tasks.extend(
                    db.query(Task)
                    .filter(Task.date == d, Task.completed == False)
                    .order_by(Task.start_time.asc(), Task.priority.desc(), Task.id.asc())
                    .all()
                )
            replanned_ids = {t.id for t in replanned}
            unchanged = [t for t in all_day_tasks if t.id not in replanned_ids]
            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "replan",
                    "last_user_message": message,
                    "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                    "last_updated_task_ids": referenced_ids,
                    "last_referenced_task_ids": referenced_ids,
                },
            )
            return (
                _build_response(
                    mode="replan",
                    message="Updated existing tasks based on your follow-up request.",
                    updated_tasks=replanned,
                    unchanged_tasks=unchanged,
                    used_replan_handler=True,
                    resolved_thread_key=thread_key,
                    memory_used=bool(replan_meta.get("memory_used")),
                    affected_dates=affected_dates,
                ),
                [],
                False,
            )
        return (
            _build_replan_clarification_response(
                message=message,
                db=db,
                thread_key=thread_key,
                thread_state=thread_state,
                effective_thread_date=effective_thread_date,
                chat_thread_id=chat_thread_id,
                meta=replan_meta,
                used_replan_handler=True,
            ),
            [],
            False,
        )
    elif inferred_action == "get_schedule":
        question = "I infer you want to view your schedule. Is that correct?"
    else:
        question = f"I infer you want me to {inferred_label}. Is that correct?"
    options_raw = [
        {"id": "1", "label": "Get today's schedule", "value": "intent:get_schedule"},
        {"id": "2", "label": "Cancel", "value": "intent:cancel"},
    ]
    choice_map = _build_choice_map([(opt["label"], opt["value"]) for opt in options_raw])
    pending_intent_id = _save_ask_user_pending_state(
        db=db,
        thread_key=thread_key,
        thread_state=thread_state,
        effective_thread_date=effective_thread_date,
        chat_thread_id=chat_thread_id,
        message=message,
        field="intent",
        question=question,
        options_raw=options_raw,
        extra_pending_state={
            "mode": "intent_choice",
            "choice_map": choice_map,
        },
    )
    return (
        _build_response(
            mode="ask_user",
            message=question,
            resolved_thread_key=thread_key,
            requires_user_input=True,
            pending_intent_id=pending_intent_id,
            clarification=_build_clarification_prompt(
                intent_id=pending_intent_id,
                field="intent",
                question=question,
                options=options_raw,
                allow_free_text=False,
            ),
        ),
        [],
        False,
    )


def _maybe_return_interactive_create_gate(
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
    message: str,
    decision: Any,
    args_list: list[dict],
    args: dict,
    draft: dict,
) -> ChatResponse | None:
    if (
        len(args_list) == 1
        and decision.action == "create_task"
        and not draft.get("start_time")
        and not bool((args or {}).get("spread", False))
    ):
        question = "What time should I schedule this task?"
        options_raw = _dynamic_time_options_for_draft(db, draft, limit=3)
        pending_intent_id = _save_ask_user_pending_state(
            db=db,
            thread_key=thread_key,
            thread_state=thread_state,
            effective_thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            message=message,
            field="start_time",
            question=question,
            options_raw=options_raw,
            extra_pending_state={
                "draft": {
                    "title": draft["title"],
                    "description": draft["description"],
                    "date": draft["date"].isoformat(),
                    "start_time": None,
                    "end_time": None,
                    "duration_minutes": int(draft["duration_minutes"]),
                    "priority": int(draft["priority"]),
                }
            },
        )
        return _build_ask_user_response(thread_key, pending_intent_id, "start_time", question, options_raw)

    if len(args_list) == 1 and draft.get("start_time") and decision.action == "create_task":
        conflicts, suggestions = detect_conflicts_for_draft(db, draft)
        if conflicts:
            pending_intent_id = str(uuid.uuid4())
            choice_pairs: list[tuple[str, str]] = []
            for s in suggestions:
                choice_pairs.append((_format_slot_choice_label(s), f"choose_slot:{s['slot_id']}"))
            choice_pairs.append(("Keep original time and move conflicts", "keep_original_and_move_conflicts"))
            choice_pairs.append(("Cancel", "cancel"))
            choice_map = _build_choice_map(choice_pairs)
            pending_state = {
                "mode": "create_conflict",
                "draft": {
                    "title": draft["title"],
                    "description": draft["description"],
                    "date": draft["date"].isoformat(),
                    "start_time": draft["start_time"].strftime("%H:%M:%S") if draft.get("start_time") else None,
                    "end_time": draft["end_time"].strftime("%H:%M:%S") if draft.get("end_time") else None,
                    "duration_minutes": int(draft["duration_minutes"]),
                    "priority": int(draft["priority"]),
                },
                "conflict_ids": [t.id for t in conflicts],
                "suggestions": [
                    {
                        "slot_id": s["slot_id"],
                        "date": s["date"].isoformat(),
                        "start_time": s["start_time"].strftime("%H:%M:%S"),
                        "end_time": s["end_time"].strftime("%H:%M:%S"),
                        "score": float(s["score"]),
                        "reason": s["reason"],
                    }
                    for s in suggestions
                ],
                "choice_map": choice_map,
            }
            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "conflict",
                    "last_user_message": message,
                    "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                    "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                    "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                    "pending_intent_id": pending_intent_id,
                    "pending_state_type": "conflict_resolution",
                    "pending_state": pending_state,
                },
            )
            return _build_response(
                mode="conflict",
                message=_format_choice_message(
                    "I found a scheduling conflict. Choose an option.",
                    choice_pairs,
                ),
                resolved_thread_key=thread_key,
                requires_user_input=True,
                pending_intent_id=pending_intent_id,
                conflict_info=_build_conflict_prompt(
                    intent_id=pending_intent_id,
                    draft=draft,
                    conflicts=conflicts,
                    suggestions=suggestions,
                ),
            )
    return None


def _execute_create_args(
    message: str,
    args_list: list[dict],
    decision: Any,
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
) -> tuple[ChatResponse | None, list[Task], set[date], dict, dict, dict, dict]:
    created_tasks = []
    affected_dates = set()
    pinned_task_ids_by_date = {}
    before_state_by_date = {}
    latest_end_by_task_id_by_date: dict[date, dict[int, datetime]] = {}
    intended_times_by_task_id: dict[int, tuple[time | None, time | None]] = {}
    ordered_mode = bool(re.search(r"\b(then|followed by|after that|after this|after)\b", (message or "").lower()))
    last_end_by_date: dict[date, time] = {}
    event_start_by_date: dict[date, time] = {}

    def ensure_before_state(target_date):
        if target_date in before_state_by_date:
            return
        tasks = db.query(Task).filter(Task.date == target_date, Task.completed == False).all()
        before_state_by_date[target_date] = {t.id: (t.date, t.start_time, t.end_time) for t in tasks}

    # Precompute event anchors in the message to keep prep sessions before the event.
    for args in args_list or []:
        raw_text = f"{args.get('title') or ''} {args.get('description') or ''}".strip()
        if not raw_text:
            continue
        if not _is_event_like(raw_text):
            continue
        task_date_str = args.get("date")
        event_date = datetime.strptime(task_date_str, "%Y-%m-%d").date() if task_date_str else effective_thread_date
        start_time_str = args.get("start_time")
        start_time_val = parse_time(start_time_str.strip()) if isinstance(start_time_str, str) and start_time_str.strip() else None
        if not start_time_val:
            start_time_val = extract_time_from_text(raw_text)
        if start_time_val:
            existing = event_start_by_date.get(event_date)
            if not existing or start_time_val < existing:
                event_start_by_date[event_date] = start_time_val

    def find_slot_before(task_date: date, duration_min: int, latest_end: time):
        day_start = datetime.combine(task_date, time(9, 0))
        day_end = datetime.combine(task_date, latest_end)
        now = datetime.now()
        if task_date == now.date():
            day_start = max(day_start, now.replace(second=0, microsecond=0))
        if day_end <= day_start:
            return None
        occupied = []
        for t in db.query(Task).filter(Task.date == task_date, Task.completed == False).all():
            s, e = _task_datetime_range(t)
            if s and e:
                occupied.append((s, e))
        step = timedelta(minutes=15)
        latest_start = day_end - timedelta(minutes=duration_min)
        cur = latest_start
        while cur >= day_start:
            slot_start = cur
            slot_end = cur + timedelta(minutes=duration_min)
            conflict = any(_slot_overlaps(slot_start, slot_end, o_s, o_e) for o_s, o_e in occupied)
            if not conflict:
                return slot_start.time(), slot_end.time()
            cur -= step
        return None

    for args in args_list or []:
        draft = _normalize_task_draft(args=args or {}, message=message, effective_date=effective_thread_date, db=db)
        interactive_gate = _maybe_return_interactive_create_gate(
            db=db,
            thread_key=thread_key,
            thread_state=thread_state,
            effective_thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            message=message,
            decision=decision,
            args_list=args_list,
            args=args or {},
            draft=draft,
        )
        if interactive_gate:
            return (
                interactive_gate,
                created_tasks,
                affected_dates,
                pinned_task_ids_by_date,
                before_state_by_date,
                latest_end_by_task_id_by_date,
                intended_times_by_task_id,
            )

        raw_text = args.get("title") or args.get("description") or message
        text = title_from_text(raw_text)
        description = description_from_text(args.get("description") or raw_text)
        priority = args.get("priority", 1)
        task_date_str = args.get("date")
        start_time_str = args.get("start_time")
        end_time_str = args.get("end_time")
        task_type = args.get("task_type", "other")
        spread = args.get("spread", False)
        total_effort = args.get("total_effort_minutes")
        deadline_str = args.get("deadline")

        task_date = datetime.strptime(task_date_str, "%Y-%m-%d").date() if task_date_str else None
        deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d").date() if deadline_str else None
        base_date = effective_thread_date

        start_time_val = parse_time(start_time_str.strip()) if isinstance(start_time_str, str) and start_time_str.strip() else None
        end_time_val = parse_time(end_time_str.strip()) if isinstance(end_time_str, str) and end_time_str.strip() else None
        has_explicit_start = bool(start_time_val)
        sequence_assigned_start = False
        local_text = f"{args.get('title') or ''} {args.get('description') or ''}".strip()
        local_text_lower = local_text.lower()
        if not start_time_val:
            # Important: only parse time from this task text, not the full message.
            is_prep = any(k in local_text_lower for k in ["prep", "prepare", "study", "revision", "review"])
            event_anchor = None
            effective_date = task_date or base_date
            if is_prep and event_start_by_date.get(effective_date):
                event_anchor = event_start_by_date.get(effective_date)
            if not event_anchor:
                start_time_val = extract_time_from_text(local_text)
            has_explicit_start = bool(start_time_val)

        effective_date = task_date or base_date
        if not total_effort:
            mood = db.query(Mood).filter(Mood.date == effective_date).first()
            energy = mood.energy if mood else 3
            historical_avg = get_historical_avg_duration(db, task_type, energy)
            total_effort = infer_duration_minutes(
                text=text,
                task_type=task_type,
                energy_level=energy,
                historical_avg=historical_avg,
                deadline_date=deadline_date,
            )

        # Preserve narrative order for chained requests: "..., then X, followed by Y ...".
        if ordered_mode and not start_time_val:
            prev_end = last_end_by_date.get(effective_date)
            if prev_end:
                start_time_val = (
                    datetime.combine(effective_date, prev_end) + timedelta(minutes=5)
                ).time()
                sequence_assigned_start = True
            local_text_for_order = f"{args.get('title') or ''} {args.get('description') or ''}".lower()
            # Keep "rest of the night" in evening.
            if "rest of the night" in local_text_for_order and (not start_time_val or start_time_val < time(19, 0)):
                start_time_val = time(19, 0)
                sequence_assigned_start = True
            # Routine anchors for better human-like defaults.
            if any(k in local_text_for_order for k in ["dinner", "eat dinner", "have dinner", "supper"]):
                if not start_time_val or start_time_val < time(19, 0):
                    start_time_val = time(19, 0)
                    sequence_assigned_start = True
            if any(k in local_text_for_order for k in ["sleep", "go to bed", "bedtime"]):
                if not start_time_val or start_time_val < time(22, 0):
                    start_time_val = time(22, 0)
                    sequence_assigned_start = True

        if start_time_val and not end_time_val and total_effort:
            end_time_val = (datetime.combine(effective_date, start_time_val) + timedelta(minutes=total_effort)).time()

        if spread and deadline_date:
            if deadline_date <= base_date:
                session_date = base_date
                duration_min = total_effort
                ensure_before_state(session_date)
                task = create_task(
                    db=db,
                    title=f"{text} (Urgent)",
                    description=description,
                    date=session_date,
                    start_time=start_time_val,
                    end_time=end_time_val,
                    priority=priority + 1,
                    duration_minutes=duration_min,
                )
                affected_dates.add(session_date)
                if start_time_val:
                    pinned_task_ids_by_date.setdefault(session_date, set()).add(task.id)
                    if task.end_time:
                        last_end_by_date[session_date] = task.end_time
                created_tasks.append(task)
                continue

            days_available = (deadline_date - base_date).days
            days_to_plan = min(max(days_available, 1), 7)
            base_session = max(45, total_effort // days_to_plan)
            remaining = total_effort
            for i in range(days_to_plan):
                if remaining <= 0:
                    break
                session_date = base_date + timedelta(days=i)
                duration_min = min(base_session, remaining)
                remaining -= duration_min
                ensure_before_state(session_date)
                task = create_task(
                    db=db,
                    title=f"{text} (Session {i+1})",
                    description=description,
                    date=session_date,
                    start_time=None,
                    end_time=None,
                    priority=priority,
                    duration_minutes=duration_min,
                )
                affected_dates.add(session_date)
                created_tasks.append(task)
            continue

        if not task_date:
            task_date = base_date
        ensure_before_state(task_date)
        # If this is a prep task with an event time anchor, try to place it before the event.
        if not start_time_val:
            is_prep = any(k in local_text_lower for k in ["prep", "prepare", "study", "revision", "review"])
            event_anchor = event_start_by_date.get(task_date)
            if is_prep and event_anchor:
                slot = find_slot_before(task_date, int(total_effort or 60), event_anchor)
                if slot:
                    start_time_val, end_time_val = slot
                    sequence_assigned_start = True
        task = create_task(
            db=db,
            title=text,
            description=description,
            date=task_date,
            start_time=start_time_val,
            end_time=end_time_val,
            priority=priority,
            duration_minutes=total_effort,
        )
        affected_dates.add(task_date)
        if (has_explicit_start or sequence_assigned_start) and start_time_val:
            pinned_task_ids_by_date.setdefault(task_date, set()).add(task.id)
        if has_explicit_start:
            intended_times_by_task_id[task.id] = (start_time_val, end_time_val)
        if any(k in local_text_lower for k in ["prep", "prepare", "study", "revision", "review"]):
            event_anchor = event_start_by_date.get(task_date)
            if event_anchor:
                latest_end_by_task_id_by_date.setdefault(task_date, {})[task.id] = datetime.combine(task_date, event_anchor)
        if task.end_time:
            last_end_by_date[task_date] = task.end_time
        created_tasks.append(task)

    return None, created_tasks, affected_dates, pinned_task_ids_by_date, before_state_by_date, latest_end_by_task_id_by_date, intended_times_by_task_id


def process_chat_request(
    message: str,
    db: Session,
    decide_fn: Callable[[str], Any],
    chat_thread_id: str | None = None,
    thread_date: date | None = None,
    pending_response: dict | None = None,
    confirm: bool | None = None,
) -> ChatResponse:
    today = date.today()
    effective_thread_date = thread_date or today
    thread_key = resolve_thread_key(chat_thread_id, effective_thread_date)
    thread_state = load_thread_state(db, thread_key)

    def finalize(response: ChatResponse) -> ChatResponse:
        persist_chat_turn(
            db=db,
            thread_key=thread_key,
            thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            user_message=message,
            response=response,
        )
        return response

    pending_result = handle_pending_resolution(
        message=message,
        pending_response=pending_response,
        confirm=confirm,
        db=db,
        thread_key=thread_key,
        thread_state=thread_state,
        effective_thread_date=effective_thread_date,
        chat_thread_id=chat_thread_id,
    )
    if pending_result:
        return finalize(pending_result)

    generic_category = _classify_generic_conversation(message, thread_state)
    if generic_category:
        variant_seed = _generic_reply_variant_seed(db, thread_key)
        save_thread_state(
            db,
            thread_key,
            {
                "thread_date": effective_thread_date,
                "chat_thread_id": chat_thread_id,
                "last_intent_type": "respond",
                "last_user_message": message,
                "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                "pending_intent_id": None,
                "pending_state_type": None,
                "pending_state": {},
            },
        )
        return finalize(
            _build_response(
                mode="respond",
                message=_build_generic_conversation_reply(generic_category, message, variant_seed),
                resolved_thread_key=thread_key,
            )
        )

    model_input = _build_model_input_for_decision(
        db=db,
        thread_key=thread_key,
        thread_state=thread_state,
        effective_thread_date=effective_thread_date,
        message=message,
    )
    decision = decide_fn(model_input)

    if decision.action == "ask_user":
        if _looks_like_followup_instruction(message) and _has_thread_task_context(thread_state):
            _, replan_meta = handle_followup_replan(
                message,
                db,
                thread_state=thread_state,
                reference_date=effective_thread_date,
                return_metadata=True,
                dry_run=True,
            )
            return finalize(_build_replan_clarification_response(
                message=message,
                db=db,
                thread_key=thread_key,
                thread_state=thread_state,
                effective_thread_date=effective_thread_date,
                chat_thread_id=chat_thread_id,
                meta=replan_meta,
                used_replan_handler=True,
            ))
        args = decision.arguments if isinstance(decision.arguments, dict) else {}
        field = args.get("field") or "detail"
        question = args.get("question") or decision.message or "Please clarify so I can continue."
        options_raw = args.get("options") if isinstance(args.get("options"), list) else []
        pending_intent_id = _save_ask_user_pending_state(
            db=db,
            thread_key=thread_key,
            thread_state=thread_state,
            effective_thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            message=message,
            field=field,
            question=question,
            options_raw=options_raw,
        )
        return finalize(_build_ask_user_response(thread_key, pending_intent_id, field, question, options_raw))

    non_create_response, fallback_from_non_create, used_fallback_from_non_create = _handle_non_create_decision(
        message=message,
        decision=decision,
        db=db,
        thread_key=thread_key,
        thread_state=thread_state,
        effective_thread_date=effective_thread_date,
        chat_thread_id=chat_thread_id,
    )
    if non_create_response:
        return finalize(non_create_response)

    args_list, used_fallback_parser = _normalize_args_list(decision, message)
    if used_fallback_from_non_create:
        args_list = fallback_from_non_create
        used_fallback_parser = True

    if decision.action == "create_task" and not args_list:
        # Per user request: do not ask generic clarification for create_task.
        return finalize(_build_response(
            mode="create",
            message="No tasks were created because no task details were found.",
            resolved_thread_key=thread_key,
            warnings=["No task details found in the request."],
        ))

    if decision.action == "create_task" and len(args_list) > 1:
        drafts: list[dict] = []
        first_missing_index = None
        ordered_mode = bool(re.search(r"\b(then|followed by|after that|after this|after)\b", (message or "").lower()))
        last_end_time = None
        for idx, args in enumerate(args_list):
            draft = _normalize_task_draft(args=args or {}, message=message, effective_date=effective_thread_date, db=db)
            start_time_val = draft.get("start_time")
            end_time_val = draft.get("end_time")
            explicit_time = bool(args.get("start_time") or start_time_val)
            if start_time_val and not end_time_val:
                end_time_val = (datetime.combine(draft.get("date"), start_time_val) + timedelta(minutes=int(draft.get("duration_minutes") or 60))).time()
            min_start_time = None
            if ordered_mode and last_end_time and not explicit_time:
                min_start_time = last_end_time
            draft_payload = {
                "title": draft.get("title"),
                "description": draft.get("description"),
                "date": draft.get("date").isoformat() if draft.get("date") else effective_thread_date.isoformat(),
                "start_time": start_time_val.strftime("%H:%M:%S") if start_time_val else None,
                "end_time": end_time_val.strftime("%H:%M:%S") if end_time_val else None,
                "duration_minutes": int(draft.get("duration_minutes") or 60),
                "priority": int(draft.get("priority") or 1),
                "explicit_time": explicit_time,
                "min_start_time": min_start_time.strftime("%H:%M:%S") if min_start_time else None,
            }
            drafts.append(draft_payload)
            if not explicit_time and first_missing_index is None:
                first_missing_index = idx
            if end_time_val:
                last_end_time = end_time_val
        if first_missing_index is not None:
            return _build_multi_create_prompt(
                db=db,
                thread_key=thread_key,
                thread_state=thread_state,
                effective_thread_date=effective_thread_date,
                chat_thread_id=chat_thread_id,
                message=message,
                drafts=drafts,
                pending_index=first_missing_index,
            )

    interactive_response, created_tasks, affected_dates, pinned_task_ids_by_date, before_state_by_date, latest_end_by_task_id_by_date, intended_times_by_task_id = _execute_create_args(
        message=message,
        args_list=args_list,
        decision=decision,
        db=db,
        thread_key=thread_key,
        thread_state=thread_state,
        effective_thread_date=effective_thread_date,
        chat_thread_id=chat_thread_id,
        )
    if interactive_response:
        return finalize(interactive_response)

    # If any created task with an explicit time conflicts with existing tasks, ask before moving anything.
    for d in affected_dates:
        existing_ids = set(before_state_by_date.get(d, {}).keys())
        if not existing_ids:
            continue
        existing_tasks = db.query(Task).filter(Task.id.in_(existing_ids)).all()
        existing_ranges = []
        for t in existing_tasks:
            s, e = _task_datetime_range(t)
            if s and e:
                existing_ranges.append((t, s, e))
        for t in created_tasks:
            if t.date != d:
                continue
            intended = intended_times_by_task_id.get(t.id)
            if not intended:
                continue
            start_t, end_t = intended
            if not start_t or not end_t:
                continue
            s = datetime.combine(d, start_t)
            e = datetime.combine(d, end_t)
            conflicts = []
            for other, o_s, o_e in existing_ranges:
                if _slot_overlaps(s, e, o_s, o_e):
                    conflicts.append(other)
            if conflicts:
                pending_intent_id = str(uuid.uuid4())
                conflict_prompt = _build_unscheduled_conflict_prompt(t, conflicts, pending_intent_id)
                choice_tasks = [t] + conflicts
                choice_pairs = [(_format_task_choice_label(task), f"choose_task:{task.id}") for task in choice_tasks]
                choice_pairs.append(("Cancel", "cancel"))
                choice_map = _build_choice_map(choice_pairs)
                save_thread_state(
                    db,
                    thread_key,
                    {
                        "thread_date": effective_thread_date,
                        "chat_thread_id": chat_thread_id,
                        "last_intent_type": "conflict",
                        "last_user_message": message,
                        "last_created_task_ids": [task.id for task in created_tasks][:10],
                        "last_updated_task_ids": [],
                        "last_referenced_task_ids": [t.id] + [c.id for c in conflicts][:9],
                        "pending_intent_id": pending_intent_id,
                        "pending_state_type": "conflict_resolution",
                        "pending_state": {
                            "mode": "unscheduled_conflict",
                            "phase": "choose_task",
                            "unscheduled_task_id": t.id,
                            "conflict_ids": [c.id for c in conflicts],
                            "candidate_move_task_ids": [t.id] + [c.id for c in conflicts],
                            "choice_map": choice_map,
                            "draft": {
                                "title": t.title,
                                "description": t.description,
                                "date": d.isoformat(),
                                "start_time": start_t.strftime("%H:%M:%S"),
                                "end_time": end_t.strftime("%H:%M:%S"),
                                "duration_minutes": task_duration_minutes(t),
                                "priority": int(t.priority or 1),
                            },
                            "unscheduled_intended_start": start_t.strftime("%H:%M:%S"),
                            "unscheduled_intended_end": end_t.strftime("%H:%M:%S"),
                        },
                    },
                )
                conflict_titles = ", ".join([c.title for c in conflicts])
                return finalize(_build_response(
                    mode="conflict",
                    message=_format_choice_message(
                        f"Task {t.title} couldn't be scheduled because it conflicts with {conflict_titles}. Which task should I move?",
                        choice_pairs,
                    ),
                    resolved_thread_key=thread_key,
                    requires_user_input=True,
                    pending_intent_id=pending_intent_id,
                    conflict_info=conflict_prompt,
                    affected_dates=affected_dates,
                ))

    existing_pinned_by_date = {d: set(before_state_by_date.get(d, {}).keys()) for d in affected_dates}
    for d in affected_dates:
        rebalance_day(
            db,
            d,
            pinned_task_ids=(pinned_task_ids_by_date.get(d, set()) | existing_pinned_by_date.get(d, set())),
            latest_end_by_task_id=latest_end_by_task_id_by_date.get(d),
        )

    # Restore existing tasks to their original times to avoid unintended moves.
    for d in affected_dates:
        before_state = before_state_by_date.get(d, {})
        if not before_state:
            continue
        existing_ids = set(before_state.keys())
        tasks = db.query(Task).filter(Task.id.in_(existing_ids)).all()
        changed = False
        for t in tasks:
            before = before_state.get(t.id)
            if not before:
                continue
            _, before_start, before_end = before
            if t.start_time != before_start or t.end_time != before_end:
                t.start_time = before_start
                t.end_time = before_end
                changed = True
        if changed:
            db.commit()

    updated_tasks = _collect_updated_tasks_for_dates(db, before_state_by_date, affected_dates)
    created_ids = {t.id for t in created_tasks}
    updated_tasks = [t for t in updated_tasks if t.id not in created_ids]

    created_ids = [t.id for t in created_tasks][:10]
    updated_ids = [t.id for t in updated_tasks][:10]
    referenced_ids = (updated_ids + created_ids)[:10]
    save_thread_state(
        db,
        thread_key,
        {
            "thread_date": effective_thread_date,
            "chat_thread_id": chat_thread_id,
            "last_intent_type": "create",
            "last_user_message": message,
            "last_created_task_ids": created_ids,
            "last_updated_task_ids": updated_ids,
            "last_referenced_task_ids": referenced_ids,
        },
    )

    return finalize(_build_response(
        mode="create",
        message="Created and scheduled tasks.",
        created_tasks=created_tasks,
        updated_tasks=updated_tasks,
        used_fallback_parser=used_fallback_parser,
        resolved_thread_key=thread_key,
        memory_used=False,
        affected_dates=affected_dates,
    ))
