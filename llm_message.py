# =========================================================
# llm_message.py : 추천 문구를 ChatKHU(API Gateway)로 자연스럽게 다듬는 모듈
# 원칙:
#   - 판정(급등/등급/추천 품목)은 기존 코드가 하고, AI는 '문장'만 다듬음
#   - 화면에 등급 아이콘이 이미 표시되므로, AI 문장에는 등급/태그를 반복하지 않음
#   - AI 실패/지연/엉뚱한 답이면 → 조사 보정한 기본 문장으로 대체 (서비스 안 멈춤)
#   - 같은 내용은 하루 동안 캐시해서 호출 횟수(크레딧) 절약
#   - app.py에서 "이유 보기" 버튼을 눌렀을 때만 호출됨 (즉시 호출 안 함)
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
    """받침 있으면 with_batchim, 없으면 without_batchim 조사를 붙여 반환"""
    word = str(word).strip()
    base = _core(word)
    if not base:
        return word
    last = base[-1]
    if '가' <= last <= '힣':
        has_batchim = (ord(last) - ord('가')) % 28 != 0
        return word + (with_batchim if has_batchim else without_batchim)
    return word + without_batchim


def _target_list(target):
    """recommend_target('깻잎, 치커리' 또는 '깻잎')을 리스트로 변환"""
    if isinstance(target, (list, tuple)):
        return list(target)
    return [t.strip() for t in str(target).split(',') if t.strip()]


# ---------- 2. 기본 문장 (AI 실패 시 사용) ----------
def fallback_text(item, grade, target, tag):
    """AI 없이 만드는 기본 문장. 대체재가 1~2개인 경우 모두 처리."""
    targets = _target_list(target)
    target_str = ", ".join(targets) if targets else str(target)

    if grade.startswith("3등급"):
        return f"{josa(item, '이', '가')} 올랐는데, 같은 용도로 쓸 대체 품목이 없어요. 오늘은 다른 메뉴를 고려해 보세요."
    if grade.startswith("1등급"):
        return f"{josa(item, '이', '가')} 올랐어요. 대신 {target_str}의 가격 흐름이 비교적 안정적이에요."
    return f"{josa(item, '이', '가')} 올랐어요. 대신 {target_str}도 오르는 중이지만 상대적으로 완만해요."


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
        "화면에 등급 아이콘과 이름이 이미 표시되어 있으니, '등급', '1등급', '추천 등급', "
        "'최선', '차선', '급등', '상승 상위' 같은 표현은 절대 쓰지 마. "
        "아래 [사실]에 있는 내용만 사용해서 한국어 해요체로 1~2문장을 써. "
        "가격이 얼마나 올랐는지와 대체재가 왜 더 나은 선택인지를 자연스럽게 설명해. "
        "대체 품목이 여러 개면 자연스럽게 함께 언급해. "
        "숫자·품목명을 바꾸거나 새로운 정보(원인, 조리법 등)를 추가하지 마. "
        "조사(은/는, 이/가, 을/를)는 받침에 맞게 정확히 써. "
        "이모지, 마크다운, 대괄호 태그는 쓰지 마."
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": facts_text},
        ],
        "temperature": 0.3,
        "max_tokens": 1500,
    }
    res = requests.post(
        GATEWAY_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body, timeout=TIMEOUT_SEC,
    )
    res.raise_for_status()
    data = res.json()
    text = data["choices"][0]["message"]["content"]
    if not text or not text.strip():
        raise RuntimeError(f"빈 응답 (finish_reason={data['choices'][0].get('finish_reason')})")
    return text.strip()


# ---------- 4. 최종 문구 만들기 (app.py의 "이유 보기" 버튼이 이 함수를 호출) ----------
def card_message(row, df, name_col="품목명"):
    """추천 카드 1장의 '이유' 문장을 반환. row = result_df의 한 행"""
    item = row[name_col]
    grade = row["recommend_grade"]
    target = row["recommend_target"]
    tag = "급등" if row.get("is_surge", False) else ("1차 경고" if row.get("is_warning", False) else "상승 상위")
    base = fallback_text(item, grade, target, tag)

    if not _get_secret("KHU_LLM_KEY"):
        return base

    targets = _target_list(target)
    facts = [
        f"품목: {item} (1일 {row['rate_1d']:.1f}%, 1개월 {row['rate_1m']:.1f}% 상승)",
    ]
    if grade.startswith("3등급"):
        facts.append("같은 용도로 쓸 대체 품목이 없어서 다른 메뉴로 바꾸는 걸 권장함")
    else:
        sub_facts = []
        for t in targets:
            trow = df[df[name_col] == t]
            if not trow.empty:
                tr = trow.iloc[0]
                sub_facts.append(f"{t} (1일 {tr['rate_1d']:.1f}%, 1개월 {tr['rate_1m']:.1f}%)")
            else:
                sub_facts.append(t)
        facts.append("대체 품목 후보: " + "; ".join(sub_facts))
    facts_text = "[사실]\n" + "\n".join(facts)

    try:
        text = _ask_llm(facts_text)
        if _core(item) not in text or len(text) > 220:
            return base
        return text
    except Exception as e:
        print(f"[LLM 실패 → 기본 문장 사용] {e}")
        return base