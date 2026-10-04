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
        self.assertFalse(info["compacted"])
        self.assertEqual(body, b.flatten_converted_entries(entries, "END"))
        self.assertEqual(kept, entries)

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
        plan["delta"] = {"text": "NEW", "source": "history", "cid": "cdx1_old", "prev": "r0"}
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


if __name__ == "__main__":
    unittest.main()
