"""Word packs: built-in words (Classic), word pairs (Undercover) and host-made custom packs."""

from __future__ import annotations

import json
import random
from pathlib import Path

MAX_PACK_NAME = 30
MAX_WORD_LEN = 30
MIN_PACK_WORDS = 6
MAX_PACK_WORDS = 80


class WordBankError(ValueError):
    pass


class WordBank:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.builtin: dict[str, list[str]] = self._load("words.json")
        self.pairs: dict[str, list[list[str]]] = self._load("pairs.json")
        self.custom: dict[str, list[str]] = self._load("custom_words.json")

    def _load(self, name: str) -> dict:
        path = self.data_dir / name
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_custom(self):
        path = self.data_dir / "custom_words.json"
        path.write_text(json.dumps(self.custom, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---------- lookups ----------

    @property
    def all_words(self) -> dict[str, list[str]]:
        return {**self.builtin, **self.custom}

    def has_category(self, name: str) -> bool:
        return name in self.all_words

    def words(self, category: str) -> list[str]:
        return list(self.all_words.get(category, []))

    def categories(self) -> list[dict]:
        return [
            {"name": name, "words": len(words), "pairs": len(self.pairs.get(name, [])), "custom": name in self.custom}
            for name, words in self.all_words.items()
        ]

    # ---------- picking ----------

    def pick_word(self, category: str, used: set[str], rng: random.Random) -> tuple[str, str]:
        """Classic mode: one secret word. Avoids repeats until a category runs out."""
        packs = self.all_words
        if category not in packs:
            category = rng.choice(list(packs))
        pool = packs[category]
        fresh = [w for w in pool if w not in used]
        if not fresh:
            used.difference_update(pool)
            fresh = list(pool)
        word = rng.choice(fresh)
        used.add(word)
        return category, word

    def pick_pair(self, category: str, used: set[str], rng: random.Random) -> tuple[str, str, str]:
        """Undercover mode: (category, civilian_word, undercover_word)."""
        if category not in self.pairs:
            category = rng.choice(list(self.pairs))
        pool = self.pairs[category]
        keys = ["|".join(p) for p in pool]
        fresh = [p for p, k in zip(pool, keys) if k not in used]
        if not fresh:
            used.difference_update(keys)
            fresh = list(pool)
        pair = rng.choice(fresh)
        used.add("|".join(pair))
        civ, spy = (pair[0], pair[1]) if rng.random() < 0.5 else (pair[1], pair[0])
        return category, civ, spy

    # ---------- custom packs ----------

    def add_pack(self, name: str, words) -> str:
        name = " ".join(str(name or "").split())[:MAX_PACK_NAME]
        if not name:
            raise WordBankError("Give the pack a name.")
        if name in self.builtin:
            raise WordBankError("That name is used by a built-in pack.")
        if isinstance(words, str):
            words = words.replace("\n", ",").split(",")
        cleaned, seen = [], set()
        for w in words or []:
            w = " ".join(str(w).split())[:MAX_WORD_LEN]
            if w and w.lower() not in seen:
                seen.add(w.lower())
                cleaned.append(w)
        if len(cleaned) < MIN_PACK_WORDS:
            raise WordBankError(f"A pack needs at least {MIN_PACK_WORDS} different words.")
        self.custom[name] = cleaned[:MAX_PACK_WORDS]
        self._save_custom()
        return name

    def delete_pack(self, name: str):
        if name not in self.custom:
            raise WordBankError("Only custom packs can be deleted.")
        del self.custom[name]
        self._save_custom()
