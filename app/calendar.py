"""Exchange calendar; unverified years fail explicitly."""
from datetime import date, datetime, timedelta, time
from .store import DomainError, parse_time, CN_TZ

SOURCE = "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml"
HOLIDAY_RANGES = {
    2026: [("2026-01-01", "2026-01-03"), ("2026-02-15", "2026-02-23"),
           ("2026-04-04", "2026-04-06"), ("2026-05-01", "2026-05-05"),
           ("2026-06-19", "2026-06-21"), ("2026-09-25", "2026-09-27"),
           ("2026-10-01", "2026-10-07")]
}


def _date(value):
    if isinstance(value, datetime):
        return value.astimezone(CN_TZ).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as e:
        raise DomainError("交易日期格式不正确") from e


def calendar_status(value):
    d = _date(value)
    ok = d.year in HOLIDAY_RANGES
    return {"year": d.year, "verified": ok, "source": SOURCE if ok else None,
            "warning": None if ok else f"{d.year} 年交易日历尚未核验，请更新后再计算交易日"}


def is_trading_day(value):
    d = _date(value)
    status = calendar_status(d)
    if not status["verified"]:
        raise DomainError(status["warning"])
    return d.weekday() < 5 and not any(date.fromisoformat(a) <= d <= date.fromisoformat(b) for a, b in HOLIDAY_RANGES[d.year])


def next_trading_day(value, count=1):
    if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > 250:
        raise DomainError("后续交易日数量必须在 1 至 250 之间")
    d = _date(value)
    while count:
        d += timedelta(days=1)
        if is_trading_day(d):
            count -= 1
    return d


def expected_market_date(value):
    dt = parse_time(value)
    d = dt.date()
    if is_trading_day(d) and dt.time() >= time(9, 30):
        return d
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d

