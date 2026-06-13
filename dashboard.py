from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, Callable

from paper_tracker import PaperTracker


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DashboardConfig:
    enabled: bool
    host: str
    port: int
    refresh_seconds: int


class PaperDashboardServer:
    def __init__(
        self,
        tracker: PaperTracker,
        config: DashboardConfig,
        grid_snapshot_provider: Callable[[], dict[str, Any]] | None = None,
        loop_details_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.tracker = tracker
        self.config = config
        self.grid_snapshot_provider = grid_snapshot_provider
        self.loop_details_provider = loop_details_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    def snapshot(self, include_grid: bool = True, include_loop: bool = True) -> dict[str, Any]:
        payload = self.tracker.snapshot()
        if include_grid and self.grid_snapshot_provider is not None:
            payload["grid_stats"] = self.grid_snapshot_provider()
        if include_loop and self.loop_details_provider is not None:
            payload["loop_details"] = self.loop_details_provider()
        return payload

    def start(self) -> None:
        if not self.config.enabled or self._server is not None:
            return

        dashboard = self
        refresh_seconds = self.config.refresh_seconds

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/api/paper":
                    self._send_json(dashboard.snapshot())
                    return
                if path in {"/", "/paper", "/loop"}:
                    self._send_html(render_loop_dashboard(dashboard.snapshot(include_grid=False), refresh_seconds))
                    return
                if path == "/grid":
                    self._send_html(render_grid_dashboard(dashboard.snapshot(include_loop=False), refresh_seconds))
                    return
                self.send_error(404, "Not found")

            def log_message(self, format: str, *args: Any) -> None:
                LOGGER.info("Dashboard: " + format, *args)

            def _send_json(self, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, payload: str) -> None:
                body = payload.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((self.config.host, self.config.port), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        LOGGER.info("Paper dashboard running at http://%s:%s", self.config.host, self.config.port)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None


def render_loop_dashboard(snapshot: dict[str, Any], refresh_seconds: int) -> str:
    window = snapshot["window_stats"]
    all_time = snapshot["all_stats"]
    loop_details = snapshot.get("loop_details", {})
    active_trades = snapshot["active_trades"]
    closed_trades = snapshot["closed_trades"]

    active_rows = "".join(_active_row(row) for row in active_trades) or _empty_row(7, "No active alerts.")
    closed_rows = "".join(_closed_row(row) for row in closed_trades) or _empty_row(10, "No closed paper trades yet.")
    loop_scanned = loop_details.get("scanned", [])
    if loop_scanned:
        scanned_rows = "".join(_loop_scan_row(row) for row in loop_scanned)
    else:
        scanned_rows = "".join(_loop_scan_row({"symbol": symbol}) for symbol in loop_details.get("pairs", []))
    scanned_rows = scanned_rows or _empty_row(7, "No LOOP pairs loaded.")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{int(refresh_seconds)}">
  <title>Loopbots Paper Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0f1117;
      --panel: #171b24;
      --panel-2: #1f2530;
      --line: #2c3442;
      --text: #edf2f7;
      --muted: #9aa4b2;
      --good: #25d695;
      --bad: #ff6875;
      --accent: #4da3ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, Helvetica, sans-serif;
      line-height: 1.4;
    }}
    main {{
      width: min(1180px, calc(100vw - 32px));
      margin: 24px auto 40px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-end;
      margin-bottom: 18px;
    }}
    h1, h2 {{ margin: 0; }}
    h1 {{ font-size: 28px; }}
    h2 {{ font-size: 18px; margin-bottom: 10px; }}
    nav {{ display: flex; gap: 8px; margin: 0 0 18px; }}
    nav a {{
      color: var(--text);
      text-decoration: none;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 13px;
      font-weight: 700;
    }}
    nav a.active {{ background: var(--accent); color: #07111f; border-color: var(--accent); }}
    .muted {{ color: var(--muted); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}
    .card, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .card {{ padding: 14px; }}
    .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    .value {{ font-size: 24px; font-weight: 700; margin-top: 4px; }}
    .good {{ color: var(--good); }}
    .bad {{ color: var(--bad); }}
    section {{ padding: 16px; margin-top: 16px; overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 760px; }}
    th, td {{
      text-align: left;
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      white-space: nowrap;
      font-size: 14px;
    }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    tr:last-child td {{ border-bottom: 0; }}
    .pill {{
      display: inline-block;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--panel-2);
      color: var(--text);
      font-size: 12px;
    }}
    @media (max-width: 860px) {{
      header {{ display: block; }}
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 520px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>LOOP Paper</h1>
        <div class="muted">Auto-refreshes every {int(refresh_seconds)} seconds. Data older than {snapshot["retention_days"]} days is pruned.</div>
      </div>
      <div class="muted">Generated {_escape(snapshot["generated_at"])}</div>
    </header>
    <nav>
      <a class="active" href="/loop">LOOP Bots</a>
      <a href="/grid">GRID Bots</a>
    </nav>

    <div class="grid">
      {_metric("Closed", window["closed"], f"Last {snapshot['lookback_days']} days")}
      {_metric("Win Rate", _pct(window["win_rate_pct"]), "TP closes / closed alerts")}
      {_metric("Net Return", _signed_pct(window["net_return_pct"]), f"After {snapshot['fee_pct']:.2f}% fee estimate", window["net_return_pct"])}
      {_metric("Avg Net/Trade", _signed_pct(window["avg_net_return_pct"]), "Quality target", window["avg_net_return_pct"])}
      {_metric("Active", len(active_trades), "Open alerts")}
      {_metric("Avg Hold", f"{window['avg_hold_hours']:.2f}h", "Closed alerts")}
      {_metric("All-Time WR", _pct(all_time["win_rate_pct"]), "Retained history")}
      {_metric("All-Time Avg", _signed_pct(all_time["avg_net_return_pct"]), "Retained history", all_time["avg_net_return_pct"])}
    </div>

    <section>
      <h2>Scanned LOOP Pairs</h2>
      <table>
        <thead>
          <tr><th>Coin</th><th>Hot Score</th><th>Price</th><th>24h Volatility</th><th>24h Move</th><th>Quote Volume</th><th>Reason</th></tr>
        </thead>
        <tbody>{scanned_rows}</tbody>
      </table>
    </section>

    <section>
      <h2>Active Alerts</h2>
      <table>
        <thead>
          <tr><th>Coin</th><th>Preset</th><th>Entry</th><th>Take Profit</th><th>Safety Exit</th><th>Opened</th><th>Reason</th></tr>
        </thead>
        <tbody>{active_rows}</tbody>
      </table>
    </section>

    <section>
      <h2>Closed Paper Trades</h2>
      <table>
        <thead>
          <tr><th>Coin</th><th>Result</th><th>Preset</th><th>Entry</th><th>Exit</th><th>Grid</th><th>Net</th><th>Hold</th><th>Closed</th><th>Reason</th></tr>
        </thead>
        <tbody>{closed_rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>"""


def render_grid_dashboard(snapshot: dict[str, Any], refresh_seconds: int) -> str:
    grid_stats = snapshot.get("grid_stats", {})
    scanned_rows = "".join(_grid_scan_row(row) for row in grid_stats.get("scanned", [])) or _empty_row(6, "No GRID scan details loaded yet.")
    active_rows = "".join(_grid_active_row(row) for row in grid_stats.get("active_trades", [])) or _empty_row(8, "No active GRID paper trades.")
    closed_rows = "".join(_grid_closed_row(row) for row in grid_stats.get("closed_trades", [])) or _empty_row(9, "No closed GRID paper trades yet.")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{int(refresh_seconds)}">
  <title>GRID Paper Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0f1117;
      --panel: #171b24;
      --panel-2: #1f2530;
      --line: #2c3442;
      --text: #edf2f7;
      --muted: #9aa4b2;
      --good: #25d695;
      --bad: #ff6875;
      --accent: #4da3ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, Helvetica, sans-serif;
      line-height: 1.4;
    }}
    main {{
      width: min(1180px, calc(100vw - 32px));
      margin: 24px auto 40px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-end;
      margin-bottom: 18px;
    }}
    h1, h2 {{ margin: 0; }}
    h1 {{ font-size: 28px; }}
    h2 {{ font-size: 18px; margin-bottom: 10px; }}
    nav {{ display: flex; gap: 8px; margin: 0 0 18px; }}
    nav a {{
      color: var(--text);
      text-decoration: none;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 13px;
      font-weight: 700;
    }}
    nav a.active {{ background: var(--accent); color: #07111f; border-color: var(--accent); }}
    .muted {{ color: var(--muted); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}
    .card, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .card {{ padding: 14px; }}
    .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    .value {{ font-size: 24px; font-weight: 700; margin-top: 4px; }}
    .good {{ color: var(--good); }}
    .bad {{ color: var(--bad); }}
    section {{ padding: 16px; margin-top: 16px; overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 760px; }}
    th, td {{
      text-align: left;
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      white-space: nowrap;
      font-size: 14px;
    }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    tr:last-child td {{ border-bottom: 0; }}
    .pill {{
      display: inline-block;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--panel-2);
      color: var(--text);
      font-size: 12px;
    }}
    @media (max-width: 860px) {{
      header {{ display: block; }}
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 520px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>GRID Paper</h1>
        <div class="muted">Auto-refreshes every {int(refresh_seconds)} seconds. GRID exits are paper-only and do not send Telegram exit alerts.</div>
      </div>
      <div class="muted">Generated {_escape(snapshot["generated_at"])}</div>
    </header>
    <nav>
      <a href="/loop">LOOP Bots</a>
      <a class="active" href="/grid">GRID Bots</a>
    </nav>

    <div class="grid">
      {_metric("Entries", grid_stats.get("entries", 0), "GRID paper entries")}
      {_metric("Closed", grid_stats.get("closed", 0), "TP/SL paper closes")}
      {_metric("Win Rate", _pct(grid_stats.get("win_rate_pct", 0.0)), "TP / closed GRID")}
      {_metric("Net Return", _signed_pct(grid_stats.get("net_return_pct", 0.0)), "Paper TP/SL net", grid_stats.get("net_return_pct", 0.0))}
      {_metric("Avg Net/Trade", _signed_pct(grid_stats.get("avg_net_return_pct", 0.0)), "Closed GRID average", grid_stats.get("avg_net_return_pct", 0.0))}
      {_metric("Active", grid_stats.get("active", 0), "Open GRID paper")}
      {_metric("Wins", grid_stats.get("wins", 0), "GRID TP closes")}
      {_metric("Losses", grid_stats.get("losses", 0), "GRID SL closes")}
    </div>

    <section>
      <h2>Scanned GRID Setups</h2>
      <table>
        <thead>
          <tr><th>Coin</th><th>Score</th><th>Status</th><th>Price</th><th>Cooldown</th><th>Reason</th></tr>
        </thead>
        <tbody>{scanned_rows}</tbody>
      </table>
    </section>

    <section>
      <h2>Active GRID Paper</h2>
      <table>
        <thead>
          <tr><th>Coin</th><th>Preset</th><th>Entry</th><th>TP</th><th>SL</th><th>Grid Step</th><th>Levels</th><th>Opened</th></tr>
        </thead>
        <tbody>{active_rows}</tbody>
      </table>
    </section>

    <section>
      <h2>Closed GRID Paper</h2>
      <table>
        <thead>
          <tr><th>Coin</th><th>Result</th><th>Preset</th><th>Entry</th><th>Exit</th><th>Net</th><th>Grid Step</th><th>Closed</th><th>Reason</th></tr>
        </thead>
        <tbody>{closed_rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>"""


def _metric(label: str, value: Any, detail: str, signed_value: float | None = None) -> str:
    value_class = ""
    if signed_value is not None:
        value_class = " good" if signed_value >= 0 else " bad"
    return (
        '<div class="card">'
        f'<div class="label">{_escape(label)}</div>'
        f'<div class="value{value_class}">{_escape(value)}</div>'
        f'<div class="muted">{_escape(detail)}</div>'
        "</div>"
    )


def _active_row(row: dict[str, Any]) -> str:
    return (
        "<tr>"
        f"<td>{_escape(row['symbol'])}</td>"
        f"<td><span class=\"pill\">{_escape(row['preset'])}</span></td>"
        f"<td>{_price(row['entry_price'])}</td>"
        f"<td>{_price(row['take_profit_price'])}</td>"
        f"<td>{_price(row['safety_exit_price'])}</td>"
        f"<td>{_short_time(row.get('opened_at', ''))}</td>"
        f"<td>{_escape(row.get('reason', ''))}</td>"
        "</tr>"
    )


def _closed_row(row: dict[str, Any]) -> str:
    event = "Win" if row.get("event") == "TAKE_PROFIT" else "Loss"
    result_class = "good" if event == "Win" else "bad"
    net = float(row.get("net_return_pct", 0.0))
    return (
        "<tr>"
        f"<td>{_escape(row['symbol'])}</td>"
        f'<td class="{result_class}">{event}</td>'
        f"<td><span class=\"pill\">{_escape(row['preset'])}</span></td>"
        f"<td>{_price(row['entry_price'])}</td>"
        f"<td>{_price(row['exit_price'])}</td>"
        f"<td>{int(row.get('grid_cycles', 0))}</td>"
        f"<td class=\"{'good' if net >= 0 else 'bad'}\">{_signed_pct(net)}</td>"
        f"<td>{float(row.get('hold_hours', 0.0)):.2f}h</td>"
        f"<td>{_short_time(row.get('event_at', ''))}</td>"
        f"<td>{_escape(row.get('exit_reason') or row.get('reason') or '')}</td>"
        "</tr>"
    )


def _loop_scan_row(row: dict[str, Any]) -> str:
    score = row.get("score", "")
    score_text = f"{int(float(score))}/100" if score != "" else "scanned"
    quote_volume = float(row.get("quote_volume", 0.0) or 0.0)
    return (
        "<tr>"
        f"<td>{_escape(row.get('symbol', ''))}</td>"
        f"<td>{_escape(score_text)}</td>"
        f"<td>{_price(row.get('last_price', 0.0))}</td>"
        f"<td>{float(row.get('volatility_pct', 0.0) or 0.0):.2f}%</td>"
        f"<td>{float(row.get('change_pct', 0.0) or 0.0):+.2f}%</td>"
        f"<td>${quote_volume:,.0f}</td>"
        f"<td>{_escape(row.get('reason', 'scanned every cycle'))}</td>"
        "</tr>"
    )


def _grid_scan_row(row: dict[str, Any]) -> str:
    status = "Ready" if row.get("ready") else "Waiting"
    if row.get("active"):
        status = "Active paper"
    status_class = "good" if row.get("ready") else ""
    return (
        "<tr>"
        f"<td>{_escape(row.get('symbol', ''))}</td>"
        f"<td>{int(float(row.get('score', 0)))}/100</td>"
        f'<td class="{status_class}">{_escape(status)}</td>'
        f"<td>{_price(row.get('current_price', 0.0))}</td>"
        f"<td>{'yes' if row.get('cooldown') else 'no'}</td>"
        f"<td>{_escape(row.get('reason', ''))}</td>"
        "</tr>"
    )


def _grid_active_row(row: dict[str, Any]) -> str:
    return (
        "<tr>"
        f"<td>{_escape(row.get('symbol', ''))}</td>"
        f"<td><span class=\"pill\">{_escape(row.get('preset_name', ''))}</span></td>"
        f"<td>{_price(row.get('entry_price', 0.0))}</td>"
        f"<td>{_price(row.get('take_profit_price', 0.0))}</td>"
        f"<td>{_price(row.get('stop_loss_price', 0.0))}</td>"
        f"<td>{_escape(row.get('grid_step_pct', ''))}%</td>"
        f"<td>{_escape(row.get('levels', ''))}</td>"
        f"<td>{_short_time(row.get('opened_at', ''))}</td>"
        "</tr>"
    )


def _grid_closed_row(row: dict[str, Any]) -> str:
    event = "Win" if row.get("event") == "GRID_TAKE_PROFIT" else "Loss"
    result_class = "good" if event == "Win" else "bad"
    net = float(row.get("net_return_pct", 0.0))
    return (
        "<tr>"
        f"<td>{_escape(row.get('symbol', ''))}</td>"
        f'<td class="{result_class}">{event}</td>'
        f"<td><span class=\"pill\">{_escape(row.get('preset_name', ''))}</span></td>"
        f"<td>{_price(row.get('entry_price', 0.0))}</td>"
        f"<td>{_price(row.get('exit_price', 0.0))}</td>"
        f"<td class=\"{'good' if net >= 0 else 'bad'}\">{_signed_pct(net)}</td>"
        f"<td>{_escape(row.get('grid_step_pct', ''))}%</td>"
        f"<td>{_short_time(row.get('event_at', ''))}</td>"
        f"<td>{_escape(row.get('exit_reason', ''))}</td>"
        "</tr>"
    )


def _empty_row(colspan: int, message: str) -> str:
    return f'<tr><td colspan="{colspan}" class="muted">{_escape(message)}</td></tr>'


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _pct(value: float) -> str:
    return f"{float(value):.2f}%"


def _signed_pct(value: float) -> str:
    return f"{float(value):+.2f}%"


def _price(value: float) -> str:
    value = float(value or 0.0)
    if value >= 100:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:,.6f}"


def _short_time(value: str) -> str:
    return _escape(value.replace("+00:00", " UTC").replace("T", " ")[:22])
