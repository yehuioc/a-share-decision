import json

import pytest

import app.ledger as ledger_module
from app.ledger import Ledger
from app.store import ConflictError, DomainError, Store


@pytest.fixture(autouse=True)
def fixed_ledger_now(monkeypatch):
    monkeypatch.setattr(ledger_module, "now_iso", lambda: "2026-11-01T12:00:00+08:00")


def baseline(
    ledger,
    *,
    request_id="baseline-1",
    as_of="2026-09-27T18:00:00+08:00",
    cash=10000,
    positions=None,
    risk_budget=None,
):
    return ledger.set_baseline({
        "request_id": request_id,
        "confirmed": True,
        "as_of": as_of,
        "cash": cash,
        "positions": positions or [],
        "risk_budget": risk_budget,
    })


def event(ledger, request_id, as_of, event_type, **values):
    return ledger.record_event({
        "request_id": request_id,
        "as_of": as_of,
        "type": event_type,
        "status": values.pop("status", "actual"),
        **values,
    })


def valuation(ledger, request_id, as_of, prices=None):
    return ledger.record_valuation({
        "request_id": request_id,
        "as_of": as_of,
        "prices": prices or {},
    })


def test_unconfirmed_has_no_invented_account_and_actual_is_rejected(tmp_path):
    ledger = Ledger(Store(tmp_path))

    summary = ledger.summary()
    assert summary["confirmed"] is False
    assert summary["baseline"] is None
    assert summary["cash"] is None
    assert summary["net_value"] is None
    assert summary["positions"] == []
    assert summary["risk_budget"] is None
    assert summary["risk_budget_source"] is None

    with pytest.raises(DomainError, match="尚未确认"):
        event(
            ledger,
            "actual-before-baseline",
            "2026-09-28T10:00:00+08:00",
            "deposit",
            amount=7000,
        )

    paper = event(
        ledger,
        "paper-before-baseline",
        "2026-09-28T10:00:00+08:00",
        "buy",
        status="paper",
        symbol="601398",
        quantity=100,
        price=7,
        fees=None,
    )
    summary = ledger.summary()
    assert paper["status"] == "paper"
    assert summary["confirmed"] is False
    assert summary["cash"] is None
    assert summary["events"] == [paper]


def test_baseline_uses_market_price_without_counting_historical_gain(tmp_path):
    ledger = Ledger(Store(tmp_path))
    result = baseline(
        ledger,
        cash=1000,
        positions=[{"symbol": "601398", "quantity": 100, "avg_cost": 8, "price": 10}],
    )

    assert result["initial_value"] == 2000
    summary = ledger.summary(prices={"601398": 10})
    assert summary["net_value"] == 2000
    assert summary["net_profit"] == 0
    assert summary["unrealized_pnl"] == 200
    assert summary["benchmark_value"] == 2000
    assert summary["incremental_profit"] == 0
    assert summary["risk_budget_source"] == "baseline"
    assert isinstance(summary["cash"], (int, float))
    json.dumps(summary, allow_nan=False)

    # The HTTP state consumer supplies an empty current-price mapping before the
    # first market refresh; the ledger remains usable from its recorded baseline.
    without_market = ledger.summary(prices={})
    assert without_market["net_value"] == 2000
    assert without_market["valuation_source"] == "baseline"
    assert without_market["max_drawdown_amount"] is None
    assert without_market["drawdown_scope"] == "没有已记录估值点"


def test_deposit_and_withdrawal_are_flows_not_profit(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=1000)
    event(ledger, "deposit", "2026-09-28T09:00:00+08:00", "deposit", amount=500)
    event(ledger, "withdraw", "2026-09-28T09:05:00+08:00", "withdrawal", amount=200)

    summary = ledger.summary()
    assert summary["cash"] == 1300
    assert summary["net_value"] == 1300
    assert summary["net_inflows"] == 300
    assert summary["net_profit"] == 0
    assert summary["benchmark_value"] == 1300
    assert summary["incremental_profit"] == 0


