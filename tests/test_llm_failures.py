import httpx
import pytest

from applypilot import llm


def client_for(handler):
    client = llm.LLMClient('https://api.openai.com/v1', 'gpt-4o-mini', 'synthetic-key')
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.parametrize('error', [
    {'type': 'insufficient_quota'}, {'code': 'credit_balance_exhausted'},
    {'code': 'billing_hard_limit_reached'},
])
def test_exhausted_credit_stops_without_sleep_or_repeated_requests(monkeypatch, error):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(429, json={'error': {**error, 'message': 'private billing detail'}})
    monkeypatch.setattr(llm.time, 'sleep', lambda _: pytest.fail('Must not sleep on exhausted credit'))
    client = client_for(handle)
    try:
        with pytest.raises(llm.ProviderQuotaError) as failure:
            client.ask('test')
        assert len(calls) == 1
        assert 'private billing detail' not in str(failure.value)
    finally:
        client.close()


@pytest.mark.parametrize('retry_after,expected', [('9999', 60), ('-1', 10), ('NaN', 10), ('2', 2)])
def test_transient_rate_limit_retries_with_bounded_delay(monkeypatch, retry_after, expected):
    calls, sleeps = [], []
    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={'Retry-After': retry_after}, json={'error': {'type': 'rate_limit_exceeded'}})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
    monkeypatch.setattr(llm.time, 'sleep', sleeps.append)
    client = client_for(handle)
    try:
        assert client.ask('test') == 'ok'
        assert len(calls) == 2
        assert sleeps == [expected]
    finally:
        client.close()
