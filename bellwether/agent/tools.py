"""Tool schemas and dispatch.

The schemas are what the model sees; the dispatcher maps its requests to
the real implementations. Descriptions are effectively prompts: they
steer tool choice, so wording changes here change agent behaviour, and
they are one of the levers evaluated in eval/.

Dispatch obeys the same contract as the tools: whatever the model sends,
including nonsense, comes back as a dict with ok True or False, never an
exception.
"""

from __future__ import annotations

import json
import sqlite3

from tavily import TavilyClient

from bellwether.tools_impl import holdings, news, prices

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_portfolio",
            "description": (
                "Get a manager's US equity portfolio for one quarter, largest "
                "positions first with weights. Use this to see context around "
                "a position, such as what else the manager holds and how "
                "concentrated the book is."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cik": {"type": "integer",
                            "description": "SEC CIK number of the manager"},
                    "period": {"type": "string",
                               "description": "quarter end date, e.g. 2024-06-30"},
                },
                "required": ["cik", "period"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_position_history",
            "description": (
                "Get one security's full history in one manager's portfolio "
                "across all quarters on record: value, shares, and weight per "
                "quarter. Use this to see when a position was built, trimmed, "
                "or exited, and whether a change was sudden or gradual."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cik": {"type": "integer",
                            "description": "SEC CIK number of the manager"},
                    "cusip": {"type": "string",
                              "description": "9-character CUSIP of the security"},
                },
                "required": ["cik", "cusip"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_price_summary",
            "description": (
                "Get a stock's price move between two dates: start and end "
                "price, percent change, high, low, and any stock splits in "
                "the window. Use this to check what the market did during the "
                "filing period, and to distinguish real buying or selling "
                "from price effects and splits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string",
                               "description": "exchange ticker symbol"},
                    "start": {"type": "string",
                              "description": "start date, e.g. 2024-03-31"},
                    "end": {"type": "string",
                            "description": "end date, e.g. 2024-06-30"},
                },
                "required": ["ticker", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": (
                "Search the public web for news and information. Use this to "
                "find events that might explain a portfolio change, such as "
                "earnings, mergers, activist campaigns, or public statements. "
                "Results include URLs, which must be cited as sources for any "
                "claim taken from them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "search query, a few keywords"},
                    "max_results": {"type": "integer",
                                    "description": "number of results, default 5"},
                },
                "required": ["query"],
            },
        },
    },
]


class ToolDispatcher:
    """Routes model tool requests to implementations and counts spend."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        managers: dict[int, str],
        tavily_api_key: str,
        max_tavily_calls: int,
    ):
        self.conn = conn
        self.managers = managers
        self.tavily = TavilyClient(api_key=tavily_api_key)
        self.max_tavily_calls = max_tavily_calls
        self.tavily_calls = 0

    def dispatch(self, name: str, arguments: str) -> dict:
        """Run one tool request. arguments is the model's JSON string."""
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as e:
            return {"ok": False,
                    "error": f"arguments were not valid JSON ({e}); "
                             "resend the call with corrected JSON"}
        if not isinstance(args, dict):
            return {"ok": False,
                    "error": "arguments must be a JSON object of parameters"}

        try:
            if name == "get_portfolio":
                return holdings.get_portfolio(
                    self.conn, self.managers,
                    int(args["cik"]), str(args["period"]),
                )
            if name == "get_position_history":
                return holdings.get_position_history(
                    self.conn, self.managers,
                    int(args["cik"]), str(args["cusip"]),
                )
            if name == "get_price_summary":
                return prices.get_price_summary(
                    str(args["ticker"]), str(args["start"]), str(args["end"]),
                )
            if name == "search_news":
                if self.tavily_calls >= self.max_tavily_calls:
                    return {"ok": False,
                            "error": "the news search budget for this run is "
                                     "exhausted; work with the evidence already "
                                     "gathered, or state that the cause could "
                                     "not be established"}
                self.tavily_calls += 1
                return news.search_news(
                    self.tavily, str(args["query"]),
                    int(args.get("max_results", 5)),
                )
            return {"ok": False,
                    "error": f"unknown tool '{name}'; available tools: "
                             "get_portfolio, get_position_history, "
                             "get_price_summary, search_news"}
        except KeyError as e:
            return {"ok": False,
                    "error": f"missing required parameter {e} for tool '{name}'"}
        except (TypeError, ValueError) as e:
            return {"ok": False,
                    "error": f"bad parameter value for tool '{name}': {e}"}
