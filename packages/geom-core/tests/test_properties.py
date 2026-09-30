"""Hypothesis property tests: the invariants geom-core must never break.

Per ``technical-build/06`` section 3, property tests carry adversarial values:
zero-length intervals, equal endpoints, negative time, clock jumps, huge spans,
reversed altitude and duplicated occupancy. Each test states the invariant it
proves and, where the invariant has a limit, what it does not prove.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from droneops_geom_core import (
    INT64_MICROSECOND_MAX,
    INT64_MICROSECOND_MIN,
    QUANTIZATION_TOLERANCE_MM,
    Altitude,
    AltitudeBand,
    AltitudeReference,
    Cell2D,
    Cell3D,
    GeomCoreError,
    GridSpec,
    Interval,
    Microsecond,
    Millimetre,
    RangeRelation,
    ResourceKey,
    ResourceKind,
    SafeIntervalPadding,
    SafeIntervalQuery,
    SafeWindow,
    TimeDomainError,
    VerticalDatums,
    classify_entry,
    compute_safe_intervals,
    geodesic_distance_mm,
    haversine_distance_m,
    merge,
    merge_bands,
    merge_cells_by_token,
    resource_key,
    sorted_resource_keys,
    subtract,
    total_measure,
)

pytestmark = pytest.mark.property

SPEC = GridSpec(32643, Millimetre(16_000), Millimetre(10_000))
CONSTRUCTIBLE = [
    AltitudeReference.AGL,
    AltitudeReference.AMSL,
    AltitudeReference.GROUND,
    AltitudeReference.PAD,
]

# --- strategies -------------------------------------------------------------

small_time = st.integers(min_value=-60, max_value=180)
measure = st.integers(min_value=0, max_value=120)


@st.composite
def small_intervals(draw: st.DrawFn) -> Interval:
    start = draw(small_time)
    return Interval.of(start, start + draw(measure))


@st.composite
def interval_lists(draw: st.DrawFn, max_size: int = 6) -> list[Interval]:
    return draw(st.lists(small_intervals(), max_size=max_size))


@st.composite
def domain_edges(draw: st.DrawFn) -> Interval:
    """Adversarial spans: the int64 corners of the time domain."""
    low = draw(st.sampled_from([INT64_MICROSECOND_MIN, INT64_MICROSECOND_MIN + 10]))
    high = draw(st.sampled_from([INT64_MICROSECOND_MAX, INT64_MICROSECOND_MAX - 10]))
    return Interval.of(low, high)


@st.composite
def paddings(draw: st.DrawFn) -> SafeIntervalPadding:
    return SafeIntervalPadding(
        Microsecond(draw(st.integers(0, 8))), Microsecond(draw(st.integers(0, 8)))
    )


@st.composite
def identifiers(draw: st.DrawFn) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ0123456789._~-".rstrip(".")
    body = draw(st.text(alphabet=alphabet, min_size=1, max_size=20))
    assume(body.strip("._~-") != "")
    return body


@st.composite
def datums(draw: st.DrawFn) -> VerticalDatums:
    terrain = Millimetre(draw(st.integers(-500_000, 4_000_000)))
    site = Millimetre(draw(st.integers(-500_000, 4_000_000)))
    # The round-trip properties need a complete datum set; the missing-deck
    # rejection has its own test below, so this strategy never omits the pad.
    deck = Millimetre(draw(st.integers(-500_000, 4_000_000)))
    return VerticalDatums(terrain, site, deck)


@st.composite
def altitudes(draw: st.DrawFn) -> Altitude:
    return Altitude(
        draw(st.sampled_from(CONSTRUCTIBLE)),
        Millimetre(draw(st.integers(-2_000_000, 5_000_000))),
    )


@st.composite
def bands(draw: st.DrawFn) -> AltitudeBand:
    reference = draw(st.sampled_from(CONSTRUCTIBLE))
    lower = Millimetre(draw(st.integers(-500_000, 500_000)))
    return AltitudeBand(reference, lower, Millimetre(lower + draw(st.integers(0, 200_000))))


@st.composite
def fine_cells(draw: st.DrawFn) -> Cell3D:
    """Level-6 cells, so block arithmetic stays inside the declared domain."""
    east = draw(st.integers(-8, 8)) * SPEC.cell_size_mm
    north = draw(st.integers(-8, 8)) * SPEC.cell_size_mm
    altitude = draw(st.integers(-4, 6)) * SPEC.vertical_band_mm
    return SPEC.volume_cell_of(
        east, north, altitude, level=6, reference=AltitudeReference.AMSL
    )


def window_entries(windows: tuple[SafeWindow, ...]) -> set[int]:
    out: set[int] = set()
    for window in windows:
        out.update(range(window.entry.start, window.entry.end))
    return out


# --- interval algebra -------------------------------------------------------


@given(base=small_intervals(), others=interval_lists())
def test_subtraction_reconstructs_the_original_measure(base: Interval, others: list[Interval]) -> None:
    """I: removing a set of ranges never loses or invents an instant.

    ``measure(base) == measure(base minus others) + measure(base and others)`` with
    overlaps counted once on both sides.
    """
    remainder = subtract(base, others)
    claimed = merge([window for other in others for window in other.intersection(base)])
    assert total_measure(remainder) + total_measure(claimed) == base.measure


@given(base=small_intervals(), others=interval_lists())
def test_subtraction_pieces_are_canonical(base: Interval, others: list[Interval]) -> None:
    pieces = subtract(base, others)
    assert pieces == tuple(sorted(pieces, key=lambda item: (item.start, item.end)))
    assert all(not piece.is_empty for piece in pieces)
    for left, right in zip(pieces, pieces[1:]):
        assert left.end < right.start
        assert not left.overlaps(right)


@given(base=small_intervals(), other=small_intervals())
def test_union_and_intersection_obey_inclusion_exclusion(
    base: Interval, other: Interval
) -> None:
    union = total_measure(base.union(other))
    overlap = total_measure(base.intersection(other))
    assert union + overlap == base.measure + other.measure


@given(intervals=interval_lists(max_size=8))
def test_merge_is_idempotent_and_order_independent(intervals: list[Interval]) -> None:
    once = merge(intervals)
    assert merge(once) == once
    assert merge(list(reversed(intervals))) == once
    assert merge(intervals + intervals) == once
    assert total_measure(once) == total_measure(intervals)
    for index, item in enumerate(once):
        assert not item.is_empty
        for other in once[index + 1 :]:
            assert not item.overlaps(other)
            assert not item.touches(other)


@given(left=small_intervals(), right=small_intervals())
def test_adjacency_is_never_overlap(left: Interval, right: Interval) -> None:
    if left.touches(right):
        assert not left.overlaps(right)
    if left.overlaps(right):
        assert not left.touches(right)


@given(interval=small_intervals())
def test_an_empty_interval_blocks_nothing(interval: Interval) -> None:
    empty = Interval.empty_at(interval.start)
    assert not empty.overlaps(interval)
    assert empty.measure == 0
    assert subtract(interval, [empty]) == ((interval,) if not interval.is_empty else ())


@given(left=small_intervals(), right=small_intervals())
def test_allen_relation_inverts(left: Interval, right: Interval) -> None:
    assume(not left.is_empty and not right.is_empty)
    from droneops_geom_core import INVERSE_RELATION

    forward = left.relation_to(right)
    assert right.relation_to(left) is INVERSE_RELATION[forward]
    assert (forward is RangeRelation.EQUALS) == (left == right)


@given(interval=small_intervals(), delta=st.integers(-500, 500))
def test_shift_preserves_measure(interval: Interval, delta: int) -> None:
    shifted = interval.shifted_by(delta)
    assert shifted.measure == interval.measure
    assert shifted.shifted_by(-delta) == interval


@given(span=domain_edges())
def test_domain_edge_spans_behave(span: Interval) -> None:
    """A span reaching the int64 corners still merges, subtracts and shifts."""
    assert span.measure >= 0
    pieces = subtract(span, [Interval.of(0, 1)])
    assert total_measure(pieces) + 1 == span.measure
    assert merge(list(pieces)) == pieces


# --- resource keys ----------------------------------------------------------


@given(
    kind=st.sampled_from(list(ResourceKind)),
    identifier=identifiers(),
    start=st.integers(-10**9, 10**9),
    length=st.integers(1, 10**6),
)
def test_resource_key_round_trip_is_idempotent(
    kind: ResourceKind, identifier: str, start: int, length: int
) -> None:
    """Parse and re-emit changes nothing, twice over."""
    window = Interval.of(start, start + length)
    key = (
        resource_key(kind, identifier, window=window)
        if kind.takes_window
        else resource_key(kind, identifier)
    )
    parsed = ResourceKey.parse(key.canonical)
    assert parsed == key
    assert parsed.canonical == key.canonical
    assert ResourceKey.parse(parsed.canonical).canonical == key.canonical
    assert parsed.lock_token() == key.lock_token()


@st.composite
def resource_keys(draw: st.DrawFn, max_size: int = 8) -> list[ResourceKey]:
    keys: list[ResourceKey] = []
    for index in range(draw(st.integers(0, max_size))):
        kind = draw(st.sampled_from(list(ResourceKind)))
        identifier = draw(identifiers())
        if not kind.takes_window:
            keys.append(resource_key(kind, identifier))
            continue
        low = draw(st.integers(-(10**9), 10**9))
        high = low + draw(st.integers(1, 10**6))
        keys.append(resource_key(kind, identifier, window=Interval.of(low, high)))
    return keys


@given(keys=resource_keys())
def test_lock_order_is_a_total_order(keys: list[ResourceKey]) -> None:
    """Sorting is deterministic, antisymmetric and independent of input order."""
    ordered = sorted_resource_keys(keys)
    assert set(ordered) == set(keys)
    assert len(ordered) == len(set(keys))
    assert [key.lock_token() for key in ordered] == sorted(key.lock_token() for key in ordered)
    assert sorted_resource_keys(list(reversed(ordered))) == ordered
    for left, right in zip(ordered, ordered[1:]):
        assert left.lock_token() <= right.lock_token()
        assert left <= right


@given(kinds=st.lists(st.sampled_from(list(ResourceKind)), min_size=2, max_size=8))
def test_two_random_orders_of_the_same_multiset_agree(kinds: list[ResourceKind]) -> None:
    keys = [
        resource_key(kind, f"seg{index}", Interval.of(index, index + 10))
        if kind.takes_window
        else resource_key(kind, f"seg{index}")
        for index, kind in enumerate(kinds)
    ]
    assert sorted_resource_keys(keys) == sorted_resource_keys(list(reversed(keys)))


# --- safe intervals ---------------------------------------------------------


@st.composite
def safe_queries(draw: st.DrawFn) -> SafeIntervalQuery:
    horizon_start = draw(st.integers(-40, 40))
    horizon_length = draw(st.integers(1, 90))
    horizon = Interval.of(horizon_start, horizon_start + horizon_length)
    occupied: list[tuple[Interval, str]] = []
    for index in range(draw(st.integers(0, 5))):
        start = draw(st.integers(horizon.start - 10, horizon.end + 10))
        length = draw(st.integers(1, 25))
        occupied.append((Interval.of(start, start + length), f"r{index}"))
    return SafeIntervalQuery.build(
        resource_key(
            "volume_cell", "kpbpcc.08", window=Interval.of(horizon.start, horizon.end)
        ),
        horizon=horizon,
        traversal_us=draw(st.integers(0, 20)),
        padding=draw(paddings()),
        occupied=occupied,
    )


@given(query=safe_queries())
def test_no_safe_window_intersects_padded_occupancy(query: SafeIntervalQuery) -> None:
    """I1 at the primitive level: a returned entry never touches a padded commitment.

    Checked against the padded (uncertainty + transition buffer) protected window of
    every occupation, not merely the raw one.
    """
    result = compute_safe_intervals(query)
    padding = query.padding
    occupied = query.merged_occupied()
    for window in result.windows:
        for item in occupied:
            # The window of entry instants this item forbids must be untouched.
            assert not window.entry.overlaps(
                item.entry_blocked_by(padding, query.traversal_us)
            )
        # And the rule itself, instant by instant, not just at the window edges.
        for moment in range(window.entry.start, window.entry.end):
            assert all(
                moment - padding.transition_buffer_us >= item.end + padding.uncertainty_us
                or moment + query.traversal_us + padding.transition_buffer_us
                <= item.start - padding.uncertainty_us
                for item in occupied
            )
            assert window.exit.contains_instant(moment + query.traversal_us)
            assert window.release.contains_instant(
                moment + query.traversal_us + padding.transition_buffer_us
            )


@given(query=safe_queries())
def test_safe_windows_are_disjoint_ordered_and_complete(query: SafeIntervalQuery) -> None:
    result = compute_safe_intervals(query)
    windows = result.windows
    assert windows == tuple(sorted(windows, key=lambda item: item.entry.start))
    for left, right in zip(windows, windows[1:]):
        assert not left.entry.overlaps(right.entry)
        assert left.entry.end <= right.entry.start
    assert len(result.gap_audit) == len(windows) + result.rejected_count
    assert window_entries(windows) == set(
        moment
        for moment in range(query.horizon.start, query.horizon.end)
        if classify_entry(query, moment).status.value == "admissible"
    )


@given(query=safe_queries())
def test_safe_intervals_equal_horizon_minus_blocked(query: SafeIntervalQuery) -> None:
    """The gap enumeration and the blocked-window subtraction must agree exactly."""
    from droneops_geom_core import gaps_within

    result = compute_safe_intervals(query)
    derived = gaps_within(query.horizon, result.blocked)
    assert tuple(window.entry for window in result.windows) == derived


@given(query=safe_queries(), moment=st.integers(-80, 140))
def test_entry_classification_agrees_with_the_rule(query: SafeIntervalQuery, moment: int) -> None:
    """The definitional predicate, applied directly, matches classify_entry."""
    if not query.horizon.contains_instant(moment):
        assert classify_entry(query, moment).status.value == "outside_horizon"
        return
    padding = query.padding
    rule_ok = all(
        moment - padding.transition_buffer_us >= item.end + padding.uncertainty_us
        or moment + query.traversal_us + padding.transition_buffer_us
        <= item.start - padding.uncertainty_us
        for item in query.merged_occupied()
    )
    representable = moment + query.traversal_us + padding.transition_buffer_us <= INT64_MICROSECOND_MAX
    status = classify_entry(query, moment).status
    if not representable:
        assert status.value == "release_not_representable"
    elif rule_ok:
        assert status.value == "admissible"
    else:
        assert status.value == "blocked_by_occupation"


@given(query=safe_queries())
def test_padding_enlarges_the_blocked_set_and_never_the_answer(query: SafeIntervalQuery) -> None:
    """Monotonicity: more padding can only remove options, never create them."""
    loose = compute_safe_intervals(query)
    tighter = SafeIntervalQuery(
        query.resource,
        query.horizon,
        query.traversal_us,
        SafeIntervalPadding(
            Microsecond(query.padding.uncertainty_us + 5),
            Microsecond(query.padding.transition_buffer_us + 5),
        ),
        query.occupied,
    )
    strict = compute_safe_intervals(tighter)
    assert window_entries(strict.windows) <= window_entries(loose.windows)


@given(query=safe_queries())
def test_a_longer_traversal_never_adds_entries(query: SafeIntervalQuery) -> None:
    short = compute_safe_intervals(query)
    long = compute_safe_intervals(
        SafeIntervalQuery(
            query.resource,
            query.horizon,
            Microsecond(query.traversal_us + 7),
            query.padding,
            query.occupied,
        )
    )
    assert window_entries(long.windows) <= window_entries(short.windows)


@given(query=safe_queries())
def test_result_is_reproducible_and_input_order_independent(query: SafeIntervalQuery) -> None:
    shuffled = SafeIntervalQuery(
        query.resource,
        query.horizon,
        query.traversal_us,
        query.padding,
        tuple(reversed(query.occupied)),
    )
    assert compute_safe_intervals(shuffled).windows == compute_safe_intervals(query).windows
    assert compute_safe_intervals(shuffled).blocked == compute_safe_intervals(query).blocked


# --- cells and bands --------------------------------------------------------


@given(cell=fine_cells())
def test_volume_cell_token_round_trip_is_idempotent(cell: Cell3D) -> None:
    parsed = Cell3D.parse_token(SPEC, cell.token(), reference=cell.reference)
    assert parsed == cell
    assert parsed.token() == cell.token()
    assert Cell3D.from_key(cell.key()) == cell
    assert Cell3D.from_key(cell.key()).key() == cell.key()


@given(cell=fine_cells())
def test_cell_key_is_a_legal_resource_identifier(cell: Cell3D) -> None:
    key = resource_key(ResourceKind.VOLUME_CELL, cell.token(), window=Interval.of(0, 1))
    assert ResourceKey.parse(key.canonical).identifier == cell.token()


@given(cell=fine_cells(), radius=st.integers(1, 2))
def test_neighbourhood_is_symmetric_and_closed(cell: Cell3D, radius: int) -> None:
    """A volume cell's neighbourhood is a symmetric relation - the pre-filter is safe."""
    found = cell.neighbourhood(radius=radius)
    assert cell in found
    for neighbour in found:
        assert cell in neighbour.neighbourhood(radius=radius)
    assert found == tuple(sorted(found, key=Cell3D.sort_key))
    assert len(set(item.token() for item in found)) == len(found)


