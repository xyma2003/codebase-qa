"""Render a restricted Markdown AST and redact secrets at application boundaries."""

import logging
import re
import traceback

from markdown_it import MarkdownIt
from security.sources import secret_spans

_PRIVATE_BLOCK = re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|$)")
_SOURCE_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/blob/[0-9a-f]{40}/[^\s?#]+#L[1-9][0-9]*-L[1-9][0-9]*\Z")


def valid_source_url(url):
    return isinstance(url, str) and _SOURCE_URL.fullmatch(url) is not None and not secret_spans(url)


def redact_text(value, *, extra_secrets=()):
    text = str(value)
    spans = secret_spans(text)
    spans.extend((match.start(), match.end()) for match in _PRIVATE_BLOCK.finditer(text))
    for secret in extra_secrets:
        if secret:
            start = 0
            while (start := text.find(secret, start)) >= 0:
                spans.append((start, start + len(secret)))
                start += len(secret)
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        text = text[:start] + "[REDACTED]" + text[end:]
    return text


def redact_data(value, *, extra_secrets=()):
    if isinstance(value, str):
        return redact_text(value, extra_secrets=extra_secrets)
    if isinstance(value, dict):
        return {redact_text(key, extra_secrets=extra_secrets): redact_data(item, extra_secrets=extra_secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_data(item, extra_secrets=extra_secrets) for item in value]
    return value


def safe_error(error):
    return redact_text(f"{type(error).__name__}: {error}")[:2048]


def safe_answer_html(text, *, source_urls=()):
    """Only application-checked citation links survive; no HTML, images or linkify."""
    parser = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable(["table", "strikethrough"])
    tokens = parser.parse(redact_text(text))
    allowed = {url for url in source_urls if valid_source_url(url)}
    for block in tokens:
        links = []
        for token in block.children or []:
            if token.type == "image":
                token.type, token.tag, token.children = "text", "", None
                token.attrs = {}
            elif token.type == "link_open":
                url = token.attrGet("href")
                approved = url in allowed
                links.append(approved)
                token.attrs = {"href": url, "target": "_blank", "rel": "noopener noreferrer"} if approved else {}
                if not approved:
                    token.tag = "span"
            elif token.type == "link_close" and links:
                if not links.pop():
                    token.tag = "span"
    return parser.renderer.render(tokens, parser.options, {})


def install_log_redaction():
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_codebase_qa_redactor", False):
        return
    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.msg, record.args = redact_text(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = redact_text("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return record
    factory._codebase_qa_redactor = True
    logging.setLogRecordFactory(factory)
