"""
Office Spy game engine.

Pure game logic with no networking, so it can be unit-tested. The server
calls these methods and passes in the current time; `tick(now)` advances any
phase whose timer has run out.

Modes
  classic     Everyone gets the same word except the spy(ies), who know they
              are the spy. One vote. A caught spy gets a last guess at the word.
  undercover  The undercover agent(s) get a *similar* word and don't know they
              are undercover. Vote out one player per round until a side wins.

Clue styles
  talk        Discuss out loud against a timer, then vote.
  typed       Players type a short clue on their phone in turn, then vote.
"""

from __future__ import annotations

import logging
import random
import re
from collections import Counter
from dataclasses import dataclass

from wordbank import WordBank, WordBankError

log = logging.getLogger("office_spy")

MIN_PLAYERS = 3
MAX_PLAYERS = 20
MAX_NAME = 20
MAX_CLUE = 30
TURN_SECONDS = 45
VOTE_SECONDS = 90
GUESS_SECONDS = 45
REVEAL_SECONDS = 10
HOST_HANDOVER_SECONDS = 15
OFFLINE_TURN_GRACE = 6

LOBBY, PLAYING, VOTING, REVEAL, GUESS, RESULTS = "lobby", "playing", "voting", "reveal", "guess", "results"
CLASSIC, UNDERCOVER = "classic", "undercover"
SPIES, CIVILIANS = "spies", "civilians"

DEFAULT_SETTINGS = {
    "mode": CLASSIC,
    "clue_mode": "talk",
    "category": "Random",
    "spies": 1,
    "minutes": 5,
    "clue_rounds": 1,
    "spy_sees_category": True,
}


CODENAME_A = ["Silent", "Midnight", "Crimson", "Velvet", "Hidden", "Golden", "Frozen", "Paper",
              "Quiet", "Iron", "Hollow", "Lucky", "Monsoon", "Smoky", "Copper", "Foggy"]
CODENAME_B = ["Momo", "Yak", "Rhino", "Teacup", "Temple", "Kite", "Lantern", "Himal",
              "Sparrow", "Tiger", "Prayer Flag", "Microbus", "Chiya", "Peacock", "Rickshaw", "Sel Roti"]


class GameError(Exception):
    """A user action that isn't allowed. The message is shown to that player."""


@dataclass
class Player:
    id: str
    name: str
    score: int = 0
    wins: int = 0
    games: int = 0
    online: bool = True
    offline_since: float | None = None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _clean(s, limit: int) -> str:
    return " ".join(str(s or "").split())[:limit]