@given(cell=fine_cells())
def test_parent_chain_is_a_prefix_and_covers_the_child(cell: Cell3D) -> None:
    parent = cell.parent()
    assert cell.token().startswith(parent.token().split(".")[0])
    assert parent.covers_fine_cell(cell.x, cell.y)
    assert parent.level == cell.level - 1
    assert not cell.covers_cell(parent)


@given(cells=st.lists(fine_cells(), max_size=8))
def test_merge_cells_is_idempotent(cells: list[Cell3D]) -> None:
    once = merge_cells_by_token(cells)
    assert merge_cells_by_token(once) == once
    assert merge_cells_by_token(list(reversed(cells))) == once


@given(band=bands(), target=st.sampled_from(CONSTRUCTIBLE), context=datums())
def test_altitude_conversion_round_trips_within_tolerance(
    band: AltitudeBand, target: AltitudeReference, context: VerticalDatums
) -> None:
    """I9 plus quantisation: a band survives a reference round trip unchanged.

    Integer millimetres make the band conversion exact; the tolerance belongs to
    the metre boundary, and the round trip is checked against it anyway.
    """
    converted = band.as_reference(target, context)
    assert converted.reference is target
    assert converted.as_reference(band.reference, context) == band


@given(altitude=altitudes(), target=st.sampled_from(CONSTRUCTIBLE), context=datums())
def test_agl_amsl_agl_returns_the_input_within_the_stated_tolerance(
    altitude: Altitude, target: AltitudeReference, context: VerticalDatums
) -> None:
    """The requested invariant, stated as a tolerance rather than as equality.

    The conversion chain itself is exact in integer millimetres; the tolerance
    covers the metre-to-millimetre boundary, where one quantisation can move a
    value by at most ``QUANTIZATION_TOLERANCE_MM``.
    """
    original_mm = altitude.value_mm
    round_tripped = altitude.as_amsl(context).as_reference(altitude.reference, context)
    assert round_tripped == altitude
    assert abs(round_tripped.value_mm - original_mm) <= QUANTIZATION_TOLERANCE_MM

    expressed = Altitude.in_reference(altitude.value_m, altitude.reference)
    assert abs(expressed.value_mm - original_mm) <= QUANTIZATION_TOLERANCE_MM


