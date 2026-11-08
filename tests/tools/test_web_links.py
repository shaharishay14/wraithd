"""
read_website returns the links in the part it shows, so a skill can link to the real article or job posting: absolute
http(s) URLs, once each, without the site's menus; an RSS or Atom feed gives each item's title and link. The links
are tool output like the text, so the gate still knows they came from read_website, not from the owner.
"""

import json
import socket

import httpx
import pytest

from tests.scripted import fake_tool
from wraithd.core import render
from wraithd.core.gate import PermissionGate, guard_function_tools
from wraithd.tools import web

NEWS = b'''<!doctype html><html><head><title>Harbor Daily</title></head><body>
<header><a href="/">Harbor Daily</a><nav><a href="/world">World</a> <a href="/sport">Sport</a>
<a href="/login">Sign in</a></nav></header>
<main>
<h1>Top stories</h1>
<article><h2><a href="https://harbor.example/2026/11/08/ferry-strike">Ferry strike ends after 3 days</a></h2>
<p>Crews return on Monday.</p></article>
<article><h2><a href="/2026/11/08/new-bridge?ref=home#comments">Council approves the new bridge</a></h2>
<p>Work starts in spring.</p></article>
<article><h2><a href="https://other.example/story/42">Port traffic up 12% this year</a></h2>
<p>From our partners.</p><a href="https://other.example/story/42">Read more</a></article>
<p><a href="javascript:void(0)">Load more</a> <a href="mailto:tips@harbor.example">Send a tip</a>
<a href="#top">Back to top</a> <a href="/"><img src="logo.png"></a> <a href="https://harbor.example/x"> </a></p>
</main>
<footer><a href="/about">About</a> <a href="/privacy">Privacy</a></footer>
</body></html>'''

JOBS = b'''<html><head><title>Careers</title><base href="https://jobs.example/board/"></head><body>
<ul>
<li><a href="postings/101">Backend Engineer (Python)</a> Remote</li>
<li><a href="../postings/102">Data Analyst</a> Lisbon</li>
<li><a href="/postings/103" aria-label="Product Designer"><svg><text>icon</text></svg></a></li>
<li><a href="//cdn.jobs.example/postings/104.pdf">Office Manager (PDF)</a></li>
</ul></body></html>'''

