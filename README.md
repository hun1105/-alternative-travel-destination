# Plan B 관광 API 모듈

웹 구현 전에 한국관광공사 국문 관광정보 OpenAPI 연결을 검증하기 위한 Python 패키지다.

현재 공공데이터포털 명세인 `KorService2`와 `*2` 오퍼레이션을 사용한다.

## 구현 API

- 지역·시군구 코드 조회
- 관광 분류 코드 조회
- 지역 기반 관광정보 조회
- 위치 기반 관광정보 조회
- 키워드 검색
- 행사 날짜 검색
- 공통 상세정보
- 소개 상세정보
- 반복 상세정보
- 이미지 상세정보
- 추천 엔진용 상세 묶음 조회

## 1. 설치

```powershell
cd outputs\plan_b_api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

`.env`의 `KTO_SERVICE_KEY`에 공공데이터포털 인증키를 입력한다. Encoding 키와 Decoding 키를 모두 처리한다.

기상청 단기예보 활용신청 후 `.env`에 키를 추가한다.

```text
KMA_SERVICE_KEY=발급받은_일반_인증키
```

실제 보행 경로를 사용하려면 SK open API에서 TMAP 보행자 경로 안내
상품을 신청하고 앱 키를 추가한다.

```text
TMAP_APP_KEY=발급받은_TMAP_앱키
```

## 2. 연결 확인

지역 코드:

```powershell
python -m plan_b_api.cli areas
```

윈도우 터미널에서 한글이 깨지면 `python` 뒤에 `-X utf8`을 추가한다.

키워드 검색:

```powershell
python -m plan_b_api.cli keyword "경복궁" --rows 5
```

현재 위치 주변 검색:

```powershell
python -m plan_b_api.cli nearby --x 126.9770 --y 37.5796 --radius 2000
```

행사 검색:

```powershell
python -m plan_b_api.cli festivals --start 20260801 --end 20260831
```

상세 묶음:

```powershell
python -m plan_b_api.cli detail 콘텐츠ID 콘텐츠타입ID
```

상세정보 정규화:

```powershell
python -X utf8 -m plan_b_api.cli normalize 126508 12
```

실제 주변 후보 추천:

```powershell
python -X utf8 -m plan_b_api.cli recommend-nearby `
  --x 126.9767 --y 37.5760 `
  --radius 1500 --rows 2 `
  --arrival 2026-07-29T14:00 `
  --weather-severity 0.3 `
  --party-size 2 --max-walking-minutes 15
```

실행 후 우선순위 번호를 높은 순서대로 입력한다.
예: `2,3,5`는 이동시간→혼잡 회피→보행 부담 순서다.

API 호출 최적화 추천:

```powershell
python -X utf8 -m plan_b_api.cli recommend-nearby-optimized `
  --x 126.9767 --y 37.5760 `
  --radius 3000 --search-rows 20 `
  --eligible-count 3 --max-detail-calls 12 `
  --arrival 2026-07-29T15:00 `
  --party-size 2 --max-walking-minutes 15
```

`TMAP_APP_KEY`가 있으면 실제 보행 거리·시간을 자동 반영한다.
키가 없거나 API가 실패하면 직선거리 추정으로 계속 실행한다.
강제로 선택하려면 `--route-mode tmap|estimated|auto`를 사용한다.

같은 `TMAP_APP_KEY`로 자동차 경로도 조회한다. 도보 15분 초과 후보는
자동차 30분 이내면 차량 후보로 복구한다. 차량도 불가능하면 서울시
대중교통을 확인한다. `--car-mode auto|tmap|off`로 제어한다.

도보 경로가 15분을 초과하면 서울시 버스·지하철 환승경로를 조회한다.
대중교통 30분 이내면 후보를 복구하고, 초과하면 강제 제외한다.
`--transit-mode auto|seoul|off`로 동작을 선택한다.
추천 1회 최대 호출은 `--max-seoul-transit-calls 3`으로 제한한다.

관광지와 함께 쇼핑 유형 `38`, 음식점 유형 `39`를 기본 후보로 포함한다.
숙박 유형 `32`는 제외한다.

음식점만 검색하려면 관광공사 음식점 유형 `39`를 지정한다.

```powershell
python -X utf8 -m plan_b_api.cli recommend-nearby-optimized `
  --x 126.9767 --y 37.5760 `
  --content-type-id 39
