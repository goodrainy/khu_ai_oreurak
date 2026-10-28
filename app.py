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

TELEGRAM_TOKEN = st.secrets["TELEGRAM_TOKEN"]
MY_CHAT_ID = st.secrets["MY_CHAT_ID"]
SUBSCRIBERS_FILE = 'subscribers.json'
METADATA_FILE = '품목58선_최종_통합본.xlsx'
MAX_CARDS = 9  # 추천 카드 최대 표시 개수

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

    # 변동 점수: 1일 40% + 1개월 60%
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

        candidates = df[(df[group_col] == target_group) & (df.index != idx)]
        if 'has_data' in candidates.columns:
            candidates = candidates[candidates['has_data']]

        if candidates.empty:
            grade, target = '3등급(최후)', '메뉴 변경 권장'
            reason = f"[{tag}] [{target_group}] 내 대체 품목 없음"
        else:
            # [변경] 대체재를 최대 2개까지 추천 (안정적인 순서대로)
            best_candidates = candidates.sort_values(by='score').head(2)
            target = ", ".join(best_candidates[name_col].tolist())
            top_score = best_candidates.iloc[0]['score']

            if top_score < surge_threshold * 0.5:
                grade = '1등급(최선)'
                reason = f"[{tag}] {target}는 추세가 비교적 안정적입니다."
            else:
                grade = '2등급(차선)'
                reason = f"[{tag}] {target}도 상승 중이나 상대적으로 완만합니다."

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

def render_card(row, result_df):
    """추천 카드 1장을 그려줌. AI 이유는 버튼을 눌렀을 때만 호출."""
    with st.container(border=True):
        if row['is_surge']:
            alert_badge = "🔴 급등"
        elif row['is_warning']:
            alert_badge = "🟡 1차 경고"
        else:
            alert_badge = "📈 상승 상위"
        st.markdown(f"##### {alert_badge} · {row['품목명']}")

        targets = [t.strip() for t in str(row['recommend_target']).split(',') if t.strip()]
        target_display = ", ".join(targets) if targets else row['recommend_target']

        if row['recommend_grade'] == '1등급(최선)':
            st.success(f"✅ 적극 추천: **{target_display}**")
        elif row['recommend_grade'] == '2등급(차선)':
            st.warning(f"🔄 참고 추천: **{target_display}**")
        else:
            st.error(f"🚫 대체 어려움 → {target_display}")

        # [요구사항 7] 절감액 안내 (1순위 대체재 기준)
        if targets:
            item_price = row.get('current_price', 0) or 0
            t_row = result_df[result_df['품목명'] == targets[0]]
            if not t_row.empty and item_price > 0:
                target_price = t_row.iloc[0].get('current_price', 0) or 0
                if target_price > 0:
                    diff = item_price - target_price
                    if diff > 0:
                        st.caption(f"💰 {targets[0]}로 바꾸면 약 {diff:,.0f}원 절감돼요")

        # [요구사항 3] 이유는 버튼을 눌렀을 때만 ChatKHU 호출
        reason_key = f"reason_{row['품목명']}"
        if st.session_state.get(reason_key):
            st.caption(f"이유: {st.session_state[reason_key]}")
        else:
            if st.button("💬 이유 보기", key=f"btn_{row['품목명']}"):
                st.session_state[reason_key] = card_message(row, result_df)
                st.rerun()

        if targets:
            basis = result_df.loc[result_df['품목명'] == targets[0], 'price_basis']
            if not basis.empty and basis.iloc[0] == '중도매':
                st.caption("*중도매가 기준이므로 소매가와 다를 수 있습니다*")


