# debug_api.py : API가 실제로 뭘 돌려주는지 확인하는 진단용 (키는 화면에 출력하지 않음)
import tomllib, requests, pandas as pd
from datetime import datetime

with open(".streamlit/secrets.toml", "rb") as f:
    key = tomllib.load(f)["DATA_GO_KR_KEY"]

print("내 컴퓨터 오늘 날짜:", datetime.now().strftime("%Y-%m-%d"))

URL = "https://apis.data.go.kr/B552845/risesAndFalls/info"
params = {"serviceKey": key, "returnType": "JSON", "pageNo": 1, "numOfRows": 5,
          "cond[exmn_ymd::EQ]": datetime.now().strftime("%Y%m%d"),
          "cond[ctgry_cd::EQ]": "200"}
res = requests.get(URL, params=params, timeout=15)
print("HTTP 상태:", res.status_code)
print("응답 앞부분:", res.text[:1500])

# 엑셀 쪽 품목코드 모양도 같이 확인
meta = pd.read_excel("품목58선_최종_통합본.xlsx")  # 실제 파일명으로
print("엑셀 컬럼:", list(meta.columns))
print("품목코드 예시:", meta["품목코드"].head(5).tolist())

from datetime import datetime, timedelta
for back in range(0, 8):
    d = (datetime.now() - timedelta(days=back)).strftime("%Y%m%d")
    line = [d]
    for c in ["100", "200", "300", "400", "600"]:
        p = {"serviceKey": key, "returnType": "JSON", "pageNo": 1, "numOfRows": 3,
             "cond[exmn_ymd::EQ]": d, "cond[ctgry_cd::EQ]": c}
        b = requests.get(URL, params=p, timeout=15).json()["response"]["body"]
        line.append(f"{c}:{b.get('totalCount')}")
    print(" ".join(line))

# 데이터가 있는 날짜 하나의 실제 응답 모양
p = {"serviceKey": key, "returnType": "JSON", "pageNo": 1, "numOfRows": 2,
     "cond[exmn_ymd::EQ]": "20261026", "cond[ctgry_cd::EQ]": "200"}
print(requests.get(URL, params=p, timeout=15).text[:1500])
