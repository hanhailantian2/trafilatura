"""
Deriving link info from sitemaps.
"""

## This file is available from https://github.com/adbar/trafilatura
## under GNU GPL v3 license


import logging
import re
from itertools import islice
from typing import List, Optional

from courlan import (
    clean_url,
    extract_domain,
    filter_urls,
    fix_relative_urls,
    get_hostinfo,
    lang_filter,
)

from .downloads import fetch_url, is_live_page
from .settings import MAX_LINKS, MAX_SITEMAPS_SEEN
from .utils import is_similar_domain

# import urllib.robotparser # Python >= 3.8
# ROBOT_PARSER = urllib.robotparser.RobotFileParser()


LOGGER = logging.getLogger(__name__)

LINK_REGEX = re.compile(r"<loc>(?:<!\[CDATA\[)?(http.+?)(?:\]\]>)?</loc>")
XHTML_REGEX = re.compile(r"<xhtml:link.+?>", re.DOTALL)
HREFLANG_REGEX = re.compile(r'href=["\'](.+?)["\']')
WHITELISTED_PLATFORMS = re.compile(
    r"(?:blogger|blogpost|ghost|hubspot|livejournal|medium|typepad|squarespace|tumblr|weebly|wix|wordpress)\."
)

SITEMAP_FORMAT = re.compile(r"^.{0,5}<\?xml|<sitemap|<urlset")
DETECT_SITEMAP_LINK = re.compile(r"\.xml(\..{2,4})?$|\.xml[?#]")
DETECT_LINKS = re.compile(r'https?://[^\s<"]+')
SCRUB_REGEX = re.compile(r"\?.*$|#.*$")
POTENTIAL_SITEMAP = re.compile(r"\.xml\b")  # |\bsitemap\b

GUESSES = ["sitemap.xml.gz", "sitemap", "sitemap_index.xml", "sitemap_news.xml"]


class SitemapObject:
    "Store all necessary information on sitemap download and processing."
    __slots__ = [
        "base_url",
        "content",
        "domain",
        "external",
        "sitemap_url",
        "sitemap_urls",
        "target_lang",
        "urls",
    ]

    def __init__(
        self,
        base_url: str,
        domain: str,
        sitemap_url: str,
        target_lang: Optional[str] = None,
        external: bool = False,
    ) -> None:
        self.base_url: str = base_url
        self.content: str = ""
        self.domain: str = domain
        self.external: bool = external
        self.sitemap_url: str = sitemap_url
        self.sitemap_urls: List[str] = []
        self.target_lang: Optional[str] = target_lang
        self.urls: List[str] = []

    def fetch(self) -> None:
        "Fetch a sitemap over the network."
        LOGGER.debug("fetching sitemap: %s", self.sitemap_url)
        self.content = fetch_url(self.sitemap_url)

    def handle_link(self, link: str) -> None:
        """Examine a link and determine if it's valid and if it leads to
        a sitemap or a web page."""
        if link == self.sitemap_url:  # safety check
            return
        # fix, check, clean and normalize
        link = fix_relative_urls(self.base_url, link)
        link = clean_url(link, self.target_lang)

        if link is None or not lang_filter(link, self.target_lang):
            return

        newdomain = extract_domain(link, fast=True)
        if newdomain is None:
            LOGGER.error("couldn't extract domain: %s", link)
            return

        # don't take links from another domain and make an exception for main platforms
        # also bypass: subdomains vs. domains
        if (
            not self.external
            and not WHITELISTED_PLATFORMS.search(newdomain)
            and not is_similar_domain(self.domain, newdomain)
        ):
            LOGGER.warning(
                "link discarded, diverging domain names: %s %s", self.domain, newdomain
            )
            return

        if DETECT_SITEMAP_LINK.search(link):
            self.sitemap_urls.append(link)
        else:
            self.urls.append(link)

    def extract_sitemap_langlinks(self) -> None:
        "Extract links corresponding to a given target language."
        if "hreflang=" not in self.content:
            return
        # compile regex here for modularity and efficiency
        lang_regex = re.compile(
            rf"hreflang=[\"']({self.target_lang}.*?|x-default)[\"']", re.DOTALL
        )
        # extract
        for attrs in (
            m[0] for m in islice(XHTML_REGEX.finditer(self.content), MAX_LINKS)
        ):
            if lang_regex.search(attrs):
                lang_match = HREFLANG_REGEX.search(attrs)
                if lang_match:
                    self.handle_link(lang_match[1])
        LOGGER.debug(
            "%s sitemaps and %s links with hreflang found for %s",
            len(self.sitemap_urls),
            len(self.urls),
            self.sitemap_url,
        )

    def extract_sitemap_links(self) -> None:
        "Extract sitemap links and web page links from a sitemap file."
        # extract
        for match in (
            m[1] for m in islice(LINK_REGEX.finditer(self.content), MAX_LINKS)
        ):
            # process middle part of the match tuple
            self.handle_link(match)
        LOGGER.debug(
            "%s sitemaps and %s links found for %s",
            len(self.sitemap_urls),
            len(self.urls),
            self.sitemap_url,
        )

    def process(self) -> None:
        "Download a sitemap and extract the links it contains."
        plausible = is_plausible_sitemap(self.sitemap_url, self.content)
        # safeguard
        if not plausible:
            return
        # try to extract links from TXT file
        if not SITEMAP_FORMAT.match(self.content):
            for match in (
                m[0] for m in islice(DETECT_LINKS.finditer(self.content), MAX_LINKS)
            ):
                self.handle_link(match)
            return
        # process XML sitemap
        if self.target_lang is not None:
            self.extract_sitemap_langlinks()
            if self.sitemap_urls or self.urls:
                return
        self.extract_sitemap_links()