def test_actual_buy_sell_reconciles_cash_cost_and_realized_profit(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=10000)
    event(
        ledger,
        "buy",
        "2026-09-28T10:00:00+08:00",
        "buy",
        symbol="601398",
        quantity=100,
        price=10,
        fees=5,
    )
    event(
        ledger,
        "sell",
        "2026-09-29T10:00:00+08:00",
        "sell",
        symbol="601398",
        quantity=100,
        price=12,
        fees=5,
    )

    summary = ledger.summary()
    assert summary["positions"] == []
    assert summary["cash"] == 10190
    assert summary["net_value"] == 10190
    assert summary["net_profit"] == 190
    assert summary["realized_pnl"] == 190
    assert summary["unrealized_pnl"] == 0
    assert summary["fees"] == 10
    assert summary["fees_complete"] is True


def test_unknown_fee_is_gross_estimate_until_unique_correction(tmp_path):
    store = Store(tmp_path)
    ledger = Ledger(store)
    baseline(ledger, cash=2000)
    trade = event(
        ledger,
        "unknown-buy",
        "2026-09-28T10:00:00+08:00",
        "buy",
        symbol="601398",
        quantity=100,
        price=10,
        fees=None,
    )

    estimated = ledger.summary(prices={"601398": 10})
    assert estimated["cash"] == 1000
    assert estimated["cash_estimated"] is True
    assert estimated["fees_complete"] is False
    assert estimated["fees"] is None
    assert estimated["net_profit"] is None
    assert estimated["realized_pnl"] is None
    assert estimated["unrealized_pnl"] is None
    assert estimated["incremental_profit"] is None

    supplement = event(
        ledger,
        "fee-supplement",
        "2026-09-29T09:00:00+08:00",
        "fee",
        amount=5,
        related_event_id=trade["id"],
    )
    exact = Ledger(Store(tmp_path)).summary(prices={"601398": 10})
    assert supplement["related_event_id"] == trade["id"]
    assert exact["cash"] == 995
    assert exact["positions"][0]["avg_cost"] == 10.05
    assert exact["net_value"] == 1995
    assert exact["net_profit"] == -5
    assert exact["fees"] == 5
    assert exact["fees_complete"] is True

    with pytest.raises(ConflictError, match="已经补录"):
        event(
            ledger,
            "fee-supplement-again",
            "2026-09-29T09:05:00+08:00",
            "fee",
            amount=5,
            related_event_id=trade["id"],
        )


def test_known_fee_cannot_be_supplemented_and_is_not_double_deducted(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=2000)
    trade = event(
        ledger,
        "known-buy",
        "2026-09-28T10:00:00+08:00",
        "buy",
        symbol="510300",
        quantity=100,
        price=4,
        fees=1,
    )
    with pytest.raises(ConflictError, match="已经明确"):
        event(
            ledger,
            "duplicate-known-fee",
            "2026-09-29T10:00:00+08:00",
            "fee",
            amount=1,
            related_event_id=trade["id"],
        )
    assert ledger.summary(prices={"510300": 4})["cash"] == 1599


