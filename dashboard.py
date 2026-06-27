from __future__ import annotations

import json
import time
from typing import Dict, Iterable

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from config import CONFIG
from data_feed import create_feed
from logger import get_logger
from models import Direction, IctSnapshot, Trade
from signal_engine import SignalEngine
from trade_manager import TradeManager

log = get_logger(__name__)


st.set_page_config(page_title=f"{CONFIG.data.symbol} Institutional Terminal", page_icon="BTC", layout="wide")

st.markdown(
    """
    <style>
      [data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"] {
        visibility: hidden;
        height: 0%;
        position: fixed;
      }
      #MainMenu, footer {visibility: hidden;}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def resources():
    return create_feed(), SignalEngine(), TradeManager()


def chart(df: pd.DataFrame, snapshots: Dict[str, IctSnapshot], trades: Iterable[Trade]) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"], name=CONFIG.data.symbol))
    primary = snapshots.get(CONFIG.timeframes.primary)
    if primary:
        for zone in primary.zones + primary.liquidity_levels:
            color = "rgba(32, 180, 115, 0.22)" if zone.direction == Direction.BUY else "rgba(220, 70, 70, 0.22)"
            line = "rgba(32, 180, 115, 0.9)" if zone.direction == Direction.BUY else "rgba(220, 70, 70, 0.9)"
            fig.add_shape(type="rect", x0=zone.start_time, x1=df.index[-1], y0=zone.low, y1=zone.high, fillcolor=color, line=dict(color=line, width=1))
            fig.add_annotation(x=df.index[-1], y=(zone.low + zone.high) / 2, text=zone.kind, showarrow=False, font=dict(size=10), xanchor="left")
    for trade in trades:
        sig = trade.signal
        marker = "triangle-up" if sig.direction == Direction.BUY else "triangle-down"
        color = "#25b06d" if sig.direction == Direction.BUY else "#d94d4d"
        fig.add_trace(go.Scatter(x=[sig.timestamp], y=[sig.entry], mode="markers", marker=dict(symbol=marker, size=13, color=color), name=f"{sig.direction.value} #{trade.ticket}"))
        fig.add_hline(y=sig.stop_loss, line_dash="dot", line_color="#d94d4d", annotation_text=f"SL #{trade.ticket}")
        fig.add_hline(y=sig.take_profit, line_dash="dot", line_color="#25b06d", annotation_text=f"TP #{trade.ticket}")
    fig.update_layout(height=650, template="plotly_dark", margin=dict(l=10, r=10, t=25, b=10), xaxis_rangeslider_visible=False, legend=dict(orientation="h"))
    return fig


def tradingview_chart(df: pd.DataFrame, snapshots: Dict[str, IctSnapshot], trades: Iterable[Trade]) -> None:
    candles = [
        {
            "time": int(ts.timestamp()),
            "open": round(float(row.open), 2),
            "high": round(float(row.high), 2),
            "low": round(float(row.low), 2),
            "close": round(float(row.close), 2),
        }
        for ts, row in df.iterrows()
    ]
    volume = [
        {
            "time": int(ts.timestamp()),
            "value": float(row.volume),
            "color": "rgba(38,166,154,0.35)" if row.close >= row.open else "rgba(239,83,80,0.35)",
        }
        for ts, row in df.iterrows()
    ]
    markers = []
    price_lines = []
    for trade in trades:
        sig = trade.signal
        markers.append(
            {
                "time": int(pd.Timestamp(sig.timestamp).timestamp()),
                "position": "belowBar" if sig.direction == Direction.BUY else "aboveBar",
                "color": "#26a69a" if sig.direction == Direction.BUY else "#ef5350",
                "shape": "arrowUp" if sig.direction == Direction.BUY else "arrowDown",
                "text": f"{sig.direction.value} #{trade.ticket}",
            }
        )
        price_lines.extend(
            [
                {"price": sig.entry, "color": "#f5c542", "title": f"Entry #{trade.ticket}"},
                {"price": sig.stop_loss, "color": "#ef5350", "title": f"SL #{trade.ticket}"},
                {"price": sig.take_profit, "color": "#26a69a", "title": f"TP #{trade.ticket}"},
            ]
        )

    primary = snapshots.get(CONFIG.timeframes.primary)
    zone_lines = []
    if primary:
        for zone in primary.zones + primary.liquidity_levels:
            zone_lines.append(
                {
                    "price": round((zone.low + zone.high) / 2, 2),
                    "color": "#26a69a" if zone.direction == Direction.BUY else "#ef5350",
                    "title": zone.kind,
                }
            )

    payload = {
        "candles": candles,
        "volume": volume,
        "markers": markers,
        "priceLines": price_lines + zone_lines[-12:],
    }
    html = f"""
    <div id="tv-chart" style="height:680px;width:100%;background:#131722;"></div>
    <script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
    <script>
      const payload = {json.dumps(payload)};
      const root = document.getElementById('tv-chart');
      root.innerHTML = '';
      const chart = LightweightCharts.createChart(root, {{
        layout: {{
          background: {{ type: 'solid', color: '#131722' }},
          textColor: '#d1d4dc',
          fontFamily: '-apple-system, BlinkMacSystemFont, Trebuchet MS, Roboto, Ubuntu, sans-serif',
        }},
        grid: {{
          vertLines: {{ color: '#1f2937' }},
          horzLines: {{ color: '#1f2937' }},
        }},
        crosshair: {{
          mode: LightweightCharts.CrosshairMode.Normal,
          vertLine: {{ color: '#758696', width: 1, style: LightweightCharts.LineStyle.Dashed, labelBackgroundColor: '#2962ff' }},
          horzLine: {{ color: '#758696', width: 1, style: LightweightCharts.LineStyle.Dashed, labelBackgroundColor: '#2962ff' }},
        }},
        rightPriceScale: {{
          borderColor: '#2a2e39',
          scaleMargins: {{ top: 0.08, bottom: 0.22 }},
        }},
        timeScale: {{
          borderColor: '#2a2e39',
          timeVisible: true,
          secondsVisible: false,
        }},
        handleScroll: false,
        handleScale: false,
        localization: {{
          priceFormatter: price => price.toFixed(2),
        }},
      }});

      const candleSeries = chart.addCandlestickSeries({{
        upColor: '#26a69a',
        downColor: '#ef5350',
        borderUpColor: '#26a69a',
        borderDownColor: '#ef5350',
        wickUpColor: '#26a69a',
        wickDownColor: '#ef5350',
        priceLineColor: '#2962ff',
      }});
      candleSeries.setData(payload.candles);
      candleSeries.setMarkers(payload.markers);

      const volumeSeries = chart.addHistogramSeries({{
        priceFormat: {{ type: 'volume' }},
        priceScaleId: '',
      }});
      volumeSeries.priceScale().applyOptions({{
        scaleMargins: {{ top: 0.82, bottom: 0 }},
      }});
      volumeSeries.setData(payload.volume);

      payload.priceLines.forEach(line => {{
        candleSeries.createPriceLine({{
          price: Number(line.price),
          color: line.color,
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: true,
          title: line.title,
        }});
      }});

      chart.timeScale().fitContent();
      const ro = new ResizeObserver(entries => {{
        const rect = entries[0].contentRect;
        chart.applyOptions({{ width: Math.floor(rect.width), height: 680 }});
      }});
      ro.observe(root);
    </script>
    """
    components.html(html, height=700, scrolling=False)


def open_trades_df(trades: Iterable[Trade]) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append(
            {
                "Time": t.opened_at,
                "Ticket": t.ticket,
                "Symbol": t.signal.symbol,
                "Side": t.signal.direction.value,
                "Volume": t.lot_size,
                "Entry": t.signal.entry,
                "SL": round(float(t.current_sl), 2),
                "TP": round(float(t.current_tp), 2),
                "PnL": t.pnl,
                "Status": t.status.value,
                "Confidence": round(t.signal.confidence, 1),
            }
        )
    return pd.DataFrame(rows)


def closed_trades_df(trades: Iterable[Trade]) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append(
            {
                "Open Time": t.opened_at,
                "Ticket": t.ticket,
                "Symbol": t.signal.symbol,
                "Type": t.signal.direction.value.lower(),
                "Volume": t.lot_size,
                "Open Price": t.signal.entry,
                "S / L": t.signal.stop_loss,
                "T / P": t.signal.take_profit,
                "Close Time": t.closed_at,
                "Close Price": t.close_price,
                "Result": "Win" if t.pnl > 0 else "Loss",
                "Profit": t.pnl,
                "RR": round(t.rr_achieved, 2),
                "Duration": str(t.closed_at - t.opened_at) if t.closed_at else "",
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    st.title(f"{CONFIG.data.symbol} ICT/Crypto Signal Bot")
    feed, signal_engine, trade_manager = resources()
    with st.sidebar:
        st.subheader("Bot")
        auto_refresh = st.toggle("Live scan", value=True)
        st.caption("Account-free public data mode. No MT5 or TradingView login. Playback and fake candles are disabled.")

    bars = CONFIG.data.history_bars
    try:
        frames = feed.get_multi_timeframe(bars)
        tick = feed.get_tick()
    except Exception as exc:
        st.error(str(exc))
        st.info("No fake candles are shown. Connect a real data source or wait until the public source returns candles.")
        return
    if CONFIG.timeframes.primary not in frames or frames[CONFIG.timeframes.primary].empty:
        st.error("No real candle data is available.")
        return
    snapshots = signal_engine.analyze(frames)
    if CONFIG.timeframes.primary not in snapshots:
        st.warning("Collecting enough closed candles for ICT analysis.")
        return
    last = frames[CONFIG.timeframes.primary].iloc[-1]
    trade_manager.update(last.to_dict(), frames[CONFIG.timeframes.primary].index[-1].to_pydatetime())
    signals = signal_engine.generate_all(frames, tick)
    for signal in signals:
        opened, reason = trade_manager.submit_signal(signal, tick)
        if not opened:
            log.info("Signal rejected by risk manager: %s", reason)

    stats = trade_manager.stats()
    source_name = getattr(feed, "source_symbol", CONFIG.data.symbol) or CONFIG.data.symbol
    st.caption(f"Data source: {source_name} | Last closed price: {frames[CONFIG.timeframes.primary]['close'].iloc[-1]:.2f}")
    cols = st.columns(8)
    for col, key in zip(cols, ["total_trades", "wins", "losses", "winrate", "current_streak", "daily_pnl", "weekly_pnl", "net_pnl"]):
        col.metric(key.replace("_", " ").title(), stats[key])

    left, right = st.columns([3, 1])
    with left:
        chart_df = frames[CONFIG.timeframes.primary].tail(500)
        tradingview_chart(chart_df, snapshots, trade_manager.open_trades + trade_manager.closed_trades[-20:])
    with right:
        primary = snapshots[CONFIG.timeframes.primary]
        st.subheader("ICT Read")
        st.metric("Bias", primary.bias.title())
        st.metric("Trend Strength", round(primary.trend_strength, 1))
        st.metric("ATR", round(primary.atr, 2))
        st.metric("Premium/Discount", primary.premium_discount.title())
        st.write("Concepts")
        st.dataframe(pd.DataFrame({"Detected": primary.concepts}), hide_index=True, use_container_width=True)

    tab1, tab2 = st.tabs(["Trade", "History"])
    with tab1:
        st.subheader("Open Positions")
        st.dataframe(open_trades_df(trade_manager.open_trades), hide_index=True, use_container_width=True)
    with tab2:
        st.subheader("Account History")
        history = closed_trades_df(trade_manager.closed_trades)
        st.dataframe(history, hide_index=True, use_container_width=True)
        hist_cols = st.columns(4)
        hist_cols[0].metric("Closed P/L", stats["net_pnl"])
        hist_cols[1].metric("Deposits", "0.00")
        hist_cols[2].metric("Withdrawals", "0.00")
        hist_cols[3].metric("Balance", round(CONFIG.risk.account_equity + float(stats["net_pnl"]), 2))

    if auto_refresh:
        time.sleep(CONFIG.data.poll_seconds)
        st.rerun()


if __name__ == "__main__":
    main()
