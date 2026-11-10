"""Retrieve readable article text for a confirmed publication, and refuse everything else.

A response only counts as an article when it is a PDF, PMC/JATS XML, or an HTML page whose
extracted body reads like an article. Sign-in pages, bot checks ("Just a moment..."),
JavaScript application shells (an OSF project page is just the word "OSF" before scripts run),
abstract-only landing pages and empty responses are recorded as failed attempts with their
reason, so missing access stays an open access problem rather than becoming a screening result.
"""
import re
from html import unescape
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit

LOGIN = re.compile(r'(?i)(sign in|log in|login|institutional (login|access)|access through your institution|shibboleth|openathens|purchase (this )?article|buy (this )?article|subscribe to (read|access)|rent this article|get access)')
BOT = re.compile(r'(?i)(just a moment\.\.\.|checking your browser|cf-chl|challenges\.cloudflare\.com|captcha|are you a robot|verify you are (a )?human|unusual traffic|access denied|request unsuccessful|incapsula|perimeterx|bot detection|ddos protection)')
SHELL = re.compile(r'(?i)(enable javascript|requires javascript|javascript (is )?(required|disabled)|you need to enable javascript|loading\.\.\.|<noscript>)')
SECTIONS = re.compile(r'(?im)^\s*(\d+\.?\s*)?(abstract|introduction|background|methods?|materials and methods|participants|procedure|results|discussion|conclusions?|references)\s*$')


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden, self.title, self.in_title, self.meta, self.parts, self.scripts, self.forms = 0, '', False, {}, [], 0, []
        self.article_depth, self.article_parts = 0, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style', 'noscript', 'template', 'svg'):
            self.hidden += 1
            self.scripts += tag == 'script'
        if tag == 'title':
            self.in_title = True
        if tag == 'meta':
            key = (a.get('name') or a.get('property') or '').lower()
            if key:
                self.meta.setdefault(key, []).append(a.get('content') or '')
        if tag == 'input' and (a.get('type') or '').lower() == 'password':
            self.forms.append('password')
        if tag in ('article', 'main') or (tag == 'div' and re.search(r'(?i)\b(article-?body|fulltext|full-text|main-content)\b', a.get('class') or a.get('id') or '')):
            self.article_depth += 1
        if tag in ('p', 'div', 'br', 'li', 'tr', 'section') or re.fullmatch(r'h[1-6]', tag):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'template', 'svg'):
            self.hidden = max(0, self.hidden - 1)
        if tag == 'title':
            self.in_title = False
        if tag in ('article', 'main') and self.article_depth:
            self.article_depth -= 1
        if tag in ('p', 'li', 'section') or re.fullmatch(r'h[1-6]', tag):
            self.parts.append('\n')
            if self.article_depth:
                self.article_parts.append('\n')

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.hidden:
            self.parts.append(data)
            if self.article_depth:
                self.article_parts.append(data)


def clean(text):
    text = re.sub(r'[ \t\r\f\v]+', ' ', unescape(text))
    return re.sub(r'\n\s*\n+', '\n\n', text).strip()


