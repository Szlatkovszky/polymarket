"""Regenerate the public PAPER dashboard from a sqlite ledger.

The database is opened read-only. This does not trade, sign, or change risk
limits. ``edge_proven`` is always false.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from research_lab.lab import OrderBook, simulate_fok
from research_lab.money import D, q_cash

REPO_URL = "https://github.com/Szlatkovszky/polymarket"
_BUDAPEST = ZoneInfo("Europe/Budapest")


class PublishError(RuntimeError):
    pass


def publish_public_status(
    *,
    db_path: Path,
    docs_dir: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write ``status.json`` and ``index.html``. Does not write the database."""

    path = Path(db_path)
    if not path.is_file():
        raise PublishError(f"PAPER database not found: {path}")
    status = read_paper_status(path, now=now or datetime.now(timezone.utc))
    docs = Path(docs_dir)
    docs.mkdir(parents=True, exist_ok=True)
    status_path = docs / "status.json"
    html_path = docs / "index.html"
    status_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    html_path.write_text(render_status_html(status), encoding="utf-8")
    return {
        "status_path": str(status_path),
        "html_path": str(html_path),
        "mode": status["mode"],
        "edge_proven": False,
        "live_trading": False,
        "disclaimer": status["disclaimer"],
    }


def read_paper_status(db_path: Path, *, now: datetime) -> dict[str, Any]:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise PublishError(f"cannot open PAPER database read-only: {db_path}") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        mode_row = conn.execute("SELECT v FROM meta WHERE k = 'mode'").fetchone()
        if mode_row is None or str(mode_row["v"]) != "PAPER":
            found = None if mode_row is None else str(mode_row["v"])
            raise PublishError(f"publish-status reads a PAPER database only (found {found})")
        account = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if account is None:
            raise PublishError("paper database has no account row")
        positions = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id"
            )
        ]
        fills = [dict(row) for row in conn.execute("SELECT * FROM fills ORDER BY id")]
        decision_counts = [
            {"action": row["action"], "reason": row["reason"], "c": int(row["c"])}
            for row in conn.execute(
                """
                SELECT action, reason, COUNT(*) AS c
                FROM decisions
                GROUP BY action, reason
                ORDER BY c DESC, action, reason
                """
            )
        ]
        cash = str(account["cash"])
        equity, valuation_complete = _mark_equity(conn, D(cash), positions)
    finally:
        conn.close()
    local = now.astimezone(_BUDAPEST)
    return {
        "updated_at": local.strftime("%Y-%m-%d %H:%M %Z"),
        "mode": "PAPER",
        "cash": cash,
        "equity": equity,
        "peak_equity": str(account["peak_equity"]),
        "paused": bool(account["paused"]),
        "risk_version": str(account["risk_version"]),
        "valuation_complete": valuation_complete,
        "edge_proven": False,
        "live_trading": False,
        "disclaimer": (
            "PAPER/DEMO simulation only. Fills are not live orders. "
            "No profitability is claimed. edge_proven is false."
        ),
        "positions": positions,
        "fills": fills,
        "decision_counts": decision_counts,
        "repo": REPO_URL,
        "last_run": {
            "kind": "publish_status",
            "new_paper_fills": 0,
            "note": (
                "PAPER snapshot regenerated from the local paper ledger. "
                "Numbers are simulated. edge_proven is false. "
                "No live trading and no profitability claim."
            ),
        },
    }


def _mark_equity(
    conn: sqlite3.Connection,
    cash: Decimal,
    positions: list[dict[str, Any]],
) -> tuple[str, bool]:
    marked = D(0)
    complete = True
    for pos in positions:
        book_row = conn.execute(
            "SELECT * FROM books WHERE token_id = ? ORDER BY id DESC LIMIT 1",
            (pos["token_id"],),
        ).fetchone()
        if book_row is None:
            complete = False
            continue
        book = OrderBook.from_clob(
            json.loads(book_row["snapshot_json"]),
            token_id=str(pos["token_id"]),
        )
        rate_s = book_row["fee_rate"]
        fee_rate = D(rate_s) if rate_s not in (None, "") else None
        fill = simulate_fok(book, side="SELL", shares=D(pos["shares"]), fee_rate=fee_rate)
        if not fill.filled:
            complete = False
            continue
        marked += fill.notional - fill.fee
    return str(q_cash(cash + marked)), complete


