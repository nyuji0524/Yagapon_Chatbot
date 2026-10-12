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
