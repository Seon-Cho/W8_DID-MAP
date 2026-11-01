# W8 DID MAP — 도시는 넓어지고, 밀도는 낮아진다

스마트시티 융합기술세미나(2026-2) 과제 3.
SGIS 1km 격자 인구로 도시화지역(UN·EU·OECD 도시화 등급의 도시·준밀집지역)을 직접 추출해 2015~2024년 도시화 면적과 인구밀도의 변화를 보고,
행정안전부 인구감소지역 92곳 가운데 밀도 감소율·면적 증가율 순위에서 앞선 6곳(완도군, 청도군, 양구군, 순창군, 괴산군, 제천시)의
도시화지역 변화, 공공·교통시설 입지, 인구 이동 방향을 분석했다.
처음에는 일본식 인구집중지구(DID, 2,000·4,000명/km²)로 분석했으나 국제 기준으로 바꿨다. 저장소·변수 이름의 `did`는 이제 도시화지역을 뜻한다.
2010→2015 사이 SGIS 격자 인구 집계 기준이 바뀌어 변화 분석은 2015년부터 한다(2000년부터의 값은 참고용).

- 웹페이지: https://w8-did-map.pages.dev
- 페이지 원본: [`site/`](site/) (정적 HTML, Cloudflare Pages로 배포)

## 폴더

| 경로 | 내용 |
|---|---|
| `site/` | 웹페이지 (`index.html`, 그림 `img/*.webp`, 차트 데이터 `data.js`) |
| `scripts/did_analysis.py` | 격자 인구 → 도시화지역 추출, 시계열·시군구 표, 전국·지역 지도 |
| `scripts/fetch_facilities.py` | 학교·보건·청사·철도·터미널·고속도로 IC 위치 수집 |
| `scripts/migration_od.py` | MDIS 국내인구이동 마이크로데이터 → 시군구·읍면동 OD 집계 |
| `scripts/migration_analysis.py` | 6개 지역 이동 방향(순유출률, 바깥쪽 이동 비율), 흐름 지도 |
| `scripts/lost_did_flows.py` | 도시화지역에서 빠진 격자(빈 네모) 주민의 도착지 추정 |
| `scripts/export_webmap.py` | 동적 지도용 GeoJSON·시계열 |
| `scripts/build_site.py` | 웹페이지용 그림·데이터 만들기 |
| `results/tables/*.csv` | 결과 표 (도시화지역 요약, 시군구 밀도·면적 변화, 시설 입지, 인구 이동 요약 등) |
| `results/did_polygons.gpkg` | 도시화지역 폴리곤 (레이어 `did_{500M\|1K}_{300\|1500}_{연도}`, EPSG:5179) |

## 도시화지역 정의 (Degree of Urbanisation 1단계)

| 구분 | 격자 밀도 | 묶는 방식 | 묶음 인구 |
|---|---|---|---|
| 도시·준밀집지역 (주 분석) | 300명/km² 이상 | queen (꼭짓점 포함) | 5,000명 이상 |
| 도시 | 1,500명/km² 이상 | rook (변) | 50,000명 이상 |

주 분석은 1km 격자. SGIS 도시화지역 경계와 비교하면 도시·준밀집은 SGIS 도시+준도시와 IoU 0.95, 도시는 SGIS 도시와 IoU 0.86이다(빈틈 메우기는 하지 않음).
인구감소지역 목록은 `data/reference/population_decline_92.csv`(행정안전부 지정 현황을 옮겨 적음)를 쓴다.

## 원자료 (저장소에 포함하지 않음)

제공 기관의 이용 조건(재배포 제한)과 용량 때문에 원자료는 올리지 않았다. 아래에서 받아 `data/`에 두면 스크립트가 그대로 돌아간다.

| 자료 | 받는 곳 | 저장 위치 |
|---|---|---|
| 격자 통계(인구) 500m·1km, 격자 경계, 도시화지역 경계 | SGIS 자료신청 | `data/_census_reqdoc_*.zip`, `data/_grid_border_*.zip` |
| 센서스용 시군구 경계 2025 2Q | SGIS 경계 다운로드 | `data/bnd_sigungu_00_2025_2Q.zip` |
| 국내인구이동통계 인구관련 연간자료(2015·2020·2024) | MDIS 다운로드 서비스 (CSV + R 스크립트) | `data/migration/*.zip` |
| 행정동 경계 | [vuski/admdongkor](https://github.com/vuski/admdongkor) (CC BY 4.0) | `data/admdong/` |
| 학교·보건기관 표준데이터, 철도역, 도시철도 노선 | 공공데이터포털, 국가철도공단, 국토교통부 | `data/facilities/` |
| 고속도로 영업소, 장소 검색 | 한국도로공사 API(`EX_API_KEY`), 카카오 로컬(`KAKAO_API_KEY`) | `.env` |

## 실행

```bash
python scripts/did_analysis.py        # 도시화지역 추출·지도
python scripts/fetch_facilities.py    # 시설 위치 (API 키 필요)
python scripts/migration_od.py        # 인구 이동 OD 집계
python scripts/migration_analysis.py  # 이동 방향 분석
python scripts/lost_did_flows.py      # 빈 네모 도착지 추정
python scripts/export_webmap.py        # 동적 지도 자료
python scripts/build_site.py          # 웹페이지 그림·데이터
```

필요 패키지: geopandas, pyogrio, shapely 2, matplotlib, contextily, xyzservices, pandas, Pillow.

## 출처

통계청 SGIS 격자통계·도시화지역 경계, 통계청 국내인구이동통계(MDIS), 행정안전부 인구감소지역, 공공데이터포털, 국가철도공단, 국토교통부,
한국도로공사, 카카오 로컬, © OpenStreetMap contributors, vuski/admdongkor (CC BY 4.0), Esri 배경지도.
