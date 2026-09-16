import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from applypilot.apply import archer


def schema():
    return [{'id': key, 'label': label+' *' if required else label, 'required': required,
             'type': 'text', 'role': None} for key, (label, _, required) in archer.FIELDS.items()]


def test_reviewed_schema_is_exhaustive():
    assert len(archer.validate_contract(schema())) == 64


@pytest.mark.parametrize('change', ['extra', 'missing', 'label', 'required', 'duplicate'])
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
def test_hydrated_preview_blocks_writes_and_keeps_unknown_answers(tmp_path, monkeypatch):
    requests = []
    fields = ''.join(f'<label for="{key}">{html.escape(label)}</label>'
                     f'<input id="{key}" aria-required="{str(required).lower()}" '
                     f'type="{"file" if key in {"resume","cover_letter"} else "text"}">'
                     for key, (label, _, required) in archer.FIELDS.items())
    content = (f'<h1>{archer.TITLE}</h1><p>Archer</p><form method="post">{fields}</form>'
               '<script>document.addEventListener("input",()=>{fetch("/leak",{method:"POST",body:"test"});'
               'document.querySelector("form").submit()})</script>')
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
    try:
        job = {'url': url, 'application_url': url, 'company': archer.COMPANY, 'title': archer.TITLE}
        result = archer.preview(job, profile={'personal': {'email': 'candidate@example.test'}}, output_dir=tmp_path)
        assert result['status'] == 'needs_input', result
        assert result['submitted'] is False
        assert result['filled'] == [{'field': 'email', 'profile_path': 'personal.email'}]
        assert 'first_name' in {f['field'] for f in result['missing_fields']}
        assert requests == [('GET', '/application')]
        assert any(r['method'] == 'POST' for r in result['blocked_requests'])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
