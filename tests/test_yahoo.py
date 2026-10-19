from resultagent.yahoo import _ret, _rsi, describe, signals


def test_rsi_and_returns():
    up = [float(x) for x in range(1, 40)]
    assert _rsi(up) == 100.0 and _rsi(up[:5]) is None
    assert round(_ret([100.0, 110.0], 1)) == 10


def base(**kw):
    s = {"symbol": "XYZ", "name": "Xyz Ltd", "industry": "Auto Parts", "currency": "INR", "fin_currency": "INR",
         "mcap": 1919, "price": 300.0, "dma200": 180.0, "rsi": 75, "vol_x": 8.9, "high_52w": 300.0,
         "pe": 33.2, "fwd_pe": 20.0, "de": 0.23, "debt": 119, "cash": 28,
         "quarters": [("2025-06-30", {"TotalRevenue": 139}), ("2025-09-30", {"TotalRevenue": 131}),
                      ("2025-12-31", {"TotalRevenue": 177}), ("2026-03-31", {"TotalRevenue": 225}),
                      ("2026-06-30", {"TotalRevenue": 184})]}
    s.update(kw)
    return s


def test_signals_from_numbers():
    sig = signals(base())
    assert "overbought (RSI>70)" in sig and "uptrend (above 200DMA)" in sig
    assert "extended: >50% above 200DMA" in sig and any("8.9x" in x for x in sig)
    assert "revenue rose in 2 of last 4 quarters" in sig
    assert any("fwd P/E" in x for x in sig)


def test_describe_inr_and_usd_reporting():
    d = describe(base())
    assert "mcap ₹1,919 Cr (small cap)" in d and "debt ₹119 Cr" in d and "₹ Cr; the newest result" in d
    us = describe(base(symbol="INFY", currency="INR", fin_currency="USD", debt=923, cash=3100))
    assert "debt $923M" in us and "cash $3.1B" in us and "USD M" in us
    assert "RSI 75 (overbought)" in d and "RSI 38 (weak)" in describe(base(rsi=38))
    aapl = describe(base(symbol="AAPL", currency="USD", fin_currency="USD", mcap=5_000_000, price=343.32))
    assert "price $343.32" in aapl and "mega cap" in aapl
