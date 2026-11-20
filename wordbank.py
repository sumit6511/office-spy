"""Word packs: built-in words (Classic), word pairs (Undercover) and host-made custom packs."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import tempfile
from pathlib import Path

log = logging.getLogger("office_spy")

MAX_PACK_NAME = 30
MAX_WORD_LEN = 30
MIN_PACK_WORDS = 6
MAX_PACK_WORDS = 80
MIN_PACK_PAIRS = 3
MAX_PACK_PAIRS = 40
PAIR_SEPARATORS = re.compile(r"\s*(?:/|\||\t|,|\s+vs\.?\s+)\s*", re.IGNORECASE)


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
        self.custom_pairs: dict[str, list[list[str]]] = self._load("custom_pairs.json", pairs=True)

    def _load(self, name: str, pairs: bool = False) -> dict:
        data = read_json(self.data_dir / name)
        # Keep only well-formed packs, so one bad entry doesn't break the whole game.
        out = {}
        for k, v in data.items():
            if not isinstance(k, str) or not isinstance(v, list):
                continue
            if pairs:
                v = [p for p in v if isinstance(p, list) and len(p) == 2 and all(isinstance(w, str) and w for w in p)]
            if v:
                out[k] = v
        return out

    def _save_custom(self):
        write_json_atomic(self.data_dir / "custom_words.json", self.custom)

    def _save_custom_pairs(self):
        write_json_atomic(self.data_dir / "custom_pairs.json", self.custom_pairs)

    # ---------- lookups ----------

    @property
    def all_words(self) -> dict[str, list[str]]:
        return {**self.builtin, **self.custom}

    @property
    def all_pairs(self) -> dict[str, list[list[str]]]:
        return {**self.pairs, **self.custom_pairs}

    def has_category(self, name: str) -> bool:
        return name in self.all_words or name in self.all_pairs

    def words(self, category: str) -> list[str]:
        return list(self.all_words.get(category, []))

    def categories(self) -> list[dict]:
        words, pairs = self.all_words, self.all_pairs
        names = list(dict.fromkeys([*words, *pairs]))  # keep order, no repeats
        return [
            {"name": n, "words": len(words.get(n, [])), "pairs": len(pairs.get(n, [])),
             "custom": n in self.custom or n in self.custom_pairs}
            for n in names
        ]

    def _builtin_name(self, name: str) -> bool:
        return name in self.builtin or name in self.pairs

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
        packs = self.all_pairs
        if not packs:
            raise WordBankError("There are no Undercover word pairs. Restore data/pairs.json.")
        if category not in packs:
            category = rng.choice(list(packs))
        pool = packs[category]
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
        if self._builtin_name(name):
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

    def add_pairs(self, name: str, pairs) -> str:
        """Undercover pairs for a custom pack: one pair per line, like "Printer / Scanner"."""
        name = " ".join(str(name or "").split())[:MAX_PACK_NAME]
        if not name:
            raise WordBankError("Give the pack a name.")
        if self._builtin_name(name):
            raise WordBankError("That name is used by a built-in pack.")
        if isinstance(pairs, str):
            pairs = [PAIR_SEPARATORS.split(line.strip()) for line in pairs.splitlines() if line.strip()]
        cleaned, seen = [], set()
        for i, pair in enumerate(pairs or [], 1):
            if not isinstance(pair, (list, tuple)):
                continue
            words = [" ".join(str(w).split())[:MAX_WORD_LEN] for w in pair]
            words = [w for w in words if w]
            if len(words) != 2:
                raise WordBankError(f"Line {i} needs exactly two words, like: Printer / Scanner")
            a, b = words
            if re.sub(r"[^a-z0-9]", "", a.lower()) == re.sub(r"[^a-z0-9]", "", b.lower()):
                raise WordBankError(f"Line {i}: the two words must be different.")
            key = frozenset((a.lower(), b.lower()))
            if key not in seen:
                seen.add(key)
                cleaned.append([a, b])
        if len(cleaned) < MIN_PACK_PAIRS:
            raise WordBankError(f"Add at least {MIN_PACK_PAIRS} different pairs, one per line.")
        self.custom_pairs[name] = cleaned[:MAX_PACK_PAIRS]
        self._save_custom_pairs()
        return name

    def delete_pack(self, name: str):
        """Delete a custom pack: its Classic words and its Undercover pairs."""
        if name not in self.custom and name not in self.custom_pairs:
            raise WordBankError("Only custom packs can be deleted.")
        if self.custom.pop(name, None) is not None:
            self._save_custom()
        if self.custom_pairs.pop(name, None) is not None:
            self._save_custom_pairs()
