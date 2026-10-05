"""Acceptance tests for the inheritance-interval audit.

Covers the duty-officer review feature: continuous intervals over a whole
frozen trajectory where a task's effective priority deviates from its
base priority, including:

* two-hop (nested) inheritance;
* interval continuity across source-task changes;
* release fall-back;
* tasks that never inherit;
* actionable failures (unknown task, frozen illegal verdict, unknown read).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from engine import Engine  # noqa: E402
from inheritance import AuditError, inheritance_report  # noqa: E402
from store import VerdictStore, normalize  # noqa: E402


def verdict(tasks, locks, events):
    return Engine(tasks, locks).apply(events)


TWOHOP = dict(
    tasks=[{"id": 1, "priority": 8}, {"id": 2, "priority": 5}, {"id": 3, "priority": 1}],
    locks=[{"id": 1, "priority": 9}, {"id": 2, "priority": 9}],
    events=[
        {"type": "acquire", "taskId": 2, "lockId": 2},
        {"type": "acquire", "taskId": 1, "lockId": 1},
        {"type": "acquire", "taskId": 2, "lockId": 1},
        {"type": "acquire", "taskId": 3, "lockId": 2},
        {"type": "release", "taskId": 1, "lockId": 1},
        {"type": "release", "taskId": 2, "lockId": 1},
        {"type": "release", "taskId": 2, "lockId": 2},
    ],
)


class TwoHopAuditTests(unittest.TestCase):
    def setUp(self):
        self.r = verdict(**TWOHOP)
        self.assertTrue(self.r["accepted"])

    def test_outer_holder_interval_spans_two_hop_onset(self):
        # Task 1 (base 8) is boosted from event 3 (B queues on L1, eff 5)
        # through event 4 (C -> B -> A, eff 1), and falls back at event 5
        # when it hands L1 away.
        rep = inheritance_report(self.r, 1)
        self.assertEqual(rep["basePriority"], 8)
        self.assertEqual(rep["intervalCount"], 1)
        seg = rep["intervals"][0]
        self.assertEqual((seg["startEventIndex"], seg["endEventIndex"]), (3, 4))
        self.assertEqual(seg["mostUrgentPriority"], 1)
        # Source at start is task 2; task 3 first appears at the next
        # snapshot -> de-duplicated, first-appearance order: [2, 3].
        self.assertEqual(seg["sourceTaskIds"], [2, 3])
        self.assertEqual(seg["startEvent"], {"type": "acquire", "taskId": 2, "lockId": 1})
        self.assertEqual(seg["endEvent"], {"type": "acquire", "taskId": 3, "lockId": 2})

    def test_start_and_end_carry_reviewable_wait_chain_evidence(self):
        seg = inheritance_report(self.r, 1)["intervals"][0]
        start, end = seg["startEvidence"], seg["endEvidence"]
        self.assertEqual(start["stepIndex"], 3)
        self.assertEqual(end["stepIndex"], 4)
        # At the start only the single-hop chain exists; at the end the
        # nested two-hop chain C -> B -> A is reviewable.
        self.assertEqual(start["waitChains"], [[2, 1]])
        self.assertIn([3, 2, 1], end["waitChains"])
        self.assertIn([2, 1], end["waitChains"])
        self.assertEqual(start["taskState"]["effectivePriority"], 5)
        self.assertEqual(end["taskState"]["effectivePriority"], 1)
        self.assertEqual(end["taskState"]["inheritedFrom"], [2, 3])
        for ev in (start, end):
            self.assertIsNotNone(ev["event"])
            self.assertEqual(ev["taskState"]["id"], 1)

    def test_middle_holder_inherits_two_hops_then_keeps_boost(self):
        # Task 2 (base 5) only deviates once C queues behind it (event 4),
        # stays boosted while it holds both locks, falls back when L2 is
        # handed to C at event 7 -> interval covers events 4..6.
        rep = inheritance_report(self.r, 2)
        self.assertEqual(rep["intervalCount"], 1)
        seg = rep["intervals"][0]
        self.assertEqual((seg["startEventIndex"], seg["endEventIndex"]), (4, 6))
        self.assertEqual(seg["mostUrgentPriority"], 1)
        self.assertEqual(seg["sourceTaskIds"], [3])
        # Endpoint evidence: still inheriting at event 6, chain C -> B.
        self.assertEqual(seg["endEvidence"]["waitChains"], [[3, 2]])

    def test_urgent_leaf_task_never_inherits(self):
        # Task 3 has the most urgent base priority (1): it is always the
        # source, never the target, so no interval exists.
        rep = inheritance_report(self.r, 3)
        self.assertEqual(rep["intervalCount"], 0)
        self.assertEqual(rep["intervals"], [])
        self.assertEqual(rep["basePriority"], 1)


class ContinuityAcrossSourceChangeTests(unittest.TestCase):
    def test_source_change_keeps_one_continuous_interval(self):
        # A(8) holds L1. Urgent B(1) queues (event 2); C(2) queues too
        # (event 3); B cancels (event 4). A remains continuously boosted:
        # the source changes B -> C, but deviation never breaks.
        r = verdict(
            [{"id": 1, "priority": 8}, {"id": 2, "priority": 1}, {"id": 3, "priority": 2}],
            [{"id": 1, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 1},
                {"type": "acquire", "taskId": 3, "lockId": 1},
                {"type": "cancel", "taskId": 2},
            ],
        )
        rep = inheritance_report(r, 1)
        self.assertEqual(rep["intervalCount"], 1)
        seg = rep["intervals"][0]
        self.assertEqual((seg["startEventIndex"], seg["endEventIndex"]), (2, 4))
        self.assertEqual(seg["mostUrgentPriority"], 1)
        # B appeared first; after B cancels C remains the only source.
        self.assertEqual(seg["sourceTaskIds"], [2, 3])
        self.assertEqual(seg["endEvidence"]["taskState"]["inheritedFrom"], [3])
        self.assertEqual(seg["endEvidence"]["taskState"]["effectivePriority"], 2)

    def test_boost_break_starts_new_interval(self):
        # Boost, fall back, then boost again via a different waiter: two
        # distinct intervals must be reported.
        r = verdict(
            [{"id": 1, "priority": 8}, {"id": 2, "priority": 1}, {"id": 3, "priority": 2}],
            [{"id": 1, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 1},
                {"type": "cancel", "taskId": 2},   # boost ends
                {"type": "acquire", "taskId": 3, "lockId": 1},
            ],
        )
        rep = inheritance_report(r, 1)
        self.assertEqual(rep["intervalCount"], 2)
        self.assertEqual(
            [(s["startEventIndex"], s["endEventIndex"], s["mostUrgentPriority"])
             for s in rep["intervals"]],
            [(2, 2, 1), (4, 4, 2)],
        )
        self.assertEqual(rep["intervals"][0]["sourceTaskIds"], [2])
        self.assertEqual(rep["intervals"][1]["sourceTaskIds"], [3])


class ReleaseFallbackAuditTests(unittest.TestCase):
    def test_release_ends_interval_and_falls_back(self):
        r = verdict(
            [{"id": 1, "priority": 8}, {"id": 2, "priority": 1}],
            [{"id": 1, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 1},
                {"type": "release", "taskId": 1, "lockId": 1},
                {"type": "release", "taskId": 2, "lockId": 1},
            ],
        )
        rep = inheritance_report(r, 1)
        # Single-event interval: boosted only while T2 queues, back to
        # base 8 immediately after the handover.
        self.assertEqual(rep["intervalCount"], 1)
        seg = rep["intervals"][0]
        self.assertEqual((seg["startEventIndex"], seg["endEventIndex"]), (2, 2))
        self.assertEqual(seg["mostUrgentPriority"], 1)
        self.assertEqual(seg["sourceTaskIds"], [2])
        self.assertEqual(seg["startEvidence"]["taskState"]["effectivePriority"], 1)

    def test_waiter_with_own_base_urgency_is_not_an_interval(self):
        # T2 (base 1) is the urgent waiter: it never deviates itself.
        r = verdict(
            [{"id": 1, "priority": 8}, {"id": 2, "priority": 1}],
            [{"id": 1, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 1},
                {"type": "release", "taskId": 1, "lockId": 1},
            ],
        )
        self.assertEqual(inheritance_report(r, 2)["intervalCount"], 0)


class AuditFailureTests(unittest.TestCase):
    def test_unknown_task_is_actionable_404(self):
        r = verdict(
            [{"id": 1, "priority": 8}], [{"id": 1, "priority": 9}],
            [{"type": "acquire", "taskId": 1, "lockId": 1}],
        )
        with self.assertRaises(AuditError) as ctx:
            inheritance_report(r, 99)
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "task_not_found")
        self.assertEqual(ctx.exception.extra["availableTaskIds"], [1])
        self.assertIn("99", ctx.exception.message)

    def test_frozen_illegal_verdict_has_no_auditable_range(self):
        r = verdict(
            [{"id": 1, "priority": 8}, {"id": 2, "priority": 5}],
            [{"id": 1, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "release", "taskId": 2, "lockId": 1},
            ],
        )
        self.assertFalse(r["accepted"])
        with self.assertRaises(AuditError) as ctx:
            inheritance_report(r, 1)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "verdict_frozen")
        self.assertEqual(ctx.exception.extra["errorIndex"], 2)

    def test_audit_reads_frozen_store_record_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = VerdictStore(os.path.join(tmp, "v.json"))
            sub = normalize({
                "auditId": "AUD",
                "tasks": [{"id": 1, "priority": 8}, {"id": 2, "priority": 1}],
                "locks": [{"id": 1, "priority": 9}],
                "events": [
                    {"type": "acquire", "taskId": 1, "lockId": 1},
                    {"type": "acquire", "taskId": 2, "lockId": 1},
                ],
            })
            rec, status = store.submit(sub)
            self.assertEqual(status, 200)
            fetched = store.get("AUD")
            rep = inheritance_report(fetched["verdict"], 1)
            self.assertEqual(rep["intervalCount"], 1)
            self.assertEqual(rep["intervals"][0]["sourceTaskIds"], [2])


if __name__ == "__main__":
    unittest.main()