def sitemap_search(
    url: str, target_lang: Optional[str] = None, external: bool = False
) -> List[str]:
    """Look for sitemaps for the given URL and gather links.

    Args:
        url: Webpage or sitemap URL as string.
             Triggers URL-based filter if the webpage isn't a homepage.
        target_lang: Define a language to filter URLs based on heuristics
                     (two-letter string, ISO 639-1 format).
        external: Similar hosts only or external URLs
                  (boolean, defaults to False).

    Returns:
        The extracted links as a list (sorted list of unique links).

    """
    domainname, baseurl = get_hostinfo(url)
    if domainname is None:
        LOGGER.warning("invalid URL: %s", url)
        return []

    if not is_live_page(baseurl):
        LOGGER.warning("base URL unreachable, dropping sitemap: %s", url)
        return []

    urlfilter = None
    if url.endswith((".gz", "sitemap", ".xml")):
        sitemapurl = url
    else:
        sitemapurl = baseurl + "/sitemap.xml"
        # filter triggered, prepare it
        if len(url) > len(baseurl) + 2:
            urlfilter = url

    sitemap = SitemapObject(baseurl, domainname, sitemapurl, target_lang, external)
    sitemap.fetch()
    sitemap.process()

    if not sitemap.sitemap_urls and sitemap.urls:
        linklist = filter_urls(sitemap.urls, urlfilter)
        LOGGER.debug("%s sitemap links found for %s", len(linklist), domainname)
        return linklist

    # try sitemaps in robots.txt file if nothing has been found
    if not sitemap.sitemap_urls and not sitemap.urls:
        sitemap.sitemap_urls = find_robots_sitemaps(baseurl)
        # try additional URLs just in case
        if not sitemap.sitemap_urls:
            sitemap.sitemap_urls = ["".join([baseurl, "/", g]) for g in GUESSES]

    # iterate through nested sitemaps and results
    seen = {sitemapurl}
    i = 1
    while sitemap.sitemap_urls:
        sitemap.sitemap_url = sitemap.sitemap_urls.pop()
        sitemap.fetch()
        sitemap.process()
        # sanity check: keep track of visited sitemaps and exclude them
        seen.add(sitemap.sitemap_url)
        sitemap.sitemap_urls = [s for s in sitemap.sitemap_urls if s not in seen]
        # counter and safeguard
        i += 1
        if i > MAX_SITEMAPS_SEEN:
            break

    sitemap.urls = filter_urls(sitemap.urls, urlfilter)
    LOGGER.debug("%s sitemap links found for %s", len(sitemap.urls), domainname)
    return sitemap.urls


def is_plausible_sitemap(url: str, contents: Optional[str]) -> bool:
    """Check if the sitemap corresponds to an expected format,
    i.e. TXT or XML."""
    if contents is None:
        return False

    # strip query and fragments
    url = SCRUB_REGEX.sub("", url)

    # check content
    if (
        POTENTIAL_SITEMAP.search(url)
        and (not isinstance(contents, str) or not SITEMAP_FORMAT.match(contents))
        or "<html" in contents[:150].lower()
    ):
        LOGGER.warning("not a valid XML sitemap: %s", url)
        return False

    return True


def find_robots_sitemaps(baseurl: str) -> List[str]:
    """Guess the location of the robots.txt file and try to extract
    sitemap URLs from it"""
    robotstxt = fetch_url(baseurl + "/robots.txt")
    return extract_robots_sitemaps(robotstxt, baseurl)


def extract_robots_sitemaps(robotstxt: str, baseurl: str) -> List[str]:
    "Read a robots.txt file and find sitemap links."
    # sanity check on length (cause: redirections)
    if robotstxt is None or len(robotstxt) > 10000:
        return []
    sitemapurls = []
    # source: https://github.com/python/cpython/blob/3.8/Lib/urllib/robotparser.py
    for line in robotstxt.splitlines():
        # remove optional comment and strip line
        i = line.find("#")
        if i >= 0:
            line = line[:i]
        line = line.strip()
        if not line:
            continue
        line = line.split(":", 1)
        if len(line) == 2:
            line[0] = line[0].strip().lower()
            if line[0] == "sitemap":
                # urllib.parse.unquote(line[1].strip())
                candidate = fix_relative_urls(baseurl, line[1].strip())
                sitemapurls.append(candidate)
    LOGGER.debug("%s sitemaps found in robots.txt", len(sitemapurls))
    return sitemapurls
