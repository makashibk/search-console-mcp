"""The server must never be able to change anything in Search Console, and the docs must match the code."""

import asyncio
import importlib.util
import pathlib
import re

import pytest

from conftest import call

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = "sc-domain%3Ashop.example"
SITEMAP = "https%3A%2F%2Fwww.shop.example%2Fsitemap.xml"


def registered_tools() -> dict:
    from search_console_mcp import server
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


# ------------------------------------------------------------------ level 1: the Google scope
def test_scope_is_read_only():
    from search_console_mcp import auth
    assert auth.SCOPES == ["https://www.googleapis.com/auth/webmasters.readonly"]
    assert "webmasters.readonly" in auth.LOGIN_HELP


# ------------------------------------------------------------------ level 2: no write tools
def test_no_write_tools_are_registered():
    names = set(registered_tools())
    assert not names & {"submit_sitemap", "delete_sitemap"}
    assert len(names) == 44


def test_server_status_reports_read_only_mode(env):
    status = call("server_status")
    assert status["mode"].startswith("read-only") and "webmasters.readonly" in status["mode"]


# ------------------------------------------------------------------ level 3: the request guard
def test_write_requests_are_blocked_before_leaving_the_machine(env):
    from search_console_mcp import api
    attempts = [
        ("PUT", f"{api.WM}/sites/{SITE}/sitemaps/{SITEMAP}"),        # submit a sitemap
        ("DELETE", f"{api.WM}/sites/{SITE}/sitemaps/{SITEMAP}"),     # delete a sitemap
        ("PUT", f"{api.WM}/sites/{SITE}"),                           # add a property
        ("DELETE", f"{api.WM}/sites/{SITE}"),                        # remove a property
        ("PATCH", f"{api.WM}/sites/{SITE}"),
        ("POST", f"{api.WM}/sites"),
        ("POST", f"{api.WM}/sites/{SITE}/sitemaps"),
        ("POST", f"{api.WM}/sites/{SITE}/searchAnalytics/query/extra"),
        ("POST", f"{api.WM}/sites/{SITE}/searchAnalytics/query/"),
        ("POST", f"{api.WM}/sites/a/b/searchAnalytics/query"),
        ("put", f"{api.WM}/sites/{SITE}/sitemaps/{SITEMAP}"),        # method case doesn't matter
    ]
    for method, url in attempts:
        with pytest.raises(api.WriteBlocked):
            api.request(method, url)
    assert env.calls == []  # not a single request reached Google


def test_read_requests_pass_the_guard(env):
    from search_console_mcp import api
    assert api.request("GET", f"{api.WM}/sites")["siteEntry"]
    api.request("POST", f"{api.WM}/sites/{SITE}/searchAnalytics/query",
                json_body={"startDate": "2026-09-01", "endDate": "2026-09-02", "dimensions": [], "rowLimit": 1})
    api.request("POST", api.INSPECT_URL, json_body={"inspectionUrl": "https://www.shop.example/p1",
                                                    "siteUrl": "sc-domain:shop.example"})
    assert [c[0] for c in env.calls] == ["GET", "POST", "POST"]


def test_credentials_only_go_to_the_search_console_api(env):
    from search_console_mcp import api
    for url in ("https://evil.example/webmasters/v3/sites",
                "http://searchconsole.googleapis.com/webmasters/v3/sites",
                "https://searchconsole.googleapis.com.evil.example/webmasters/v3/sites",
                "https://searchconsole.googleapis.com@evil.example/webmasters/v3/sites"):
        with pytest.raises(api.WriteBlocked):
            api.request("GET", url)
    assert env.calls == []


def test_blocked_write_is_reported_as_a_tool_error(env, monkeypatch):
    from search_console_mcp import api

    def blocked(*args, **kwargs):
        raise api.WriteBlocked("Blocked PUT x: this server is read-only.")

    monkeypatch.setattr(api, "list_sites", blocked)
    with pytest.raises(Exception) as err:
        call("list_properties")
    assert "read-only" in str(err.value)


def test_only_the_api_module_uses_the_credentialed_session():
    """The guard lives in api.request; nothing else may build requests with the signed-in session."""
    import search_console_mcp
    package = pathlib.Path(search_console_mcp.__file__).parent
    offenders = [p.name for p in package.glob("*.py") if p.name not in ("api.py", "auth.py")
                 and re.search(r"auth\.session\(|AuthorizedSession", p.read_text(encoding="utf-8"))]
    assert offenders == []


# ------------------------------------------------------------------ docs match the code
def test_readme_lists_every_tool():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = re.search(r"^## What it can do\n(.*?)(?=^## )", readme, re.S | re.M).group(1)
    documented = set()
    for line in section.splitlines():
        if line.startswith("| `"):
            documented |= set(re.findall(r"`(\w+)`", line.split("|")[1]))
    registered = set(registered_tools())
    assert documented == registered, (sorted(documented - registered), sorted(registered - documented))
    assert f"{len(registered)} tools" in readme


def test_tools_reference_is_up_to_date():
    spec = importlib.util.spec_from_file_location("gen_tools_doc", ROOT / "scripts" / "gen_tools_doc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--check"]) == 0, "docs/tools.md is out of date: run python scripts/gen_tools_doc.py"
