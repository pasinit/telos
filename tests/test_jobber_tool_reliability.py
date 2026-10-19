import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock

import pymupdf
import pytest
from ddgs.exceptions import DDGSException

from telos.browser import BrowserSession
from telos.domain.tools.web import BrowserReadInput, _shape_browser_result
from telos.transport import sandbox
from telos.transport.browser import documents
from telos.transport.search import base
from telos.transport.search.duckduckgo import DuckDuckGoSearch, filter_results


def test_pdf_text_is_extracted_with_page_provenance(tmp_path):
    path = tmp_path / 'job.pdf'
    with pymupdf.open() as doc:
        doc.new_page().insert_text((50, 50), 'Minimum ten years in software engineering')
        doc.new_page().insert_text((50, 50), 'London hybrid')
        doc.save(path)
    text = documents.read_pdf(path)
    assert '[Page 1]' in text and 'ten years' in text
    assert '[Page 2]' in text and 'London hybrid' in text


def test_document_limits_and_unsupported_files_fail_explicitly(tmp_path, monkeypatch):
    path = tmp_path / 'attachment'
    path.write_bytes(b'not a PDF')
    with pytest.raises(ValueError, match='Unsupported'):
        documents.read_pdf(path)
    monkeypatch.setattr(documents, 'MAX_DOCUMENT_BYTES', 4)
    with pytest.raises(ValueError, match='size limit'):
        documents.read_pdf(path)


def test_scanned_pdf_does_not_claim_empty_requirements(tmp_path):
    path = tmp_path / 'scanned.pdf'
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(path)
    with pytest.raises(ValueError, match='OCR'):
        documents.read_pdf(path)


def test_mixed_pdf_discloses_pages_without_extractable_text(tmp_path):
    path = tmp_path / 'mixed.pdf'
    with pymupdf.open() as doc:
        doc.new_page().insert_text((50, 50), 'Job description')
        doc.new_page()
        doc.save(path)
    text = documents.read_pdf(path)
    assert '[Page 2]' in text and 'require OCR' in text and 'not verified' in text


def test_page_offsets_preserve_complete_content_and_expose_next_read():
    browser = BrowserSession()
    text = 'A' * 13000 + 'ESSENTIAL REQUIREMENTS'
    browser._snapshot = {'ok': True, 'url': 'https://example.com', 'title': 'Job', 'content': text, 'images': [], 'interactive': []}
    first = browser._content_page(0)
    offset = first['pagination']['next_offset']
    assert offset == 12000
    assert 'offset=12000' in _shape_browser_result(first).text
    second = browser._content_page(offset)
    assert second['pagination']['next_offset'] is None
    assert first['content'] + second['content'] == text
    assert not browser._content_page(len(text) + 1)['ok']
    with pytest.raises(ValueError):
        BrowserReadInput(offset=-1)


def test_site_restrictions_subdomains_paths_negations_and_ad_filter():
    urls = ['https://www.bing.com/aclick?ad=1', 'https://jobs.example.net.evil.org/jobs/1',
            'https://jobs.example.net/jobs/1', 'https://www.jobs.example.net/jobs/2',
            'https://jobs.example.net/articles/1', 'https://other.org/jobs/1', 'javascript:alert(1)']
    results = [{'href': url} for url in urls]
    assert [r['href'] for r in filter_results('site:jobs.example.net/jobs policy', results)] == urls[2:4]
    assert [r['href'] for r in filter_results('(site:jobs.example.net OR site:other.org) -site:www.jobs.example.net', results)] == [urls[2], urls[4], urls[5]]
    assert not filter_results('policy', [{'href': urls[0]}])
    assert not filter_results('policy', [None, {'href': None}, {'href': ['bad']}])


async def test_search_tries_independent_backend_after_failure(monkeypatch):
    calls = []
    def search(query, backend):
        calls.append(backend)
        if backend == 'bing':
            raise RuntimeError('backend down')
        return [{'href': 'https://example.org/job', 'title': 'Software Engineer'}]
    monkeypatch.setattr(DuckDuckGoSearch, '_search_sync', staticmethod(search))
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    engine = DuckDuckGoSearch()
    text, error = await engine.search('policy')
    assert not error and 'Software Engineer' in text and calls == ['bing', 'brave']
    calls.clear()
    await engine.search('another query')
    assert calls == ['brave']


async def test_only_off_domain_results_are_not_empty_inventory(monkeypatch):
    monkeypatch.setattr(DuckDuckGoSearch, '_search_sync', staticmethod(lambda *args: [{'href': 'https://other.org/job'}]))
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    text, error = await DuckDuckGoSearch().search('site:example.org policy')
    assert error and 'not empty inventory' in text


