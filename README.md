# 🕵️ Office Spy

A social deduction party game for the office. Everyone gets a secret word, except someone doesn't.
Play over office Wi-Fi: one laptop runs the server and everyone joins from their phone's browser. Nobody installs an app.

## Quick start

```bash
cd office-spy
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

The terminal prints a join link (e.g. `http://192.168.1.25:8000`) and a QR code. Everyone on the same Wi-Fi scans it.
The first person to join is the host 👑.

Options: `python server.py --port 9000` and `--ip 192.168.1.25` (if the auto-detected IP is wrong).

**Shared screen (optional):** open `http://<ip>:8000/screen` on a TV or projector. It shows the join QR, the timer, the clues and
the results, but never anyone's word.

## Game modes

| | 🕵️ Classic | 🎭 Undercover |
|---|---|---|
| Civilians get | the secret word | the secret word |
| The odd one out gets | "You are the SPY" (no word) | a *similar* word, e.g. Momo vs Samosa |
| Knows they're the odd one? | Yes | **No** |
| Voting | One vote. A caught spy gets one guess at the word | Vote out one player per round, and their role is revealed |
| Civilians win when | the spy is caught and guesses wrong | every undercover agent is out |
| Spies win when | an innocent is voted out, it's a tie, or the spy guesses the word | they survive until it's 1-on-1 |

In Classic, the spy can tap **"🎯 I know the word!"** at any time to reveal themselves and guess early.

### Clue styles
- **🗣️ Talk out loud:** discuss against a timer (2–10 min), then vote.
- **⌨️ Type on phones:** players take turns typing a short clue (45 s each, 1–3 laps). Everyone sees the clue board.
  Clues containing your own word are rejected. Offline or slow players are skipped automatically.

**Points:** winning civilians get +1 each. Winning spies or undercover agents get +2 each. The scoreboard also tracks wins/games.

## Host tools
- Game setup: mode, clue style, category, number of spies (1–3), timer or clue laps, whether the spy sees the category
- **Custom word packs:** add your own (office in-jokes, team names, local places). They're saved to `data/custom_words.json`
- Skip a player's turn, start voting early, end voting, end the round
- Remove players, hand over host, reset the scoreboard
- If the host's phone is offline for 15 seconds, hosting passes to the next player automatically

## Look and feel
The UI is designed as a card table: green felt, manila case files, rubber stamps and poker-chip players.
- Your role arrives as a sealed case file. Tap it and the cover flips open. Spies get a red **SPY** stamp that slams onto the page.
- Clues appear in a paper "transmission log" and type themselves out letter by letter.
- Verdicts are stamped (*Spy caught*, *Case closed*…) and winners get a shower of paper shreds.
- Phase changes use smooth view transitions. The page is patched in place (a small keyed DOM morph in `app.js`),
  so only new elements animate, and typing is never interrupted by updates from other players.
- Respects *Reduce motion*, has visible keyboard focus, and the fonts (Big Shoulders, Courier Prime, SIL OFL) are bundled
  in `static/fonts/`, so it works on a Wi-Fi network with no internet.

## Built to survive real phones
- A phone that sleeps or refreshes rejoins with the same role. If someone switches tabs or browsers, they can
  rejoin with **the same name** and get their seat back.
- The server only ever sends each player **their own** word. Opening dev tools won't reveal anything,
  and the secret word is never printed in the host's terminal either.
- Sounds and vibration are generated in the browser (no audio files). There's a mute button in the top bar.

## Troubleshooting
- **Phones can't open the link:** the office Wi-Fi may block devices from talking to each other ("client isolation").
  Turn on the laptop's hotspot and have everyone join that instead.
- **macOS asks to accept incoming connections:** click Allow. **Windows firewall prompt:** allow on *Private* networks.
- **Wrong IP in the link:** run `python server.py --ip <your-ip>`. Find your IP with `ipconfig` (Windows) or `ipconfig getifaddr en0` (Mac).

## Project structure

```
office-spy/
├── server.py          # FastAPI app: serves the web UI, WebSocket handling, timer loop
├── game.py            # Game engine: rules, phases, scoring (no networking, fully unit-tested)
├── wordbank.py        # Word packs, Undercover word pairs, custom packs
├── data/
│   ├── words.json     # Classic word packs (6 categories)
│   ├── pairs.json     # Undercover word pairs (6 categories)
│   └── custom_words.json   # created when the host adds a pack
├── static/
│   ├── index.html
│   ├── style.css      # design system: felt, paper, stamps, chips, motion
│   ├── app.js         # phone + shared-screen UI (vanilla JS, no build step)
│   └── fonts/         # bundled web fonts
└── tests/test_game.py # pytest suite for the engine
```

**How it works:** clients send small JSON actions over a WebSocket (`{"type": "vote", "target": "…"}`).
The server applies them to the `Game` engine, then sends every connection its own personalised state.
A background loop calls `game.tick()` twice a second to move phases along when timers run out.
The server is the single source of truth, and phones just display what they're told.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

To add built-in words, edit `data/words.json` (Classic) or `data/pairs.json` (Undercover pairs of similar words).
