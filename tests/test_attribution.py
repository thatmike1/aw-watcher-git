"""tests for the pure attribution engine - simulated ticks, no I/O."""

from aw_watcher_git.attribution import AttributionEngine

TICK = 10.0


def run_ticks(engine, ticks):
    """feed a list of (fs_repos, window_repo, agent_repos, suppress) ticks, return attributions."""
    results = []
    now = 1000.0
    for fs_repos, window_repo, agent_repos, suppress in ticks:
        results.append(
            engine.decide(
                now=now,
                fs_repos=set(fs_repos),
                window_repo=window_repo,
                agent_repos=set(agent_repos),
                suppress_inferred=suppress,
            )
        )
        now += TICK
    return results


def test_single_repo_fs_activity():
    engine = AttributionEngine()
    results = run_ticks(engine, [({"alpha"}, None, set(), False)] * 3)
    assert all(r is not None and r.repo == "alpha" for r in results)
    assert results[0].reason == "fs"


def test_no_signal_no_attribution():
    engine = AttributionEngine()
    results = run_ticks(engine, [(set(), None, set(), False)] * 3)
    assert results == [None, None, None]


def test_interleaved_fs_does_not_flip_flop():
    """two repos with fs activity every tick: window focus decides, stably."""
    engine = AttributionEngine()
    ticks = [({"work", "personal"}, "personal", set(), False)] * 10
    results = run_ticks(engine, ticks)
    assert all(r.repo == "personal" for r in results)


def test_tie_without_window_sticks_to_current():
    """concurrent fs in two repos and no window signal must not alternate."""
    engine = AttributionEngine()
    # establish current on alpha
    first = run_ticks(engine, [({"alpha"}, None, set(), False)])
    assert first[0].repo == "alpha"
    results = run_ticks(engine, [({"alpha", "beta"}, None, set(), False)] * 10)
    assert all(r.repo == "alpha" for r in results)


def test_window_focus_switches_attribution():
    """focus moving to a repo that stays busy wins, once a commit window closes."""
    engine = AttributionEngine()
    run_ticks(engine, [({"alpha"}, "alpha", set(), False)] * 3)
    results = run_ticks(engine, [({"alpha", "beta"}, "beta", set(), False)] * 15)
    assert results[-1].repo == "beta"


def test_window_focus_switches_immediately_without_commit_window():
    engine = AttributionEngine(commit_seconds=0.0)
    run_ticks(engine, [({"alpha"}, "alpha", set(), False)] * 3)
    results = run_ticks(engine, [({"alpha", "beta"}, "beta", set(), False)] * 3)
    assert all(r.repo == "beta" for r in results)


def test_agent_only_signal_attributes():
    """a cpu-active agent session with no edits and no matching window still counts."""
    engine = AttributionEngine()
    results = run_ticks(engine, [(set(), None, {"alpha"}, False)] * 3)
    assert all(r is not None and r.repo == "alpha" for r in results)
    assert results[0].reason == "agent"


def test_suppress_inferred_allows_only_this_tick_fs():
    """while idle/on-call, window and agent signals are ignored; real saves count."""
    engine = AttributionEngine()
    results = run_ticks(
        engine,
        [
            (set(), "alpha", {"beta"}, True),  # inferred only -> nothing
            ({"gamma"}, "alpha", {"beta"}, True),  # real save -> gamma
        ],
    )
    assert results[0] is None
    assert results[1].repo == "gamma"
    assert results[1].reason == "fs"


def test_fs_signal_lingers_within_window():
    """a save keeps its repo attributable for fs_signal_window seconds."""
    engine = AttributionEngine(fs_signal_window=60.0, tail_seconds=0.0)
    results = run_ticks(
        engine,
        [({"alpha"}, None, set(), False)] + [(set(), None, set(), False)] * 5,
    )
    # save at t=0; lingering fs keeps attributing through t=60
    assert [r.repo if r else None for r in results] == [
        "alpha", "alpha", "alpha", "alpha", "alpha", "alpha",
    ]


