"""Unit tests for the game engine. Run with:  python -m pytest -q"""

import json
import random
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import game as G  # noqa: E402
from game import Game, GameError  # noqa: E402
from wordbank import WordBank  # noqa: E402

T0 = 1_000_000.0


@pytest.fixture
def bank(tmp_path):
    for f in ("words.json", "pairs.json"):
        shutil.copy(ROOT / "data" / f, tmp_path / f)
    return WordBank(tmp_path)


def new_game(bank, n=4, seed=1, **settings):
    g = Game(bank, random.Random(seed))
    ids = [f"p{i}xxxxx" for i in range(n)]
    for i, pid in enumerate(ids):
        g.join(pid, f"Player{i}", T0)
    g.update_settings(ids[0], settings)
    return g, ids


def spy_and_civs(g):
    spy = next(iter(g.spies))
    return spy, [p for p in g.round_players if p not in g.spies]


def vote_all(g, target, now=T0):
    """Everyone alive votes for target (target votes for someone else)."""
    for p in list(g.alive):
        if g.phase != G.VOTING:
            break
        t = target if p != target else next(x for x in g.alive if x != p)
        g.vote(p, t, now)


# ---------------------------------------------------------------- players

def test_first_player_is_host_and_names_are_unique(bank):
    g, ids = new_game(bank, 3)
    assert g.host_id == ids[0]
    with pytest.raises(GameError, match="taken"):
        g.join("newperson1", "player1", T0)


def test_offline_player_can_reclaim_seat_by_name_in_lobby(bank):
    g, ids = new_game(bank)
    g.players[ids[2]].score = 5
    g.set_online(ids[2], False, T0)
    g.join("brandnewid", "Player2", T0 + 5)
    assert ids[2] not in g.players and g.players["brandnewid"].score == 5


def test_seat_in_a_live_round_cannot_be_taken_by_name(bank):
    """Otherwise a spy could open a private window, type an offline civilian's name, and read the word."""
    g, ids = new_game(bank)
    g.start(ids[0], T0)
    victim = next(p for p in g.round_players if p not in g.spies)
    g.set_online(victim, False, T0)
    with pytest.raises(GameError, match="in this round"):
        g.join("snoopertab", g.players[victim].name, T0 + 5)
    assert "snoopertab" not in g.players and victim in g.round_players
    # Once the round is over (words are public anyway), the seat can be reclaimed.
    g.back_to_lobby(ids[0] if ids[0] != victim else ids[1])
    g.join("snoopertab", g.players[victim].name, T0 + 10)
    assert "snoopertab" in g.players


def test_only_host_can_start_and_needs_three_players(bank):
    g, ids = new_game(bank, 2)
    with pytest.raises(GameError, match="at least 3"):
        g.start(ids[0], T0)
    g.join("p9xxxxx", "Late", T0)
    with pytest.raises(GameError, match="host"):
        g.start(ids[1], T0)
    g.start(ids[0], T0)
    assert g.phase == G.PLAYING


def test_host_hands_over_when_offline(bank):
    g, ids = new_game(bank)
    g.set_online(ids[0], False, T0)
    assert not g.tick(T0 + 5)
    assert g.tick(T0 + G.HOST_HANDOVER_SECONDS + 1)
    assert g.host_id == ids[1]


def test_settings_are_validated(bank):
    g, ids = new_game(bank)
    g.update_settings(ids[0], {"mode": "hacker", "spies": 99, "minutes": -1, "category": "Nope"})
    assert g.settings == G.DEFAULT_SETTINGS


# ---------------------------------------------------------------- classic

def test_classic_spy_never_receives_the_word(bank):
    g, ids = new_game(bank, 5)
    g.start(ids[0], T0)
    spy, civs = spy_and_civs(g)
    word = g.word
    assert word not in json.dumps(g.state_for(spy, T0))
    assert word not in json.dumps(g.state_for(None, T0))  # shared screen
    assert g.state_for(civs[0], T0)["role"]["word"] == word


def test_caught_spy_who_guesses_right_wins(bank):
    g, ids = new_game(bank)
    g.start(ids[0], T0)
    spy, _ = spy_and_civs(g)
    g.start_vote(ids[0], T0)
    vote_all(g, spy)
    assert g.phase == G.GUESS and g.accused == spy
    assert "guess_options" in g.state_for(spy, T0)
    assert "guess_options" not in g.state_for(ids[0] if ids[0] != spy else ids[1], T0)
    g.submit_guess(spy, g.word)
    assert g.winner == G.SPIES and g.players[spy].score == 2


