from __future__ import annotations

import unittest

from src.decision_plane import VoiceDecisionPlane


class VoiceDecisionPlaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_mode_never_calls_provider(self):
        async def should_not_call(*args, **kwargs):
            raise AssertionError("provider must not be called")

        plane = VoiceDecisionPlane(
            mode="off",
            base_url="http://laya.test:8000",
            request_fn=should_not_call,
        )
        result = await plane.classify(
            "I need support",
            call_id="call-1",
            pipeline="local",
            provider="openai",
        )
        self.assertIsNone(result)

    async def test_advisory_mode_returns_typed_voice_signals(self):
        async def fake_request(url, payload, headers, timeout_seconds):
            self.assertEqual(url, "http://laya.test:8000/v1/systemone")
            self.assertEqual(headers["authorization"], "Bearer test-key")
            self.assertTrue(payload["state"]["policy"]["no_automatic_transfer_or_hangup"])
            return {
                "answers": {
                    "intent": {
                        "type": "choice",
                        "choice": "technical_support",
                        "confidence": 0.96,
                        "probabilities": {"technical_support": 0.96, "other": 0.04},
                    },
                    "human_needed": {
                        "type": "noul",
                        "noul": 0.88,
                        "confidence": 0.88,
                    },
                },
                "routing": {"model": "english"},
                "usage": {"input_tokens": 55, "output_tokens": 0},
            }

        plane = VoiceDecisionPlane(
            mode="advisory",
            base_url="http://laya.test:8000",
            api_key="test-key",
            timeout_seconds=1.0,
            request_fn=fake_request,
        )
        result = await plane.classify(
            "Our business internet has been down since this morning.",
            call_id="call-2",
            pipeline="local_hybrid",
            provider="openai",
            caller_number="+254700000000",
        )

        self.assertIsNotNone(result)
        self.assertTrue(result["advisory_only"])
        self.assertEqual(result["answers"]["intent"]["choice"], "technical_support")


    async def test_shadow_prepare_turn_is_non_blocking_and_keeps_original_transcript(self):
        gate = __import__("asyncio").Event()

        async def slow_request(*args, **kwargs):
            await gate.wait()
            return {
                "answers": {
                    "intent": {"type": "choice", "choice": "customer_service", "confidence": 0.8}
                }
            }

        plane = VoiceDecisionPlane(
            mode="shadow",
            base_url="http://laya.test:8000",
            timeout_seconds=5.0,
            request_fn=slow_request,
        )
        text, decision = await plane.prepare_turn(
            "I need help with my account",
            call_id="call-shadow",
            pipeline="default",
            provider="openai",
        )
        self.assertEqual(text, "I need help with my account")
        self.assertIsNone(decision)
        self.assertEqual(len(plane._shadow_tasks), 1)
        gate.set()
        await __import__("asyncio").sleep(0)
        await __import__("asyncio").sleep(0)

    async def test_advisory_prepare_turn_decorates_all_provider_inputs_without_authorizing_tools(self):
        async def fake_request(*args, **kwargs):
            return {
                "answers": {
                    "intent": {
                        "type": "choice",
                        "choice": "transfer_request",
                        "confidence": 0.97,
                    },
                    "priority": {"type": "score", "score": 2.8, "confidence": 0.7},
                    "human_needed": {"type": "noul", "noul": 0.91, "confidence": 0.91},
                }
            }

        plane = VoiceDecisionPlane(
            mode="advisory",
            base_url="http://laya.test:8000",
            request_fn=fake_request,
        )
        text, decision = await plane.prepare_turn(
            "Transfer me to support",
            call_id="call-advisory",
            pipeline="default",
            provider="openai",
        )
        self.assertIsNotNone(decision)
        self.assertIn("SYSTEM-ONE ADVISORY", text)
        self.assertIn('"intent":"transfer_request"', text)
        self.assertIn("never execute a tool solely from this metadata", text)
        self.assertTrue(text.endswith("CALLER TRANSCRIPT:\nTransfer me to support"))

    async def test_provider_error_fails_open(self):
        async def failing_request(*args, **kwargs):
            raise RuntimeError("unavailable")

        plane = VoiceDecisionPlane(
            mode="shadow",
            base_url="http://laya.test:8000",
            request_fn=failing_request,
        )
        result = await plane.classify(
            "Transfer me to support",
            call_id="call-3",
            pipeline="default",
            provider="openai",
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