RSS = b'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>"harbor" - Google News</title><link>https://news.google.com/</link>
<item><title>Ferry strike ends after 3 days - Harbor Daily</title>
<link>https://news.google.com/rss/articles/CBMiAAA?oc=5</link>
<pubDate>Sun, 08 Nov 2026 08:00:00 GMT</pubDate>
<description>&lt;a href="https://news.google.com/rss/articles/CBMiAAA?oc=5"&gt;Ferry strike&lt;/a&gt;</description>
<source url="https://harbor.example">Harbor Daily</source></item>
<item><title><![CDATA[Council approves the new bridge & tunnel]]></title>
<link>https://news.google.com/rss/articles/CBMiBBB?oc=5</link>
<pubDate>Sat, 07 Nov 2026 18:30:00 GMT</pubDate></item>
</channel></rss>'''

ATOM = b'''<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Dev blog</title>
<link href="https://blog.example/"/><entry><title>Release 2.0</title>
<link rel="replies" href="https://blog.example/2.0#comments"/><link href="https://blog.example/2.0"/>
<updated>2026-11-01T10:00:00Z</updated></entry></feed>'''


@pytest.fixture
def dns(monkeypatch):
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]

    monkeypatch.setattr(socket, 'getaddrinfo', getaddrinfo)


def serving(body: bytes, content_type='text/html; charset=utf-8'):
    return httpx.MockTransport(lambda request: httpx.Response(200, headers={'content-type': content_type},
                                                              content=body))


async def read(transport, url, offset=0):
    tool = web.read_website_tool(transport)
    return json.loads(await tool.on_invoke_tool(None, json.dumps({'url': url, 'offset': offset})))


async def test_a_page_gives_its_article_links_without_the_menus(dns):
    page = await read(serving(NEWS), 'https://harbor.example/')
    assert page['links'] == [
        {'text': 'Ferry strike ends after 3 days', 'url': 'https://harbor.example/2026/11/08/ferry-strike'},
        {'text': 'Council approves the new bridge', 'url': 'https://harbor.example/2026/11/08/new-bridge?ref=home'},
        {'text': 'Port traffic up 12% this year', 'url': 'https://other.example/story/42'},
    ]  # no nav/header/footer, no "Read more" twin, no javascript:, mailto:, #top, logo, or empty-text link
    assert 'Ferry strike ends after 3 days' in page['content'] and 'Crews return on Monday.' in page['content']


async def test_relative_links_are_resolved_against_the_page_and_its_base(dns):
    page = await read(serving(JOBS), 'https://jobs.example/careers')
    assert page['links'] == [
        {'text': 'Backend Engineer (Python)', 'url': 'https://jobs.example/board/postings/101'},
        {'text': 'Data Analyst', 'url': 'https://jobs.example/postings/102'},
        {'text': 'Product Designer', 'url': 'https://jobs.example/postings/103'},  # an icon link's aria-label
        {'text': 'Office Manager (PDF)', 'url': 'https://cdn.jobs.example/postings/104.pdf'},
    ]
    nested = await read(serving(JOBS.replace(b'<base href="https://jobs.example/board/">', b'')),
                        'https://jobs.example/careers/all')
    assert nested['links'][0]['url'] == 'https://jobs.example/careers/postings/101'


@pytest.mark.parametrize('content_type', ['application/rss+xml; charset=UTF-8', 'text/xml', 'text/html'])
async def test_rss_items_come_with_their_links(dns, content_type):
    feed = await read(serving(RSS, content_type), 'https://news.google.com/rss/search?q=harbor')
    assert feed['title'] == '"harbor" - Google News'
    assert feed['content'] == ('Ferry strike ends after 3 days - Harbor Daily (Sun, 08 Nov 2026 08:00:00 GMT)\n'
                               'Council approves the new bridge & tunnel (Sat, 07 Nov 2026 18:30:00 GMT)')
    assert feed['links'] == [
        {'text': 'Ferry strike ends after 3 days - Harbor Daily',
         'url': 'https://news.google.com/rss/articles/CBMiAAA?oc=5'},
        {'text': 'Council approves the new bridge & tunnel', 'url': 'https://news.google.com/rss/articles/CBMiBBB?oc=5'},
    ]


async def test_atom_entries_link_to_their_page(dns):
    feed = await read(serving(ATOM, 'application/atom+xml'), 'https://blog.example/feed')
    assert feed['content'] == 'Release 2.0 (2026-11-01T10:00:00Z)'
    assert feed['links'] == [{'text': 'Release 2.0', 'url': 'https://blog.example/2.0'}]


async def test_each_part_has_the_links_in_its_text_and_fits_the_result_cap(dns):
    rows = ''.join(f'<li><a href="/jobs/{i}">Job number {i:03d}, a fairly long posting title here</a> '
                   f'<p>{"Details of the role. " * 6}</p></li>' for i in range(150))
    body = f'<html><body><ul>{rows}</ul></body></html>'.encode()
    url, offset, found, text = 'https://jobs.example/list', 0, [], ''
    while True:
        tool = web.read_website_tool(serving(body))
        raw = await tool.on_invoke_tool(None, json.dumps({'url': url, 'offset': offset}))
        assert len(raw) <= render.MAX_TOOL_RESULT_CHARS  # never cut in the middle of the JSON
        part = json.loads(raw)
        assert len(part['links']) <= web.MAX_LINKS
        assert all(link['text'].split(',')[0] in part['content'] for link in part['links'])
        found += [link['url'] for link in part['links']]
        text += part['content']
        if not part['truncated']:
            break
        offset = part['next_offset']
    assert found == [f'https://jobs.example/jobs/{i}' for i in range(150)]  # every posting, once, in order
    assert text == web.read_page(body, 'text/html', url)[1]


def test_links_stay_put_through_whitespace_and_blank_lines():
    title, text, links = web.read_page(b'<p>  intro  </p><div>\n\n <a href="/a">\n  First </a></div><p>x <a href=b>'
                                       b'Second</a></p>', 'text/html', 'https://s.example/dir/page')
    assert text == 'intro\nFirst\nx Second'
    assert [(text[link.position:link.position + len(link.text)], link.url) for link in links] == [
        ('First', 'https://s.example/a'), ('Second', 'https://s.example/dir/b')]


async def test_a_link_from_read_websites_links_still_came_from_read_website(store, dns):
    """After a private read, only links the owner wrote open freely: a link from the page's links isn't one."""
    page = await read(serving(NEWS), 'https://harbor.example/')
    gate = PermissionGate(store, None, user_message='check my inbox, then the news on https://harbor.example')
    [fetch] = guard_function_tools([fake_tool('read_website', lambda **a: page)], gate)
    await fetch.on_invoke_tool(None, '{}')
    gate.record_output('GMAIL_FETCH_EMAILS', json.dumps({'body': 'lunch at noon'}))

    article = page['links'][0]['url']
    assert article not in page['content']  # the URL is only in links, not in the text
    assert gate.url_sources[article] == 'read_website'
    error = json.loads(await gate.check('read_website', {'url': article}))['error']
    assert 'came from read_website' in error
    assert await gate.check('read_website', {'url': 'https://harbor.example'}) is None  # the owner wrote this one


def test_tokens_a_typical_page_adds():
    """The links' cost per call, in characters (about 4 per token): bounded by the result cap either way."""
    for body, url in ((NEWS, 'https://harbor.example/'), (JOBS, 'https://jobs.example/careers'),
                      (RSS, 'https://news.google.com/rss/search?q=harbor')):
        part = web.page_part(url, *web.read_page(body, 'text/html', url), 0)
        assert len(json.dumps(part['links'], ensure_ascii=False)) / 4 < 150
