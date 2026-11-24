"""The base URL an operator sets decides where an adapter sends its credential. It is checked where it is set."""
import pytest

from redevops_connectors.base_url import BaseUrlError, credential_base
from redevops_connectors.providers.postiz import PostizAdapter
from redevops_connectors.providers.web import WebAdapter
from redevops_connectors.providers.whatsapp_waha import WhatsAppWahaAdapter

BAD = ["https://api.search.brave.com@other.example/res/v1", "https://user:pw@other.example", "ftp://other.example", "file:///etc/passwd",
       "//other.example", "other.example", "https://", "https://ok.example/v1?x=1", "https://ok.example/v1#x", "https://ok.example\\@other.example",
       "https://ok.example /v1", "https://ok.example:notaport", "", "   "]


@pytest.mark.parametrize("url", BAD)
def test_a_base_that_is_not_a_plain_http_url_with_a_host_is_refused(url):
    with pytest.raises(BaseUrlError):
        credential_base(url)


@pytest.mark.parametrize("url,out", [("https://api.search.brave.com/res/v1/", "https://api.search.brave.com/res/v1"), ("http://localhost:3000", "http://localhost:3000"),
                                     ("http://waha:3000/", "http://waha:3000"), ("https://postiz.internal.example/api/public/v1", "https://postiz.internal.example/api/public/v1"),
                                     ("http://[::1]:8080/x", "http://[::1]:8080/x")])
def test_an_ordinary_base_is_kept_without_its_trailing_slash(url, out):
    assert credential_base(url) == out


def _adapters():
    kw = dict(transport=object(), resolver=lambda ref: "k")
    return [(WebAdapter(**kw), "search_base"), (PostizAdapter(**kw), "base_url"), (WhatsAppWahaAdapter(**kw), "base_url")]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_connect_refuses_a_base_whose_host_is_not_the_one_it_shows(index):
    adapter, key = _adapters()[index]
    before = {k: v for k, v in vars(adapter).items() if "base" in k}
    with pytest.raises(BaseUrlError):
        adapter.connect({key: "https://api.search.brave.com@other.example/"}, "ref")
    assert {k: v for k, v in vars(adapter).items() if "base" in k} == before      # the destination did not change


def test_the_constructors_refuse_it_too():
    kw = dict(transport=object(), resolver=lambda ref: "k")
    for build in (lambda: WebAdapter(search_base="https://a.example@b.example", **kw), lambda: PostizAdapter(base_url="https://a.example@b.example", **kw),
                  lambda: WhatsAppWahaAdapter(base_url="https://a.example@b.example", **kw)):
        with pytest.raises(BaseUrlError):
            build()
