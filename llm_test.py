# llm_test.py : ChatKHU(API Gateway) 호출이 되는지만 확인하는 진단용 (키는 출력하지 않음)
import tomllib, requests

with open(".streamlit/secrets.toml", "rb") as f:
    s = tomllib.load(f)

print("secrets에 있는 이름:", list(s.keys()))   # 값은 출력하지 않음
key = s.get("KHU_LLM_KEY")
model = s.get("KHU_LLM_MODEL", "(없음 → 기본값 claude-sonnet-5 사용됨)")
print("모델:", model)
print("키 입력 여부:", bool(key), "/ 키 길이:", len(key) if key else 0)

URL = "https://factchat-cloud.mindlogic.ai/v1/gateway/chat/completions/"
body = {"model": s.get("KHU_LLM_MODEL", "claude-sonnet-5"),
        "messages": [{"role": "user", "content": "깻잎은 안정적이에요 라는 문장을 자연스럽게 한 문장으로 바꿔줘"}],
        "max_tokens": 1500}
res = requests.post(URL, headers={"Authorization": f"Bearer {key}",
                                  "Content-Type": "application/json"},
                    json=body, timeout=15)
print("HTTP 상태:", res.status_code)
print("응답:", res.text[:800])

