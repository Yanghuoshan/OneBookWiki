#!/usr/bin/env python3
"""Diagnose chat worker issues."""
import sqlite3
import sys
from pathlib import Path

def main():
    print("=== OneBookWiki Chat Worker Diagnostics ===\n")

    # 1. Find database
    from server.config import db_path
    db = db_path()
    print(f"1. Database path: {db}")

    if not db.exists():
        print(f"   ERROR: Database does not exist at {db}")
        return 1

    print(f"   ✓ Database exists\n")

    # 2. Check tables
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"2. Database tables: {', '.join(tables)}")

    required_tables = ['books', 'chat_conversations', 'chat_turns', 'chat_jobs']
    missing = [t for t in required_tables if t not in tables]
    if missing:
        print(f"   ERROR: Missing tables: {', '.join(missing)}")
        print("   Run migration or reinitialize database")
        return 1

    print(f"   ✓ All required tables exist\n")

    # 3. Check chat_jobs
    job_count = conn.execute("SELECT COUNT(*) FROM chat_jobs").fetchone()[0]
    print(f"3. Total chat jobs: {job_count}")

    if job_count == 0:
        print("   No jobs found. Try creating a question first.")
        return 0

    # Show recent jobs
    jobs = conn.execute("""
        SELECT j.turn_id, j.status, j.attempt, j.claimed_by, j.created_at,
               t.question, t.status as turn_status
        FROM chat_jobs j
        JOIN chat_turns t ON t.id = j.turn_id
        ORDER BY j.created_at DESC
        LIMIT 5
    """).fetchall()

    print(f"\n   Recent jobs:")
    for job in jobs:
        print(f"   - Turn: {job['turn_id'][:16]}...")
        print(f"     Job status: {job['status']}, Turn status: {job['turn_status']}")
        print(f"     Attempt: {job['attempt']}, Claimed by: {job['claimed_by'] or 'none'}")
        print(f"     Question: {job['question'][:50]}...")
        print()

    # 4. Check queued jobs
    queued = conn.execute("""
        SELECT COUNT(*) FROM chat_jobs j
        JOIN chat_turns t ON t.id = j.turn_id
        WHERE j.status = 'queued' AND t.status = 'queued'
    """).fetchone()[0]

    print(f"4. Queued jobs (ready to process): {queued}")

    if queued == 0:
        print("   No queued jobs. All jobs may be completed or stuck.")
    else:
        print(f"   ✓ {queued} job(s) waiting for worker\n")

    # 5. Check generation config
    print("5. Generation configuration:")
    try:
        from onebookwiki.providers import GenerationConfig
        config = GenerationConfig.from_env()
        print(f"   Provider: {config.provider}")
        print(f"   Model: {config.model}")
        print(f"   Max tokens: {config.max_output_tokens}")

        if config.provider in {"", "none"}:
            print("   ERROR: Provider not configured!")
            print("   Set GENERATION_PROVIDER environment variable")
            return 1

        print(f"   ✓ Generation provider configured\n")
    except Exception as e:
        print(f"   ERROR: {e}")
        return 1

    # 6. Test worker claim
    print("6. Testing job claim mechanism:")
    try:
        from server.database import claim_chat_job
        from server.config import ChatSettings

        settings = ChatSettings.from_env()
        job = claim_chat_job(conn, "diagnostic-test", settings.lease_seconds)

        if job:
            print(f"   ✓ Successfully claimed job: {job['id'][:16]}...")
            print(f"   Status changed to: {job['status']}")
            # Rollback to not interfere with real worker
            conn.rollback()
            print("   (Rolled back for diagnostic)")
        else:
            print("   No jobs available to claim")
    except Exception as e:
        print(f"   ERROR during claim: {e}")
        import traceback
        traceback.print_exc()
        return 1

    print("\n7. Recommendations:")
    if queued > 0:
        print("   - Chat worker should be running to process queued jobs")
        print("   - Start with: ./start.sh --chat-worker")
        print("   - Or check logs: tail -f logs/chat_worker.log")
    else:
        print("   - No pending jobs. Try submitting a question from the web UI")

    conn.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
