import exporter


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


def test_rows_equal_treats_int_and_str_cells_as_equal():
    assert exporter._rows_equal([["Alice", 1, 0]], [["Alice", "1", "0"]])


def test_rows_equal_detects_difference():
    assert not exporter._rows_equal([["Alice", 1]], [["Alice", "0"]])
