# W8 DID MAP — 도시는 넓어지고, 밀도는 낮아진다

스마트시티 융합기술세미나(2026-2) 과제 3.
SGIS 격자 인구로 인구집중지구(DID)를 추출해 2000~2024년 도시화 면적과 인구밀도의 변화를 보고,
6개 도시(남원·목포·부산·대구·광주·창원)의 DID 변화, 공공·교통시설 입지, 인구 이동 방향을 분석했다.

- 웹페이지: https://w8-did-map.pages.dev
- 페이지 원본: [`site/`](site/) (정적 HTML, Cloudflare Pages로 배포)

## 폴더

| 경로 | 내용 |
|---|---|
| `site/` | 웹페이지 (`index.html`, 그림 `img/*.webp`, 차트 데이터 `data.js`) |
| `scripts/did_analysis.py` | 격자 인구 → DID 추출, 시계열·시군구 표, 전국·도시 지도 |
| `scripts/fetch_facilities.py` | 학교·보건·청사·철도·터미널·고속도로 IC 위치 수집 |
| `scripts/migration_od.py` | MDIS 국내인구이동 마이크로데이터 → 시군구·읍면동 OD 집계 |
| `scripts/migration_analysis.py` | 6개 도시권 이동 방향(순유출률, 바깥쪽 이동 비율), 흐름 지도 |
| `scripts/lost_did_flows.py` | DID에서 빠진 격자(빈 네모) 주민의 도착지 추정 |
| `scripts/build_site.py` | 웹페이지용 그림·데이터 만들기 |
| `results/tables/*.csv` | 결과 표 (DID 요약, 시군구 밀도·면적 변화, 시설 입지, 인구 이동 요약 등) |
| `results/did_polygons.gpkg` | DID 폴리곤 (레이어 `did_{500M\|1K}_{2000\|4000}_{연도}`, EPSG:5179) |

## DID 정의

1. 인구밀도가 기준(2,000 또는 4,000명/km²) 이상인 격자를 고른다.
2. 변을 맞댄(rook) 격자끼리 묶는다.
3. 묶음 인구가 5,000명 이상이면 DID로 본다. 주 분석은 500m 격자, 2,000명/km².

## 원자료 (저장소에 포함하지 않음)

제공 기관의 이용 조건(재배포 제한)과 용량 때문에 원자료는 올리지 않았다. 아래에서 받아 `data/`에 두면 스크립트가 그대로 돌아간다.

| 자료 | 받는 곳 | 저장 위치 |
|---|---|---|
| 격자 통계(인구) 500m·1km, 격자 경계, 도시화지역 경계 | SGIS 자료신청 | `data/_census_reqdoc_*.zip`, `data/_grid_border_*.zip` |
| 센서스용 시군구 경계 2025 2Q | SGIS 경계 다운로드 | `data/bnd_sigungu_00_2025_2Q.zip` |
| 국내인구이동통계 인구관련 연간자료(2005~2024), 인구이동 서머리(2000) | MDIS 다운로드 서비스 (CSV + R 스크립트) | `data/migration/*.zip` |
| 행정동 경계 | [vuski/admdongkor](https://github.com/vuski/admdongkor) (CC BY 4.0) | `data/admdong/` |
| 학교·보건기관 표준데이터, 철도역, 도시철도 노선 | 공공데이터포털, 국가철도공단, 국토교통부 | `data/facilities/` |
| 고속도로 영업소, 장소 검색 | 한국도로공사 API(`EX_API_KEY`), 카카오 로컬(`KAKAO_API_KEY`) | `.env` |

## 실행

```bash
python scripts/did_analysis.py        # DID 추출·지도
python scripts/fetch_facilities.py    # 시설 위치 (API 키 필요)
python scripts/migration_od.py        # 인구 이동 OD 집계
python scripts/migration_analysis.py  # 이동 방향 분석
python scripts/lost_did_flows.py      # 빈 네모 도착지 추정
python scripts/build_site.py          # 웹페이지 그림·데이터
```

필요 패키지: geopandas, pyogrio, shapely 2, matplotlib, contextily, xyzservices, pandas, Pillow.

## 출처

통계청 SGIS 격자통계·도시화지역 경계, 통계청 국내인구이동통계(MDIS), 공공데이터포털, 국가철도공단, 국토교통부,
한국도로공사, 카카오 로컬, © OpenStreetMap contributors, vuski/admdongkor (CC BY 4.0), Esri 배경지도.
