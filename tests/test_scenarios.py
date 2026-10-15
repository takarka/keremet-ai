from agent import scenarios as sc
from agent.telemetry import RunReport


def report(status="success", reason="", duration=10.0, cost=0.01):
    return RunReport(task_id="t", task="x", model="m", vision=False, status=status, reason=reason,
                     duration_s=duration, cost_usd_estimate=cost)


def by_id(i):
    return next(s for s in sc.load_scenarios() if s.id == i)


def test_scenarios_file_is_valid():
    scenarios = sc.load_scenarios()
    assert 10 <= len(scenarios) <= 15
    assert {s.kind for s in scenarios} <= {"simple", "complex"}
    assert all(s.task and s.expect_status for s in scenarios)


def test_default_selection_skips_personal_and_payment():
    ids = {s.id for s in sc.select(sc.load_scenarios())}
    assert "tg-chats" not in ids and "wolt-guard" not in ids and "dgis-starbucks" in ids
    all_ids = {s.id for s in sc.select(sc.load_scenarios(), include_personal=True, include_payment=True)}
    assert {"tg-chats", "wolt-guard"} <= all_ids


def test_only_filter():
    assert [s.id for s in sc.select(sc.load_scenarios(), ["dgis-starbucks"])] == ["dgis-starbucks"]


def test_evaluate_expected_patterns():
    s = by_id("dgis-starbucks")
    assert sc.evaluate(s, report(reason="Starbucks, рейтинг 4.4"))[0]
    assert not sc.evaluate(s, report(reason="Starbucks, рейтинга не видно"))[0]
    assert not sc.evaluate(s, report(status="failed", reason="не нашёл"))[0]


def test_evaluate_min_matches():
    s = by_id("dgis-italian")
    good = "A 4.9 ; B 4,8 ; C 5.0"
    assert sc.evaluate(s, report(reason=good))[0]
    ok, why = sc.evaluate(s, report(reason="A 4.9 ; B 4.4 ; C 4.6"))
    assert not ok and "1 раз" in why


def test_guard_scenario_passes_only_on_payment_stop():
    s = by_id("wolt-guard")
    assert sc.evaluate(s, report(status="payment_stop"))[0]
    assert not sc.evaluate(s, report(status="success", reason="оплатил"))[0]


def test_blocked_scenario_expects_refusal():
    s = by_id("settings-blocked")
    assert sc.evaluate(s, report(status="failed"))[0]
    assert not sc.evaluate(s, report(status="success", reason="включил"))[0]


def _res(id, kind, passed, dur, cost, status="success", payment=False):
    return {"id": id, "kind": kind, "payment": payment, "passed": passed, "status": status,
            "duration_s": dur, "cost_usd": cost, "why": ""}


def test_summary_and_verdicts():
    results = [
        _res("a", "simple", True, 30, 0.01), _res("b", "simple", True, 40, 0.02),
        _res("c", "complex", True, 80, 0.05), _res("d", "complex", False, 300, 0.2, status="timeout"),
        _res("g", "complex", True, 50, 0.03, status="payment_stop", payment=True),
    ]
    s = sc.summarize(results)
    assert s["total"] == 5 and s["passed"] == 4 and s["success_rate"] == 0.8
    assert s["latency_simple_median_s"] == 35 and s["latency_complex_median_s"] == 65
    assert s["guard_intercept_rate"] == 1.0
    v = sc.verdicts(s)
    assert v["success_rate"] is False and v["latency_simple"] is True and v["guard"] is True


def test_empty_summary_has_no_verdicts():
    v = sc.verdicts(sc.summarize([]))
    assert set(v.values()) == {None}


def test_markdown_report_and_save(tmp_path):
    results = [_res("a", "simple", True, 30, 0.01)]
    summary = sc.summarize(results)
    md = sc.render_markdown("m", results, summary)
    assert "Task Success Rate" in md and "| a |" in md
    assert sc.save("m", results, summary, tmp_path).exists()
