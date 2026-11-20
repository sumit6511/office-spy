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
    victim = next(p for p in g.round_players if p not in g.spies and p != g.host_id)
    word = g.word
    g.set_online(victim, False, T0)
    g.join("snoopertab", g.players[victim].name, T0 + 5)
    assert "snoopertab" not in g.players and victim in g.round_players
    st = g.state_for("snoopertab", T0 + 5)
    assert st["you"] is None and st["rejoin_pending"] == g.players[victim].name
    assert word not in json.dumps(st)
    # Only the host is told about the request.
    assert [r["seat"] for r in g.state_for(g.host_id, T0)["rejoin_requests"]] == [victim]
    assert "rejoin_requests" not in g.state_for(victim, T0)


def test_host_can_let_a_player_back_in_mid_round(bank):
    g, ids = new_game(bank)
    g.start(ids[0], T0)
    victim = next(p for p in g.round_players if p != g.host_id)
    role_word, was_spy = g.word_for(victim), victim in g.spies
    g.set_online(victim, False, T0)
    g.join("newphone01", g.players[victim].name, T0 + 5)
    with pytest.raises(GameError, match="host"):
        g.approve_rejoin(next(p for p in ids if p not in (g.host_id, victim)), "newphone01")
    g.approve_rejoin(g.host_id, "newphone01")
    assert victim not in g.players and "newphone01" in g.round_players
    assert g.word_for("newphone01") == role_word and ("newphone01" in g.spies) == was_spy
    assert g.rejoin_requests == {}


def test_rejoin_request_can_be_denied_or_dropped(bank):
    g, ids = new_game(bank)
    g.start(ids[0], T0)
    victim = next(p for p in g.round_players if p != g.host_id)
    g.set_online(victim, False, T0)
    g.join("newphone01", g.players[victim].name, T0 + 5)
    g.deny_rejoin(g.host_id, "newphone01")
    assert g.state_for("newphone01", T0)["rejoin_pending"] is None
    # A request is dropped if the player comes back on their original phone.
    g.join("newphone02", g.players[victim].name, T0 + 6)
    g.set_online(victim, True, T0 + 7)
    assert g.tick(T0 + 7) and g.rejoin_requests == {}
    with pytest.raises(GameError):
        g.approve_rejoin(g.host_id, "newphone02")


def test_waiting_rejoin_goes_through_when_the_round_ends(bank):
    g, ids = new_game(bank)
    g.start(ids[0], T0)
    victim = next(p for p in g.round_players if p != g.host_id)
    name = g.players[victim].name
    g.set_online(victim, False, T0)
    g.join("newphone01", name, T0 + 5)
    g.back_to_lobby(g.host_id)
    assert g.tick(T0 + 6)
    assert g.players["newphone01"].name == name and victim not in g.players


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


# ---------------------------------------------------------------- files & persistence

def test_corrupt_custom_pack_file_is_ignored(bank, tmp_path):
    (tmp_path / "custom_words.json").write_text("{not json", encoding="utf-8")
    b = WordBank(tmp_path)
    assert b.custom == {} and b.has_category("Office")
    b.add_pack("Team", ["a", "b", "c", "d", "e", "f"])
    assert json.loads((tmp_path / "custom_words.json").read_text())["Team"][0] == "a"
    assert not list(tmp_path.glob(".*.tmp"))  # no leftover temp files


def test_missing_word_files_give_a_friendly_error(tmp_path):
    g = Game(WordBank(tmp_path), random.Random(1))
    for i in range(3):
        g.join(f"z{i}xxxxx", f"P{i}", T0)
    with pytest.raises(GameError, match="no word packs"):
        g.start("z0xxxxx", T0)
    g.update_settings("z0xxxxx", {"mode": G.UNDERCOVER})
    with pytest.raises(GameError, match="no Undercover word pairs"):
        g.start("z0xxxxx", T0)
    assert g.phase == G.LOBBY and g.round == 0


