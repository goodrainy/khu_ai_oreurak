import streamlit as st
import pandas as pd
import json
import requests
import os
from api_client import load_master_data
from llm_message import card_message

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
                    warning_threshold=10.0, surge_threshold=20.0, fallback_top_n=5):
    df = df.copy()
    df[group_col] = df[group_col].astype(str).str.strip()
    df[name_col] = df[name_col].astype(str).str.strip()
    df['recommend_grade'] = '정상'
    df['recommend_target'] = ''
    df['reason'] = ''

    # 변동 점수: 1일 40% + 1개월 60% (설문 결과 반영한 급등/경고 판정에도 동일하게 사용)
    df['score'] = (df['rate_1d'] * 0.4) + (df['rate_1m'] * 0.6)

    # [설문 기반 2단계 경고 기준] 1일+1개월이 섞인 score로 판정
    # 총 89명 설문 결과, 38.2%가 10~20% 상승 구간을 체감 기준으로 선택 → 10%를 1차 경고, 20%를 급등 기준으로 설정
    df['is_surge'] = df['score'] >= surge_threshold
    df['is_warning'] = (df['score'] >= warning_threshold) & (df['score'] < surge_threshold)
    df['alert_level'] = '정상'
    df.loc[df['is_warning'], 'alert_level'] = '1차 경고'
    df.loc[df['is_surge'], 'alert_level'] = '급등'

    # 급등 + 1차 경고 둘 다 추천 대상에 포함
    target_idx = list(df[df['is_surge'] | df['is_warning']].index)

    # 그래도 부족하면, 점수가 높은(많이 오르는) 품목으로 채움
    if len(target_idx) < fallback_top_n:
        others = df[(~df['is_surge']) & (~df['is_warning']) & (df['score'] > 0)]
        need = fallback_top_n - len(target_idx)
        target_idx += list(others.nlargest(need, 'score').index)

    for idx in target_idx:
        row = df.loc[idx]
        if row['is_surge']:
            tag = '급등'
        elif row['is_warning']:
            tag = '1차 경고'
        else:
            tag = '상승 상위'
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
            if best['score'] < surge_threshold * 0.5:
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
                        st.caption(f"이유: {card_message(row, result_df)}")
                        
                        basis = result_df.loc[result_df['품목명'] == row['recommend_target'], 'price_basis']
                        if not basis.empty and basis.iloc[0] == '중도매':
                            st.caption("*중도매가 기준이므로 소매가와 다를 수 있습니다*")

        st.divider()

        # [요구사항 5] 전체 품목 현황을 아래로 배치
        st.subheader("📊 실시간 급등 품목 및 전체 현황")
        
        # 메트릭 섹션 (급등 / 1차 경고 / 평균 등락률 / 추천 대안)
        m_col1, m_col2, m_col3, m_col4 = st.columns(4)
        with m_col1:
            spikes = int(result_df['is_surge'].sum())
            st.metric("🔴 급등 품목", f"{spikes}개")
        with m_col2:
            warnings = int(result_df['is_warning'].sum())
            st.metric("🟡 1차 경고 품목", f"{warnings}개")
        with m_col3:
            avg_rate = result_df['rate_1m'].mean()
            st.metric("📈 평균 등락률(1개월)", f"{avg_rate:.1f}%")
        with m_col4:
            rec_count = len(result_df[result_df['recommend_target'] != ''])
            st.metric("💡 추천 대안 수", f"{rec_count}건")

        st.write("#### 📋 전체 품목 현황")
        st.dataframe(result_df, use_container_width=True)

    elif menu == "📖 서비스 소개":
        st.subheader("💡 서비스 소개")
        st.markdown("""
#### 가격은 오르락내리락, 장보기 고민은 가볍게!

"늘 사던 식재료인데, 어느새 가격이 이렇게 올랐네?"

장을 보다 보면 예상보다 비싸진 가격에 장바구니에 담기를 망설일 때가 있어요. 그렇다고 다른 재료의 가격을 일일이 비교하기도 쉽지 않죠.

**오르락내리락**은 식재료 가격 변화를 살펴보고, 가격이 크게 올랐을 때 대체 식품을 찾아주는 서비스예요.

가격이 조금씩 오르기 시작하면 미리 확인할 수 있도록 알려드리고, 많이 오른 경우에는 비슷한 용도로 사용할 수 있는 다른 식품을 추천해요. 대체하기 적절한 식품을 찾기 어려울 때는 메뉴를 바꿔보는 방법도 제안합니다.

오늘은 무엇을 장바구니에 담을지 고민될 때, 오르락내리락과 함께 더 알뜰한 선택을 해보세요. 🛒
        """)

        st.divider()
        st.subheader("📈 가격 변화는 이렇게 알려드려요")
        st.warning("""
**🟡 1차 경고 · 가격 상승률 10% 이상 ~ 20% 미만**

가격이 오르고 있어요. 구매하기 전에 가격 변화를 한 번 살펴보세요.
        """)
        st.error("""
**🔴 가격 급등 · 가격 상승률 20% 이상**

가격이 크게 올랐어요. 비슷한 용도로 활용할 수 있는 대체 식품을 확인해 보세요.
        """)

        st.divider()
        st.subheader("🥕 대체 식품은 어떻게 추천하나요?")
        st.write("비슷한 용도로 사용할 수 있는 식품의 가격 흐름을 살펴보고, 선택할 수 있는 대안을 안내해 드려요.")
        st.info("""
**🥇 1등급 · 가장 먼저 추천해요**
같은 용도로 사용할 수 있는 식품 중 가격 흐름이 안정적인 품목이에요.

**🥈 2등급 · 함께 고려해 보세요**
가격이 오르고 있지만, 상대적으로 완만하게 오르는 품목이에요.

**🥉 3등급 · 메뉴를 바꿔보세요**
같은 용도의 식품들이 모두 크게 올라 대체하기 어렵다면, 다른 메뉴를 선택하는 방법을 제안해요.
        """)

        st.caption("가격 정보와 추천 결과는 장보기를 돕기 위한 참고 자료예요. 실제 구매 가격은 판매처와 구매 시점에 따라 다를 수 있습니다.")

    elif menu == "🔔 챗봇 구독하기":
        st.subheader("📱 실시간 알림 구독 설정")

        st.markdown("""
#### 내 장바구니에 필요한 소식만 쏙!

자주 사는 식재료 가격이 궁금하지만, 매번 직접 확인하기 번거로우셨나요?

오르락내리락 챗봇을 구독하고 관심 있는 품목을 선택해 보세요. 설정한 조건에 따라 가격이 급등하거나 추천 정보가 나왔을 때 텔레그램으로 소식을 받아볼 수 있어요.

비싸진 식재료 앞에서 고민하는 대신, 오르락내리락이 알려주는 다른 선택지도 함께 살펴보세요. 🥬
        """)

        # [요구사항 1] 구독 가이드
        with st.expander("📢 구독은 이렇게 해요", expanded=True):
            st.markdown("""
**① 텔레그램에서 봇을 시작해 주세요.**
텔레그램 앱에서 안내된 오르락내리락 봇을 찾고 [시작]을 눌러주세요.

**② Chat ID를 입력해 주세요.**
알림을 받을 텔레그램 Chat ID를 입력해 주세요.

**③ 관심 품목과 알림 조건을 선택해 주세요.**
가격이 궁금한 식재료를 고르고, 원하는 알림 조건을 선택해 주세요.

**④ [구독 신청하기]를 누르면 끝!**
이제 관심 품목의 소식을 텔레그램에서 편하게 확인해 보세요.
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