#!/usr/bin/env python3
"""Small, dependency-light Chess.com review server."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import chess
    import chess.engine
    import chess.pgn
except ImportError:
    print("Missing python-chess. Run: ./run.sh", file=sys.stderr)
    raise


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
DATABASE = DATA / "replay.db"
USER_AGENT = "Replay-Chess-Coach/1.0 (local personal game reviewer)"
GAMES: dict[str, dict[str, Any]] = {}

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError("That Chess.com username was not found.") from exc
        if exc.code == 429:
            raise ValueError("Chess.com is rate-limiting requests. Wait a moment and try again.") from exc
        raise ValueError(f"Chess.com returned an error ({exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError("Could not reach Chess.com. Check your internet connection.") from exc


def fetch_text(url: str, accept: str = "text/plain") -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError("That Lichess username was not found.") from exc
        if exc.code == 429:
            raise ValueError("Lichess is rate-limiting requests. Wait a moment and try again.") from exc
        raise ValueError(f"Lichess returned an error ({exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError("Could not reach Lichess. Check your internet connection.") from exc


def clean_username(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,30}", value):
        raise ValueError("Enter a valid Chess.com or Lichess username.")
    return value


def import_chesscom_games(username: str, latest: bool = False) -> dict[str, Any]:
    username = clean_username(username)
    base = f"https://api.chess.com/pub/player/{urllib.parse.quote(username.lower())}"
    archive_data = fetch_json(f"{base}/games/archives")
    archives = archive_data.get("archives", [])
    if not archives:
        return {"games": [], "window": "Last 7 days"}

    cutoff = int(time.time()) - (7 * 24 * 60 * 60)
    raw_games: list[dict[str, Any]] = []
    # Archives are monthly. Work backwards until we have crossed the seven-day
    # boundary and also have enough games for the twenty-game fallback.
    for archive_url in reversed(archives):
        month_games = fetch_json(archive_url).get("games", [])
        raw_games.extend(reversed(month_games))
        oldest = min((game.get("end_time", 0) for game in month_games), default=0)
        if len(raw_games) >= 20 and oldest and oldest < cutoff:
            break

    recent_games = [game for game in raw_games if game.get("end_time", 0) >= cutoff]
    using_recent = bool(recent_games) and not latest
    selected_games = raw_games[:20] if latest else (recent_games[:100] if using_recent else raw_games[:20])

    summaries = []
    for raw in selected_games:
        pgn_text = raw.get("pgn", "")
        if not pgn_text:
            continue
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if not game:
            continue
        headers = game.headers
        white = raw.get("white", {})
        black = raw.get("black", {})
        game_id = hashlib.sha1((raw.get("url", "") + pgn_text).encode()).hexdigest()[:14]
        user_is_white = white.get("username", "").lower() == username.lower()
        player = white if user_is_white else black
        opponent = black if user_is_white else white
        result = result_for_user(headers.get("Result", "*"), user_is_white)
        ended = raw.get("end_time")
        date = (
            datetime.fromtimestamp(ended, tz=timezone.utc).strftime("%b %-d, %Y")
            if ended
            else headers.get("Date", "").replace(".", "-")
        )
        record = {
            "id": game_id,
            "username": username,
            "pgn": pgn_text,
            "url": raw.get("url", ""),
            "summary": {
                "id": game_id,
                "opponent": opponent.get("username", "Unknown"),
                "opponentRating": opponent.get("rating"),
                "playerRating": player.get("rating"),
                "playerColor": "white" if user_is_white else "black",
                "result": result,
                "date": date,
                "timeClass": raw.get("time_class", "game").title(),
                "timeControl": raw.get("time_control", ""),
                "opening": opening_name(headers),
                "endTime": ended,
                "source": "chesscom",
            },
        }
        GAMES[game_id] = record
        summaries.append(record["summary"])
    return {
        "games": summaries,
        "window": "Last 7 days" if using_recent else "Latest 20 games",
    }


def header_rating(value: str | None) -> int | None:
    try:
        return int(value or "")
    except ValueError:
        return None


def lichess_time_class(headers: chess.pgn.Headers) -> str:
    event = headers.get("Event", "")
    for name in ("ultrabullet", "bullet", "blitz", "rapid", "classical", "correspondence"):
        if name in event.lower():
            return name.title()
    return "Game"


def import_lichess_games(username: str, latest: bool = False) -> dict[str, Any]:
    username = clean_username(username)
    cutoff_ms = (int(time.time()) - 7 * 24 * 60 * 60) * 1000
    params = {"max": "20" if latest else "100", "opening": "true", "moves": "true"}
    if not latest:
        params["since"] = str(cutoff_ms)
    url = f"https://lichess.org/api/games/user/{urllib.parse.quote(username)}?{urllib.parse.urlencode(params)}"
    pgn_blob = fetch_text(url, "application/x-chess-pgn")
    games: list[chess.pgn.Game] = []
    stream = io.StringIO(pgn_blob)
    while game := chess.pgn.read_game(stream):
        if game.headers.get("Variant", "Standard") in {"Standard", "From Position"}:
            games.append(game)

    using_recent = bool(games) and not latest
    if not games and not latest:
        fallback = f"https://lichess.org/api/games/user/{urllib.parse.quote(username)}?max=20&opening=true&moves=true"
        stream = io.StringIO(fetch_text(fallback, "application/x-chess-pgn"))
        while game := chess.pgn.read_game(stream):
            if game.headers.get("Variant", "Standard") in {"Standard", "From Position"}:
                games.append(game)

    summaries = []
    for game in games[:100 if using_recent else 20]:
        headers = game.headers
        white_name, black_name = headers.get("White", "Unknown"), headers.get("Black", "Unknown")
        user_is_white = white_name.lower() == username.lower()
        opponent = black_name if user_is_white else white_name
        player_rating = headers.get("WhiteElo" if user_is_white else "BlackElo")
        opponent_rating = headers.get("BlackElo" if user_is_white else "WhiteElo")
        exporter = io.StringIO()
        print(game, file=exporter, end="\n\n")
        pgn_text = exporter.getvalue()
        site = headers.get("Site", "")
        game_id = hashlib.sha1(("lichess:" + site + pgn_text).encode()).hexdigest()[:14]
        date_value = headers.get("UTCDate", headers.get("Date", "")).replace(".", "-")
        try:
            parsed_date = datetime.strptime(date_value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            ended = int(parsed_date.timestamp())
            display_date = parsed_date.strftime("%b %-d, %Y")
        except ValueError:
            ended, display_date = None, date_value
        summary = {
            "id": game_id,
            "opponent": opponent,
            "opponentRating": header_rating(opponent_rating),
            "playerRating": header_rating(player_rating),
            "playerColor": "white" if user_is_white else "black",
            "result": result_for_user(headers.get("Result", "*"), user_is_white),
            "date": display_date,
            "timeClass": lichess_time_class(headers),
            "timeControl": headers.get("TimeControl", ""),
            "opening": headers.get("Opening", headers.get("ECO", "Unknown opening")),
            "endTime": ended,
            "source": "lichess",
        }
        GAMES[game_id] = {"id": game_id, "username": username, "pgn": pgn_text, "url": site, "summary": summary}
        summaries.append(summary)
    return {"games": summaries, "window": "Last 7 days" if using_recent else "Latest 20 games"}


def import_games(username: str, source: str = "chesscom", latest: bool = False) -> dict[str, Any]:
    if source == "lichess":
        return import_lichess_games(username, latest)
    if source != "chesscom":
        raise ValueError("Choose Chess.com or Lichess as the game source.")
    return import_chesscom_games(username, latest)


def result_for_user(result: str, is_white: bool) -> str:
    if result == "1/2-1/2":
        return "Draw"
    if (result == "1-0" and is_white) or (result == "0-1" and not is_white):
        return "Win"
    if result in {"1-0", "0-1"}:
        return "Loss"
    return "—"


def opening_name(headers: chess.pgn.Headers) -> str:
    eco_url = headers.get("ECOUrl", "")
    if eco_url:
        return urllib.parse.unquote(eco_url.rstrip("/").split("/")[-1]).replace("-", " ")
    return headers.get("Opening", headers.get("ECO", "Unknown opening"))


def game_detail(game_id: str) -> dict[str, Any]:
    record = GAMES.get(game_id)
    if not record:
        raise ValueError("Game not found. Import your games again.")
    game = chess.pgn.read_game(io.StringIO(record["pgn"]))
    if not game:
        raise ValueError("That game could not be parsed.")

    board = game.board()
    frames = [{"fen": board.fen(), "lastMove": None, "san": None, "ply": 0}]
    moves = []
    for ply, move in enumerate(game.mainline_moves(), start=1):
        san = board.san(move)
        uci = move.uci()
        board.push(move)
        item = {"fen": board.fen(), "lastMove": uci[:4], "san": san, "ply": ply, "uci": uci}
        frames.append(item)
        moves.append(item)
    return {"summary": record["summary"], "frames": frames, "moves": moves, "url": record["url"]}


def stockfish_path() -> str | None:
    candidates = [
        os.environ.get("STOCKFISH"),
        shutil.which("stockfish"),
        "/usr/games/stockfish",
        str(ROOT / "bin" / "stockfish"),
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def static_eval(board: chess.Board, perspective: chess.Color) -> int:
    if board.is_checkmate():
        return -100_000 if board.turn == perspective else 100_000
    if board.is_game_over():
        return 0
    score = 0
    for piece_type, value in PIECE_VALUES.items():
        score += len(board.pieces(piece_type, perspective)) * value
        score -= len(board.pieces(piece_type, not perspective)) * value
    # A modest activity term makes the fallback less material-only without
    # pretending it can see as deeply as Stockfish.
    mobility = board.legal_moves.count()
    score += mobility * (2 if board.turn == perspective else -2)
    if board.is_check():
        score += -35 if board.turn == perspective else 35
    return score


@dataclass
class Candidate:
    move: chess.Move
    score: int
    reply: chess.Move | None = None


def fallback_candidate(board: chess.Board, perspective: chess.Color, move: chess.Move) -> Candidate:
    trial = board.copy(stack=False)
    trial.push(move)
    if trial.is_game_over():
        return Candidate(move, static_eval(trial, perspective))
    replies = list(trial.legal_moves)
    # Checks and captures first; cap the quiet-move fanout for responsiveness.
    replies.sort(key=lambda m: (trial.is_capture(m), trial.gives_check(m)), reverse=True)
    worst_score = 100_000
    worst_reply = None
    for reply in replies[:32]:
        after = trial.copy(stack=False)
        after.push(reply)
        score = static_eval(after, perspective)
        if score < worst_score:
            worst_score, worst_reply = score, reply
    return Candidate(move, worst_score, worst_reply)


def fallback_analysis(board: chess.Board, actual: chess.Move, perspective: chess.Color) -> tuple[int, chess.Move, list[chess.Move]]:
    candidates = [fallback_candidate(board, perspective, move) for move in board.legal_moves]
    best = max(candidates, key=lambda item: item.score)
    played = next(item for item in candidates if item.move == actual)
    loss = max(0, min(2000, best.score - played.score))
    line = [best.move] + ([best.reply] if best.reply else [])
    return loss, best.move, line


def score_for(info: dict[str, Any], color: chess.Color) -> int:
    score = info["score"].pov(color).score(mate_score=100_000)
    return int(score if score is not None else 0)


def stockfish_move_analysis(
    engine: chess.engine.SimpleEngine,
    board: chess.Board,
    actual: chess.Move,
    perspective: chess.Color,
) -> tuple[int, chess.Move, list[chess.Move]]:
    limit = chess.engine.Limit(depth=13)
    before = engine.analyse(board, limit)
    best = before.get("pv", [actual])[0]
    before_score = score_for(before, perspective)
    after = board.copy(stack=False)
    after.push(actual)
    after_info = engine.analyse(after, limit)
    after_score = score_for(after_info, perspective)
    loss = max(0, min(5000, before_score - after_score))
    return loss, best, list(before.get("pv", []))[:5]


def san_line(board: chess.Board, line: list[chess.Move]) -> str:
    replay = board.copy(stack=False)
    sans = []
    for move in line:
        if move not in replay.legal_moves:
            break
        sans.append(replay.san(move))
        replay.push(move)
    return " ".join(sans)


def move_idea(board: chess.Board, best: chess.Move) -> str:
    if board.is_capture(best):
        victim = board.piece_at(best.to_square)
        if victim:
            return f"wins or trades the {chess.piece_name(victim.piece_type)} on {chess.square_name(best.to_square)}"
        return "uses a tactical capture"
    if board.gives_check(best):
        return "forces the king to respond"
    piece = board.piece_at(best.from_square)
    if piece and piece.piece_type in (chess.KNIGHT, chess.BISHOP) and chess.square_rank(best.from_square) in (0, 7):
        return "develops a piece while keeping the position sound"
    if best.to_square in (chess.D4, chess.E4, chess.D5, chess.E5):
        return "improves control of the center"
    return "keeps more options and avoids the tactical drop"


def classify(loss: int) -> str | None:
    if loss >= 250:
        return "Blunder"
    if loss >= 100:
        return "Miss"
    if loss >= 55:
        return "Inaccuracy"
    return None


def analyze_game(game_id: str) -> dict[str, Any]:
    record = GAMES.get(game_id)
    if not record:
        raise ValueError("Game not found. Import your games again.")
    game = chess.pgn.read_game(io.StringIO(record["pgn"]))
    if not game:
        raise ValueError("That game could not be parsed.")
    player_color = chess.WHITE if record["summary"]["playerColor"] == "white" else chess.BLACK
    engine_path = stockfish_path()
    engine = chess.engine.SimpleEngine.popen_uci(engine_path) if engine_path else None
    board = game.board()
    moments = []
    try:
        for ply, actual in enumerate(game.mainline_moves(), start=1):
            if board.turn == player_color:
                fen = board.fen()
                actual_san = board.san(actual)
                if engine:
                    loss, best, pv = stockfish_move_analysis(engine, board, actual, player_color)
                else:
                    loss, best, pv = fallback_analysis(board, actual, player_color)
                label = classify(loss)
                if label and best != actual:
                    best_san = board.san(best)
                    moments.append({
                        "ply": ply,
                        "moveNumber": board.fullmove_number,
                        "fen": fen,
                        "label": label,
                        "loss": loss,
                        "actual": actual.uci(),
                        "actualSan": actual_san,
                        "best": best.uci(),
                        "bestSan": best_san,
                        "line": san_line(board, pv),
                        "explanation": f"{best_san} {move_idea(board, best)}. Your {actual_san} gave up about {loss / 100:.1f} pawns of value.",
                    })
            board.push(actual)
    finally:
        if engine:
            engine.quit()
    # Keep a review focused. A long losing sequence can contain several similar
    # drops; the twelve largest swings make a better training set.
    if len(moments) > 12:
        moments = sorted(sorted(moments, key=lambda item: item["loss"], reverse=True)[:12], key=lambda item: item["ply"])
    counts = {name: sum(m["label"] == name for m in moments) for name in ("Blunder", "Miss", "Inaccuracy")}
    return {
        "engine": "Stockfish" if engine_path else "Quick tactical scan",
        "moments": moments,
        "counts": counts,
    }


def db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_database() -> None:
    DATA.mkdir(exist_ok=True)
    with db_connection() as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            """
        )