def test_scoreboard_survives_a_restart(bank, tmp_path):
    path = tmp_path / "scores.json"
    g = Game(bank, random.Random(1), scores_path=path)
    ids = [f"s{i}xxxxx" for i in range(3)]
    for i, pid in enumerate(ids):
        g.join(pid, f"Player{i}", T0)
    g.start(ids[0], T0)
    g.start_vote(ids[0], T0)
    vote_all(g, next(p for p in g.round_players if p not in g.spies))  # an innocent: spy wins
    before = {p.name: (p.score, p.wins, p.games) for p in g.players.values()}

    g2 = Game(bank, random.Random(2), scores_path=path)  # "restart"
    g2.join("newtokenid", "player1", T0)  # names match case-insensitively
    p = g2.players["newtokenid"]
    assert (p.score, p.wins, p.games) == before["Player1"] and p.games == 1

    g2.reset_scores("newtokenid")
    saved = Game(bank, scores_path=path).saved_scores
    assert set(saved) == {"player1"} and saved["player1"]["score"] == saved["player1"]["games"] == 0


# ---------------------------------------------------------------- custom undercover pairs

def test_custom_pairs_are_parsed_and_used_in_undercover(bank):
    g, ids = new_game(bank, mode=G.UNDERCOVER)
    g.add_pairs(ids[0], "Our Office", "Printer / Scanner\nStandup | Retro\nKettle vs Coffee machine\nLunch, Brunch\n\n")
    assert bank.custom_pairs["Our Office"][2] == ["Kettle", "Coffee machine"]
    assert g.settings["category"] == "Our Office"
    cat = next(c for c in bank.categories() if c["name"] == "Our Office")
    assert cat == {"name": "Our Office", "words": 0, "pairs": 4, "custom": True}
    g.start(ids[0], T0)
    assert g.category == "Our Office"
    assert sorted([g.word, g.spy_word]) in [sorted(p) for p in bank.custom_pairs["Our Office"]]
    # Saved to disk, and reloaded.
    assert WordBank(bank.data_dir).custom_pairs == bank.custom_pairs


def test_bad_custom_pairs_are_rejected_with_a_helpful_message(bank):
    g, ids = new_game(bank)
    with pytest.raises(GameError, match="Line 2 needs exactly two"):
        g.add_pairs(ids[0], "P", "A / B\nJustOne\nC / D")
    with pytest.raises(GameError, match="must be different"):
        g.add_pairs(ids[0], "P", "A / B\nTea / tea\nC / D")
    with pytest.raises(GameError, match="at least 3"):
        g.add_pairs(ids[0], "P", "A / B\nB / A\nC / D")  # duplicates count once
    with pytest.raises(GameError, match="built-in"):
        g.add_pairs(ids[0], "Office", "A / B\nC / D\nE / F")


def test_one_custom_pack_can_have_words_and_pairs(bank):
    g, ids = new_game(bank)
    g.add_pack(ids[0], "Team", ["a", "b", "c", "d", "e", "f"])
    g.add_pairs(ids[0], "Team", "A / B\nC / D\nE / F")
    assert [c for c in bank.categories() if c["name"] == "Team"][0]["words"] == 6
    g.delete_pack(ids[0], "Team")
    assert not bank.has_category("Team") and "Team" not in WordBank(bank.data_dir).custom_pairs


# ---------------------------------------------------------------- mr. white

def white_game(bank, n=5, seed=3):
    g, ids = new_game(bank, n, seed, mode=G.UNDERCOVER, mr_white=True)
    g.start(ids[0], T0)
    return g, ids


def vote_out(g, target, now=T0):
    g.start_vote(g.host_id, now)
    vote_all(g, target, now)


