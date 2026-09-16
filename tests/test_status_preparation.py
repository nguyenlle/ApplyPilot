"""Discovery is not reported as application-ready; aggregate explicit/implicit queues."""

from applypilot import database, policy


def test_application_states_distinguish_unprepared_and_group_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "load_policy", lambda: policy.RuntimePolicy(min_score=8))
    conn = database.init_db(tmp_path / "jobs.db")
    rows = [
        ("discovered", None, None, None),
        ("scored", 9, None, None),
        ("below_threshold", 7, "resume.txt", None),
        ("ready", 8, "resume.txt", None),
        ("explicit_queue", 8, "resume.txt", "queued"),
        ("held", 9, "resume.txt", "missing_info"),
    ]
    conn.executemany("INSERT INTO jobs(url,fit_score,tailored_resume_path,apply_status) VALUES (?,?,?,?)", rows)
    conn.commit()
    assert database.get_stats(conn)["application_states"] == {
        "preparation_pending": 3, "queued": 2, "missing_info": 1,
    }
    conn.close()


def test_prepared_count_and_tailoring_respect_configured_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "load_policy", lambda: policy.RuntimePolicy(min_score=8))
    conn = database.init_db(tmp_path / "counts.db")
    rows = [
        ("below", 6, "resume.txt", "https://example.test/1", None),
        ("empty", 8, "", "https://example.test/2", None),
        ("no_url", 8, "resume.txt", "", None),
        ("claimed", 8, "resume.txt", "https://example.test/4", "owned"),
        ("prepared", 8, "resume.txt", "https://example.test/5", None),
        ("needs_tailoring", 8, None, "https://example.test/6", None),
        ("below_tailoring", 7, None, "https://example.test/7", None),
    ]
    conn.executemany("INSERT INTO jobs(url,fit_score,tailored_resume_path,application_url,claim_token,full_description) "
                     "VALUES (?,?,?,?,?,'Description')", rows)
    conn.commit()
    stats = database.get_stats(conn)
    assert stats["min_score"] == 8
    assert stats["ready_to_apply"] == 1
    assert stats["untailored_eligible"] == 2
    conn.close()
