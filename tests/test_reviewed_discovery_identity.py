"""Official reviewed requisitions disambiguate same-title jobs without duplicate locations."""

from applypilot.database import init_db, store_jobs


def test_distinct_reviewed_requisitions_with_same_title_are_retained(tmp_path):
    conn = init_db(tmp_path / 'jobs.db')
    common = {'company': 'Example Hardware', 'title': 'Product Design Engineer', 'location': 'Sunnyvale, CA'}
    rows = [{**common, 'url': f'https://careers.example.test/{key}', 'verified_requisition_id': key}
            for key in ('mechatronics-123', 'home-456')]
    assert store_jobs(conn, rows, 'official', 'reviewed') == (2, 0)
    assert store_jobs(conn, rows, 'official', 'reviewed') == (0, 2)
    conn.close()


def test_same_verified_requisition_across_locations_is_one_job(tmp_path):
    conn = init_db(tmp_path / 'jobs.db')
    common = {'company': 'Example Hardware', 'title': 'Product Design Engineer', 'verified_requisition_id': '123'}
    rows = [{**common, 'url': f'https://careers.example.test/123/{location}', 'location': location}
            for location in ('Sunnyvale', 'San Jose')]
    assert store_jobs(conn, rows, 'official', 'reviewed') == (1, 1)
    assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
    conn.close()
