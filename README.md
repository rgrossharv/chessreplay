# Replay

Replay is a local web app that imports public Chess.com and Lichess games and
turns important mistakes into a spaced-repetition training deck. It also
includes one-click study decks for Magnus Carlsen,
Hikaru Nakamura, Levy Rozman, and Fabiano Caruana.

## Run it

```bash
git clone https://github.com/rgrossharv/chessreplay.git
cd chessreplay
chmod +x run.sh
./run.sh
```

Open <http://localhost:47831>, create a local account or continue as a guest,
then enter a Chess.com or Lichess username.

## Network access

Replay listens only on the computer running it by default. To allow access from
other devices on the same trusted network, start it with:

```bash
REPLAY_HOST=all ./run.sh
```

Then open `http://your-hostname:47831` on the other device, replacing
`your-hostname` with the computer's local hostname. This does not create a
public internet URL.

To select a different port or hostname:

```bash
REPLAY_PORT=47832 ./run.sh
REPLAY_HOST=your-hostname.local ./run.sh
```

Do not expose the development server directly to the public internet.

The first launch installs the small `python-chess` dependency into a local
virtual environment. No Chess.com password or API key is needed.

## How training works

- Replay uses games played in the last seven days. If there are none, it falls
  back to the account's latest twenty games. Featured master decks use their
  latest twenty games so the preloaded buttons remain useful.
- Stockfish 18 WebAssembly runs in the visitor's browser. The server only
  downloads and parses public game records.
- A position becomes a puzzle only when the played move loses at least three
  pawns of evaluation, misses a forced mate, or misses a clearly winning
  position. Ordinary inaccuracies are excluded.
- Review scheduling and board preferences are stored locally per Replay account
  (or in the browser's guest profile). Again, Hard, Good, and Easy grades
  control when a puzzle returns.
- Puzzle classifications stay hidden until the position is solved or the user
  asks for the solution. Pieces support both click-to-move and pointer-based
  drag-and-drop on mouse, pen, and touch devices.
- Website themes, board colors, Lichess piece sets, lightweight motion, and
  master volume are remembered separately for each Replay account.
- Incorrect puzzle attempts are evaluated on demand by the in-browser engine.
- Analysis results are cached in the browser so the same game does not need to
  be analyzed again on every visit.

## Open-source components

- Chess rules: `chess.js` 1.4.0, BSD-2-Clause.
- Browser engine: Stockfish.js 18, GPLv3. Its license is included at
  `static/vendor/stockfish/COPYING.txt`.
- Piece artwork: the Cburnett, Alpha, and Merida sets from Lichess. The Lichess
  license is included at `static/pieces/LICHESS-LICENSE.txt`.

The unlicensed Chess.com asset archive referenced during development is not
bundled. The referenced third-party sound repository also lacks a published
license, so Replay uses tiny synthesized Web Audio cues rather than copying its
files. Board colors, licensed Lichess sets, motion, and volume can be changed
from the settings dialog.