def test_tail_is_bounded_and_does_not_self_sustain():
    engine = AttributionEngine(fs_signal_window=10.0, tail_seconds=30.0)
    ticks = [({"alpha"}, None, set(), False)] + [(set(), None, set(), False)] * 6
    results = run_ticks(engine, ticks)
    repos = [r.repo if r else None for r in results]
    # direct fs at t=0, lingering fs at t=10, tail until t=30, then quiet
    assert repos[0] == "alpha"
    assert repos[1] == "alpha"
    assert "tail" in [r.reason for r in results if r]
    assert repos[-1] is None
    assert repos[-2] is None


def test_tail_suppressed_while_idle():
    engine = AttributionEngine(fs_signal_window=10.0, tail_seconds=300.0)
    run_ticks(engine, [({"alpha"}, None, set(), False)])
    results = run_ticks(engine, [(set(), None, set(), True)] * 3)
    assert results == [None, None, None]


def test_window_alone_attributes_after_current_cleared():
    """window focus on a watched repo counts even with no edits at all."""
    engine = AttributionEngine()
    results = run_ticks(engine, [(set(), "alpha", set(), False)] * 3)
    assert all(r.repo == "alpha" for r in results)
    assert results[0].reason == "window"


def test_stronger_combined_signal_beats_lone_fs():
    """user typing+focused in one repo beats an agent saving in another."""
    engine = AttributionEngine()
    ticks = [({"agent-repo", "user-repo"}, "user-repo", set(), False)] * 5
    results = run_ticks(engine, ticks)
    assert all(r.repo == "user-repo" for r in results)


def test_commit_window_absorbs_flapping():
    """focus bouncing between two repos every tick must not shred attribution."""
    engine = AttributionEngine(commit_seconds=60.0)
    ticks = [
        ({"alpha", "beta"}, "alpha" if i % 2 == 0 else "beta", set(), False)
        for i in range(12)
    ]
    results = run_ticks(engine, ticks)
    repos = [r.repo for r in results]
    switches = sum(1 for a, b in zip(repos, repos[1:]) if a != b)
    # 120s of ticks, so at most one boundary can move it - per-tick deciding
    # would produce a switch on nearly every tick instead
    assert switches <= 1


def test_commit_window_releases_a_repo_that_goes_silent():
    """a departure (old repo gone for good) is released without waiting for the boundary."""
    engine = AttributionEngine(commit_seconds=60.0, fs_signal_window=0.0)
    run_ticks(engine, [({"alpha"}, "alpha", set(), False)])
    results = run_ticks(engine, [({"beta"}, "beta", set(), False)] * 6)
    repos = [r.repo for r in results]
    # held while alpha might still come back, released well before t=1060
    assert repos[0] == "alpha"
    assert repos[-1] == "beta"
    assert repos.index("beta") <= 4


def test_hold_does_not_extend_the_tail():
    """time billed by a hold must not push out the silent repo's tail budget."""
    engine = AttributionEngine(commit_seconds=60.0, fs_signal_window=0.0, tail_seconds=30.0)
    # alpha wins the window, then beta is the only thing signalling for 3 ticks
    ticks = [({"alpha"}, "alpha", set(), False)] * 2 + [({"beta"}, None, set(), False)] * 3
    run_ticks(engine, ticks)
    # alpha's tail is measured from its own last real signal, not from the holds
    assert engine._last_direct["alpha"] == 1010.0


def test_commit_window_bypassed_while_suppressed():
    """while idle, only the repo actually being written to earns the tick."""
    engine = AttributionEngine(commit_seconds=60.0)
    run_ticks(engine, [({"alpha"}, "alpha", set(), False)] * 2)
    results = run_ticks(engine, [({"beta"}, None, set(), True)] * 2)
    assert [r.repo for r in results] == ["beta", "beta"]
