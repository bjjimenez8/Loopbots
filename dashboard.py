from __future__ import annotations

import html
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

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
        research_provider: Callable[[], dict[str, Any]] | None = None,
        opportunity_provider: Callable[[dict[str, list[str]]], dict[str, Any]] | None = None,
        backtest_provider: Callable[[dict[str, list[str]]], dict[str, Any]] | None = None,
        active_setup_provider: Callable[[], dict[str, Any]] | None = None,
        opportunity_paper_provider: Callable[[], dict[str, Any]] | None = None,
        use_setup_handler: Callable[[dict[str, list[str]]], None] | None = None,
        finish_setup_handler: Callable[[dict[str, list[str]]], None] | None = None,
    ) -> None:
        self.tracker = tracker
        self.config = config
        self.grid_snapshot_provider = grid_snapshot_provider
        self.loop_details_provider = loop_details_provider
        self.research_provider = research_provider
        self.opportunity_provider = opportunity_provider
        self.backtest_provider = backtest_provider
        self.active_setup_provider = active_setup_provider
        self.opportunity_paper_provider = opportunity_paper_provider
        self.use_setup_handler = use_setup_handler
        self.finish_setup_handler = finish_setup_handler
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    def snapshot(
        self,
        include_grid: bool = True,
        include_loop: bool = True,
        include_research: bool = False,
        opportunity_query: dict[str, list[str]] | None = None,
        backtest_query: dict[str, list[str]] | None = None,
        include_active_setups: bool = False,
        include_opportunity_paper: bool = False,
    ) -> dict[str, Any]:
        payload = self.tracker.snapshot()
        if include_grid and self.grid_snapshot_provider is not None:
            payload["grid_stats"] = self.grid_snapshot_provider()
        if include_loop and self.loop_details_provider is not None:
            payload["loop_details"] = self.loop_details_provider()
        if include_research and self.research_provider is not None:
            payload["research"] = self.research_provider()
        if opportunity_query is not None and self.opportunity_provider is not None:
            payload["opportunities"] = self.opportunity_provider(opportunity_query)
        if backtest_query is not None and self.backtest_provider is not None:
            payload["backtest"] = self.backtest_provider(backtest_query)
        if include_active_setups and self.active_setup_provider is not None:
            payload["active_setups"] = self.active_setup_provider()
        if include_opportunity_paper and self.opportunity_paper_provider is not None:
            payload["opportunity_paper"] = self.opportunity_paper_provider()
        return payload

    def start(self) -> None:
        if not self.config.enabled or self._server is not None:
            return

        dashboard = self
        refresh_seconds = self.config.refresh_seconds

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if path == "/api/paper":
                    self._send_json(dashboard.snapshot())
                    return
                if path == "/api/research":
                    self._send_json(dashboard.snapshot(include_grid=False, include_loop=False, include_research=True))
                    return
                if path == "/api/opportunities":
                    self._send_json(dashboard.snapshot(include_grid=False, include_loop=False, opportunity_query=query, include_active_setups=True, include_opportunity_paper=True))
                    return
                if path == "/api/active-setups":
                    self._send_json(dashboard.snapshot(include_grid=False, include_loop=False, include_active_setups=True))
                    return
                if path == "/api/opportunity-paper":
                    self._send_json(dashboard.snapshot(include_grid=False, include_loop=False, include_opportunity_paper=True))
                    return
                if path == "/api/backtest":
                    self._send_json(dashboard.snapshot(include_grid=False, include_loop=False, backtest_query=query))
                    return
                if path == "/opportunities":
                    self._send_html(render_opportunities_dashboard(dashboard.snapshot(include_grid=False, include_loop=False, opportunity_query=query, include_active_setups=True, include_opportunity_paper=True), refresh_seconds))
                    return
                if path in {"/", "/backtest"}:
                    self._send_html(render_backtest_dashboard(dashboard.snapshot(include_grid=False, include_loop=False, backtest_query=query), refresh_seconds))
                    return
                if path in {"/paper", "/loop"}:
                    self._send_html(render_loop_dashboard(dashboard.snapshot(include_grid=False), refresh_seconds))
                    return
                if path == "/grid":
                    self._send_html(render_grid_dashboard(dashboard.snapshot(include_loop=False), refresh_seconds))
                    return
                if path == "/research":
                    self._send_html(render_research_dashboard(dashboard.snapshot(include_grid=False, include_loop=False, include_research=True), refresh_seconds))
                    return
                self.send_error(404, "Not found")

            def do_POST(self) -> None:
                parsed = urlsplit(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)).decode("utf-8")
                form = parse_qs(body)
                if parsed.path == "/api/use-setup" and dashboard.use_setup_handler is not None:
                    dashboard.use_setup_handler(form)
                    self._redirect(_opportunities_redirect(form))
                    return
                if parsed.path == "/api/finish-setup" and dashboard.finish_setup_handler is not None:
                    dashboard.finish_setup_handler(form)
                    self._redirect(_opportunities_redirect(form))
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

            def _redirect(self, location: str) -> None:
                self.send_response(303)
                self.send_header("Location", location)
                self.end_headers()

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


def _opportunities_redirect(form: dict[str, list[str]]) -> str:
    if _form_value(form, "setup_id", ""):
        return "/opportunities#saved-bots"
    paper = _form_value(form, "paper", "")
    if paper in {"grid", "loop"}:
        return "/opportunities?" + urlencode({"paper": paper}) + "#paper-performance"
    return "/opportunities"


def _form_value(form: dict[str, list[str]], key: str, default: str) -> str:
    values = form.get(key)
    if not values:
        return default
    value = str(values[0]).strip().lower()
    return value or default