def test_caught_spy_who_guesses_wrong_loses(bank):
    g, ids = new_game(bank)
    g.start(ids[0], T0)
    spy, civs = spy_and_civs(g)
    g.start_vote(ids[0], T0)
    vote_all(g, spy)
    wrong = next(w for w in g.guess_options if w != g.word)
    g.submit_guess(spy, wrong)
    assert g.winner == G.CIVILIANS
    assert all(g.players[c].score == 1 for c in civs)


def test_voting_out_an_innocent_lets_spy_win(bank):
    g, ids = new_game(bank)
    g.start(ids[0], T0)
    spy, civs = spy_and_civs(g)
    g.start_vote(ids[0], T0)
    vote_all(g, civs[0])
    assert g.phase == G.RESULTS and g.winner == G.SPIES


def test_tie_vote_lets_spy_win(bank):
    g, ids = new_game(bank, 4)
    g.start(ids[0], T0)
    g.start_vote(ids[0], T0)
    a, b, c, d = g.alive
    g.vote(a, b, T0); g.vote(b, a, T0); g.vote(c, a, T0); g.vote(d, b, T0)
    assert g.winner == G.SPIES and "tie" in g.reason


def test_spy_can_reveal_early(bank):
    g, ids = new_game(bank)
    g.start(ids[0], T0)
    spy, civs = spy_and_civs(g)
    with pytest.raises(GameError):
        g.spy_reveal(civs[0], T0)
    g.spy_reveal(spy, T0)
    assert g.phase == G.GUESS and g.self_reveal


def test_talk_timer_moves_to_voting_and_guess_times_out(bank):
    g, ids = new_game(bank, minutes=2)
    g.start(ids[0], T0)
    assert not g.tick(T0 + 60)
    assert g.tick(T0 + 121) and g.phase == G.VOTING
    spy, _ = spy_and_civs(g)
    vote_all(g, spy, T0 + 130)
    assert g.tick(T0 + 130 + G.GUESS_SECONDS + 1)
    assert g.winner == G.CIVILIANS and "time" in g.reason


# ---------------------------------------------------------------- typed clues

def test_typed_clues_take_turns_and_reject_the_word(bank):
    g, ids = new_game(bank, 3, clue_mode="typed", clue_rounds=1)
    g.start(ids[0], T0)
    first = g.current_turn
    other = next(p for p in g.order if p != first)
    with pytest.raises(GameError, match="turn"):
        g.submit_clue(other, "hello", T0)

    civ = next(p for p in g.order if p not in g.spies)
    while g.current_turn != civ:
        g.submit_clue(g.current_turn, "something", T0)
    with pytest.raises(GameError, match="secret word"):
        g.submit_clue(civ, f"my {g.word.upper()}!", T0)
    while g.phase == G.PLAYING:
        g.submit_clue(g.current_turn, "clue", T0)
    assert g.phase == G.VOTING and len(g.clues) == 3


def test_typed_turn_timeout_and_offline_player_are_skipped(bank):
    g, ids = new_game(bank, 3, clue_mode="typed", clue_rounds=1)
    g.start(ids[0], T0)
    first = g.current_turn
    assert g.tick(T0 + G.TURN_SECONDS + 1)
    assert g.clues[-1] == {"pid": first, "text": None, "d": 1, "r": 1}
    second = g.current_turn
    g.set_online(second, False, T0 + 50)
    assert g.tick(T0 + 50 + G.OFFLINE_TURN_GRACE + 1)
    assert g.clues[-1]["pid"] == second


# ---------------------------------------------------------------- undercover

def test_undercover_players_do_not_know_their_role(bank):
    g, ids = new_game(bank, 5, mode="undercover")
    g.start(ids[0], T0)
    spy, civs = spy_and_civs(g)
    s_spy, s_civ = g.state_for(spy, T0), g.state_for(civs[0], T0)
    assert "spy" not in s_spy["role"] and "spy" not in s_civ["role"]
    assert s_spy["role"]["word"] == g.spy_word != g.word == s_civ["role"]["word"]
    assert g.word not in json.dumps(s_spy) and g.spy_word not in json.dumps(s_civ)


