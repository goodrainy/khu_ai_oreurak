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
MAX_CARDS = 9
APP_URL = "https://khuaioreurak-jyje7pzwepyquuajjurvvn.streamlit.app/"

GRADE_LABEL_MAP = {
    '1등급(최선)': '✅ 적극 추천',
    '2등급(차선)': '🔄 참고 추천',
    '3등급(최후)': '🚫 대체 어려움',
    '정상': '-',
}
ALERT_LABEL_MAP = {
    '급등': '🔴 급등',
    '1차 경고': '🟡 1차 경고',
    '정상': '정상',
}
ALERT_TYPE_DESC = {
    '급등 시': "선택하신 품목의 가격이 20% 이상 급등했을 때만 알려드려요.",
    '추천 포함': "1차 경고(10% 이상)부터 급등까지 모두 알려드리고, 대체 식품 추천까지 함께 보내드려요.",
}

# =========================================================
# [Part 2] 핵심 엔진 (알고리즘 및 통신)
# =========================================================

def send_telegram_message(token, chat_id, message):
    """전송 성공 여부(True/False)와 상세 메시지를 반환"""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': str(chat_id), 'text': message, 'parse_mode': 'HTML'}
    try:
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code == 200:
            return True, "전송 성공"
        else:
            return False, f"{response.status_code} - {response.text}"
    except Exception as e:
        return False, f"요청 중 오류: {e}"

def run_grade_logic(df, group_col='조리 용도', name_col='품목명',
                    warning_threshold=10.0, surge_threshold=20.0, fallback_top_n=5):
    df = df.copy()
    df[group_col] = df[group_col].astype(str).str.strip()
    df[name_col] = df[name_col].astype(str).str.strip()
    df['recommend_grade'] = '정상'
    df['recommend_target'] = ''
    df['reason'] = ''

    df['score'] = (df['rate_1d'] * 0.4) + (df['rate_1m'] * 0.6)

    df['is_surge'] = df['score'] >= surge_threshold
    df['is_warning'] = (df['score'] >= warning_threshold) & (df['score'] < surge_threshold)
    df['alert_level'] = '정상'
    df.loc[df['is_warning'], 'alert_level'] = '1차 경고'
    df.loc[df['is_surge'], 'alert_level'] = '급등'

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
            # [변경] 조건 설명 문구 추가
            desc = ALERT_TYPE_DESC.get(alert_type, "")
            msg = (f"✅ <b>[오르락내리락] 구독 완료!</b>\n\n"
                   f"📍 <b>대상 품목:</b> {item_name}\n"
                   f"🔔 <b>알림 조건:</b> {alert_type}\n"
                   f"ℹ️ {desc}\n\n"
                   f"앞으로 설정하신 조건에 맞춰 안내해 드릴게요! 🚀")
            send_telegram_message(TELEGRAM_TOKEN, chat_id, msg)
            return True
        return False
    except Exception as e:
        print(f"Subscription Error: {e}")
        return False

def build_alert_message(row, result_df, base_date):
    """추천 카드와 동일한 구성으로 텔레그램 알림 메시지를 만듦 (ChatKHU 이유 포함)"""
    item = row['품목명']
    alert_badge = "🔴 급등" if row['is_surge'] else ("🟡 1차 경고" if row['is_warning'] else "📈 상승 상위")
    grade_badge = GRADE_LABEL_MAP.get(row['recommend_grade'], row['recommend_grade'])
    targets = [t.strip() for t in str(row['recommend_target']).split(',') if t.strip()]
    target_display = ", ".join(targets) if targets else row['recommend_target']
    date_str = f"{base_date[:4]}-{base_date[4:6]}-{base_date[6:]}" if base_date else "확인 불가"

    lines = [
        f"{alert_badge} <b>[오르락내리락] {item} 알림</b>",
        "",
        f"📅 기준일: {date_str}",
        f"📈 1일 등락률: {row['rate_1d']:.1f}%",
        f"📈 1개월 등락률: {row['rate_1m']:.1f}%",
        "",
        f"{grade_badge}: <b>{target_display}</b>",
    ]

    # 절감액 (1순위 대체재 기준)
    if targets:
        item_price = row.get('current_price', 0) or 0
        t_row = result_df[result_df['품목명'] == targets[0]]
        if not t_row.empty and item_price > 0:
            target_price = t_row.iloc[0].get('current_price', 0) or 0
            if target_price > 0:
                diff = item_price - target_price
                if diff > 0:
                    lines.append(f"💰 {targets[0]}로 바꾸면 약 {diff:,.0f}원 절감돼요")

    # ChatKHU 이유
    reason = card_message(row, result_df)
    lines.append("")
    lines.append(f"💬 {reason}")

    lines.append("")
    lines.append(f"🔗 자세히 보기: {APP_URL}")

    return "\n".join(lines)

