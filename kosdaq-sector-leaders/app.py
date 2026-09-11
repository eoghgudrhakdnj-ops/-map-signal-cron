import os
import math
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import streamlit as st
import FinanceDataReader as fdr

try:
    from pykrx import stock
except Exception:
    stock = None

st.set_page_config(page_title="KOSDAQ 섹터 대장주 TOP2", page_icon="📈", layout="wide")

WEIGHTS = {
    "거래대금": 0.30,
    "개인순매수": 0.25,
    "거래량증가": 0.20,
    "뉴스증가": 0.15,
    "검색관심": 0.10,
}

SECTOR_KEYWORDS = {
    "바이오/제약": ["바이오", "제약", "의약", "신약", "의료", "진단", "헬스"],
    "반도체": ["반도체", "전자부품", "장비", "칩", "패키지", "PCB", "기판"],
    "2차전지": ["2차전지", "배터리", "전지", "양극재", "음극재"],
    "로봇": ["로봇", "자동화", "스마트팩토리"],
    "AI/SW": ["소프트웨어", "AI", "인공지능", "IT서비스", "클라우드"],
    "엔터/미디어": ["엔터", "미디어", "콘텐츠", "방송", "게임"],
    "화장품/미용": ["화장품", "미용", "뷰티"],
    "우주/방산": ["우주", "항공", "방산", "위성"],
    "원전/전력": ["원전", "전력", "발전", "변압", "송전"],
    "자동차/전장": ["자동차", "전장", "자율주행"],
    "디스플레이": ["디스플레이", "OLED", "LED"],
    "건설/기계": ["건설", "기계", "플랜트", "중공업"],
    "환경/폐기물": ["환경", "폐기물", "재활용"],
}

def percentile_100(s: pd.Series) -> pd.Series:
    return s.rank(pct=True, method="average").fillna(0) * 100

def classify_sector(row):
    text = " ".join(str(row.get(k, "")) for k in ["Sector", "Industry", "Dept", "Name"])
    for sector, kws in SECTOR_KEYWORDS.items():
        if any(k.lower() in text.lower() for k in kws):
            return sector
    sector = str(row.get("Sector", "")).strip()
    if sector and sector.lower() != "nan":
        return sector
    industry = str(row.get("Industry", "")).strip()
    if industry and industry.lower() != "nan":
        return industry
    return "기타"

@st.cache_data(ttl=3600)
def get_kosdaq_listing():
    df = fdr.StockListing("KOSDAQ")
    rename = {}
    for c in df.columns:
        lc = c.lower()
        if lc in ("code", "symbol"):
            rename[c] = "Code"
        elif lc == "name":
            rename[c] = "Name"
        elif lc == "volume":
            rename[c] = "Volume"
        elif lc in ("amount", "tradingvalue", "value"):
            rename[c] = "Amount"
        elif lc in ("marcap", "marketcap"):
            rename[c] = "Marcap"
    df = df.rename(columns=rename)
    if "Code" not in df.columns or "Name" not in df.columns:
        raise RuntimeError("KOSDAQ 종목 목록의 Code/Name 컬럼을 찾지 못했습니다.")
    df["Code"] = df["Code"].astype(str).str.zfill(6)
    if "Amount" not in df.columns:
        df["Amount"] = 0
    if "Volume" not in df.columns:
        df["Volume"] = 0
    df["SectorGroup"] = df.apply(classify_sector, axis=1)
    return df

def nearest_business_day(date=None):
    d = date or datetime.now()
    for _ in range(10):
        if d.weekday() < 5:
            return d
        d -= timedelta(days=1)
    return d

