#!/usr/bin/env python3
"""
Token & Cost Estimator

A stdlib-only CLI that reads a JSONL file of request/response strings,
estimates token counts using a simple whitespace/word heuristic, and
computes cost against a user-supplied price card (per 1K input/output tokens).

No third-party dependencies — Python standard library only.
"""

import argparse
import json
import sys
from typing import Iterator, NamedTuple


# ----------------------------------------------------------------------
# Token estimation
# ----------------------------------------------------------------------

# Rough ratio of bytes-per-token for mixed English text.
# 4 bytes/char * ~4 chars/word / ~1.3 tokens/word ≈ 3.08 → round up.
BYTES_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """
    Estimate token count using a byte-length heuristic.

    Approach:
      - Split on whitespace, count words (1 token each).
      - Add overhead: ~2 tokens per message (role, framing) + 1 per newline.
      - Cap per-word at 3 tokens (handles long technical words).
    This matches the ratio used internally (bytes // 4) but is more interpretable.

    Returns estimated token count (minimum 1).
    """
    if not text:
        return 1

    words = text.split()
    if not words:
        return 1

    # Base: 1 token per word, +2 for message overhead, +1 per newline.
    overhead = 2 + text.count("\n")
    per_word = sum(min(len(w), 10) // 4 + 1 for w in words)

    return max(overhead + per_word, 1)


# ----------------------------------------------------------------------
# Price card
# ----------------------------------------------------------------------

class PriceCard(NamedTuple):
    """Pricing for a single model (per 1 000 tokens)."""
    input_cost: float
    output_cost: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        """Return total cost in dollars."""
        return (input_tokens / 1000 * self.input_cost +
                output_tokens / 1000 * self.output_cost)


DEFAULT_PRICES: dict[str, PriceCard] = {
    "gpt-4o":      PriceCard(input_cost=0.005, output_cost=0.015),
    "gpt-4o-mini": PriceCard(input_cost=0.00015, output_cost=0.0006),
    "claude-3-5-sonnet": PriceCard(input_cost=0.003, output_cost=0.015),
}


# ----------------------------------------------------------------------
# JSONL parsing
# ----------------------------------------------------------------------

def parse_jsonl(path: str) -> Iterator[dict]:
    """Yield one dict per line from a UTF-8 JSONL file."""
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\n\r")
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {lineno}: {exc}") from exc


# ----------------------------------------------------------------------
# Core estimator
# ----------------------------------------------------------------------

class Estimate(NamedTuple):
    line: int
    request_tokens: int
    response_tokens: int
    input_cost: float
    output_cost: float
    total_cost: float


def estimate_entry(entry: dict, price: PriceCard, *, line: int = 0) -> Estimate:
    """
    Estimate tokens and cost for a single JSONL entry.

    Expected fields (all optional, defaults to empty string):
      request / prompt  – the input text
      response / completion / output – the output text
    """
    # Accept either field name convention.
    request_text = entry.get("request") or entry.get("prompt") or ""
    response_text = (
        entry.get("response")
        or entry.get("completion")
        or entry.get("output")
        or ""
    )

    req_tok = estimate_tokens(request_text)
    res_tok = estimate_tokens(response_text)

    req_cost = req_tok / 1000 * price.input_cost
    res_cost = res_tok / 1000 * price.output_cost

    return Estimate(
        line=line,
        request_tokens=req_tok,
        response_tokens=res_tok,
        input_cost=req_cost,
        output_cost=res_cost,
        total_cost=req_cost + res_cost,
    )


def process_jsonl(jsonl_path: str, price: PriceCard) -> Iterator[Estimate]:
    """Yield Estimate for every valid JSONL entry."""
    for lineno, entry in enumerate(parse_jsonl(jsonl_path), start=1):
        yield estimate_entry(entry, price, line=lineno)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

SUMMARY_FMT = (
    "{line:>5}  {req_tok:>6} in  {res_tok:>6} out  "
    "{req_cost:>9}  {res_cost:>9}  {total:>9}\n"
)
SUMMARY_HEADER = (
    f"{'L':>5}  {'IN_TOK':>6}  {'OUT_TOK':>6}  "
    f"{'IN_$':>9}  {'OUT_$':>9}  {'TOTAL_$':>9}\n"
    + "-" * 58
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="token-cost-estimator",
        description="Estimate token counts and costs from a JSONL log file.",
    )
    parser.add_argument(
        "jsonl", help="Path to JSONL file with request/response text fields."
    )
    parser.add_argument(
        "--model",
        choices=list(DEFAULT_PRICES),
        default="gpt-4o",
        help="Model pricing to apply (default: gpt-4o).",
    )
    parser.add_argument(
        "--input-price",
        type=float,
        metavar="DOLLARS",
        dest="input_price",
        help="Override input cost per 1 K tokens (USD).",
    )
    parser.add_argument(
        "--output-price",
        type=float,
        metavar="DOLLARS",
        dest="output_price",
        help="Override output cost per 1 K tokens (USD).",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show per-line summary table.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve price card.
    if args.input_price is not None or args.output_price is not None:
        price = PriceCard(
            input_cost=args.input_price or 0.0,
            output_cost=args.output_price or 0.0,
        )
    else:
        price = DEFAULT_PRICES[args.model]

    # Process.
    try:
        estimates = list(process_jsonl(args.jsonl, price))
    except FileNotFoundError:
        print(f"error: file not found: {args.jsonl}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not estimates:
        print("No entries found.", file=sys.stderr)
        return 0

    # Output.
    if args.summary:
        print(SUMMARY_HEADER)
        for e in estimates:
            print(
                SUMMARY_FMT.format(
                    line=e.line,
                    req_tok=e.request_tokens,
                    res_tok=e.response_tokens,
                    req_cost=f"${e.input_cost:.6f}",
                    res_cost=f"${e.output_cost:.6f}",
                    total=f"${e.total_cost:.6f}",
                )
            )
        print("-" * 58)

    total_cost = sum(e.total_cost for e in estimates)
    total_req = sum(e.request_tokens for e in estimates)
    total_res = sum(e.response_tokens for e in estimates)

    print(
        f"Model: {args.model}  |  "
        f"Entries: {len(estimates)}  |  "
        f"In tokens: {total_req}  |  "
        f"Out tokens: {total_res}  |  "
        f"Total cost: ${total_cost:.6f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
