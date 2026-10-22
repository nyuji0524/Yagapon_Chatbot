from bot.operations import OperationalStore


def test_operational_store_persists_jobs_events_and_dead_letters(tmp_path):
    store = OperationalStore(tmp_path / "operations.sqlite3")
    store.record_event(
        "voice", "transcription", "success", guild_id=1, duration_ms=1200,
        details={"chunks": 2},
    )
    job_id = store.start_job("backfill", guild_id=1, progress={"channels_completed": 0})
    store.update_job(job_id, "completed", {"channels_completed": 2})
    store.dead_letter(
        "voice", "session-1", attempts=3, error_type="RateLimitExceeded",
        details={"stage": "transcription"}, retry_after_seconds=60,
    )

    assert store.get_job(job_id)["status"] == "completed"
    assert store.get_job(job_id)["channels_completed"] == 2
    assert store.summary()["pending_dead_letters"] == 1
    assert store.summary()["events"][0]["average_duration_ms"] == 1200
    assert store.pending_dead_letters()[0]["details"] == {"stage": "transcription"}

    store.resolve_dead_letter("voice", "session-1")
    assert store.summary()["pending_dead_letters"] == 0


def test_interrupt_running_jobs_is_component_scoped_and_terminal(tmp_path):
    store = OperationalStore(tmp_path / "operations.sqlite3")
    backfill_id = store.start_job(
        "backfill",
        guild_id=1,
        progress={"status": "running", "channels_completed": 2, "finished_at": None},
    )
    voice_id = store.start_job("voice", guild_id=1, progress={"status": "running"})

    interrupted = store.interrupt_running_jobs(
        component="backfill",
        reason="process_restart",
    )

    assert interrupted == [backfill_id]
    backfill = store.get_job(backfill_id)
    assert backfill["status"] == "interrupted"
    assert backfill["finished_at"] is not None
    assert backfill["interrupted_reason"] == "process_restart"
    assert backfill["channels_completed"] == 2
    assert store.get_job(voice_id)["status"] == "running"
    assert store.summary()["running_jobs"] == 1
