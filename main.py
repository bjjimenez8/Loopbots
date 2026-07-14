from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from active_setups import ActiveSetupConfig, ActiveSetupStore
from backtest_lab import run_interactive_backtest
from dashboard import DashboardConfig, PaperDashboardServer
from market_regime import mode_allowed
from market_data import MarketDataClient, MarketDataConfig
from news_brief import MorningBriefConfig, MorningBriefService
from opportunity import opportunity_snapshot
from opportunity_paper import OpportunityPaperConfig, OpportunityPaperTracker
from paper_tracker import PaperTracker, PaperTrackingConfig
from proof_registry import AdaptiveProofRegistry, adaptive_loop_diagnostic
from strategy import LoopStrategy, Signal
from telegram_alerts import TelegramAlertClient
from trade_manager import TradeManager


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("LOOPBOTS_CONFIG", PROJECT_ROOT / "config.yaml")).expanduser()


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def setup_logging(log_file: str) -> None:
    log_path = PROJECT_ROOT / log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


class LoopbotsApp:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.proof_registry = AdaptiveProofRegistry(PROJECT_ROOT / "adaptive_proof_registry.json")
        exchange_config = config["exchange"]
        self.discovery_config = config.get("pair_discovery", {})
        self.market_data = MarketDataClient(
            MarketDataConfig(
                exchange_id=exchange_config["id"],
                enable_rate_limit=exchange_config["enable_rate_limit"],
                sandbox=exchange_config["sandbox"],
                timeframe=exchange_config["timeframe"],
                candle_limit=exchange_config["candle_limit"],
                discovery_refresh_minutes=self.discovery_config.get("refresh_minutes", 60),
            )
        )
        self.strategies = self._build_strategies(config)
        self.trade_manager = TradeManager(
            active_trades_file=str(PROJECT_ROOT / config["storage"]["active_trades_file"]),
            trade_history_file=str(PROJECT_ROOT / config["storage"]["trade_history_file"]),
            fee_pct=float(config.get("loop_settings", {}).get("assumed_round_trip_fee_pct", 0.2)),
        )
        self.telegram = TelegramAlertClient(
            bot_token=config["telegram"]["bot_token"],
            chat_id=config["telegram"]["chat_id"],
        )
        self.telegram_enabled = False
        self.status_config = config.get("status_report", {})
        self.status_state_path = PROJECT_ROOT / self.status_config.get("state_file", "data/status_report_state.json")
        self.status_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.fallback_pairs = list(config["pairs"])
        self.pairs = list(self.fallback_pairs)
        self.loop_scan_rows: list[dict[str, Any]] = []
        self.loop_market_regime: dict[str, Any] = {
            "risk_on": False,
            "reason": "BTC market regime has not been checked yet",
        }
        self.opportunity_market_cache: dict[str, dict[str, Any]] = {}
        morning_config = config.get("morning_brief", {})
        self.morning_brief = MorningBriefService(
            exchange=self.market_data.exchange,
            pairs=self.pairs,
            config=MorningBriefConfig(
                enabled=morning_config.get("enabled", True),
                hour=morning_config.get("hour", 8),
                minute=morning_config.get("minute", 0),
                timezone=morning_config.get("timezone", config["scheduler"]["timezone"]),
                headline_count=morning_config.get("headline_count", 3),
                state_file=str(PROJECT_ROOT / morning_config.get("state_file", "data/morning_brief_state.json")),
                headline_feed_url=morning_config.get(
                    "headline_feed_url",
                    "https://www.coindesk.com/arc/outboundfeeds/rss/",
                ),
            ),
        )
        paper_config = config.get("paper_tracking", {})
        self.paper_tracker = PaperTracker(
            active_trades_file=str(PROJECT_ROOT / config["storage"]["active_trades_file"]),
            trade_history_file=str(PROJECT_ROOT / config["storage"]["trade_history_file"]),
            config=PaperTrackingConfig(
                enabled=paper_config.get("enabled", True),
                lookback_days=paper_config.get("lookback_days", 7),
                retention_days=paper_config.get("retention_days", 30),
                fee_pct=float(config.get("loop_settings", {}).get("assumed_round_trip_fee_pct", 0.2)),
            ),
        )
        active_setup_config = config.get("active_setups", {})
        self.active_setups = ActiveSetupStore(
            ActiveSetupConfig(
                state_file=str(PROJECT_ROOT / active_setup_config.get("state_file", "data/active_setups.json")),
            )
        )
        proof_config = config.get("adaptive_proof", {})
        self.proof_market_data = MarketDataClient(
            MarketDataConfig(
                exchange_id=str(proof_config.get("history_exchange", "okx")),
                enable_rate_limit=exchange_config["enable_rate_limit"],
                sandbox=False,
                timeframe="1h",
                candle_limit=1200,
                discovery_refresh_minutes=60,
            )
        )
        opportunity_paper_config = config.get("opportunity_paper", {})
        self.opportunity_paper = OpportunityPaperTracker(
            OpportunityPaperConfig(
                state_file=str(PROJECT_ROOT / opportunity_paper_config.get("state_file", "data/opportunity_paper_trades.json")),
                investment_usd=float(opportunity_paper_config.get("investment_usd", 1000.0)),
                starting_balance_usd=float(opportunity_paper_config.get("starting_balance_usd", 10000.0)),
                fee_pct=float(opportunity_paper_config.get("fee_pct", 0.40)),
            )
        )
        dashboard_config = config.get("dashboard", {})
        self.dashboard = PaperDashboardServer(
            tracker=self.paper_tracker,
            config=DashboardConfig(
                enabled=dashboard_config.get("enabled", True),
                host=dashboard_config.get("host", "127.0.0.1"),
                port=int(dashboard_config.get("port", 3000)),
                refresh_seconds=int(dashboard_config.get("refresh_seconds", 30)),
                timezone=str(dashboard_config.get("timezone", config["scheduler"]["timezone"])),
            ),
            loop_details_provider=lambda: {
                "pairs": list(self.pairs),
                "scanned": self.market_data.discovery_snapshot(),
                "entry_rows": self._sorted_loop_scan_rows(),
            },
            research_provider=self._research_snapshot,
            opportunity_provider=self._opportunity_snapshot,
            backtest_provider=lambda query: run_interactive_backtest({**query, "bot": ["loop"]}, PROJECT_ROOT),
            active_setup_provider=self._active_setups_snapshot,
            opportunity_paper_provider=self._opportunity_paper_display_snapshot,
            use_setup_handler=self._use_setup_from_form,
            finish_setup_handler=self._finish_setup_from_form,
        )

    def start_dashboard(self) -> None:
        self.dashboard.start()

    async def scan_once(self) -> None:
        self.refresh_pairs()
        self._refresh_loop_market_regime()
        logging.info("Starting scan for %d pairs", len(self.pairs))
        loop_entry_count = 0
        loop_exit_count = 0
        loop_diagnostics: list[dict[str, Any]] = []
        self.loop_scan_rows = []
        for symbol in self.pairs:
            try:
                candles = self.market_data.fetch_ohlcv(symbol)
                self._cache_opportunity_market_snapshot(symbol, candles, self.market_data.timeframe)
                active_trade = self.trade_manager.get_active_trade(symbol)

                if active_trade:
                    active_trade = self.trade_manager.update_paper_grid(symbol, candles.iloc[-1]) or active_trade
                    current_price = float(candles["close"].iloc[-1])
                    take_profit_price = float(active_trade["take_profit_price"])
                    loop_plan = (active_trade.get("loop_settings") or {}).get("loop_plan") or {}
                    take_profit_mode = str(loop_plan.get("take_profit_mode", "price")).lower()
                    total_target_pct = float(loop_plan.get("take_profit_pct") or 0.0)
                    total_net_pct = self.trade_manager.current_total_net_return_pct(active_trade, current_price)
                    take_profit_reached = (
                        total_net_pct >= total_target_pct
                        if take_profit_mode == "total_pnl" and total_target_pct > 0
                        else current_price >= take_profit_price
                    )
                    if take_profit_reached:
                        take_profit_signal = Signal(
                            "HOLD",
                            symbol=symbol,
                            price=current_price,
                            take_profit_price=take_profit_price,
                            safety_exit_price=float(active_trade["safety_exit_price"]),
                            reason="take profit reached",
                        )
                        self.trade_manager.close_trade(
                            take_profit_signal,
                            "total PnL take profit reached" if take_profit_mode == "total_pnl" else "take profit reached",
                            event="TAKE_PROFIT",
                        )
                        continue

                    exit_signal = self.strategies[0]["strategy"].analyze_exit(symbol, candles, active_trade)
                    if exit_signal.signal_type == "EXIT":
                        self.trade_manager.close_trade(exit_signal, exit_signal.reason)
                        if self.telegram_enabled:
                            await self.telegram.send_exit_alert(exit_signal)
                        loop_exit_count += 1
                    continue

                symbol_diagnostics = self._loop_diagnostics(symbol, candles)
                for profile in self.proof_registry.proven_profiles("LOOP", symbol):
                    settings = profile.get("settings") or {}
                    timeframe = str(settings.get("timeframe", "1h"))
                    lookback_days = int(settings.get("lookback_days", 45) or 45)
                    proof_candles = self.proof_market_data.fetch_ohlcv_timeframe(
                        symbol,
                        timeframe,
                        max(250, lookback_days * 24 + 24),
                    )
                    symbol_diagnostics.append(
                        adaptive_loop_diagnostic(profile, proof_candles, live_price=float(candles["close"].iloc[-1]))
                    )
                loop_diagnostics.extend(symbol_diagnostics)
                self.loop_scan_rows.append(self._apply_loop_market_gate(self._loop_scan_row(symbol, symbol_diagnostics)))
                entry_signal = self._analyze_entry(symbol, candles)
                if entry_signal.signal_type == "ENTER":
                    opened_trade = self.trade_manager.open_trade(entry_signal)
                    if opened_trade:
                        if self.telegram_enabled:
                            await self.telegram.send_enter_alert(entry_signal)
                        loop_entry_count += 1
            except Exception:
                logging.exception("Failed to scan %s", symbol)

        logging.info("Scan complete")
        total_alerts = loop_entry_count + loop_exit_count
        if self.telegram_enabled and total_alerts == 0:
            await self.maybe_send_no_alert_status(loop_diagnostics)
        self._auto_track_opportunity_paper()
        self._opportunity_paper_snapshot(refresh=True)
        self._prune_paper_history()

    async def maybe_send_no_alert_status(self, loop_diagnostics: list[dict[str, Any]]) -> None:
        if not self.telegram_enabled:
            return
        if not self.status_config.get("enabled", True):
            return
        hour = int(self.status_config.get("hour", 20))
        minute = int(self.status_config.get("minute", 0))
        timezone = str(self.status_config.get("timezone", "America/Los_Angeles"))
        if not self._status_report_due(hour, minute, timezone):
            return

        message = self._build_no_alert_status(loop_diagnostics)
        await self.telegram.send_status_report(message)
        self._mark_status_report_sent(timezone)

    def _status_report_due(self, hour: int, minute: int, timezone: str) -> bool:
        local_now = datetime.now(ZoneInfo(timezone))
        target_time = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if local_now < target_time:
            return False

        local_date = local_now.date().isoformat()
        try:
            state = json.loads(self.status_state_path.read_text(encoding="utf-8")) if self.status_state_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            return True
        return state.get("last_sent_date") != local_date

    def _mark_status_report_sent(self, timezone: str) -> None:
        local_now = datetime.now(ZoneInfo(timezone))
        self.status_state_path.write_text(
            json.dumps(
                {
                    "last_sent_at": datetime.now(UTC).isoformat(),
                    "last_sent_date": local_now.date().isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _build_no_alert_status(self, loop_diagnostics: list[dict[str, Any]]) -> str:
        closest_loop = sorted(loop_diagnostics, key=lambda row: row.get("score", 0), reverse=True)[:3]
        lines = [
            "BOT STATUS",
            "No entries this scan.",
            "Why: waiting for cleaner setup.",
            f"Closest LOOP: {self._format_status_names(closest_loop, max_score=80)}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _format_status_names(rows: list[dict[str, Any]], max_score: int) -> str:
        names = [
            f"{row.get('symbol', 'n/a')} {max(0, min(100, round((float(row.get('score', 0)) / max_score) * 100)))}/100"
            for row in rows[:2]
        ]
        return ", ".join(names) if names else "none"

    @staticmethod
    def _format_loop_closest(rows: list[dict[str, Any]]) -> list[str]:
        if not rows:
            return ["- none"]
        return [
            f"- {row.get('symbol', 'n/a')} {row.get('score', 0)}/80 needs {row.get('reason', 'cleaner setup')}"
            for row in rows
        ]

    def refresh_pairs(self) -> None:
        if not self.discovery_config.get("enabled", False):
            self.pairs = self._merge_pairs(self.proof_registry.proven_symbols("LOOP"), self.fallback_pairs)
            return

        try:
            discovered_pairs = self.market_data.discover_pairs(self.discovery_config)
            self.pairs = self._merge_pairs(
                [*discovered_pairs, *self.proof_registry.proven_symbols("LOOP")],
                self.fallback_pairs,
            )
        except Exception:
            logging.exception("Failed to refresh discovered pairs, falling back to configured list")
            self.pairs = self._merge_pairs(self.proof_registry.proven_symbols("LOOP"), self.fallback_pairs)

    @staticmethod
    def _merge_pairs(primary_pairs: list[str], fallback_pairs: list[str]) -> list[str]:
        merged_pairs = []
        for symbol in [*primary_pairs, *fallback_pairs]:
            if symbol not in merged_pairs:
                merged_pairs.append(symbol)
        return merged_pairs

    async def send_morning_brief(self) -> None:
        if not self.telegram_enabled:
            return
        if not self.morning_brief.config.enabled:
            return

        local_now = datetime.now(ZoneInfo(self.morning_brief.config.timezone))
        local_date = local_now.date().isoformat()
        if not self.morning_brief.should_send_today(local_date):
            return

        try:
            message = self.morning_brief.build_brief()
            await self.telegram.send_morning_brief(message)
            self.morning_brief.mark_sent(local_date)
            logging.info("Morning brief sent for %s", local_date)
        except Exception:
            logging.exception("Failed to send morning brief")

    def _prune_paper_history(self) -> None:
        if not self.paper_tracker.config.enabled:
            return
        removed_count = self.trade_manager.prune_history(self.paper_tracker.config.retention_days)
        if removed_count:
            logging.info("Pruned %d old paper history rows", removed_count)

    def _analyze_entry(self, symbol: str, candles: Any) -> Signal:
        if not bool(self.loop_market_regime.get("risk_on")):
            return Signal(
                "HOLD",
                symbol=symbol,
                price=float(candles["close"].iloc[-1]),
                reason=str(self.loop_market_regime.get("reason") or "BTC market regime is not risk-on"),
            )
        entry_candidates: list[Signal] = []
        for strategy_mode in self.strategies:
            if not mode_allowed(strategy_mode["mode"], candles, symbol):
                continue
            strategy = strategy_mode["strategy"]
            signal = strategy.analyze_entry(symbol, candles)
            if signal.signal_type == "ENTER":
                entry_candidates.append(signal)

        if entry_candidates:
            return max(entry_candidates, key=self._entry_score)
        return Signal("HOLD", symbol=symbol, price=float(candles["close"].iloc[-1]), reason="no entry setup")

    @staticmethod
    def _loop_scan_row(symbol: str, diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
        if not diagnostics:
            return {
                "symbol": symbol,
                "entry_score": 0,
                "status": "Waiting",
                "mode": "",
                "price": 0.0,
                "reason": "not checked",
            }
        best = max(diagnostics, key=lambda row: row.get("score", 0))
        ready = best.get("reason") == "READY"
        entry_score = max(0, min(100, round((float(best.get("score", 0)) / 80) * 100)))
        if ready:
            entry_score = 100
        else:
            entry_score = min(entry_score, 99)
        return {
            "symbol": symbol,
            "entry_score": entry_score,
            "status": "Ready" if ready else "Waiting",
            "mode": best.get("mode", ""),
            "price": float(best.get("price") or 0.0),
            "reason": best.get("reason", ""),
            "order_distance_pct": best.get("order_distance_pct", ""),
            "order_count": best.get("order_count", ""),
            "entry_zone_low": best.get("entry_zone_low", ""),
            "entry_zone_high": best.get("entry_zone_high", ""),
            "take_profit_price": best.get("take_profit_price", ""),
            "safety_exit_price": best.get("safety_exit_price", ""),
            "target_tier": best.get("target_tier", ""),
            "strong_momentum": best.get("strong_momentum", False),
            "take_profit_mode": best.get("take_profit_mode", ""),
            "take_profit_pct": best.get("take_profit_pct", ""),
            "monitored_stop_loss_pct": best.get("monitored_stop_loss_pct", ""),
            "timeframe": best.get("timeframe", ""),
        }

    def _sorted_loop_scan_rows(self) -> list[dict[str, Any]]:
        return sorted(
            self.loop_scan_rows,
            key=lambda row: (
                row.get("status") != "Ready",
                -int(row.get("entry_score", 0)),
                row.get("symbol", ""),
            ),
        )

    def _research_snapshot(self) -> dict[str, Any]:
        loop_rows = [self._with_loop_setup(row) for row in self._sorted_loop_scan_rows()]
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "scan_interval_minutes": int(self.config.get("scheduler", {}).get("interval_minutes", 15)),
            "loop": {
                "timeframe": self.config.get("exchange", {}).get("timeframe", ""),
                "quote_asset": self.discovery_config.get("quote_asset", "USDT"),
                "scanned_count": len(loop_rows),
                "ready_count": sum(1 for row in loop_rows if row.get("status") == "Ready"),
                "top_live": loop_rows[:8],
                "proof": self._loop_research_proof(),
                "paper": self.paper_tracker.snapshot()["window_stats"],
            },
        }

    def _opportunity_snapshot(self, query: dict[str, list[str]]) -> dict[str, Any]:
        if self._query_value(query, "scan", "") == "now":
            self._dashboard_scan_now()
        loop_rows = [self._with_loop_setup(row) for row in self._sorted_loop_scan_rows()]
        horizon_filter = self._query_value(query, "horizon", "all")
        if horizon_filter not in {"all", "short", "mid", "long"}:
            horizon_filter = "all"
        strategy_filter = "loop"
        speed_filter = "all" if horizon_filter == "all" else self._horizon_to_speed(horizon_filter)
        snapshot = opportunity_snapshot(
            loop_rows=loop_rows,
            loop_proof_rows=self._loop_research_proof(),
            strategy_filter=strategy_filter,
            status_filter=self._query_value(query, "status", "all"),
            risk_filter="all",
            speed_filter=speed_filter,
        )
        if speed_filter == "all" and not any(item.get("status") == "Ready Now" for item in snapshot.get("opportunities", [])):
            snapshot = opportunity_snapshot(
                loop_rows=loop_rows,
                loop_proof_rows=self._loop_research_proof(),
                strategy_filter=strategy_filter,
                status_filter=self._query_value(query, "status", "all"),
                risk_filter="all",
                speed_filter="all",
            )
        snapshot.setdefault("filters", {})["horizon"] = horizon_filter
        snapshot["filters"]["speed"] = speed_filter
        snapshot["filters"]["risk"] = "all"
        paper_filter = self._query_value(query, "paper", "all")
        snapshot["filters"]["paper"] = "loop"
        self._attach_market_snapshots(snapshot, horizon_filter)
        self._track_ready_opportunity_paper(snapshot)
        return snapshot

    def _dashboard_scan_now(self) -> None:
        try:
            self.refresh_pairs()
            self._refresh_loop_market_regime()
            self.loop_scan_rows = []
            for symbol in self.pairs:
                try:
                    candles = self.market_data.fetch_ohlcv(symbol)
                    self._cache_opportunity_market_snapshot(symbol, candles, self.market_data.timeframe)
                    diagnostics = self._loop_diagnostics(symbol, candles)
                    self.loop_scan_rows.append(self._apply_loop_market_gate(self._loop_scan_row(symbol, diagnostics)))
                except Exception:
                    logging.exception("Dashboard scan failed for LOOP %s", symbol)
            logging.info("Dashboard Scan Kraken Now completed")
        except Exception:
            logging.exception("Dashboard Scan Kraken Now failed")

    def _refresh_loop_market_regime(self) -> None:
        try:
            candles = self.market_data.fetch_ohlcv_timeframe("BTC/USDT", "1h", 720)
            close = candles["close"].astype(float)
            if len(close) < 600:
                raise RuntimeError(f"only {len(close)} BTC hourly candles available")
            ema_50 = close.ewm(span=50, adjust=False).mean()
            ema_200 = close.ewm(span=200, adjust=False).mean()
            return_30d_pct = ((float(close.iloc[-1]) / float(close.iloc[0])) - 1) * 100
            ema_200_rising = float(ema_200.iloc[-1]) > float(ema_200.iloc[-72])
            risk_on = bool(
                float(close.iloc[-1]) > float(ema_50.iloc[-1]) > float(ema_200.iloc[-1])
                and return_30d_pct > 0
                and ema_200_rising
            )
            reason = (
                "BTC 30-day regime is risk-on"
                if risk_on
                else "BTC 30-day regime is not risk-on; LOOP entries are paused"
            )
            self.loop_market_regime = {
                "risk_on": risk_on,
                "reason": reason,
                "return_30d_pct": round(return_30d_pct, 2),
                "checked_at": datetime.now(UTC).isoformat(),
            }
            logging.info("%s (30-day return %.2f%%)", reason, return_30d_pct)
        except Exception as exc:
            self.loop_market_regime = {
                "risk_on": False,
                "reason": "BTC market regime unavailable; LOOP entries are paused",
                "error": exc.__class__.__name__,
                "checked_at": datetime.now(UTC).isoformat(),
            }
            logging.exception("Failed to refresh BTC LOOP market regime")

    def _apply_loop_market_gate(self, row: dict[str, Any]) -> dict[str, Any]:
        if bool(self.loop_market_regime.get("risk_on")):
            return row
        return {
            **row,
            "status": "Waiting",
            "entry_score": min(int(row.get("entry_score", 0) or 0), 69),
            "reason": str(self.loop_market_regime.get("reason") or "BTC market regime is not risk-on"),
        }

    def _auto_track_opportunity_paper(self) -> None:
        considered = 0
        for horizon in ("short", "mid", "long"):
            try:
                snapshot = self._opportunity_snapshot({"strategy": ["loop"], "horizon": [horizon]})
            except Exception:
                logging.exception("Failed to auto-track LOOP %s opportunity paper", horizon)
                continue
            considered += sum(1 for item in snapshot.get("opportunities", []) if self._is_customer_ready_opportunity(item))
        if considered:
            logging.info("Opportunity paper auto-tracked/confirmed %d Ready Now setups", considered)

    def _track_ready_opportunity_paper(self, snapshot: dict[str, Any]) -> int:
        count = 0
        for item in snapshot.get("opportunities", []):
            if not self._is_customer_ready_opportunity(item):
                continue
            self.opportunity_paper.add_from_opportunity(item)
            count += 1
        return count

    @staticmethod
    def _is_customer_ready_opportunity(item: dict[str, Any]) -> bool:
        if str(item.get("status", "")) != "Ready Now":
            return False
        fields = item.get("bitsgap_fields", {})
        if not isinstance(fields, dict):
            return False
        strategy = str(item.get("strategy", "")).upper()
        if strategy == "LOOP":
            required = ["Order distance", "Order count", "Take profit", "Stop loss"]
            minimum_score = 70
        else:
            return False
        for key in required:
            if key == "Stop loss":
                value = fields.get("Stop loss") or fields.get("Safety exit / stop guidance")
            else:
                value = fields.get(key)
            if value in {"", None, "n/a", "Needs live price"}:
                return False
        try:
            score = int(float(item.get("score", 0) or 0))
        except (TypeError, ValueError):
            score = 0
        return score >= minimum_score

    def _attach_market_snapshots(self, snapshot: dict[str, Any], horizon: str) -> None:
        bars = {"short": 24, "mid": 72, "long": 168}.get(horizon, 24)
        for item in snapshot.get("opportunities", []):
            symbol = str(item.get("pair", ""))
            if not symbol:
                continue
            cached = self.opportunity_market_cache.get(symbol)
            if cached:
                closes = list(cached.get("closes", []))[-bars:]
                timestamps = list(cached.get("timestamps", []))[-bars:]
                if closes:
                    first = float(closes[0])
                    current = float(closes[-1])
                    change_pct = ((current / first) - 1) * 100 if first > 0 else 0.0
                    item["market_snapshot"] = {
                        "current_price": current,
                        "change_pct": round(change_pct, 2),
                        "closes": closes[-48:],
                        "timeframe": cached.get("timeframe", ""),
                        "updated_at": timestamps[-1] if timestamps else cached.get("updated_at", ""),
                    }
                    continue
            current = self._opportunity_current_price(item)
            if current:
                item["market_snapshot"] = {
                    "current_price": current,
                    "change_pct": 0.0,
                    "closes": [current],
                    "timeframe": self.market_data.timeframe,
                    "updated_at": snapshot.get("generated_at", datetime.now(UTC).isoformat()),
                }

    def _cache_opportunity_market_snapshot(self, symbol: str, candles: Any, timeframe: str) -> None:
        try:
            window = candles.tail(min(len(candles), 168))
            if window.empty:
                return
            self.opportunity_market_cache[str(symbol)] = {
                "closes": [float(value) for value in window["close"].tolist()],
                "timestamps": [value.isoformat() for value in window["timestamp"].tolist()],
                "timeframe": timeframe,
                "updated_at": window["timestamp"].iloc[-1].isoformat(),
            }
        except Exception:
            logging.exception("Failed to cache market snapshot for %s", symbol)

    @staticmethod
    def _opportunity_current_price(item: dict[str, Any]) -> float | None:
        for key in ("price", "current_price"):
            try:
                value = float(item.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        return None

    def _use_setup_from_form(self, form: dict[str, list[str]]) -> None:
        opportunity_id = self._raw_form_value(form, "opportunity_id")
        if not opportunity_id:
            return
        snapshot = self._opportunity_snapshot(form)
        for item in snapshot.get("opportunities", []):
            if item.get("id") == opportunity_id:
                self.active_setups.add_from_opportunity(item)
                self.opportunity_paper.add_from_opportunity(item)
                logging.info("Saved active manual setup %s %s", item.get("strategy"), item.get("pair"))
                return
        logging.warning("Opportunity id %s was not found for active setup save", opportunity_id)

    def _finish_setup_from_form(self, form: dict[str, list[str]]) -> None:
        setup_id = self._raw_form_value(form, "setup_id")
        if setup_id and self.active_setups.finish(setup_id):
            logging.info("Finished active manual setup %s", setup_id)

    def _active_setups_snapshot(self) -> dict[str, Any]:
        return self._loop_only_snapshot(self.active_setups.snapshot(self._active_setup_candles))

    def _opportunity_paper_display_snapshot(self) -> dict[str, Any]:
        return self._opportunity_paper_snapshot(refresh=False)

    def _opportunity_paper_snapshot(self, refresh: bool = True) -> dict[str, Any]:
        return self._loop_only_snapshot(self.opportunity_paper.snapshot(self._active_setup_candles, refresh=refresh))

    def _active_setup_candles(self, setup: dict[str, Any]) -> Any:
        symbol = str(setup.get("pair", ""))
        return self.market_data.fetch_ohlcv(symbol)

    @staticmethod
    def _loop_only_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        filtered = dict(snapshot)
        for key in ("active", "finished", "open", "closed"):
            if isinstance(snapshot.get(key), list):
                filtered[key] = [row for row in snapshot[key] if str(row.get("strategy", "")).upper() == "LOOP"]
        return filtered

    @staticmethod
    def _raw_form_value(form: dict[str, list[str]], key: str) -> str:
        values = form.get(key)
        if not values:
            return ""
        return str(values[0]).strip()

    @staticmethod
    def _query_value(query: dict[str, list[str]], key: str, default: str) -> str:
        values = query.get(key)
        if not values:
            return default
        value = str(values[0]).strip().lower()
        return value or default

    def _legacy_horizon(self, query: dict[str, list[str]]) -> str:
        speed = self._query_value(query, "speed", "short")
        return {"fast": "short", "medium": "mid", "slow": "long"}.get(speed, "short")

    @staticmethod
    def _horizon_to_speed(horizon: str) -> str:
        return {"short": "fast", "mid": "medium", "long": "slow"}.get(horizon, "fast")

    def _customer_strategy_filter(self, query: dict[str, list[str]]) -> str:
        strategy = self._query_value(query, "strategy", "both")
        return "loop"

    def _with_loop_setup(self, row: dict[str, Any]) -> dict[str, Any]:
        setup = self._loop_setup_by_mode().get(str(row.get("mode", "")), {})
        return {
            **row,
            "preset_name": setup.get("preset_name", row.get("mode", "")),
            "method_name": setup.get("method_name", "Trend pullback"),
            "order_distance_pct": row.get("order_distance_pct") or setup.get("order_distance_pct", ""),
            "order_count": row.get("order_count") or setup.get("order_count", ""),
        }

    def _loop_setup_by_mode(self) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for mode in self.config.get("strategy_modes", []):
            loop_settings = dict(self.config.get("loop_settings", {}))
            loop_settings.update(mode.get("loop_settings", {}))
            rows[str(mode.get("name", ""))] = {
                "preset_name": loop_settings.get("preset_name", mode.get("name", "")),
                "method_name": loop_settings.get("method_name", "Trend pullback"),
                "order_distance_pct": loop_settings.get("order_distance_pct", ""),
                "order_count": loop_settings.get("order_count", ""),
            }
        return rows

    def _loop_research_proof(self) -> list[dict[str, Any]]:
        return self.proof_registry.research_rows("LOOP")

    def _loop_diagnostics(self, symbol: str, candles: Any) -> list[dict[str, Any]]:
        results = []
        for strategy_mode in self.strategies:
            dynamic_mode = self._dynamic_scan_mode(strategy_mode["mode"])
            if not mode_allowed(dynamic_mode, candles, symbol):
                continue
            strategy = strategy_mode["strategy"]
            try:
                results.append(self._loop_strategy_diagnostic(symbol, candles, strategy_mode, strategy))
            except Exception:
                logging.exception("Failed to build LOOP diagnostics for %s", symbol)
        return results

    @staticmethod
    def _dynamic_scan_mode(mode: dict[str, Any]) -> dict[str, Any]:
        dynamic_mode = dict(mode)
        dynamic_mode.pop("allowed_base_assets", None)
        return dynamic_mode

    @staticmethod
    def _loop_strategy_diagnostic(symbol: str, candles: Any, strategy_mode: dict[str, Any], strategy: LoopStrategy) -> dict[str, Any]:
        df = strategy._with_indicators(candles)
        if len(df) < strategy._minimum_candles:
            return {"symbol": symbol, "mode": strategy_mode["mode"].get("name", ""), "score": 0, "reason": "not enough data"}

        latest = df.iloc[-1]
        previous = df.iloc[-2]
        recent = df.iloc[-strategy.config["pullback_lookback"] :]
        range_window = df.iloc[-strategy._range_lookback :]
        price = float(latest["close"])
        atr = float(latest["atr"])
        range_low = float(range_window["low"].min())
        range_high = float(range_window["high"].max())
        range_span = max(range_high - range_low, 0.0)
        range_position = ((price - range_low) / range_span) if range_span else 1.0
        range_pct = ((range_high / range_low) - 1) * 100 if range_low > 0 else 0.0
        profile = strategy._symbol_profile(symbol)

        trend_ok = latest["ema_fast"] > latest["ema_slow"] > latest["ema_trend"] and latest["ema_trend"] > previous["ema_trend"]
        price_reclaimed_fast_ema = latest["close"] > latest["ema_fast"] * profile["ema_reclaim_buffer"]
        recent_high = float(recent["high"].max())
        pullback_pct = (recent_high - price) / recent_high if recent_high else 0.0
        pullback_ok = strategy.config["pullback_min_pct"] <= pullback_pct <= strategy.config["pullback_max_pct"]
        bounce_ok = (
            latest["close"] >= latest["low"] * (1 + (strategy.config["bounce_confirmation_pct"] * profile["bounce_multiplier"]))
            and latest["close"] >= previous["close"] * profile["previous_close_buffer"]
            and latest["close"] >= latest["open"] * profile["open_buffer"]
        )
        rsi_ok = (
            (strategy.config["min_rsi"] - profile["rsi_low_buffer"])
            <= latest["rsi"]
            <= (strategy.config["max_rsi"] + profile["rsi_high_buffer"])
        )
        volume_ok = latest["volume_ratio"] >= max(strategy.config["min_volume_ratio"] - profile["volume_buffer"], 0.6)
        breakdown_ok = strategy._breakdown_ok(df)
        strong_momentum = strategy._strong_momentum(latest, previous, trend_ok, price_reclaimed_fast_ema)
        loop_plan = strategy._build_loop_plan(range_window, price, atr, strong_momentum=strong_momentum)
        loop_ready = bool(loop_plan) and strategy._loop_ready(loop_plan, price, range_position, profile)
        score = strategy._setup_score(
            latest=latest,
            trend_ok=trend_ok,
            price_reclaimed_fast_ema=price_reclaimed_fast_ema,
            pullback_ok=pullback_ok,
            bounce_ok=bounce_ok,
            rsi_ok=rsi_ok,
            volume_ok=volume_ok,
            loop_plan=loop_plan,
            range_position=range_position,
        )
        failures = [
            label
            for label, passed in {
                "trend": trend_ok,
                "EMA reclaim": price_reclaimed_fast_ema,
                "pullback": pullback_ok,
                "bounce": bounce_ok,
                "RSI": rsi_ok,
                "volume": volume_ok,
                "breakdown": breakdown_ok,
                "range/TP": loop_ready,
            }.items()
            if not passed
        ]
        loop_plan = loop_plan or {}
        return {
            "symbol": symbol,
            "mode": strategy_mode["mode"].get("name", ""),
            "score": score,
            "price": price,
            "reason": ", ".join(failures) if failures else "READY",
            "raw_score": score,
            "range_position": round(range_position, 3),
            "volatility": round(range_pct, 2),
            "volume_ratio": round(float(latest["volume_ratio"]), 3),
            "trend_regime": "uptrend" if trend_ok else "not uptrend",
            "trend_ok": trend_ok,
            "ema_reclaim_ok": price_reclaimed_fast_ema,
            "pullback_ok": pullback_ok,
            "bounce_ok": bounce_ok,
            "rsi_ok": rsi_ok,
            "volume_ok": volume_ok,
            "breakdown_ok": breakdown_ok,
            "range_tp_ok": loop_ready,
            "fee_impact_pct": "",
            "order_distance_pct": loop_plan.get("order_distance_pct", ""),
            "order_count": loop_plan.get("order_count", ""),
            "entry_zone_low": loop_plan.get("low_price", ""),
            "entry_zone_high": loop_plan.get("high_price", ""),
            "take_profit_price": loop_plan.get("take_profit_price", ""),
            "safety_exit_price": loop_plan.get("safety_exit_price", ""),
            "target_tier": loop_plan.get("target_tier", ""),
            "strong_momentum": loop_plan.get("strong_momentum", False),
            "take_profit_mode": loop_plan.get("take_profit_mode", ""),
            "take_profit_pct": loop_plan.get("take_profit_pct", ""),
            "monitored_stop_loss_pct": loop_plan.get("monitored_stop_loss_pct", ""),
            "timeframe": strategy_mode.get("timeframe", ""),
        }

    @staticmethod
    def _entry_score(signal: Signal) -> tuple[float, float, float]:
        loop_plan = (signal.loop_settings or {}).get("loop_plan", {})
        setup_score = float(loop_plan.get("setup_score") or 0.0)
        reward_to_risk = float(loop_plan.get("reward_to_risk") or 0.0)
        order_distance_pct = float(loop_plan.get("order_distance_pct") or 0.0)
        return setup_score, reward_to_risk, order_distance_pct

    def _build_strategies(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        strategy_modes = config.get("strategy_modes") or self._default_strategy_modes()
        strategies: list[dict[str, Any]] = []
        for mode in strategy_modes:
            if mode.get("enabled") is False:
                continue
            strategy_config = deepcopy(config["strategy"])
            strategy_config.update(mode.get("strategy_overrides", {}))
            loop_settings = deepcopy(config["loop_settings"])
            loop_settings.update(mode.get("loop_settings", {}))
            strategies.append(
                {
                    "mode": mode,
                    "strategy": LoopStrategy(strategy_config, loop_settings),
                    "timeframe": config.get("exchange", {}).get("timeframe", ""),
                }
            )
        return strategies

    @staticmethod
    def _default_strategy_modes() -> list[dict[str, Any]]:
        return [
            {
                "name": "short",
                "market_type": "sideways",
                "allowed_base_assets": ["DOGE", "LINK", "SOL"],
                "market_type_rules": {
                    "lookback": 96,
                    "min_range_width_pct": 3.0,
                    "max_range_width_pct": 9.0,
                    "max_ema_slope_pct": 0.7,
                    "min_support_touches": 4,
                    "min_resistance_touches": 4,
                    "min_range_position": 0.2,
                    "max_range_position": 0.6,
                },
                "strategy_overrides": {
                    "pullback_lookback": 5,
                    "pullback_max_pct": 0.028,
                    "bounce_confirmation_pct": 0.0012,
                    "min_volume_ratio": 0.8,
                    "max_active_minutes": 240,
                },
                "loop_settings": {
                    "preset_name": "Short-term",
                    "order_distance_pct": 1.0,
                    "order_count": 10,
                },
            },
            {
                "name": "mid",
                "market_type": "any",
                "strategy_overrides": {
                    "pullback_lookback": 6,
                    "pullback_max_pct": 0.035,
                    "bounce_confirmation_pct": 0.0015,
                    "min_volume_ratio": 0.85,
                    "max_active_minutes": 180,
                },
                "loop_settings": {
                    "preset_name": "Mid-term",
                    "order_distance_pct": 1.5,
                    "order_count": 10,
                },
            },
        ]


async def main() -> None:
    config = load_config()
    setup_logging(config["storage"]["log_file"])
    app = LoopbotsApp(config)
    if app.strategies:
        strategy = app.strategies[0]["strategy"]
        logging.info(
            "LOOP targets loaded: normal %.1f-%.1f%%, momentum %.1f-%.1f%%, monitored stop %.1f-%.1f%%",
            strategy.normal_take_profit_min_pct,
            strategy.normal_take_profit_max_pct,
            strategy.momentum_take_profit_min_pct,
            strategy.momentum_take_profit_max_pct,
            strategy.monitored_stop_loss_min_pct,
            strategy.monitored_stop_loss_max_pct,
        )

    scheduler = AsyncIOScheduler(timezone=config["scheduler"]["timezone"])
    scheduler.add_job(
        app.scan_once,
        trigger=IntervalTrigger(minutes=config["scheduler"]["interval_minutes"]),
        id="loopbots_scan",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    if app.telegram_enabled:
        scheduler.add_job(
            app.send_morning_brief,
            trigger=CronTrigger(
                hour=app.morning_brief.config.hour,
                minute=app.morning_brief.config.minute,
                timezone=app.morning_brief.config.timezone,
            ),
            id="loopbots_morning_brief",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    scheduler.start()

    app.start_dashboard()
    await app.scan_once()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
