import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onebookwiki.chat_agent import AgentTraceEvent
from server.database import (
    append_chat_agent_step,
    append_chat_turn,
    claim_chat_job,
    complete_chat_turn,
    create_chat_conversation,
    create_placeholder,
    delete_book,
    get_chat_agent_trace,
    get_chat_conversation,
    init_db,
    renew_chat_lease,
    reset_stuck_chat_jobs,
)


class ChatPersistenceTest(unittest.TestCase):
    generation = {
        "provider": "test-provider",
        "model": "test-model",
        "max_output_tokens": 1800,
        "config_hash": "test-config-hash",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "onebookwiki.db"))
        self.book_id = create_placeholder(self.conn, "Chat book")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def create_conversation(self):
        return create_chat_conversation(self.conn, self.book_id, "What is the thesis?", self.generation)

    def test_create_and_complete_conversation(self):
        conversation_id, turn_id = self.create_conversation()
        job = claim_chat_job(self.conn, "worker-a", 60)

        self.assertEqual(job["id"], turn_id)
        self.assertEqual(job["conversation_id"], conversation_id)
        self.assertEqual(job["status"], "retrieving")
        self.assertTrue(complete_chat_turn(
            self.conn,
            turn_id,
            status="succeeded",
            answer="The thesis is grounded in evidence. C1E1",
            citations=[{"evidence_id": "C1E1"}],
            claimed_by="worker-a",
        ))

        result = get_chat_conversation(self.conn, conversation_id)
        self.assertEqual(result["turns"][0]["status"], "succeeded")
        self.assertEqual(result["turns"][0]["citations"], [{"evidence_id": "C1E1"}])

    def test_only_one_turn_can_be_pending(self):
        conversation_id, _ = self.create_conversation()
        with self.assertRaisesRegex(RuntimeError, "conversation_busy"):
            append_chat_turn(self.conn, conversation_id, "Follow up", 10)

    def test_turn_order_advances_after_terminal_result(self):
        conversation_id, first_turn = self.create_conversation()
        claim_chat_job(self.conn, "worker-a", 60)
        complete_chat_turn(self.conn, first_turn, status="refused", refusal_code="raw_evidence_missing", claimed_by="worker-a")

        second_turn = append_chat_turn(self.conn, conversation_id, "Follow up", 10)
        result = get_chat_conversation(self.conn, conversation_id)
        self.assertNotEqual(first_turn, second_turn)
        self.assertEqual([turn["turn_no"] for turn in result["turns"]], [1, 2])

    def test_expired_lease_requeues_turn_before_next_claim(self):
        _, turn_id = self.create_conversation()
        first = claim_chat_job(self.conn, "worker-a", 1)
        self.conn.execute("UPDATE chat_jobs SET lease_until = '2000-01-01T00:00:00Z' WHERE turn_id = ?", (turn_id,))
        self.conn.commit()

        second = claim_chat_job(self.conn, "worker-b", 60)
        self.assertEqual(first["id"], turn_id)
        self.assertEqual(second["id"], turn_id)
        self.assertEqual(second["status"], "retrieving")
        self.assertEqual(second["attempt"], 2)

    def test_stale_worker_cannot_complete_reclaimed_turn(self):
        _, turn_id = self.create_conversation()
        claim_chat_job(self.conn, "worker-a", 1)
        self.conn.execute("UPDATE chat_jobs SET lease_until = '2000-01-01T00:00:00Z' WHERE turn_id = ?", (turn_id,))
        self.conn.commit()
        claim_chat_job(self.conn, "worker-b", 60)

        self.assertFalse(complete_chat_turn(self.conn, turn_id, status="failed", error_code="old_worker", claimed_by="worker-a"))
        self.assertTrue(complete_chat_turn(self.conn, turn_id, status="failed", error_code="new_worker", claimed_by="worker-b"))
        result = self.conn.execute("SELECT error_code FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
        self.assertEqual(result[0], "new_worker")

    def test_restart_requeues_expired_job_and_generating_turn(self):
        _, turn_id = self.create_conversation()
        claim_chat_job(self.conn, "worker-a", 60)
        self.conn.execute("UPDATE chat_turns SET status = 'generating' WHERE id = ?", (turn_id,))
        self.conn.execute("UPDATE chat_jobs SET lease_until = '2000-01-01T00:00:00Z' WHERE turn_id = ?", (turn_id,))
        self.conn.commit()

        self.assertEqual(reset_stuck_chat_jobs(self.conn), 1)
        status = self.conn.execute("SELECT status FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
        self.assertEqual(status[0], "queued")

    def test_lease_renewal_requires_an_unexpired_owner(self):
        _, turn_id = self.create_conversation()
        claim_chat_job(self.conn, "worker-a", 60)
        self.assertTrue(renew_chat_lease(self.conn, turn_id, "worker-a", 60))
        self.conn.execute("UPDATE chat_jobs SET lease_until = '2000-01-01T00:00:00Z' WHERE turn_id = ?", (turn_id,))
        self.conn.commit()
        self.assertFalse(renew_chat_lease(self.conn, turn_id, "worker-a", 60))
        self.assertFalse(complete_chat_turn(
            self.conn, turn_id, status="failed", error_code="expired", claimed_by="worker-a"
        ))

    def test_agent_trace_is_attempt_scoped_and_requires_ownership(self):
        _, turn_id = self.create_conversation()
        job = claim_chat_job(self.conn, "worker-a", 60)
        event = AgentTraceEvent(
            step_no=1, phase="retrieving", action="book_overview",
            request={"arguments": {}}, result={"title": "Test"}, prompt_hash="abc",
            usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        )
        self.assertTrue(append_chat_agent_step(
            self.conn, turn_id, int(job["attempt"]), event, claimed_by="worker-a"
        ))
        trace = get_chat_agent_trace(self.conn, turn_id)
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["action"], "book_overview")
        self.assertEqual(trace[0]["request"], {"arguments": {}})
        self.assertFalse(append_chat_agent_step(
            self.conn, turn_id, int(job["attempt"]), event, claimed_by="worker-b"
        ))

    def test_book_delete_cascades_conversations_turns_and_jobs(self):
        conversation_id, turn_id = self.create_conversation()
        self.assertTrue(delete_book(self.conn, self.book_id))

        self.assertIsNone(get_chat_conversation(self.conn, conversation_id))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM chat_jobs WHERE turn_id = ?", (turn_id,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
