"""Extract implicit quality signals from user messages."""
import re

# --- regex patterns ---

_CORRECTION_PATTERNS = [
    r"\bactually\b(?=.*\b(wrong|incorrect|not right|not what|mistaken|no|disagree)\b)",
    r"\bno[,.]?\s+(that'?s?|it'?s?)\s+(wrong|incorrect|not right)\b",
    r"\bthat'?s?\s+not\s+(right|correct|what i (meant|said|asked))\b",
    r"\bwait[,.]?\s+no\b",
    r"\bi meant\b",
    r"\byou('re| are) (wrong|incorrect|mistaken|off)\b",
    r"\bincorrect\b",
    r"\bwrong\b",
    r"\bnot quite\b",
    r"\bnot (exactly|really)\b",
    r"\byou misunderstood\b",
    r"\bthat'?s?\s+not\s+(it|what i|correct|right)\b",
    r"\bthat'?s?\s+(wrong|incorrect|off|inaccurate)\b",
    r"\bno[,\s](?!\s*(problem|worries|rush|need|thanks|thank|issue|doubt))",
    r"^no$",                             # standalone "no"
    r"\byou('ve| have) (got it wrong|misunderstood|missed)\b",
    r"\bnot what i (asked|meant|said|wanted)\b",
    r"\blet me (correct|clarify|rephrase)\b",
    r"\bthat('s| is) (not|incorrect|wrong)\b",
]

_FOLLOW_UP_PATTERNS = [
    r"\?",                            # any question
    r"\bwhat (else|about|if)\b",
    r"\btell me more\b",
    r"\bcan you (also|elaborate|explain)\b",
    r"\band (how|what|why|when|where)\b",
    r"\bhow about\b",
    r"\bwhat about\b",
    r"\balso (check|look|search|try|include|add)\b",
    r"\bshow me\b",
    r"\bmore (details?|info|about|options?)\b",
    r"\bany (other|more|remote|similar|alternatives?)\b",
    r"\b(look|check|search|find)\b.*\b(too|as well|also)\b",
    r"\bnarrow\b",
    r"\bfilter\b",
    r"\bexpand\b",
    r"\bsimilar\b",
    r"\bkeep (going|looking|searching)\b",
    r"\bcontinue\b",
    r"\bgo (on|ahead|deeper)\b",
]

_IGNORED_PATTERNS = [
    r"^(ok|okay|sure|k|thanks?|thank you|got it|noted|cool|great|good|fine|alright|nice|perfect|awesome|lovely|brilliant|cheers|yes|yep|yeah|yup)[\.\!]?$",
    r"^(ok|okay|sure)[\s,]+(thanks?|thank you|great|good|cool|cheers|perfect)[\.\!]?$",
    r"^(thanks?|thank you)[\s,]+(that'?s?[\s]*)?(great|good|helpful|perfect)[\.\!]?$",
]


def detect_signals(message: str) -> tuple[bool, bool, bool]:
    """Return (corrected, followed_up, ignored) for a user message."""
    text = message.strip().lower()

    corrected = any(re.search(p, text) for p in _CORRECTION_PATTERNS)
    followed_up = any(re.search(p, text) for p in _FOLLOW_UP_PATTERNS)
    ignored = bool(re.fullmatch(r"[a-z\.\! ]+", text)) and any(
        re.fullmatch(p, text) for p in _IGNORED_PATTERNS
    )

    return corrected, followed_up, ignored