def render_status_html(status: dict[str, Any]) -> str:
    payload = json.dumps(status, ensure_ascii=False).replace("<", "\\u003c")
    note = str((status.get("last_run") or {}).get("note") or "")
    edge = "false" if status.get("edge_proven") is False else "true"
    live = "off" if status.get("live_trading") is False else "on"
    return f"""<!DOCTYPE html>
<html lang="hu">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Polymarket Research Lab — PAPER status</title>
  <style>
    :root {{ --ink:#14232d; --muted:#526477; --line:#d9e0e5; --bg:#edf1f2; --paper:#fff; --accent:#236957; --warn:#92400e; --warnbg:#fef3c7; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:var(--bg); }}
    .wrap {{ max-width:920px; margin:0 auto; padding:28px 18px 64px; }}
    .banner {{ background:#14232d; color:#eff8f4; padding:18px 20px; border-radius:10px; }}
    .banner strong {{ color:#d1f0e1; }}
    h1 {{ font-size:clamp(26px,4vw,36px); margin:22px 0 8px; line-height:1.15; }}
    .sub {{ color:var(--muted); margin:0 0 20px; }}
    .warn {{ background:var(--warnbg); color:var(--warn); padding:14px 16px; border-radius:10px; margin:0 0 22px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:22px; }}
    .card {{ background:var(--paper); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
    .card .label {{ font-size:12px; letter-spacing:.04em; text-transform:uppercase; color:var(--muted); }}
    .card .value {{ font-size:22px; font-weight:650; margin-top:6px; font-variant-numeric:tabular-nums; }}
    .card .value.false {{ color:#b45309; }}
    table {{ width:100%; border-collapse:collapse; background:var(--paper); border:1px solid var(--line); border-radius:10px; overflow:hidden; font-size:14px; }}
    th, td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ background:#eaf0ef; font-size:12px; color:var(--muted); }}
    h2 {{ font-size:18px; margin:28px 0 12px; }}
    a {{ color:var(--accent); }}
    code {{ background:#edf1f4; padding:2px 6px; border-radius:4px; font-size:13px; }}
    footer {{ margin-top:28px; font-size:12px; color:var(--muted); }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="banner"><strong>PAPER mode</strong> — csak szimuláció. Élő kereskedés, tárca és automatikus élesítés ki van kapcsolva. PAPER/DEMO only. No live trading.</div>
    <h1>Polymarket Research Lab</h1>
    <p class="sub">Nyilvános státusz-dashboard · frissítve: <span id="updated">{_esc(status.get("updated_at"))}</span> (Europe/Budapest)</p>
    <div class="warn"><strong>Nincs igazolt edge.</strong> edge_proven: {edge}. A számok paper/demo könyvelés, nem élő P&amp;L. No profitability claim.</div>
    <div class="warn" id="last-run-note">{_esc(note)}</div>
    <div class="grid">
      <div class="card"><div class="label">Cash</div><div class="value">{_esc(status.get("cash"))}</div></div>
      <div class="card"><div class="label">Marked equity</div><div class="value">{_esc(status.get("equity"))}</div></div>
      <div class="card"><div class="label">Peak equity</div><div class="value">{_esc(status.get("peak_equity"))}</div></div>
      <div class="card"><div class="label">Risk</div><div class="value">{_esc(status.get("risk_version"))}</div></div>
      <div class="card"><div class="label">Edge proven</div><div class="value false">{edge}</div></div>
      <div class="card"><div class="label">Live trading</div><div class="value false">{live}</div></div>
    </div>

    <h2>Nyitott paper pozíciók</h2>
    <table>
      <thead><tr><th>ID</th><th>Market</th><th>Side</th><th>Shares</th><th>Avg</th><th>Fee</th><th>Status</th><th>Opened (UTC)</th></tr></thead>
      <tbody id="positions"></tbody>
    </table>

    <h2>Paper fills</h2>
    <table>
      <thead><tr><th>ID</th><th>Market</th><th>Side</th><th>Token</th><th>Shares</th><th>Price</th><th>Fee</th><th>Filled (UTC)</th></tr></thead>
      <tbody id="fills"></tbody>
    </table>

    <h2>Döntések (összesítés)</h2>
    <table>
      <thead><tr><th>Action</th><th>Reason</th><th>Count</th></tr></thead>
      <tbody id="decisions"></tbody>
    </table>

    <footer>
      Forrásrepo: <a href="{REPO_URL}">{REPO_URL}</a>
      · Ez a GitHub Pages snapshot a <code>docs/</code> mappából.
      · A helyi élő API továbbra is <code>http://127.0.0.1:8000</code> a lab gépen.
      · PAPER disclaimer. edge_proven is false.
    </footer>
  </div>
  <script id="state" type="application/json">{payload}</script>
  <script>
    const s = JSON.parse(document.getElementById('state').textContent);
    const esc = (x) => String(x ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
    document.getElementById('positions').innerHTML = (s.positions||[]).map(p =>
      `<tr><td>${{esc(p.id)}}</td><td><code>${{esc(p.market_id)}}</code><br/><small>${{esc(p.cluster_id||'')}}</small></td><td>${{esc(p.token_side)}}</td><td>${{esc(p.shares)}}</td><td>${{esc(p.avg_price)}}</td><td>${{esc(p.entry_fee)}}</td><td>${{esc(p.status)}}</td><td>${{esc(p.opened_at)}}</td></tr>`
    ).join('') || '<tr><td colspan="8">Nincs nyitott pozíció</td></tr>';
    document.getElementById('fills').innerHTML = (s.fills||[]).map(f =>
      `<tr><td>${{esc(f.id)}}</td><td><code>${{esc(f.market_id)}}</code></td><td>${{esc(f.side)}}</td><td>${{esc(f.token_side)}}</td><td>${{esc(f.shares)}}</td><td>${{esc(f.avg_price)}}</td><td>${{esc(f.fee)}}</td><td>${{esc(f.filled_at)}}</td></tr>`
    ).join('') || '<tr><td colspan="8">Nincs fill</td></tr>';
    document.getElementById('decisions').innerHTML = (s.decision_counts||[]).map(d =>
      `<tr><td>${{esc(d.action)}}</td><td>${{esc(d.reason)}}</td><td>${{esc(d.c)}}</td></tr>`
    ).join('') || '<tr><td colspan="3">Nincs adat</td></tr>';
  </script>
</body>
</html>
"""


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
