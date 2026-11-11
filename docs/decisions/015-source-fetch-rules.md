# 015: One small fetcher for every source

Date: 2026-11-11

## Context

Three adapters fetch from Hydrocarbon Processing, GDELT and EIA. All three must respect
robots.txt and rate limits, send a plain User-Agent with a contact email, back off on 429 and
5xx, and never evade a block. They run on the 512 MB ingest Droplet. GDELT leads point at any
news site, so a fetched URL is not always one we chose.

## Options

- A crawler framework such as Scrapy. Does all of this, but is a large dependency for three sources.
- The standard library's `urllib.request`. No dependency, but weak timeouts, and tests need a fake server.
- `requests`. Common, but needs a second package to mock it in tests.
- `httpx`. Sets connect and read timeouts separately, and has a mock transport built in.

## Decision

One fetcher on `httpx`, used by all three adapters. It sends
`refinery-watch/0.1 (+mailto:<CONTACT_EMAIL>)`, with the email taken from config. Timeouts are
20 seconds. On 429 or 5xx it retries up to 3 times. It waits for Retry-After when given, up to
5 minutes. Otherwise it waits 30, 60, then 120 seconds. A 401, 403 or 451, or a host that still
fails after the retries, gets no more requests in that run. robots.txt is read with
`urllib.robotparser` through the same client and kept per host for one run. A robots.txt that
returns 401, 403 or 5xx counts as a disallow. Requests to one host are at least 5 seconds apart,
or longer if robots.txt sets a crawl delay. `source.fetch_interval_min` is the shortest gap between
two runs of one source. It is not a gap between requests. The fetcher only connects to public
addresses on ports 80 and 443, and it checks this on every redirect.

Feed XML is parsed with `defusedxml`. HTML becomes text with the standard library's
`html.parser`. We do not use feedparser, BeautifulSoup, lxml or trafilatura.

The Hydrocarbon Processing robots.txt allows reference use and disallows AI training. We send
article text to a model only to pull out outage facts for a refined products desk. Nothing is
trained on it. A source runs only when its `robots_status` in the source list is one of a short
set of stated allows. The fetcher also reads each host's live robots.txt at the start of every
run and skips any path it disallows. The code does not watch robots.txt for changes between runs
or compare it with an earlier copy. If the publisher objects or its policy changes, the operator
edits the status in the source list and reloads it, and the source stops.

## Consequences

The fetching rules live in one file, and the tests swap the network for a mock transport. A
blocked host costs one run, not a retry storm. Text extraction from arbitrary sites will be
rougher than trafilatura's. If the golden-set score shows noise from page boilerplate, revisit
this record. To undo, replace the fetcher module. The adapters only call `get`.
