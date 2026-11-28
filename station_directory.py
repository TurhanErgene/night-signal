"""Search Radio Browser and parse stream URLs for Night Signal."""

from dataclasses import dataclass
from http.client import HTTPException
import json
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


MIRRORS = ("https://de1.api.radio-browser.info", "https://nl1.api.radio-browser.info")
SEARCH_FIELDS = ("Name", "Genre", "Country", "URL")
RESULT_LIMIT = 30
RESPONSE_LIMIT = 512_000


@dataclass(frozen=True)
class Station:
    name: str
    genre: str
    url: str = ""
    file: Path | None = None


def clean_text(value):
    return "".join(character for character in " ".join(value.split()) if character.isprintable())


def stream_host(url):
    if not url or any(character.isspace() or not character.isprintable() for character in url):
        raise ValueError("Enter a direct HTTP or HTTPS stream URL.")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Enter a direct HTTP or HTTPS stream URL.")
    return parsed.hostname


def custom_station(query):
    name, separator, url = query.partition("|")
    if not separator:
        name, url = "", name
    url = url.strip()
    host = stream_host(url)
    return Station(clean_text(name) or host, "Custom stream", url)


def search_stations(field, query):
    field_name = {"Name": "name", "Genre": "tag", "Country": "country"}[field]
    if field == "Country" and len(query) == 2 and query.isascii() and query.isalpha():
        field_name, query = "countrycode", query.upper()
    params = urlencode({
        field_name: query, "limit": RESULT_LIMIT, "hidebroken": "true",
        "order": "votes", "reverse": "true",
    })
    for mirror in MIRRORS:
        request = Request(f"{mirror}/json/stations/search?{params}",
                          headers={"User-Agent": "Night Signal terminal radio/1.0",
                                   "Accept": "application/json"})
        try:
            with urlopen(request, timeout=4) as response:
                payload = response.read(RESPONSE_LIMIT + 1)
            if len(payload) > RESPONSE_LIMIT:
                raise ValueError("Search response is too large.")
            entries = json.loads(payload)
            if not isinstance(entries, list):
                raise ValueError("Unexpected search response.")
        except (OSError, ValueError, HTTPException):
            continue

        results = {}
        for entry in entries[:RESULT_LIMIT]:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url_resolved") or entry.get("url")
            if not isinstance(url, str) or url in results:
                continue
            try:
                host = stream_host(url)
            except ValueError:
                continue
            name = clean_text(entry.get("name") or "") if isinstance(entry.get("name"), str) else ""
            details = [clean_text(entry[key]) for key in ("country", "tags")
                       if isinstance(entry.get(key), str) and entry[key].strip()]
            results[url] = Station(name or host, " / ".join(details) or "Directory station", url)
        return list(results.values())
    raise OSError("Station directory unavailable. Try again or add a URL.")


class Discovery:
    def __init__(self):
        self.field = "Name"
        self.query = ""
        self.results = []
        self.selected = 0
        self.note = ""
        self.pending = None

    def clear(self):
        self.query = ""
        self.results = []
        self.selected = 0
        self.note = ""

    def search(self):
        self.pending = SimpleQueue()
        self.note = "Searching Radio Browser..."
        Thread(target=self.fetch, args=(self.field, self.query.strip(), self.pending),
               daemon=True).start()

    @staticmethod
    def fetch(field, query, response):
        try:
            stations = search_stations(field, query)
        except OSError as error:
            response.put(([], str(error)))
        else:
            response.put((stations, "" if stations else "No stations found. Try another search."))

    def update(self):
        if self.pending is not None:
            try:
                self.results, self.note = self.pending.get_nowait()
            except Empty:
                return
            self.pending = None
            self.selected = 0
