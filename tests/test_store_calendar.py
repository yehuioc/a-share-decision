from pathlib import Path
import pytest
from app.store import Store, ConflictError, DomainError, parse_time
from app.calendar import next_trading_day, expected_market_date, is_trading_day


def test_atomic_no_overwrite_and_paths(tmp_path):
    s = Store(tmp_path)
    with s.transaction():
        s.write('records/a.json', {'x': 1})
        s.write('records/a.json', {'x': 1})
    with pytest.raises(ConflictError):
        s.write('records/a.json', {'x': 2})
    with pytest.raises(DomainError):
        s.read('../outside.json')
    assert Store(tmp_path).list('records') == [{'x': 1}]


def test_holidays_and_timestamp():
    assert next_trading_day('2026-09-24').isoformat() == '2026-09-28'
    assert next_trading_day('2026-09-24', 5).isoformat() == '2026-10-09'
    assert expected_market_date('2026-09-27T18:00:00+08:00').isoformat() == '2026-09-24'
    assert expected_market_date('2026-09-28T08:00:00+08:00').isoformat() == '2026-09-24'
    assert expected_market_date('2026-09-28T15:05:00+08:00').isoformat() == '2026-09-28'
    assert not is_trading_day('2026-09-20')
    with pytest.raises(DomainError):
        is_trading_day('2027-01-01')
    with pytest.raises(DomainError):
        parse_time('2026-09-27T18:00:00')