def test_undercover_elimination_until_civilians_win(bank):
    g, ids = new_game(bank, 5, mode="undercover")
    g.start(ids[0], T0)
    spy, civs = spy_and_civs(g)
    g.start_vote(ids[0], T0)
    vote_all(g, civs[0])  # wrong person out: 1 undercover vs 3 civilians, continue
    assert g.phase == G.REVEAL and g.last_out == civs[0] and civs[0] not in g.alive
    assert g.tick(T0 + G.REVEAL_SECONDS + 1) and g.phase == G.PLAYING and g.discussion == 2
    g.start_vote(ids[0], T0)
    vote_all(g, spy)
    assert g.winner == G.CIVILIANS


def test_undercover_wins_when_equal_to_civilians(bank):
    g, ids = new_game(bank, 4, mode="undercover")
    g.start(ids[0], T0)
    spy, civs = spy_and_civs(g)
    g.start_vote(ids[0], T0)
    vote_all(g, civs[0])  # 1 undercover vs 2 civilians
    g.continue_round(ids[0], T0)  # host can continue even if eliminated
    g.start_vote(ids[0], T0)
    vote_all(g, civs[1])  # 1 vs 1
    assert g.winner == G.SPIES


# ---------------------------------------------------------------- word packs

def test_custom_pack_add_use_delete(bank):
    g, ids = new_game(bank)
    with pytest.raises(GameError, match="at least"):
        g.add_pack(ids[0], "Tiny", "a, b")
    g.add_pack(ids[0], "Our Team", "Ram, Shyam, Hari, Gita, Sita, Rita, Ram")
    assert bank.custom["Our Team"] == ["Ram", "Shyam", "Hari", "Gita", "Sita", "Rita"]
    assert g.settings["category"] == "Our Team"
    assert (bank.data_dir / "custom_words.json").exists()
    g.start(ids[0], T0)
    assert g.word in bank.custom["Our Team"]
    g.back_to_lobby(ids[0])
    g.delete_pack(ids[0], "Our Team")
    assert g.settings["category"] == "Random"
    with pytest.raises(GameError):
        g.delete_pack(ids[0], "Office")


def test_words_do_not_repeat_until_category_exhausted(bank):
    g, ids = new_game(bank, category="Animals")
    seen = []
    for _ in range(len(bank.words("Animals"))):
        g.start(ids[0], T0)
        seen.append(g.word)
        g.back_to_lobby(ids[0])
    assert len(set(seen)) == len(seen)


def test_custom_pack_cannot_be_changed_mid_round(bank):
    """Replacing the current pack mid-round would drop the word from the spy's guess list."""
    g, ids = new_game(bank)
    g.add_pack(ids[0], "Team", ["a", "b", "c", "d", "e", "f"])
    g.start(ids[0], T0)
    with pytest.raises(GameError):
        g.add_pack(ids[0], "Team", ["q", "r", "s", "t", "u", "v"])
    assert g.word in g.bank.words("Team")


def test_offline_players_do_not_hold_up_the_vote(bank):
    g, ids = new_game(bank, 4)
    g.start(ids[0], T0)
    g.start_vote(ids[0], T0)
    gone = g.alive[3]
    g.set_online(gone, False, T0)
    a, b, c = g.alive[:3]
    g.vote(a, b, T0 + 1); g.vote(b, c, T0 + 1); g.vote(c, b, T0 + 1)
    assert g.phase == G.VOTING  # a phone that only just went offline still gets a chance
    assert g.tick(T0 + G.OFFLINE_TURN_GRACE + 1)
    assert g.phase in (G.GUESS, G.RESULTS)


def test_vote_completes_immediately_when_absent_player_already_long_gone(bank):
    g, ids = new_game(bank, 4)
    g.start(ids[0], T0)
    gone = g.alive[3]
    g.set_online(gone, False, T0)
    g.start_vote(ids[0], T0 + 30)
    a, b, c = g.alive[:3]
    g.vote(a, b, T0 + 31); g.vote(b, c, T0 + 31); g.vote(c, b, T0 + 31)
    assert g.phase != G.VOTING


def test_nobody_present_means_no_early_tally(bank):
    g, ids = new_game(bank, 3)
    g.start(ids[0], T0)
    g.start_vote(ids[0], T0)
    for p in ids:
        g.set_online(p, False, T0)
    g.tick(T0 + G.OFFLINE_TURN_GRACE + 1)
    assert g.phase == G.VOTING