def send_daily_alerts(result_df, base_date):
    """구독자 목록을 돌면서 조건에 맞으면 텔레그램 발송. 관리자 테스트 버튼에서 호출."""
    results = []
    if not os.path.exists(SUBSCRIBERS_FILE):
        return results
    with open(SUBSCRIBERS_FILE, 'r') as f:
        subscribers = json.load(f)

    for sub in subscribers:
        item = sub.get('item')
        chat_id = sub.get('chat_id')
        alert_type = sub.get('alert_type')

        row_match = result_df[result_df['품목명'] == item]
        if row_match.empty:
            continue
        row = row_match.iloc[0]

        should_send = False
        if alert_type == '급등 시' and row['is_surge']:
            should_send = True
        elif alert_type == '추천 포함' and (row['is_surge'] or row['is_warning']):
            should_send = True

        if not should_send:
            continue

        msg = build_alert_message(row, result_df, base_date)
        ok, detail = send_telegram_message(TELEGRAM_TOKEN, chat_id, msg)
        results.append({'item': item, 'ok': ok, 'detail': detail})

    return results

# =========================================================
# [Part 3] Streamlit UI 구성
# =========================================================

def render_card(row, result_df):
    item_name = row['품목명']
    with st.container(border=True):
        if row['is_surge']:
            alert_badge = "🔴 급등"
        elif row['is_warning']:
            alert_badge = "🟡 1차 경고"
        else:
            alert_badge = "📈 상승 상위"
        st.markdown(f"##### {alert_badge} · {item_name}")

        targets = [t.strip() for t in str(row['recommend_target']).split(',') if t.strip()]
        target_display = ", ".join(targets) if targets else row['recommend_target']

        if row['recommend_grade'] == '1등급(최선)':
            st.success(f"✅ 적극 추천: **{target_display}**")
        elif row['recommend_grade'] == '2등급(차선)':
            st.warning(f"🔄 참고 추천: **{target_display}**")
        else:
            st.error(f"🚫 대체 어려움 → {target_display}")

        if targets:
            item_price = row.get('current_price', 0) or 0
            t_row = result_df[result_df['품목명'] == targets[0]]
            if not t_row.empty and item_price > 0:
                target_price = t_row.iloc[0].get('current_price', 0) or 0
                if target_price > 0:
                    diff = item_price - target_price
                    if diff > 0:
                        st.caption(f"💰 {targets[0]}로 바꾸면 약 {diff:,.0f}원 절감돼요")

        reason_key = f"reason_{item_name}"
        chart_key = f"chart_{item_name}"
        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            if st.button("💬 이유 보기", key=f"btn_reason_{item_name}"):
                st.session_state[reason_key] = card_message(row, result_df)
                st.rerun()
        with btn_col2:
            if targets:
                chart_label = "📊 그래프 숨기기" if st.session_state.get(chart_key) else "📊 그래프 보기"
                if st.button(chart_label, key=f"btn_chart_{item_name}"):
                    st.session_state[chart_key] = not st.session_state.get(chart_key, False)
                    st.rerun()

        if st.session_state.get(reason_key):
            st.caption(f"이유: {st.session_state[reason_key]}")

        if targets and st.session_state.get(chart_key):
            chart_df = pd.DataFrame(index=['1일 등락률(%)', '1개월 등락률(%)'])
            chart_df[item_name] = [row['rate_1d'], row['rate_1m']]
            for t in targets:
                t_row = result_df[result_df['품목명'] == t]
                if not t_row.empty:
                    tr = t_row.iloc[0]
                    chart_df[t] = [tr['rate_1d'], tr['rate_1m']]
            st.bar_chart(chart_df)

        if targets:
            basis = result_df.loc[result_df['품목명'] == targets[0], 'price_basis']
            if not basis.empty and basis.iloc[0] == '중도매':
                st.caption("*중도매가 기준이므로 소매가와 다를 수 있습니다*")


