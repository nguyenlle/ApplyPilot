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