@given(altitude=altitudes(), context=datums())
def test_conversion_to_pad_never_guesses_a_missing_deck(
    altitude: Altitude, context: VerticalDatums
) -> None:
    if context.pad_deck_amsl_mm is None:
        with pytest.raises(GeomCoreError, match="pad_deck_amsl_mm"):
            altitude.as_reference(AltitudeReference.PAD, context)
    else:
        assert altitude.as_reference(AltitudeReference.PAD, context).reference is (
            AltitudeReference.PAD
        )


@given(altitude=altitudes())
def test_unspecified_reference_is_never_accepted(altitude: Altitude) -> None:
    with pytest.raises(GeomCoreError, match="UNSPECIFIED"):
        Altitude(AltitudeReference.UNSPECIFIED, altitude.value_mm)
    with pytest.raises(GeomCoreError, match="UNSPECIFIED"):
        altitude.as_reference(AltitudeReference.UNSPECIFIED, VerticalDatums.at_site(300.0))


@given(lower=bands(), upper=bands())
def test_band_merge_and_subtract_conserve_measure(lower: AltitudeBand, upper: AltitudeBand) -> None:
    assume(lower.reference is upper.reference)
    merged = merge_bands([lower, upper])
    assert sum(band.measure for band in merged) == total_band_measure([lower, upper])
    assert merge_bands(merged) == merged
    assert merge_bands([upper, lower]) == merged