def main():
    st.title("🍎 오르락내리락")
    st.caption("실시간 식재료 가격 변동 및 맞춤형 대체재 추천 서비스")

    master_df, base_date, warn_msg = load_master_data(METADATA_FILE)
    master_df['품목명'] = master_df['품목명'].astype(str).str.strip()
    result_df = run_grade_logic(master_df)
    result_df['recommend_label'] = result_df['recommend_grade'].map(GRADE_LABEL_MAP).fillna(result_df['recommend_grade'])
    result_df['alert_label'] = result_df['alert_level'].map(ALERT_LABEL_MAP).fillna(result_df['alert_level'])

    st.sidebar.header("📂 메뉴")
    menu = st.sidebar.radio("이동하기", ["🏠 홈 (대시보드)", "📖 서비스 소개", "🔔 챗봇 구독하기"])

    with st.sidebar.expander("🛠️ 관리자 테스트"):
        if st.button("지금 바로 데일리 알림 테스트"):
            test_results = send_daily_alerts(result_df, base_date)
            if not test_results:
                st.info("조건에 맞는 구독/알림 대상이 없습니다.")
            else:
                for r in test_results:
                    if r['ok']:
                        st.success(f"[{r['item']}] 발송 성공")
                    else:
                        st.error(f"[{r['item']}] 발송 실패 - {r['detail']}")

    if menu == "🏠 홈 (대시보드)":
        if base_date:
            st.caption(f"📅 데이터 기준일: {base_date[:4]}-{base_date[4:6]}-{base_date[6:]}")

        st.subheader("💡 오늘의 추천 카드")

        recommend_items = result_df[result_df['recommend_target'] != '']
        surge_pool = recommend_items[recommend_items['is_surge']].sort_values(by='score', ascending=False)
        warning_pool = recommend_items[recommend_items['is_warning']].sort_values(by='score', ascending=False)

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
        if warn_msg:
            st.caption(warn_msg)

        display_cols = ['품목명', '조리 용도', '급등 원인 세부 유형', 'price_basis',
                         'rate_1d', 'rate_1w', 'rate_1m', 'alert_label', 'recommend_label']
        display_cols = [c for c in display_cols if c in result_df.columns]
        st.dataframe(
            result_df[display_cols].rename(columns={'alert_label': 'alert_level', 'recommend_label': 'recommend_grade'}),
            use_container_width=True
        )

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

카드의 "📊 그래프 보기"를 누르면 급등 품목과 대체 품목의 등락률을 나란히 비교하는 그래프가 뜨고, "💬 이유 보기"를 누르면 AI가 그 자리에서 바로 설명을 만들어드려요. 둘 다 필요할 때만 눌러서 보는 방식이라 화면이 깔끔하고 더 빨라요.

챗봇으로 받는 알림도 똑같은 구성으로 발송돼요.
        """)

        st.subheader("📋 전체 품목 현황 표는 이렇게 읽어요")
        st.write("""
- **품목명 / 조리 용도**: 어떤 식재료이고 어떤 용도로 분류되는지
- **급등 원인 세부 유형**: 가격 변동의 성격(계절적 요인 등)
- **price_basis**: 가격 기준이 소매인지 중도매인지
- **rate_1d / rate_1w / rate_1m**: 1일·1주·1개월 전 대비 등락률(%)
- **alert_level**: 정상 / 1차 경고 / 급등 (품목 자체의 상태)
- **recommend_grade**: 적극 추천 / 참고 추천 / 대체 어려움 (추천 품질)
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
        user_id = st.text_input(
            "본인의 텔레그램 Chat ID를 입력하세요",
            value="",
            placeholder="예: 123456789"
        )

        item_options = sorted(master_df['품목명'].unique())
        selected_items = st.multiselect("알림을 받고 싶은 품목을 선택하세요", item_options)
        st.caption(f"총 {len(item_options)}개 품목 중에서 선택할 수 있어요.")

        alert_option = st.radio(
            "어떤 상황에서 알림을 받을까요?",
            ["🚨 품목 가격이 급등할 때만", "💡 추천 제품이 떴을 때도 포함"],
            index=0
        )
        st.caption(ALERT_TYPE_DESC["급등 시"] if alert_option.startswith("🚨") else ALERT_TYPE_DESC["추천 포함"])

        option_map = {
            "🚨 품목 가격이 급등할 때만": "급등 시",
            "💡 추천 제품이 떴을 때도 포함": "추천 포함"
        }

        if st.button("구독 신청하기"):
            if not user_id.strip():
                st.warning("Chat ID를 입력해주세요.")
            elif selected_items:
                for item in selected_items:
                    subscribe_item(user_id, item, option_map[alert_option])
                st.success(f"{len(selected_items)}개 품목 구독이 완료되었습니다!")
            else:
                st.warning("품목을 선택해주세요.")

if __name__ == "__main__":
    main()