def test_mr_white_knows_and_gets_no_word(bank):
    g, ids = white_game(bank)
    assert g.white and g.white not in g.spies
    st = g.state_for(g.white, T0)
    assert st["role"]["mr_white"] is True and st["role"]["word"] is None
    assert g.word not in json.dumps(st) and g.spy_word not in json.dumps(st)
    civ = next(p for p in g.round_players if not g._impostor(p))
    assert g.state_for(civ, T0)["role"]["mr_white"] is False
    assert g.state_for(civ, T0)["white_in_play"] is True
    # Nobody learns who Mr. White is before they're out.
    assert g.white not in json.dumps(g.state_for(civ, T0)["eliminated"])


def test_mr_white_guessing_right_wins_alone(bank):
    g, ids = white_game(bank)
    white = g.white
    vote_out(g, white)
    assert g.phase == G.GUESS and g.accused == white
    assert g.state_for(white, T0)["white_guessing"] and "guess_options" not in g.state_for(white, T0)
    other = next(p for p in g.round_players if p != white)
    with pytest.raises(GameError, match="Mr. White"):
        g.submit_guess(other, g.word, T0)
    g.submit_guess(white, "  " + g.word.upper() + "!", T0)  # case and punctuation don't matter
    assert g.winner == G.WHITE
    assert g.players[white].score == 3
    assert all(g.players[p].score == 0 for p in g.round_players if p != white)


def test_mr_white_guessing_wrong_is_just_out(bank):
    g, ids = white_game(bank)
    white = g.white
    vote_out(g, white)
    g.submit_guess(white, "definitely not it", T0)
    assert g.phase == G.REVEAL and white not in g.alive
    assert g.state_for(ids[0], T0)["white_guess"] == "definitely not it"
    assert g.state_for(ids[0], T0)["eliminated"][-1]["role"] == "white"
    # Then catching the undercover agent finishes it for the civilians.
    g.continue_round(g.host_id, T0)
    vote_out(g, next(iter(g.spies)))
    assert g.winner == G.CIVILIANS and "Mr. White" in g.reason


def test_mr_white_timeout_and_team_win(bank):
    g, ids = white_game(bank, n=4, seed=5)
    white = g.white
    vote_out(g, white)
    g.tick(T0 + G.GUESS_SECONDS + 1)
    assert white not in g.alive and g.phase == G.REVEAL
    # 1 undercover vs 2 civilians: voting out a civilian ties it, and the undercover side wins.
    g.continue_round(g.host_id, T0 + 60)
    vote_out(g, next(p for p in g.alive if not g._impostor(p)), T0 + 60)
    assert g.winner == G.SPIES
    assert g.players[white].score == 2  # Mr. White is on the undercover side


def test_mr_white_needs_four_players_and_is_undercover_only(bank):
    g, ids = new_game(bank, 3, mode=G.UNDERCOVER, mr_white=True)
    with pytest.raises(GameError, match="at least 4"):
        g.start(ids[0], T0)
    g, ids = new_game(bank, 4, mode=G.CLASSIC, mr_white=True)
    g.start(ids[0], T0)
    assert g.white is None


# ---------------------------------------------------------------- history

def test_round_history_is_recorded_and_never_leaks_a_live_word(bank):
    g, ids = new_game(bank)
    g.start(ids[0], T0)
    spy, civs = spy_and_civs(g)
    g.start_vote(ids[0], T0)
    vote_all(g, civs[0])
    first_word = g.word
    [h] = g.state_for(None, T0)["history"]
    assert h["round"] == 1 and h["word"] == first_word and h["winner"] == G.SPIES
    assert h["spies"] == [g.players[spy].name] and h["white"] is None and h["spy_word"] is None
    # During the next round, history only holds finished rounds.
    g.start(ids[0], T0 + 100)
    st = g.state_for(None, T0 + 100)
    assert len(st["history"]) == 1 and g.word not in json.dumps(st)


def test_history_is_capped(bank):
    g, ids = new_game(bank)
    for i in range(G.MAX_HISTORY + 5):
        g.start(ids[0], T0 + i)
        g.start_vote(ids[0], T0 + i)
        g.end_vote(ids[0], T0 + i)
    assert len(g.history) == G.MAX_HISTORY and g.history[-1]["round"] == G.MAX_HISTORY + 5