def assess_html(raw, url=''):
    """Classify an HTML response. Returns dict(outcome, reason, text, title, pdf_url, words)."""
    html = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
    page = Page()
    try:
        page.feed(html)
    except Exception:
        pass
    body = clean(''.join(page.article_parts)) if len(''.join(page.article_parts).split()) > 300 else clean(''.join(page.parts))
    words = len(re.findall(r'\w+', body))
    title = clean(page.title)
    pdf_url = (page.meta.get('citation_pdf_url') or [None])[0]
    pdf_url = urljoin(url, pdf_url) if pdf_url else None
    head = html[:20000]
    result = {'title': title, 'words': words, 'pdf_url': pdf_url, 'text': body}
    if not html.strip() or words == 0 and not page.scripts and not title and not page.forms:
        return {**result, 'outcome': 'empty', 'reason': 'The response was empty.'}
    if BOT.search(title) or BOT.search(head) and words < 400:
        return {**result, 'outcome': 'bot_check', 'reason': f'Bot check or access-denied page ("{title or "untitled"}"), not article text.'}
    if page.forms and words < 1500 or LOGIN.search(title):
        return {**result, 'outcome': 'login_page', 'reason': f'Sign-in or purchase page ("{title or "untitled"}"), not article text.'}
    if words < 150 and (page.scripts >= 1 or SHELL.search(html)):
        return {**result, 'outcome': 'js_shell', 'reason': f'JavaScript application shell: only {words} word(s) of text ("{body[:60]}") before scripts run; not article text.'}
    sections = {m.group(2).lower() for m in SECTIONS.finditer(body)}
    has_body = len(sections & {'methods', 'method', 'materials and methods', 'results', 'discussion', 'participants', 'procedure'}) >= 2
    if words >= 1500 and has_body:
        return {**result, 'outcome': 'readable_html', 'reason': f'Article HTML with {words} words and sections: {", ".join(sorted(sections))}.'}
    if LOGIN.search(body[:5000]) and words < 3000:
        return {**result, 'outcome': 'login_page', 'reason': 'Landing page that offers sign-in or purchase; full text not available.'}
    if (page.meta.get('citation_abstract') or 'abstract' in sections) and not has_body:
        return {**result, 'outcome': 'abstract_only', 'reason': f'Landing page with abstract/metadata only ({words} words, no methods/results sections).'}
    return {**result, 'outcome': 'too_short' if words < 1500 else 'unrecognized_html',
            'reason': f'{words} words without article sections; not treated as article text.'}


def classify_response(data, headers, url):
    """Classify any downloaded response: PDF, XML, HTML or other."""
    kind = next((str(v) for k, v in (headers or {}).items() if str(k).lower() == 'content-type'), '')
    if data[:1024].lstrip().startswith(b'%PDF-') or b'%PDF-' in data[:1024]:
        return {'outcome': 'pdf', 'reason': 'PDF document.', 'content_type': kind}
    if not data.strip():
        return {'outcome': 'empty', 'reason': 'The response was empty.', 'content_type': kind}
    if re.search(rb'<(article|pmc-articleset)[\s>]', data[:4000]) and b'<html' not in data[:2000].lower():
        return {'outcome': 'xml', 'reason': 'JATS/PMC article XML.', 'content_type': kind}
    if 'html' in kind.lower() or re.search(rb'(?i)<(!doctype html|html|head|body)[\s>]', data[:2000]):
        return {**assess_html(data, url), 'content_type': kind}
    return {'outcome': 'unrecognized', 'reason': f'Unrecognized content ({kind or "no content type"}).', 'content_type': kind}


def fetch_landing(fetcher, url, limit=15_000_000):
    """Fetch a landing/article URL and classify it. Never raises for HTTP errors; records them."""
    try:
        data, final, headers = fetcher.get(url, limit=limit, headers={'Accept': 'text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5'})
    except HTTPError as exc:
        body = b''
        try:
            body = exc.read(200_000) or b''
        except Exception:
            pass
        outcome = classify_response(body, dict(exc.headers or {}), url) if body else {'outcome': 'http_error'}
        reason = f'HTTP {exc.code}' + (': ' + outcome['reason'] if outcome.get('reason') else '')
        return {'outcome': outcome['outcome'] if outcome['outcome'] in ('bot_check', 'login_page') else 'http_error', 'reason': reason,
                'url': url, 'final': url, 'status': exc.code, 'data': None}
    except Exception as exc:
        return {'outcome': 'network_error', 'reason': f'{type(exc).__name__}: {exc}'[:400], 'url': url, 'final': url, 'status': None, 'data': None}
    result = classify_response(data, headers, final)
    return {**result, 'url': url, 'final': final, 'status': 200, 'data': data, 'bytes': len(data),
            'host': urlsplit(final).hostname}
