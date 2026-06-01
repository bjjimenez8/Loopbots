from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class MorningBriefConfig:
    enabled: bool
    hour: int
    minute: int
    timezone: str
    headline_count: int
    state_file: str
    headline_feed_url: str


class MorningBriefService:
    def __init__(self, exchange: Any, pairs: list[str], config: MorningBriefConfig) -> None:
        self.exchange = exchange
        self.pairs = pairs
        self.config = config
        self.state_path = Path(config.state_file)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_state_file()

    def should_send_today(self, local_date: str) -> bool:
        state = self._load_state()
        return state.get("last_sent_date") != local_date

    def mark_sent(self, local_date: str) -> None:
        self.state_path.write_text(json.dumps({"last_sent_date": local_date}, indent=2), encoding="utf-8")

    def build_brief(self) -> str:
        snapshot = self._market_snapshot()
        headlines = self._fetch_headlines()

        lines = [
            "Good morning.",
            "",
            "Morning Crypto Brief",
            snapshot["mood"],
            "",
            "Market check:",
        ]
        for line in snapshot["lines"]:
            lines.append(line)

        if headlines:
            lines.extend(["", "Headlines:"])
            for index, headline in enumerate(headlines, start=1):
                lines.append(f"{index}. {headline}")

        lines.extend(
            [
                "",
                "Take it easy and wait for clean setups.",
            ]
        )
        return "\n".join(lines)

    def _market_snapshot(self) -> dict[str, Any]:
        major_pairs = [pair for pair in self.pairs if pair in {"BTC/USDT", "ETH/USDT", "SOL/USDT"}]
        if not major_pairs:
            major_pairs = self.pairs[:3]

        lines: list[str] = []
        green_count = 0
        for pair in major_pairs:
            ticker = self.exchange.fetch_ticker(pair)
            last_price = float(ticker.get("last") or 0.0)
            open_price = float(ticker.get("open") or last_price or 1.0)
            pct_change = ((last_price / open_price) - 1) * 100 if open_price else 0.0
            if pct_change >= 0:
                green_count += 1
            lines.append(f"- {pair}: {last_price:.4f} ({pct_change:+.2f}% 24h)")

        mood = "Mood: mostly green and steady." if green_count >= 2 else "Mood: mixed tape, stay patient."
        return {"mood": mood, "lines": lines}

    def _fetch_headlines(self) -> list[str]:
        request = Request(
            self.config.headline_feed_url,
            headers={"User-Agent": "Loopbots/1.0"},
        )
        with urlopen(request, timeout=15) as response:
            body = response.read()

        root = ET.fromstring(body)
        headlines: list[str] = []
        for item in root.findall(".//item"):
            title = item.findtext("title")
            if not title:
                continue
            headlines.append(title.strip())
            if len(headlines) >= self.config.headline_count:
                break
        return headlines

    def _ensure_state_file(self) -> None:
        if not self.state_path.exists():
            self.state_path.write_text(json.dumps({"last_sent_date": ""}, indent=2), encoding="utf-8")

    def _load_state(self) -> dict[str, str]:
        with self.state_path.open("r", encoding="utf-8") as file:
            return json.load(file)