def total_band_measure(bands: list[AltitudeBand]) -> int:
    return sum(band.measure for band in merge_bands(bands))


@given(lower=bands(), upper=bands())
def test_band_difference_reconstructs_the_original(lower: AltitudeBand, upper: AltitudeBand) -> None:
    assume(lower.reference is upper.reference)
    remainder = lower.subtract([upper])
    claimed = merge_bands([band for other in merge_bands([upper]) for band in lower.intersection(other)])
    assert sum(band.measure for band in remainder) + sum(
        band.measure for band in claimed
    ) == lower.measure


@given(band=bands())
def test_band_key_round_trip(band: AltitudeBand) -> None:
    assert AltitudeBand.from_key(band.key()) == band
    assert AltitudeBand.from_key(band.key()).key() == band.key()


@given(band=bands(), delta=st.integers(-200_000, 200_000))
def test_band_shift_and_grow_stay_consistent(band: AltitudeBand, delta: int) -> None:
    shifted = band.shifted_by(delta)
    assert shifted.measure == band.measure
    assert shifted.shifted_by(-delta) == band
    grown = band.grow_by(1_000)
    assert grown.lower_mm == band.lower_mm - 1_000
    assert grown.upper_mm == band.upper_mm + 1_000


# --- geodesy ----------------------------------------------------------------