@st.cache_data(ttl=1800)
def get_investor_netbuy(target_date_str):
    if stock is None:
        return pd.DataFrame(columns=["Code", "개인순매수"])
    try:
        df = stock.get_market_net_purchases_of_equities_by_ticker(target_date_str, target_date_str, "KOSDAQ", "개인")
        if df is None or df.empty:
            return pd.DataFrame(columns=["Code", "개인순매수"])
        df = df.copy()
        df.index = df.index.astype(str).str.zfill(6)
        col = next((c for c in ["순매수거래대금", "순매수", "순매수금액"] if c in df.columns), None)
        if col is None:
            return pd.DataFrame(columns=["Code", "개인순매수"])
        out = df[[col]].rename(columns={col: "개인순매수"}).reset_index()
        out = out.rename(columns={out.columns[0]: "Code"})
        return out
    except Exception:
        return pd.DataFrame(columns=["Code", "개인순매수"])

@st.cache_data(ttl=1800)
def get_volume_ratio(codes):
    results = []
    end = datetime.now()
    start = end - timedelta(days=45)
    for i, code in enumerate(codes):
        try:
            px = fdr.DataReader(code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            if px is None or len(px) < 6 or "Volume" not in px.columns:
                continue
            vol = pd.to_numeric(px["Volume"], errors="coerce").dropna()
            if len(vol) < 6:
                continue
            today = float(vol.iloc[-1])
            baseline = float(vol.iloc[-min(21, len(vol)):-1].mean())
            ratio = today / baseline if baseline > 0 else 1.0
            results.append((code, ratio))
        except Exception:
            continue
        if i % 100 == 0 and i > 0:
            time.sleep(0.2)
    return pd.DataFrame(results, columns=["Code", "거래량배수"])

def google_news_count(query, days=1):
    q = urllib.parse.quote(f"{query} when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        r = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.text)
        return len(root.findall(".//item"))
    except Exception:
        return 0

@st.cache_data(ttl=3600)
def enrich_news(df_top):
    rows = []
    for _, r in df_top.iterrows():
        now = google_news_count(r["Name"], 1)
        week = google_news_count(r["Name"], 7)
        avg = week / 7 if week else 0
        ratio = now / avg if avg > 0 else (1.0 if now > 0 else 0.0)
        rows.append((r["Code"], now, ratio))
    return pd.DataFrame(rows, columns=["Code", "뉴스건수1일", "뉴스증가배수"])

def score_frame(df):
    work = df.copy()
    for f in ["Amount", "개인순매수", "거래량배수", "뉴스증가배수"]:
        if f not in work:
            work[f] = 0.0
        work[f] = pd.to_numeric(work[f], errors="coerce").fillna(0)
    work["거래대금점수"] = percentile_100(np.log1p(work["Amount"].clip(lower=0)))
    work["개인순매수점수"] = percentile_100(work["개인순매수"])
    work["거래량점수"] = percentile_100(work["거래량배수"])
    work["뉴스점수"] = percentile_100(work["뉴스증가배수"])
    weights = {"거래대금": .30/.90, "개인순매수": .25/.90, "거래량증가": .20/.90, "뉴스증가": .15/.90}
    work["종합점수"] = (
        work["거래대금점수"] * weights["거래대금"] +
        work["개인순매수점수"] * weights["개인순매수"] +
        work["거래량점수"] * weights["거래량증가"] +
        work["뉴스점수"] * weights["뉴스증가"]
    ).round(1)
    return work

def money_krw(x):
    try:
        x = float(x)
    except Exception:
        return "-"
    if abs(x) >= 1e12:
        return f"{x/1e12:.2f}조"
    if abs(x) >= 1e8:
        return f"{x/1e8:.0f}억"
    if abs(x) >= 1e4:
        return f"{x/1e4:.0f}만"
    return f"{x:,.0f}"

st.title("📈 코스닥 섹터별 오늘의 대장주 TOP 2")
st.caption("거래대금 · 개인 순매수 · 거래량 증가 · 뉴스 증가를 합산해 섹터별 상위 2개 종목을 자동 선정합니다.")

with st.sidebar:
    st.header("점수 기준")
    st.write("거래대금 30%")
    st.write("개인 순매수 25%")
    st.write("거래량 증가 20%")
    st.write("뉴스 증가 15%")
    top_pool = st.slider("분석 대상 상위 종목 수", 40, 200, 80, 10)
    min_amount = st.number_input("최소 거래대금(억원)", min_value=0, value=20, step=10)

run = st.button("오늘 섹터 대장주 계산", type="primary", use_container_width=True)

if run:
    with st.spinner("코스닥 데이터를 불러오는 중입니다..."):
        listing = get_kosdaq_listing().copy()
        listing["Amount"] = pd.to_numeric(listing["Amount"], errors="coerce").fillna(0)
        listing = listing[listing["Amount"] >= min_amount * 1e8].sort_values("Amount", ascending=False)
        d = nearest_business_day()
        inv = pd.DataFrame()
        for shift in range(8):
            ds = (d - timedelta(days=shift)).strftime("%Y%m%d")
            inv = get_investor_netbuy(ds)
            if not inv.empty:
                target_date = ds
                break
        else:
            target_date = "데이터 없음"
        listing = listing.merge(inv, on="Code", how="left")
        listing["개인순매수"] = listing["개인순매수"].fillna(0)
        pool = listing.head(top_pool).copy()

    with st.spinner("거래량과 뉴스를 분석하는 중입니다..."):
        vol = get_volume_ratio(pool["Code"].tolist())
        pool = pool.merge(vol, on="Code", how="left")
        pool["거래량배수"] = pool["거래량배수"].fillna(1.0)
        news = enrich_news(pool[["Code", "Name"]])
        pool = pool.merge(news, on="Code", how="left")
        pool["뉴스증가배수"] = pool["뉴스증가배수"].fillna(0)

    scored = score_frame(pool)
    leaders = scored.sort_values(["SectorGroup", "종합점수"], ascending=[True, False]).groupby("SectorGroup", group_keys=False).head(2).copy()
    leaders["순위"] = leaders.groupby("SectorGroup")["종합점수"].rank(method="first", ascending=False).astype(int)
    leaders = leaders.sort_values(["SectorGroup", "순위"])

    st.success(f"계산 완료 · 개인 순매수 기준일: {target_date}")
    if inv.empty:
        st.warning("개인 순매수 데이터가 연결되지 않아 해당 점수는 0으로 처리됐습니다.")

    sectors = sorted([x for x in leaders["SectorGroup"].dropna().unique().tolist() if x != "기타"])
    selected = st.multiselect("섹터 선택", sectors, default=sectors)
    view = leaders[leaders["SectorGroup"].isin(selected)] if selected else leaders

    show = view[["SectorGroup", "순위", "Code", "Name", "종합점수", "Amount", "개인순매수", "거래량배수", "뉴스건수1일"]].copy()
    show.columns = ["섹터", "순위", "코드", "종목명", "종합점수", "거래대금", "개인순매수", "거래량배수", "오늘뉴스"]
    show["거래대금"] = show["거래대금"].map(money_krw)
    show["개인순매수"] = show["개인순매수"].map(money_krw)
    show["거래량배수"] = show["거래량배수"].map(lambda x: f"{x:.2f}x")
    st.subheader("🏆 섹터별 1위 · 2위")
    st.dataframe(show, use_container_width=True, hide_index=True)

    st.subheader("🔥 전체 종합점수 TOP 20")
    top20 = scored.sort_values("종합점수", ascending=False).head(20)[["SectorGroup", "Code", "Name", "종합점수"]].copy()
    top20.columns = ["섹터", "코드", "종목명", "종합점수"]
    st.dataframe(top20, use_container_width=True, hide_index=True)

    csv = leaders.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button("CSV 다운로드", data=csv, file_name=f"kosdaq_sector_top2_{datetime.now().strftime('%Y%m%d')}.csv", mime="text/csv", use_container_width=True)
    st.info("이 도구는 시장 관심도·수급·거래 강도를 정리하는 선별 도구입니다. 종합점수가 높다고 향후 상승을 보장하지 않습니다.")
else:
    st.info("위의 '오늘 섹터 대장주 계산' 버튼을 누르면 시작합니다.")