async def test_query_specific_no_results_does_not_disable_provider(monkeypatch):
    calls = []
    def search(query, backend):
        calls.append((query, backend))
        if query == 'first' and backend != 'yahoo':
            raise DDGSException('No results found.')
        return [{'href': 'https://example.org/job', 'title': 'Software Engineer'}]
    monkeypatch.setattr(DuckDuckGoSearch, '_search_sync', staticmethod(search))
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    engine = DuckDuckGoSearch()
    assert not (await engine.search('first'))[1]
    assert calls[-1] == ('first', 'yahoo')
    assert not (await engine.search('second'))[1]
    assert calls[-1] == ('second', 'bing')


@pytest.mark.parametrize('fails', [False, True])
async def test_bing_selection_never_silently_uses_another_engine(monkeypatch, fails):
    engine = base.get_engine('bing')
    assert isinstance(engine, DuckDuckGoSearch) and engine._backends == ('bing',)
    calls = []
    def search(query, backend):
        calls.append(backend)
        if fails:
            raise DDGSException('No results found.')
        return [{'href': 'https://example.org/job', 'title': 'Software Engineer'}]
    monkeypatch.setenv('SEARCH_ENGINE', 'bing')
    monkeypatch.setattr(DuckDuckGoSearch, '_search_sync', staticmethod(search))
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    _, error = await base.search('London policy')
    assert error is fails
    assert calls == ['bing']


async def test_bing_unexpected_error_does_not_trigger_fallback(monkeypatch):
    monkeypatch.setenv('SEARCH_ENGINE', 'bing')
    failing = AsyncMock(side_effect=RuntimeError('provider failure'))
    monkeypatch.setattr(DuckDuckGoSearch, 'search', failing)
    _, error = await base.search('London property')
    assert error and failing.await_count == 1


async def test_nested_sandbox_uses_child_user_namespace(monkeypatch, tmp_path):
    runner = sandbox.SandboxRunner(enable_nested_sandbox=True)
    runner._initialized = True
    monkeypatch.setattr(sandbox.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(sandbox.SandboxManager, 'wrap_with_sandbox', AsyncMock(return_value='bwrap safe-command'))
    proc = AsyncMock()
    proc.communicate.return_value = (b'ok', b'')
    proc.returncode = 0
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(sandbox.asyncio, 'create_subprocess_exec', spawn)
    assert await runner.run('echo ok', cwd=tmp_path) == ('ok', '', 0)
    assert spawn.call_args.args == ('unshare', '--user', '--map-root-user', '--', '/bin/sh', '-c', 'bwrap safe-command')
    assert spawn.call_args.kwargs['start_new_session'] is True


@pytest.mark.parametrize('error', [TimeoutError(), asyncio.CancelledError()])
async def test_sandbox_timeout_and_cancellation_kill_process_group(monkeypatch, error):
    runner = sandbox.SandboxRunner()
    runner._initialized = True
    monkeypatch.setattr(sandbox.SandboxManager, 'wrap_with_sandbox', AsyncMock(return_value='wrapped'))
    proc = AsyncMock(pid=1234)
    proc.communicate.side_effect = [error, (b'', b'')]
    monkeypatch.setattr(sandbox.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc))
    kill = MagicMock()
    monkeypatch.setattr(sandbox.os, 'killpg', kill)
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await runner.run('sleep 30')
    else:
        assert await runner.run('sleep 30') == ('', 'Execution timed out.', 1)
    kill.assert_called_once_with(1234, signal.SIGKILL)
    assert proc.communicate.await_count == 2


