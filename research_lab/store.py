"""SQLite persistence. PAPER and DEMO use separate files; never mixed."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from research_lab.money import D, q_cash

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  mode TEXT NOT NULL,
  cash TEXT NOT NULL,
  peak_equity TEXT NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0,
  risk_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS day_marks (
  day TEXT PRIMARY KEY,
  start_equity TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS markets (
  market_id TEXT PRIMARY KEY,
  condition_id TEXT,
  question TEXT,
  rules_text TEXT NOT NULL,
  rules_hash TEXT NOT NULL,
  yes_token_id TEXT,
  no_token_id TEXT,
  cutoff_at TEXT,
  timezone TEXT,
  resolution_source TEXT,
  raw_json TEXT NOT NULL,
  ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  token_side TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  rules_hash TEXT NOT NULL,
  fee_bps INTEGER,
  fee_rate TEXT,
  meta_json TEXT NOT NULL,
  captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules_reviews (
  market_id TEXT PRIMARY KEY,
  rules_hash TEXT NOT NULL,
  cluster_id TEXT NOT NULL,
  trading_cutoff TEXT NOT NULL,
  expected_resolution TEXT,
  expected_settlement_source TEXT,
  paper_model_version TEXT,
  reviewer TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS authorized_models (
  model_version TEXT PRIMARY KEY,
  authorized_at TEXT NOT NULL,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS forecasts (
  forecast_id TEXT PRIMARY KEY,
  canonical_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  market_id TEXT NOT NULL,
  model_version TEXT NOT NULL,
  rules_hash TEXT NOT NULL,
  as_of TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  p_yes TEXT NOT NULL,
  p_low TEXT NOT NULL,
  p_high TEXT NOT NULL,
  imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  forecast_id TEXT,
  market_id TEXT,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  token_side TEXT,
  details_json TEXT NOT NULL,
  decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  forecast_id TEXT NOT NULL,
  token_side TEXT NOT NULL,
  token_id TEXT NOT NULL,
  shares TEXT NOT NULL,
  avg_price TEXT NOT NULL,
  entry_fee TEXT NOT NULL,
  cluster_id TEXT,
  opened_at TEXT NOT NULL,
  status TEXT NOT NULL,
  closed_at TEXT,
  settle_payoff TEXT,
  settle_source TEXT
);

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER,
  forecast_id TEXT,
  market_id TEXT NOT NULL,
  side TEXT NOT NULL,
  token_side TEXT NOT NULL,
  token_id TEXT NOT NULL,
  shares TEXT NOT NULL,
  avg_price TEXT NOT NULL,
  fee TEXT NOT NULL,
  notional TEXT NOT NULL,
  book_id INTEGER,
  filled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  amount TEXT NOT NULL,
  ref TEXT,
  note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ops_costs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  amount TEXT NOT NULL,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_estimates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  rules_hash TEXT NOT NULL,
  as_of TEXT NOT NULL,
  model_version TEXT NOT NULL,
  calibration_version TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  variant_json TEXT NOT NULL,
  decision_json TEXT NOT NULL,
  request_json TEXT,
  input_hash TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_archive (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  source TEXT NOT NULL,
  market_id TEXT,
  token_id TEXT,
  url TEXT,
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  rules_hash TEXT,
  captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id TEXT NOT NULL,
  market_id TEXT,
  as_of TEXT,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  details_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_books_market ON books(market_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_research_estimates_market
  ON research_estimates(market_id, as_of);
CREATE INDEX IF NOT EXISTS idx_raw_archive_market
  ON raw_archive(market_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_paper_runs_cycle ON paper_runs(cycle_id, id);
"""


@dataclass(frozen=True)
class MarketRow:
    market_id: str
    condition_id: str | None
    question: str | None
    rules_text: str
    rules_hash: str
    yes_token_id: str | None
    no_token_id: str | None
    cutoff_at: str | None
    timezone: str | None
    resolution_source: str | None
    raw_json: str
    ingested_at: str


