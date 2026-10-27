# =========================================================
# api_client.py : 공공데이터 등락률 API 연동 모듈
# 하는 일:
#   1) 오늘 날짜로 조회 → 데이터 없으면 하루씩 이전 날짜로 후퇴 (최대 7일)
#   2) 소매 데이터를 우선 사용, 소매가 없는 품목은 중도매로 대체
#   3) 품목코드(item_cd) 기준으로 품종/등급을 평균내어 품목당 1줄로 만듦
#   4) 우리 58개 품목 엑셀과 품목코드로 합침
# =========================================================
import os
from datetime import datetime, timedelta

import pandas as pd
import requests
import streamlit as st

BASE_URL = "https://apis.data.go.kr/B552845/risesAndFalls/info"
MAX_FALLBACK_DAYS = 7  # 오늘부터 최대 며칠 전까지 거슬러 올라갈지


def _get_api_key():
    """secrets.toml 또는 환경변수에서 API 키를 읽습니다. (코드에 키를 직접 쓰지 않기)"""
    try:
        return st.secrets["DATA_GO_KR_KEY"]
    except Exception:
        key = os.environ.get("DATA_GO_KR_KEY")
        if not key:
            raise RuntimeError("API 키가 없습니다. .streamlit/secrets.toml 에 DATA_GO_KR_KEY 를 넣어주세요.")
        return key


def _fetch_category(ctgry_cd, ymd, key, num_rows=1000):
    """부류코드 1개 + 날짜 1개를 조회해서 item 리스트를 돌려줍니다. 실패하면 빈 리스트."""
    params = {
        "serviceKey": key,
        "returnType": "JSON",
        "pageNo": 1,
        "numOfRows": num_rows,
        "cond[exmn_ymd::EQ]": ymd,
        "cond[ctgry_cd::EQ]": ctgry_cd,
    }
    try:
        res = requests.get(BASE_URL, params=params, timeout=15)
        res.raise_for_status()
        body = res.json().get("response", {}).get("body", {})
        items = body.get("items", {})
        # items가 {} 이거나 문자열("")로 오는 경우 대비
        items = items.get("item", []) if isinstance(items, dict) else []
        # 결과가 1건이면 dict 하나로 오는 경우 대비
        if isinstance(items, dict):
            items = [items]
        return items
    except Exception as e:
        print(f"[API 오류] 부류 {ctgry_cd}, 날짜 {ymd}: {e}")
        return []


def _fetch_all_with_fallback(ctgry_codes, key):
    """
    오늘 → 하루씩 과거로 후퇴하며,
    '데이터가 있고 + 1일/1개월 등락률이 충분히 채워진 첫 날짜'의 전체 데이터를 반환.
    (월요일처럼 전날 시세가 없으면 1일 등락률이 null로 와서 그 날짜는 건너뜀)
    """
    today = datetime.now()
    for back in range(MAX_FALLBACK_DAYS + 1):
        ymd = (today - timedelta(days=back)).strftime("%Y%m%d")
        all_items = []
        for cd in ctgry_codes:
            all_items.extend(_fetch_category(cd, ymd, key))
        if not all_items:
            continue  # 휴장일 등 데이터 없음 → 이전 날짜로

        # 1일/1개월 등락률이 채워진 행의 비율 계산
        total = len(all_items)
        ok_1d = sum(1 for it in all_items if it.get("dd1_bfr_cmpr_rafrt") not in (None, ""))
        ok_1m = sum(1 for it in all_items if it.get("mm1_bfr_cmpr_rafrt") not in (None, ""))
        if ok_1d / total >= 0.5 and ok_1m / total >= 0.5:
            return all_items, ymd
        print(f"[건너뜀] {ymd}: 1일 등락률 {ok_1d}/{total}, 1개월 등락률 {ok_1m}/{total}")
    return [], None


def _aggregate(api_raw_df):
    """품목코드별 대표값 계산: 소매 우선, 없으면 중도매. price_basis 컬럼에 기준 기록."""
    api_raw_df = api_raw_df.copy()
    api_raw_df["item_cd"] = api_raw_df["item_cd"].astype(str)
    api_raw_df["rate_1d"] = pd.to_numeric(api_raw_df.get("dd1_bfr_cmpr_rafrt"), errors="coerce")
    api_raw_df["rate_1w"] = pd.to_numeric(api_raw_df.get("ww1_bfr_cmpr_rafrt"), errors="coerce")
    api_raw_df["rate_1m"] = pd.to_numeric(api_raw_df.get("mm1_bfr_cmpr_rafrt"), errors="coerce")
    api_raw_df["current_price"] = pd.to_numeric(api_raw_df.get("exmn_dd_cnvs_avg_prc"), errors="coerce")
    # 'se_nm'에 '소매'가 들어있으면 소매 (중도매의 '도매'와 헷갈리지 않게 '소매'로 판별)
    api_raw_df["is_retail"] = api_raw_df["se_nm"].astype(str).str.contains("소매") & \
                              ~api_raw_df["se_nm"].astype(str).str.contains("도매")

    rows = []
    for item_cd, g in api_raw_df.groupby("item_cd"):
        retail = g[g["is_retail"]]
        if not retail.empty:
            use, basis = retail, "소매"
        else:
            use, basis = g, "중도매"
        rows.append({
            "item_cd": item_cd,
            "rate_1d": use["rate_1d"].mean(),
            "rate_1w": use["rate_1w"].mean(),
            "rate_1m": use["rate_1m"].mean(),
            "current_price": use["current_price"].mean(),
            "price_basis": basis,
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=3600, show_spinner="실시간 가격 데이터를 불러오는 중...")
def load_master_data(metadata_path):
    """
    반환: (master_df, 기준날짜 'YYYYMMDD' 또는 None, 경고메시지 또는 '')
    master_df 컬럼: 메타데이터 컬럼들 + rate_1d, rate_1w, rate_1m, current_price, price_basis, has_data
    """
    metadata_df = pd.read_excel(metadata_path)
    metadata_df["품목코드"] = metadata_df["품목코드"].astype(str)
    ctgry_codes = sorted(metadata_df["부류코드"].astype(str).unique())

    key = _get_api_key()
    items, ymd = _fetch_all_with_fallback(ctgry_codes, key)

    if not items:
        # API 실패 시: 가격 컬럼을 비워서 반환 (앱이 죽지 않게)
        df = metadata_df.copy()
        for c in ["rate_1d", "rate_1w", "rate_1m", "current_price"]:
            df[c] = 0.0
        df["price_basis"] = ""
        df["has_data"] = False
        return df, None, "⚠️ 실시간 데이터를 불러오지 못했습니다. API 키/네트워크를 확인해주세요."

    agg_df = _aggregate(pd.DataFrame(items))
    df = pd.merge(metadata_df, agg_df, left_on="품목코드", right_on="item_cd", how="left")
    df["has_data"] = df["rate_1d"].notna() & df["rate_1m"].notna()
    for c in ["rate_1d", "rate_1w", "rate_1m", "current_price"]:
        df[c] = df[c].fillna(0.0)
    df["price_basis"] = df["price_basis"].fillna("")

    missing = int((~df["has_data"]).sum())
    warn = f"ℹ️ API에서 값을 찾지 못한 품목 {missing}개는 0으로 처리됩니다." if missing else ""
    return df, ymd, warn
