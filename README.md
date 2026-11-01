# W8 DID MAP — 도시는 넓어지고, 밀도는 낮아진다

스마트시티 융합기술세미나(2026-2) 과제 3.
SGIS 1km 격자 인구로 도시(UN·EU·OECD 도시화 등급, 1,500명/km²)를 직접 추출해 2015~2024년 도시 면적과 인구밀도의 변화를 보고,
도시 밀도 감소율 순위에서 수도권·충청·전라·경상 권역마다 1곳씩 고른 4곳(인천 중구, 청주시, 광양시, 안동시)의
도시 변화, 공공·교통시설 입지, 도로 이동시간 기반 중력 접근성(Hansen, 반감 10분)을 분석했다.
저장소·변수 이름의 `did`는 도시화지역(도시)을 뜻한다. 300명/km²(도시·준밀집) 결과도 `results/`에 남겨 두었지만 웹페이지에는 1,500 기준만 싣는다.
2010→2015 사이 SGIS 격자 인구 집계 기준이 바뀌어 변화 분석은 2015년부터 한다(2000년부터의 값은 참고용).

- 웹페이지: https://w8-did-map.pages.dev
- 페이지 원본: [`site/`](site/) (정적 HTML, Cloudflare Pages로 배포)

## 폴더

| 경로 | 내용 |
|---|---|
| `site/` | 웹페이지 (`index.html`, 그림 `img/*.webp`, 차트 데이터 `data.js`) |
| `scripts/did_analysis.py` | 격자 인구 → 도시화지역 추출, 시계열·시군구 표, 전국·지역 지도 |
| `scripts/fetch_facilities.py` | 대학교·보건소·청사·철도·터미널·고속도로 IC 위치 수집 |
| `scripts/fetch_roads.py` | OSM 한국 추출본(PBF) → 4개 지역 차량 도로망(GraphML, 간선 이동시간) |
| `scripts/accessibility.py` | 1km 격자별 시설 6종 중력 접근성, 민감도(반감 5·10·20분), 접근성 지도 |
| `scripts/migration_od.py` | MDIS 국내인구이동 마이크로데이터 → 시군구·읍면동 OD 집계 |
| `scripts/migration_analysis.py` | 확대 분석 지역 이동 방향(순유출률, 바깥쪽 이동 비율), 흐름 지도 (웹페이지에서는 뺌) |
| `scripts/lost_did_flows.py` | 도시화지역에서 빠진 격자(빈 네모) 주민의 도착지 추정 |
| `scripts/export_webmap.py` | 동적 지도용 GeoJSON·시계열 |
| `scripts/build_site.py` | 웹페이지용 그림·데이터 만들기 |
| `results/tables/*.csv` | 결과 표 (도시화지역 요약, 시군구 밀도·면적 변화, 시설 입지, 인구 이동 요약 등) |
| `results/did_polygons.gpkg` | 도시화지역 폴리곤 (레이어 `did_{500M\|1K}_{300\|1500}_{연도}`, EPSG:5179) |

## 도시화지역 정의 (Degree of Urbanisation 1단계)

| 구분 | 격자 밀도 | 묶는 방식 | 묶음 인구 |
|---|---|---|---|
| 도시 (주 분석) | 1,500명/km² 이상 | rook (변) | 50,000명 이상 |
| 도시·준밀집지역 (참고) | 300명/km² 이상 | queen (꼭짓점 포함) | 5,000명 이상 |

주 분석은 1km 격자. SGIS 도시화지역 경계와 비교하면 도시·준밀집은 SGIS 도시+준도시와 IoU 0.95, 도시는 SGIS 도시와 IoU 0.86이다(빈틈 메우기는 하지 않음).

## 중력 접근성

`S_k(i) = Σ_j exp(−β·t_ij)`, `β = ln2 / 10분`, `A(i) = 평균_k S_k(i) / max S_k` (k = 대학교, 보건소, 시·군·구청, 철도역, 버스터미널, 고속도로 IC, 같은 가중치).
반감 10분은 국토교통부(2018) 기초생활인프라 국가적 최저기준의 지역거점시설 차량 10분 기준(이하연·김성표·강상혁, 2021, 대한토목학회논문집 41(3))을 따랐다.
이동시간은 OSM 제한속도(없으면 도로 등급 기본값) 기준이다. 격자별 결과(`accessibility_cells.csv`)는 격자 인구가 들어 있어 올리지 않고, 구역별 요약과 민감도 표만 올렸다.

## 원자료 (저장소에 포함하지 않음)

제공 기관의 이용 조건(재배포 제한)과 용량 때문에 원자료는 올리지 않았다. 아래에서 받아 `data/`에 두면 스크립트가 그대로 돌아간다.

| 자료 | 받는 곳 | 저장 위치 |
|---|---|---|
| 격자 통계(인구) 500m·1km, 격자 경계, 도시화지역 경계 | SGIS 자료신청 | `data/_census_reqdoc_*.zip`, `data/_grid_border_*.zip` |
| 센서스용 시군구 경계 2025 2Q | SGIS 경계 다운로드 | `data/bnd_sigungu_00_2025_2Q.zip` |
| 국내인구이동통계 인구관련 연간자료(2015·2020·2024) | MDIS 다운로드 서비스 (CSV + R 스크립트) | `data/migration/*.zip` |
| 행정동 경계 | [vuski/admdongkor](https://github.com/vuski/admdongkor) (CC BY 4.0) | `data/admdong/` |
| 학교·보건기관 표준데이터, 철도역, 도시철도 노선 | 공공데이터포털, 국가철도공단, 국토교통부 | `data/facilities/` |
| 도로망 | [Geofabrik 한국 추출본](https://download.geofabrik.de/asia/south-korea.html) (© OpenStreetMap, ODbL) | `data/roads/south-korea-latest.osm.pbf` |
| 고속도로 영업소, 장소 검색 | 한국도로공사 API(`EX_API_KEY`), 카카오 로컬(`KAKAO_API_KEY`) | `.env` |

## 실행

```bash
python scripts/did_analysis.py        # 도시화지역 추출·지도
python scripts/fetch_facilities.py    # 시설 위치 (API 키 필요)
python scripts/migration_od.py        # 인구 이동 OD 집계
python scripts/migration_analysis.py  # 이동 방향 분석
python scripts/lost_did_flows.py      # 빈 네모 도착지 추정
python scripts/fetch_roads.py         # 도로망 (PBF 필요)
python scripts/accessibility.py       # 중력 접근성
python scripts/export_webmap.py       # 동적 지도 자료
python scripts/build_site.py          # 웹페이지 그림·데이터
```

필요 패키지: geopandas, pyogrio, shapely 2, matplotlib, contextily, xyzservices, pandas, Pillow, networkx, osmium(pyosmium).

## 출처

통계청 SGIS 격자통계·도시화지역 경계, 통계청 국내인구이동통계(MDIS), 공공데이터포털, 국가철도공단, 국토교통부,
한국도로공사, 카카오 로컬, © OpenStreetMap contributors, vuski/admdongkor (CC BY 4.0), Esri 배경지도.
