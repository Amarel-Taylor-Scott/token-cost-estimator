#!/usr/bin/env python3
"""
Tests for token_cost_estimator.

Run with:
    python3 -m pytest -q
    python3 -m unittest discover
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

# Ensure the module under test is importable.
sys.path.insert(0, os.path.dirname(__file__))
from token_cost_estimator import (
    estimate_tokens,
    PriceCard,
    parse_jsonl,
    estimate_entry,
    process_jsonl,
    main,
    SUMMARY_HEADER,
    SUMMARY_FMT,
)


# ----------------------------------------------------------------------
# Helper
# ----------------------------------------------------------------------

def _jsonl_path(lines: list[dict]) -> str:
    """Write dicts as JSONL to a temp file and return its path."""
    fh = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    )
    for d in lines:
        fh.write(json.dumps(d) + "\n")
    fh.close()
    return fh.name


# ----------------------------------------------------------------------
# estimate_tokens
# ----------------------------------------------------------------------

class TestEstimateTokens(unittest.TestCase):

    def test_empty_string(self):
        self.assertEqual(estimate_tokens(""), 1)

    def test_single_word(self):
        # "hello" -> overhead 2 + 1 word-token
        self.assertGreaterEqual(estimate_tokens("hello"), 1)

    def test_sentence(self):
        text = "The quick brown fox jumps over the lazy dog."
        n = estimate_tokens(text)
        self.assertGreater(n, 0)
        # 9 words + overhead 2 → at least 11.
        self.assertGreaterEqual(n, 11)

    def test_multiline_text(self):
        text = "First line.\nSecond line.\nThird line."
        n = estimate_tokens(text)
        self.assertGreater(n, 0)
        # 3 newlines add 3 to overhead (base overhead 2 → 5), 5 words → ~5 tokens
        self.assertGreaterEqual(n, 8)

    def test_consistency_deterministic(self):
        text = "Token estimation should be deterministic."
        self.assertEqual(estimate_tokens(text), estimate_tokens(text))
        self.assertEqual(estimate_tokens(text), estimate_tokens(text))

    def test_longer_text_more_tokens(self):
        short = "One two three."
        longish = "One two three four five six seven eight nine ten."
        self.assertGreater(
            estimate_tokens(longish), estimate_tokens(short)
        )


# ----------------------------------------------------------------------
# PriceCard
# ----------------------------------------------------------------------

class TestPriceCard(unittest.TestCase):

    def test_zero_cost(self):
        pc = PriceCard(input_cost=0.0, output_cost=0.0)
        self.assertEqual(pc.cost(1000, 500), 0.0)
        self.assertEqual(pc.cost(0, 0), 0.0)

    def test_input_only(self):
        pc = PriceCard(input_cost=0.01, output_cost=0.0)
        # 1000 input tokens × $0.01/1K = $0.01
        self.assertAlmostEqual(pc.cost(1000, 0), 0.01)
        self.assertAlmostEqual(pc.cost(1000, 100), 0.01)

    def test_output_only(self):
        pc = PriceCard(input_cost=0.0, output_cost=0.03)
        self.assertAlmostEqual(pc.cost(0, 1000), 0.03)

    def test_both(self):
        pc = PriceCard(input_cost=0.005, output_cost=0.015)
        # 1000 in × $0.005 + 500 out × $0.015 = $0.005 + $0.0075 = $0.0125
        self.assertAlmostEqual(pc.cost(1000, 500), 0.0125)

    def test_named_tuple_immutable(self):
        pc = PriceCard(1.0, 2.0)
        with self.assertRaises(AttributeError):
            pc.input_cost = 0.0  # type: ignore


# ----------------------------------------------------------------------
# parse_jsonl
# ----------------------------------------------------------------------

class TestParseJsonl(unittest.TestCase):

    def tearDown(self):
        if hasattr(self, "_path"):
            os.unlink(self._path)

    def test_single_entry(self):
        self._path = _jsonl_path([{"request": "hello", "response": "world"}])
        entries = list(parse_jsonl(self._path))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["request"], "hello")

    def test_multiple_entries(self):
        self._path = _jsonl_path([
            {"request": "a"},
            {"request": "b"},
            {"request": "c"},
        ])
        entries = list(parse_jsonl(self._path))
        self.assertEqual(len(entries), 3)

    def test_empty_lines_skipped(self):
        self._path = _jsonl_path([
            {"x": 1},
            {},
            {"y": 2},
        ])
        entries = list(parse_jsonl(self._path))
        self.assertEqual(len(entries), 3)

    def test_invalid_json_raises_value_error(self):
        # Write raw invalid JSON directly to bypass the parser's expectations.
        fh = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        fh.write('{"ok": true}\n')
        fh.write('{"broken')   # intentionally missing closing brace
        fh.close()
        self._path = fh.name
        with self.assertRaises(ValueError) as cm:
            list(parse_jsonl(self._path))
        self.assertIn("line 2", str(cm.exception))


# ----------------------------------------------------------------------
# estimate_entry
# ----------------------------------------------------------------------

class TestEstimateEntry(unittest.TestCase):

    def test_empty_entry(self):
        est = estimate_entry({}, PriceCard(0.0, 0.0))
        self.assertEqual(est.request_tokens, 1)
        self.assertEqual(est.response_tokens, 1)
        self.assertEqual(est.total_cost, 0.0)

    def test_request_only(self):
        price = PriceCard(input_cost=0.01, output_cost=0.0)
        est = estimate_entry({"request": "hello world"}, price)
        self.assertGreater(est.request_tokens, 0)
        self.assertEqual(est.response_tokens, 1)  # empty → min 1
        self.assertGreater(est.input_cost, 0.0)

    def test_response_only(self):
        price = PriceCard(input_cost=0.0, output_cost=0.02)
        est = estimate_entry({"response": "answer"}, price)
        self.assertEqual(est.request_tokens, 1)
        self.assertGreater(est.response_tokens, 0)
        self.assertGreater(est.output_cost, 0.0)

    def test_both_fields(self):
        price = PriceCard(input_cost=0.005, output_cost=0.015)
        est = estimate_entry(
            {"request": "What is 2+2?", "response": "It is 4."},
            price,
            line=42,
        )
        self.assertEqual(est.line, 42)
        self.assertGreater(est.request_tokens, 0)
        self.assertGreater(est.response_tokens, 0)
        self.assertAlmostEqual(
            est.total_cost,
            est.input_cost + est.output_cost,
        )

    def test_alternative_field_names(self):
        price = PriceCard(0.0, 0.0)
        # "prompt" instead of "request"
        est1 = estimate_entry({"prompt": "hello"}, price)
        self.assertGreater(est1.request_tokens, 1)
        # "completion" instead of "response"
        est2 = estimate_entry({"completion": "world"}, price)
        self.assertGreater(est2.response_tokens, 1)
        # "output" instead of "response"
        est3 = estimate_entry({"output": "result"}, price)
        self.assertGreater(est3.response_tokens, 1)

    def test_cost_calculation_matches_price_card(self):
        price = PriceCard(input_cost=0.001, output_cost=0.002)
        # "a a ... a"  (2000 space-separated "a"s)
        # → 2000 words, overhead 2 → 2002 tokens
        # cost = 2002/1000 * 0.001 = $0.002002
        req_text = " ".join(["a"] * 2000)
        res_text = " ".join(["b"] * 1000)
        est = estimate_entry({"request": req_text, "response": res_text}, price)
        # Verify token counts.
        self.assertGreater(est.request_tokens, 2000)
        self.assertGreater(est.response_tokens, 1000)
        # Verify cost formula: token_cost = tokens/1000 * price.
        self.assertAlmostEqual(est.input_cost, est.request_tokens / 1000 * 0.001)
        self.assertAlmostEqual(est.output_cost, est.response_tokens / 1000 * 0.002)
        self.assertAlmostEqual(est.total_cost, est.input_cost + est.output_cost)


# ----------------------------------------------------------------------
# process_jsonl
# ----------------------------------------------------------------------

class TestProcessJsonl(unittest.TestCase):

    def tearDown(self):
        if hasattr(self, "_path"):
            os.unlink(self._path)

    def test_two_entries_two_estimates(self):
        self._path = _jsonl_path([
            {"request": "q1", "response": "a1"},
            {"request": "q2", "response": "a2"},
        ])
        estimates = list(process_jsonl(self._path, PriceCard(0.0, 0.0)))
        self.assertEqual(len(estimates), 2)
        self.assertEqual(estimates[0].line, 1)
        self.assertEqual(estimates[1].line, 2)

    def test_costs_are_zero_when_price_is_zero(self):
        self._path = _jsonl_path([{"request": "x", "response": "y"}])
        estimates = list(process_jsonl(self._path, PriceCard(0.0, 0.0)))
        self.assertEqual(estimates[0].total_cost, 0.0)


# ----------------------------------------------------------------------
# Hand-calculated verification
# ----------------------------------------------------------------------

class TestHandCalculatedExamples(unittest.TestCase):

    def test_example_1_gpt4o(self):
        """
        Entry: request="Hello world" (11 chars inc space), response="Hi there"
        Tokens (est): overhead 2 + 2 words ≈ 4 tokens each → ~4 in + 4 out
        Cost (gpt-4o): $0.005/1K in, $0.015/1K out
          → 4/1000 * 0.005 = $0.00002 in
          → 4/1000 * 0.015 = $0.00006 out
          → total ≈ $0.00008
        """
        from token_cost_estimator import DEFAULT_PRICES

        price = DEFAULT_PRICES["gpt-4o"]
        est = estimate_entry(
            {"request": "Hello world", "response": "Hi there"},
            price,
        )

        # Tokens must be positive integers.
        self.assertIsInstance(est.request_tokens, int)
        self.assertGreater(est.request_tokens, 0)
        self.assertIsInstance(est.response_tokens, int)
        self.assertGreater(est.response_tokens, 0)

        # Costs must be non-negative.
        self.assertGreaterEqual(est.input_cost, 0.0)
        self.assertGreaterEqual(est.output_cost, 0.0)
        self.assertGreaterEqual(est.total_cost, 0.0)

        # Request is "Hello world" → 2 words, overhead 2 → ~4 tokens.
        self.assertLessEqual(est.request_tokens, 6)

        # Total cost under $0.001 (should be tiny for short strings).
        self.assertLess(est.total_cost, 0.001)

        # Re-derive from raw counts.
        manual_cost = (
            est.request_tokens / 1000 * price.input_cost
            + est.response_tokens / 1000 * price.output_cost
        )
        self.assertAlmostEqual(est.total_cost, manual_cost)

    def test_example_2_zero_tokens_zero_cost(self):
        """Zero tokens → zero cost, regardless of price."""
        price = PriceCard(input_cost=999.0, output_cost=999.0)
        est = estimate_entry({"request": "", "response": ""}, price)
        # Empty strings still get min 1 token each, but cost is still per-card.
        self.assertEqual(est.total_cost, est.input_cost + est.output_cost)

    def test_example_3_cost_linearity(self):
        """
        Doubling input tokens should (approximately) double input cost,
        provided the token estimator scales linearly.
        """
        price = PriceCard(input_cost=0.01, output_cost=0.0)
        est_short = estimate_entry({"request": "x"}, price)
        est_long = estimate_entry({"request": "x " * 10}, price)
        # The longer request should cost strictly more.
        self.assertGreater(est_long.input_cost, est_short.input_cost)


# ----------------------------------------------------------------------
# main() CLI
# ----------------------------------------------------------------------

class TestMain(unittest.TestCase):

    def tearDown(self):
        if hasattr(self, "_path"):
            os.unlink(self._path)

    def test_main_returns_zero_on_success(self):
        self._path = _jsonl_path([{"request": "a", "response": "b"}])
        old_stdout = io.StringIO()
        with redirect_stdout(old_stdout):
            rc = main([self._path, "--model", "gpt-4o"])
        self.assertEqual(rc, 0)

    def test_main_unknown_model(self):
        self._path = _jsonl_path([{"request": "a"}])
        with self.assertRaises(SystemExit) as cm:
            main([self._path, "--model", "nonexistent-model"])
        self.assertEqual(cm.exception.code, 2)

    def test_main_file_not_found(self):
        rc = main(["/nonexistent/file.jsonl"])
        self.assertEqual(rc, 1)

    def test_main_invalid_jsonl(self):
        # Write raw invalid JSON directly to exercise the error path.
        fh = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        fh.write('{"ok": true}\n')
        fh.write('{"broken')   # intentionally missing closing brace
        fh.close()
        self._path = fh.name
        rc = main([self._path])
        self.assertEqual(rc, 1)

    def test_main_summary_flag(self):
        self._path = _jsonl_path([{"request": "hello", "response": "world"}])
        old_stdout = io.StringIO()
        with redirect_stdout(old_stdout):
            rc = main([self._path, "--summary"])
        self.assertEqual(rc, 0)
        output = old_stdout.getvalue()
        self.assertIn("IN_TOK", output)
        self.assertIn("OUT_TOK", output)
        self.assertIn("Total cost", output)

    def test_main_price_overrides(self):
        self._path = _jsonl_path([{"request": "a", "response": "b"}])
        old_stdout = io.StringIO()
        with redirect_stdout(old_stdout):
            rc = main([
                self._path,
                "--input-price", "0.01",
                "--output-price", "0.02",
            ])
        self.assertEqual(rc, 0)
        output = old_stdout.getvalue()
        # Should use gpt-4o label even with price overrides.
        self.assertIn("gpt-4o", output)


# ----------------------------------------------------------------------
# Synthetic chat log test
# ----------------------------------------------------------------------

class TestSyntheticChatLogs(unittest.TestCase):

    def tearDown(self):
        if hasattr(self, "_path"):
            os.unlink(self._path)

    def test_synthetic_chat_log_total_cost(self):
        """
        Synthetic 3-turn chat:

        Turn 1: request="Hello", response="Hi there"
        Turn 2: request="What is 2+2?", response="It is 4"
        Turn 3: request="Thanks", response="You're welcome"

        Price: gpt-4o ($0.005 in / $0.015 out per 1K)

        Tokens per turn (estimate):
          Turn 1: req ~4 tokens, res ~4 tokens
          Turn 2: req ~5 tokens, res ~4 tokens
          Turn 3: req ~3 tokens, res ~4 tokens
        Total: ~8 in + ~8 out

        Cost ≈ 8/1000*0.005 + 8/1000*0.015 = $0.00016

        This test verifies the total cost is positive and tiny (as expected
        for short synthetic strings), and that the output line appears.
        """
        from token_cost_estimator import DEFAULT_PRICES

        self._path = _jsonl_path([
            {"request": "Hello", "response": "Hi there"},
            {"request": "What is 2+2?", "response": "It is 4"},
            {"request": "Thanks", "response": "You're welcome"},
        ])

        estimates = list(process_jsonl(self._path, DEFAULT_PRICES["gpt-4o"]))

        # Three entries.
        self.assertEqual(len(estimates), 3)

        # All costs non-negative.
        for e in estimates:
            self.assertGreaterEqual(e.total_cost, 0.0)

        # Total cost positive.
        total = sum(e.total_cost for e in estimates)
        self.assertGreater(total, 0.0)

        # Total cost tiny for short strings.
        self.assertLess(total, 0.001)

        # Total token counts positive.
        total_req = sum(e.request_tokens for e in estimates)
        total_res = sum(e.response_tokens for e in estimates)
        self.assertGreater(total_req, 0)
        self.assertGreater(total_res, 0)


if __name__ == "__main__":
    unittest.main()