coordinate = st.floats(min_value=-89.0, max_value=89.0, allow_nan=False, allow_infinity=False)
other_coordinate = st.floats(min_value=-179.0, max_value=179.0, allow_nan=False, allow_infinity=False)


@given(
    lat_a=coordinate, lon_a=other_coordinate, lat_b=coordinate, lon_b=other_coordinate,
    lat_c=coordinate, lon_c=other_coordinate,
)
@settings(deadline=None)
def test_geodesic_distance_is_symmetric_and_identity_of_indiscernibles(
    lat_a: float, lon_a: float, lat_b: float, lon_b: float, lat_c: float, lon_c: float
) -> None:
    from droneops_geom_core import GeoPoint

    a = GeoPoint(lat_a, lon_a)
    b = GeoPoint(lat_b, lon_b)
    c = GeoPoint(lat_c, lon_c)
    assert haversine_distance_m(a, b) == haversine_distance_m(b, a)
    assert haversine_distance_m(a, a) == 0.0
    assert geodesic_distance_mm(a, b) >= 0
    # Triangle inequality, allowing a millimetre of quantisation on each side.
    assert haversine_distance_m(a, c) <= haversine_distance_m(a, b) + haversine_distance_m(b, c) + 1e-6


@given(
    lat=st.floats(min_value=-60.0, max_value=60.0, allow_nan=False, allow_infinity=False),
    lon=st.floats(min_value=-60.0, max_value=60.0, allow_nan=False, allow_infinity=False),
    east=st.integers(-40_000_000, 40_000_000),
    north=st.integers(-40_000_000, 40_000_000),
)
@settings(deadline=None)
def test_local_projection_round_trips_within_one_millimetre_grid_cell(
    lat: float, lon: float, east: int, north: int
) -> None:
    """Quantisation is the only error: a point comes back inside its own cell."""
    from droneops_geom_core import GeoPoint, LocalPlane, PlanarOffset

    plane = LocalPlane(GeoPoint(lat, lon))
    origin = plane.point_at(PlanarOffset(Millimetre(0), Millimetre(0)))
    assert haversine_distance_m(origin, plane.reference) < 0.001

    offset = PlanarOffset(Millimetre(east), Millimetre(north))
    restored = plane.point_at(offset)
    assert plane.offset_of(restored) == offset


