import json

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

import exporter


class _FakePage:
    """Records goto calls and times out for the first `failures` of them."""

    def __init__(self, failures=0):
        self.failures = failures
        self.calls = []

    def goto(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if len(self.calls) <= self.failures:
            raise PlaywrightTimeout("Page.goto: Timeout 60000ms exceeded.")


def _header(tags_idx=2, width=4):
    header = [f"col{i}" for i in range(width)]
    header[tags_idx] = "Tags"
    return header


def test_expand_tags_adds_sorted_columns():
    rows = [
        _header(),
        ["Alice", "Smith", "sangeet, reception", "yes"],
        ["Bob", "Jones", "reception, optional-trip", "no"],
    ]
    exporter._expand_tags(rows)
    assert rows[0] == _header() + [
        "optional-trip (tag)",
        "reception (tag)",
        "sangeet (tag)",
    ]
    assert rows[1] == ["Alice", "Smith", "sangeet, reception", "yes", 0, 1, 1]
    assert rows[2] == ["Bob", "Jones", "reception, optional-trip", "no", 1, 1, 0]


def test_expand_tags_guest_without_tags():
    rows = [
        _header(),
        ["Alice", "Smith", "sangeet", "yes"],
        ["Bob", "Jones", "", "no"],
    ]
    exporter._expand_tags(rows)
    assert rows[2] == ["Bob", "Jones", "", "no", 0]


def test_expand_tags_pads_ragged_rows():
    rows = [
        _header(tags_idx=2, width=4),
        ["Alice", "Smith", "sangeet", "yes"],
        ["Bob"],
    ]
    exporter._expand_tags(rows)
    assert rows[2] == ["Bob", "", "", "", 0]


def test_expand_tags_strips_whitespace_and_empty_segments():
    rows = [
        _header(),
        ["Alice", "Smith", " sangeet ,  , reception,", "yes"],
    ]
    exporter._expand_tags(rows)
    assert rows[0][-2:] == ["reception (tag)", "sangeet (tag)"]
    assert rows[1][-2:] == [1, 1]


def test_expand_tags_no_tags_header_is_noop():
    rows = [
        ["First", "Last"],
        ["Alice", "Smith"],
    ]
    exporter._expand_tags(rows)
    assert rows == [["First", "Last"], ["Alice", "Smith"]]


def test_expand_tags_header_only_is_noop():
    rows = [_header()]
    exporter._expand_tags(rows)
    assert rows == [_header()]


def test_expand_tags_no_tags_anywhere_is_noop():
    rows = [
        _header(),
        ["Alice", "Smith", "", "yes"],
    ]
    exporter._expand_tags(rows)
    assert rows == [_header(), ["Alice", "Smith", "", "yes"]]


def test_parse_columns_splits_on_commas_and_newlines():
    assert exporter._parse_columns("first name, last name\nemail") == [
        "first name",
        "last name",
        "email",
    ]


def test_parse_columns_strips_whitespace_and_empty_segments():
    assert exporter._parse_columns("  first name , ,\n\n last name ,") == [
        "first name",
        "last name",
    ]


def test_parse_columns_empty_string():
    assert exporter._parse_columns("") == []


def test_select_columns_reorders_and_drops_undeclared():
    rows = [
        ["First", "Last", "Suffix", "Email"],
        ["Alice", "Smith", "Jr", "alice@example.com"],
    ]
    exporter._select_columns(rows, ["email", "first"])
    assert rows == [
        ["email", "first"],
        ["alice@example.com", "Alice"],
    ]


def test_select_columns_missing_column_is_empty():
    rows = [
        ["First", "Last"],
        ["Alice", "Smith"],
    ]
    exporter._select_columns(rows, ["First", "Title", "Last"])
    assert rows == [
        ["First", "Title", "Last"],
        ["Alice", "", "Smith"],
    ]


def test_select_columns_then_expand_tags_keeps_all_tag_columns():
    rows = [
        ["First", "Suffix", "Tags"],
        ["Alice", "Jr", "sangeet, admin"],
        ["Bob", "", "admin"],
    ]
    exporter._select_columns(rows, ["First", "Tags"])
    exporter._expand_tags(rows)
    assert rows[0] == ["First", "Tags", "admin (tag)", "sangeet (tag)"]
    assert rows[1] == ["Alice", "sangeet, admin", 1, 1]
    assert rows[2] == ["Bob", "admin", 1, 0]


def test_select_columns_uses_declared_names_and_matches_case_insensitively():
    rows = [
        ["  First Name ", "LAST NAME"],
        ["Alice", "Smith"],
    ]
    exporter._select_columns(rows, ["first name", "last name"])
    assert rows[0] == ["first name", "last name"]
    assert rows[1] == ["Alice", "Smith"]


def test_select_columns_pads_ragged_rows():
    rows = [
        ["First", "Last", "Email"],
        ["Alice", "Smith", "alice@example.com"],
        ["Bob"],
    ]
    exporter._select_columns(rows, ["Email", "First"])
    assert rows[2] == ["", "Bob"]


def test_select_columns_empty_config_is_noop():
    rows = [
        ["First", "Last"],
        ["Alice", "Smith"],
    ]
    exporter._select_columns(rows, [])
    assert rows == [["First", "Last"], ["Alice", "Smith"]]


def test_rows_equal_treats_int_and_str_cells_as_equal():
    assert exporter._rows_equal([["Alice", 1, 0]], [["Alice", "1", "0"]])


def test_rows_equal_detects_difference():
    assert not exporter._rows_equal([["Alice", 1]], [["Alice", "0"]])


def test_goto_login_succeeds_without_retrying():
    page = _FakePage()
    slept = []
    exporter._goto_login(page, sleep=slept.append)
    assert len(page.calls) == 1
    assert page.calls[0][0] == exporter.LOGIN_URL
    assert page.calls[0][1]["timeout"] == exporter.LOGIN_NAV_TIMEOUT_MS
    assert slept == []


def test_goto_login_retries_transient_timeouts():
    page = _FakePage(failures=2)
    slept = []
    exporter._goto_login(page, sleep=slept.append)
    assert len(page.calls) == 3
    assert slept == [exporter.LOGIN_NAV_BACKOFF_SECONDS] * 2


def test_goto_login_reraises_after_last_attempt():
    page = _FakePage(failures=3)
    slept = []
    with pytest.raises(PlaywrightTimeout):
        exporter._goto_login(page, sleep=slept.append, attempts=3)
    assert len(page.calls) == 3
    assert slept == [exporter.LOGIN_NAV_BACKOFF_SECONDS] * 2


class _FakeContext:
    def __init__(self):
        self.closed = False

    def new_page(self):
        return object()

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.contexts = []

    def new_context(self, **kwargs):
        self.contexts.append(_FakeContext())
        return self.contexts[-1]


def _scripted_login(monkeypatch, outcomes):
    """Patch _login to raise each queued exception in turn, then succeed."""
    calls = []

    def fake_login(page, username, password):
        calls.append(page)
        if outcomes:
            raise outcomes.pop(0)

    monkeypatch.setattr(exporter, "_login", fake_login)
    return calls


def test_login_with_retry_backs_off_on_throttle_then_succeeds(monkeypatch):
    calls = _scripted_login(monkeypatch, [exporter.RateLimited("t"), exporter.RateLimited("t")])
    browser, slept = _FakeBrowser(), []
    page = exporter._login_with_retry(browser, "u", "p", sleep=slept.append, backoff=(1, 2, 3))
    assert page is calls[-1]
    assert len(calls) == 3
    assert slept == [1, 2]
    assert [c.closed for c in browser.contexts] == [True, True, False]


def test_login_with_retry_reraises_after_last_attempt(monkeypatch):
    calls = _scripted_login(monkeypatch, [exporter.RateLimited("t")] * 3)
    slept = []
    with pytest.raises(exporter.RateLimited):
        exporter._login_with_retry(_FakeBrowser(), "u", "p", sleep=slept.append, backoff=(1, 2))
    assert len(calls) == 3
    assert slept == [1, 2]


def test_login_with_retry_does_not_retry_other_login_failures(monkeypatch):
    calls = _scripted_login(monkeypatch, [exporter.LoginFailed("bad password")])
    slept = []
    with pytest.raises(exporter.LoginFailed):
        exporter._login_with_retry(_FakeBrowser(), "u", "p", sleep=slept.append, backoff=(1, 2))
    assert len(calls) == 1
    assert slept == []


GUEST_LIST = "https://withjoy.com/edit/guests"


class _SessionPage:
    """A page whose goto lands on `lands_on` (the guest list, or a login bounce)."""

    def __init__(self, context, lands_on):
        self.context = context
        self.lands_on = lands_on
        self.url = "about:blank"

    def goto(self, url, **kwargs):
        self.url = self.lands_on


class _SessionContext(_FakeContext):
    def __init__(self, lands_on, state=None):
        super().__init__()
        self.lands_on = lands_on
        self.state = state

    def new_page(self):
        return _SessionPage(self, self.lands_on)

    def storage_state(self, **kwargs):
        self.storage_state_kwargs = kwargs
        return self.state


class _SessionBrowser:
    def __init__(self, saved_lands_on=GUEST_LIST, load_error=None):
        self.saved_lands_on = saved_lands_on
        self.load_error = load_error
        self.contexts = []

    def new_context(self, **kwargs):
        if "storage_state" in kwargs and self.load_error:
            raise self.load_error
        self.contexts.append(_SessionContext(self.saved_lands_on))
        return self.contexts[-1]


def _fake_fresh_login(monkeypatch):
    """Patch _login_with_retry to return a logged-in page and record the call."""
    logins = []

    def fake(browser, username, password):
        logins.append(username)
        return _SessionPage(_SessionContext(GUEST_LIST), GUEST_LIST)

    monkeypatch.setattr(exporter, "_login_with_retry", fake)
    return logins


def test_open_session_reuses_a_valid_saved_session(monkeypatch, tmp_path):
    logins = _fake_fresh_login(monkeypatch)
    state = tmp_path / "state.json"
    state.write_text("{}")
    page = exporter._open_session(_SessionBrowser(), str(state), "u", "p", GUEST_LIST)
    assert page.url == GUEST_LIST
    assert logins == []


def test_open_session_logs_in_when_the_saved_session_expired(monkeypatch, tmp_path):
    logins = _fake_fresh_login(monkeypatch)
    state = tmp_path / "state.json"
    state.write_text("{}")
    browser = _SessionBrowser(saved_lands_on="https://auth.withjoy.com/login?state=x")
    page = exporter._open_session(browser, str(state), "u", "p", GUEST_LIST)
    assert page.url == GUEST_LIST
    assert logins == ["u"]
    assert browser.contexts[0].closed


def test_open_session_logs_in_without_a_state_file(monkeypatch, tmp_path):
    logins = _fake_fresh_login(monkeypatch)
    browser = _SessionBrowser()
    exporter._open_session(browser, str(tmp_path / "missing.json"), "u", "p", GUEST_LIST)
    assert logins == ["u"]
    assert browser.contexts == []


def test_open_session_logs_in_when_the_state_file_is_unreadable(monkeypatch, tmp_path):
    logins = _fake_fresh_login(monkeypatch)
    state = tmp_path / "state.json"
    state.write_text("not json")
    browser = _SessionBrowser(load_error=ValueError("bad json"))
    exporter._open_session(browser, str(state), "u", "p", GUEST_LIST)
    assert logins == ["u"]


def test_open_session_without_a_state_path_logs_in(monkeypatch):
    logins = _fake_fresh_login(monkeypatch)
    exporter._open_session(_SessionBrowser(), None, "u", "p", GUEST_LIST)
    assert logins == ["u"]


def test_save_session_writes_private_file_with_indexed_db(tmp_path):
    context = _SessionContext(GUEST_LIST, state={"cookies": [], "origins": []})
    path = tmp_path / "state.json"
    exporter._save_session(context, str(path))
    assert json.loads(path.read_text()) == {"cookies": [], "origins": []}
    assert path.stat().st_mode & 0o777 == 0o600
    assert context.storage_state_kwargs == {"indexed_db": True}
    assert not (tmp_path / "state.json.tmp").exists()


def test_save_session_failure_does_not_raise(tmp_path):
    context = _SessionContext(GUEST_LIST, state={})
    exporter._save_session(context, str(tmp_path / "no-such-dir" / "state.json"))


@pytest.mark.parametrize(("changed", "expected"), [(True, "changed"), (False, "unchanged")])
def test_report_result_writes_the_termination_message(tmp_path, monkeypatch, changed, expected):
    path = tmp_path / "termination-log"
    monkeypatch.setattr(exporter, "TERMINATION_LOG_PATH", str(path))
    exporter._report_result(changed)
    assert path.read_text() == expected


def test_report_result_is_silent_outside_kubernetes(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "TERMINATION_LOG_PATH", str(tmp_path / "missing" / "log"))
    exporter._report_result(True)