async def test_pdf_parse_timeout_reaps_child(monkeypatch, tmp_path):
    process = MagicMock(returncode=None)
    process.communicate = AsyncMock(side_effect=[TimeoutError(), (b'', b'')])
    monkeypatch.setattr(documents.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    with pytest.raises(TimeoutError):
        await documents.extract_pdf(tmp_path / 'job.pdf')
    process.kill.assert_called_once()
    assert process.communicate.await_count == 2


def make_odt(path, body, *, declaration=""):
    import zipfile

    xml = (
        declaration
        + f'<office:document-content xmlns:office="{documents.OFFICE_NS}" '
        + f'xmlns:text="{documents.TEXT_NS}" xmlns:table="{documents.TABLE_NS}">'
        + '<office:body><office:text>' + body
        + '</office:text></office:body></office:document-content>'
    )
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('mimetype', documents.ODT_MIMETYPE)
        archive.writestr('content.xml', xml)
    return path


def test_odt_text_paragraphs_cells_and_content_dispatch(tmp_path):
    path = make_odt(tmp_path / 'opaque-download', (
        '<text:h>Directory</text:h><text:p>First<text:s text:c="2"/>paragraph'
        '<text:tab/>inline<text:line-break/>line</text:p>'
        '<table:table><table:table-row><table:table-cell><text:p>Country</text:p>'
        '</table:table-cell><table:table-cell><text:p>Contact</text:p>'
        '</table:table-cell></table:table-row></table:table>'
        '<office:scripts><text:p>NEVER_INCLUDE</text:p></office:scripts>'
    ))
    text = documents.read_document(path)
    assert text.startswith('Directory\nFirst  paragraph\tinline\nline\n')
    assert 'Country\n\tContact' in text
    assert 'NEVER_INCLUDE' not in text


@pytest.mark.parametrize('limit,value,message', [
    ('MAX_DOCUMENT_BYTES', 4, 'size limit'),
    ('MAX_UNCOMPRESSED_BYTES', 64, 'decompressed size'),
    ('MAX_ARCHIVE_ENTRIES', 1, 'entry limit'),
    ('MAX_DOCUMENT_CHARS', 4, 'character limit'),
])
def test_odt_limits(tmp_path, monkeypatch, limit, value, message):
    path = make_odt(tmp_path / 'large.odt', '<text:p>' + 'x' * 10000 + '</text:p>')
    monkeypatch.setattr(documents, limit, value)
    with pytest.raises(ValueError, match=message):
        documents.read_document(path)


@pytest.mark.parametrize('body,declaration,message', [
    ('<text:p>broken', '', 'Malformed ODT XML'),
    ('<text:p>&external;</text:p>',
     '<!DOCTYPE doc [<!ENTITY external SYSTEM "file:///etc/passwd">]>', 'entities'),
    ('<text:p>&a;</text:p>', '<!DOCTYPE doc [<!ENTITY a "expanded">]>', 'entities'),
    ('<text:p><text:s text:c="999999999"/></text:p>', '', 'text limit'),
    ('<text:p/>', '', 'no extractable text'),
])
def test_odt_rejects_unsafe_or_malformed_xml(tmp_path, body, declaration, message):
    path = make_odt(tmp_path / 'bad.odt', body, declaration=declaration)
    with pytest.raises(ValueError, match=message):
        documents.read_document(path)


def test_odt_rejects_malformed_and_other_archives(tmp_path):
    import zipfile

    path = tmp_path / 'bad.odt'
    path.write_bytes(b'PK broken')
    with pytest.raises(ValueError, match='Malformed'):
        documents.read_document(path)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('mimetype', 'application/zip')
    with pytest.raises(ValueError, match='Unsupported attachment'):
        documents.read_document(path)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('mimetype', documents.ODT_MIMETYPE)
    with pytest.raises(ValueError, match='Malformed'):
        documents.read_document(path)


async def test_odt_download_identity_cleanup_and_continuation(tmp_path):
    path = make_odt(tmp_path / 'opaque', '<text:p>' + 'Directory ' * 4000 + 'TAIL</text:p>')
    download = MagicMock(url='https://example.org/directory.odt', suggested_filename='directory.odt')
    download.path = AsyncMock(return_value=path)
    download.cancel = AsyncMock()
    download.delete = AsyncMock()
    browser = BrowserSession()
    browser._page = MagicMock()
    result = await browser._read_download(download)
    text = result['content']
    assert result['pagination']['next_offset'] is not None
    while result['pagination']['next_offset'] is not None:
        result = await browser.read(result['pagination']['next_offset'])
        assert result['url'] == download.url
        assert result['title'] == download.suggested_filename
        text += result['content']
    assert text.endswith('TAIL')
    download.cancel.assert_awaited_once()
    download.delete.assert_awaited_once()


@pytest.mark.parametrize('error', [TimeoutError(), asyncio.CancelledError()])
async def test_odt_parse_deadline_reaps_child(monkeypatch, tmp_path, error):
    process = MagicMock(returncode=None)
    process.communicate = AsyncMock(side_effect=[error, (b'', b'')])
    monkeypatch.setattr(documents.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    with pytest.raises(type(error)):
        await documents.extract_document(tmp_path / 'directory.odt')
    process.kill.assert_called_once()
    assert process.communicate.await_count == 2
