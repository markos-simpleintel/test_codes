import unittest

import pjsip_test_call as workflow


class PjsipTestCallWorkflowTests(unittest.TestCase):
    def test_classifies_latency_events(self):
        self.assertEqual(workflow.classify_event("[call-01] call state: CONFIRMED | x"), "call_connected")
        self.assertEqual(workflow.classify_event("[call-02] media is ready"), "media_ready")
        self.assertEqual(workflow.classify_event("[call-02] action sequence complete"), "action_sequence_complete")
        self.assertEqual(workflow.classify_event("[call-01] AMI transfer detected: DialBegin"), "transfer_detected")

    def test_extracts_structured_identity_without_inventing_numbers(self):
        events = [{
            "timestamp_utc": "2026-08-11T10:00:00.000Z",
            "elapsed_ms": 12.0,
            "message": '[TEST CALL] {"stage":"Jane","caller_number":"1001"}',
        }]
        identities = workflow.extract_identities(events)
        self.assertEqual(identities[0]["stage"], "Jane")
        self.assertEqual(identities[0]["caller_number"], "1001")

    def test_boolean_configuration(self):
        for value in ("1", "true", "YES", "on"):
            self.assertTrue(workflow.enabled(value))
        self.assertFalse(workflow.enabled("0"))

    def test_summarizes_per_call_latency(self):
        events = [
            {"call_id": 1, "event": "call_started", "elapsed_ms": 100.0},
            {"call_id": 1, "event": "call_connected", "elapsed_ms": 350.0},
            {"call_id": 1, "event": "media_ready", "elapsed_ms": 400.0},
            {"call_id": 1, "event": "action_sequence_complete", "elapsed_ms": 900.0},
        ]
        metric = workflow.summarize_call_metrics(events, 1)[0]
        self.assertEqual(metric["connect_latency_ms"], 250.0)
        self.assertEqual(metric["completion_latency_ms"], 800.0)


if __name__ == "__main__":
    unittest.main()
