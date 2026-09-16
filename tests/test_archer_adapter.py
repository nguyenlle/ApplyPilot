import hashlib
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from applypilot import database
from applypilot.apply import archer
from applypilot.artifacts import write_manifest


def schema():
    return [{'id': key, 'label': label+' *' if required else label, 'required': required,
             'type': 'file' if key in {'resume', 'cover_letter'} else 'text',
             'role': None} for key, (label, _, required) in archer.FIELDS.items()]


def test_reviewed_schema_is_exhaustive():
    assert len(archer.validate_contract(schema())) == 64


@pytest.mark.parametrize('change', ['extra', 'missing', 'label', 'required', 'duplicate', 'file_type'])
def test_unreviewed_schema_change_stops_preview(change):
    fields = schema()
    if change == 'extra':
        fields.append({'id': 'citizenship', 'label': 'Citizenship', 'required': True})
    elif change == 'missing':
        fields.pop()
    elif change == 'label':
        fields[0]['label'] = 'Legal consent'
    elif change == 'required':
        fields[0]['required'] = False
    elif change == 'file_type':
        next(item for item in fields if item['id'] == 'resume')['type'] = 'text'
    else:
        fields.append(fields[0])
    with pytest.raises(ValueError, match='changed'):
        archer.validate_contract(fields)


@pytest.mark.parametrize('value', [None, '', 'unknown', {}, []])
def test_unknown_answers_remain_unknown(value):
    assert archer.profile_value({'personal': {'last_name': value}}, 'personal.last_name') is None


def test_wrong_queued_employer_stops_before_browser():
    with pytest.raises(ValueError, match='Queued job'):
        archer.preview({'application_url': archer.APPLICATION_URL, 'company': 'Other', 'title': archer.TITLE})


def add_documents(job, directory):
    for kind, key in [('resume', 'tailored_resume_path'), ('cover_letter', 'cover_letter_path')]:
        path = directory / f'{kind}.txt'
        path.write_text(f'Approved synthetic {kind}', encoding='utf-8')
        path.with_suffix('.pdf').write_bytes(f'%PDF-1.4 synthetic {kind}'.encode())
        write_manifest(job, path, kind=kind, approved=True)
        job[key] = str(path)


def test_document_bytes_are_frozen_after_validation(tmp_path):
    job = {'url': archer.APPLICATION_URL, 'company': archer.COMPANY, 'title': archer.TITLE}
    add_documents(job, tmp_path)
    payloads = archer.verified_file_payloads(job)
    (tmp_path / 'resume.pdf').write_bytes(b'%PDF-1.4 later change')
    assert payloads['resume']['payload']['buffer'] == b'%PDF-1.4 synthetic resume'
    assert payloads['resume']['sha256'] == hashlib.sha256(payloads['resume']['payload']['buffer']).hexdigest()


def test_document_mutation_between_validation_and_read_is_rejected(tmp_path, monkeypatch):
    job = {'url': archer.APPLICATION_URL, 'company': archer.COMPANY, 'title': archer.TITLE}
    add_documents(job, tmp_path)
    verify = archer.verify_job_artifacts

    def mutate(job):
        files = verify(job)
        files['cover_letter'].write_bytes(b'%PDF-1.4 changed after verification')
        return files

    monkeypatch.setattr(archer, 'verify_job_artifacts', mutate)
    with pytest.raises(ValueError, match='changed before local file selection'):
        archer.verified_file_payloads(job)


@pytest.mark.parametrize('change', [None, 'city', 'region', 'host', 'options', 'ambiguous', 'status'])
def test_location_cache_is_bound_to_explicit_city_region_country(tmp_path, change):
    profile = {'personal': {'city': 'Example City', 'province_state': 'CA', 'country': 'United States'}}
    feature = {'properties': {'name': 'Example City', 'region': 'California', 'region_a': 'CA',
                              'country_code': 'US', 'country': 'United States'}}
    data = {'request': {'method': 'GET', 'url': 'https://api-geocode-earth-proxy.greenhouse.io/v1/autocomplete?text=Example+City&layers=locality'},
            'status': 200, 'response': {'type': 'FeatureCollection', 'features': [feature]},
            'option_labels': ['Example City, California, United States']}
    if change == 'city':
        profile['personal']['city'] = 'Another City'
    elif change == 'region':
        profile['personal']['province_state'] = 'NY'
    elif change == 'host':
        data['request']['url'] = data['request']['url'].replace('api-geocode-earth-proxy.greenhouse.io', 'other.example.test')
    elif change == 'options':
        data['option_labels'] = []
    elif change == 'ambiguous':
        data['response']['features'].append(feature)
    elif change == 'status':
        data['status'] = 500
    path = tmp_path/'cache.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    result = archer.cached_location(path, profile)
    if change is None:
        assert result[1] == data['option_labels'][0]
    else:
        assert result is None