def test_cash_lot_available_quantity_tail_and_t_plus_one_rules(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(
        ledger,
        cash=1000,
        positions=[{"symbol": "601398", "quantity": 150, "avg_cost": 10, "price": 10}],
    )

    with pytest.raises(DomainError, match="100 股"):
        event(
            ledger,
            "bad-lot",
            "2026-09-28T10:00:00+08:00",
            "buy",
            symbol="601398",
            quantity=50,
            price=10,
            fees=0,
        )
    with pytest.raises(DomainError, match="现金不足"):
        event(
            ledger,
            "overdraft-buy",
            "2026-09-28T10:00:00+08:00",
            "buy",
            symbol="601398",
            quantity=200,
            price=10,
            fees=0,
        )
    with pytest.raises(DomainError, match="尾数"):
        event(
            ledger,
            "partial-tail",
            "2026-09-28T10:00:00+08:00",
            "sell",
            symbol="601398",
            quantity=50,
            price=10,
            fees=0,
        )

    event(
        ledger,
        "same-day-buy",
        "2026-09-28T10:00:00+08:00",
        "buy",
        symbol="601398",
        quantity=100,
        price=5,
        fees=0,
    )
    with pytest.raises(DomainError, match="当前可卖 150"):
        event(
            ledger,
            "same-day-sell",
            "2026-09-28T14:00:00+08:00",
            "sell",
            symbol="601398",
            quantity=200,
            price=10,
            fees=0,
        )

    # All currently available shares may be sold even when the position has an odd tail.
    event(
        ledger,
        "sell-available-tail",
        "2026-09-28T14:05:00+08:00",
        "sell",
        symbol="601398",
        quantity=150,
        price=10,
        fees=0,
    )
    summary = ledger.summary(prices={"601398": 10})
    assert summary["positions"][0]["quantity"] == 100

    with pytest.raises(DomainError, match="出金"):
        event(ledger, "over-withdraw", "2026-09-28T15:00:00+08:00", "withdrawal", amount=3000)
    with pytest.raises(DomainError, match="不是已核验交易日"):
        event(
            ledger,
            "holiday-buy",
            "2026-10-01T10:00:00+08:00",
            "buy",
            symbol="601398",
            quantity=100,
            price=10,
            fees=0,
        )


def test_scoped_stock_etf_is_t_plus_one_without_generalizing_all_etfs(tmp_path):
    scoped = Ledger(Store(tmp_path / "scoped"))
    baseline(scoped, cash=2000)
    event(
        scoped,
        "stock-etf-buy",
        "2026-09-28T10:00:00+08:00",
        "buy",
        symbol="510300",
        quantity=100,
        price=4,
        fees=0,
    )
    with pytest.raises(DomainError, match="当日买入"):
        event(
            scoped,
            "stock-etf-sell",
            "2026-09-28T14:00:00+08:00",
            "sell",
            symbol="510300",
            quantity=100,
            price=4,
            fees=0,
        )

    other = Ledger(Store(tmp_path / "other"))
    baseline(other, cash=2000)
    event(
        other,
        "other-etf-buy",
        "2026-09-28T10:00:00+08:00",
        "buy",
        symbol="511010",
        quantity=100,
        price=1,
        fees=0,
    )
    event(
        other,
        "other-etf-sell",
        "2026-09-28T14:00:00+08:00",
        "sell",
        symbol="511010",
        quantity=100,
        price=1,
        fees=0,
    )
    assert other.summary()["positions"] == []


def test_paper_and_not_executed_are_visible_but_do_not_mutate_actual(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=1000)
    event(
        ledger,
        "paper-buy",
        "2026-09-28T10:00:00+08:00",
        "buy",
        status="paper",
        symbol="601398",
        quantity=100,
        price=7,
        fees=None,
    )
    event(
        ledger,
        "not-executed-sell",
        "2026-09-28T11:00:00+08:00",
        "sell",
        status="not_executed",
        symbol="601398",
        quantity=100,
        price=7.1,
        fees=None,
    )

    summary = ledger.summary()
    assert summary["cash"] == 1000
    assert summary["positions"] == []
    assert summary["net_profit"] == 0
    assert summary["fees_complete"] is True
    assert [item["status"] for item in summary["events"]] == ["paper", "not_executed"]


def test_dividend_affects_profit_and_matching_passive_benchmark(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(
        ledger,
        cash=1000,
        positions=[{"symbol": "601398", "quantity": 100, "avg_cost": 10, "price": 10}],
    )
    event(
        ledger,
        "dividend",
        "2026-09-28T09:00:00+08:00",
        "dividend",
        symbol="601398",
        amount=100,
    )

    summary = ledger.summary(prices={"601398": 11})
    assert summary["dividends"] == 100
    assert summary["cash"] == 1100
    assert summary["net_value"] == 2200
    assert summary["net_profit"] == 200
    assert summary["benchmark_value"] == 2200
    assert summary["incremental_profit"] == 0


def test_dividend_does_not_invent_passive_amount_after_position_diverges(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(
        ledger,
        cash=1000,
        positions=[{"symbol": "601398", "quantity": 200, "avg_cost": 10, "price": 10}],
    )
    event(
        ledger,
        "sell-half",
        "2026-09-28T10:00:00+08:00",
        "sell",
        symbol="601398",
        quantity=100,
        price=10,
        fees=0,
    )
    event(
        ledger,
        "dividend-after-sell",
        "2026-09-29T10:00:00+08:00",
        "dividend",
        symbol="601398",
        amount=50,
    )

    summary = ledger.summary(prices={"601398": 10})
    assert summary["net_profit"] == 50
    assert summary["benchmark_value"] is None
    assert summary["incremental_profit"] is None
    assert any("无法精确匹配被动对照分红" in warning for warning in summary["warnings"])


def test_per_share_dividend_updates_passive_after_actual_position_is_sold(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(
        ledger,
        cash=1000,
        positions=[{"symbol": "601398", "quantity": 100, "avg_cost": 10, "price": 10}],
    )
    event(
        ledger,
        "sell-before-dividend",
        "2026-09-28T10:00:00+08:00",
        "sell",
        symbol="601398",
        quantity=100,
        price=10,
        fees=0,
    )
    dividend = event(
        ledger,
        "passive-dividend-only",
        "2026-09-29T10:00:00+08:00",
        "dividend",
        symbol="601398",
        amount=0,
        dividend_per_share=0.5,
    )

    summary = Ledger(Store(tmp_path)).summary(prices={"601398": 10})
    assert dividend["amount"] == 0
    assert dividend["dividend_per_share"] == 0.5
    assert summary["dividends"] == 0
    assert summary["net_value"] == 2000
    assert summary["net_profit"] == 0
    assert summary["benchmark_value"] == 2050
    assert summary["incremental_profit"] == -50
    assert not any("无法精确匹配被动对照分红" in warning for warning in summary["warnings"])


def test_zero_dividend_requires_positive_per_share_and_field_is_dividend_only(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=1000)
    with pytest.raises(DomainError, match="amount 必须大于零"):
        event(
            ledger,
            "zero-without-rate",
            "2026-09-28T10:00:00+08:00",
            "dividend",
            symbol="601398",
            amount=0,
        )
    with pytest.raises(DomainError, match="dividend_per_share 必须大于零"):
        event(
            ledger,
            "zero-rate",
            "2026-09-28T10:00:00+08:00",
            "dividend",
            symbol="601398",
            amount=0,
            dividend_per_share=0,
        )
    with pytest.raises(DomainError, match="不能填写 dividend_per_share"):
        event(
            ledger,
            "deposit-with-rate",
            "2026-09-28T10:00:00+08:00",
            "deposit",
            amount=100,
            dividend_per_share=1,
        )
    with pytest.raises(DomainError, match="不能填写 dividend_per_share"):
        event(
            ledger,
            "buy-with-rate",
            "2026-09-28T10:00:00+08:00",
            "buy",
            symbol="601398",
            quantity=100,
            price=1,
            fees=0,
            dividend_per_share=1,
        )


def test_settings_risk_budget_overrides_only_with_explicit_update_time(tmp_path):
    store = Store(tmp_path)
    ledger = Ledger(store)
    baseline(ledger, cash=1000, risk_budget=300)

    store.write("settings.json", {"risk_budget": 500})
    original = ledger.summary()
    assert original["risk_budget"] == 300
    assert original["risk_budget_source"] == "baseline"
    assert original["risk_budget_updated_at"] is None

    store.write(
        "settings.json",
        {"risk_budget": 500, "risk_budget_updated_at": "2026-09-29T09:00:00+08:00"},
        overwrite=True,
    )
    updated = Ledger(Store(tmp_path)).summary()
    assert updated["risk_budget"] == 500
    assert updated["risk_budget_source"] == "settings"
    assert updated["risk_budget_updated_at"] == "2026-09-29T09:00:00+08:00"

    store.write(
        "settings.json",
        {"risk_budget": None, "risk_budget_updated_at": "2026-09-30T09:00:00+08:00"},
        overwrite=True,
    )
    cleared = ledger.summary()
    assert cleared["risk_budget"] is None
    assert cleared["risk_budget_source"] == "settings"


def test_explicit_settings_risk_budget_is_visible_before_account_confirmation(tmp_path):
    store = Store(tmp_path)
    store.write(
        "settings.json",
        {"risk_budget": 200, "risk_budget_updated_at": "2026-09-29T09:00:00+08:00"},
    )
    summary = Ledger(store).summary()
    assert summary["confirmed"] is False
    assert summary["risk_budget"] == 200
    assert summary["risk_budget_source"] == "settings"
    assert summary["risk_budget_updated_at"] == "2026-09-29T09:00:00+08:00"


def test_settings_risk_budget_update_time_must_include_timezone(tmp_path):
    store = Store(tmp_path)
    ledger = Ledger(store)
    baseline(ledger, cash=1000, risk_budget=300)
    store.write(
        "settings.json",
        {"risk_budget": 500, "risk_budget_updated_at": "2026-09-29T09:00:00"},
    )
    with pytest.raises(DomainError, match="时间缺少时区"):
        ledger.summary()


def test_historical_summary_does_not_load_later_settings_risk_budget(tmp_path):
    store = Store(tmp_path)
    ledger = Ledger(store)
    baseline(ledger, cash=1000, risk_budget=300, as_of="2026-09-01T09:00:00+08:00")
    store.write(
        "settings.json",
        {"risk_budget": 500, "risk_budget_updated_at": "2026-10-01T09:00:00+08:00"},
    )

    historical = ledger.summary(as_of="2026-09-30T15:00:00+08:00")
    assert historical["risk_budget"] is None
    assert historical["risk_budget_source"] == "unknown"
    assert historical["risk_budget_updated_at"] == "2026-10-01T09:00:00+08:00"
    assert any("无法还原当时有效风险预算" in warning for warning in historical["warnings"])

    current = ledger.summary()
    assert current["risk_budget"] == 500
    assert current["risk_budget_source"] == "settings"


def test_month_boundaries_flows_and_drawdown_use_recorded_points(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=1000, as_of="2026-09-01T09:00:00+08:00")
    valuation(ledger, "sep-open-early", "2026-09-01T09:31:00+08:00")
    valuation(ledger, "sep-open-late", "2026-09-01T15:00:00+08:00")
    event(ledger, "sep-deposit", "2026-09-15T10:00:00+08:00", "deposit", amount=500)
    valuation(ledger, "sep-after-deposit", "2026-09-15T15:00:00+08:00")
    event(ledger, "sep-fee", "2026-09-20T10:00:00+08:00", "fee", amount=100)
    valuation(ledger, "sep-close", "2026-09-30T15:00:00+08:00")

    valuation(ledger, "oct-open", "2026-10-08T09:31:00+08:00")
    event(ledger, "oct-deposit", "2026-10-15T10:00:00+08:00", "deposit", amount=200)
    event(ledger, "oct-fee", "2026-10-20T10:00:00+08:00", "fee", amount=50)
    valuation(ledger, "oct-close", "2026-10-30T15:00:00+08:00")

    summary = ledger.summary()
    assert summary["cash"] == 1550
    assert summary["net_inflows"] == 700
    assert summary["net_profit"] == -150
    assert summary["max_drawdown_amount"] == 150
    assert summary["drawdown_scope"] == "从基线初始净值开始，仅覆盖 6 个已记录估值点"
    assert [m["month"] for m in summary["complete_months"]] == ["2026-09", "2026-10"]
    assert [m["net_profit"] for m in summary["complete_months"]] == [-100, -50]
    assert summary["monthly_average"] == -75


def test_incomplete_month_and_single_valuation_do_not_invent_monthly_average(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=1000, as_of="2026-09-15T09:00:00+08:00")
    valuation(ledger, "only-close", "2026-09-30T15:00:00+08:00")

    summary = ledger.summary()
    assert summary["monthly_average"] is None
    assert summary["complete_months"] == []
    assert summary["max_drawdown_amount"] == 0
    assert any("完整月边界" in warning for warning in summary["warnings"])


def test_monthly_requires_closed_calendar_month_and_closing_valuation(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=1000, as_of='2026-09-01T09:00:00+08:00')
    valuation(ledger, 'sep-intraday', '2026-09-30T10:00:00+08:00')
    assert ledger.summary()['monthly_average'] is None
    valuation(ledger, 'sep-close', '2026-09-30T15:00:00+08:00')
    assert ledger.summary(as_of='2026-09-30T15:01:00+08:00')['monthly_average'] is None
    assert ledger.summary(as_of='2026-10-01T00:00:00+08:00')['monthly_average'] == 0


def test_first_day_close_does_not_make_complete_month(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=1000, as_of='2026-09-01T15:00:00+08:00')
    valuation(ledger, 'open-missing', '2026-09-01T15:00:00+08:00')
    valuation(ledger, 'end', '2026-09-30T15:00:00+08:00')
    assert ledger.summary()['monthly_average'] is None


def test_future_paper_record_does_not_shift_account_time(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=1000)
    event(ledger,'future-paper','2026-11-03T10:00:00+08:00','buy',status='paper',symbol='601398',quantity=100,price=8,fees=None)
    result = ledger.summary()
    assert result['as_of'].startswith('2026-11-01')
    assert result['cash'] == 1000
    assert result['events'][0]['status'] == 'paper'


def test_first_recorded_loss_draws_down_from_baseline_peak(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=1000, as_of="2026-09-01T09:00:00+08:00")
    event(ledger, "loss-before-first-point", "2026-09-02T10:00:00+08:00", "fee", amount=100)
    valuation(ledger, "first-loss-point", "2026-09-02T15:00:00+08:00")

    summary = ledger.summary()
    assert summary["max_drawdown_amount"] == 100
    assert summary["drawdown_scope"] == "从基线初始净值开始，仅覆盖 1 个已记录估值点"
    assert any("从基线初始净值起算" in warning for warning in summary["warnings"])


def test_missing_valuation_uses_latest_trade_price(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=5000)
    event(
        ledger,
        "first-buy-price",
        "2026-09-28T10:00:00+08:00",
        "buy",
        symbol="601398",
        quantity=100,
        price=10,
        fees=0,
    )
    event(
        ledger,
        "latest-buy-price",
        "2026-09-29T10:00:00+08:00",
        "buy",
        symbol="601398",
        quantity=100,
        price=12,
        fees=0,
    )

    summary = ledger.summary()
    assert summary["positions"][0]["price"] == 12
    assert summary["net_value"] == 5200
    assert summary["net_profit"] == 200


def test_request_id_idempotency_conflicts_and_restart_recalculation(tmp_path):
    first = Ledger(Store(tmp_path))
    baseline_payload = {
        "request_id": "base-idempotent",
        "confirmed": True,
        "as_of": "2026-09-27T18:00:00+08:00",
        "cash": 1000,
        "positions": [],
        "risk_budget": None,
    }
    assert first.set_baseline(baseline_payload) == first.set_baseline(dict(baseline_payload))
    changed_baseline = dict(baseline_payload, cash=1001)
    with pytest.raises(ConflictError, match="相同 request_id"):
        first.set_baseline(changed_baseline)

    deposit_payload = {
        "request_id": "event-idempotent",
        "as_of": "2026-09-28T10:00:00+08:00",
        "type": "deposit",
        "status": "actual",
        "amount": 250,
    }
    recorded = first.record_event(deposit_payload)
    assert first.record_event(dict(deposit_payload)) == recorded
    with pytest.raises(ConflictError, match="相同 request_id"):
        first.record_event(dict(deposit_payload, amount=251))

    valuation_payload = {
        "request_id": "valuation-idempotent",
        "as_of": "2026-09-30T15:00:00+08:00",
        "prices": {},
    }
    point = first.record_valuation(valuation_payload)
    assert first.record_valuation(dict(valuation_payload)) == point
    with pytest.raises(ConflictError, match="相同 request_id"):
        first.record_valuation(dict(valuation_payload, as_of="2026-09-30T14:59:00+08:00"))

    before = first.summary()
    after = Ledger(Store(tmp_path)).summary()
    assert after == before
    assert after["cash"] == 1250
    assert after["net_profit"] == 0


def test_valuation_requires_all_actual_and_passive_symbols(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(
        ledger,
        cash=1000,
        positions=[{"symbol": "601398", "quantity": 100, "avg_cost": 10, "price": 10}],
    )
    event(
        ledger,
        "sell-all",
        "2026-09-28T10:00:00+08:00",
        "sell",
        symbol="601398",
        quantity=100,
        price=10,
        fees=0,
    )
    with pytest.raises(DomainError, match="被动对照价格"):
        valuation(ledger, "missing-passive", "2026-09-28T15:00:00+08:00", {})
    valuation(ledger, "complete-passive", "2026-09-28T15:00:00+08:00", {"601398": 10})


def test_real_records_reject_future_but_hypotheses_may_be_future(tmp_path):
    with pytest.raises(DomainError, match="账户基线时间不能晚于当前时间"):
        baseline(
            Ledger(Store(tmp_path / "future-baseline")),
            as_of="2026-11-02T09:00:00+08:00",
        )

    ledger = Ledger(Store(tmp_path / "events"))
    baseline(ledger, as_of="2026-10-30T15:00:00+08:00")
    with pytest.raises(DomainError, match="实际事件时间不能晚于当前时间"):
        event(
            ledger,
            "future-actual",
            "2026-11-02T10:00:00+08:00",
            "deposit",
            amount=100,
        )
    paper = event(
        ledger,
        "future-paper",
        "2027-01-04T10:00:00+08:00",
        "buy",
        status="paper",
        symbol="601398",
        quantity=100,
        price=10,
        fees=0,
    )
    skipped = event(
        ledger,
        "future-not-executed",
        "2027-01-04T10:05:00+08:00",
        "sell",
        status="not_executed",
        symbol="601398",
        quantity=100,
        price=10,
        fees=0,
    )
    assert paper["status"] == "paper"
    assert skipped["status"] == "not_executed"

    with pytest.raises(DomainError, match="估值时间不能晚于当前时间"):
        valuation(ledger, "future-valuation", "2026-11-02T15:00:00+08:00")


def test_validation_rejects_unsafe_numbers_unknown_year_and_conflicting_fields(tmp_path, monkeypatch):
    ledger = Ledger(Store(tmp_path))
    with pytest.raises(DomainError, match="明确确认"):
        ledger.set_baseline({
            "request_id": "not-confirmed",
            "confirmed": False,
            "as_of": "2026-09-27T18:00:00+08:00",
            "cash": 0,
            "positions": [],
            "risk_budget": None,
        })
    baseline(ledger, cash=1000)
    with pytest.raises(DomainError, match="有限数字"):
        event(ledger, "nan-deposit", "2026-09-28T10:00:00+08:00", "deposit", amount=float("nan"))
    monkeypatch.setattr(ledger_module, "now_iso", lambda: "2028-01-01T12:00:00+08:00")
    with pytest.raises(DomainError, match="尚未核验"):
        event(
            ledger,
            "unknown-year",
            "2027-01-04T10:00:00+08:00",
            "buy",
            symbol="601398",
            quantity=100,
            price=1,
            fees=0,
        )
    with pytest.raises(DomainError, match="quantity × price"):
        event(
            ledger,
            "wrong-gross",
            "2026-09-28T10:00:00+08:00",
            "buy",
            symbol="601398",
            quantity=100,
            price=1,
            amount=99,
            fees=0,
        )


@pytest.mark.parametrize("trade_type", ["buy", "sell"])
def test_zero_actual_fee_can_complete_an_unknown_trade_once(tmp_path, trade_type):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=2000, positions=[
        {"symbol": "601398", "quantity": 100, "avg_cost": 10, "price": 10},
    ])
    trade = event(ledger, "unknown", "2026-09-28T10:00:00+08:00", trade_type,
                  symbol="601398", quantity=100, price=10, fees=None)
    assert ledger.summary()["net_profit"] is None
    payload = dict(request_id="confirmed-zero", as_of="2026-09-29T09:00:00+08:00",
                   type="fee", status="actual", amount=0, related_event_id=trade["id"])
    receipt = ledger.record_event(payload)
    assert ledger.record_event(payload) == receipt
    exact = Ledger(Store(tmp_path)).summary({"601398": 10})
    assert exact["fees_complete"] is True
    assert exact["net_profit"] == 0
    assert exact["fees"] == 0
    assert exact["cash"] == (1000 if trade_type == "buy" else 3000)
    with pytest.raises(ConflictError):
        ledger.record_event({**payload, "request_id": "second-zero"})
    with pytest.raises(ConflictError):
        ledger.record_event({**payload, "amount": 1})


def test_later_unknown_fee_does_not_erase_completed_earlier_month(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=3000, as_of="2026-09-01T09:00:00+08:00")
    event(ledger, "sep-cost", "2026-09-15T10:00:00+08:00", "fee", amount=10)
    valuation(ledger, "sep-close", "2026-09-30T15:00:00+08:00")
    event(ledger, "oct-unknown", "2026-10-08T10:00:00+08:00", "buy",
          symbol="601398", quantity=100, price=10, fees=None)
    valuation(ledger, "oct-close", "2026-10-30T15:00:00+08:00", {"601398": 10})
    summary = ledger.summary()
    assert summary["net_profit"] is None
    assert summary["fees_complete"] is False
    assert [m["net_profit"] for m in summary["complete_months"]] == [-10, None]
    assert summary["monthly_average"] == -10
    assert any("2026-10" in w and "费用" in w for w in summary["warnings"])


def test_missing_month_is_explained_when_other_month_is_usable(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=1000, as_of="2026-09-01T09:00:00+08:00")
    valuation(ledger, "sep-close", "2026-09-30T15:00:00+08:00")
    summary = ledger.summary()
    assert summary["monthly_average"] == 0
    assert [m["month"] for m in summary["complete_months"]] == ["2026-09"]
    assert any("2026-10" in w and "边界" in w for w in summary["warnings"])


def test_backfilled_trade_with_missing_historical_price_keeps_summary_usable(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=2000, as_of="2026-09-01T09:00:00+08:00")
    valuation(ledger, "old-cash-only", "2026-09-30T15:00:00+08:00")
    event(ledger, "backfilled-buy", "2026-09-28T10:00:00+08:00", "buy",
          symbol="601398", quantity=100, price=10, fees=1)
    summary = ledger.summary({"601398": 12})
    assert summary["cash"] == 999
    assert summary["net_profit"] == 199
    assert summary["monthly_average"] is None
    assert summary["max_drawdown_amount"] is None
    assert any("601398" in w and "估值" in w for w in summary["warnings"])
    assert Ledger(Store(tmp_path)).summary({"601398": 12}) == summary


def test_same_instant_conflicting_valuation_cannot_be_hidden_by_new_request_id(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=1000, positions=[
        {"symbol": "601398", "quantity": 100, "avg_cost": 10, "price": 10},
    ])
    first = valuation(ledger, "first-close", "2026-09-30T15:00:00+08:00", {"601398": 10})
    with pytest.raises(ConflictError):
        valuation(ledger, "different-close", "2026-09-30T07:00:00Z", {"601398": 12})
    assert ledger.summary()["valuations"] == [first]


def test_combined_net_value_formula_ignores_capital_and_old_unrealized_gain(tmp_path):
    ledger = Ledger(Store(tmp_path))
    baseline(ledger, cash=1000, as_of="2026-09-01T09:00:00+08:00", positions=[
        {"symbol": "601398", "quantity": 200, "avg_cost": 8, "price": 10},
    ])
    event(ledger, "in", "2026-09-02T09:00:00+08:00", "deposit", amount=500)
    event(ledger, "out", "2026-09-02T09:05:00+08:00", "withdrawal", amount=200)
    event(ledger, "sell", "2026-09-03T10:00:00+08:00", "sell",
          symbol="601398", quantity=100, price=11, fees=2)
    event(ledger, "buy", "2026-09-04T10:00:00+08:00", "buy",
          symbol="601398", quantity=100, price=9, fees=1)
    event(ledger, "div", "2026-09-07T09:00:00+08:00", "dividend",
          symbol="601398", amount=40, dividend_per_share=0.2)
    event(ledger, "expense", "2026-09-08T09:00:00+08:00", "fee", amount=3)
    for status in ("paper", "not_executed"):
        event(ledger, status, "2027-01-04T10:00:00+08:00", "deposit", status=status, amount=90000)
    valuation(ledger, "month-end", "2026-09-30T15:00:00+08:00", {"601398": 12})
    result = ledger.summary()
    assert result["as_of"].startswith("2026-11-01")
    assert result["cash"] == 1534
    assert result["initial_value"] == 3000
    assert result["net_inflows"] == 300
    assert result["net_value"] == 3934
    assert result["net_profit"] == 634
    assert result["realized_pnl"] == 298
    assert result["unrealized_pnl"] == 699
    assert result["fees"] == 6
    assert result["dividends"] == 40
    assert result["benchmark_value"] == 3740
    assert result["incremental_profit"] == 194
    assert result["monthly_average"] == 634
    assert result["max_drawdown_amount"] == 0
    # Cost-basis P&L contains the initial 400 gain, unlike period net profit.
    assert result["realized_pnl"] + result["unrealized_pnl"] + 40 - 3 - 400 == 634
    assert Ledger(Store(tmp_path)).summary() == result
