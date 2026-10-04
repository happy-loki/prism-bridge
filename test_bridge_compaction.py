"""Offline regression checks; no browser, credentials or model calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge as b


def entry(text, role="user", images=None):
    return {"role": role, "text": text, "images": images or []}


class CompactionTests(unittest.TestCase):
    def setUp(self):
        settings = patch.multiple(
            b, MAX_TURN_BYTES=12000, MAX_TURN_PARTS=8, COMPACT_MAX_PARTS=2,
            PART_GAP_SEC=0, CONTINUE_CONVERSATIONS=True, CALLER_OWNED_TOOLS=False,
            _relay_records={}, _relay_heads={},
        )
        settings.start()
        self.addCleanup(settings.stop)

    def assert_fits(self, text, count=2):
        pieces = b.split_turn_text(text)
        self.assertLessEqual(len(pieces), count)
        for i, piece in enumerate(pieces):
            sent = piece if len(pieces) == 1 else b._part_text(piece, i + 1, i == len(pieces) - 1)
            size = len(json.dumps(sent, ensure_ascii=False).encode("utf-8")) - 2
            self.assertLessEqual(size, b.MAX_TURN_BYTES)

    def test_multibyte_history_preserves_current_and_fits_wrapped_turns(self):
        for token in ("中", "\U0001f642", '\\"\n\t\x00'):
            with self.subTest(token=repr(token)):
                entries = [entry(token * 16000), entry("noted", "assistant"), entry("CURRENT exact\ntext")]
                body, kept, info = b.fit_replay_text("HEADER\n", entries, "\nREMINDER")
                self.assertTrue(info["compacted"])
                self.assert_fits("HEADER\n" + body)
                self.assertTrue(body.endswith("CURRENT exact\ntext\nREMINDER"))
                self.assertEqual(kept[-1], entries[-1])

    def test_current_larger_than_target_rejects_candidate_without_dropping_history(self):
        entries = [entry("old " * 20000), entry("current " + "y" * 30000)]
        body, kept, info = b.fit_replay_text("HEADER", entries, "END")
        self.assertTrue(info["compacted"])
        current = "current " + "y" * 30000
        self.assertIn(current, body)
        self.assertTrue(body.endswith(current + "\nEND") or current in body)
        self.assert_fits("HEADER" + body, 8)
        self.assertEqual(kept[-1], entries[-1])
        self.assertFalse(info.get("source_accessible"))
        self.assertEqual(info.get("source_note"), "original text not retained")

    def test_current_larger_than_soft_target_but_within_hard_cap_stays_raw(self):
        entries = [entry("old " * 2000), entry("current " + "y" * 30000)]
        body, kept, info = b.fit_replay_text("HEADER", entries, "END")
        self.assertFalse(info["compacted"])
        self.assertEqual(body, b.flatten_converted_entries(entries, "END"))
        self.assertEqual(kept, entries)
        self.assertLessEqual(len(b.split_turn_text("HEADER" + body)), 8)

    def test_header_leaving_less_than_1000_bytes_has_no_budget_floor(self):
        header = "H" * 11900
        entries = [entry("中" * 10000), entry("NOW")]
        with patch.object(b, "COMPACT_MAX_PARTS", 1):
            body, _, info = b.fit_replay_text(header, entries, "")
        self.assertTrue(info["compacted"])
        self.assert_fits(header + body, 1)
        self.assertIn("NOW", body)

    def test_header_consuming_entire_budget_leaves_request_unchanged(self):
        entries = [entry("old " * 10000), entry("NOW")]
        with patch.object(b, "COMPACT_MAX_PARTS", 1):
            body, kept, info = b.fit_replay_text("H" * 12000, entries, "")
        self.assertFalse(info["compacted"])
        self.assertEqual(body, b.flatten_converted_entries(entries, ""))
        self.assertEqual(kept, entries)

    def test_configured_send_cap_limits_compaction_target(self):
        request = {"input": [{"role": "user", "content": "中" * 6000}, {"role": "user", "content": "NOW"}]}
        with patch.object(b, "MAX_TURN_PARTS", 1):
            plan = b.build_relay_plan(request, "tenant", {}, "model")
        self.assertTrue(plan["full"]["compacted"])
        self.assert_fits(plan["full"]["text"], 1)

    def test_disabled_compaction_preserves_original(self):
        entries = [entry("中" * 16000), entry("NOW")]
        with patch.object(b, "COMPACT_MAX_PARTS", 0):
            body, kept, info = b.fit_replay_text("HEADER", entries, "END")
        self.assertFalse(info["compacted"])
        self.assertEqual(body, b.flatten_converted_entries(entries, "END"))
        self.assertEqual(kept, entries)

    def test_clipping_respects_json_byte_limits(self):
        text = ('中文\U0001f642\\"\n\t\x00' * 200)
        for cap in (0, 1, 3, 4, 32, 80, 400, 1200):
            with self.subTest(cap=cap):
                clipped = b._clip_text(text, cap)
                size = len(json.dumps(clipped, ensure_ascii=False).encode("utf-8")) - 2
                self.assertLessEqual(size, cap)
        self.assertEqual(b._clip_text(text, b.transport_size(text)), text)

    def test_current_tool_batch_and_images_remain_intact(self):
        question = entry("RUN BOTH TOOLS", images=["question-image"])
        call1 = entry('<client_tool_call id="c1">{}</client_tool_call>', "assistant")
        call2 = entry('<client_tool_call id="c2">{}</client_tool_call>', "assistant")
        out1 = entry('<client_tool_output call_id="c1">EXACT1</client_tool_output>', images=["result-image-1"])
        out1.update(call_id="c1", output="EXACT1")
        out2 = entry('<client_tool_output call_id="c2">EXACT2</client_tool_output>', images=["result-image-2"])
        out2.update(call_id="c2", output="EXACT2")
        entries = [entry("中" * 16000, images=["old-image"]), entry("old answer", "assistant"), question, call1, call2, out1, out2]
        body, kept, info = b.fit_replay_text("HEADER", entries, "END")
        self.assertTrue(info["compacted"])
        for exact in (question["text"], call1["text"], call2["text"], out1["text"], out2["text"]):
            self.assertIn(exact, body)
        self.assertIn("END", body)
        self.assertEqual([img for item in kept for img in item["images"]], ["question-image", "result-image-1", "result-image-2"])

    def test_no_older_history_means_current_tool_batch_is_not_compacted(self):
        question = entry("Q")
        output = entry('<client_tool_output call_id="c1">EXACT</client_tool_output>')
        output.update(call_id="c1", output="EXACT")
        entries = [question, output]
        body, kept, info = b.fit_replay_text("HEADER", entries, "END")
        self.assertFalse(info["compacted"])
        self.assertEqual(body, b.flatten_converted_entries(entries, "END"))
        self.assertEqual(kept, entries)

    def test_compacted_history_continues_after_state_reload_and_is_tenant_isolated(self):
        messages = [{"role": "user", "content": "old " * 12000}, {"role": "user", "content": "NOW"}]
        plan = b.build_relay_plan({"input": messages}, "tenant", {}, "model")
        self.assertTrue(plan["full"]["compacted"])
        result = {"cid": "cdx1_test", "rid": "upstream-1", "continuable": True, "text": "ANSWER", "snapshot": {"codex_session_id": "s1", "transcript_cursor": 5}, "mode": "full"}
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, "RELAY_STATE_FILE", Path(tmp) / "sessions.json"), patch.object(b, "load_cookie", return_value=""):
            b.remember_turn(plan, result, "resp_test")
            b._relay_records.clear()
            b._relay_heads.clear()
            b.load_relay_state()
            followup = {"input": messages + [{"role": "assistant", "content": "ANSWER"}, {"role": "user", "content": "FOLLOWUP"}]}
            next_plan = b.build_relay_plan(followup, "tenant", {}, "model")
            self.assertEqual(next_plan["delta"]["cid"], "cdx1_test")
            self.assertEqual(next_plan["delta"]["prev"], "upstream-1")
            self.assertIn("FOLLOWUP", next_plan["delta"]["text"])
            self.assertNotIn("old old", next_plan["delta"]["text"])
            self.assertIsNone(b.build_relay_plan(followup, "other-tenant", {}, "model")["delta"])

    def test_single_turn_exact_limit_does_not_add_ack(self):
        self.assertEqual(b.split_turn_text("x" * 12000), ["x" * 12000])
        self.assertEqual(len(b.split_turn_text("x" * 12001)), 2)

    def test_size_retry_reserves_wrapper_after_first_part_delivered(self):
        controller = b.PrismPage()
        attempts = []
        accepted = []

        def chat(items, model, effort, images, cid, tools, prev, snapshot):
            text = items[-1]["content"][0]["text"]
            attempts.append(text)
            if len(attempts) == 2:
                raise b.PrismTooLarge("size refusal")
            if len(attempts) > 2:
                self.assertLessEqual(b.transport_size(text), 43000)
            accepted.append(text)
            return {"cid": cid, "rid": f"r{len(accepted)}", "snapshot": {}, "text": "answer"}

        spec = {"text": "A" * 80000 + "\n" + "B" * 42900, "images": [], "cid": "cdx1_test", "prev": "r0"}
        with patch.object(b, "MAX_TURN_BYTES", 86000), patch.object(controller, "chat", side_effect=chat):
            result = controller._send_parts(spec, "model", "high", [])
        self.assertEqual(result["parts"], 3)
        payloads = [text.split(">\n", 1)[1].rsplit("\n</relay_part>", 1)[0] for text in accepted]
        self.assertEqual("".join(payloads), spec["text"])

    def test_oversized_current_input_fails_before_creating_conversation(self):
        plan = b.build_relay_plan({"input": "x" * 200000}, "tenant", {}, "model")
        self.assertFalse(plan["full"]["compacted"])
        controller = b.PrismPage()
        with patch.object(controller, "new_conversation") as create, patch.object(controller, "chat") as chat:
            with self.assertRaises(b.PrismTooLarge):
                controller._send_parts(plan["full"], "model", "high", [])
        create.assert_not_called()
        chat.assert_not_called()

    def test_delta_size_refusal_uses_compacted_full_but_unknown_failure_is_not_replayed(self):
        request = {"input": [{"role": "user", "content": "old " * 12000}, {"role": "user", "content": "NOW"}]}
        plan = b.build_relay_plan(request, "tenant", {}, "model")
        plan["delta"] = {"text": "NEW", "source": "history", "cid": "cdx1_old", "prev": "r0", "images": [], "since_catalog": 3}
        controller = b.PrismPage()
        with patch.object(controller, "_send_parts", side_effect=[b.PrismTooLarge("size"), {"text": "answer"}]) as send:
            result = controller.relay(plan, "high")
        self.assertEqual(result["mode"], "full")
        sent_spec = send.call_args_list[1].args[0]
        self.assertTrue(sent_spec["compacted"])
        self.assert_fits(sent_spec["text"])
        with patch.object(controller, "_send_parts", side_effect=b.PrismTurnError("unknown result")) as send:
            with self.assertRaises(b.PrismTurnError):
                controller.relay(plan, "high")
        self.assertEqual(send.call_count, 1)
        with patch.object(controller, "_send_parts", side_effect=RuntimeError("HTTP 403 unknown execution state")) as send:
            with self.assertRaises(RuntimeError):
                controller.relay(plan, "high")
        self.assertEqual(send.call_count, 1)

    def test_small_request_is_left_verbatim(self):
        entries = [entry("hello"), entry("hi", "assistant"), entry("NEXT")]
        body, kept, info = b.fit_replay_text("HEADER\n", entries, "END")
        self.assertFalse(info["compacted"])
        self.assertEqual(body, b.flatten_converted_entries(entries, "END"))
        self.assertEqual(kept, entries)

    def test_hundred_thousand_prior_and_two_hundred_thousand_current_stays_raw_six_parts(self):
        request = {
            "input": [
                {"role": "user", "content": "A" * 100000},
                {"role": "user", "content": "B" * 200000},
            ]
        }
        with patch.multiple(b, MAX_TURN_BYTES=86000, MAX_TURN_PARTS=8, COMPACT_MAX_PARTS=2):
            plan = b.build_relay_plan(request, "tenant", {}, "model")
            spec = b.ensure_full_spec(plan)
            self.assertFalse(spec["compacted"])
            pieces = b.split_turn_text(spec["text"])
            self.assertEqual(len(pieces), 6)
            self.assertIn("A" * 100000, spec["text"])
            self.assertIn("B" * 200000, spec["text"])
            for index, piece in enumerate(pieces):
                sent = piece if len(pieces) == 1 else b._part_text(piece, index + 1, index == len(pieces) - 1)
                self.assertLessEqual(b.transport_size(sent), 86000)

    def test_eight_hundred_thousand_prior_compacts_within_hard_cap_keeping_current(self):
        request = {
            "input": [
                {"role": "user", "content": "A" * 800000},
                {"role": "user", "content": "B" * 200000},
            ]
        }
        with patch.multiple(b, MAX_TURN_BYTES=86000, MAX_TURN_PARTS=8, COMPACT_MAX_PARTS=2):
            with patch.object(b, "COMPACT_MAX_PARTS", 0):
                raw_plan = b.build_relay_plan(request, "tenant", {}, "model")
                raw_spec = b.ensure_full_spec(raw_plan)
                self.assertFalse(raw_spec["compacted"])
                self.assertEqual(len(b.split_turn_text(raw_spec["text"])), 14)
                current_start = b._current_batch_start(raw_plan["entries"])
                current_only = raw_plan["header"] + b._current_batch_text(
                    raw_plan["entries"][current_start:], raw_plan["reminder"]
                )
                self.assertEqual(len(b.split_turn_text(current_only)), 4)
            plan = b.build_relay_plan(request, "tenant", {}, "model")
            spec = b.ensure_full_spec(plan)
            self.assertTrue(spec["compacted"])
            pieces = b.split_turn_text(spec["text"])
            self.assertLessEqual(len(pieces), 8)
            self.assertIn("B" * 200000, spec["text"])
            for index, piece in enumerate(pieces):
                sent = piece if len(pieces) == 1 else b._part_text(piece, index + 1, index == len(pieces) - 1)
                self.assertLessEqual(b.transport_size(sent), 86000)

    def test_compact_max_parts_zero_never_drops_history(self):
        entries = [entry("A" * 800000), entry("B" * 200000)]
        with patch.multiple(b, MAX_TURN_BYTES=86000, COMPACT_MAX_PARTS=0, MAX_TURN_PARTS=8):
            body, kept, info = b.fit_replay_text("H\n", entries, "")
            self.assertFalse(info["compacted"])
            self.assertEqual(body, b.flatten_converted_entries(entries, ""))
            self.assertEqual(kept, entries)

    def test_wrapped_multibyte_parts_stay_within_json_utf8_budget(self):
        with patch.multiple(b, MAX_TURN_BYTES=86000, MAX_TURN_PARTS=8, COMPACT_MAX_PARTS=2):
            token = '中\U0001f642\\"\n'
            entries = [entry(token * 8000), entry("CURRENT exact")]
            body, kept, info = b.fit_replay_text("HEADER\n", entries, "\nREMINDER")
            packed = "HEADER\n" + body
            pieces = b.split_turn_text(packed)
            self.assertLessEqual(len(pieces), 8)
            for index, piece in enumerate(pieces):
                sent = piece if len(pieces) == 1 else b._part_text(piece, index + 1, index == len(pieces) - 1)
                self.assertLessEqual(b.transport_size(sent), 86000)
            self.assertIn("CURRENT exact", body)
            self.assertEqual(kept[-1], entries[-1])

    def test_continuation_does_not_build_full_spec(self):
        messages = [{"role": "user", "content": "old " * 12000}, {"role": "user", "content": "NOW"}]
        plan = b.build_relay_plan({"input": messages}, "tenant", {}, "model")
        result = {
            "cid": "cdx1_test",
            "rid": "upstream-1",
            "continuable": True,
            "text": "ANSWER",
            "snapshot": {"codex_session_id": "s1", "transcript_cursor": 5},
            "mode": "full",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, "RELAY_STATE_FILE", Path(tmp) / "sessions.json"), patch.object(b, "load_cookie", return_value=""):
            b.remember_turn(plan, result, "resp_test")
            b._relay_records.clear()
            b._relay_heads.clear()
            b.load_relay_state()
            followup = {"input": messages + [{"role": "assistant", "content": "ANSWER"}, {"role": "user", "content": "FOLLOWUP"}]}
            with patch.object(b, "fit_replay_text", wraps=b.fit_replay_text) as fit:
                next_plan = b.build_relay_plan(followup, "tenant", {}, "model")
            self.assertIsNotNone(next_plan["delta"])
            self.assertIsNone(next_plan["full"])
            fit.assert_not_called()
            with patch.object(b, "ensure_full_spec") as ensure:
                spec = b.prepare_send_spec(next_plan)
            ensure.assert_not_called()
            self.assertIs(spec, next_plan["delta"])
            self.assertIn("FOLLOWUP", spec["text"])
            self.assertIsNone(next_plan["full"])

    def test_tool_catalog_refresh_is_included_in_delta(self):
        messages = [{"role": "user", "content": "old " * 12000}, {"role": "user", "content": "NOW"}]
        plan = b.build_relay_plan({"input": messages}, "tenant", {}, "model")
        result = {
            "cid": "cdx1_test",
            "rid": "upstream-1",
            "continuable": True,
            "text": "ANSWER",
            "snapshot": {"codex_session_id": "s1", "transcript_cursor": 5},
            "mode": "full",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, "RELAY_STATE_FILE", Path(tmp) / "sessions.json"), patch.object(b, "load_cookie", return_value=""):
            b.remember_turn(plan, result, "resp_test")
            b._relay_records.clear()
            b._relay_heads.clear()
            b.load_relay_state()
            followup = {
                "input": messages + [{"role": "assistant", "content": "ANSWER"}, {"role": "user", "content": "FOLLOWUP"}],
                "tools": [{"type": "function", "name": "bash", "parameters": {"type": "object"}}],
            }
            next_plan = b.build_relay_plan(followup, "tenant", {}, "model")
            self.assertIsNotNone(next_plan["delta"])
            self.assertIn("<relay_instructions>", next_plan["delta"]["text"])
            self.assertIn("FOLLOWUP", next_plan["delta"]["text"])
            same = {"input": messages + [{"role": "assistant", "content": "ANSWER"}, {"role": "user", "content": "FOLLOWUP"}]}
            same_plan = b.build_relay_plan(same, "tenant", {}, "model")
            self.assertIsNotNone(same_plan["delta"])
            self.assertNotIn("<relay_instructions>", same_plan["delta"]["text"])

    def test_sendable_delta_is_not_rejected_because_backup_full_is_huge(self):
        messages = [{"role": "user", "content": "old " * 12000}, {"role": "user", "content": "NOW"}]
        plan = b.build_relay_plan({"input": messages}, "tenant", {}, "model")
        result = {
            "cid": "cdx1_test",
            "rid": "upstream-1",
            "continuable": True,
            "text": "ANSWER",
            "snapshot": {"codex_session_id": "s1", "transcript_cursor": 5},
            "mode": "full",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, "RELAY_STATE_FILE", Path(tmp) / "sessions.json"), patch.object(b, "load_cookie", return_value=""):
            b.remember_turn(plan, result, "resp_test")
            b._relay_records.clear()
            b._relay_heads.clear()
            b.load_relay_state()
            followup = {"input": messages + [{"role": "assistant", "content": "ANSWER"}, {"role": "user", "content": "FOLLOWUP"}]}
            next_plan = b.build_relay_plan(followup, "tenant", {}, "model")
            next_plan["entries"] = [entry("A" * 800000), entry("B" * 200000)]
            with patch.object(b, "ensure_full_spec") as ensure:
                spec = b.prepare_send_spec(next_plan)
            ensure.assert_not_called()
            self.assertIs(spec, next_plan["delta"])
            self.assertIsNone(next_plan["full"])
            self.assertIn("FOLLOWUP", spec["text"])

    def test_locally_oversized_delta_selects_compacted_full_without_upstream(self):
        request = {
            "input": [
                {"role": "user", "content": "A" * 120000},
                {"role": "user", "content": "NOW"},
            ]
        }
        rec = {
            "cid": "cdx1_old",
            "rid": "r0",
            "catalog": "other",
            "since_catalog": 0,
            "calls": [],
            "snapshot": {},
        }
        with patch.object(b, "find_continuation", return_value={"rec": rec, "start": 0, "source": "history"}):
            plan = b.build_relay_plan(request, "tenant", {}, "model")
        self.assertIsNotNone(plan["delta"])
        self.assertIsNone(plan["full"])
        self.assertFalse(b._text_fits_parts(plan["delta"]["text"], b.MAX_TURN_PARTS))
        spec = b.prepare_send_spec(plan)
        self.assertTrue(spec.get("compacted"))
        self.assert_fits(spec["text"], 8)
        self.assertIn("NOW", spec["text"])
        controller = b.PrismPage()
        with patch.object(
            controller, "_send_parts", return_value={"text": "answer", "cid": "c", "rid": "r", "parts": 1}
        ) as send:
            result = controller.relay(plan, "high")
        self.assertEqual(result["mode"], "full")
        self.assertEqual(send.call_count, 1)
        self.assertTrue(send.call_args.args[0].get("compacted"))

    def test_preflight_rejects_oversized_current_before_get_worker(self):
        handler = b.Handler.__new__(b.Handler)
        handler.path = "/v1/responses"
        handler.headers = {}
        captured = []
        handler._send = lambda code, obj: captured.append((code, obj))
        handler._read_json = lambda: {"input": "x" * 200000, "model": "gpt-6.1-sol"}
        handler._reject_foreign = lambda: False
        handler._reject_auth = lambda: False
        with patch.object(b, "get_worker") as worker:
            handler._handle_post()
        worker.assert_not_called()
        self.assertEqual(captured[0][0], 400)
        self.assertEqual(captured[0][1]["error"]["code"], "context_length_exceeded")
        plan = b.build_relay_plan({"input": "x" * 200000}, "tenant", {}, "model")
        with self.assertRaises(b.PrismTooLarge):
            b.prepare_send_spec(plan)

    def test_preflight_stream_uses_same_context_length_error(self):
        handler = b.Handler.__new__(b.Handler)
        handler.path = "/v1/responses"
        handler.headers = {}
        handler._sse_closed = False
        events = []
        handler._sse_begin = lambda: events.append("begin") or setattr(handler, "_sse_lock", object())
        handler._sse = lambda event, obj: events.append((event, obj))
        handler._sse_data = lambda obj: events.append(obj)
        handler._send = lambda code, obj: events.append((code, obj))
        handler._read_json = lambda: {"input": "x" * 200000, "model": "gpt-6.1-sol", "stream": True}
        handler._reject_foreign = lambda: False
        handler._reject_auth = lambda: False
        with patch.object(b, "get_worker") as worker:
            handler._handle_post()
        worker.assert_not_called()
        self.assertIn("begin", events)
        failed = [item for item in events if isinstance(item, tuple) and item and item[0] == "response.failed"]
        self.assertEqual(failed[0][1]["response"]["error"]["code"], "context_length_exceeded")

    def test_chat_does_not_resend_unknown_http_403(self):
        controller = b.PrismPage()
        controller.sandbox = {"pid": "p", "sandboxToken": "t", "sandboxUrl": "http://s"}
        controller.cookie = "cookie"
        failure = RuntimeError("HTTP 403 unknown execution state")
        with patch.object(controller, "_chat_once", side_effect=failure) as once:
            with self.assertRaises(RuntimeError) as raised:
                controller.chat(
                    [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                    "model",
                    "high",
                )
        self.assertIs(raised.exception, failure)
        self.assertEqual(once.call_count, 1)

    def test_chat_does_not_resend_missing_request_id_or_timeout(self):
        controller = b.PrismPage()
        controller.sandbox = {"pid": "p", "sandboxToken": "t", "sandboxUrl": "http://s"}
        controller.cookie = "cookie"
        for failure in (
            b.PrismTurnError("no request_id: {}"),
            b.PrismTurnError("llm timeout after 600s"),
            b.PlaywrightError("net::ERR_CONNECTION_CLOSED"),
        ):
            with self.subTest(failure=str(failure)):
                with patch.object(controller, "_chat_once", side_effect=failure) as once:
                    with self.assertRaises(type(failure)):
                        controller.chat(
                            [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                            "model",
                            "high",
                        )
                self.assertEqual(once.call_count, 1)

    def test_structured_start_rejection_replays_full_once(self):
        request = {"input": [{"role": "user", "content": "old " * 12000}, {"role": "user", "content": "NOW"}]}
        plan = b.build_relay_plan(request, "tenant", {}, "model")
        plan["delta"] = {"text": "NEW", "source": "history", "cid": "cdx1_old", "prev": "r0", "images": [], "since_catalog": 3}
        controller = b.PrismPage()
        refusal = b.PrismUnexecutedRefusal(
            "llm start HTTP 400 gone",
            reason="rejected_at_start",
            phase="start",
            retryable=True,
            http_status=400,
        )
        with patch.object(controller, "_send_parts", side_effect=[refusal, {"text": "answer"}]) as send:
            result = controller.relay(plan, "high")
        self.assertEqual(result["mode"], "full")
        self.assertEqual(send.call_count, 2)

    def test_partial_ack_size_refusal_does_not_full_replay(self):
        request = {"input": [{"role": "user", "content": "old " * 12000}, {"role": "user", "content": "NOW"}]}
        plan = b.build_relay_plan(request, "tenant", {}, "model")
        plan["delta"] = {"text": "NEW", "source": "history", "cid": "cdx1_old", "prev": "r0", "images": [], "since_catalog": 3}
        controller = b.PrismPage()
        error = b.PrismTooLarge("size after ack", delivered_parts=1)
        with patch.object(controller, "_send_parts", side_effect=error) as send:
            with self.assertRaises(b.PrismTooLarge):
                controller.relay(plan, "high")
        self.assertEqual(send.call_count, 1)

    def test_raise_llm_start_failure_classifies_status(self):
        with self.assertRaises(b.PrismTooLarge):
            b._raise_llm_start_failure(
                {"status": 400, "json": {"response": {"payload": {"reason": "conversation_too_large", "message": "too big"}}}, "text": ""}
            )
        with self.assertRaises(b.PrismTurnError):
            b._raise_llm_start_failure({"status": 403, "json": {}, "text": "forbidden"})
        with self.assertRaises(b.PrismTurnError):
            b._raise_llm_start_failure({"status": 401, "json": {}, "text": "auth"})
        with self.assertRaises(b.PrismUnexecutedRefusal) as raised:
            b._raise_llm_start_failure({"status": 404, "json": {
                "error": {"code": "conversation_not_found"}}, "text": "missing conversation"})
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.phase, "start")
        self.assertEqual(raised.exception.reason, "conversation_not_found")
        with self.assertRaises(b.PrismTurnError):
            b._raise_llm_start_failure({"status": 500, "json": {}, "text": "upstream"})

    def test_recent_verbatim_keeps_complete_prior_tool_batch(self):
        out = entry('<client_tool_output call_id="c1">RESULT_EXACT</client_tool_output>')
        out.update(call_id="c1", output="RESULT_EXACT")
        entries = [
            entry("PAD " * 8000),
            entry("ok", "assistant"),
            entry("OLD_TASK_QUESTION"),
            entry('<client_tool_call id="c1">{"name": "bash"}</client_tool_call>', "assistant"),
            out,
            entry("NOW"),
        ]
        body, kept, info = b.fit_replay_text("HEADER", entries, "END")
        self.assertTrue(info["compacted"])
        self.assertIn("NOW", body)
        recent = body.split("【近期原文 / Recent turns kept verbatim】", 1)
        if len(recent) == 2:
            recent_text = recent[1].split("【当前提问 / Current Question】", 1)[0]
            if "RESULT_EXACT" in recent_text:
                self.assertIn("OLD_TASK_QUESTION", recent_text)
                self.assertIn('<client_tool_call id="c1">', recent_text)
        else:
            self.assertIn("OLD_TASK_QUESTION", body)
            self.assertIn("RESULT_EXACT", body)

    def test_unfinished_prior_tool_batch_is_not_omitted(self):
        entries = [
            entry("PAD " * 8000),
            entry("ok", "assistant"),
            entry("UNFINISHED_TASK"),
            entry('<client_tool_call id="open1">UNFINISHED_CALL</client_tool_call>', "assistant"),
            entry("RECENT_TASK"),
            entry("RECENT_ANSWER", "assistant"),
            entry("NOW"),
        ]
        body, kept, info = b.fit_replay_text("HEADER", entries, "END")
        self.assertTrue(info["compacted"])
        self.assertIn("UNFINISHED_TASK", body)
        self.assertIn("UNFINISHED_CALL", body)
        self.assertIn("NOW", body)
        self.assertEqual(kept[-1]["text"], "NOW")

    def test_tool_digest_keeps_error_evidence_over_padding(self):
        log = ("noise padding line\n" * 400) + "ERROR exit code 2 in bridge.py\n" + ("noise padding line\n" * 400)
        out = entry(f'<client_tool_output call_id="c1">{log}</client_tool_output>')
        out.update(call_id="c1", output=log)
        entries = [
            entry("OLD_TASK"),
            entry('<client_tool_call id="c1">{"name": "bash"}</client_tool_call>', "assistant"),
            out,
            entry("NOW"),
        ]
        with patch.object(b, "MAX_TURN_BYTES", 4000), patch.object(b, "COMPACT_MAX_PARTS", 1):
            body, kept, info = b.fit_replay_text("H", entries, "END")
        self.assertTrue(info["compacted"])
        self.assertIn("NOW", body)
        self.assertIn("ERROR exit code 2 in bridge.py", body)
        self.assertEqual(kept[-1]["text"], "NOW")

if __name__ == "__main__":
    unittest.main()
