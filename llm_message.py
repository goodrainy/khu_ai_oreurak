# =========================================================
# llm_message.py : 추천 문구를 ChatKHU(API Gateway)로 자연스럽게 다듬는 모듈
# 원칙:
#   - 판정(급등/등급/추천 품목)은 기존 코드가 하고, AI는 '문장'만 다듬음
#   - AI 실패/지연/엉뚱한 답이면 → 조사 보정한 기본 문장으로 대체 (서비스 안 멈춤)
#   - 같은 내용은 하루 동안 캐시해서 호출 횟수(크레딧) 절약
# =========================================================
import re
import requests
import streamlit as st

GATEWAY_URL = "https://factchat-cloud.mindlogic.ai/v1/gateway/chat/completions/"
TIMEOUT_SEC = 10


# ---------- 1. 조사 보정 (AI와 무관하게 항상 정확) ----------
def _core(word):
    """'깐마늘(국산)' → '깐마늘' : 끝의 괄호 부분을 빼고 받침 판단용 글자를 얻음"""
    return re.sub(r"\s*\([^)]*\)\s*$", "", str(word).strip())


def josa(word, with_batchim, without_batchim):
    """받침 있으면 with_batchim, 없으면 without_batchim 조사를 붙여 반환
    예) josa('깻잎','은','는') → '깻잎은',  josa('상추','은','는') → '상추는'"""
    word = str(word).strip()
    base = _core(word)
    if not base:
        return word
    last = base[-1]
    if '가' <= last <= '힣':
        has_batchim = (ord(last) - ord('가')) % 28 != 0
        return word + (with_batchim if has_batchim else without_batchim)
    return word + without_batchim


# ---------- 2. 기본 문장 (AI 실패 시 사용) ----------
def fallback_text(item, grade, target, tag):
    """AI 없이 만드는 기본 문장. 조사 보정 적용."""
    if grade.startswith("3등급"):
        return f"[{tag}] {josa(item, '이', '가')} 올랐는데, 같은 용도로 쓸 대체 품목이 없어요. 오늘은 다른 메뉴를 고려해 보세요."
    if grade.startswith("1등급"):
        return f"[{tag}] {josa(item, '이', '가')} 올랐어요. 대신 {josa(target, '은', '는')} 가격 흐름이 안정적이에요."
    return f"[{tag}] {josa(item, '이', '가')} 올랐어요. 대신 {josa(target, '도', '도')} 오르는 중이지만 상대적으로 완만해요."


# ---------- 3. AI 호출 ----------
def _get_secret(name, default=None):
    try:
        return st.secrets[name]
    except Exception:
        return default


@st.cache_data(ttl=86400, show_spinner=False)  # 같은 입력은 하루 동안 재사용
def _ask_llm(facts_text):
    """facts_text(사실 요약)를 주고 문장을 받아옴. 실패하면 예외 → 캐시되지 않음"""
    key = _get_secret("KHU_LLM_KEY")
    model = _get_secret("KHU_LLM_MODEL", "claude-sonnet-5")
    if not key:
        raise RuntimeError("KHU_LLM_KEY 없음")

    system = (
        "너는 식재료 가격 알림 서비스 '오르락내리락'의 문구 작성자야. "
        "아래 [사실]에 있는 내용만 사용해서 한국어 해요체로 1~2문장을 써. "
        "숫자·품목명·등급을 바꾸거나 새로운 정보(원인, 가격, 요리법 등)를 추가하지 마. "
        "조사(은/는, 이/가, 을/를)는 받침에 맞게 정확히 써. 이모지와 마크다운은 쓰지 마."
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": facts_text},
        ],
        "temperature": 0.3,
        "max_tokens": 200,
    }
    res = requests.post(
        GATEWAY_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body, timeout=TIMEOUT_SEC,
    )
    res.raise_for_status()
    return res.json()["choices"][0]["message"]["content"].strip()


# ---------- 4. 최종 문구 만들기 (app.py에서 이 함수를 호출) ----------
def card_message(row, df, name_col="품목명"):
    """추천 카드 1장의 '이유' 문장을 반환. row = result_df의 한 행"""
    item = row[name_col]
    grade = row["recommend_grade"]
    target = row["recommend_target"]
    tag = "급등" if row.get("is_surge", False) else "상승 상위"
    base = fallback_text(item, grade, target, tag)

    # AI 키가 없으면 기본 문장
    if not _get_secret("KHU_LLM_KEY"):
        return base

    # AI에게 줄 '사실' 정리
    facts = [
        f"올라온 품목: {item} (1일 {row['rate_1d']:.1f}%, 1개월 {row['rate_1m']:.1f}%)",
        f"상태: {tag}",
        f"추천 등급: {grade}",
    ]
    if grade.startswith("3등급"):
        facts.append("대체 품목: 없음 (같은 조리 용도 안에 대체할 품목이 없어 다른 메뉴 권장)")
    else:
        t = df[df[name_col] == target]
        if not t.empty:
            t = t.iloc[0]
            facts.append(f"대체 품목: {target} (1일 {t['rate_1d']:.1f}%, 1개월 {t['rate_1m']:.1f}%)")
        else:
            facts.append(f"대체 품목: {target}")
    facts_text = "[사실]\n" + "\n".join(facts)

    try:
        text = _ask_llm(facts_text)
        # 안전장치: 품목명이 빠졌거나 너무 길면 AI 답을 버리고 기본 문장 사용
        if _core(item) not in text or len(text) > 200:
            return base
        return f"[{tag}] {text}"
    except Exception as e:
        print(f"[LLM 실패 → 기본 문장 사용] {e}")
        return base
