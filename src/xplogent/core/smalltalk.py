"""Recognise small talk ("hi", "thanks!", "how are you?") so it is treated as such.

Chat sends no tools for it: a small local model used to answer "hi" by calling
``read_file``, failing, and then "learning" facts from its own error. On calls it is
not a knowledge gap: "hello" is not a question the knowledge base failed to answer.
"""

from __future__ import annotations

import re

_GREETING = re.compile(
    r"^(hi+|hey+|hello+|hiya|yo|howdy|greetings|good (morning|afternoon|evening|day)|"
    r"thanks?( you)?( so much| a lot)?|thank you|thx|ty|cheers|ok(ay)?|cool|great|nice|"
    r"bye|goodbye|see you|how are you( doing)?( today)?|how('s| is) it going|what'?s up|sup|"
    r"salam|assalam(u|o) ?alaikum|aoa|hola|bonjour)\b", re.I)
# Words that turn a greeting into a request ("hi, can you read my notes?").
_REQUEST = re.compile(r"\b(can|could|would|will|please|help|show|tell|read|write|make|create|list|find|"
                      r"run|open|check|what|why|when|where|which|who|how (do|does|can|much|many|long))\b", re.I)


def is_small_talk(text: str, max_words: int = 6) -> bool:
    t = " ".join(str(text or "").split()).strip(" .!?,;:-~*")
    if not t:
        return True
    words = t.split()
    if len(words) > max_words or not _GREETING.match(t):
        return False
    rest = _GREETING.sub("", t, count=1)
    return not _REQUEST.search(rest)
