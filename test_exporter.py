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


def _golkonda_header():
    return ["First", "Last", "golkonda guest covered", "golkonda guest own"]


def test_aggregate_golkonda_coalesces_source_values():
    rows = [
        _golkonda_header(),
        ["Alice", "Smith", "2", ""],
        ["Bob", "Jones", "", "3"],
        ["Cara", "Lee", "", ""],
    ]
    exporter._aggregate_golkonda(rows)
    assert rows[0] == ["First", "Last", "golkonda guest covered", "golkonda guest own", "golkonda aggregated"]
    assert rows[1] == ["Alice", "Smith", "2", "", "2"]
    assert rows[2] == ["Bob", "Jones", "", "3", "3"]
    assert rows[3] == ["Cara", "Lee", "", "", ""]


def test_aggregate_golkonda_inserts_after_own_column():
    rows = [
        ["First", "golkonda guest own", "golkonda guest covered", "Last"],
        ["Alice", "", "4", "Smith"],
    ]
    exporter._aggregate_golkonda(rows)
    assert rows[0] == ["First", "golkonda guest own", "golkonda aggregated", "golkonda guest covered", "Last"]
    assert rows[1] == ["Alice", "", "4", "4", "Smith"]


def test_aggregate_golkonda_covered_wins_when_both_present():
    rows = [
        _golkonda_header(),
        ["Alice", "Smith", "1", "9"],
    ]
    exporter._aggregate_golkonda(rows)
    assert rows[1] == ["Alice", "Smith", "1", "9", "1"]


def test_aggregate_golkonda_pads_ragged_rows():
    rows = [
        _golkonda_header(),
        ["Alice", "Smith", "2"],
        ["Bob"],
    ]
    exporter._aggregate_golkonda(rows)
    assert rows[1] == ["Alice", "Smith", "2", "", "2"]
    assert rows[2] == ["Bob", "", "", "", ""]


def test_aggregate_golkonda_missing_column_is_noop():
    rows = [
        ["First", "Last", "golkonda guest covered"],
        ["Alice", "Smith", "2"],
    ]
    exporter._aggregate_golkonda(rows)
    assert rows == [
        ["First", "Last", "golkonda guest covered"],
        ["Alice", "Smith", "2"],
    ]


def test_aggregate_golkonda_header_only_is_noop():
    rows = [_golkonda_header()]
    exporter._aggregate_golkonda(rows)
    assert rows == [_golkonda_header()]


def test_rows_equal_treats_int_and_str_cells_as_equal():
    assert exporter._rows_equal([["Alice", 1, 0]], [["Alice", "1", "0"]])


def test_rows_equal_detects_difference():
    assert not exporter._rows_equal([["Alice", 1]], [["Alice", "0"]])
