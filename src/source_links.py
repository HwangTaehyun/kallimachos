import re
from urllib.parse import urlsplit

import yaml

from frontmatter import FM_RE, FM_OPEN_RE


MAX_URL_CHARS = 2048
MAX_FRONTMATTER_CHARS = 64000
MAX_YAML_DEPTH = 32
MAX_YAML_NODES = 4096


def external_url(value):
    if not isinstance(value, str) or not value or len(value) > MAX_URL_CHARS:
        return ""
    if re.search(r"[\s\x00-\x1f\x7f-\x9f<>\"'`\\]", value):
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return ""
        if parsed.port is not None and not 0 < parsed.port <= 65535:
            return ""
    except ValueError:
        return ""
    return value


def load_frontmatter(raw):
    match = FM_RE.match(raw[:MAX_FRONTMATTER_CHARS])
    if not match:
        if FM_OPEN_RE.match(raw):
            raise ValueError("malformed or oversized frontmatter")
        return {}
    try:
        depth = nodes = 0
        events = yaml.parse(match.group(1), Loader=yaml.SafeLoader)
        try:
            for event in events:
                if isinstance(event, yaml.events.AliasEvent):
                    raise ValueError("YAML alias")
                if isinstance(event, yaml.events.ScalarEvent):
                    if (event.tag == "tag:yaml.org,2002:merge"
                            or (event.value == "<<" and event.style is None)):
                        raise ValueError("YAML merge")
                    nodes += 1
                elif isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                    depth += 1
                    nodes += 1
                elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                    depth -= 1
                if depth > MAX_YAML_DEPTH or nodes > MAX_YAML_NODES:
                    raise ValueError("YAML budget exceeded")
        finally:
            events.close()
        data = yaml.safe_load(match.group(1))
    except (yaml.YAMLError, RecursionError, ValueError):
        raise ValueError("malformed, unsupported, or over-budget frontmatter") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("frontmatter must be a mapping")
    return data


def _provenance(raw):
    try:
        return load_frontmatter(raw)
    except ValueError:
        return {}


def source_resources(raw):
    sources = _provenance(raw).get("sources")
    if not isinstance(sources, list):
        return []
    return [entry["resource"] for entry in sources[:256]
            if isinstance(entry, dict) and isinstance(entry.get("resource"), str)]


def source_urls(raw):
    data = _provenance(raw)
    values = [data.get(key) for key in ("original_url", "source_url", "source", "resource")]
    sources = data.get("sources")
    if isinstance(sources, list):
        values += [entry.get("resource") if isinstance(entry, dict) else entry
                   for entry in sources[:256]]
    urls = []
    for value in values:
        url = external_url(value)
        if url and url not in urls:
            urls.append(url)
    return urls
