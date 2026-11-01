import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from ddgs.exceptions import RatelimitException, TimeoutException

from telos.domain.tools.zoopla import ZooplaSearchInput, ZooplaSearchTool
from telos.transport.search import base, duckduckgo, gemini
from telos.transport.search.duckduckgo import DuckDuckGoSearch
from telos.transport.search.gemini import GeminiSearch
from telos.transport.search.serpapi import SerpApiSearch
from telos.transport.search.serper import SerperSearch


async def test_ddg_failure_is_not_an_empty_search(monkeypatch):
    def fail(query, backend):
        raise RuntimeError('provider outage')
    monkeypatch.setattr(DuckDuckGoSearch, '_search_sync', staticmethod(fail))
    text, error = await DuckDuckGoSearch().search('property')
    assert error and 'unavailable' in text


async def test_ddg_runs_off_loop_and_genuine_empty_remains_success(monkeypatch):
    loop_thread = threading.get_ident()
    def search(query, backend):
        assert threading.get_ident() != loop_thread
        return []
    monkeypatch.setattr(DuckDuckGoSearch, '_search_sync', staticmethod(search))
    text, error = await DuckDuckGoSearch().search('property')
    assert not error and 'No usable results' in text


async def test_ddg_retries_transient_error_once_before_brief_bench(monkeypatch):
    # A single slow/flaky response must not remove a backend for a full minute.
    calls = []
    def flaky(query, backend):
        calls.append(backend)
        raise TimeoutException('slow')
    monkeypatch.setattr(DuckDuckGoSearch, '_search_sync', staticmethod(flaky))
    search = DuckDuckGoSearch(backends=('bing',))
    text, error = await search.search('property')
    assert error and 'unavailable' in text
    assert calls == ['bing', 'bing']  # retried once before benching
    cooldown = search._cooldowns['bing'] - time.monotonic()
    assert 0 < cooldown <= duckduckgo._TRANSIENT_COOLDOWN * 2  # brief, not the 60s bench


async def test_ddg_rate_limit_benches_for_a_full_minute_without_retry(monkeypatch):
    calls = []
    def ratelimited(query, backend):
        calls.append(backend)
        raise RatelimitException('429 Too Many Requests')
    monkeypatch.setattr(DuckDuckGoSearch, '_search_sync', staticmethod(ratelimited))
    search = DuckDuckGoSearch(backends=('bing',))
    text, error = await search.search('property')
    assert error
    assert calls == ['bing']  # an explicit rate-limit is not retried
    cooldown = search._cooldowns['bing'] - time.monotonic()
    assert duckduckgo._RATELIMIT_COOLDOWN - 2 < cooldown <= duckduckgo._RATELIMIT_COOLDOWN


async def test_ddg_retry_recovers_a_transient_failure(monkeypatch):
    outcomes = iter([TimeoutException('slow'),
                     [{'title': 'A', 'href': 'https://example.com/a', 'body': 'b'}]])
    def flaky(query, backend):
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(DuckDuckGoSearch, '_search_sync', staticmethod(flaky))
    search = DuckDuckGoSearch(backends=('bing',))
    text, error = await search.search('example')
    assert not error and 'example.com/a' in text
    assert 'bing' not in search._cooldowns  # recovered on retry, never benched


async def test_ddg_network_timeout_is_explicit(monkeypatch):
    def factory(**kwargs):
        assert kwargs == {'timeout': 8}
        return SimpleNamespace(text=lambda query, **options: [])
    monkeypatch.setattr(duckduckgo, 'DDGS', factory)
    assert not (await DuckDuckGoSearch().search('property'))[1]


async def test_gemini_uses_worker_timeout_and_closes_client(monkeypatch):
    monkeypatch.setenv('GOOGLE_API_KEY', 'test')
    main_thread = threading.get_ident()
    client = MagicMock()
    client.__enter__.return_value = client
    def generate(**kwargs):
        assert threading.get_ident() != main_thread
        return SimpleNamespace(text='result', candidates=[SimpleNamespace(grounding_metadata=None)])
    client.models.generate_content.side_effect = generate
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(gemini.genai, 'Client', factory)
    assert await GeminiSearch().search('property') == ('result', False)
    assert factory.call_args.kwargs['http_options'].timeout == 30000
    client.__exit__.assert_called_once()


async def test_error_flag_triggers_fallback_and_fallback_errors_remain_errors(monkeypatch):
    monkeypatch.setenv('SEARCH_ENGINE', 'serpapi')
    monkeypatch.setattr(base, 'get_engine', lambda name: SimpleNamespace(search=AsyncMock(return_value=('failed', True))))
    monkeypatch.setattr(DuckDuckGoSearch, 'search', AsyncMock(return_value=('fallback failed', True)))
    assert await base.search('property') == ('fallback failed', True)


async def test_search_errors_do_not_leak_provider_keys(monkeypatch, caplog):
    secret = 'https://provider.example/search?api_key=secret-value'
    monkeypatch.setenv('SEARCH_ENGINE', 'serpapi')
    monkeypatch.setattr(base, 'get_engine', lambda name: SimpleNamespace(search=AsyncMock(side_effect=RuntimeError(secret))))
    monkeypatch.setattr(DuckDuckGoSearch, 'search', AsyncMock(side_effect=RuntimeError(secret)))
    text, error = await base.search('policy')
    assert error and 'secret-value' not in text and 'secret-value' not in caplog.text


@pytest.mark.parametrize('provider,key,method', [(SerpApiSearch, 'SERPAPI_KEY', 'get'), (SerperSearch, 'SERPER_KEY', 'post')])
@pytest.mark.parametrize('body', [{'error': 'quota exhausted'}, {}, []])
async def test_http_200_error_or_unknown_shape_is_not_empty(provider, key, method, body, monkeypatch):
    monkeypatch.setenv(key, 'test')
    response = httpx.Response(200, json=body, request=httpx.Request('GET', 'https://example.com'))
    monkeypatch.setattr(httpx.AsyncClient, method, AsyncMock(return_value=response))
    with pytest.raises(RuntimeError):
        await provider().search('property')


@pytest.mark.parametrize('response', [{'text': 'blocked', 'status_code': 403}, {'text': 'navigation'},
                                    {'text': '[search_results] fake page text'}])
async def test_zoopla_unusable_response_is_error(response):
    result = await ZooplaSearchTool(AsyncMock(return_value=response))(ZooplaSearchInput(location='W2'), None)
    assert result.is_error


async def test_zoopla_tries_fallback_after_fetch_exception():
    fetch = AsyncMock(side_effect=[RuntimeError('blocked'), {'text': 'listings', 'structured': {'kind': 'search_results'}}])
    result = await ZooplaSearchTool(fetch)(ZooplaSearchInput(location='Bayswater'), None)
    assert not result.is_error and result.text == 'listings'