def main():
    st.title("🍎 오르락내리락")
    st.caption("실시간 식재료 가격 변동 및 맞춤형 대체재 추천 서비스")

    master_df, base_date, warn_msg = load_master_data(METADATA_FILE)
    if base_date:
        st.caption(f"📅 데이터 기준일: {base_date[:4]}-{base_date[4:6]}-{base_date[6:]}")
    if warn_msg:
        st.warning(warn_msg)
    result_df = run_grade_logic(master_df)

    st.sidebar.header("📂 메뉴")
    menu = st.sidebar.radio("이동하기", ["🏠 홈 (대시보드)", "📖 서비스 소개", "🔔 챗봇 구독하기"])

    if menu == "🏠 홈 (대시보드)":
        st.subheader("💡 오늘의 추천 카드")

        recommend_items = result_df[result_df['recommend_target'] != '']
        surge_pool = recommend_items[recommend_items['is_surge']].sort_values(by='score', ascending=False)
        warning_pool = recommend_items[recommend_items['is_warning']].sort_values(by='score', ascending=False)

        # [요구사항 1, 2] 급등 우선 채우고, 부족하면 경고에서 채움 (최대 9개)
        show_surge = surge_pool.head(MAX_CARDS)
        remain = MAX_CARDS - len(show_surge)
        show_warning = warning_pool.head(remain) if remain > 0 else warning_pool.iloc[0:0]

        total_count = len(recommend_items)
        shown_count = len(show_surge) + len(show_warning)

        if shown_count == 0:
            st.info("현재 추천할 수 있는 품목이 없습니다.")
        else:
            if total_count > shown_count:
                st.caption(f"⚠️ 현재 {total_count}개 품목에 변동이 있어요. 변동이 큰 상위 {shown_count}개만 보여드려요. (전체 목록은 아래 표에서 확인 가능)")

            if not show_surge.empty:
                st.markdown("#### 🔴 급등 품목")
                cols = st.columns(3)
                for idx, (_, row) in enumerate(show_surge.iterrows()):
                    with cols[idx % 3]:
                        render_card(row, result_df)

            if not show_warning.empty:
                st.markdown("#### 🟡 1차 경고 품목")
                cols = st.columns(3)
                for idx, (_, row) in enumerate(show_warning.iterrows()):
                    with cols[idx % 3]:
                        render_card(row, result_df)

        st.divider()

        st.subheader("📊 실시간 급등 품목 및 전체 현황")

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
        # [요구사항 4] 필요한 컬럼만 선택 (없는 컬럼은 자동으로 제외)
        display_cols = ['품목명', '조리 용도', '급등 원인 세부 유형', 'price_basis',
                         'rate_1d', 'rate_1w', 'rate_1m', 'alert_level', 'recommend_grade']
        display_cols = [c for c in display_cols if c in result_df.columns]
        st.dataframe(result_df[display_cols], use_container_width=True)

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
        st.write("비슷한 용도로 사용할 수 있는 식품의 가격 흐름을 살펴보고, 선택할 수 있는 대안을 1~2가지 안내해 드려요.")
        st.info("""
**✅ 적극 추천**
같은 용도로 사용할 수 있는 식품 중 가격 흐름이 안정적인 품목이에요. 바로 바꿔도 좋아요.

**🔄 참고 추천**
가격이 오르고 있지만, 상대적으로 완만하게 오르는 품목이에요. 그나마 나은 선택이에요.

**🚫 대체 어려움**
같은 용도의 식품들이 모두 크게 올라 대체하기 어렵다면, 다른 메뉴를 선택하는 방법을 제안해요.
        """)

        st.divider()
        st.subheader("🗂️ 추천 카드는 이렇게 구성돼요")
        st.write("""
가격 변동이 있는 품목이 많을 땐 전부 보여드리는 대신, **급등 품목을 먼저 채우고** 자리가 남으면 **1차 경고 품목 중 변동이 큰 순서대로** 채워서 최대 9개까지만 카드로 보여드려요.
화면이 복잡해지지 않게 하기 위함이고, 그 외 품목들은 아래 "전체 품목 현황" 표에서 모두 확인할 수 있어요.

카드의 "이유 보기" 버튼을 누르면 AI가 그 자리에서 바로 설명을 만들어드려요. (미리 만들어두지 않고 필요할 때만 생성해서 더 빠르게 화면을 볼 수 있어요)
        """)

        st.subheader("📋 전체 품목 현황 표는 이렇게 읽어요")
        st.write("""
- **품목명 / 조리 용도**: 어떤 식재료이고 어떤 용도로 분류되는지
- **급등 원인 세부 유형**: 가격 변동의 성격(계절적 요인 등)
- **price_basis**: 가격 기준이 소매인지 중도매인지
- **rate_1d / rate_1w / rate_1m**: 1일·1주·1개월 전 대비 등락률(%)
- **alert_level**: 정상 / 1차 경고 / 급등 (품목 자체의 상태)
- **recommend_grade**: 적극 추천 / 참고 추천 / 대체 어려움에 해당하는 내부 등급
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