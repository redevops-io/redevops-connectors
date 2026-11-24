"""An id written into a request path is one segment of it. A value that would name another endpoint never reaches the provider."""
import pytest

from redevops_connectors.base_url import PathSegmentError, path_segment
from redevops_connectors.providers import apollo, ayrshare, blotato, gcalendar, gmail, hubspot, klaviyo, polar, stripe
from redevops_connectors.transport import Response


class Recorder:
    def __init__(self):
        self.urls = []

    def request(self, method, url, headers=None, body=None, **kw):
        self.urls.append((method, url))
        return Response(status=404, text="{}", json={})


class Secrets:
    def resolve(self, ref):
        return {"api_key": "k", "access_token": "t", "secret_key": "k"}


HOSTILE = ["ch_1/../customers", "../customers", "x?expand[]=data", "x#frag", "a/b", "..", ".", "", "x y", "x\\y", "%2e%2e/customers"]
ADAPTERS = [(stripe, "StripeAdapter"), (polar, "PolarAdapter"), (hubspot, "HubSpotAdapter"), (klaviyo, "KlaviyoAdapter"), (apollo, "ApolloAdapter"),
            (ayrshare, "AyrshareAdapter"), (blotato, "BlotatoAdapter"), (gmail, "GmailAdapter"), (gcalendar, "GoogleCalendarAdapter")]


def _build(module, name):
    cls = next(v for k, v in vars(module).items() if k.lower() == name.lower())
    rec = Recorder()
    return cls(transport=rec, resolver=Secrets(), credential_ref="ref"), rec


@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_value_is_refused_or_becomes_exactly_one_segment(value):
    try:
        seg = path_segment(value)
    except PathSegmentError:
        return
    assert "/" not in seg and "?" not in seg and "#" not in seg and seg not in (".", "..") and "%2e" not in seg.lower().replace("%252e", "")


@pytest.mark.parametrize("module,name", ADAPTERS, ids=[n for _m, n in ADAPTERS])
@pytest.mark.parametrize("ref", HOSTILE)
def test_observe_never_asks_the_provider_for_another_path(module, name, ref):
    adapter, rec = _build(module, name)
    plain, plain_rec = _build(module, name)
    plain.observe("abc123")
    assert plain_rec.urls, "the control reference made no request: the test would prove nothing"
    depth = plain_rec.urls[-1][1].split("?")[0].count("/")
    out = adapter.observe(ref)
    assert out.found is False
    for _method, url in rec.urls:
        path = url.split("?")[0]
        assert path.count("/") == depth and "/../" not in path and not path.endswith("/..") and "#" not in url, url
        assert url.count("?") == plain_rec.urls[-1][1].count("?"), url


def test_a_find_by_id_with_a_hostile_id_is_an_error_result_and_no_other_endpoint_is_called():
    adapter, rec = _build(stripe, "StripeAdapter")
    out = adapter.execute("billing.charge.find", {"id": "ch_1/../customers", "charge_id": "ch_1/../customers"})
    assert out.ok is False
    assert all("customers" not in url.split("?")[0].rsplit("/", 1)[0] and "/../" not in url for _m, url in rec.urls)