def render_loop_dashboard(snapshot: dict[str, Any], refresh_seconds: int) -> str:
    window = snapshot["window_stats"]
    all_time = snapshot["all_stats"]
    loop_details = snapshot.get("loop_details", {})
    active_trades = snapshot["active_trades"]
    closed_trades = snapshot["closed_trades"]

    active_rows = "".join(_active_row(row) for row in active_trades) or _empty_row(7, "No active alerts.")
    closed_rows = "".join(_closed_row(row) for row in closed_trades) or _empty_row(10, "No closed paper trades yet.")
    loop_entry_rows = loop_details.get("entry_rows", [])
    if loop_entry_rows:
        scanned_rows = "".join(_loop_scan_row(row) for row in loop_entry_rows)
    else:
        scanned_rows = "".join(_loop_scan_row({"symbol": symbol}) for symbol in loop_details.get("pairs", []))
    scanned_rows = scanned_rows or _empty_row(6, "No LOOP pairs loaded.")

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
    nav a, .mini-nav a {{
      color: var(--text);
      text-decoration: none;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 13px;
      font-weight: 700;
    }}
    nav a.active, .mini-nav a.active {{ background: var(--accent); color: #07111f; border-color: var(--accent); }}
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
      <a href="/opportunities">Opportunities</a>
      <a class="active" href="/loop">LOOP Bots</a>
      <a href="/grid">GRID Bots</a>
      <a href="/research">Research</a>
      <a href="/backtest">Backtest Lab</a>
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
      <h2>LOOP Entry Readiness</h2>
      <table>
        <thead>
          <tr><th>Coin</th><th>Entry Score</th><th>Status</th><th>Mode</th><th>Price</th><th>Reason</th></tr>
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
      <a href="/opportunities">Opportunities</a>
      <a href="/loop">LOOP Bots</a>
      <a class="active" href="/grid">GRID Bots</a>
      <a href="/research">Research</a>
      <a href="/backtest">Backtest Lab</a>
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
          <tr><th>Coin</th><th>Preset</th><th>Type</th><th>Score</th><th>Status</th><th>Price</th><th>Hist. Monthly</th><th>Cooldown</th><th>Reason</th></tr>
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


def _render_ready_right_now_dashboard(snapshot: dict[str, Any], payload: dict[str, Any]) -> str:
    opportunities = payload.get("opportunities", [])
    active_setups = snapshot.get("active_setups", {})
    generated_at = payload.get("generated_at", snapshot.get("generated_at", ""))
    ready = [row for row in opportunities if _deploy_action(row) != "AVOID"]
    grid_ready = [row for row in ready if str(row.get("strategy", "")).upper() == "GRID"]
    loop_ready = [row for row in ready if str(row.get("strategy", "")).upper() == "LOOP"]
    grid_cards = "".join(_ready_setup_card(row) for row in grid_ready) or _no_ready_card("GRID")
    loop_cards = "".join(_ready_setup_card(row) for row in loop_ready) or _no_ready_card("LOOP")
    active_cards = _existing_bot_action_cards(active_setups)
    filters = payload.get("filters", {})
    paper_cards = _opportunity_paper_cards(snapshot.get("opportunity_paper", {}), filters)
    quick_tabs = _opportunities_quick_tabs(filters)
    no_ready = ""
    if not grid_ready and not loop_ready:
        no_ready = '<div class="empty-ready">No ready coins right now. Next scan in 15 minutes.</div>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ready Right Now</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --line: #d9e2ec;
      --text: #111827;
      --muted: #64748b;
      --green: #079455;
      --green-bg: #ecfdf3;
      --orange: #b45309;
      --orange-bg: #fffbeb;
      --red: #c2410c;
      --red-bg: #fff7ed;
      --shadow: 0 12px 30px rgba(15, 23, 42, 0.06);
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
      margin: 28px auto 42px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      margin-bottom: 22px;
    }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 38px; line-height: 1.05; }}
    h2 {{ font-size: 22px; margin-bottom: 12px; }}
    h3 {{ font-size: 19px; }}
    .subtitle {{ color: var(--muted); margin-top: 8px; font-size: 16px; }}
    .top-actions {{ display: grid; gap: 8px; justify-items: end; }}
    .updated {{ color: var(--muted); font-size: 13px; }}
    .quick-tabs {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin: -8px 0 16px;
    }}
    .quick-tab {{
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #ffffff;
      color: var(--text);
      padding: 0 13px;
      text-decoration: none;
      font-size: 12px;
      font-weight: 900;
      box-shadow: 0 6px 16px rgba(15, 23, 42, 0.05);
    }}
    .quick-tab.active {{
      border-color: #12b76a;
      background: var(--green-bg);
      color: var(--green);
    }}
    .section-term-tabs {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin: 0 0 12px;
    }}
    .section-term-tab {{
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #f8fafc;
      color: var(--text);
      padding: 0 12px;
      text-decoration: none;
      font-size: 12px;
      font-weight: 900;
    }}
    .section-term-tab.active {{
      border-color: #12b76a;
      background: var(--green-bg);
      color: var(--green);
    }}
    .section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: var(--shadow);
      padding: 16px;
      margin-top: 16px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: var(--shadow);
    }}
    .section-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 10px;
    }}
    .section-title {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .section-meta {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      flex-shrink: 0;
    }}
    .top-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 26px;
      height: 26px;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--text);
      background: #ffffff;
      text-decoration: none;
      font-weight: 900;
      font-size: 13px;
      line-height: 1;
    }}
    .top-link:hover {{
      border-color: #12b76a;
      color: var(--green);
      background: var(--green-bg);
    }}
    .count-pill {{
      border-radius: 999px;
      padding: 5px 10px;
      background: #eef2f7;
      color: #334155;
      font-size: 12px;
      font-weight: 900;
    }}
    .ready-grid {{ display: grid; gap: 12px; }}
    .ready-card {{
      display: grid;
      grid-template-columns: 190px minmax(320px, 1fr) 190px;
      gap: 14px;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      background: #ffffff;
    }}
    .pair {{ font-size: 24px; font-weight: 900; }}
    .bot-type {{ color: var(--muted); font-size: 13px; margin-top: 3px; }}
    .score {{ font-size: 13px; font-weight: 900; margin-top: 10px; }}
    .reason {{ color: #334155; font-size: 13px; margin-top: 10px; }}
    .action-chip {{
      display: inline-flex;
      width: fit-content;
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 12px;
      font-weight: 900;
      margin-top: 10px;
    }}
    .deploy {{ color: var(--green); background: var(--green-bg); border: 1px solid #abefc6; }}
    .settings {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }}
    .field {{
      border: 1px solid var(--line);
      border-radius: 9px;
      background: #f8fafc;
      padding: 8px;
      min-height: 54px;
    }}
    .field .name {{ color: var(--muted); font-size: 11px; font-weight: 900; text-transform: uppercase; }}
    .field .value {{ color: var(--text); font-size: 14px; font-weight: 900; margin-top: 2px; overflow-wrap: anywhere; }}
    .buttons {{ display: grid; gap: 8px; align-content: center; }}
    .save-button, .remove-button {{
      min-height: 42px;
      border-radius: 8px;
      font-size: 12px;
      font-weight: 900;
      cursor: pointer;
      font-family: inherit;
    }}
    .save-button {{ border: 1px solid #067647; background: #079455; color: #ffffff; width: 100%; }}
    .remove-button {{ border: 1px solid #fda29b; background: #fef3f2; color: #b42318; width: 100%; margin-top: 8px; }}
    .empty-ready {{
      border: 1px dashed #cbd5e1;
      border-radius: 12px;
      background: #ffffff;
      color: var(--muted);
      padding: 22px;
      text-align: center;
      font-weight: 800;
      margin-top: 12px;
    }}
    .bot-actions {{ display: grid; gap: 10px; }}
    .bot-card {{
      display: grid;
      grid-template-columns: 190px 150px minmax(240px, 1fr);
      gap: 12px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
      background: #ffffff;
    }}
    .bot-action {{
      border-radius: 999px;
      padding: 7px 10px;
      text-align: center;
      font-weight: 900;
      font-size: 12px;
    }}
    .let-run {{ color: var(--green); background: var(--green-bg); }}
    .take-profit {{ color: #0369a1; background: #e0f2fe; }}
    .stop-bot {{ color: #b42318; background: #fef3f2; }}
    .good {{ color: var(--green); }}
    .bad {{ color: #b42318; }}
    .paper-section {{ margin-top: 16px; padding: 16px; }}
    .paper-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 12px;
    }}
    .paper-tabs {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }}
    .paper-tab {{
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #ffffff;
      color: var(--text);
      padding: 8px 13px;
      text-decoration: none;
      font-size: 12px;
      font-weight: 900;
    }}
    .paper-tab.active {{ border-color: #12b76a; background: var(--green-bg); color: var(--green); }}
    .paper-strategy-block {{
      border-top: 1px solid var(--line);
      padding-top: 14px;
      margin-top: 14px;
    }}
    .paper-strategy-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 12px;
    }}
    .paper-stats {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
    .paper-stat {{
      border: 1px solid var(--line);
      border-radius: 9px;
      background: #f8fafc;
      padding: 7px 9px;
      min-width: 104px;
    }}
    .paper-stat .name {{ color: var(--muted); font-size: 11px; font-weight: 900; }}
    .paper-stat .number {{ color: var(--text); font-size: 15px; font-weight: 900; margin-top: 1px; }}
    .paper-list {{ display: grid; gap: 10px; }}
    .paper-trade {{
      display: grid;
      grid-template-columns: 150px minmax(260px, 1fr) 140px;
      gap: 12px;
      align-items: start;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #ffffff;
      padding: 12px;
    }}
    .paper-title {{ font-size: 17px; font-weight: 900; }}
    .strategy-chip {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 11px;
      font-weight: 900;
      margin-top: 6px;
    }}
    .strategy-chip.grid {{ color: #065f46; background: #d1fae5; border: 1px solid #a7f3d0; }}
    .strategy-chip.loop {{ color: #3730a3; background: #e0e7ff; border: 1px solid #c7d2fe; }}
    .paper-result {{ font-size: 20px; font-weight: 900; text-align: right; }}
    .paper-detail {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
      margin-top: 8px;
    }}
    .paper-detail div {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f8fafc;
      padding: 6px 7px;
      color: var(--muted);
      font-size: 12px;
    }}
    .paper-detail strong {{ color: var(--text); }}
    .risk-note {{ color: var(--muted); font-size: 12px; margin-top: 18px; }}
    @media (max-width: 900px) {{
      header {{ display: block; }}
      .top-actions {{ justify-items: start; margin-top: 14px; }}
      .quick-tabs {{ margin-top: 0; }}
      .ready-card, .bot-card, .paper-trade {{ grid-template-columns: 1fr; }}
      .settings, .paper-detail {{ grid-template-columns: 1fr; }}
      .paper-result {{ text-align: left; }}
      .paper-head, .paper-strategy-head {{ display: block; }}
      .paper-stats {{ justify-content: flex-start; margin-top: 10px; }}
    }}
  </style>
</head>
<body>
  <main id="top">
    <header>
      <div>
        <h1>Ready Right Now</h1>
        <div class="subtitle">Kraken-only manual Bitsgap GRID and LOOP setups. Copy the settings and manage them manually in Bitsgap.</div>
      </div>
      <div class="top-actions">
        <div class="updated">Updated {_escape(_short_time(generated_at))}</div>
      </div>
    </header>
    {quick_tabs}
    {no_ready}
    <section id="grid-ready" class="section">
      <div class="section-head"><div class="section-title"><h2>GRID Bots Ready Now</h2></div><div class="section-meta"><span class="count-pill">{len(grid_ready)} ready</span><a class="top-link" href="/opportunities#top" aria-label="Back to top">↑</a></div></div>
      {_section_horizon_tabs("grid-ready", filters)}
      <div class="ready-grid">{grid_cards}</div>
    </section>
    <section id="loop-ready" class="section">
      <div class="section-head"><div class="section-title"><h2>LOOP Bots Ready Now</h2></div><div class="section-meta"><span class="count-pill">{len(loop_ready)} ready</span><a class="top-link" href="/opportunities#top" aria-label="Back to top">↑</a></div></div>
      {_section_horizon_tabs("loop-ready", filters)}
      <div class="ready-grid">{loop_cards}</div>
    </section>
    {paper_cards}
    <section id="saved-bots" class="section">
      <div class="section-head"><div class="section-title"><h2>Existing Bot Actions</h2></div><div class="section-meta"><span class="count-pill">manual tracking</span><a class="top-link" href="/opportunities#top" aria-label="Back to top">↑</a></div></div>
      <div class="bot-actions">{active_cards}</div>
    </section>
    <div class="risk-note">Dashboard only. No trading API and no automatic Bitsgap actions. You manually enter and manage settings in Bitsgap.</div>
  </main>
  <script>
    if (!window.location.hash) {{
      window.scrollTo(0, 0);
      if ("scrollRestoration" in history) history.scrollRestoration = "manual";
    }}
  </script>
</body>
</html>"""


def _ready_setup_card(row: dict[str, Any]) -> str:
    pair = str(row.get("pair", ""))
    strategy = str(row.get("strategy", "")).upper()
    score = int(float(row.get("score", 0) or 0))
    action = _deploy_action(row)
    fields = _ready_settings(row)
    settings_html = "".join(_ready_field(name, value) for name, value in fields)
    return (
        '<article class="ready-card">'
        '<div>'
        f'<div class="pair">{_escape(pair)}</div>'
        f'<div class="bot-type">{_escape(strategy)} Bot &bull; Kraken</div>'
        f'<div class="score">Score {score}/100</div>'
        f'<div class="action-chip deploy">{_escape(action)}</div>'
        f'<div class="reason">{_escape(_short_ready_reason(row))}</div>'
        "</div>"
        f'<div class="settings">{settings_html}</div>'
        '<div class="buttons">'
        '<form method="post" action="/api/use-setup">'
        f'<input type="hidden" name="opportunity_id" value="{_escape(row.get("id", ""))}">'
        '<input type="hidden" name="strategy" value="both">'
        '<button class="save-button" type="submit">SAVE BOT</button>'
        "</form>"
        "</div>"
        "</article>"
    )


def _ready_field(name: str, value: Any) -> str:
    return (
        '<div class="field">'
        f'<div class="name">{_escape(name)}</div>'
        f'<div class="value">{_escape(value)}</div>'
        "</div>"
    )


def _ready_settings(row: dict[str, Any]) -> list[tuple[str, Any]]:
    fields = row.get("bitsgap_fields", {})
    if not isinstance(fields, dict):
        fields = {}
    strategy = str(row.get("strategy", "")).upper()
    term = _bitsgap_term(row)
    if strategy == "GRID":
        return [
            ("Bitsgap preset", term),
            ("Low price", fields.get("Low price", "n/a")),
            ("High price", fields.get("High price", "n/a")),
            ("Grid levels", fields.get("Grid levels", "n/a")),
            ("Grid step", fields.get("Grid step", "n/a")),
            ("Take profit", fields.get("Take profit", "n/a")),
            ("Stop loss", fields.get("Stop loss", "n/a")),
            ("Trailing up", "On"),
            ("Trailing down", "Off"),
            ("Pump protection", "On"),
        ]
    entry = _entry_display(row)
    safety = fields.get("Safety exit / stop guidance", "n/a")
    take_profit = fields.get("Take profit", "n/a")
    return [
        ("Bitsgap preset", term),
        ("Entry price", entry),
        ("Order distance", fields.get("Order distance", "n/a")),
        ("Order count", fields.get("Order count", "n/a")),
        ("Low/high range", row.get("entry_zone", "n/a")),
        ("Take profit", _loop_take_profit_text(entry, take_profit)),
        ("Safety exit", safety),
        ("Stop loss", _loop_stop_loss_text(entry, safety)),
    ]


def _bitsgap_term(row: dict[str, Any]) -> str:
    speed = str(row.get("speed", "") or "").lower()
    if speed == "fast":
        return "Short-term"
    if speed == "slow":
        return "Long-term"
    return "Mid-term"


def _copy_settings_text(pair: str, strategy: str, action: str, fields: list[tuple[str, Any]], reason: Any) -> str:
    lines = [f"{pair} {strategy} Bot", f"Action: {action}", "Exchange: Kraken"]
    lines.extend(f"{name}: {value}" for name, value in fields)
    lines.append(f"Reason: {_short_text(reason, 120)}")
    return "\n".join(lines)


def _deploy_action(row: dict[str, Any]) -> str:
    if not _has_copy_ready_settings(row):
        return "AVOID"
    score = int(float(row.get("score", 0) or 0))
    strategy = str(row.get("strategy", "")).upper()
    if score >= 70:
        return "READY TO BE DEPLOYED"
    if strategy == "LOOP" and score >= 35:
        return "READY TO BE DEPLOYED"
    if strategy == "GRID" and score >= 30:
        return "READY TO BE DEPLOYED"
    return "AVOID"


def _has_copy_ready_settings(row: dict[str, Any]) -> bool:
    fields = row.get("bitsgap_fields", {})
    if not isinstance(fields, dict):
        return False
    strategy = str(row.get("strategy", "")).upper()
    if strategy == "GRID":
        required = ["Low price", "High price", "Grid levels", "Grid step", "Take profit", "Stop loss"]
    else:
        required = ["Order distance", "Order count", "Take profit", "Safety exit / stop guidance"]
    for key in required:
        value = fields.get(key)
        if value in {"", None, "n/a", "Needs live price"}:
            return False
    return True


def _short_ready_reason(row: dict[str, Any]) -> str:
    strategy = str(row.get("strategy", "")).upper()
    score = int(float(row.get("score", 0) or 0))
    term = _bitsgap_term(row).lower()
    fields = row.get("bitsgap_fields", {})
    if not isinstance(fields, dict):
        fields = {}
    if strategy == "GRID":
        return _grid_ready_reason(score, term, fields)
    return _loop_ready_reason(score, term, fields)


def _grid_ready_reason(score: int, term: str, fields: dict[str, Any]) -> str:
    grid_step = str(fields.get("Grid step", "") or "").strip()
    stop_loss = str(fields.get("Stop loss", "") or "").strip()
    take_profit = str(fields.get("Take profit", "") or "").strip()
    if score >= 85:
        return f"Strong {term} range. {grid_step} steps with TP and SL already mapped."
    if score >= 70:
        return f"Clean {term} grid range. Price has room before the TP area."
    if "at" in stop_loss:
        return f"Usable {term} range. Keep the mapped stop active if the range breaks."
    if "at" in take_profit:
        return f"Range setup is usable. TP is mapped, but keep size controlled."
    return f"Usable {term} grid setup with copy-ready range settings."


def _loop_ready_reason(score: int, term: str, fields: dict[str, Any]) -> str:
    order_distance = str(fields.get("Order distance", "") or "").strip()
    take_profit = str(fields.get("Take profit", "") or "").strip()
    safety = str(fields.get("Safety exit / stop guidance", "") or "").strip()
    if score >= 90:
        return f"Strong {term} bounce. TP has room and the safety exit is defined."
    if score >= 75:
        return f"Good {term} pullback. {order_distance} spacing gives the LOOP room to work."
    if "at" in take_profit:
        return f"Usable {term} LOOP. TP is mapped; respect the safety exit."
    if safety and safety != "n/a":
        return f"Bounce is starting, with safety exit already mapped."
    return f"Usable {term} LOOP setup with copy-ready entry settings."


def _entry_display(row: dict[str, Any]) -> str:
    market = row.get("market_snapshot", {})
    if isinstance(market, dict) and market.get("current_price") not in {"", None}:
        return _fmt_active_price(market.get("current_price"))
    return str(row.get("entry_zone", "n/a"))


def _loop_stop_loss_text(entry: Any, safety: Any) -> str:
    entry_price = _first_number(entry)
    safety_price = _first_number(safety)
    if entry_price and safety_price and entry_price > 0:
        return f"{((entry_price - safety_price) / entry_price) * 100:.2f}% at {_fmt_active_price(safety_price)}"
    return "Use safety exit"


def _loop_take_profit_text(entry: Any, take_profit: Any) -> str:
    entry_price = _first_number(entry)
    take_profit_price = _first_number(take_profit)
    if entry_price and take_profit_price and entry_price > 0:
        return f"+{((take_profit_price / entry_price) - 1) * 100:.2f}% at {_fmt_active_price(take_profit_price)}"
    return str(take_profit or "n/a")


def _first_number(value: Any) -> float | None:
    import re

    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _short_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _no_ready_card(strategy: str) -> str:
    return f'<div class="empty-ready">No {strategy} bots ready right now. Next scan in 15 minutes.</div>'


def _existing_bot_action_cards(active_setups: dict[str, Any]) -> str:
    active = active_setups.get("active", []) if isinstance(active_setups, dict) else []
    if not active:
        return '<div class="empty-ready">No saved bots being tracked right now.</div>'
    return "".join(_existing_bot_action_card(setup) for setup in active)


def _existing_bot_action_card(setup: dict[str, Any]) -> str:
    action = _saved_bot_action(setup)
    css = "stop-bot" if action.startswith("REMOVE ") else {"LET RUN": "let-run", "TAKE PROFIT": "take-profit"}.get(action, "let-run")
    pair = str(setup.get("pair", ""))
    strategy = str(setup.get("strategy", "")).upper()
    current = _fmt_active_price(setup.get("current_price"))
    profit = _signed_pct(float(setup.get("profit_pct", 0.0) or 0.0))
    remove_label = _remove_bot_label(setup)
    return (
        '<article class="bot-card">'
        f'<div><div class="pair" style="font-size:18px;">{_escape(pair)}</div><div class="bot-type">{_escape(strategy)} saved bot</div></div>'
        f'<div class="bot-action {css}">{_escape(action)}</div>'
        '<div>'
        f'<div class="field"><div class="name">Now / PnL</div><div class="value">{_escape(current)} &bull; {profit}</div></div>'
        '<form method="post" action="/api/finish-setup">'
        f'<input type="hidden" name="setup_id" value="{_escape(setup.get("id", ""))}">'
        '<input type="hidden" name="strategy" value="both">'
        f'<button class="remove-button" type="submit">{_escape(remove_label)}</button>'
        '</form>'
        '</div>'
        "</article>"
    )


def _saved_bot_action(setup: dict[str, Any]) -> str:
    action = str(setup.get("recommended_action", "HOLD")).upper()
    if action == "TAKE_PROFIT":
        return "TAKE PROFIT"
    if action == "EXIT":
        return _remove_bot_label(setup)
    return "LET RUN"


def _remove_bot_label(setup: dict[str, Any]) -> str:
    strategy = str(setup.get("strategy", "")).upper()
    if strategy == "GRID":
        return "REMOVE GRID BOT"
    if strategy == "LOOP":
        return "REMOVE LOOP BOT"
    return "REMOVE BOT"


def render_opportunities_dashboard(snapshot: dict[str, Any], refresh_seconds: int) -> str:
    payload = snapshot.get("opportunities", {})
    return _render_ready_right_now_dashboard(snapshot, payload)
    opportunities = payload.get("opportunities", [])
    filters = payload.get("filters", {})
    counts = payload.get("counts", {})
    active_setups = snapshot.get("active_setups", {})
    opportunity_paper = snapshot.get("opportunity_paper", {})
    usable = [row for row in opportunities if row.get("status") == "Ready Now"]
    visible_opportunities = usable[:6]
    cards = "".join(_opportunity_card(row, filters) for row in visible_opportunities) or '<div class="empty-card">No usable setup right now. Try the other strategy or time horizon.</div>'
    active_cards = _active_setup_cards(active_setups, filters)
    paper_cards = _opportunity_paper_cards(opportunity_paper, filters)
    ready_count = int(counts.get("ready_now", 0) or 0)
    wait_count = int(counts.get("wait", 0) or 0)
    avoid_count = int(counts.get("avoid", 0) or 0)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bitsgap Opportunities</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f9fc;
      --panel: #ffffff;
      --panel-2: #f9fafb;
      --line: #e2e8f0;
      --text: #101828;
      --muted: #667085;
      --good: #079455;
      --bad: #d92d20;
      --warn: #dc6803;
      --accent: #0f9f5f;
      --blue: #475467;
      --shadow: 0 12px 30px rgba(16, 24, 40, 0.06);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, Helvetica, sans-serif;
      line-height: 1.4;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 16px;
      margin-bottom: 32px;
    }}
    .brand-mark {{
      width: 46px;
      height: 46px;
      border-radius: 50%;
      border: 5px solid var(--accent);
      display: grid;
      place-items: center;
      color: var(--accent);
      font-size: 22px;
      font-weight: 900;
    }}
    .brand-name {{ font-size: 24px; font-weight: 900; letter-spacing: 0; }}
    .brand-sub {{ font-size: 13px; color: var(--text); margin-top: -2px; }}
    main {{
      width: min(1040px, calc(100vw - 56px));
      margin: 34px auto 44px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      margin-bottom: 28px;
    }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 31px; line-height: 1.15; }}
    h2 {{ font-size: 18px; }}
    .subtitle {{ margin-top: 8px; color: #101828; font-size: 17px; }}
    .updated {{
      display: flex;
      gap: 9px;
      align-items: center;
      color: #101828;
      white-space: nowrap;
      font-size: 14px;
    }}
    .dot {{ width: 8px; height: 8px; border-radius: 999px; background: var(--accent); display: inline-block; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: var(--shadow);
    }}
    .filter-panel {{
      display: grid;
      grid-template-columns: 1fr 1.45fr 0.8fr;
      gap: 18px;
      padding: 18px;
      align-items: stretch;
      margin-bottom: 14px;
    }}
    .filter-group {{
      border-right: 1px solid var(--line);
      padding-right: 18px;
    }}
    .filter-group:last-child {{ border-right: 0; padding-right: 0; }}
    .filter-title {{
      font-size: 14px;
      font-weight: 900;
      margin-bottom: 12px;
    }}
    .tile-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }}
    .tile-grid.three {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .filter-tile {{
      min-height: 86px;
      border: 1px solid var(--line);
      border-radius: 9px;
      display: grid;
      place-items: center;
      gap: 5px;
      color: #344054;
      text-decoration: none;
      font-weight: 800;
      font-size: 11px;
      text-align: center;
      padding: 10px 8px;
      min-width: 0;
    }}
    .filter-tile span {{
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .filter-tile.active {{
      border-color: #12b76a;
      background: #ecfdf3;
      color: #067647;
    }}
    .filter-icon {{ font-size: 23px; line-height: 1; }}
    .tile-sub {{ color: var(--muted); font-size: 11px; font-weight: 700; }}
    .exchange-card {{
      min-height: 86px;
      border: 1px solid var(--line);
      border-radius: 9px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 14px;
      font-weight: 900;
    }}
    .exchange-logo {{
      width: 32px;
      height: 32px;
      border-radius: 999px;
      background: #3f37ff;
      color: white;
      display: grid;
      place-items: center;
      font-size: 14px;
      font-weight: 900;
    }}
    .muted {{ color: var(--muted); }}
    .good {{ color: var(--good); }}
    .bad {{ color: var(--bad); }}
    .warn {{ color: var(--warn); }}
    .status-badge {{
      display: inline-block;
      padding: 4px 9px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
      border: 1px solid transparent;
    }}
    .status-ready {{
      color: var(--good);
      background: #ecfdf3;
      border-color: #abefc6;
    }}
    .status-wait {{
      color: var(--warn);
      background: #fffaeb;
      border-color: #fedf89;
    }}
    .status-avoid {{
      color: var(--bad);
      background: #fef3f2;
      border-color: #fecdca;
    }}
    .list-toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      color: #344054;
      margin: 18px 4px 14px;
      font-size: 14px;
    }}
    .opportunity-list {{ display: grid; gap: 14px; }}
    .opportunity-card {{
      display: grid;
      grid-template-columns: 220px minmax(320px, 1fr) 170px;
      gap: 18px;
      padding: 12px;
      align-items: stretch;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: 0 10px 24px rgba(16, 24, 40, 0.05);
    }}
    .coin-cell {{
      display: grid;
      gap: 10px;
      align-items: start;
      padding: 10px 0 10px 4px;
    }}
    .pair {{ font-size: 24px; font-weight: 900; color: var(--text); }}
    .subline {{ color: #475467; font-size: 13px; margin-top: 3px; }}
    .meta-row {{ display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }}
    .pill {{
      display: inline-block;
      padding: 4px 9px;
      border-radius: 999px;
      background: #f2f4f7;
      color: var(--blue);
      font-size: 12px;
      font-weight: 700;
    }}
    .settings-cell {{
      border-left: 1px solid var(--line);
      padding-left: 18px;
      display: grid;
      align-content: center;
      gap: 12px;
    }}
    .detail-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .detail-label {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .detail-value {{
      color: var(--text);
      font-size: 17px;
      font-weight: 900;
      margin-top: 2px;
      white-space: normal;
    }}
    .market-panel {{
      display: grid;
      grid-template-columns: 170px minmax(180px, 1fr);
      gap: 12px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
      padding: 11px 12px;
    }}
    .market-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 3px;
    }}
    .live-badge {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      border-radius: 999px;
      background: #ecfdf3;
      color: var(--good);
      border: 1px solid #abefc6;
      padding: 2px 7px;
      font-size: 10px;
      font-weight: 900;
      white-space: nowrap;
      animation: livePulse 2.8s ease-in-out infinite;
    }}
    .live-badge::before {{
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 999px;
      background: var(--good);
    }}
    @keyframes livePulse {{
      0%, 100% {{ opacity: 1; box-shadow: 0 0 0 0 rgba(7, 148, 85, 0.18); }}
      50% {{ opacity: 0.72; box-shadow: 0 0 0 5px rgba(7, 148, 85, 0); }}
    }}
    .market-price {{ font-size: 24px; font-weight: 900; color: var(--text); line-height: 1.05; }}
    .market-meta {{ color: var(--muted); font-size: 12px; margin-top: 2px; }}
    .sparkline {{
      width: 100%;
      height: 54px;
      overflow: visible;
    }}
    .sparkline-line {{
      fill: none;
      stroke-width: 3;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}
    .fields {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      white-space: normal;
    }}
    .fields div {{
      background: #f9fafb;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--muted);
      padding: 5px 7px;
      font-size: 12px;
    }}
    .fields strong {{ color: var(--text); }}
    .help-icon {{
      position: relative;
      display: inline-grid;
      place-items: center;
      width: 13px;
      height: 13px;
      margin-left: 3px;
      border-radius: 999px;
      background: #eef4ff;
      color: #344054;
      font-size: 9px;
      font-weight: 900;
      cursor: help;
      vertical-align: super;
      line-height: 1;
      top: -1px;
    }}
    .help-icon:hover::after {{
      content: attr(data-tip);
      position: absolute;
      left: 50%;
      bottom: calc(100% + 8px);
      transform: translateX(-50%);
      width: min(280px, 70vw);
      padding: 9px 10px;
      border-radius: 8px;
      background: #101828;
      color: #ffffff;
      box-shadow: 0 12px 28px rgba(16, 24, 40, 0.22);
      font-size: 12px;
      font-weight: 700;
      line-height: 1.35;
      text-align: left;
      z-index: 20;
    }}
    .setup-reason {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    .action-box {{
      border-radius: 12px;
      border: 1px solid var(--line);
      padding: 14px 12px;
      display: grid;
      align-content: center;
      gap: 10px;
      text-align: left;
    }}
    .action-ready {{ background: #ecfdf3; border-color: #abefc6; }}
    .action-wait {{ background: #fffaeb; border-color: #fedf89; }}
    .action-avoid {{ background: #fef3f2; border-color: #fecdca; }}
    .action-title {{ font-size: 15px; font-weight: 900; }}
    .action-ready .action-title {{ color: var(--good); }}
    .action-wait .action-title {{ color: var(--warn); }}
    .action-avoid .action-title {{ color: var(--bad); }}
    .confidence {{ font-size: 13px; color: var(--muted); }}
    .confidence strong {{ display: block; color: var(--text); margin-top: 2px; }}
    .action-button {{
      border: 1px solid transparent;
      border-radius: 8px;
      min-height: 48px;
      padding: 13px 14px;
      font-size: 13px;
      font-weight: 900;
      letter-spacing: 0;
      text-align: center;
      width: 100%;
      cursor: pointer;
      position: relative;
      z-index: 2;
      touch-action: manipulation;
    }}
    button.action-button {{ font-family: inherit; }}
    .status-action {{
      border-radius: 8px;
      min-height: 48px;
      padding: 13px 14px;
      display: grid;
      place-items: center;
      text-align: center;
      font-size: 13px;
      font-weight: 900;
      line-height: 1.2;
    }}
    .action-note {{
      color: #067647;
      font-size: 12px;
      font-weight: 900;
      line-height: 1.35;
      text-align: center;
    }}
    .action-ready .action-button {{ background: var(--good); color: #ffffff; }}
    .action-wait .status-action {{ background: transparent; color: var(--warn); border: 1px solid #f79009; }}
    .action-avoid .status-action {{ background: transparent; color: var(--bad); border: 1px solid #f04438; }}
    .how-card {{ margin-top: 18px; padding: 18px; }}
    .active-section {{ margin-top: 20px; }}
    .active-section h2 {{ margin: 0 0 12px; font-size: 18px; }}
    .active-card {{
      display: grid;
      grid-template-columns: 190px 150px minmax(260px, 1fr) 130px;
      gap: 12px;
      align-items: center;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel);
      box-shadow: 0 10px 24px rgba(16, 24, 40, 0.04);
      margin-bottom: 10px;
    }}
    .active-action {{ font-weight: 900; color: var(--good); }}
    .finish-button {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      color: var(--text);
      padding: 9px 10px;
      font-weight: 900;
      cursor: pointer;
      width: 100%;
    }}
    .paper-section {{ margin-top: 20px; padding: 16px; }}
    .paper-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 12px;
    }}
    .paper-stats {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
    .paper-stat {{
      border: 1px solid var(--line);
      border-radius: 9px;
      background: #f9fafb;
      padding: 7px 9px;
      min-width: 92px;
    }}
    .paper-stat .name {{ color: var(--muted); font-size: 11px; font-weight: 800; }}
    .paper-stat .number {{ color: var(--text); font-size: 15px; font-weight: 900; margin-top: 1px; }}
    .paper-strategy-block {{
      border-top: 1px solid var(--line);
      padding-top: 14px;
      margin-top: 14px;
    }}
    .paper-strategy-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 12px;
    }}
    .paper-strategy-head h3 {{
      font-size: 20px;
      font-weight: 900;
      margin: 0;
    }}
    .paper-tabs {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 10px;
    }}
    .paper-tab {{
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #ffffff;
      color: var(--text);
      padding: 8px 13px;
      text-decoration: none;
      font-size: 12px;
      font-weight: 900;
    }}
    .paper-tab.active {{
      border-color: #12b76a;
      background: #ecfdf3;
      color: #067647;
    }}
    .paper-split {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin: 10px 0 14px;
    }}
    .paper-split-card {{
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #f9fafb;
      padding: 8px 10px;
      color: var(--text);
      font-size: 12px;
      font-weight: 800;
    }}
    .paper-split-card strong {{ font-size: 13px; }}
    .paper-list {{ display: grid; gap: 10px; }}
    .paper-trade {{
      display: grid;
      grid-template-columns: 150px minmax(260px, 1fr) 140px;
      gap: 12px;
      align-items: start;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #ffffff;
      padding: 12px;
    }}
    .paper-title {{ font-size: 17px; font-weight: 900; }}
    .strategy-chip {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 11px;
      font-weight: 900;
      margin-top: 6px;
    }}
    .strategy-chip.grid {{ color: #065f46; background: #d1fae5; border: 1px solid #a7f3d0; }}
    .strategy-chip.loop {{ color: #3730a3; background: #e0e7ff; border: 1px solid #c7d2fe; }}
    .paper-result {{ font-size: 20px; font-weight: 900; text-align: right; }}
    .paper-detail {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
      margin-top: 8px;
    }}
    .paper-detail div {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f9fafb;
      padding: 6px 7px;
      color: var(--muted);
      font-size: 12px;
    }}
    .paper-detail strong {{ color: var(--text); }}
    .empty-card {{
      padding: 28px;
      color: var(--muted);
      text-align: center;
    }}
    .risk-note {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 18px;
    }}
    @media (max-width: 1100px) {{
      main {{ width: min(1040px, calc(100vw - 28px)); }}
      .filter-panel {{ grid-template-columns: 1fr 1fr; }}
      .filter-group {{ border-right: 0; padding-right: 0; }}
      .opportunity-card {{ grid-template-columns: 1fr; }}
      .active-card {{ grid-template-columns: 1fr; }}
      .paper-trade {{ grid-template-columns: 1fr; }}
      .paper-result {{ text-align: left; }}
      .market-panel {{ grid-template-columns: 1fr; }}
      .settings-cell {{ border-left: 0; border-top: 1px solid var(--line); padding-left: 0; padding-top: 14px; }}
    }}
    @media (max-width: 720px) {{
      header {{ display: block; }}
      .filter-panel {{ grid-template-columns: 1fr; }}
      .tile-grid, .tile-grid.three {{ grid-template-columns: 1fr; }}
      .opportunity-card {{ grid-template-columns: 1fr; }}
      .fields {{ grid-template-columns: 1fr; }}
      .paper-head {{ display: block; }}
      .paper-strategy-head {{ display: block; }}
      .paper-stats {{ justify-content: flex-start; margin-top: 10px; }}
      .paper-detail {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
    <main>
      <header>
        <div>
          <div class="brand" style="margin-bottom:18px;">
            <div class="brand-mark">G</div>
            <div>
              <div class="brand-name">GridPilot</div>
              <div class="brand-sub">for Bitsgap</div>
            </div>
          </div>
          <h1>What Should I Deploy Today?</h1>
          <div class="subtitle">Pick a bot type and time horizon. The app calculates the setup.</div>
        </div>
        <div class="updated"><span class="dot"></span> Updated {_escape(_short_time(payload.get("generated_at", snapshot.get("generated_at", ""))))} <span style="color:#344054;">&#8635;</span></div>
      </header>
      <section class="panel filter-panel">
        <div class="filter-group">
          <div class="filter-title">Strategy</div>
          <div class="tile-grid">
            {_op_filter_tile(filters, "strategy", "grid", "Grid", "&#9638;", "Grid bot")}
            {_op_filter_tile(filters, "strategy", "loop", "Loop", "&#8734;", "Loop bot")}
          </div>
        </div>
        <div class="filter-group">
          <div class="filter-title">Time Horizon</div>
          <div class="tile-grid three">
            {_op_filter_tile(filters, "horizon", "short", "Short-term", "&#9889;", "Days - 2 weeks")}
            {_op_filter_tile(filters, "horizon", "mid", "Mid-term", "&#128337;", "1-3 weeks")}
            {_op_filter_tile(filters, "horizon", "long", "Long-term", "&#128034;", "2-8 weeks")}
          </div>
        </div>
        <div class="filter-group">
          <div class="filter-title">Exchange</div>
          <div class="exchange-card"><span><span class="exchange-logo">K</span></span><span>Kraken</span><span class="status-badge status-ready">&#10003;</span></div>
        </div>
      </section>

      <div class="list-toolbar">
        <div>Showing {len(visible_opportunities)} usable setup{"" if len(visible_opportunities) == 1 else "s"}</div>
      </div>
      <div class="opportunity-list">{cards}</div>
      {active_cards}
      {paper_cards}
      <div class="risk-note">&#128737; Trading bots involve risk. Past performance is not predictive of future results.</div>
    </main>
</body>
</html>"""


def render_research_dashboard(snapshot: dict[str, Any], refresh_seconds: int) -> str:
    research = snapshot.get("research", {})
    loop = research.get("loop", {})
    grid = research.get("grid", {})
    loop_paper = loop.get("paper", {})
    grid_paper = grid.get("paper", {})

    loop_live_rows = "".join(_research_loop_live_row(row) for row in loop.get("top_live", [])) or _empty_row(7, "No LOOP scan results loaded yet.")
    grid_live_rows = "".join(_research_grid_live_row(row) for row in grid.get("top_live", [])) or _empty_row(8, "No GRID scan results loaded yet.")
    loop_proof_rows = "".join(_research_loop_proof_row(row) for row in loop.get("proof", [])) or _empty_row(6, "No LOOP proof rows loaded.")
    grid_proof_rows = "".join(_research_grid_proof_row(row) for row in grid.get("proof", [])) or _empty_row(8, "No GRID proof rows loaded.")

    loop_ready = int(loop.get("ready_count", 0))
    grid_ready = int(grid.get("ready_count", 0))
    loop_scanned = int(loop.get("scanned_count", 0))
    grid_scanned = int(grid.get("scanned_count", 0))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{int(refresh_seconds)}">
  <title>Loopbots Research</title>
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
      --warn: #ffd166;
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
      width: min(1240px, calc(100vw - 32px));
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
    nav {{ display: flex; gap: 8px; margin: 0 0 18px; flex-wrap: wrap; }}
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
    .warn {{ color: var(--warn); }}
    section {{ padding: 16px; margin-top: 16px; overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 880px; }}
    th, td {{
      text-align: left;
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      white-space: nowrap;
      font-size: 14px;
      vertical-align: middle;
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
    .score {{
      display: grid;
      grid-template-columns: 52px 120px;
      align-items: center;
      gap: 8px;
    }}
    .bar {{
      height: 8px;
      border-radius: 999px;
      overflow: hidden;
      background: #303848;
    }}
    .bar span {{
      display: block;
      height: 100%;
      background: var(--accent);
      width: var(--w);
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
        <h1>Bot Research</h1>
        <div class="muted">LOOP scans Kraken { _escape(loop.get("quote_asset", "USDT")) } on { _escape(loop.get("timeframe", "")) }; GRID scans Kraken { _escape(", ".join(grid.get("quote_assets", []))) } on { _escape(grid.get("timeframe", "")) }.</div>
      </div>
      <div class="muted">Generated {_escape(research.get("generated_at", snapshot.get("generated_at", "")))}</div>
    </header>
    <nav>
      <a href="/opportunities">Opportunities</a>
      <a href="/loop">LOOP Bots</a>
      <a href="/grid">GRID Bots</a>
      <a class="active" href="/research">Research</a>
      <a href="/backtest">Backtest Lab</a>
    </nav>

    <div class="grid">
      {_metric("LOOP Ready", f"{loop_ready}/{loop_scanned}", "Live entry-ready scans")}
      {_metric("GRID Ready", f"{grid_ready}/{grid_scanned}", "Live entry-ready scans")}
      {_metric("LOOP Paper WR", _pct(loop_paper.get("win_rate_pct", 0.0)), "Last paper window")}
      {_metric("GRID Paper WR", _pct(grid_paper.get("win_rate_pct", 0.0)), "All GRID paper closes")}
      {_metric("LOOP Paper Net", _signed_pct(loop_paper.get("net_return_pct", 0.0)), "Tracked alert result", loop_paper.get("net_return_pct", 0.0))}
      {_metric("GRID Paper Net", _signed_pct(grid_paper.get("net_return_pct", 0.0)), "Tracked alert result", grid_paper.get("net_return_pct", 0.0))}
      {_metric("Scan Cycle", f"{int(research.get('scan_interval_minutes', 15))}m", "Telegram can fire any scan")}
      {_metric("Status", "Live", "Results page active")}
    </div>

    <section>
      <h2>LOOP Live Results</h2>
      <table>
        <thead>
          <tr><th>Coin</th><th>Entry Score</th><th>Status</th><th>Setup</th><th>Distance</th><th>Price</th><th>Reason</th></tr>
        </thead>
        <tbody>{loop_live_rows}</tbody>
      </table>
    </section>

    <section>
      <h2>GRID Live Results</h2>
      <table>
        <thead>
          <tr><th>Coin</th><th>Score</th><th>Status</th><th>Low / High</th><th>Levels</th><th>Grid Step</th><th>TP / SL</th><th>Reason</th></tr>
        </thead>
        <tbody>{grid_live_rows}</tbody>
      </table>
    </section>

    <section>
      <h2>LOOP Historical Proof</h2>
      <table>
        <thead>
          <tr><th>Coin</th><th>Setup</th><th>Trades</th><th>Win Rate</th><th>Est. Monthly / $1k</th><th>Status</th></tr>
        </thead>
        <tbody>{loop_proof_rows}</tbody>
      </table>
    </section>

    <section>
      <h2>GRID Historical Proof</h2>
      <table>
        <thead>
          <tr><th>Coin</th><th>Preset</th><th>Setup</th><th>Win Rate</th><th>Est. Monthly</th><th>Worst DD</th><th>Alerts/Mo</th><th>Status</th></tr>
        </thead>
        <tbody>{grid_proof_rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>"""


def render_backtest_dashboard(snapshot: dict[str, Any], refresh_seconds: int) -> str:
    backtest = snapshot.get("backtest", {})
    bot = str(backtest.get("bot", "grid"))
    rows = backtest.get("rows", [])
    if bot == "loop":
        result_rows = "".join(_backtest_loop_row(row) for row in rows) or _empty_row(11, "No LOOP settings passed.")
        result_head = "<tr><th>Coin</th><th>Timeframe</th><th>Distance</th><th>Count</th><th>Low</th><th>High</th><th>Trades</th><th>Win Rate</th><th>Est. Monthly</th><th>Net</th><th>Avg Hold</th></tr>"
    else:
        result_rows = "".join(_backtest_grid_row(row) for row in rows) or _empty_row(12, "No GRID settings passed.")
        result_head = "<tr><th>Coin</th><th>Timeframe</th><th>Low</th><th>High</th><th>Levels</th><th>Grid Step</th><th>TP / SL</th><th>Starts</th><th>Win Rate</th><th>Est. Monthly</th><th>Worst DD</th><th>Score</th></tr>"
    best_setup_card = _best_backtest_card(bot, rows[0] if rows else None)

    errors = backtest.get("errors") or []
    error_html = "".join(f"<div class=\"bad\">{_escape(error)}</div>" for error in errors)
    max_days = int(backtest.get("max_days", 1825))
    best_row = rows[0] if rows else None
    bot_label = "LOOP" if bot == "loop" else "GRID"
    settings_panel = _bitsgap_settings_panel(bot, best_row, backtest)
    result_panel = _bitsgap_result_panel(bot, best_row, backtest, rows)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Loopbots Backtest Lab</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #11101a;
      --panel: #1d1a2a;
      --panel-2: #28243a;
      --panel-3: #15131f;
      --line: #393449;
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
      width: min(1280px, calc(100vw - 28px));
      margin: 18px auto 32px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-end;
      margin-bottom: 18px;
    }}
    h1, h2 {{ margin: 0; }}
    h1 {{ font-size: 26px; }}
    h2 {{ font-size: 18px; margin-bottom: 10px; }}
    nav {{ display: flex; gap: 8px; margin: 0 0 18px; flex-wrap: wrap; }}
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
    .good {{ color: var(--good); }}
    .bad {{ color: var(--bad); }}
    .mini-nav {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .card, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    section {{ padding: 16px; margin-top: 16px; overflow-x: auto; }}
    .workspace {{
      display: grid;
      grid-template-columns: 370px minmax(0, 1fr);
      gap: 14px;
      align-items: start;
    }}
    .bot-panel {{
      margin-top: 0;
      padding: 14px;
      overflow: visible;
    }}
    .result-panel {{
      margin-top: 0;
      min-height: 680px;
    }}
    .bot-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }}
    .bot-title {{
      font-size: 17px;
      font-weight: 800;
    }}
    .error-box {{
      background: #302039;
      border: 1px solid #5b344f;
      color: #ff6f84;
      border-radius: 8px;
      padding: 10px;
      font-size: 12px;
      margin-bottom: 12px;
    }}
    form {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
      align-items: stretch;
    }}
    label {{ display: grid; gap: 6px; color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    input, select {{
      width: 100%;
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--text);
      border-radius: 8px;
      padding: 10px;
      font-size: 14px;
    }}
    button {{
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: #07111f;
      padding: 11px 14px;
      font-weight: 800;
      cursor: pointer;
    }}
    .segmented {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 7px;
    }}
    .segment-option {{
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--muted);
      padding: 8px 6px;
      text-align: center;
      font-size: 12px;
      font-weight: 700;
      text-transform: none;
      display: block;
      cursor: pointer;
    }}
    .segment-option input {{
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }}
    .segment-option.active {{
      background: var(--accent);
      border-color: var(--accent);
      color: #07111f;
    }}
    .manual-title {{
      color: var(--accent);
      font-weight: 800;
      text-align: center;
      margin: 2px 0 -2px;
    }}
    .bitsgap-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }}
    .bitsgap-input {{
      background: var(--panel-3);
      border: 1px solid #242033;
      border-radius: 8px;
      padding: 10px;
      min-height: 62px;
    }}
    .bitsgap-input .name {{
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 4px;
    }}
    .bitsgap-input .value {{
      font-size: 18px;
      font-weight: 800;
      overflow-wrap: anywhere;
    }}
    .toggle-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: var(--panel-2);
      border-radius: 8px;
      padding: 11px 12px;
      font-weight: 700;
    }}
    .switch {{
      width: 38px;
      height: 22px;
      border-radius: 999px;
      background: #51586b;
      position: relative;
      flex: 0 0 auto;
    }}
    .switch.on {{ background: var(--accent); }}
    .switch:after {{
      content: "";
      position: absolute;
      top: 3px;
      left: 3px;
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: white;
    }}
    .switch.on:after {{ left: 19px; }}
    .summary {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      margin-top: 12px;
    }}
    .setup-card {{
      display: grid;
      grid-template-columns: minmax(220px, 0.9fr) minmax(0, 1.6fr) minmax(220px, 0.9fr);
      gap: 14px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-top: 12px;
    }}
    .setup-title {{
      font-size: 20px;
      font-weight: 800;
      margin-bottom: 8px;
    }}
    .setup-fields {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }}
    .setup-field {{
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 9px;
      min-height: 58px;
    }}
    .setup-field .name {{
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }}
    .setup-field .setting {{
      font-size: 18px;
      font-weight: 800;
      margin-top: 3px;
    }}
    .proof-list {{
      display: grid;
      gap: 8px;
    }}
    .result-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
      margin-bottom: 14px;
    }}
    .result-title {{
      font-size: 19px;
      font-weight: 800;
    }}
    .result-stats {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .stat {{
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
    }}
    .stat .label {{
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }}
    .stat .number {{
      font-size: 21px;
      font-weight: 800;
      margin-top: 3px;
    }}
    .chart {{
      background: #15131f;
      border: 1px solid var(--line);
      border-radius: 8px;
      height: 310px;
      margin-bottom: 14px;
      overflow: hidden;
    }}
    .chart svg {{
      width: 100%;
      height: 100%;
      display: block;
    }}
    table {{ width: 100%; border-collapse: collapse; min-width: 980px; }}
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
      .workspace {{ grid-template-columns: 1fr; }}
      .setup-card {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>{bot_label} Backtester</h1>
        <div class="muted">Type a Kraken coin, run it, and the page auto-finds the best Bitsgap-ready settings.</div>
      </div>
      <div class="muted">Kraken / Bitsgap-style optimizer</div>
    </header>
    <div class="workspace">
    <section class="bot-panel">
      <div class="bot-header">
        <div class="bot-title">{bot_label} Opportunity</div>
        <span class="muted">Auto optimizer</span>
      </div>
      <div class="error-box">Kraken pair check + backtest only. No orders are placed.</div>
      <form method="get" action="/backtest">
        <input type="hidden" name="run" value="1">
        <label>Bot
          <select name="bot">
            <option value="grid"{_selected(bot, "grid")}>GRID</option>
            <option value="loop"{_selected(bot, "loop")}>LOOP</option>
          </select>
        </label>
        <label>Pair
          <input name="symbol" value="{_escape(backtest.get('symbol', 'ZEC/USD'))}" placeholder="ZEC/USD or AUTO">
        </label>
        <label>History
          <input name="days" value="{_escape(backtest.get('days', 365))}" inputmode="numeric">
        </label>
        <label>Timeframe
          <select name="timeframe">
            {_timeframe_options(str(backtest.get("timeframe", "1h")))}
          </select>
        </label>
        <label>Investment
          <input name="investment" value="{_escape(backtest.get('investment', 1000))}" inputmode="decimal">
        </label>
        <div>
          <div class="muted">Quick Setup</div>
          <div class="segmented">{_setup_mode_options(str(backtest.get("setup", "auto")))}</div>
        </div>
        <div class="manual-title">Manual adjustment</div>
        {settings_panel}
        <details>
          <summary class="muted">Optimizer settings</summary>
          <label>Max Pairs<input name="max_pairs" value="{_escape(backtest.get('max_pairs', 10))}" inputmode="numeric"></label>
          <label>LOOP Distances<input name="distances" value="{_escape(backtest.get('distances', '0.8,1,1.2,1.5,2,2.5,3'))}"></label>
          <label>LOOP Counts<input name="order_counts" value="{_escape(backtest.get('order_counts', '10,20,40'))}"></label>
          <label>GRID Low %<input name="grid_lowers" value="{_escape(backtest.get('grid_lowers', '3,5,8,10,14,18,25,35'))}"></label>
          <label>GRID High %<input name="grid_uppers" value="{_escape(backtest.get('grid_uppers', '7.5,10,17,22,35,50,65,80'))}"></label>
          <label>GRID Levels<input name="grid_levels" value="{_escape(backtest.get('grid_levels', '5,10,20,35,50,65,85,100'))}"></label>
          <label>Hold Days<input name="hold_days" value="{_escape(backtest.get('hold_days', '30'))}" inputmode="decimal"></label>
          <label>Take Profit %<input name="take_profit_pct" value="{_escape(backtest.get('take_profit_pct', '8'))}" inputmode="decimal"></label>
          <label>Stop Loss %<input name="stop_loss_pct" value="{_escape(backtest.get('stop_loss_pct', '5'))}" inputmode="decimal"></label>
          <label>Min Starts<input name="min_starts" value="{_escape(backtest.get('min_starts', '3'))}" inputmode="numeric"></label>
          <label>Min Win Rate<input name="min_win_rate" value="{_escape(backtest.get('min_win_rate', '45'))}" inputmode="decimal"></label>
        </details>
        <button type="submit">Backtest</button>
      </form>
      {error_html}
    </section>

    <section class="result-panel">
      {result_panel}

      <h2>Ranked Alternatives</h2>
      <table>
        <thead>{result_head}</thead>
        <tbody>{result_rows}</tbody>
      </table>
    </section>
    </div>
  </main>
</body>
</html>"""


def _opportunity_card(row: dict[str, Any], filters: dict[str, Any] | None = None) -> str:
    status = str(row.get("status", "Wait"))
    strategy = str(row.get("strategy", ""))
    speed = str(row.get("speed", ""))
    pair = str(row.get("pair", ""))
    return (
        '<article class="opportunity-card">'
        '<div class="coin-cell">'
        '<div>'
        f'<div class="pair">{_escape(pair)}</div>'
        f'<div class="subline">{_escape(strategy.title())} Bot &bull; Kraken</div>'
        f'<div class="subline">{_duration_label(speed)}</div>'
        "</div>"
        "</div>"
        '<div class="settings-cell">'
        '<div class="detail-grid">'
        '<div><div class="detail-label">Entry Zone</div>'
        f'<div class="detail-value">{_escape(row.get("entry_zone", ""))}</div></div>'
        "</div>"
        f"{_market_panel(row)}"
        f"{_settings_summary(row)}"
        "</div>"
        f"{_action_box(status, row, filters or {})}"
        "</article>"
    )


def _active_setup_cards(active_setups: dict[str, Any], filters: dict[str, Any]) -> str:
    active = active_setups.get("active", []) if isinstance(active_setups, dict) else []
    if not active:
        return ""
    cards = []
    for setup in active:
        action = str(setup.get("recommended_action", "HOLD"))
        pair = str(setup.get("pair", ""))
        strategy = str(setup.get("strategy", ""))
        current = _fmt_active_price(setup.get("current_price"))
        profit = _signed_pct(float(setup.get("profit_pct", 0.0) or 0.0))
        guidance = str(setup.get("guidance", "Keep running."))
        cards.append(
            '<article class="active-card">'
            f'<div><div class="pair" style="font-size:18px;">{_escape(pair)}</div><div class="subline">{_escape(strategy)} manual setup</div></div>'
            f'<div><div class="detail-label">Action</div><div class="active-action">{_escape(action)}</div></div>'
            f'<div><div class="detail-label">Now / PnL</div><div class="detail-value" style="font-size:15px;">{_escape(current)} &bull; {profit}</div><div class="subline">{_escape(guidance)}</div></div>'
            '<form method="post" action="/api/finish-setup">'
            f'<input type="hidden" name="setup_id" value="{_escape(setup.get("id", ""))}">'
            f'<input type="hidden" name="strategy" value="{_escape(filters.get("strategy", "grid"))}">'
            f'<input type="hidden" name="horizon" value="{_escape(filters.get("horizon", "short"))}">'
            '<button class="finish-button" type="submit">FINISH</button>'
            "</form>"
            "</article>"
        )
    return '<section class="active-section"><h2>Active Manual Setups</h2>' + "".join(cards) + "</section>"


def _opportunity_paper_cards(opportunity_paper: dict[str, Any], filters: dict[str, Any]) -> str:
    if not isinstance(opportunity_paper, dict):
        return ""
    open_trades = opportunity_paper.get("open", []) or []
    closed_trades = opportunity_paper.get("closed", []) or []
    all_rows = list(open_trades) + list(reversed(closed_trades))
    display_rows = list(open_trades) + list(reversed(closed_trades[-8:]))
    if not all_rows:
        return (
            '<section class="panel paper-section">'
            '<div class="paper-head"><div><h2>Opportunity Paper Trades</h2>'
            '<div class="muted">$1,000 paper tracking starts automatically when a setup is Ready Now.</div></div></div>'
            '<div class="empty-card">No saved opportunity paper trades yet.</div>'
            "</section>"
    )
    investment = _money(opportunity_paper.get("investment_usd", 1000.0))
    fee = _pct(float(opportunity_paper.get("fee_pct", 0.0) or 0.0))
    grid_rows = [row for row in all_rows if str(row.get("strategy", "")).upper() == "GRID"]
    loop_rows = [row for row in all_rows if str(row.get("strategy", "")).upper() == "LOOP"]
    grid_display_rows = [row for row in display_rows if str(row.get("strategy", "")).upper() == "GRID"]
    loop_display_rows = [row for row in display_rows if str(row.get("strategy", "")).upper() == "LOOP"]
    paper_filter = str(filters.get("paper", "all") or "all").lower()
    if paper_filter not in {"all", "grid", "loop"}:
        paper_filter = "all"
    if paper_filter == "grid":
        sections = _paper_strategy_section("GRID", grid_rows, grid_display_rows)
    elif paper_filter == "loop":
        sections = _paper_strategy_section("LOOP", loop_rows, loop_display_rows)
    else:
        sections = _paper_strategy_section("GRID", grid_rows, grid_display_rows) + _paper_strategy_section("LOOP", loop_rows, loop_display_rows)
    return (
        '<section id="paper-performance" class="panel paper-section">'
        f'<div class="paper-head"><div><h2>Paper Trading Performance</h2><div class="muted">{investment} simulated per deploy-ready setup. Fee estimate: {fee}. Separate from real Bitsgap.</div>{_paper_tabs(filters, paper_filter)}</div></div>'
        f'{sections}'
        "</section>"
    )


def _paper_stat(label: str, value: Any) -> str:
    return (
        '<div class="paper-stat">'
        f'<div class="name">{_escape(label)}</div>'
        f'<div class="number">{_escape(value)}</div>'
        "</div>"
    )


def _opportunities_quick_tabs(filters: dict[str, Any]) -> str:
    selected = str(filters.get("paper", "") or "").lower()
    selected_horizon = str(filters.get("horizon", "all") or "all").lower()
    tabs = [
        ("quick", "GRID Bots Ready Now", "/opportunities#grid-ready", selected == "" and selected_horizon == "all"),
        ("quick", "LOOP Bots Ready Now", "/opportunities#loop-ready", False),
        ("quick", "GRID Bot Paper", "/opportunities?" + urlencode({"paper": "grid"}) + "#paper-performance", selected == "grid"),
        ("quick", "LOOP Bot Paper Trading", "/opportunities?" + urlencode({"paper": "loop"}) + "#paper-performance", selected == "loop"),
        ("quick", "Saved Bots", "/opportunities#saved-bots", False),
    ]
    links = [
        f'<a class="quick-tab {kind}{" active" if active else ""}" href="{href}">{_escape(label)}</a>'
        for kind, label, href, active in tabs
    ]
    return f'<nav class="quick-tabs" aria-label="Dashboard sections">{"".join(links)}</nav>'


def _section_horizon_tabs(anchor: str, filters: dict[str, Any]) -> str:
    selected = str(filters.get("horizon", "all") or "all").lower()
    tabs = [
        ("all", "All terms"),
        ("short", "Short-term"),
        ("mid", "Mid-term"),
        ("long", "Long-term"),
    ]
    links = []
    for value, label in tabs:
        active = " active" if selected == value else ""
        query = {} if value == "all" else {"horizon": value}
        href = "/opportunities" + (("?" + urlencode(query)) if query else "") + f"#{anchor}"
        links.append(f'<a class="section-term-tab{active}" href="{href}">{_escape(label)}</a>')
    return f'<nav class="section-term-tabs" aria-label="Term filter">{"".join(links)}</nav>'


def _horizon_tabs(filters: dict[str, Any]) -> str:
    return ""


# Backward-compatible name for older render paths.
def _paper_quick_tabs(filters: dict[str, Any]) -> str:
    return _opportunities_quick_tabs(filters)


def _paper_tabs(filters: dict[str, Any], selected: str) -> str:
    tabs = [("grid", "GRID"), ("loop", "LOOP")]
    links = []
    for value, label in tabs:
        href = "/opportunities?" + urlencode({"paper": value}) + "#paper-performance"
        active = " active" if selected == value else ""
        links.append(f'<a class="paper-tab{active}" href="{href}">{_escape(label)}</a>')
    return f'<div class="paper-tabs">{"".join(links)}</div>'


def _paper_strategy_section(strategy: str, rows: list[dict[str, Any]], display_rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (
            '<div class="paper-strategy-block">'
            f'<div class="paper-strategy-head"><div class="section-title"><h3>{strategy} Paper</h3></div><a class="top-link" href="/opportunities#top" aria-label="Back to top">↑</a></div>'
            f'<div class="empty-card">No {strategy} paper trades yet.</div>'
            "</div>"
        )
    open_rows = [row for row in rows if row.get("status") == "OPEN"]
    closed_rows = [row for row in rows if row.get("status") == "CLOSED"]
    wins = [row for row in closed_rows if float(row.get("net_return_pct", 0.0) or 0.0) > 0]
    losses = max(len(closed_rows) - len(wins), 0)
    trade_size = float(rows[0].get("investment_usd", 1000.0) or 1000.0)
    realized_pnl = sum(float(row.get("net_pnl_usd", 0.0) or 0.0) for row in closed_rows)
    open_pnl = sum(float(row.get("unrealized_pnl_usd", 0.0) or 0.0) for row in open_rows)
    equity_pnl = realized_pnl + open_pnl
    closed_equity = (len(closed_rows) * trade_size) + realized_pnl
    open_equity = (len(open_rows) * trade_size) + open_pnl
    total_equity = closed_equity + open_equity
    win_rate = _win_rate_text(len(wins), len(closed_rows))
    stat_html = "".join(
        _paper_stat(label, value)
        for label, value in [
            ("Open", len(open_rows)),
            ("Closed", len(closed_rows)),
            ("W / L", f"{len(wins)} / {losses}"),
            ("Win Rate", win_rate),
            ("Realized PnL", _money(realized_pnl)),
            ("Open PnL", _money(open_pnl)),
            ("Equity PnL", _money(equity_pnl)),
            ("Closed Equity", _money(closed_equity)),
            ("Open Equity", _money(open_equity)),
            ("Total Equity", _money(total_equity)),
        ]
    )
    trade_html = "".join(_paper_trade_card(row) for row in display_rows[:8])
    return (
        '<div class="paper-strategy-block">'
        '<div class="paper-strategy-head">'
        f'<div class="section-title"><h3>{strategy} Paper</h3></div>'
        f'<div class="paper-stats">{stat_html}</div>'
        '<a class="top-link" href="/opportunities#top" aria-label="Back to top">↑</a>'
        "</div>"
        f'<div class="paper-list">{trade_html}</div>'
        "</div>"
    )


def _win_rate_text(wins: int, closed: int) -> str:
    if closed <= 0:
        return "Pending"
    return f"{wins}/{closed} ({_pct((wins / closed) * 100)})"


def _paper_trade_card(row: dict[str, Any]) -> str:
    status = str(row.get("status", "OPEN"))
    pair = str(row.get("pair", ""))
    strategy = str(row.get("strategy", ""))
    is_closed = status == "CLOSED"
    pnl_value = row.get("net_pnl_usd") if is_closed else row.get("unrealized_pnl_usd")
    pct_value = row.get("net_return_pct") if is_closed else row.get("unrealized_net_return_pct")
    pnl = _money(pnl_value or 0.0)
    pct = _signed_pct(float(pct_value or 0.0))
    result_class = "good" if float(pct_value or 0.0) >= 0 else "bad"
    exit_text = _price(row.get("exit_price")) if is_closed else "Open"
    reason = row.get("exit_reason") if is_closed else row.get("entry_reason")
    note = row.get("paper_note") or ""
    chip_class = "grid" if strategy.upper() == "GRID" else "loop"
    chip_text = "GRID PAPER" if strategy.upper() == "GRID" else "LOOP PAPER"
    return (
        '<article class="paper-trade">'
        f'<div><div class="paper-title">{_escape(pair)}</div><span class="strategy-chip {chip_class}">{chip_text}</span><div class="subline">{status.title()}</div>'
        f'<div class="subline">Opened {_escape(_short_time(row.get("opened_at", "")))}</div></div>'
        '<div>'
        '<div class="paper-detail">'
        f'<div><strong>Entry:</strong> {_price(row.get("entry_price"))}</div>'
        f'<div><strong>Exit:</strong> {_escape(exit_text)}</div>'
        f'<div><strong>TP:</strong> {_price(row.get("take_profit_price"))}</div>'
        f'<div><strong>SL / Safety:</strong> {_price(row.get("stop_price"))}</div>'
        f'<div><strong>Settings:</strong> {_escape(_paper_settings(row))}</div>'
        f'<div><strong>Reason:</strong> {_escape(reason or note or "Watching setup.")}</div>'
        "</div>"
        f'<div class="subline" style="margin-top:8px;">Last checked {_escape(_short_time(row.get("last_checked_at", "")))}</div>'
        "</div>"
        f'<div class="paper-result {result_class}">{pnl}<div class="subline">{pct}</div></div>'
        "</article>"
    )


def _paper_strategy_split(rows: list[dict[str, Any]]) -> str:
    cards = []
    for strategy in ("GRID", "LOOP"):
        strategy_rows = [row for row in rows if str(row.get("strategy", "")).upper() == strategy]
        if not strategy_rows:
            continue
        open_count = sum(1 for row in strategy_rows if row.get("status") == "OPEN")
        closed = [row for row in strategy_rows if row.get("status") == "CLOSED"]
        pnl = sum(float(row.get("net_pnl_usd", 0.0) or 0.0) for row in closed)
        cards.append(
            '<div class="paper-split-card">'
            f'<strong>{strategy}</strong> &bull; Open {open_count} &bull; Closed {len(closed)} &bull; PnL {_money(pnl)}'
            "</div>"
        )
    return f'<div class="paper-split">{"".join(cards)}</div>' if cards else ""


def _paper_settings(row: dict[str, Any]) -> str:
    fields = row.get("settings", {})
    if not isinstance(fields, dict):
        return "n/a"
    strategy = str(row.get("strategy", "")).upper()
    if strategy == "GRID":
        parts = [
            f"Low {fields.get('Low price')}",
            f"High {fields.get('High price')}",
            f"Levels {fields.get('Grid levels')}",
            f"TP {fields.get('Take profit')}",
            f"SL {fields.get('Stop loss')}",
        ]
    else:
        parts = [
            f"Distance {fields.get('Order distance')}",
            f"Count {fields.get('Order count')}",
            f"TP {fields.get('Take profit')}",
            f"Safety {fields.get('Safety exit / stop guidance')}",
        ]
    return " | ".join(str(part) for part in parts if "None" not in str(part))


def _money(value: Any) -> str:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        number = 0.0
    return f"${number:,.2f}"


def _market_panel(row: dict[str, Any]) -> str:
    market = row.get("market_snapshot", {})
    if not isinstance(market, dict) or not market:
        return ""
    change = float(market.get("change_pct", 0.0) or 0.0)
    color = "#079455" if change >= 0 else "#d92d20"
    change_text = _signed_pct(change)
    current = _fmt_active_price(market.get("current_price"))
    updated = _short_time(market.get("updated_at", ""))
    timeframe = str(market.get("timeframe", ""))
    sparkline = _sparkline_svg(market.get("closes", []), color)
    strategy = str(row.get("strategy", "")).upper()
    context = "Entry / TP / safety view" if strategy == "LOOP" else "Low / high / current view"
    return (
        '<div class="market-panel">'
        '<div>'
        '<div class="market-head"><div class="detail-label">Current price</div><span class="live-badge">LIVE</span></div>'
        f'<div class="market-price">{_escape(current)}</div>'
        f'<div class="market-meta"><span style="color:{color};font-weight:900;">{_escape(change_text)}</span> over selected window ({_escape(timeframe)})</div>'
        f'<div class="market-meta">Last updated { _escape(updated) }</div>'
        "</div>"
        f'<div><div class="market-meta">{_escape(context)}</div>{sparkline}</div>'
        "</div>"
    )


def _sparkline_svg(values: Any, color: str) -> str:
    try:
        points = [float(value) for value in values]
    except (TypeError, ValueError):
        points = []
    if len(points) < 2:
        return '<svg class="sparkline" viewBox="0 0 220 54" aria-hidden="true"></svg>'
    minimum = min(points)
    maximum = max(points)
    span = max(maximum - minimum, 0.0000001)
    width = 220
    height = 54
    coords = []
    for index, value in enumerate(points):
        x = (index / max(len(points) - 1, 1)) * width
        y = height - ((value - minimum) / span) * (height - 8) - 4
        coords.append(f"{x:.1f},{y:.1f}")
    return (
        '<svg class="sparkline" viewBox="0 0 220 54" aria-hidden="true">'
        f'<polyline class="sparkline-line" stroke="{_escape(color)}" points="{" ".join(coords)}"></polyline>'
        "</svg>"
    )


def _op_filter_tile(filters: dict[str, Any], key: str, value: str, label: str, icon: str, sublabel: str) -> str:
    selected = str(filters.get(key, "grid" if key == "strategy" else "short") or "").lower()
    if key == "strategy" and selected not in {"grid", "loop"}:
        selected = "grid"
    active = " active" if selected == value.lower() else ""
    current_strategy = str(filters.get("strategy", "grid") or "grid").lower()
    if current_strategy not in {"grid", "loop"}:
        current_strategy = "grid"
    query = {
        "strategy": current_strategy,
        "horizon": str(filters.get("horizon", "short") or "short").lower(),
    }
    query[key] = value.lower()
    href = "/opportunities?" + "&".join(f"{name}={_escape(raw)}" for name, raw in query.items())
    return (
        f'<a class="filter-tile{active}" href="{href}">'
        f'<span class="filter-icon">{icon}</span>'
        f'<span>{_escape(label)}</span>'
        f'<span class="tile-sub">{_escape(sublabel)}</span>'
        "</a>"
    )


def _short_time(value: Any) -> str:
    text = str(value or "")
    manual = _manual_time(text)
    if manual:
        return manual
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%-I:%M:%S %p") if os.name != "nt" else parsed.strftime("%#I:%M:%S %p")
    except (TypeError, ValueError):
        return text


def _fmt_active_price(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if number >= 100:
        return f"{number:,.2f}"
    if number >= 1:
        return f"{number:,.4f}"
    return f"{number:,.6f}"


def _manual_time(text: str) -> str:
    separator = "T" if "T" in text else " " if " " in text else ""
    if not separator:
        return ""
    raw_time = text.split(separator, 1)[1].split("+", 1)[0].split(".", 1)[0]
    parts = raw_time.split(":")
    if len(parts) < 2:
        return ""
    try:
        hour = int(parts[0])
    except ValueError:
        return ""
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    seconds = f":{parts[2]}" if len(parts) > 2 else ""
    return f"{display_hour}:{parts[1]}{seconds} {suffix}"


def _coin_initial(pair: str) -> str:
    base = str(pair or "?").split("/")[0]
    return (base[:1] or "?").upper()


def _duration_label(speed: str) -> str:
    speed = speed.lower()
    if speed == "fast":
        return "Days - 2 weeks"
    if speed == "slow":
        return "2 - 8 weeks"
    return "1 - 3 weeks"


def _entry_tag(status: str) -> str:
    if status == "Ready Now":
        return "In Entry Zone"
    if status == "Avoid":
        return "Above Entry"
    return "Approaching Entry"


def _short_reason(reason: Any) -> str:
    text = str(reason or "Setup conditions are being monitored.")
    text = text.replace("Waiting: ", "").rstrip(".")
    parts = [part.strip() for part in text.split(",") if part.strip()]
    return " • ".join(parts[:3]) if parts else text


def _fit_class(row: dict[str, Any]) -> str:
    status = str(row.get("status", "Wait"))
    if status == "Ready Now":
        return "status-ready"
    if status == "Avoid":
        return "status-avoid"
    return "status-wait"


def _fit_text_class(row: dict[str, Any]) -> str:
    status = str(row.get("status", "Wait"))
    if status == "Ready Now":
        return "good"
    if status == "Avoid":
        return "bad"
    return "warn"


def _fit_label(row: dict[str, Any]) -> str:
    status = str(row.get("status", "Wait"))
    if status == "Ready Now":
        return "READY"
    if status == "Avoid":
        return "WATCH"
    return "GOOD"


def _opportunity_row(row: dict[str, Any]) -> str:
    status = str(row.get("status", "Wait"))
    return (
        "<tr>"
        f"<td>{_escape(row.get('pair', ''))}</td>"
        f"<td><span class=\"pill\">{_escape(row.get('strategy', ''))}</span></td>"
        f"<td>{_status_badge(status)}</td>"
        f"<td>{_escape(row.get('risk', ''))}</td>"
        f"<td>{_escape(row.get('speed', ''))}</td>"
        f"<td>{_escape(row.get('entry_zone', ''))}</td>"
        f"<td>{_settings_summary(row)}</td>"
        f"<td>{_escape(row.get('reason', ''))}</td>"
        "</tr>"
    )


def _action_box(status: str, row: dict[str, Any] | None = None, filters: dict[str, Any] | None = None) -> str:
    key = status.lower().replace(" ", "-")
    if key == "ready-now":
        css_class = "action-ready"
        button = "SAVE & MONITOR"
    elif key == "avoid":
        css_class = "action-avoid"
        button = "NOT IDEAL NOW"
    else:
        css_class = "action-wait"
        button = "NOTIFY ME"
    if key == "ready-now" and row is not None:
        filters = filters or {}
        return (
            f'<aside class="action-box {css_class}">'
            '<div class="action-note">Settings ready to copy right now</div>'
            '<form method="post" action="/api/use-setup">'
            f'<input type="hidden" name="opportunity_id" value="{_escape(row.get("id", ""))}">'
            f'<input type="hidden" name="strategy" value="{_escape(filters.get("strategy", "grid"))}">'
            f'<input type="hidden" name="horizon" value="{_escape(filters.get("horizon", "short"))}">'
            f'<button class="action-button" type="submit">{button}</button>'
            "</form>"
            "</aside>"
        )
    return (
        f'<aside class="action-box {css_class}">'
        f'<div class="status-action">{button}</div>'
        "</aside>"
    )


def _status_badge(status: str) -> str:
    status_key = status.lower().replace(" ", "-")
    css_class = {
        "ready-now": "status-ready",
        "wait": "status-wait",
        "avoid": "status-avoid",
    }.get(status_key, "status-wait")
    return f'<span class="status-badge {css_class}">{_escape(status)}</span>'


def _settings_summary(row: dict[str, Any]) -> str:
    fields = row.get("bitsgap_fields", {})
    if not isinstance(fields, dict) or not fields:
        return '<div class="fields"><div>n/a</div></div>'
    strategy = str(row.get("strategy", "")).upper()
    if strategy == "GRID":
        items = [
            ("Low", fields.get("Low price")),
            ("High", fields.get("High price")),
            ("Levels", fields.get("Grid levels")),
            ("SL", fields.get("Stop loss")),
            ("TP", fields.get("Take profit")),
            ("Trailing Up", "On"),
            ("Trailing Down", "Off"),
            ("Protection", "Monitoring on"),
        ]
    else:
        items = [
            ("Order distance", fields.get("Order distance")),
            ("Order count", fields.get("Order count")),
            ("Entry zone", row.get("entry_zone")),
            ("TP", fields.get("Take profit")),
            ("Safety exit", fields.get("Safety exit / stop guidance")),
            ("Live", "Now"),
        ]
    visible = [
        f"<div><strong>{_field_label(label)}:</strong> {_escape(value)}</div>"
        for label, value in items
        if value not in {"", None}
    ]
    return f'<div class="fields">{"".join(visible) if visible else "<div>n/a</div>"}</div>'


def _field_label(label: str) -> str:
    help_text = _field_help_text(label)
    if not help_text:
        return _escape(label)
    return f'{_escape(label)}<span class="help-icon" title="{_escape(help_text)}" data-tip="{_escape(help_text)}">?</span>'


def _field_help_text(label: str) -> str:
    key = label.lower()
    if key == "tp":
        return "Starting take-profit setting for Bitsgap. If this setup keeps improving, the app may later alert you to move TP higher, take profit, or trail profit."
    if key in {"sl", "safety exit"}:
        return "Starting protection level. If price moves in your favor, the app may later alert you to move this closer to breakeven or into profit to protect capital."
    if key == "trailing up":
        return "Starting trailing-up setting. If price keeps rising and the setup remains healthy, the app may alert you when trailing up is useful."
    if key == "trailing down":
        return "Starting trailing-down setting. Usually off for safer setups. The app may only suggest it later if market conditions justify it."
    if key == "protection":
        return "These are the starting protection rules. After you mark a setup active, the app watches the market and may alert you to update TP, SL/safety exit, trailing, or exit based on current conditions."
    if key == "live":
        return "This setup is live from current Kraken data. After you mark it active, the app may alert you to update TP, move the safety exit, trail profit, or exit. This is not Bitsgap pump protection."
    return ""


def _customer_setup_reason(row: dict[str, Any]) -> str:
    if str(row.get("strategy", "")).upper() != "LOOP":
        return ""
    reason = str(row.get("reason", "") or "").strip()
    if not reason:
        return ""
    return f'<div class="setup-reason">{_escape(reason)}</div>'


def _field_list(fields: Any) -> str:
    if not isinstance(fields, dict) or not fields:
        return '<div class="fields"><div>n/a</div></div>'
    items = []
    for key, value in fields.items():
        if value in {"", None}:
            continue
        if key in {"Pair", "Entry zone"}:
            continue
        items.append(f"<div><strong>{_escape(key)}:</strong> {_escape(value)}</div>")
    return f'<div class="fields">{"".join(items) if items else "<div>n/a</div>"}</div>'


def _proof_summary(proof: dict[str, Any]) -> str:
    parts = []
    if proof.get("win_rate_pct") is not None:
        parts.append(f"WR {_pct(float(proof.get('win_rate_pct') or 0.0))}")
    if proof.get("average_return_pct") is not None:
        parts.append(f"Avg {_signed_pct(float(proof.get('average_return_pct') or 0.0))}")
    if proof.get("worst_drawdown_pct") is not None:
        parts.append(f"DD {_signed_pct(float(proof.get('worst_drawdown_pct') or 0.0))}")
    if proof.get("historical_starts") is not None:
        parts.append(f"Starts {int(float(proof.get('historical_starts') or 0))}")
    label = proof.get("label") or "experimental"
    parts.append(str(label).title())
    return '<div class="fields">' + "".join(f"<div>{_escape(part)}</div>" for part in parts) + "</div>"


def _filter_options(selected: Any, options: list[tuple[str, str]]) -> str:
    selected_value = str(selected or "").lower()
    return "".join(
        f'<option value="{_escape(value)}"{_selected(selected_value, value)}>{_escape(label)}</option>'
        for value, label in options
    )


def _research_loop_live_row(row: dict[str, Any]) -> str:
    score = int(float(row.get("entry_score", 0) or 0))
    status = row.get("status", "Waiting")
    status_class = "good" if status == "Ready" else ""
    distance = row.get("order_distance_pct", "")
    distance_text = f"{float(distance):g}%" if distance != "" else ""
    setup = row.get("preset_name") or row.get("mode") or ""
    return (
        "<tr>"
        f"<td>{_escape(row.get('symbol', ''))}</td>"
        f"<td>{_score_bar(score)}</td>"
        f'<td class="{status_class}">{_escape(status)}</td>'
        f"<td><span class=\"pill\">{_escape(setup)}</span></td>"
        f"<td>{_escape(distance_text)}</td>"
        f"<td>{_price(row.get('price', 0.0))}</td>"
        f"<td>{_escape(row.get('reason', ''))}</td>"
        "</tr>"
    )


def _research_grid_live_row(row: dict[str, Any]) -> str:
    score = int(float(row.get("score", 0) or 0))
    status = "Ready" if row.get("ready") else "Waiting"
    if row.get("active"):
        status = "Active paper"
    status_class = "good" if row.get("ready") else ""
    lower_pct = float(row.get("lower_pct", 0.0) or 0.0)
    upper_pct = float(row.get("upper_pct", 0.0) or 0.0)
    levels = int(float(row.get("levels", 0) or 0))
    grid_step_pct = float(row.get("grid_step_pct", 0.0) or 0.0)
    take_profit_pct = float(row.get("take_profit_pct", 0.0) or 0.0)
    stop_loss_pct = float(row.get("stop_loss_pct", 0.0) or 0.0)
    range_text = f"-{lower_pct:g}% / +{upper_pct:g}%"
    tp_sl_text = f"+{take_profit_pct:g}% / -{stop_loss_pct:g}%"
    return (
        "<tr>"
        f"<td>{_escape(row.get('symbol', ''))}</td>"
        f"<td>{_score_bar(score)}</td>"
        f'<td class="{status_class}">{_escape(status)}</td>'
        f"<td>{_escape(range_text)}</td>"
        f"<td>{levels}</td>"
        f"<td>Roughly {_escape(f'{grid_step_pct:.2f}'.rstrip('0').rstrip('.'))}%</td>"
        f"<td>{_escape(tp_sl_text)}</td>"
        f"<td>{_escape(row.get('reason', ''))}</td>"
        "</tr>"
    )


def _research_loop_proof_row(row: dict[str, Any]) -> str:
    status = str(row.get("status", ""))
    status_class = "good" if status == "Proven" else "warn" if "sample" in status.lower() else ""
    return (
        "<tr>"
        f"<td>{_escape(row.get('symbol', ''))}</td>"
        f"<td>{_escape(row.get('setup', ''))}</td>"
        f"<td>{int(float(row.get('trades', 0) or 0))}</td>"
        f"<td>{_pct(row.get('win_rate_pct', 0.0))}</td>"
        f"<td>${float(row.get('monthly_per_1k', 0.0) or 0.0):.2f}</td>"
        f'<td class="{status_class}">{_escape(status)}</td>'
        "</tr>"
    )


def _research_grid_proof_row(row: dict[str, Any]) -> str:
    status = str(row.get("status", ""))
    status_class = "good" if status == "Proven" else "warn"
    return (
        "<tr>"
        f"<td>{_escape(row.get('symbol', ''))}</td>"
        f"<td><span class=\"pill\">{_escape(row.get('preset_name', ''))}</span></td>"
        f"<td>{_escape(row.get('setup', ''))}</td>"
        f"<td>{_pct(row.get('win_rate_pct', 0.0))}</td>"
        f"<td>{_signed_pct(row.get('monthly_pct', 0.0))}</td>"
        f"<td>{_signed_pct(row.get('worst_drawdown_pct', 0.0))}</td>"
        f"<td>{float(row.get('alerts_per_month', 0.0) or 0.0):.1f}</td>"
        f'<td class="{status_class}">{_escape(status)}</td>'
        "</tr>"
    )


def _score_bar(score: int) -> str:
    clipped = max(0, min(100, int(score)))
    return (
        '<div class="score">'
        f"<span>{clipped}/100</span>"
        f'<div class="bar" style="--w:{clipped}%"><span></span></div>'
        "</div>"
    )


def _bitsgap_settings_panel(bot: str, row: dict[str, Any] | None, backtest: dict[str, Any]) -> str:
    if row is None:
        if bot == "loop":
            fields = [
                ("Low price", "auto"),
                ("High price", "auto"),
                ("Order distance, %", "auto"),
                ("Order count", "auto"),
            ]
            toggles = [("Take Profit", True)]
        else:
            fields = [
                ("Low price", "auto"),
                ("High price", "auto"),
                ("Grid step, %", "auto"),
                ("Grid levels", "auto"),
            ]
            toggles = [
                ("Trailing Up", True),
                ("Pump Protection", True),
                ("Trailing Down", False),
                ("Stop Loss", False),
                ("Take Profit", False),
            ]
        return (
            f'<div class="bitsgap-grid">{"".join(_bitsgap_input(name, value) for name, value in fields)}</div>'
            f'{"".join(_toggle_row(label, enabled) for label, enabled in toggles)}'
        )

    if bot == "loop":
        symbol = str(row.get("symbol", backtest.get("symbol", "")))
        fields = [
            ("Timeframe", row.get("timeframe", backtest.get("timeframe", ""))),
            ("Low price", _price(row.get("low_price", 0.0))),
            ("High price", _price(row.get("high_price", 0.0))),
            ("Order distance, %", _pct_trim(row.get("order_distance_pct", 0.0)).replace("%", "")),
            ("Order count", int(float(row.get("order_count", 0) or 0))),
        ]
        toggles = [("Take Profit", True)]
        balance = f"Quote currency: 0.00 {symbol.split('/')[-1] if '/' in symbol else 'USD'}"
    else:
        symbol = str(row.get("symbol", backtest.get("symbol", "")))
        fields = [
            ("Timeframe", row.get("timeframe", backtest.get("timeframe", ""))),
            ("Low price", _price(row.get("low_price", 0.0))),
            ("High price", _price(row.get("high_price", 0.0))),
            ("Grid step, %", _pct_trim(row.get("grid_step_pct", 0.0)).replace("%", "")),
            ("Grid levels", int(float(row.get("levels", 0) or 0))),
        ]
        toggles = [
            ("Trailing Up", True),
            ("Pump Protection", True),
            ("Trailing Down", False),
            ("Stop Loss", True),
            ("Take Profit", True),
        ]
        balance = f"Quote currency: 0.00 {symbol.split('/')[-1] if '/' in symbol else 'USD'}"

    return (
        f'<div class="bitsgap-grid">{"".join(_bitsgap_input(name, value) for name, value in fields)}</div>'
        f'{"".join(_toggle_row(label, enabled) for label, enabled in toggles)}'
        f'<div class="muted">{_escape(balance)}</div>'
    )


def _bitsgap_input(name: str, value: Any) -> str:
    return (
        '<div class="bitsgap-input">'
        f'<div class="name">{_escape(name)}</div>'
        f'<div class="value">{_escape(value)}</div>'
        "</div>"
    )


def _toggle_row(label: str, enabled: bool) -> str:
    return (
        '<div class="toggle-row">'
        f"<span>{_escape(label)}</span>"
        f'<span class="switch{" on" if enabled else ""}"></span>'
        "</div>"
    )


def _bitsgap_result_panel(bot: str, row: dict[str, Any] | None, backtest: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    if row is None:
        return _preview_result_panel(bot, backtest, rows)
    symbol = row.get("symbol", backtest.get("symbol", ""))
    lock_note = ""
    symbol_input = str(backtest.get("symbol_input", symbol))
    if symbol_input and symbol_input != str(symbol):
        lock_note = f" | Locked: {symbol_input} -> {symbol}"
    result_pct = _row_result_pct(bot, row)
    result_class = "good" if result_pct >= 0 else "bad"
    stats = _result_stats(bot, row)
    return (
        '<div class="result-head">'
        f'<div><div class="result-title">{_escape(symbol)} Backtest</div>'
        f'<div class="muted">Trading fee { "0.25%" if bot == "grid" else "0.20%" } | Timeframe: {_escape(row.get("timeframe", backtest.get("timeframe", "")))} | Tested: {_escape(", ".join(backtest.get("tested_timeframes", []) or [str(backtest.get("timeframe", ""))]))}{_escape(lock_note)}</div></div>'
        f'<div class="{result_class}" style="font-size:24px;font-weight:800;">{_signed_pct(result_pct)}</div>'
        "</div>"
        f'<div class="result-stats">{"".join(_result_stat(label, value, signed) for label, value, signed in stats)}</div>'
        f'<div class="chart">{_backtest_svg(bot, row)}</div>'
        f'<div class="summary"><div>{_escape(backtest.get("summary", ""))}</div><div class="muted">{len(rows)} ranked rows</div></div>'
    )


def _preview_result_panel(bot: str, backtest: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    preview = backtest.get("preview") if isinstance(backtest.get("preview"), dict) else {}
    symbol = preview.get("symbol") or backtest.get("symbol", "")
    exists = bool(preview.get("exists"))
    chart = preview.get("chart") or []
    latest = float(preview.get("latest_price", 0.0) or 0.0)
    points = int(float(preview.get("points", 0) or 0))
    status_class = "good" if exists else "warn"
    status_text = "Coin found" if exists else "Waiting"
    message = preview.get("message") or "Run a coin to generate optimized settings."
    chart_row = _preview_chart_row(chart)
    chart_html = _backtest_svg(bot, chart_row) if exists else '<div class="muted" style="padding:16px;">No chart yet. Enter a Kraken pair like ZEC/USD, then run the check.</div>'
    stats = [
        ("Status", status_text, False),
        ("Last price", _price(latest) if latest else "n/a", False),
        ("Chart points", points, False),
    ]
    return (
        '<div class="result-head">'
        f'<div><div class="result-title">{_escape(symbol)} Preview</div>'
        f'<div class="muted">{_escape(message)}</div></div>'
        f'<div class="{status_class}" style="font-size:18px;font-weight:800;">Kraken check</div>'
        "</div>"
        f'<div class="result-stats">{"".join(_result_stat(label, value, signed) for label, value, signed in stats)}</div>'
        f'<div class="chart">{chart_html}</div>'
        f'<div class="summary"><div>{_escape(backtest.get("summary", ""))}</div><div class="muted">{len(rows)} ranked rows</div></div>'
    )


def _preview_chart_row(chart: list[dict[str, Any]]) -> dict[str, Any]:
    prices = [float(point.get("close", 0.0) or 0.0) for point in chart if float(point.get("close", 0.0) or 0.0) > 0]
    if not prices:
        return {"chart": chart}
    return {
        "chart": chart,
        "low_price": min(prices),
        "high_price": max(prices),
        "levels": 10,
    }


def _row_result_pct(bot: str, row: dict[str, Any]) -> float:
    if bot == "loop":
        return float(row.get("monthly_return_on_trade_size_pct", 0.0) or 0.0)
    return float(row.get("avg_monthly_pct", 0.0) or 0.0)


def _result_stats(bot: str, row: dict[str, Any]) -> list[tuple[str, Any, bool]]:
    if bot == "loop":
        return [
            ("Win rate", _pct(row.get("win_rate_pct", 0.0)), False),
            ("Trades", int(float(row.get("trades", 0) or 0)), False),
            ("Net return", _signed_pct(row.get("net_return_pct", 0.0)), True),
        ]
    return [
        ("Win rate", _pct(row.get("win_rate_pct", 0.0)), False),
        ("Valid starts", int(float(row.get("starts", 0) or 0)), False),
        ("Worst DD", _signed_pct(row.get("worst_max_drawdown_pct", 0.0)), True),
    ]


def _result_stat(label: str, value: Any, signed: bool) -> str:
    value_class = ""
    if signed:
        value_class = " good" if not str(value).startswith("-") else " bad"
    return (
        '<div class="stat">'
        f'<div class="label">{_escape(label)}</div>'
        f'<div class="number{value_class}">{_escape(value)}</div>'
        "</div>"
    )


def _backtest_svg(bot: str, row: dict[str, Any]) -> str:
    chart = row.get("chart") or []
    if not chart:
        return '<div class="muted" style="padding:16px;">No chart data loaded.</div>'

    width = 900
    height = 300
    pad = 24
    prices = [float(point.get("close", 0.0) or 0.0) for point in chart if float(point.get("close", 0.0) or 0.0) > 0]
    if not prices:
        return '<div class="muted" style="padding:16px;">No chart data loaded.</div>'
    low_price = float(row.get("low_price", min(prices)) or min(prices))
    high_price = float(row.get("high_price", max(prices)) or max(prices))
    min_price = min(min(prices), low_price)
    max_price = max(max(prices), high_price)
    span = max(max_price - min_price, 0.00000001)

    def x(index: int) -> float:
        return pad + (index / max(len(prices) - 1, 1)) * (width - pad * 2)

    def y(price: float) -> float:
        return height - pad - ((price - min_price) / span) * (height - pad * 2)

    path = " ".join(f"{'M' if index == 0 else 'L'} {x(index):.2f} {y(price):.2f}" for index, price in enumerate(prices))
    low_y = y(low_price)
    high_y = y(high_price)
    grid_lines = []
    if bot == "grid":
        levels = int(float(row.get("levels", 10) or 10))
        for level in range(max(min(levels, 18), 1) + 1):
            ratio = level / max(min(levels, 18), 1)
            line_y = high_y + (low_y - high_y) * ratio
            color = "#2ddf9a" if ratio > 0.55 else "#ff6875"
            grid_lines.append(f'<line x1="{pad}" y1="{line_y:.2f}" x2="{width-pad}" y2="{line_y:.2f}" stroke="{color}" stroke-opacity="0.35" />')
    else:
        grid_lines = [
            f'<line x1="{pad}" y1="{low_y:.2f}" x2="{width-pad}" y2="{low_y:.2f}" stroke="#2ddf9a" stroke-opacity="0.65" />',
            f'<line x1="{pad}" y1="{high_y:.2f}" x2="{width-pad}" y2="{high_y:.2f}" stroke="#ff6875" stroke-opacity="0.65" />',
        ]
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img">'
        '<rect width="100%" height="100%" fill="#15131f" />'
        f'{"".join(grid_lines)}'
        f'<path d="{path}" fill="none" stroke="#4da3ff" stroke-width="2.4" />'
        f'<text x="{pad}" y="{max(high_y - 6, 14):.2f}" fill="#ffb4c0" font-size="12">High</text>'
        f'<text x="{pad}" y="{min(low_y + 16, height - 6):.2f}" fill="#9df4cd" font-size="12">Low</text>'
        "</svg>"
    )


def _best_backtest_card(bot: str, row: dict[str, Any] | None) -> str:
    if not row:
        return '<div class="muted">Run a coin to get one exact Bitsgap setup here.</div>'
    if bot == "loop":
        return _best_loop_card(row)
    return _best_grid_card(row)


def _best_grid_card(row: dict[str, Any]) -> str:
    symbol = row.get("symbol", "")
    proof = [
        ("Win Rate", _pct(row.get("win_rate_pct", 0.0))),
        ("Valid Starts", int(float(row.get("starts", 0) or 0))),
        ("Est. Monthly", _signed_pct(row.get("avg_monthly_pct", 0.0))),
        ("Worst DD", _signed_pct(row.get("worst_max_drawdown_pct", 0.0))),
    ]
    fields = [
        ("Exchange", "Kraken"),
        ("Pair", symbol),
        ("Timeframe", row.get("timeframe", "")),
        ("Low Price", _price(row.get("low_price", 0.0))),
        ("High Price", _price(row.get("high_price", 0.0))),
        ("Grid Step", _pct_trim(row.get("grid_step_pct", 0.0))),
        ("Grid Levels", int(float(row.get("levels", 0) or 0))),
        ("Order Size Currency", row.get("order_size_currency", "")),
        ("Trailing Up", "On"),
        ("Pump Protection", "On"),
        ("Trailing Down", "Off"),
        ("Stop Loss", f"On (-{_pct_trim(row.get('stop_loss_pct', 0.0))})"),
        ("Take Profit", f"On (+{_pct_trim(row.get('take_profit_pct', 0.0))})"),
    ]
    return (
        '<div class="setup-card">'
        f'<div><div class="setup-title">GRID { _escape(symbol) }</div><div class="muted">Copy these into Bitsgap Create GRID Bot.</div></div>'
        f'<div class="setup-fields">{"".join(_setup_field(name, value) for name, value in fields)}</div>'
        f'<div class="proof-list">{"".join(_setup_field(name, value) for name, value in proof)}</div>'
        "</div>"
    )


def _best_loop_card(row: dict[str, Any]) -> str:
    symbol = row.get("symbol", "")
    proof = [
        ("Win Rate", _pct(row.get("win_rate_pct", 0.0))),
        ("Trades", int(float(row.get("trades", 0) or 0))),
        ("Est. Monthly", _signed_pct(row.get("monthly_return_on_trade_size_pct", 0.0))),
        ("Net Return", _signed_pct(row.get("net_return_pct", 0.0))),
    ]
    fields = [
        ("Exchange", "Kraken"),
        ("Pair", symbol),
        ("Timeframe", row.get("timeframe", "")),
        ("Low Price", _price(row.get("low_price", 0.0))),
        ("High Price", _price(row.get("high_price", 0.0))),
        ("Order Distance", _pct_trim(row.get("order_distance_pct", 0.0))),
        ("Order Count", int(float(row.get("order_count", 0) or 0))),
        ("Order Size Currency", str(symbol).split("/")[-1] if "/" in str(symbol) else "USD"),
        ("Take Profit", "On"),
    ]
    return (
        '<div class="setup-card">'
        f'<div><div class="setup-title">LOOP { _escape(symbol) }</div><div class="muted">Copy these into Bitsgap Create LOOP Bot.</div></div>'
        f'<div class="setup-fields">{"".join(_setup_field(name, value) for name, value in fields)}</div>'
        f'<div class="proof-list">{"".join(_setup_field(name, value) for name, value in proof)}</div>'
        "</div>"
    )


def _setup_field(name: str, value: Any) -> str:
    return (
        '<div class="setup-field">'
        f'<div class="name">{_escape(name)}</div>'
        f'<div class="setting">{_escape(value)}</div>'
        "</div>"
    )


def _backtest_grid_row(row: dict[str, Any]) -> str:
    score = float(row.get("optimizer_score", 0.0) or 0.0)
    return (
        "<tr>"
        f"<td>{_escape(row.get('symbol', ''))}</td>"
        f"<td>{_escape(row.get('timeframe', ''))}</td>"
        f"<td>{_price(row.get('low_price', 0.0))}</td>"
        f"<td>{_price(row.get('high_price', 0.0))}</td>"
        f"<td>{int(float(row.get('levels', 0) or 0))}</td>"
        f"<td>{_pct_trim(row.get('grid_step_pct', 0.0))}</td>"
        f"<td>+{_pct_trim(row.get('take_profit_pct', 0.0))} / -{_pct_trim(row.get('stop_loss_pct', 0.0))}</td>"
        f"<td>{int(float(row.get('starts', 0) or 0))}</td>"
        f"<td>{_pct(row.get('win_rate_pct', 0.0))}</td>"
        f"<td>{_signed_pct(row.get('avg_monthly_pct', 0.0))}</td>"
        f"<td>{_signed_pct(row.get('worst_max_drawdown_pct', 0.0))}</td>"
        f"<td>{score:.2f}</td>"
        "</tr>"
    )


def _backtest_loop_row(row: dict[str, Any]) -> str:
    return (
        "<tr>"
        f"<td>{_escape(row.get('symbol', ''))}</td>"
        f"<td>{_escape(row.get('timeframe', ''))}</td>"
        f"<td>{_pct_trim(row.get('order_distance_pct', 0.0))}</td>"
        f"<td>{int(float(row.get('order_count', 0) or 0))}</td>"
        f"<td>{_price(row.get('low_price', 0.0))}</td>"
        f"<td>{_price(row.get('high_price', 0.0))}</td>"
        f"<td>{int(float(row.get('trades', 0) or 0))}</td>"
        f"<td>{_pct(row.get('win_rate_pct', 0.0))}</td>"
        f"<td>{_signed_pct(row.get('monthly_return_on_trade_size_pct', 0.0))}</td>"
        f"<td>{_signed_pct(row.get('net_return_pct', 0.0))}</td>"
        f"<td>{float(row.get('avg_hold_hours', 0.0) or 0.0):.2f}h</td>"
        "</tr>"
    )


def _selected(value: str, expected: str) -> str:
    return " selected" if str(value).lower() == expected.lower() else ""


def _timeframe_options(selected: str) -> str:
    return "".join(
        f'<option value="{timeframe}"{_selected(selected, timeframe)}>{timeframe}</option>'
        for timeframe in ["15m", "30m", "1h", "2h", "4h", "1d"]
    )


def _setup_mode_options(selected: str) -> str:
    modes = [
        ("auto", "Automatic"),
        ("fast", "Fast"),
        ("balanced", "Balanced"),
        ("slow", "Slow"),
    ]
    selected = selected if selected in {mode for mode, _ in modes} else "auto"
    return "".join(
        '<label class="segment-option{active}">'
        '<input type="radio" name="setup" value="{value}"{checked}>'
        "{label}"
        "</label>".format(
            active=" active" if value == selected else "",
            value=_escape(value),
            checked=" checked" if value == selected else "",
            label=_escape(label),
        )
        for value, label in modes
    )


def _pct_trim(value: Any) -> str:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        number = 0.0
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


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
    score = row.get("entry_score", "")
    score_text = f"{int(float(score))}/100" if score != "" else "pending"
    status = row.get("status", "Waiting")
    status_class = "good" if status == "Ready" else ""
    return (
        "<tr>"
        f"<td>{_escape(row.get('symbol', ''))}</td>"
        f"<td>{_escape(score_text)}</td>"
        f'<td class="{status_class}">{_escape(status)}</td>'
        f"<td>{_escape(row.get('mode', ''))}</td>"
        f"<td>{_price(row.get('price', 0.0))}</td>"
        f"<td>{_escape(row.get('reason', 'waiting for scan'))}</td>"
        "</tr>"
    )


def _grid_scan_row(row: dict[str, Any]) -> str:
    status = "Ready" if row.get("ready") else "Waiting"
    if row.get("active"):
        status = "Active paper"
    status_class = "good" if row.get("ready") else ""
    setup_type = "Experimental" if row.get("experimental") else "Proven"
    return (
        "<tr>"
        f"<td>{_escape(row.get('symbol', ''))}</td>"
        f"<td><span class=\"pill\">{_escape(row.get('preset_name', ''))}</span></td>"
        f"<td>{_escape(setup_type)}</td>"
        f"<td>{int(float(row.get('score', 0)))}/100</td>"
        f'<td class="{status_class}">{_escape(status)}</td>'
        f"<td>{_price(row.get('current_price', 0.0))}</td>"
        f"<td>{float(row.get('historical_monthly_pct', 0.0) or 0.0):+.2f}%</td>"
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
    return _manual_time(str(value or "")) or _escape(str(value or "").replace("+00:00", " UTC").replace("T", " ")[:22])
