"""Intent classifier for incoming messages: decides what can be answered locally without an LLM.
Trained per tenant on the seed examples below plus the tenant's own `playbook.intent_examples`."""
import re

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

# English plus common Indian transliterated phrasing. "other" = anything that needs real reasoning -> LLM.
SEED_EXAMPLES = {
    "greeting": ["hi", "hello", "hey there", "good morning", "good evening", "hii", "helo sir", "vanakkam", "namaste",
                 "hi team", "hello, anyone there?"],
    "thanks": ["thanks", "thank you", "thank you so much", "ok thanks", "great thanks", "nandri", "dhanyavaad", "thx",
               "ok got it, thanks", "super, thank you"],
    "price": ["what is the price of", "price of 20L white emulsion", "how much is", "rate for", "cost of 10 litre paint",
              "price please", "send me the rate for putty 40kg", "how much for 5 tins", "quote for 10 buckets",
              "price list for primer", "evvalavu", "kitna hai", "rate enna", "what's the cost", "mrp of"],
    "stock": ["is it available", "do you have it in stock", "available now?", "stock irukka", "in stock?",
              "when will it be available", "is the 20L pack available", "do you have white 10L", "availability of putty"],
    "human": ["talk to a person", "call me", "i want to speak to someone", "connect me to sales", "agent please",
              "can someone call me back", "human please", "manager number", "give me your phone number"],
    "other": ["which colour suits my bedroom", "my roof is leaking what should i use", "can you give a discount",
              "my order was damaged", "i want to become a dealer", "how long does paint take to dry",
              "compare your emulsion with asian paints", "can you visit my site tomorrow", "i need help choosing",
              "the delivery is late", "what do you recommend for exterior walls in coastal area", "cancel my order",
              "my last invoice is wrong", "do you give credit for contractors", "need 500 litres for a project next month"],
}

QTY_PATTERNS = [
    r"\b(?:qty|quantity|need|want|order|send)\s*[:=]?\s*(\d{1,6})\b(?!\s*(?:l|ltr|litre|liter|kg|ml|g)\b)",
    r"\b(\d{1,6})\s*(?:pcs|pieces|nos|no\.|units|tins|buckets|bags|boxes|packs|drums|cans|sets)\b",
    r"\b(\d{1,6})\s*(?:x|×)\s",
]


FILLER_WORDS = {
    "price", "prices", "rate", "rates", "cost", "mrp", "quote", "quotation", "how", "much", "many", "what", "whats",
    "is", "are", "the", "a", "an", "of", "for", "to", "me", "my", "i", "we", "you", "your", "do", "does", "have", "has",
    "need", "want", "send", "give", "please", "pls", "sir", "available", "availability", "in", "stock", "any", "it",
    "qty", "quantity", "pcs", "pieces", "nos", "units", "tins", "buckets", "bags", "boxes", "packs", "drums", "cans",
    "and", "with", "list", "kitna", "hai", "enna", "evvalavu", "irukka",
}


def product_query(text: str) -> str:
    """Strips request words and bare counts so only the product description is matched ('20L' and '40kg' stay)."""
    words = re.findall(r"[\w.]+", text.lower())
    return " ".join(w for w in words if w not in FILLER_WORDS and not w.isdigit())


def extract_qty(text: str) -> float | None:
    """Quantity if the customer stated one explicitly; pack sizes like '20L' or '40kg' are not quantities."""
    t = text.lower()
    for pattern in QTY_PATTERNS:
        if m := re.search(pattern, t):
            return float(m.group(1))
    return None


class IntentModel:
    def __init__(self, extra_examples: dict[str, list[str]] | None = None):
        examples = {k: list(v) for k, v in SEED_EXAMPLES.items()}
        for intent, texts in (extra_examples or {}).items():
            examples.setdefault(intent, []).extend(texts)
        self.texts = [t for texts in examples.values() for t in texts]
        self.labels = [intent for intent, texts in examples.items() for _ in texts]
        self.pipeline = make_pipeline(
            TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), lowercase=True, sublinear_tf=True),
            LogisticRegression(max_iter=2000, C=8),
        ).fit(self.texts, self.labels)

    def predict(self, text: str) -> tuple[str, float]:
        probs = self.pipeline.predict_proba([text])[0]
        best = probs.argmax()
        return str(self.pipeline.classes_[best]), float(probs[best])