class Game:
    def __init__(self, bank: WordBank, rng: random.Random | None = None):
        self.bank = bank
        self.rng = rng or random.Random()
        self.players: dict[str, Player] = {}
        self.host_id: str | None = None
        self.settings = dict(DEFAULT_SETTINGS)
        self.phase = LOBBY
        self.round = 0
        self.codename = ""
        self.used: set[str] = set()
        self._reset_round()

    def _reset_round(self):
        self.mode = self.settings["mode"]
        self.clue_mode = self.settings["clue_mode"]
        self.round_players: list[str] = []
        self.alive: list[str] = []
        self.order: list[str] = []  # speaking order for the current discussion
        self.spies: set[str] = set()  # spies (classic) or undercover agents
        self.category: str | None = None
        self.word: str | None = None  # civilians' word
        self.spy_word: str | None = None  # undercover agents' word
        self.deadline: float | None = None
        self.deadline_len: float | None = None
        self.discussion = 0
        self.turn = 0
        self.clue_round = 1
        self.clues: list[dict] = []
        self.votes: dict[str, str] = {}
        self.eliminated: list[str] = []
        self.last_out: str | None = None
        self.tie = False
        self.accused: str | None = None
        self.self_reveal = False
        self.guess_options: list[str] = []
        self.guess: str | None = None
        self.winner: str | None = None
        self.reason = ""

    # ------------------------------------------------------------------ helpers

    def name(self, pid: str | None) -> str:
        p = self.players.get(pid) if pid else None
        return p.name if p else "?"

    def _require_player(self, pid: str) -> Player:
        p = self.players.get(pid)
        if not p:
            raise GameError("Join the game first.")
        return p

    def _require_host(self, pid: str):
        self._require_player(pid)
        if pid != self.host_id:
            raise GameError("Only the host can do that.")

    def _require_phase(self, *phases: str):
        if self.phase not in phases:
            raise GameError("You can't do that right now.")

    def _set_timer(self, now: float, seconds: float | None):
        self.deadline = now + seconds if seconds else None
        self.deadline_len = seconds

    def word_for(self, pid: str) -> str | None:
        if pid not in self.round_players:
            return None
        if pid in self.spies:
            return self.spy_word if self.mode == UNDERCOVER else None
        return self.word

    @property
    def current_turn(self) -> str | None:
        if self.phase == PLAYING and self.clue_mode == "typed" and self.order:
            return self.order[self.turn]
        return None

    @property
    def spy_label(self) -> str:
        return "undercover" if self.mode == UNDERCOVER else "spy"

    # ------------------------------------------------------------------ players

    def join(self, pid: str, name, now: float):
        name = _clean(name, MAX_NAME)
        if not name:
            raise GameError("Please enter a name.")
        me = self.players.get(pid)
        clash = next((p for p in self.players.values() if p.name.lower() == name.lower() and p.id != pid), None)
        if clash:
            if clash.online or me:
                raise GameError("That name is taken.")
            if clash.id in self.round_players and self.phase not in (LOBBY, RESULTS):
                # Their seat holds a secret role right now. Handing it over by name alone would let
                # anyone read another player's word, so they must come back on their own device.
                raise GameError("That player is in this round. Reopen the game on their original "
                                "phone, or wait for the round to end.")
            # Same person on a new tab/device: hand their old seat (and score) over.
            self._rekey(clash.id, pid)
            clash.online, clash.offline_since = True, None
            log.info("%s rejoined", clash.name)
            return
        if not me:
            if len(self.players) >= MAX_PLAYERS:
                raise GameError("The game is full.")
            me = self.players[pid] = Player(pid, name)
            log.info("%s joined (%d players)", name, len(self.players))
        me.name = name
        me.online, me.offline_since = True, None
        if self.host_id not in self.players:
            self.host_id = pid

    def _rekey(self, old: str, new: str):
        def sub(x):
            return new if x == old else x

        self.players = {sub(k): v for k, v in self.players.items()}
        self.players[new].id = new
        self.round_players = [sub(x) for x in self.round_players]
        self.alive = [sub(x) for x in self.alive]
        self.order = [sub(x) for x in self.order]
        self.eliminated = [sub(x) for x in self.eliminated]
        self.spies = {sub(x) for x in self.spies}
        self.votes = {sub(k): sub(v) for k, v in self.votes.items()}
        for c in self.clues:
            c["pid"] = sub(c["pid"])
        self.accused, self.last_out, self.host_id = sub(self.accused), sub(self.last_out), sub(self.host_id)

    def set_online(self, pid: str, online: bool, now: float):
        p = self.players.get(pid)
        if p:
            p.online = online
            p.offline_since = None if online else now

    def leave(self, pid: str):
        self._require_player(pid)
        self._require_phase(LOBBY)
        self._remove(pid)

    def kick(self, pid: str, target: str):
        self._require_host(pid)
        self._require_phase(LOBBY, RESULTS)
        if target == pid or target not in self.players:
            raise GameError("Can't remove that player.")
        self._remove(target)

    def _remove(self, pid: str):
        p = self.players.pop(pid)
        log.info("%s left", p.name)
        if self.phase == RESULTS:
            self.back_to_lobby(self.host_id if self.host_id != pid else pid, force=True)
        if self.host_id == pid:
            online = [x.id for x in self.players.values() if x.online]
            self.host_id = (online or list(self.players) or [None])[0]

    def make_host(self, pid: str, target: str):
        self._require_host(pid)
        if target not in self.players:
            raise GameError("Unknown player.")
        self.host_id = target

    def reset_scores(self, pid: str):
        self._require_host(pid)
        for p in self.players.values():
            p.score = p.wins = p.games = 0

    # ------------------------------------------------------------------ settings & packs

    def update_settings(self, pid: str, changes: dict):
        self._require_host(pid)
        self._require_phase(LOBBY, RESULTS)
        s = self.settings
        c = changes if isinstance(changes, dict) else {}
        if c.get("mode") in (CLASSIC, UNDERCOVER):
            s["mode"] = c["mode"]
        if c.get("clue_mode") in ("talk", "typed"):
            s["clue_mode"] = c["clue_mode"]
        if c.get("category") == "Random" or self.bank.has_category(c.get("category", "")):
            s["category"] = c["category"]
        if c.get("spies") in (1, 2, 3):
            s["spies"] = c["spies"]
        if c.get("minutes") in (1, 2, 3, 4, 5, 6, 8, 10, 15):
            s["minutes"] = c["minutes"]
        if c.get("clue_rounds") in (1, 2, 3):
            s["clue_rounds"] = c["clue_rounds"]
        if isinstance(c.get("spy_sees_category"), bool):
            s["spy_sees_category"] = c["spy_sees_category"]

    def add_pack(self, pid: str, name, words):
        self._require_host(pid)
        # Not mid-round: replacing the current category's pack would drop the secret word from the
        # spy's guess list, so they could never guess correctly.
        self._require_phase(LOBBY, RESULTS)
        try:
            self.settings["category"] = self.bank.add_pack(name, words)
        except WordBankError as e:
            raise GameError(str(e)) from None

    def delete_pack(self, pid: str, name):
        self._require_host(pid)
        self._require_phase(LOBBY, RESULTS)
        try:
            self.bank.delete_pack(name)
        except WordBankError as e:
            raise GameError(str(e)) from None
        if self.settings["category"] == name:
            self.settings["category"] = "Random"

    # ------------------------------------------------------------------ round flow

    def start(self, pid: str, now: float):
        self._require_host(pid)
        self._require_phase(LOBBY, RESULTS)
        online = [p.id for p in self.players.values() if p.online]
        if len(online) < MIN_PLAYERS:
            raise GameError(f"Need at least {MIN_PLAYERS} connected players.")

        self._reset_round()
        self.round += 1
        self.codename = f"Operation {self.rng.choice(CODENAME_A)} {self.rng.choice(CODENAME_B)}"
        self.rng.shuffle(online)
        self.round_players = online[:]
        self.alive = online[:]
        n_spies = min(self.settings["spies"], max(1, (len(online) - 1) // 2))
        self.spies = set(self.rng.sample(online, n_spies))

        if self.mode == UNDERCOVER:
            self.category, self.word, self.spy_word = self.bank.pick_pair(self.settings["category"], self.used, self.rng)
        else:
            self.category, self.word = self.bank.pick_word(self.settings["category"], self.used, self.rng)
        for p in online:
            self.players[p].games += 1

        # Note: the secret word is deliberately never logged, since the host is usually playing too.
        log.info("Round %d: %s mode, %s clues, %d players, %d %s", self.round, self.mode,
                 self.clue_mode, len(online), n_spies, self.spy_label)
        self._start_discussion(now)

    def _start_discussion(self, now: float):
        self.phase = PLAYING
        self.discussion += 1
        k = self.rng.randrange(len(self.alive))
        self.order = self.alive[k:] + self.alive[:k]
        self.turn, self.clue_round = 0, 1
        self.votes = {}
        if self.clue_mode == "typed":
            self._set_timer(now, TURN_SECONDS)
        else:
            self._set_timer(now, self.settings["minutes"] * 60)

    def submit_clue(self, pid: str, text, now: float):
        self._require_player(pid)
        if self.phase != PLAYING or self.clue_mode != "typed":
            raise GameError("Not taking clues right now.")
        if pid != self.current_turn:
            raise GameError("It's not your turn.")
        text = _clean(text, MAX_CLUE)
        if not text:
            raise GameError("Type a clue first.")
        own = self.word_for(pid)
        if own and _norm(own) in _norm(text):
            raise GameError("Your clue can't contain the secret word!")
        self._add_clue(pid, text)
        self._advance_turn(now)

    def skip_turn(self, pid: str, now: float):
        self._require_host(pid)
        if self.current_turn is None:
            raise GameError("Nobody's turn to skip.")
        self._add_clue(self.current_turn, None)
        self._advance_turn(now)

    def _add_clue(self, pid: str, text: str | None):
        self.clues.append({"pid": pid, "text": text, "d": self.discussion, "r": self.clue_round})

    def _advance_turn(self, now: float):
        self.turn += 1
        if self.turn >= len(self.order):
            self.turn = 0
            self.clue_round += 1
            if self.clue_round > self.settings["clue_rounds"]:
                return self._begin_vote(now)
        self._set_timer(now, TURN_SECONDS)

    def start_vote(self, pid: str, now: float):
        self._require_host(pid)
        self._require_phase(PLAYING)
        self._begin_vote(now)

    def _begin_vote(self, now: float):
        self.phase = VOTING
        self.votes = {}
        self._set_timer(now, VOTE_SECONDS)

    def vote(self, pid: str, target, now: float):
        self._require_player(pid)
        self._require_phase(VOTING)
        if pid not in self.alive:
            raise GameError("You're not voting this round.")
        if target not in self.alive or target == pid:
            raise GameError("You can't vote for that player.")
        self.votes[pid] = target
        if self._all_voted(now):
            self._tally(now)

    def _all_voted(self, now: float) -> bool:
        """Everyone who is present has voted. Players offline for longer than a short grace period
        don't hold up the vote (a phone that just blinked off still counts as present)."""
        present = [p for p in self.alive if not self._away(p, now)]
        return bool(self.votes) and all(p in self.votes for p in present)

    def _away(self, pid: str, now: float) -> bool:
        p = self.players.get(pid)
        return not p or (not p.online and p.offline_since is not None and now - p.offline_since >= OFFLINE_TURN_GRACE)

    def end_vote(self, pid: str, now: float):
        self._require_host(pid)
        self._require_phase(VOTING)
        self._tally(now)

    def _tally(self, now: float):
        counts = Counter(self.votes.values())
        top = max(counts.values(), default=0)
        leaders = [p for p, c in counts.items() if c == top]
        self.tie = len(leaders) != 1

        if self.mode == CLASSIC:
            if not counts:
                return self._finish(SPIES, "Nobody voted, so the spy got away.")
            if self.tie:
                return self._finish(SPIES, "The vote was a tie. Nobody was caught!")
            self.accused = leaders[0]
            if self.accused in self.spies:
                return self._begin_guess(now, self.accused, self_reveal=False)
            return self._finish(SPIES, f"{self.name(self.accused)} was innocent! The spy got away.")

        # Undercover: eliminate the most-voted player, then check who's left.
        self.last_out = None
        if not self.tie:
            self.last_out = leaders[0]
            self.alive.remove(self.last_out)
            self.eliminated.append(self.last_out)
        left_spies = sum(1 for p in self.alive if p in self.spies)
        left_civs = len(self.alive) - left_spies
        if left_spies == 0:
            return self._finish(CIVILIANS, "Every undercover agent was found!")
        if left_spies >= left_civs:
            return self._finish(SPIES, "The undercover agents outlasted the civilians!")
        self.phase = REVEAL
        self._set_timer(now, REVEAL_SECONDS)

    def continue_round(self, pid: str, now: float):
        self._require_host(pid)
        self._require_phase(REVEAL)
        self._start_discussion(now)

    def spy_reveal(self, pid: str, now: float):
        """Classic: a spy who thinks they know the word can reveal themselves and guess."""
        self._require_player(pid)
        self._require_phase(PLAYING, VOTING)
        if self.mode != CLASSIC or pid not in self.spies:
            raise GameError("Only the spy can do that.")
        self._begin_guess(now, pid, self_reveal=True)

    def _begin_guess(self, now: float, pid: str, self_reveal: bool):
        self.phase = GUESS
        self.accused = pid
        self.self_reveal = self_reveal
        options = self.bank.words(self.category)
        self.rng.shuffle(options)
        self.guess_options = options
        self._set_timer(now, GUESS_SECONDS)

    def submit_guess(self, pid: str, word):
        self._require_player(pid)
        self._require_phase(GUESS)
        if pid != self.accused:
            raise GameError("Only the spy can guess.")
        if word not in self.guess_options:
            raise GameError("Pick one of the words.")
        self.guess = word
        name = self.name(pid)
        if word == self.word:
            how = "revealed themselves" if self.self_reveal else "was caught"
            self._finish(SPIES, f"{name} {how} and guessed the word correctly!")
        else:
            self._finish(CIVILIANS, f"{name} guessed “{word}”. Wrong!")

    def _finish(self, winner: str, reason: str):
        self.phase = RESULTS
        self.deadline = self.deadline_len = None
        self.winner, self.reason = winner, reason
        for pid in self.round_players:
            p = self.players.get(pid)
            if not p:
                continue
            spy_side = pid in self.spies
            if (winner == SPIES) == spy_side:
                p.wins += 1
                p.score += 2 if spy_side else 1
        log.info("Round %d over: %s win. %s", self.round, winner, reason)

    def back_to_lobby(self, pid: str, force: bool = False):
        if not force:
            self._require_host(pid)
            if self.phase == LOBBY:
                return
        self.phase = LOBBY
        self._reset_round()

    # ------------------------------------------------------------------ timers

    def tick(self, now: float) -> bool:
        """Advance anything whose time is up. Returns True if state changed."""
        changed = False

        host = self.players.get(self.host_id)
        if host and not host.online and host.offline_since and now - host.offline_since >= HOST_HANDOVER_SECONDS:
            nxt = next((p.id for p in self.players.values() if p.online), None)
            if nxt:
                log.info("Host %s is offline; %s is now host", host.name, self.name(nxt))
                self.host_id = nxt
                changed = True

        cur = self.current_turn
        if cur and self._away(cur, now):
            self._add_clue(cur, None)
            self._advance_turn(now)
            changed = True

        if self.phase == VOTING and self._all_voted(now):
            self._tally(now)
            return True

        if self.deadline and now >= self.deadline:
            changed = True
            if self.phase == PLAYING:
                if self.clue_mode == "typed":
                    self._add_clue(self.current_turn, None)
                    self._advance_turn(now)
                else:
                    self._begin_vote(now)
            elif self.phase == VOTING:
                self._tally(now)
            elif self.phase == REVEAL:
                self._start_discussion(now)
            elif self.phase == GUESS:
                self._finish(CIVILIANS, f"{self.name(self.accused)} ran out of time to guess.")
        return changed

    # ------------------------------------------------------------------ state for clients

    def state_for(self, pid: str | None, now: float) -> dict:
        """Personalised view of the game. A player only ever receives their own
        word; the shared screen receives no words until the results."""
        p = self.players.get(pid) if pid else None
        ph = self.phase
        st = {
            "type": "state",
            "phase": ph,
            "round": self.round,
            "codename": self.codename,
            "settings": self.settings,
            "categories": self.bank.categories(),
            "min_players": MIN_PLAYERS,
            "you": p and {
                "id": p.id, "name": p.name, "is_host": p.id == self.host_id,
                "in_round": p.id in self.round_players, "alive": p.id in self.alive,
            },
            "players": [
                {"id": x.id, "name": x.name, "score": x.score, "wins": x.wins, "games": x.games,
                 "online": x.online, "is_host": x.id == self.host_id,
                 "in_round": x.id in self.round_players, "alive": x.id in self.alive}
                for x in self.players.values()
            ],
            "seconds_left": max(0.0, self.deadline - now) if self.deadline else None,
            "seconds_total": self.deadline_len,
        }
        if ph == LOBBY:
            return st

        st.update(
            mode=self.mode, clue_mode=self.clue_mode, order=self.order, alive=self.alive,
            spy_count=len(self.spies), discussion=self.discussion, clues=self.clues,
            clue_round=self.clue_round, clue_rounds=self.settings["clue_rounds"],
            current_turn=self.current_turn,
            eliminated=[{"pid": x, "undercover": x in self.spies} for x in self.eliminated],
            last_out=self.last_out, tie=self.tie,
        )

        if p and p.id in self.round_players:
            if self.mode == UNDERCOVER:
                # Deliberately no "spy" flag: undercover agents must not know.
                st["role"] = {"word": self.word_for(p.id), "category": self.category}
            elif p.id in self.spies:
                show_cat = self.settings["spy_sees_category"] or ph == RESULTS
                st["role"] = {"spy": True, "category": self.category if show_cat else None,
                              "can_reveal": ph in (PLAYING, VOTING)}
            else:
                st["role"] = {"spy": False, "word": self.word, "category": self.category}

        if ph == VOTING:
            st["voted"] = list(self.votes)
            st["my_vote"] = self.votes.get(pid)
        if ph == REVEAL:
            st["votes"] = self.votes
        if ph == GUESS:
            st["accused"] = self.accused
            st["self_reveal"] = self.self_reveal
            if pid == self.accused:
                st["guess_options"] = self.guess_options
        if ph == RESULTS:
            st.update(word=self.word, spy_word=self.spy_word, category=self.category,
                      spies=sorted(self.spies), votes=self.votes, accused=self.accused,
                      guess=self.guess, winner=self.winner, reason=self.reason)
        return st
