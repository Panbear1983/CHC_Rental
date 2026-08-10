from chc_rental.scheduler import ProfileCandidates, plan_daily_allocation


def candidates(profile_id, n, prefix=None):
    prefix = prefix or f"p{profile_id}"
    return [f"{prefix}-listing-{i}" for i in range(n)]


def test_equal_round_robin_interleaves_every_profile_before_a_second_allocation():
    profiles = [
        ProfileCandidates(profile_id=1, daily_cap=10, candidates=candidates(1, 3)),
        ProfileCandidates(profile_id=2, daily_cap=10, candidates=candidates(2, 3)),
        ProfileCandidates(profile_id=3, daily_cap=10, candidates=candidates(3, 3)),
    ]
    allocation = plan_daily_allocation(profiles, global_daily_budget=9)
    assert allocation[1] == candidates(1, 3)
    assert allocation[2] == candidates(2, 3)
    assert allocation[3] == candidates(3, 3)


def test_partial_budget_gives_every_profile_one_before_any_profile_gets_two():
    profiles = [
        ProfileCandidates(profile_id=1, daily_cap=10, candidates=candidates(1, 5)),
        ProfileCandidates(profile_id=2, daily_cap=10, candidates=candidates(2, 5)),
        ProfileCandidates(profile_id=3, daily_cap=10, candidates=candidates(3, 5)),
    ]
    allocation = plan_daily_allocation(profiles, global_daily_budget=4)
    assert len(allocation[1]) == 2
    assert len(allocation[2]) == 1
    assert len(allocation[3]) == 1
    assert allocation[1] == candidates(1, 5)[:2]
    assert allocation[2] == candidates(2, 5)[:1]
    assert allocation[3] == candidates(3, 5)[:1]


def test_profile_daily_cap_is_never_exceeded():
    profiles = [
        ProfileCandidates(profile_id=1, daily_cap=1, candidates=candidates(1, 5)),
        ProfileCandidates(profile_id=2, daily_cap=10, candidates=candidates(2, 5)),
    ]
    allocation = plan_daily_allocation(profiles, global_daily_budget=100)
    assert len(allocation[1]) == 1
    assert len(allocation[2]) == 5


def test_global_daily_budget_is_never_exceeded():
    profiles = [
        ProfileCandidates(profile_id=1, daily_cap=10, candidates=candidates(1, 5)),
        ProfileCandidates(profile_id=2, daily_cap=10, candidates=candidates(2, 5)),
    ]
    allocation = plan_daily_allocation(profiles, global_daily_budget=3)
    total = sum(len(v) for v in allocation.values())
    assert total == 3


def test_zero_global_budget_allocates_nothing_but_lists_every_profile():
    profiles = [
        ProfileCandidates(profile_id=1, daily_cap=10, candidates=candidates(1, 5)),
        ProfileCandidates(profile_id=2, daily_cap=10, candidates=candidates(2, 5)),
    ]
    allocation = plan_daily_allocation(profiles, global_daily_budget=0)
    assert allocation == {1: [], 2: []}


def test_profile_with_fewer_candidates_than_others_does_not_block_the_round():
    profiles = [
        ProfileCandidates(profile_id=1, daily_cap=10, candidates=candidates(1, 1)),
        ProfileCandidates(profile_id=2, daily_cap=10, candidates=candidates(2, 5)),
    ]
    allocation = plan_daily_allocation(profiles, global_daily_budget=100)
    assert allocation[1] == candidates(1, 1)
    assert allocation[2] == candidates(2, 5)


def test_same_input_yields_same_output():
    profiles = [
        ProfileCandidates(profile_id=1, daily_cap=2, candidates=candidates(1, 3)),
        ProfileCandidates(profile_id=2, daily_cap=2, candidates=candidates(2, 3)),
    ]
    first = plan_daily_allocation(profiles, global_daily_budget=3)
    second = plan_daily_allocation(profiles, global_daily_budget=3)
    assert first == second


def test_no_active_profiles_returns_empty_allocation():
    assert plan_daily_allocation([], global_daily_budget=10) == {}