class Store:
    def __init__(self, path: Path, mode: str) -> None:
        self.path = Path(path)
        self.mode = mode
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        stored = self.get_meta("mode")
        if stored is None:
            self.set_meta("mode", mode)
        elif stored != mode:
            raise RuntimeError(
                f"database {self.path} is mode={stored}, refusing to open as {mode}"
            )

    def _migrate(self) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(books)")}
        if "fee_rate" not in cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN fee_rate TEXT")
        review_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(rules_reviews)")}
        if "expected_settlement_source" not in review_cols:
            self.conn.execute(
                "ALTER TABLE rules_reviews ADD COLUMN expected_settlement_source TEXT"
            )
        if "paper_model_version" not in review_cols:
            self.conn.execute("ALTER TABLE rules_reviews ADD COLUMN paper_model_version TEXT")
        est_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(research_estimates)")
        }
        if "request_json" not in est_cols:
            self.conn.execute("ALTER TABLE research_estimates ADD COLUMN request_json TEXT")
        if "input_hash" not in est_cols:
            self.conn.execute("ALTER TABLE research_estimates ADD COLUMN input_hash TEXT")

    def close(self) -> None:
        self.conn.close()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return None if row is None else str(row["v"])

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )

    def init_account(self, *, cash: Decimal, risk_version: str, now_iso: str) -> None:
        row = self.conn.execute("SELECT id FROM account WHERE id = 1").fetchone()
        if row is not None:
            return
        cash_s = str(q_cash(cash))
        self.conn.execute(
            """
            INSERT INTO account(id, mode, cash, peak_equity, paused, risk_version, created_at)
            VALUES (1, ?, ?, ?, 0, ?, ?)
            """,
            (self.mode, cash_s, cash_s, risk_version, now_iso),
        )

    def account(self) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("account row missing")
        return row

    def cash(self) -> Decimal:
        return D(self.account()["cash"])

    def set_paused(self, paused: bool) -> None:
        self.conn.execute("UPDATE account SET paused = ? WHERE id = 1", (1 if paused else 0,))

    def is_paused(self) -> bool:
        return bool(self.account()["paused"])

    def add_cash(self, delta: Decimal) -> Decimal:
        new = q_cash(self.cash() + delta)
        self.conn.execute("UPDATE account SET cash = ? WHERE id = 1", (str(new),))
        return new

    def set_peak_equity(self, peak: Decimal) -> None:
        self.conn.execute(
            "UPDATE account SET peak_equity = ? WHERE id = 1", (str(q_cash(peak)),)
        )

    def peak_equity(self) -> Decimal:
        return D(self.account()["peak_equity"])

    def upsert_day_mark(self, day: str, start_equity: Decimal) -> Decimal:
        existing = self.conn.execute(
            "SELECT start_equity FROM day_marks WHERE day = ?", (day,)
        ).fetchone()
        if existing is not None:
            return D(existing["start_equity"])
        value = str(q_cash(start_equity))
        self.conn.execute(
            "INSERT INTO day_marks(day, start_equity) VALUES(?, ?)", (day, value)
        )
        return D(value)

    def upsert_market(self, row: Mapping[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO markets(
              market_id, condition_id, question, rules_text, rules_hash,
              yes_token_id, no_token_id, cutoff_at, timezone, resolution_source,
              raw_json, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
              condition_id = excluded.condition_id,
              question = excluded.question,
              rules_text = excluded.rules_text,
              rules_hash = excluded.rules_hash,
              yes_token_id = excluded.yes_token_id,
              no_token_id = excluded.no_token_id,
              cutoff_at = excluded.cutoff_at,
              timezone = excluded.timezone,
              resolution_source = excluded.resolution_source,
              raw_json = excluded.raw_json,
              ingested_at = excluded.ingested_at
            """,
            (
                row["market_id"],
                row.get("condition_id"),
                row.get("question"),
                row["rules_text"],
                row["rules_hash"],
                row.get("yes_token_id"),
                row.get("no_token_id"),
                row.get("cutoff_at"),
                row.get("timezone"),
                row.get("resolution_source"),
                row["raw_json"],
                row["ingested_at"],
            ),
        )

    def get_market(self, market_id: str) -> MarketRow | None:
        row = self.conn.execute(
            "SELECT * FROM markets WHERE market_id = ?", (market_id,)
        ).fetchone()
        if row is None:
            return None
        return MarketRow(
            market_id=row["market_id"],
            condition_id=row["condition_id"],
            question=row["question"],
            rules_text=row["rules_text"],
            rules_hash=row["rules_hash"],
            yes_token_id=row["yes_token_id"],
            no_token_id=row["no_token_id"],
            cutoff_at=row["cutoff_at"],
            timezone=row["timezone"],
            resolution_source=row["resolution_source"],
            raw_json=row["raw_json"],
            ingested_at=row["ingested_at"],
        )

    def list_markets(self) -> list[MarketRow]:
        rows = self.conn.execute("SELECT * FROM markets ORDER BY market_id").fetchall()
        out = []
        for row in rows:
            got = self.get_market(row["market_id"])
            if got is not None:
                out.append(got)
        return out

    def insert_book(
        self,
        *,
        market_id: str,
        token_id: str,
        token_side: str,
        snapshot: Mapping[str, Any],
        rules_hash: str,
        fee_bps: int | None,
        meta: Mapping[str, Any],
        captured_at: str,
        fee_rate: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO books(
              market_id, token_id, token_side, snapshot_json, rules_hash,
              fee_bps, fee_rate, meta_json, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_id,
                token_id,
                token_side,
                json.dumps(snapshot, sort_keys=True),
                rules_hash,
                fee_bps,
                fee_rate,
                json.dumps(meta, sort_keys=True),
                captured_at,
            ),
        )
        return int(cur.lastrowid)

    def latest_book(self, token_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM books WHERE token_id = ? ORDER BY id DESC LIMIT 1",
            (token_id,),
        ).fetchone()

    def insert_rules_review(self, row: Mapping[str, Any]) -> None:
        expected_resolution = row.get("expected_resolution") or ""
        expected_settlement = (
            row.get("expected_settlement_source") or expected_resolution or ""
        )
        self.conn.execute(
            """
            INSERT INTO rules_reviews(
              market_id, rules_hash, cluster_id, trading_cutoff,
              expected_resolution, expected_settlement_source, paper_model_version,
              reviewer, reviewed_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
              rules_hash = excluded.rules_hash,
              cluster_id = excluded.cluster_id,
              trading_cutoff = excluded.trading_cutoff,
              expected_resolution = excluded.expected_resolution,
              expected_settlement_source = excluded.expected_settlement_source,
              paper_model_version = excluded.paper_model_version,
              reviewer = excluded.reviewer,
              reviewed_at = excluded.reviewed_at,
              notes = excluded.notes
            """,
            (
                row["market_id"],
                row["rules_hash"],
                row["cluster_id"],
                row["trading_cutoff"],
                expected_resolution,
                expected_settlement,
                row.get("paper_model_version") or None,
                row["reviewer"],
                row["reviewed_at"],
                row.get("notes"),
            ),
        )

    def get_rules_review(self, market_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM rules_reviews WHERE market_id = ?", (market_id,)
        ).fetchone()

    def list_rules_reviews(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM rules_reviews ORDER BY reviewed_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_authorized_models(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM authorized_models ORDER BY model_version"
        ).fetchall()
        return [dict(r) for r in rows]

    def authorize_model(self, model_version: str, authorized_at: str, notes: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO authorized_models(model_version, authorized_at, notes)
            VALUES (?, ?, ?)
            ON CONFLICT(model_version) DO UPDATE SET
              authorized_at = excluded.authorized_at,
              notes = excluded.notes
            """,
            (model_version, authorized_at, notes),
        )

    def model_authorized(self, model_version: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM authorized_models WHERE model_version = ?",
            (model_version,),
        ).fetchone()
        return row is not None

    def get_forecast(self, forecast_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM forecasts WHERE forecast_id = ?", (forecast_id,)
        ).fetchone()

    def insert_forecast(self, row: Mapping[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO forecasts(
              forecast_id, canonical_json, content_hash, market_id, model_version,
              rules_hash, as_of, expires_at, p_yes, p_low, p_high, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["forecast_id"],
                row["canonical_json"],
                row["content_hash"],
                row["market_id"],
                row["model_version"],
                row["rules_hash"],
                row["as_of"],
                row["expires_at"],
                row["p_yes"],
                row["p_low"],
                row["p_high"],
                row["imported_at"],
            ),
        )

    def insert_decision(self, row: Mapping[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO decisions(
              forecast_id, market_id, action, reason, token_side, details_json, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("forecast_id"),
                row.get("market_id"),
                row["action"],
                row["reason"],
                row.get("token_side"),
                json.dumps(row.get("details") or {}, sort_keys=True),
                row["decided_at"],
            ),
        )
        return int(cur.lastrowid)

    def list_decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def open_position_for_market(self, market_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM positions WHERE market_id = ? AND status = 'OPEN'",
            (market_id,),
        ).fetchone()

    def get_position(self, position_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()

    def list_positions(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM positions ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def open_position_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM positions WHERE status = 'OPEN'"
        ).fetchone()
        return int(row["n"])

    def buy_fills_on_utc_day(self, day: str) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM fills
            WHERE side = 'BUY' AND substr(filled_at, 1, 10) = ?
            """,
            (day,),
        ).fetchone()
        return int(row["n"])

    def open_entry_cost(self) -> Decimal:
        rows = self.conn.execute(
            """
            SELECT shares, avg_price, entry_fee FROM positions WHERE status = 'OPEN'
            """
        ).fetchall()
        total = D(0)
        for row in rows:
            total += D(row["shares"]) * D(row["avg_price"]) + D(row["entry_fee"])
        return total

    def cluster_open_notional(self, cluster_id: str) -> Decimal:
        rows = self.conn.execute(
            """
            SELECT shares, avg_price FROM positions
            WHERE status = 'OPEN' AND cluster_id = ?
            """,
            (cluster_id,),
        ).fetchall()
        total = D(0)
        for row in rows:
            total += D(row["shares"]) * D(row["avg_price"])
        return total

    def insert_position(self, row: Mapping[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO positions(
              market_id, forecast_id, token_side, token_id, shares, avg_price,
              entry_fee, cluster_id, opened_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
            """,
            (
                row["market_id"],
                row["forecast_id"],
                row["token_side"],
                row["token_id"],
                row["shares"],
                row["avg_price"],
                row["entry_fee"],
                row.get("cluster_id"),
                row["opened_at"],
            ),
        )
        return int(cur.lastrowid)

    def insert_fill(self, row: Mapping[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO fills(
              position_id, forecast_id, market_id, side, token_side, token_id,
              shares, avg_price, fee, notional, book_id, filled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("position_id"),
                row.get("forecast_id"),
                row["market_id"],
                row["side"],
                row["token_side"],
                row["token_id"],
                row["shares"],
                row["avg_price"],
                row["fee"],
                row["notional"],
                row.get("book_id"),
                row["filled_at"],
            ),
        )
        return int(cur.lastrowid)

    def insert_ledger(self, *, kind: str, amount: Decimal, ref: str | None, note: str, created_at: str) -> None:
        self.conn.execute(
            """
            INSERT INTO ledger(kind, amount, ref, note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (kind, str(q_cash(amount)), ref, note, created_at),
        )

    def insert_ops_cost(self, amount: Decimal, note: str, created_at: str) -> None:
        self.conn.execute(
            "INSERT INTO ops_costs(amount, note, created_at) VALUES (?, ?, ?)",
            (str(q_cash(amount)), note, created_at),
        )

    def list_ops_costs(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM ops_costs ORDER BY id")]

    def insert_research_estimate(self, row: Mapping[str, Any]) -> int:
        request_payload = row.get("request") if "request" in row else row.get("request_json")
        if isinstance(request_payload, str):
            request_json = request_payload
        elif request_payload is None:
            request_json = None
        else:
            request_json = json.dumps(request_payload, sort_keys=True)
        cur = self.conn.execute(
            """
            INSERT INTO research_estimates(
              market_id, rules_hash, as_of, model_version, calibration_version,
              status, reason, variant_json, decision_json, request_json,
              input_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["market_id"],
                row["rules_hash"],
                row["as_of"],
                row["model_version"],
                row["calibration_version"],
                row["status"],
                row["reason"],
                json.dumps(row.get("variants") or {}, sort_keys=True),
                json.dumps(row.get("decision") or {}, sort_keys=True),
                request_json,
                row.get("input_hash"),
                row["created_at"],
            ),
        )
        return int(cur.lastrowid)

    def get_research_estimate(self, estimate_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM research_estimates WHERE id = ?", (estimate_id,)
        ).fetchone()
        if row is None:
            return None
        return self._decode_research_estimate(row)

    def list_research_estimates(
        self, market_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        if market_id:
            rows = self.conn.execute(
                """
                SELECT * FROM research_estimates
                WHERE market_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (market_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM research_estimates ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode_research_estimate(row) for row in rows]

    def _decode_research_estimate(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["variants"] = json.loads(item.pop("variant_json") or "{}")
        item["decision"] = json.loads(item.pop("decision_json") or "{}")
        raw_request = item.pop("request_json", None)
        if raw_request:
            try:
                item["request"] = json.loads(raw_request)
            except json.JSONDecodeError:
                item["request"] = None
                item["request_raw"] = raw_request
        else:
            item["request"] = None
        return item

    def insert_raw_archive(self, row: Mapping[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO raw_archive(
              kind, source, market_id, token_id, url, payload_json,
              payload_hash, rules_hash, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["kind"],
                row["source"],
                row.get("market_id"),
                row.get("token_id"),
                row.get("url"),
                json.dumps(row["payload"], sort_keys=True, default=str)
                if not isinstance(row.get("payload_json"), str)
                else row["payload_json"],
                row["payload_hash"],
                row.get("rules_hash"),
                row["captured_at"],
            ),
        )
        return int(cur.lastrowid)

    def list_raw_archive(
        self,
        *,
        market_id: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM raw_archive WHERE 1=1"
        params: list[Any] = []
        if market_id:
            sql += " AND market_id = ?"
            params.append(market_id)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def insert_paper_run(self, row: Mapping[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO paper_runs(
              cycle_id, market_id, as_of, action, reason, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["cycle_id"],
                row.get("market_id"),
                row.get("as_of"),
                row["action"],
                row["reason"],
                json.dumps(row.get("details") or {}, sort_keys=True, default=str),
                row["created_at"],
            ),
        )
        return int(cur.lastrowid)

    def list_paper_runs(
        self, cycle_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        if cycle_id:
            rows = self.conn.execute(
                """
                SELECT * FROM paper_runs WHERE cycle_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (cycle_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM paper_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json") or "{}")
            out.append(item)
        return out

    def list_fills(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM fills ORDER BY id")]

    def close_position(
        self,
        position_id: int,
        *,
        status: str,
        closed_at: str,
        settle_payoff: str | None = None,
        settle_source: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE positions
            SET status = ?, closed_at = ?, settle_payoff = ?, settle_source = ?
            WHERE id = ?
            """,
            (status, closed_at, settle_payoff, settle_source, position_id),
        )

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    def iter_closed_for_eval(self) -> Iterator[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT p.*, r.cluster_id AS review_cluster
            FROM positions p
            LEFT JOIN rules_reviews r ON r.market_id = p.market_id
            WHERE p.status IN ('CLOSED', 'SETTLED')
            ORDER BY p.id
            """
        ).fetchall()
        for row in rows:
            yield dict(row)
