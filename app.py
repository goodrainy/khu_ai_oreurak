import streamlit as st
import pandas as pd
import json
import requests
import os
from api_client import load_master_data

# =========================================================
# [Part 1] 설정 및 환경 세팅
# =========================================================
st.set_page_config(page_title="오르락내리락", layout="wide")

# 텔레그램 설정 (사용자 입력 필수)
TELEGRAM_TOKEN = st.secrets["TELEGRAM_TOKEN"]
MY_CHAT_ID = st.secrets["MY_CHAT_ID"]
SUBSCRIBERS_FILE = 'subscribers.json'
METADATA_FILE = '품목58선_최종_통합본.xlsx'

# =========================================================
# [Part 2] 핵심 엔진 (알고리즘 및 통신)
# =========================================================

def send_telegram_message(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': str(chat_id), 'text': message, 'parse_mode': 'HTML'}
    try:
        response = requests.post(url, data=payload)
        if response.status_code == 200:
            print("✅ 텔레그램 메시지 전송 성공!")
        else:
            print(f"❌ 전송 실패: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"🚨 전송 중 에러: {e}")

def run_grade_logic(df, group_col='조리 용도', name_col='품목명',
                    threshold_1d=5.0, threshold_1m=5.0, fallback_top_n=5):
    df = df.copy()
    df[group_col] = df[group_col].astype(str).str.strip()
    df[name_col] = df[name_col].astype(str).str.strip()
    df['recommend_grade'] = '정상'
    df['recommend_target'] = ''
    df['reason'] = ''

    # 변동 점수: 1일 40% + 1개월 60% (설문 결과 나오면 조정)
    df['score'] = (df['rate_1d'] * 0.4) + (df['rate_1m'] * 0.6)

    # 급등 여부 (기존 기준 그대로)
    df['is_surge'] = (df['rate_1d'] >= threshold_1d) & (df['rate_1m'] >= threshold_1m)

    # 추천 대상 1순위: 급등 품목
    target_idx = list(df[df['is_surge']].index)

    # 급등이 부족하면, 점수가 높은(많이 오르는) 품목으로 채움
    if len(target_idx) < fallback_top_n:
        others = df[(~df['is_surge']) & (df['score'] > 0)]
        need = fallback_top_n - len(target_idx)
        target_idx += list(others.nlargest(need, 'score').index)

    for idx in target_idx:
        row = df.loc[idx]
        tag = '급등' if row['is_surge'] else '상승 상위'
        target_group = row[group_col]

        # 같은 조리 용도 안의 후보 (자기 자신 제외, 데이터 없는 품목 제외)
        candidates = df[(df[group_col] == target_group) & (df.index != idx)]
        if 'has_data' in candidates.columns:
            candidates = candidates[candidates['has_data']]

        if candidates.empty:
            grade, target = '3등급(최후)', '메뉴 변경 권장'
            reason = f"[{tag}] [{target_group}] 내 대체 품목 없음"
        else:
            best = candidates.sort_values(by='score').iloc[0]
            if best['score'] < threshold_1d * 0.5:
                grade, target = '1등급(최선)', best[name_col]
                reason = f"[{tag}] {best[name_col]}는 추세가 매우 안정적입니다."
            else:
                grade, target = '2등급(차선)', best[name_col]
                reason = f"[{tag}] {best[name_col]}도 상승 중이나 상대적으로 완만합니다."

        df.at[idx, 'recommend_grade'] = grade
        df.at[idx, 'recommend_target'] = target
        df.at[idx, 'reason'] = reason
    return df

def subscribe_item(chat_id, item_name, alert_type):
    try:
        if not os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE, 'w') as f: json.dump([], f)
        with open(SUBSCRIBERS_FILE, 'r') as f:
            subscribers = json.load(f)
        new_sub = {'chat_id': str(chat_id), 'item': item_name, 'alert_type': alert_type}
        if new_sub not in subscribers:
            subscribers.append(new_sub)
            with open(SUBSCRIBERS_FILE, 'w') as f:
                json.dump(subscribers, f)
            msg = (f"✅ <b>[오르락내리락] 구독 완료!</b>\n\n"
                   f"📍 <b>대상 품목:</b> {item_name}\n"
                   f"🔔 <b>알림 조건:</b> {alert_type}\n\n"
                   f"앞으로 설정하신 조건에 맞춰 안내해 드릴게요! 🚀")
            send_telegram_message(TELEGRAM_TOKEN, chat_id, msg)
            return True
        return False
    except Exception as e:
        print(f"Subscription Error: {e}")
        return False

# =========================================================
# [Part 3] Streamlit UI 구성
# =========================================================

def main():
    # [이미지 유지] 상단 타이틀 섹션
    st.title("🍎 오르락내리락")
    st.caption("실시간 식재료 가격 변동 및 맞춤형 대체재 추천 서비스")

    # 실시간 API + 58개 메타데이터 통합
    master_df, base_date, warn_msg = load_master_data(METADATA_FILE)
    if base_date:
        st.caption(f"📅 데이터 기준일: {base_date[:4]}-{base_date[4:6]}-{base_date[6:]}")
    if warn_msg:
        st.warning(warn_msg)
    result_df = run_grade_logic(master_df)


    # [요구사항 3] 사이드바 메뉴 순서: 홈 -> 서비스 소개 -> 챗봇 구독하기
    st.sidebar.header("📂 메뉴")
    menu = st.sidebar.radio("이동하기", ["🏠 홈 (대시보드)", "📖 서비스 소개", "🔔 챗봇 구독하기"])

    if menu == "🏠 홈 (대시보드)":
        # [요구사항 5] 추천 카드를 최상단으로 배치
        st.subheader("💡 오늘의 추천 카드")
        recommend_items = result_df[result_df['recommend_target'] != '']
        
        if recommend_items.empty:
            st.info("현재 추천할 수 있는 품목이 없습니다.")
        else:
            cols = st.columns(3)
            for idx, (_, row) in enumerate(recommend_items.iterrows()):
                with cols[idx % 3]:
                    with st.container(border=True):
                        # [요구사항 4] 등급 안내
                        if row['recommend_grade'] == '1등급(최선)':
                            st.markdown("### ✅ 1등급 (최선)")
                        elif row['recommend_grade'] == '2등급(차선)':
                            st.markdown("### ⚠️ 2등급 (차선)")
                        else:
                            st.markdown("### 🚨 3등급 (최후)")
                        st.write(f"**품목:** {row['품목명']}")
                        st.write(f"**추천:** `{row['recommend_target']}`")
                        st.caption(f"이유: {row['reason']}")
                        
                        basis = result_df.loc[result_df['품목명'] == row['recommend_target'], 'price_basis']
                        if not basis.empty and basis.iloc[0] == '중도매':
                            st.caption("*중도매가 기준이므로 소매가와 다를 수 있습니다*")

        st.divider()

        # [요구사항 5] 전체 품목 현황을 아래로 배치
        st.subheader("📊 실시간 급등 품목 및 전체 현황")
        
        # 메트릭 섹션
        m_col1, m_col2, m_col3 = st.columns(3)
        with m_col1:
            spikes = int(result_df['is_surge'].sum())
            st.metric("🚨 현재 급등 품목", f"{spikes}개")
        with m_col2:
            avg_rate = result_df['rate_1d'].mean()
            st.metric("📈 평균 등락률(1d)", f"{avg_rate:.1f}%")
        with m_col3:
            rec_count = len(result_df[result_df['recommend_target'] != ''])
            st.metric("💡 추천 대안 수", f"{rec_count}건")

        st.write("#### 📋 전체 품목 현황")
        st.dataframe(result_df, use_container_width=True)

    elif menu == "📖 서비스 소개":
        st.subheader("💡 서비스 소개")
        st.write("오르락내리락은 AI를 통해 식재료 가격 변동을 감지하고 최적의 대체재를 추천합니다.")
        
        st.divider()
        # [요구사항 3] 선정 기준 포함
        st.subheader("⚖️ AI 선정 기준 안내")
        st.info("""
        **1. 가격 급등 판단 기준**
        - 1일 등락률 5% 이상 & 1개월 등락률 5% 이상 동시 충족 시
        
        **2. 추천 등급 안내**
        - **🥇 1등급 (최선):** 동일 용도 내에서 가격 추세가 매우 안정적인 품목
        - **🥈 2등급 (차선):** 동일 용도 내에서 가격이 오르고 있으나 상대적으로 완만한 품목
        - **🥉 3등급 (최후):** 해당 용도 전체가 급등하여 대체재가 없는 경우 $\rightarrow$ 메뉴 변경 권장
        """)

    elif menu == "🔔 챗봇 구독하기":
        st.subheader("📱 실시간 알림 구독 설정")
        
        # [요구사항 1] 구독 가이드
        with st.expander("📢 구독 방법 안내", expanded=True):
            st.markdown("""
            **알림을 받으려면 다음 단계를 따라주세요!**
            1. 텔레그램 앱을 설치하고 **[@오르락내리락_봇]** 을 검색해 **[시작]** 을 누릅니다.
            2. 아래 목록에서 관심 품목을 선택합니다.
            3. **알림 조건**을 선택하고 **[구독 신청하기]** 를 누르면 끝!
            """)
        
        st.write("---")
        user_id = st.text_input("본인의 텔레그램 Chat ID를 입력하세요", value=MY_CHAT_ID)
        selected_items = st.multiselect("알림을 받고 싶은 품목을 선택하세요", sorted(master_df['품목명'].unique()))
        
        # [요구사항 2] 알림 조건 선택
        alert_option = st.radio(
            "어떤 상황에서 알림을 받을까요?",
            ["🚨 품목 가격이 급등할 때만", "💡 추천 제품이 떴을 때도 포함"],
            index=0
        )
        
        option_map = {
            "🚨 품목 가격이 급등할 때만": "급등 시",
            "💡 추천 제품이 떴을 때도 포함": "추천 포함"
        }

        if st.button("구독 신청하기"):
            if selected_items:
                for item in selected_items:
                    subscribe_item(user_id, item, option_map[alert_option])
                st.success(f"{len(selected_items)}개 품목 구독이 완료되었습니다!")
            else:
                st.warning("품목을 선택해주세요.")

if __name__ == "__main__":
    main()