def clean_account_name(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,24}", value):
        raise ValueError("Account names must be 3–24 letters, numbers, dashes, or underscores.")
    return value


def password_digest(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 250_000).hex()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with db_connection() as database:
        database.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        database.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now, now + 30 * 24 * 60 * 60),
        )
    return token


def session_cookie(token: str, max_age: int = 30 * 24 * 60 * 60) -> str:
    return f"replay_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"


def request_session_token(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    cookies = SimpleCookie()
    try:
        cookies.load(cookie_header)
    except Exception:
        return None
    morsel = cookies.get("replay_session")
    return morsel.value if morsel else None


def user_for_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with db_connection() as database:
        row = database.execute(
            """SELECT users.id, users.username
               FROM sessions JOIN users ON users.id = sessions.user_id
               WHERE sessions.token = ? AND sessions.expires_at > ?""",
            (token, int(time.time())),
        ).fetchone()
    return {"id": row["id"], "username": row["username"]} if row else None


def register_account(username: str, password: str) -> tuple[dict[str, Any], str]:
    username = clean_account_name(username)
    if len(password) < 8:
        raise ValueError("Use a password with at least 8 characters.")
    salt = secrets.token_hex(16)
    try:
        with db_connection() as database:
            cursor = database.execute(
                "INSERT INTO users(username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
                (username, password_digest(password, salt), salt, int(time.time())),
            )
            user_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise ValueError("That account name is already in use.") from exc
    return {"id": user_id, "username": username}, create_session(user_id)


def login_account(username: str, password: str) -> tuple[dict[str, Any], str]:
    with db_connection() as database:
        row = database.execute(
            "SELECT id, username, password_hash, salt FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()
    if not row or not hmac.compare_digest(row["password_hash"], password_digest(password, row["salt"])):
        raise ValueError("Incorrect account name or password.")
    user = {"id": row["id"], "username": row["username"]}
    return user, create_session(int(row["id"]))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/status":
                self.send_json({"ok": True, "engine": "Stockfish 18 · in your browser"})
                return
            if parsed.path == "/api/auth/me":
                token = request_session_token(self.headers.get("Cookie"))
                self.send_json({"user": user_for_token(token)})
                return
            if parsed.path == "/api/games":
                query = urllib.parse.parse_qs(parsed.query)
                username = query.get("username", [""])[0]
                source = query.get("source", ["chesscom"])[0]
                latest = query.get("scope", ["recent"])[0] == "latest"
                self.send_json(import_games(username, source, latest))
                return
            if parsed.path.startswith("/api/game/"):
                game_id = parsed.path.rsplit("/", 1)[-1]
                self.send_json(game_detail(game_id))
                return
            super().do_GET()
        except (ValueError, KeyError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            print(f"Request failed: {exc}", file=sys.stderr)
            self.send_json({"error": "Something went wrong while loading that data."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if parsed.path == "/api/auth/register":
                user, token = register_account(str(payload.get("username", "")), str(payload.get("password", "")))
                self.send_json({"user": user}, headers={"Set-Cookie": session_cookie(token)})
                return
            if parsed.path == "/api/auth/login":
                user, token = login_account(str(payload.get("username", "")), str(payload.get("password", "")))
                self.send_json({"user": user}, headers={"Set-Cookie": session_cookie(token)})
                return
            if parsed.path == "/api/auth/logout":
                token = request_session_token(self.headers.get("Cookie"))
                if token:
                    with db_connection() as database:
                        database.execute("DELETE FROM sessions WHERE token = ?", (token,))
                self.send_json({"ok": True}, headers={"Set-Cookie": session_cookie("", 0)})
                return
            if parsed.path == "/api/analyze":
                self.send_json(analyze_game(str(payload.get("gameId", ""))))
                return
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            print(f"Analysis failed: {exc}", file=sys.stderr)
            self.send_json({"error": "Analysis failed for this game."}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    # Use app-specific variables so an unrelated HOST/PORT setting cannot
    # accidentally move Replay onto another service's address.
    init_database()
    host_setting = os.environ.get("REPLAY_HOST", "localhost").strip()
    host = "" if host_setting.lower() in {"all", "any"} else host_setting
    port = int(os.environ.get("REPLAY_PORT", "47831"))
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        if exc.errno == 98:
            print(
                f"Port {port} is already in use. Choose another with "
                f"REPLAY_PORT=47832 ./run.sh",
                file=sys.stderr,
            )
        raise
    print(f"Replay is ready locally at http://localhost:{port}")
    print("Analysis: Stockfish 18 runs inside each visitor's browser")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Replay.")


if __name__ == "__main__":
    main()
