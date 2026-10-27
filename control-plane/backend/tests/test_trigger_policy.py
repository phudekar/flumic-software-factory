"""Observed source changes, rather than delivery retries, authorize work."""

import unittest
from app.trigger_policy import Observation, observe


class TriggerPolicyTests(unittest.TestCase):
    def test_ready_issue_runs_once_until_observed_unready(self):
        state, outcome = observe(
            "issue-ready", Observation(), eligible=True, stamp="comment-1", active=False
        )
        self.assertEqual(outcome, "accepted")
        state, outcome = observe(
            "issue-ready", state, eligible=True, stamp="comment-2", active=False
        )
        self.assertEqual(outcome, "ignored")
        state, outcome = observe(
            "issue-ready", state, eligible=False, stamp="comment-3", active=False
        )
        self.assertEqual(outcome, "ignored")
        state, outcome = observe(
            "issue-ready", state, eligible=True, stamp="comment-4", active=False
        )
        self.assertEqual(outcome, "accepted")
        self.assertEqual(state.epoch, 2)

    def test_feedback_pending_during_active_run_is_preserved(self):
        state, _ = observe(
            "feedback", Observation(), eligible=True, stamp="41", active=False
        )
        state, outcome = observe(
            "feedback", state, eligible=True, stamp="42", active=True
        )
        self.assertEqual(outcome, "coalesced")
        self.assertEqual(state.dispatched_stamp, "41")
        state, outcome = observe(
            "feedback", state, eligible=True, stamp="42", active=False
        )
        self.assertEqual(outcome, "accepted")
        _, outcome = observe("feedback", state, eligible=True, stamp="42", active=False)
        self.assertEqual(outcome, "ignored")

    def test_unready_and_absent_merge_proof_never_launch(self):
        for kind in ("issue-ready", "feedback", "verify"):
            _, outcome = observe(
                kind, Observation(), eligible=False, stamp="42", active=False
            )
            self.assertEqual(outcome, "ignored")
        _, outcome = observe(
            "verify", Observation(), eligible=True, stamp="", active=False
        )
        self.assertEqual(outcome, "ignored")

    def test_ready_transition_during_active_run_waits_for_completion(self):
        state, outcome = observe(
            "issue-ready", Observation(), eligible=True, stamp="", active=True
        )
        self.assertEqual(outcome, "coalesced")
        state, outcome = observe(
            "issue-ready", state, eligible=True, stamp="", active=False
        )
        self.assertEqual(outcome, "accepted")
        _, outcome = observe(
            "issue-ready", state, eligible=True, stamp="", active=False
        )
        self.assertEqual(outcome, "ignored")

    def test_unknown_kind_fails_closed(self):
        with self.assertRaises(ValueError):
            observe(
                "payload-selected-workflow",
                Observation(),
                eligible=True,
                stamp="42",
                active=False,
            )

    def test_older_feedback_note_id_cannot_rearm_delivery(self):
        state, _ = observe(
            "feedback", Observation(), eligible=True, stamp="42", active=False
        )
        _, outcome = observe("feedback", state, eligible=True, stamp="41", active=False)
        self.assertEqual(outcome, "ignored")