@pytest.mark.browser
@pytest.mark.parametrize('documents', [False, True])
def test_hydrated_preview_blocks_writes_and_keeps_unknown_answers(tmp_path, monkeypatch, documents):
    requests = []
    fields = ''.join(f'<label for="{key}">{html.escape(label)}</label>'
                     f'<input id="{key}" aria-required="{str(required).lower()}" '
                     f'type="{"file" if key in {"resume","cover_letter"} else "text"}">'
                     for key, (label, _, required) in archer.FIELDS.items())
    content = (f'<h1>{archer.TITLE}</h1><p>Archer</p><form method="post">{fields}</form>'
               '<script>document.addEventListener("input",()=>{fetch("/leak",{method:"POST",body:"test"});'
               'document.querySelector("form").submit()});'
               'document.addEventListener("change",async e=>{if(e.target.type!=="file")return;'
               'const file=e.target.files[0],id=e.target.id;e.target.value="";'
               'const bytes=await file.arrayBuffer();const hash=await crypto.subtle.digest("SHA-256",bytes);'
               'document.body.dataset[id+"Sha"]=Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,"0")).join("");'
               'fetch("/upload/"+id,{method:"POST",body:bytes}).catch(()=>{});'
               'setTimeout(()=>{fetch("/async/"+id).catch(()=>{});navigator.sendBeacon("/beacon/"+id,bytes)},10);'
               '})</script>')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(('GET', self.path))
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(content.encode())
        def do_POST(self):
            requests.append(('POST', self.path))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/application'
    monkeypatch.setattr(archer, 'APPLICATION_URL', url)
    db_path = tmp_path / 'state.db'
    monkeypatch.setattr(database, 'DB_PATH', db_path)
    monkeypatch.setattr(archer.config, 'DB_PATH', db_path)
    connection = database.init_db(db_path)
    connection.execute("INSERT INTO jobs(url, apply_status, tailored_resume_path, tailored_at, tailor_attempts, "
                       "cover_letter_path, cover_letter_at, cover_attempts, apply_attempts) "
                       "VALUES (?, 'needs_input', 'resume.txt', '2026-09-16', 1, 'cover.txt', '2026-09-16', 1, 2)", (url,))
    connection.execute("INSERT INTO application_attempts(attempt_id,job_url,started_at,status) VALUES ('prior',?,'2026-09-16','needs_input')", (url,))
    connection.commit()
    before = '\n'.join(connection.iterdump())
    try:
        job = {'url': url, 'application_url': url, 'company': archer.COMPANY, 'title': archer.TITLE}
        if documents:
            add_documents(job, tmp_path)
        for index in range(2):
            output = tmp_path / f'preview-{index}'
            result = archer.preview(job, profile={'personal': {'email': 'candidate@example.test'}}, output_dir=output)
            assert result['status'] == 'needs_input', result
            assert result['submitted'] is False
            assert result['network_locked_before_candidate_input'] is True
            assert result['filled'] == [{'field': 'email', 'profile_path': 'personal.email'}]
            assert 'first_name' in {f['field'] for f in result['missing_fields']}
            assert any(r['method'] == 'POST' for r in result['blocked_requests'])
            assert all(r['network_locked'] for r in result['blocked_requests'] if r['method'] == 'POST')
            if documents:
                selected = result['local_file_selection']
                assert {item['field_id'] for item in selected} == {'resume', 'cover_letter'}
                page_html = (output / 'form.html').read_text(encoding='utf-8')
                for item in selected:
                    expected = hashlib.sha256((tmp_path / f"{item['kind']}.pdf").read_bytes()).hexdigest()
                    assert item['sha256'] == expected and expected in page_html
                    assert item['set_input_files_completed'] is True
                    assert item['observed_files'] == []  # The site clears inputs after attempted async uploads.
                    assert item['external_upload_verified'] is False
                    assert any(r['path'] == f"/upload/{item['kind']}" for r in result['blocked_requests'])
                    assert any(r['path'] == f"/async/{item['kind']}" for r in result['blocked_requests'])
            assert (output / 'form.png').is_file() and (output / 'evidence.json').is_file()
            assert '\n'.join(connection.iterdump()) == before
        assert requests == [('GET', '/application')] * 2
    finally:
        database.close_connection(db_path)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
