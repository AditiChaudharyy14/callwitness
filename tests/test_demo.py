"""The first minute a stranger spends with this.

The risk in a demo command is not that it fails -- a failure is visible and
gets reported. It is that it succeeds at something destructive on someone's
machine, or that it hangs, or that it invents arguments and sends them to a
stranger's server. So these lean on the refusals.
"""

from callwitness.demo import (
    DEFAULT_SERVER, arguments_for, human, is_safe, words,
)


# -- the safety rule -------------------------------------------------------

def test_the_tools_that_would_have_been_a_disaster_are_refused():
    """Each of these takes no required arguments, so nothing would stop them."""
    assert not is_safe("kubectl_delete")
    assert not is_safe("cleanup")
    assert not is_safe("run_process")
    assert not is_safe("execute_command")


def test_a_write_verb_anywhere_disqualifies():
    assert not is_safe("get_and_delete")
    assert not is_safe("list_then_remove_stale")
    assert not is_safe("safely_drop_table")


def test_a_read_verb_has_to_come_early():
    assert is_safe("list_pages")
    assert is_safe("getConsoleLogs")
    assert not is_safe("compact_database_and_read")


def test_camel_case_is_split_like_snake_case():
    assert words("getNetworkRequests") == ["get", "network", "requests"]
    assert is_safe("getNetworkRequests")


def test_a_name_with_no_verb_at_all_is_refused():
    assert not is_safe("thing")
    assert not is_safe("")
    assert not is_safe("x1_y2")


# -- arguments -------------------------------------------------------------

def test_required_fields_are_filled_by_type():
    schema = {"required": ["message", "count", "flag"],
              "properties": {"message": {"type": "string"},
                             "count": {"type": "integer"},
                             "flag": {"type": "boolean"}}}
    assert arguments_for(schema) == {"message": "callwitness demo",
                                     "count": 1, "flag": False}


def test_a_tool_with_no_required_fields_gets_an_empty_object():
    assert arguments_for({"type": "object", "properties": {}}) == {}
    assert arguments_for(None) == {}


def test_an_enum_takes_its_first_value_rather_than_a_guess():
    schema = {"required": ["mode"],
              "properties": {"mode": {"enum": ["read", "write"]}}}
    assert arguments_for(schema) == {"mode": "read"}


def test_a_schema_we_cannot_satisfy_is_skipped_not_guessed():
    """None means skip. Sending an invented value to someone's server is worse."""
    assert arguments_for({"required": ["thing"],
                          "properties": {"thing": {"type": "kitten"}}}) is None
    assert arguments_for({"required": ["thing"], "properties": {}}) is None
    assert arguments_for({"required": ["m"],
                          "properties": {"m": {"enum": []}}}) is None


# -- presentation ----------------------------------------------------------

def test_sizes_are_readable():
    assert human(900) == "900 B"
    assert human(2048) == "2.0 KB"


def test_the_default_server_needs_no_credentials():
    """The whole point is that it runs with nothing set up."""
    assert "npx" in DEFAULT_SERVER
    assert any("server-everything" in part for part in DEFAULT_SERVER)
