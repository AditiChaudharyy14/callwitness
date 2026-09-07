import json

from bollard.analyze import extract_entities, extract_signals, is_routing_key, shape_only


def test_finds_host_email_and_ip():
    blob = json.dumps({
        "url": "https://exfil.example.net/upload?k=1",
        "to": "Ops@Acme.com",
        "host": "10.0.0.7",
    })
    found = extract_entities(blob)
    assert found["hosts"] == ["exfil.example.net"]
    assert found["emails"] == ["ops@acme.com"]
    assert "10.0.0.7" in found["ips"]


def test_host_parsing_strips_credentials_port_and_path():
    blob = "https://user:pw@Internal.Corp:8443/a/b/c"
    assert extract_entities(blob)["hosts"] == ["internal.corp"]


def test_rejects_impossible_ipv4():
    # a version string must not be mistaken for an address
    assert "ips" not in extract_entities("build 999.999.999.999")


def test_returns_empty_dict_when_nothing_present():
    assert extract_entities(json.dumps({"query": "select * from customers"})) == {}


def test_deduplicates_and_sorts():
    blob = "https://a.com https://b.com https://a.com"
    assert extract_entities(blob)["hosts"] == ["a.com", "b.com"]


def test_shape_only_hides_values_but_keeps_structure():
    address = "secret@corp.com"
    shape = shape_only({"to": address, "rows": [1, 2, 3], "n": 4, "ok": True})
    assert shape == {
        "to": f"<str:{len(address)}>",
        "rows": ["<list:3>"],
        "n": "<num>",
        "ok": "<bool>",
    }
    assert "secret" not in json.dumps(shape)


def test_shape_only_stops_recursing():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    assert "<deep>" in json.dumps(shape_only(deep))


def test_routing_keys_are_recognised():
    for key in ("to", "attach_url", "callbackUrl", "recipients", "webhook", "DEST"):
        assert is_routing_key(key), key
    for key in ("body", "subject", "query", "content", "name"):
        assert not is_routing_key(key), key


def test_signals_separate_destination_from_payload():
    signals = extract_signals({
        "to": "archive@unknown.example",
        "body": "contact a@x.com or b@y.com, see https://blog.example/post",
    })
    assert signals["destinations"]["emails"] == ["archive@unknown.example"]
    assert "hosts" not in signals["destinations"]
    assert signals["content_counts"] == {"hosts": 1, "emails": 2}


def test_signals_are_empty_when_nothing_is_addressed():
    assert extract_signals({"query": "select 1"}) == {}


def test_nested_routing_fields_are_followed():
    signals = extract_signals({"target": {"deep": {"value": "https://a.example/x"}}})
    assert signals["destinations"]["hosts"] == ["a.example"]
