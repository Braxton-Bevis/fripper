"""Recover Amazon searches when one Lucida account region is unavailable."""
from __future__ import annotations

import re

import pyjson5

from lucidadl import api


_COUNTRIES = ("", "US", "GB", "JP")
_CACHE_ATTR = "_lucida_gui_amazon_search_country"


def _safe_error(value) -> str:
    """Keep useful upstream error text without dumping response data or tokens."""
    text = " ".join(str(value).split())
    text = re.sub(r"(?i)(?:bearer\s+)[\w.+/=-]+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)\b(?:token|cookie|csrf|cf_clearance|authorization|password|secret|handoff)"
                  r"\s*[:=]\s*[^\s,;]+", "credential=[redacted]", text)
    text = re.sub(r"[A-Za-z0-9_+/=-]{48,}", "[redacted]", text)
    return text[:240]


def _upstream_error(data, depth=0) -> str | None:
    if depth > 8:
        return None
    if isinstance(data, dict):
        for key in ("error", "err", "searchError"):
            error = data.get(key)
            if error:
                if isinstance(error, str):
                    return _safe_error(error)
                if isinstance(error, dict):
                    for field in ("message", "error", "detail"):
                        if isinstance(error.get(field), str):
                            return _safe_error(error[field])
                return "upstream reported an error"
        for value in data.values():
            error = _upstream_error(value, depth + 1)
            if error:
                return error
    elif isinstance(data, list):
        for value in data:
            error = _upstream_error(value, depth + 1)
            if error:
                return error
    return None


def make_search(original):
    async def search(self, query: str, service: str):
        if api.normalize_service(service) != "amazon":
            return await original(self, query, service)
        cached = getattr(self, _CACHE_ATTR, None)
        countries = list(_COUNTRIES)
        if cached in _COUNTRIES:
            countries.remove(cached)
            countries.insert(0, cached)
        failures = []
        for index, country in enumerate(countries):
            label = country or "Auto"
            if index:
                self.log(f"  Amazon search: trying fallback region {label}.")
            else:
                self.log(f"  searching: {query!r} on amazon ({label})")
            params = {"service": "amazon", "query": query}
            if country:
                params["country"] = country
            try:
                response = await self._get(api.LUCIDA + "/search", params=params)
            except Exception as exc:
                error = f"network request failed ({type(exc).__name__})"
            else:
                if response.status_code != 200:
                    error = f"HTTP {response.status_code}"
                else:
                    blob = api._between(response.text, api._PD_START, api._PD_END)
                    if not blob:
                        error = "search data is missing (upstream error or page format changed)"
                    else:
                        try:
                            data = pyjson5.loads(blob)
                        except Exception:
                            error = "search data could not be parsed"
                        else:
                            error = _upstream_error(data)
                            if not error and api._find_results_node(data) is None:
                                error = "upstream returned no search-results structure"
                            if not error:
                                # An actual empty results node is a successful search.
                                # Keep selection and artist/version matching upstream.
                                result = api._extract_search_results(data)
                                setattr(self, _CACHE_ATTR, country)
                                if index:
                                    self.log(f"  Amazon search: region {label} is working; using it for this client.")
                                return result
            failures.append(f"{label}: {error}")
            self.log(f"  Amazon search region {label} failed: {error}")
        raise api.LucidaError("Amazon search is unavailable in Auto, US, GB, and JP. "
                              "Retry later or choose another provider. Last error: " + failures[-1])

    search._lucida_gui_region_fallback = True
    return search


def install_patch():
    current = api.LucidaClient.search
    if getattr(current, "_lucida_gui_region_fallback", False) is not True:
        api.LucidaClient.search = make_search(current)
