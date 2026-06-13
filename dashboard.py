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
    ) -> None:
        self.tracker = tracker
        self.config = config
        self.grid_snapshot_provider = grid_snapshot_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    def snapshot(self) -> dict[str, Any]:
        payload = self.tracker.snapshot()
        if self.grid_snapshot_provider is not None:
            payload["grid_stats"] = self.grid_snapshot_provider()
        return payload

    def start(self) -> None:
        if not self.config.enabled or self._server is not None:
            return

        dashboard = self
        refresh_seconds = self.config.refresh_seconds

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/api/paper":
                    self._send_json(dashboard.snapshot())
                    return
                if self.path in {"/", "/paper"}:
                    self._send_html(render_dashboard(dashboard.snapshot(), refresh_seconds))
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


def render_dashboard(snapshot: dict[str, Any], refresh_seconds: int) -> str:
    window = snapshot["window_stats"]
    all_time = snapshot["all_stats"]
    grid_stats = snapshot.get("grid_stats", {})
    active_trades = snapshot["active_trades"]
    closed_trades = snapshot["closed_trades"]

    active_rows = "".join(_active_row(row) for row in active_trades) or _empty_row(7, "No active alerts.")
    closed_rows = "".join(_closed_row(row) for row in closed_trades) or _empty_row(10, "No closed paper trades yet.")

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
        <h1>Loopbots Paper Dashboard</h1>
        <div class="muted">Auto-refreshes every {int(refresh_seconds)} seconds. Data older than {snapshot["retention_days"]} days is pruned.</div>
      </div>
      <div class="muted">Generated {_escape(snapshot["generated_at"])}</div>
    </header>

    <div class="grid">
      {_metric("Closed", window["closed"], f"Last {snapshot['lookback_days']} days")}
      {_metric("Win Rate", _pct(window["win_rate_pct"]), "TP closes / closed alerts")}
      {_metric("Net Return", _signed_pct(window["net_return_pct"]), f"After {snapshot['fee_pct']:.2f}% fee estimate", window["net_return_pct"])}
      {_metric("Avg Net/Trade", _signed_pct(window["avg_net_return_pct"]), "Quality target", window["avg_net_return_pct"])}
      {_metric("Active", len(active_trades), "Open alerts")}
      {_metric("Avg Hold", f"{window['avg_hold_hours']:.2f}h", "Closed alerts")}
      {_metric("All-Time WR", _pct(all_time["win_rate_pct"]), "Retained history")}
      {_metric("All-Time Avg", _signed_pct(all_time["avg_net_return_pct"]), "Retained history", all_time["avg_net_return_pct"])}
      {_metric("GRID Closed", grid_stats.get("closed", 0), "Paper GRID trades")}
      {_metric("GRID WR", _pct(grid_stats.get("win_rate_pct", 0.0)), "TP / closed GRID")}
      {_metric("GRID Net", _signed_pct(grid_stats.get("net_return_pct", 0.0)), "Paper TP/SL net", grid_stats.get("net_return_pct", 0.0))}
      {_metric("GRID Active", grid_stats.get("active", 0), "Open GRID paper")}
    </div>

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


def _empty_row(colspan: int, message: str) -> str:
    return f'<tr><td colspan="{colspan}" class="muted">{_escape(message)}</td></tr>'


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _pct(value: float) -> str:
    return f"{float(value):.2f}%"


def _signed_pct(value: float) -> str:
    return f"{float(value):+.2f}%"


def _price(value: float) -> str:
    value = float(value)
    if value >= 100:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:,.6f}"


def _short_time(value: str) -> str:
    return _escape(value.replace("+00:00", " UTC").replace("T", " ")[:22])