```

쇼핑만 검색하려면 `--content-type-id 38`을 사용한다.

보행 경로만 확인:

```powershell
python -X utf8 -m plan_b_api.cli walking-route `
  --start-x 126.9767 --start-y 37.5760 `
  --end-x 126.9770 --end-y 37.5738 `
  --end-name "대한민국역사박물관"
```

도보·자동차 경로 동시 확인:

```powershell
.\도보_자동차_경로_확인.ps1
```

자동차 경로만 확인:

```powershell
python -X utf8 -m plan_b_api.cli car-route `
  --start-x 126.9767 --start-y 37.5760 `
  --end-x 127.0095 --end-y 37.5665 --end-name "DDP"
```

서울시 대중교통 경로만 확인:

```powershell
python -X utf8 -m plan_b_api.cli seoul-transit-route `
  --start-x 126.9767 --start-y 37.5760 `
  --end-x 127.0095 --end-y 37.5665
```

[ODsay](https://lab.odsay.com) 회원가입 후 애플리케이션을 등록하면
API 키를 발급받는다. 발급받은 키를 `.env`의 `ODSAY_API_KEY`에
저장한다.

(참고: 공공데이터포털의 `서울특별시_대중교통환승경로 조회 서비스`는
승인이 되어도 실제 `ws.bus.go.kr` 서버에서 키를 인식하지 못하는
문제가 있어 ODsay로 교체했다.)

환승경로는 30분 캐시한다. 대중교통 경로의 보행시간도 15분 제한을
넘으면 제외한다. 보행시간이 응답에 없으면 미확인으로 표시한다.

상세정보와 날씨를 SQLite에 캐시하고, 적격 후보 3개가 모이면 상세 조회를
중단한다. 중복 콘텐츠를 제거하며 API 실패 시 만료 캐시를 임시 사용한다.

다음 고정 일정이 있으면 후보 방문 이후 도착 가능성까지 검사한다.

```powershell
.\다음_일정_도착_테스트.ps1
```

계산식:

```text
필요시간 = 현재→후보 이동 + 후보 체류 + 후보→다음 일정 이동 + 안전 여유
필요시간 ≤ 다음 일정까지 남은 시간인 후보만 유지
```

직접 입력할 때는 `--next-x`, `--next-y`, `--next-arrival`을 함께 쓴다.
`--visit-minutes`는 후보 체류시간, `--schedule-buffer-minutes`는 지연 대비
여유시간이다. 다음 구간은 TMAP 도보를 먼저 확인하고, 도보 15분을 넘으면
서울시 대중교통을 확인한다. 차량 경로는 이후 단계에서 연결한다.

추천 결과에는 다음 설명이 함께 표시된다.

- 판단 신뢰도: 정규화 완성도 60% + 핵심 판단정보 확보율 40%
- 신뢰도 감점: `(100%-신뢰도)×15점`, 최대 15점
- 최종 점수: 기본 점수에서 신뢰도 감점을 차감
- 운영 확인: 운영 확인·미운영 확인·미확인
- 실제 데이터: API에서 직접 확인한 값
- 추정 데이터: 직선거리·콘텐츠 유형으로 계산한 값
- 중립 처리: 정보가 없어 0.5 등 기본값을 적용한 항목
- 상세정보 출처: 실시간 API·유효 캐시·만료 캐시
- 항목별 기여: 각 적합도와 가중치가 만든 실제 점수

기상청 날씨만 확인:

```powershell
python -X utf8 -m plan_b_api.cli weather --x 126.9767 --y 37.5760 --at 2026-07-29T15:00
```

실제 추천에서 `--weather-severity`를 생략하면 기상청 예보를 자동
호출한다. 수동 테스트가 필요할 때만 `--weather-severity 0.8`처럼
입력한다.

## 3. Python에서 사용

## 지도 선택 기반 여행 계획

사용자는 여행 제목·날짜·체류시간만 입력한다. 장소명·주소·좌표·카테고리·
장소 ID는 TMAP 검색 결과의 `선택` 버튼으로 자동 채운다.

장소 검색:

```powershell
.\장소검색_확인.ps1
```

여행 계획 JSON 검증:

```powershell
.\여행계획_JSON_검증.ps1
```

웹 API:

```text
GET  /place-search?q=경복궁&center_x=126.9767&center_y=37.5760&count=5
POST /trip-plans/validate
```

`GET /place-search`의 항목은 여행 계획 `schedules[].place`에 그대로 넣는다.
예시 규격은 `examples/trip_plan_example.json`에 있다.

`start_time`과 예약 일정의 `fixed_arrival_time`은 선택값이다. 위치 좌표는
사용자에게 입력받지 않고 TMAP 검색 결과에서만 저장한다.

```python
from plan_b_api import KTOClient
from plan_b_api.cli import load_env_file

load_env_file()

with KTOClient.from_env() as client:
    nearby = client.location_based_list(
        map_x=126.9770,
        map_y=37.5796,
        radius=2000,
        num_of_rows=10,
    )

for place in nearby.items:
    print(place.get("title"), place.get("contentid"))
```

## 4. 사용자별 동적 점수

우선순위는 `1=최우선`, `5=최하위`다.
원하는 항목만 1~5개 선택한다.
미선택 일반 항목에는 남은 점수를 동일하게 배분한다.
아동 적합은 사용자가 직접 우선순위로 선택한 경우에만 반영한다.

```python
from plan_b_api import CandidateSignals, UserPriorities, score_candidate

signals = CandidateSignals(
    weather_fit=0.9,
    route_time=0.7,
    crowd_avoidance=0.4,
    budget_fit=1.0,
    child_fit=0.9,
    walking_fit=0.7,
    data_confidence=0.9,
)

priorities = UserPriorities.from_order(
    "route_time",
    "child_fit",
)

result = score_candidate(signals, priorities)
print(result.total_score)
print(result.reasons)
```

동작 예제:

```powershell
python -X utf8 -m examples.priority_scoring_example
```

휴무, 안전, 도보 15분, 고정 일정 도착, 필수 접근성은 점수보다 먼저 적용한다.

선택 순서별 배점은 `32, 23, 17, 13, 10점`이다.
데이터 신뢰도는 별도 5점이다.
미선택 일반 항목은 남은 점수를 균등 배분한다.
미선택 아동 적합의 가중치는 0점이다.
예산 적합은 현재 점수와 필수 제한에서 제외한다.

## 5. 실제 데이터에서 점수 입력 자동 생성

`NormalizedPlace`에 경로·날씨 정보를 결합한다.

```python
from datetime import datetime
from plan_b_api import (
    CandidateFacts, TripContext, UserPriorities,
    evaluate_place_candidate,
)

trip = TripContext(
    arrival_time=datetime(2026, 7, 29, 14, 0),
    party_size=3,
    weather_severity=0.9,
    max_walking_minutes=15,
    max_transport_minutes=30,
)
facts = CandidateFacts(
    indoor_ratio=0.9,
    route_minutes=10,
    walking_meters=500,
    crowd_level=0.3,
)
result = evaluate_place_candidate(place, facts, trip, UserPriorities())
print(result.build.signals)
print(result.score.total_score)
```

실행 예제:

```powershell
python -X utf8 -m examples.automatic_signal_example
```

실행하면 우선순위 번호를 1~5개 입력받는다.

`weather_severity`, 비율형 입력은 `0.0~1.0`이다.
예산 정보는 보관하지만 현재 점수에는 반영하지 않는다.

도보 경로가 15분을 초과하면 후보를 강제 제외한다.
단, 서울시 버스·지하철 경로가 있으면 대중교통 후보로 다시 평가한다.
대중교통 30분 초과 후보는 강제 제외한다. 자차 경로는 현재 제외한다.
마지막 입장시간은 폐장 전 매표·입장이 끝나는 장소를 걸러내기 위해 사용한다.

## 6. 테스트

```powershell
python -m unittest discover -s tests -v
```

테스트는 실제 인증키, 외부 패키지, 네트워크를 사용하지 않는다.

### 브라우저 자동 테스트 (선택)

`web/index.html`의 핵심 화면 흐름(검색·추가, 드래그 순서변경,
잠금, 지도 표시 토글, 저장·공유 링크 재진입 등)을 실제 Chromium으로
확인한다. `.env`의 실제 인증키와 네트워크가 필요하다.

```powershell
pip install -e ".[e2e]"
python -m playwright install chromium
python -m unittest discover -s tests_e2e -v
```

## 7. JSON API 서버

서버 실행:

```powershell
cd "C:\Users\hun\Documents\Codex\2026-07-28\files-mentioned-by-the-user-2026\outputs\plan_b_api"
.\서버_실행.ps1
```

다른 PowerShell 창에서 확인:

```powershell
cd "C:\Users\hun\Documents\Codex\2026-07-28\files-mentioned-by-the-user-2026\outputs\plan_b_api"
.\API_확인.ps1
```

직접 실행할 수도 있다.

```powershell
python -X utf8 -m plan_b_api.server --host 127.0.0.1 --port 8000
```

엔드포인트:

```text
GET  /health
GET  /priorities
GET  /weather
GET  /walking-route
GET  /car-route?start_x=126.9767&start_y=37.5760&end_x=127.0095&end_y=37.5665
GET  /seoul-transit-route?start_x=126.9767&start_y=37.5760&end_x=127.0095&end_y=37.5665
GET  /seoul-crowd?area=경복궁
GET  /seoul-crowd?x=126.9767&y=37.5760
GET  /crowd?name=경복궁
GET  /crowd?poi_id=1172091
GET  /places/{content_id}?content_type_id=12
POST /recommendations
```

## 8. MVP 시나리오 테스트

```powershell
cd "C:\Users\hun\Documents\Codex\2026-07-28\files-mentioned-by-the-user-2026\outputs\plan_b_api"
.\시나리오_테스트.ps1
```

직접 실행:

```powershell
python -X utf8 -m plan_b_api.scenario_runner
```

검증 항목:

```text
폭염, 비, 야간, 휴무일, 종료 행사
도보 15분 초과, 아동 동반, 도보 부담
API 장애 복구, 반복 호출 캐시
```

## 실제 추천값의 범위

- 실제값: 관광지명, 콘텐츠 ID·타입, 직선거리, 운영시간, 휴무일, 요금
- 축제값: 행사 시작·종료일, 회차 시각, 사전예약 여부
- 추정값: 직선거리 기반 이동시간, 명칭·유형 기반 실내 비율
- 중립값: 서울시 영역 밖이거나 혼잡도 호출에 실패한 후보
- 주변 지역 혼잡도: 서울시 공식 121개 영역의 실시간·예측 인구 혼잡도 적용

## 서울시 실시간 혼잡도

`.env`에 서울 열린데이터광장 인증키를 입력한다.

```text
SEOUL_OPEN_API_KEY=발급받은_인증키
```

좌표로 단독 확인:

```powershell
python -X utf8 -m plan_b_api.cli seoul-crowd --x 126.9767 --y 37.5760
```

추천 연결:

```powershell
python -X utf8 -m plan_b_api.cli recommend-nearby-optimized `
  --x 126.9767 --y 37.5760 `
  --crowd-mode auto --max-seoul-crowd-calls 10
```

- 우선순위: 서울시 영역 혼잡도 → 중립값
- 좌표 매칭: 서울시 공식 121개 영역 폴리곤
- 목표 시각 예측값: 가장 가까운 1시간 이내 예측값
- 실시간·예측 캐시: 5분
- 후보가 같은 영역이면 API 1회 공유
- 점수 반영: 영역 혼잡도 60% + 중립값 40%
- 화면에 `영역별 데이터이므로 개별 장소·건물과 다를 수 있음`을 항상 표시
- 영역 단위 혼잡도이며 개별 건물 내부 혼잡도는 아님

## SK 장소 혼잡도 단독 도구

추천 엔진에서는 사용하지 않는다. 기존 단독 확인 도구만 유지한다.

```text
SK_CROWD_APP_KEY=발급받은_앱키
```

직접 확인:

```powershell
python -X utf8 -m plan_b_api.cli crowd --name "경복궁"
```

이 명령은 개발 확인용이며 추천 점수에는 연결되지 않는다.

## 주의사항

- `KTO_MOBILE_APP=PlanB`를 실제 서비스 고유명과 일치시킨다.
- 개발계정 호출량을 아끼기 위해 상세 조회는 후보를 줄인 뒤 실행한다.
- 원본 응답을 수정하지 말고 정규화 데이터와 분리해 저장한다.
- 운영 전 공공데이터포털에서 운영계정을 신청한다.
- API 키를 프론트엔드 코드에 넣지 않는다.
