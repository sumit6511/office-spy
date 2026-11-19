"""Word packs: built-in words (Classic), word pairs (Undercover) and host-made custom packs."""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
from pathlib import Path

log = logging.getLogger("office_spy")

MAX_PACK_NAME = 30
MAX_WORD_LEN = 30
MIN_PACK_WORDS = 6
MAX_PACK_WORDS = 80


class WordBankError(ValueError):
    pass


def read_json(path: Path) -> dict:
    """Read a JSON object, or return {} (with a warning) if the file is missing or unreadable."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("Ignoring %s: it couldn't be read (%s)", path.name, e)
        return {}
    if not isinstance(data, dict):
        log.warning("Ignoring %s: expected a JSON object", path.name)
        return {}
    return data


def write_json_atomic(path: Path, data) -> None:
    """Write JSON so a crash mid-save can never leave a half-written file behind."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class WordBank:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.builtin: dict[str, list[str]] = self._load("words.json")
        self.pairs: dict[str, list[list[str]]] = self._load("pairs.json")
        self.custom: dict[str, list[str]] = self._load("custom_words.json")

    def _load(self, name: str) -> dict:
        data = read_json(self.data_dir / name)
        # Keep only well-formed packs, so one bad entry doesn't break the whole game.
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, list) and v}

    def _save_custom(self):
        write_json_atomic(self.data_dir / "custom_words.json", self.custom)

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
        if not packs:
            raise WordBankError("There are no word packs. Add one, or restore data/words.json.")
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
        if not self.pairs:
            raise WordBankError("There are no Undercover word pairs. Restore data/pairs.json.")
        if category not in self.pairs:
            category = rng.choice(list(self.pairs))
        pool = self.pairs[category]
        keys = ["|".join(p) for p in pool]
        fresh = [p for p, k in zip(pool, keys, strict=True) if k not in used]
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