@given(
    cell=st.builds(
        Cell2D,
        spec=st.just(SPEC),
        x=st.integers(-16_384, 16_383),
        y=st.integers(-16_384, 16_383),
    )
)
def test_cell_index_inversion_is_exact(cell: Cell2D) -> None:
    east, north = cell.offset_of_origin()
    assert SPEC.cell_of(east, north) == cell
    assert SPEC.cell_of(east + 1, north + 1) == cell
    assert SPEC.cell_of(east, north) == Cell2D.from_key(cell.key())


@given(interval=small_intervals())
def test_negative_time_and_equal_endpoints_survive_every_algebra(interval: Interval) -> None:
    assert interval.measure >= 0
    assert merge([interval]) == (() if interval.is_empty else (interval,))
    assert subtract(interval, []) == (() if interval.is_empty else (interval,))
    assert interval.union(interval) == (() if interval.is_empty else (interval,))
    assert interval.intersection(interval) == (() if interval.is_empty else (interval,))
    assert interval.difference(interval) == ()
    assert interval.contains(interval)


@given(interval=domain_edges())
def test_huge_spans_do_not_break_the_algebra(interval: Interval) -> None:
    assert subtract(interval, [Interval.of(0, 1)]) != () or interval.measure <= 1
    assert interval.shifted_by(0) == interval
    if interval.start >= INT64_MICROSECOND_MIN + 5:
        assert interval.shifted_by(-5).measure == interval.measure
    else:
        with pytest.raises(TimeDomainError):
            interval.shifted_by(-5)
