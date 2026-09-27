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
