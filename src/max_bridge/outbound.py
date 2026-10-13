"""Find who a /to message from Telegram is addressed to in MAX."""
import re
from dataclasses import dataclass

MAX_CHOICES = 8

PHONE_RE = re.compile(r"^\+?[\d\-()]{10,18}$")


@dataclass(frozen=True)
class Target:
    chat_id: int
    label: str
    chat_type: str  # "DIALOG" or the MAX chat type
    user_id: int = 0  # the other person, for dialogs


def normalize(text: str) -> str:
    return text.casefold().replace("ё", "е")


def parse_phone(token: str) -> str | None:
    """"+7 999 123-45-67" / "89991234567" / "9991234567" → "+79991234567"."""
    if not PHONE_RE.match(token):
        return None
    digits = re.sub(r"\D", "", token)
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) != 11 or digits[0] != "7":
        return None
    return "+" + digits


def find_targets(query: str, people: dict[int, str], chats: dict[int, tuple[str, str]],
                 my_id: int) -> list[Target]:
    """People and chats whose name contains every word of the query.

    people: MAX user id → name; chats: chat id → (type, title).
    Names matching the whole query exactly come first, then names where
    every query word starts a word of the name, then the rest.
    """
    words = normalize(query).split()
    if not words:
        return []

    def rank(name: str) -> int | None:
        norm = normalize(name)
        if not all(w in norm for w in words):
            return None
        if norm == " ".join(words):
            return 0
        name_words = norm.split()
        if all(any(nw.startswith(w) for nw in name_words) for w in words):
            return 1
        return 2

    found = []
    for user_id, name in people.items():
        r = rank(name)
        if r is not None:
            found.append((r, name, Target(my_id ^ user_id, name, "DIALOG", user_id)))
    for chat_id, (chat_type, title) in chats.items():
        r = rank(title) if title else None
        if r is not None:
            found.append((r, title, Target(chat_id, title, chat_type)))
    found.sort(key=lambda f: (f[0], normalize(f[1])))
    return [f[2] for f in found]


def split_command(args: str, lookup) -> tuple[str, list[Target], str]:
    """Split "/to" arguments into (query, matches, text).

    With a line break the first line is who and the rest is the text.
    Otherwise the longest leading run of words that matches someone is who:
    "Мария Сидорова привет" → query "Мария Сидорова", text "привет".
    """
    args = args.strip()
    if "\n" in args:
        query, _, text = args.partition("\n")
        return query.strip(), lookup(query.strip()), text.strip()
    parts = args.split()
    for n in range(len(parts), 0, -1):
        query = " ".join(parts[:n])
        matches = lookup(query)
        if matches:
            rest = args.split(None, n)
            return query, matches, rest[n].strip() if len(rest) > n else ""
    return parts[0] if parts else "", [], ""
