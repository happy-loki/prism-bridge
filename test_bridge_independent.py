"""Independent offline acceptance checks; no real browser or upstream requests."""
import contextlib
import io
import unittest
from unittest.mock import Mock, patch

import bridge


class IndependentAcceptanceTests(unittest.TestCase):
    def setUp(self):
        settings = patch.multiple(
            bridge,
            MAX_TURN_BYTES=86000,
            MAX_TURN_PARTS=8,
            COMPACT_MAX_PARTS=2,
            PART_GAP_SEC=0,
            CONTINUE_CONVERSATIONS=True,
            CALLER_OWNED_TOOLS=False,
            CATALOG_REFRESH_CHARS=0,
            ALLOW_MODEL_FALLBACK=False,
            DUMP_DIR=None,
            _relay_records={},
            _relay_heads={},
        )
        settings.start()
        self.addCleanup(settings.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def assert_wrapped_fits(self, text):
        pieces = bridge.split_turn_text(text)
        self.assertLessEqual(len(pieces), bridge.MAX_TURN_PARTS)
        for index, piece in enumerate(pieces):
            sent = piece if len(pieces) == 1 else bridge._part_text(
                piece, index + 1, index == len(pieces) - 1
            )
            self.assertLessEqual(bridge.transport_size(sent), bridge.MAX_TURN_BYTES)
        self.assertEqual("".join(pieces), text)

    def request_header(self, request):
        directive, reminder = bridge.relay_directive(
            request, bridge.collect_client_tools(request)
        )
        return f"<relay_instructions>\n{directive}\n</relay_instructions>\n\n", reminder

    def continuation_record(self, request, catalog_matches=True):
        directive, _ = bridge.relay_directive(request, bridge.collect_client_tools(request))
        return {
            "cid": "audit-conversation",
            "rid": "audit-response",
            "catalog": bridge._text_hash(directive) if catalog_matches else "old-catalog",
            "since_catalog": 0,
            "calls": [],
            "snapshot": {"codex_session_id": "audit-session", "transcript_cursor": 2},
        }

    def test_legacy_six_part_success_retains_entire_raw_history(self):
        history = "A" * 100000
        current = "B" * 200000
        request = {"input": [
            {"role": "user", "content": history},
            {"role": "user", "content": current},
        ]}
        header, reminder = self.request_header(request)
        expected = header + bridge.flatten_converted_entries(
            bridge.convert_request_entries(request), reminder
        )
        plan = bridge.build_relay_plan(request, "audit", {}, "audit-model")
        self.assertEqual(plan["full"]["text"], expected)
        self.assertFalse(plan["full"]["compacted"])
        self.assertEqual(bridge.parts_for_transport(expected), 6)
        self.assertIn(history, expected)
        self.assertIn(current, expected)
        self.assert_wrapped_fits(expected)

    def test_previously_fourteen_part_request_now_fits_without_cutting_current(self):
        current = "B" * 200000
        request = {"input": [
            {"role": "user", "content": "A" * 800000},
            {"role": "user", "content": current},
        ]}
        plan = bridge.build_relay_plan(request, "audit", {}, "audit-model")
        selected = plan["full"]
        self.assertTrue(selected["compacted"])
        self.assertIn(current, selected["text"])
        self.assert_wrapped_fits(selected["text"])
        self.assertEqual(plan["hashes"], bridge.entry_prefix_hashes(
            bridge.convert_request_entries(request)
        ))

    def test_disabled_compaction_preserves_exact_original(self):
        entries = [
            {"role": "user", "text": "OLD" * 40000, "images": []},
            {"role": "user", "text": "CURRENT exact", "images": []},
        ]
        with patch.object(bridge, "COMPACT_MAX_PARTS", 0):
            result, kept, info = bridge.fit_replay_text("HEADER", entries, "END")
        self.assertEqual(result, bridge.flatten_converted_entries(entries, "END"))
        self.assertEqual(kept, entries)
        self.assertFalse(info["compacted"])

    def test_small_full_request_is_unchanged(self):
        request = {"input": [{"role": "user", "content": "Old"},
                             {"role": "user", "content": "Current"}]}
        header, reminder = self.request_header(request)
        expected = header + bridge.flatten_converted_entries(
            bridge.convert_request_entries(request), reminder
        )
        plan = bridge.build_relay_plan(request, "audit", {}, "audit-model")
        self.assertEqual(plan["full"]["text"], expected)
        self.assertFalse(plan["full"]["compacted"])

    def test_delta_hit_does_not_build_or_compress_unused_full(self):
        history = "UNUSED_HISTORY " + "A" * 800000
        request = {"input": [{"role": "user", "content": history},
                             {"role": "user", "content": "NEXT"}]}
        record = self.continuation_record(request)
        continuation = {"rec": record, "start": 1, "source": "history"}
        with patch.object(bridge, "find_continuation", return_value=continuation), \
                patch.object(bridge, "fit_replay_text") as compact, \
                patch.object(bridge, "flatten_converted_entries",
                             wraps=bridge.flatten_converted_entries) as flatten:
            plan = bridge.build_relay_plan(request, "audit", {}, "audit-model")
        compact.assert_not_called()
        for recorded in flatten.call_args_list:
            self.assertNotIn(history, [item.get("text") for item in recorded.args[0]])
        self.assertIn("NEXT", plan["delta"]["text"])
        self.assertNotIn(history, plan["delta"]["text"])
        self.assertEqual(plan["delta"]["cid"], record["cid"])
        self.assertEqual(plan["delta"]["prev"], record["rid"])
        self.assertEqual(plan["delta"]["snapshot"], record["snapshot"])

    def test_oversized_unused_catalog_does_not_reject_valid_delta(self):
        tools = [{"type": "function", "name": f"tool_{index}",
                  "description": "D" * 1200,
                  "parameters": {"type": "object", "properties": {}}}
                 for index in range(100)]
        request = {"tools": tools, "input": [
            {"role": "user", "content": "Old"},
            {"role": "user", "content": "NEXT"},
        ]}
        record = self.continuation_record(request)
        header, _ = self.request_header(request)
        with patch.object(bridge, "MAX_TURN_BYTES", 12000):
            self.assertGreater(bridge.parts_for_transport(header), bridge.MAX_TURN_PARTS)
            with patch.object(bridge, "find_continuation", return_value={
                    "rec": record, "start": 1, "source": "history"}), \
                    patch.object(bridge, "fit_replay_text") as compact:
                plan = bridge.build_relay_plan(request, "audit", {}, "audit-model")
            compact.assert_not_called()
            self.assertIn("NEXT", plan["delta"]["text"])
            self.assertNotIn("<relay_instructions>", plan["delta"]["text"])
            self.assert_wrapped_fits(plan["delta"]["text"])

    def test_catalog_refresh_remains_in_delta_without_building_full(self):
        request = {"tools": [{"type": "function", "name": "audit_tool",
                              "parameters": {"type": "object", "properties": {}}}],
                   "input": [{"role": "user", "content": "Old"},
                             {"role": "user", "content": "NEXT"}]}
        record = self.continuation_record(request, catalog_matches=False)
        with patch.object(bridge, "find_continuation", return_value={
                "rec": record, "start": 1, "source": "history"}), \
                patch.object(bridge, "fit_replay_text") as compact:
            plan = bridge.build_relay_plan(request, "audit", {}, "audit-model")
        compact.assert_not_called()
        self.assertIn("<relay_instructions>", plan["delta"]["text"])
        self.assertIn("audit_tool", plan["delta"]["text"])
        self.assertIn("NEXT", plan["delta"]["text"])
        self.assert_wrapped_fits(plan["delta"]["text"])

    def test_transport_shapes_preserve_content_and_wrapped_budget(self):
        shapes = ("A", "汉", "🙂", '\\"', "\n", "\t", "\x00", "row" * 33 + "\n")
        with patch.object(bridge, "MAX_TURN_BYTES", 12000):
            for token in shapes:
                with self.subTest(token=repr(token)):
                    text = token * (500 if len(token) > 10 else 6000)
                    self.assert_wrapped_fits(text)

    def test_local_oversized_delta_can_safely_use_legal_full(self):
        request = {"input": [{"role": "user", "content": "A" * 120000},
                             {"role": "user", "content": "CURRENT exact"}]}
        record = self.continuation_record(request)
        with patch.object(bridge, "MAX_TURN_BYTES", 12000), \
                patch.object(bridge, "find_continuation", return_value={
                    "rec": record, "start": 0, "source": "history"}):
            plan = bridge.build_relay_plan(request, "audit", {}, "audit-model")
            controller = self.fake_controller()

            def create_conversation():
                controller._stored_conversations.add("audit-new-conversation")
                return "audit-new-conversation"

            def answer(items, model, effort, images, conversation, tools, previous, snapshot):
                text = items[-1]["content"][0]["text"]
                self.assertLessEqual(bridge.transport_size(text), bridge.MAX_TURN_BYTES)
                return {"text": "ANSWER", "cid": conversation, "rid": "audit-new-response"}

            with patch.object(controller, "new_conversation", side_effect=create_conversation) as create, \
                    patch.object(controller, "chat", side_effect=answer) as upstream:
                result = controller.relay(plan, "high")
            self.assertEqual(result["mode"], "full")
            create.assert_called_once()
            upstream.assert_called_once()
            self.assertIn("CURRENT exact", upstream.call_args.args[0][-1]["content"][0]["text"])

    def fake_controller(self):
        controller = bridge.PrismPage()
        controller.sandbox = {"pid": "audit-project", "sandboxUrl": "https://offline.invalid",
                              "sandboxToken": "offline-token"}
        controller.cookie = "offline-cookie"
        controller.user_id = "audit-user"
        return controller

    def test_relay_does_not_full_replay_unknown_failure(self):
        failures = (RuntimeError("HTTP 401 unknown execution state"),
                    RuntimeError("HTTP 403 unknown execution state"),
                    TimeoutError("timeout after POST"),
                    ConnectionError("connection dropped after POST"),
                    bridge.PlaywrightError("page lost after POST"))
        for failure in failures:
            with self.subTest(failure=str(failure)):
                controller = self.fake_controller()
                plan = {"delta": {"text": "NEXT", "source": "history",
                                   "cid": "audit-conversation", "prev": "audit-response"},
                        "full": {"text": "FULL", "source": "new", "cid": None, "prev": None},
                        "model": "audit-model", "tools": []}
                with patch.object(controller, "_send_parts", side_effect=failure) as sender:
                    with self.assertRaises(Exception):
                        controller.relay(plan, "high")
                self.assertEqual(sender.call_count, 1)

    def test_chat_does_not_retry_unknown_once_failure(self):
        failures = (RuntimeError("HTTP 401 unknown execution state"),
                    RuntimeError("HTTP 403 unknown execution state"),
                    RuntimeError("no request_id after POST"),
                    TimeoutError("timeout after POST"),
                    bridge.PlaywrightError("page lost after POST"))
        for failure in failures:
            with self.subTest(failure=str(failure)):
                controller = self.fake_controller()
                with patch.object(controller, "_chat_once", side_effect=failure) as once, \
                        patch.object(controller, "boot") as reboot, \
                        patch.object(controller, "recover") as recover:
                    with self.assertRaises(Exception):
                        controller.chat(bridge.upstream_items("NEXT"), "audit-model", "high")
                self.assertEqual(once.call_count, 1)
                reboot.assert_not_called()
                recover.assert_not_called()

    def test_actual_post_unknown_response_is_not_resubmitted(self):
        responses = ({"status": 200, "json": {"status": "in_progress"}},
                     {"status": 401, "text": "authentication rejected", "json": {}},
                     {"status": 403, "text": "authentication rejected", "json": {}})
        for response in responses:
            with self.subTest(status=response["status"]):
                controller = self.fake_controller()
                with patch.object(controller, "fetch", return_value=response) as fetch, \
                        patch.object(controller, "upload_pending_images", return_value=[]), \
                        patch.object(controller, "_listen_snapshot", return_value={}), \
                        patch.object(controller, "boot") as reboot:
                    with self.assertRaises(Exception):
                        controller.chat(bridge.upstream_items("NEXT"), "audit-model", "high")
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(fetch.call_args.args[:2],
                                 ("POST", "/api/llm/response_with_tools_start"))
                reboot.assert_not_called()

    def test_http_oversized_current_is_context_error_before_worker_or_upload(self):
        content = [{"type": "input_text", "text": "X" * 200000},
                   {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]
        for route in ("/v1/responses", "/responses", "/v1/chat/completions"):
            for streaming in (False, True):
                with self.subTest(route=route, streaming=streaming):
                    key = "messages" if route.endswith("chat/completions") else "input"
                    body = {key: [{"role": "user", "content": content}],
                            "stream": streaming, "model": "audit-model"}
                    handler = object.__new__(bridge.Handler)
                    handler.path = route
                    handler.headers = {"Host": "127.0.0.1", "X-User-Id": "audit"}
                    handler._read_json = Mock(return_value=body)
                    handler._reject_foreign = Mock(return_value=False)
                    handler._reject_auth = Mock(return_value=False)
                    handler._send = Mock()
                    handler._sse_begin = Mock()
                    handler._sse = Mock()
                    handler._sse_data = Mock()
                    handler._start_sse_heartbeat = Mock()
                    with patch.object(bridge, "MAX_TURN_BYTES", 12000), \
                            patch.object(bridge, "get_worker") as worker, \
                            patch.object(bridge.PrismPage, "new_conversation") as create, \
                            patch.object(bridge.PrismPage, "upload_pending_images") as upload:
                        handler.do_POST()
                    worker.assert_not_called()
                    create.assert_not_called()
                    upload.assert_not_called()
                    errors = []
                    for response_call in handler._send.call_args_list:
                        status, payload = response_call.args
                        self.assertEqual(status, 400)
                        errors.append(payload.get("error", {}))
                    for response_call in handler._sse.call_args_list:
                        event, payload = response_call.args
                        if event == "response.failed":
                            errors.append(payload.get("response", {}).get("error", {}))
                    for response_call in handler._sse_data.call_args_list:
                        payload = response_call.args[0]
                        if isinstance(payload, dict) and "error" in payload:
                            errors.append(payload["error"])
                    self.assertTrue(errors)
                    self.assertTrue(all(error.get("code") == "context_length_exceeded"
                                        for error in errors))

    def test_verified_missing_conversation_replays_full_exactly_once(self):
        controller = self.fake_controller()
        plan = {"model": "audit-model", "tools": [],
                "delta": {"text": "NEXT", "images": [], "source": "history",
                          "cid": "audit-conversation", "prev": "audit-response"},
                "full": {"text": "FULL", "images": [], "source": "new",
                         "cid": None, "prev": None}}
        responses = [{"status": 404, "text": "missing conversation", "json": {
                         "error": {"code": "conversation_not_found"}}},
                     {"status": 200, "json": {"status": "completed", "response": {
                         "payload": {"id": "audit-new-response", "output": [
                             {"type": "message", "content": [
                                 {"type": "output_text", "text": "ANSWER"}]}]}}}}]

        def create_conversation():
            controller._stored_conversations.add("audit-new-conversation")
            return "audit-new-conversation"

        with patch.object(controller, "fetch", side_effect=responses) as fetch, \
                patch.object(controller, "upload_pending_images", return_value=[]), \
                patch.object(controller, "_listen_snapshot", return_value={}), \
                patch.object(controller, "new_conversation", side_effect=create_conversation) as create:
            result = controller.relay(plan, "high")
        self.assertEqual(result["text"], "ANSWER")
        self.assertEqual(result["mode"], "full")
        create.assert_called_once()
        self.assertEqual(fetch.call_count, 2)
        first, second = [record.args[2] for record in fetch.call_args_list]
        self.assertEqual(first["conversationId"], "audit-conversation")
        self.assertEqual(second["conversationId"], "audit-new-conversation")
        self.assertIn("previousResponseId", first)
        self.assertNotIn("previousResponseId", second)

    def test_structured_model_refusal_fallback_requires_explicit_optin(self):
        responses = [{"status": 200, "json": {"status": "completed", "response": {
                         "payload": {"reason": "invalid_model", "message": "HTTP 400 model rejected"}}}},
                     {"status": 200, "json": {"status": "completed", "response": {
                         "payload": {"id": "audit-response", "output": [
                             {"type": "message", "content": [
                                 {"type": "output_text", "text": "ANSWER"}]}]}}}}]
        for allowed in (False, True):
            with self.subTest(allowed=allowed):
                controller = self.fake_controller()
                with patch.object(bridge, "ALLOW_MODEL_FALLBACK", allowed), \
                        patch.object(controller, "fetch", side_effect=responses) as fetch, \
                        patch.object(controller, "upload_pending_images", return_value=[]), \
                        patch.object(controller, "_listen_snapshot", return_value={}):
                    if allowed:
                        result = controller.chat(bridge.upstream_items("NEXT"), "audit-model", "high")
                        self.assertEqual(result["text"], "ANSWER")
                        self.assertEqual(result["model"], "auto")
                        self.assertNotIn("model", fetch.call_args.args[2]["metadata"])
                    else:
                        with self.assertRaises(bridge.PrismTurnError):
                            controller.chat(bridge.upstream_items("NEXT"), "audit-model", "high")
                self.assertEqual(fetch.call_count, 2 if allowed else 1)

    def test_start_failure_requires_positive_nonexecution_evidence(self):
        responses = (
            {"status": 401, "text": "This request is too large to send", "json": {}},
            {"status": 403, "text": "This request is too large to send", "json": {}},
            {"status": 400, "text": "bad conversation", "json": {}},
            {"status": 404, "text": "missing conversation", "json": {}},
            {"status": 404, "text": "missing conversation", "json": {
                "request_id": "already-accepted", "error": {"code": "conversation_not_found"}}},
            {"status": 409, "text": "conflict", "json": {
                "status": "in_progress", "error": {"code": "conversation_not_found"}}},
            {"status": 422, "text": "too big", "json": {
                "request_id": "already-accepted", "response": {"payload": {
                    "reason": "conversation_too_large"}}}},
            {"status": 400, "text": "rejected", "json": {"response": {"payload": {
                "reason": "conversation_not_found", "id": "generated-response",
                "output": [{"type": "reasoning", "summary": []}]}}}},
        )
        for response in responses:
            with self.subTest(response=response):
                with self.assertRaises(bridge.PrismTurnError) as raised:
                    bridge._raise_llm_start_failure(response)
                self.assertNotIsInstance(raised.exception, bridge.PrismTooLarge)

    def test_auth_size_words_never_trigger_relay_resubmission(self):
        for status in (401, 403):
            with self.subTest(status=status):
                controller = self.fake_controller()
                plan = {"model": "audit-model", "tools": [],
                        "delta": {"text": "NEXT", "images": [], "source": "history",
                                  "cid": "audit-conversation", "prev": "audit-response"},
                        "full": {"text": "FULL", "images": [], "source": "new",
                                 "cid": "audit-backup", "prev": "audit-backup-response"}}
                response = {"status": status, "text": "This request is too large to send",
                            "json": {}}
                with patch.object(controller, "fetch", return_value=response) as fetch, \
                        patch.object(controller, "upload_pending_images", return_value=[]), \
                        patch.object(controller, "_listen_snapshot", return_value={}), \
                        patch.object(controller, "new_conversation") as create:
                    with self.assertRaises(bridge.PrismTurnError):
                        controller.relay(plan, "high")
                self.assertEqual(fetch.call_count, 1)
                create.assert_not_called()

    def test_postaccepted_size_message_alone_does_not_resubmit(self):
        controller = self.fake_controller()
        plan = {"model": "audit-model", "tools": [],
                "delta": {"text": "NEXT", "images": [], "source": "history",
                          "cid": "audit-conversation", "prev": "audit-response"},
                "full": {"text": "FULL", "images": [], "source": "new",
                         "cid": "audit-backup", "prev": "audit-backup-response"}}
        responses = [{"status": 200, "json": {"status": "in_progress",
                                               "request_id": "already-accepted"}},
                     {"status": 200, "json": {"status": "completed", "response": {
                         "payload": {"message": "This request is too large to send"}}}}]
        with patch.object(controller, "fetch", side_effect=responses) as fetch, \
                patch.object(controller, "upload_pending_images", return_value=[]), \
                patch.object(controller, "_listen_snapshot", return_value={}), \
                patch.object(bridge.time, "sleep"):
            with self.assertRaises(bridge.PrismTurnError):
                controller.relay(plan, "high")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([record.args[1] for record in fetch.call_args_list],
                         ["/api/llm/response_with_tools_start",
                          "/api/llm/response_with_tools_status"])

    def test_model_error_words_do_not_discard_or_resubmit_generated_answer(self):
        response = {"status": 200, "json": {"status": "completed", "response": {
            "payload": {"id": "audit-response", "message": "HTTP 400 invalid model",
                        "output": [{"type": "message", "content": [
                            {"type": "output_text", "text": "ANSWER"}]}]}}}}
        for allowed in (False, True):
            with self.subTest(allowed=allowed):
                controller = self.fake_controller()
                with patch.object(bridge, "ALLOW_MODEL_FALLBACK", allowed), \
                        patch.object(controller, "fetch", return_value=response) as fetch, \
                        patch.object(controller, "upload_pending_images", return_value=[]), \
                        patch.object(controller, "_listen_snapshot", return_value={}):
                    result = controller.chat(bridge.upstream_items("NEXT"), "audit-model", "high")
                self.assertEqual(result["text"], "ANSWER")
                self.assertEqual(result["model"], "audit-model")
                fetch.assert_called_once()

    def test_relay_rejects_retryable_flag_from_poststart_phase(self):
        controller = self.fake_controller()
        plan = {"model": "audit-model", "tools": [],
                "delta": {"text": "NEXT", "source": "history",
                          "cid": "audit-conversation", "prev": "audit-response"},
                "full": {"text": "FULL", "source": "new", "cid": None, "prev": None}}
        refusal = bridge.PrismUnexecutedRefusal(
            "poststart execution unknown", reason="conversation_not_found",
            phase="status", retryable=True)
        with patch.object(controller, "_send_parts", side_effect=refusal) as sender:
            with self.assertRaises(bridge.PrismUnexecutedRefusal):
                controller.relay(plan, "high")
        sender.assert_called_once()

    def test_partial_ack_resplit_keeps_prefix_and_snapshot_without_resending(self):
        controller = self.fake_controller()
        spec = {"text": "A" * 8000 + "\n" + "B" * 10000,
                "images": ["audit-image"], "cid": "audit-conversation", "prev": "seed",
                "snapshot": {"transcript_cursor": 0}}
        attempts = []
        accepted = []

        def answer(items, model, effort, images, conversation, tools, previous, snapshot):
            text = items[-1]["content"][0]["text"]
            attempts.append(text)
            self.assertEqual(previous, "seed" if not accepted else f"r{len(accepted)}")
            self.assertEqual(snapshot, {"transcript_cursor": len(accepted)})
            if len(attempts) == 2:
                raise bridge.PrismTooLarge("native size rejection")
            if len(attempts) > 2:
                self.assertLessEqual(bridge.transport_size(text), 6000)
            accepted.append((text, effort, images, tools))
            return {"text": "ANSWER", "cid": conversation, "rid": f"r{len(accepted)}",
                    "snapshot": {"transcript_cursor": len(accepted)}}

        with patch.object(bridge, "MAX_TURN_BYTES", 12000), \
                patch.object(controller, "chat", side_effect=answer), \
                patch.object(controller, "new_conversation") as create:
            result = controller._send_parts(spec, "audit-model", "high", ["audit_tool"])
        create.assert_not_called()
        self.assertEqual(result["parts"], len(accepted))
        self.assertGreaterEqual(len(accepted), 3)
        self.assertLessEqual(result["parts"], bridge.MAX_TURN_PARTS)
        contents = [text.split(">\n", 1)[1].rsplit("\n</relay_part>", 1)[0]
                    for text, _, _, _ in accepted]
        self.assertEqual("".join(contents), spec["text"])
        self.assertEqual(accepted[-1][1:], ("high", ["audit-image"], ["audit_tool"]))
        self.assertTrue(all(effort == "low" and not images and tools is None
                            for _, effort, images, tools in accepted[:-1]))

    def make_http_handler(self, route, body):
        handler = object.__new__(bridge.Handler)
        handler.path = route
        handler.headers = {"Host": "127.0.0.1", "X-User-Id": "audit"}
        handler._read_json = Mock(return_value=body)
        handler._reject_foreign = Mock(return_value=False)
        handler._reject_auth = Mock(return_value=False)
        handler._send = Mock()
        handler._sse_begin = Mock()
        handler._sse = Mock()
        handler._sse_data = Mock()
        handler._start_sse_heartbeat = Mock()
        return handler

    def test_http_success_preserves_text_and_tool_contracts(self):
        function_call = {"id": "call_audit", "name": "audit_tool",
                         "arguments": '{"path":"safe.txt"}'}
        custom_call = {"id": "call_custom", "name": "audit_custom",
                       "kind": "custom", "input": "print(42)"}
        cases = (("text", "ANSWER", []), ("function", "", [function_call]),
                 ("custom", "", [custom_call]))
        for route in ("/v1/responses", "/responses", "/v1/chat/completions"):
            for streaming in (False, True):
                for kind, text, tool_calls in cases:
                    chat = route.endswith("chat/completions")
                    if chat and kind == "custom":
                        continue
                    with self.subTest(route=route, streaming=streaming, kind=kind):
                        key = "messages" if chat else "input"
                        body = {key: [{"role": "user", "content": "CURRENT exact"}],
                                "stream": streaming, "model": "audit-model"}
                        if kind == "function":
                            body["tools"] = [{"type": "function", "name": "audit_tool",
                                              "parameters": {"type": "object"}}]
                        elif kind == "custom":
                            body["tools"] = [{"type": "custom", "name": "audit_custom"}]
                        handler = self.make_http_handler(route, body)
                        result = {"text": text, "reasoning": "AUDIT_REASONING",
                                  "model": "audit-model", "cid": "audit-conversation",
                                  "rid": "audit-response", "tool_calls": tool_calls}
                        worker = Mock()
                        worker.call.return_value = result
                        with patch.object(bridge, "get_worker", return_value=worker) as get, \
                                patch.object(bridge, "remember_turn") as remember:
                            handler.do_POST()
                        get.assert_called_once()
                        worker.call.assert_called_once()
                        remember.assert_called_once()
                        plan = worker.call.call_args.args[1]
                        selected = plan["delta"] or plan["full"]
                        self.assertIn("CURRENT exact", selected["text"])
                        self.assert_wrapped_fits(selected["text"])
                        if streaming:
                            handler._send.assert_not_called()
                            handler._sse_begin.assert_called_once()
                        else:
                            handler._send.assert_called_once()
                            status, payload = handler._send.call_args.args
                            self.assertEqual(status, 200)
                            self.assertEqual(payload["model"], "audit-model")
                        if chat:
                            finish = "tool_calls" if tool_calls else "stop"
                            if streaming:
                                chunks = [record.args[0] for record in
                                          handler._sse_data.call_args_list]
                                self.assertEqual(chunks[-1], "[DONE]")
                                self.assertEqual(chunks[-2]["choices"][0]["finish_reason"],
                                                 finish)
                                message = chunks[0]["choices"][0]["delta"]
                            else:
                                self.assertEqual(payload["object"], "chat.completion")
                                self.assertEqual(payload["choices"][0]["finish_reason"], finish)
                                message = payload["choices"][0]["message"]
                            self.assertEqual(message["role"], "assistant")
                            if tool_calls:
                                returned = message["tool_calls"][0]
                                self.assertEqual(returned["id"], function_call["id"])
                                self.assertEqual(returned["function"]["name"],
                                                 function_call["name"])
                                self.assertEqual(returned["function"]["arguments"],
                                                 function_call["arguments"])
                            else:
                                self.assertEqual(message["content"], text)
                        else:
                            if streaming:
                                events = [(record.args[0], record.args[1]) for record in
                                          handler._sse.call_args_list]
                                completed = [payload["response"] for event, payload in events
                                             if event == "response.completed"]
                                self.assertEqual(len(completed), 1)
                                payload = completed[0]
                                self.assertEqual(events[0][0], "response.created")
                                if tool_calls:
                                    expected_event = ("response.custom_tool_call_input.done"
                                                      if kind == "custom" else
                                                      "response.function_call_arguments.done")
                                    self.assertEqual(sum(event == expected_event
                                                         for event, _ in events), 1)
                            self.assertEqual(payload["object"], "response")
                            self.assertEqual(payload["status"], "completed")
                            if tool_calls:
                                expected_type = ("custom_tool_call" if kind == "custom"
                                                 else "function_call")
                                calls = [item for item in payload["output"]
                                         if item["type"] == expected_type]
                                self.assertEqual(len(calls), 1)
                                self.assertEqual(calls[0]["call_id"], tool_calls[0]["id"])
                                self.assertEqual(calls[0]["name"], tool_calls[0]["name"])
                                field = "input" if kind == "custom" else "arguments"
                                self.assertEqual(calls[0][field], tool_calls[0][field])
                            else:
                                messages = [item for item in payload["output"]
                                            if item["type"] == "message"]
                                self.assertEqual(len(messages), 1)
                                self.assertEqual(messages[0]["content"][0]["text"], text)

    def test_http_unknown_worker_failure_has_no_second_attempt(self):
        for route in ("/v1/responses", "/v1/chat/completions"):
            for streaming in (False, True):
                with self.subTest(route=route, streaming=streaming):
                    key = "messages" if route.endswith("chat/completions") else "input"
                    body = {key: [{"role": "user", "content": "CURRENT exact"}],
                            "stream": streaming, "model": "audit-model"}
                    handler = self.make_http_handler(route, body)
                    worker = Mock()
                    worker.call.side_effect = TimeoutError("unknown execution after POST")
                    with patch.object(bridge, "get_worker", return_value=worker) as get, \
                            patch.object(bridge, "remember_turn") as remember:
                        handler.do_POST()
                    get.assert_called_once()
                    worker.call.assert_called_once()
                    remember.assert_not_called()
                    if not streaming:
                        handler._send.assert_called_once()
                        self.assertEqual(handler._send.call_args.args[0], 502)
                    elif route.endswith("chat/completions"):
                        chunks = [record.args[0] for record in handler._sse_data.call_args_list]
                        self.assertEqual(chunks[-1], "[DONE]")
                        self.assertIn("error", chunks[0])
                    else:
                        failures = [record.args[1]["response"] for record in
                                    handler._sse.call_args_list
                                    if record.args[0] == "response.failed"]
                        self.assertEqual(len(failures), 1)
                        self.assertEqual(failures[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
