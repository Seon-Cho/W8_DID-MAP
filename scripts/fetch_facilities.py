"""
6개 도시 확대도에 얹을 공공시설·교통 레이어 만들기

출처 (data/facilities/)
  - 학교(초·중·고)      : 공공데이터포털 전국초중등학교위치표준데이터 (schools_p*.json, 좌표 포함)
  - 보건기관            : 공공데이터포털 전국보건기관표준데이터 (health_centers.json, 주소만 있음 → 주소 검색)
                          표준데이터에 빠진 지자체가 많아 이름 검색('보건소' 등)으로 보완
  - 철도역              : 국가철도공단 철도역 정보 (rail_stations_krna.csv, 좌표 포함)
  - 도시철도 역·노선    : 국토교통부 도시철도 전체노선 (urban_rail_lines.csv, 노선별 역 순서)
                          역 위치는 이름 검색, 노선은 역을 순서대로 이은 개략선
  - 청사                : 시·구·군청, 읍면동 행정복지센터를 이름 검색
  - 고속도로 IC         : 한국도로공사 공공데이터포털 영업소 위치정보 API (EX_API_KEY, ex_units.json에 저장)
  - 고속도로 선형       : 국토교통부 표준노드링크 MOCT_LINK 중 고속국도(ROAD_RANK 101) → moct_expressway.gpkg
  - 철도 선형           : OpenStreetMap에서 만든 간략 선형 (이전 과제 transport.geojson → osm_transport.geojson)
  - 청사·보건소·버스터미널 : 카카오 로컬 키워드 검색 (KAKAO_API_KEY, kakao_cache.json에 저장)
                          키가 없으면 아래 Nominatim 이름 검색으로 대신한다

이름·주소 검색은 Nominatim(OpenStreetMap 기반 지오코더)을 쓰고, 결과는 geocode_cache.json에 저장해
다시 실행할 때는 검색하지 않는다. 이용 정책에 따라 초당 1건만 보낸다.

출력: data/facilities/facilities.gpkg (EPSG:5179)
  레이어 schools, health, townhall, station, urban_rail_station, urban_rail_line, terminal, ic, highway, rail_line

실행 (did_analysis.py를 한 번 돌려 output/_cache/sigungu.gpkg가 있어야 함)
  ~/.venvs/did-analysis/bin/python scripts/fetch_facilities.py
"""

import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import LineString, MultiLineString, Point

from did_analysis import CITY_ZOOMS, DATA, load_sigungu

FAC = DATA / "facilities"
OUT_GPKG = FAC / "facilities.gpkg"
CACHE_FILE = FAC / "geocode_cache.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = {"User-Agent": "did-assignment-script/1.0 (course project)"}
PAD = 15000  # 도시 경계 바깥 수집 여백(m)
EX_API = "https://data.ex.co.kr/openapi/locationinfo/locationinfoUnit"
EX_CACHE = FAC / "ex_units.json"
KAKAO_CACHE = FAC / "kakao_cache.json"
MIN_RECT_DEG = 0.01  # 카카오 검색 범위를 이보다 좁게(약 1 km) 쪼개지 않는다
DOTENV_DEFAULT = Path.home() / "26-2. smart-city-seminar" / "professor_data" / "code-main" / ".env"

# 도시철도 권역 → 확대도 도시
URBAN_RAIL_REGION = {"부산": "부산", "대구": "대구", "광주": "광주"}
HEALTH_NAME = r"보건소|보건지소|보건진료소|보건의료원"
CENTER_NAME = r"행정복지센터|주민센터|읍사무소|면사무소"
# 보건기관 표준데이터 중 주소 검색할 대상: 6개 도시와 바로 붙은 시군만
HEALTH_SIDO = r"부산광역시|대구광역시|광주광역시|광주 |남원|목포|무안|창원|김해|양산|경산|칠곡|담양|나주|장성|화순"


# ---------------------------------------------------------------- 공통
def city_areas():
    """도시별 (시군구 표, 수집 범위 상자 EPSG:5179, WGS84 viewbox)."""
    sgg = load_sigungu()
    out = {}
    for name, cond in CITY_ZOOMS.items():
        city = sgg[cond(sgg)]
        x0, y0, x1, y1 = city.total_bounds
        box = shapely.box(x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD)
        w, s, e, n = gpd.GeoSeries([box], crs=sgg.crs).to_crs(4326).total_bounds
        out[name] = (city, box, (w, n, e, s))
    return out, sgg.crs


class Geocoder:
    def __init__(self):
        self.cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
        self.calls = 0

    def search(self, q, viewbox=None, limit=10):
        key = f"{q}|{viewbox}|{limit}"
        if key not in self.cache:
            params = {"q": q, "format": "json", "limit": limit, "countrycodes": "kr"}
            if viewbox:
                params.update(viewbox=",".join(f"{v:.5f}" for v in viewbox), bounded=1)
            time.sleep(1.1)
            try:
                req = urllib.request.Request(NOMINATIM + "?" + urllib.parse.urlencode(params), headers=UA)
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.cache[key] = json.loads(r.read())
            except Exception as e:
                print(f"    검색 실패 '{q}': {e}")
                return []
            self.calls += 1
            if self.calls % 20 == 0:
                self.save()
                print(f"    검색 {self.calls}건...")
        return self.cache[key]

    def save(self):
        CACHE_FILE.write_text(json.dumps(self.cache, ensure_ascii=False))


def hit_point(h):
    return Point(float(h["lon"]), float(h["lat"]))


def to_gdf(rows, crs):
    g = gpd.GeoDataFrame(rows, geometry="geometry", crs=4326).to_crs(crs)
    return g


def clip_city(g, areas):
    """수집 범위 상자 안의 것만 남기고 city 열을 붙인다 (상자가 겹치면 양쪽에 모두 들어간다)."""
    parts = []
    for name, (_, box, _) in areas.items():
        s = g[g.within(box)].copy()
        s["city"] = name
        parts.append(s)
    return pd.concat(parts, ignore_index=True)


# ---------------------------------------------------------------- 학교
def build_schools(areas, crs):
    recs = []
    for f in sorted(FAC.glob("schools_p*.json")):
        recs += json.loads(f.read_text())
    df = pd.DataFrame(recs)
    df = df[df["OPER_STTUS"].eq("운영")].copy()
    df["lat"] = pd.to_numeric(df["LATITUDE"], errors="coerce")
    df["lon"] = pd.to_numeric(df["LONGITUDE"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"])
    g = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs=4326).to_crs(crs)
    g["found_year"] = pd.to_datetime(g["FOND_DATE"], errors="coerce").dt.year
    g = g.rename(columns={"SCHOOL_NM": "name", "SCHOOL_SE": "subkind"})
    g["source"] = "학교위치표준데이터"
    return clip_city(g[["name", "subkind", "found_year", "FOND_TYPE", "source", "geometry"]], areas)


# ---------------------------------------------------------------- 철도
def build_rail_stations(areas, crs):
    with open(FAC / "rail_stations_krna.csv", encoding="cp949") as f:
        rows = [r for r in csv.DictReader(f)]
    out = []
    for r in rows:
        lat, lon = float(r["위도좌표"] or 0), float(r["경도좌표"] or 0)
        if lat and lon:
            out.append({"name": r["역이름"], "subkind": "철도", "lines": r["관련노선"],
                        "source": "국가철도공단 철도역정보", "geometry": Point(lon, lat)})
    return clip_city(to_gdf(out, crs), areas)


def build_urban_rail(areas, crs, geo):
    with open(FAC / "urban_rail_lines.csv", encoding="cp949") as f:
        rows = [r for r in csv.DictReader(f) if r["권역명"] in URBAN_RAIL_REGION]
    stations, lines, missing = [], [], []
    by_line = {}
    for r in rows:
        city = URBAN_RAIL_REGION[r["권역명"]]
        vb = areas[city][2]
        nm = re.sub(r"역$", "", re.sub(r"\(.*?\)", "", r["역명"]).strip())  # '하단(부산본병원)' → '하단'
        hits = [h for h in geo.search(nm, vb) if h.get("class") in ("railway", "public_transport")]
        if not hits:
            hits = [h for h in geo.search(f"{nm}역", vb) if h.get("class") in ("railway", "public_transport")]
        line_nm = f"{r['권역명']} {r['노선명']}"
        if hits:
            p = hit_point(hits[0])
            stations.append({"name": r["역명"], "subkind": line_nm, "source": "도시철도노선+이름검색",
                             "geometry": p})
            by_line.setdefault(line_nm, []).append((int(r["순번"]), p))
        else:
            missing.append(f"{line_nm} {r['역명']}")
    for line_nm, pts in by_line.items():
        pts = [p for _, p in sorted(pts, key=lambda t: t[0])]
        if len(pts) >= 2:
            lines.append({"name": line_nm, "subkind": "도시철도(역 연결 개략선)",
                          "source": "도시철도노선+이름검색", "geometry": LineString(pts)})
    if missing:
        print(f"  도시철도 역 위치를 못 찾음 {len(missing)}개: {', '.join(missing[:15])}"
              + (" ..." if len(missing) > 15 else ""))
    st = to_gdf(stations, crs).drop_duplicates(["name", "subkind"])
    ln = to_gdf(lines, crs)
    ln["geometry"] = ln.geometry.apply(drop_long_links)
    return clip_city(st, areas), clip_city(ln[~ln.geometry.is_empty], areas)


MAX_LINK = 5000  # m. 이보다 먼 역 사이는 잇지 않는다 (검색 오류나 범위 밖 역 때문에 생기는 직선 방지)


def drop_long_links(line):
    c = list(line.coords)
    segs = [LineString(pq) for pq in zip(c[:-1], c[1:]) if LineString(pq).length <= MAX_LINK]
    return shapely.line_merge(MultiLineString(segs)) if segs else LineString()


# ---------------------------------------------------------------- 청사
def sigungu_query_names(row):
    """'창원시 의창구' → ['의창구청', '창원시 의창구청'], '남원시' → ['남원시청']."""
    nm = row["SIGUNGU_NM"]
    parts = nm.split()
    return [f"{parts[-1]}청", f"{nm}청"] if len(parts) > 1 else [f"{nm}청"]


SIDO_HALL = {"21": "부산광역시청", "22": "대구광역시청", "24": "광주광역시청"}
SIDO_NAME = {"21": "부산광역시", "22": "대구광역시", "24": "광주광역시", "35": "전북특별자치도",
             "36": "전라남도", "38": "경상남도"}


def build_townhalls(areas, crs, geo):
    rows = []
    for city, (sgg, _, vb) in areas.items():
        queries = [(q, "시군구") for _, r in sgg.iterrows() for q in sigungu_query_names(r)[:1]]
        sido = sgg["sido_cd"].iloc[0]
        if sido in SIDO_HALL and len(sgg) > 1:
            queries.insert(0, (SIDO_HALL[sido], "시·도"))
        if city == "창원":
            queries.insert(0, ("창원시청", "시군구"))
        sido_nm = SIDO_NAME.get(sido, "")
        for q, level in queries:
            hits = []
            for qq in (q, f"{sido_nm} {q}"):
                hits = [h for h in geo.search(qq, vb, limit=5)
                        if (h.get("class") in ("amenity", "office") and h.get("type") in ("townhall", "government"))
                        or h.get("name", "").replace(" ", "").endswith(q.replace(" ", ""))]
                if hits:
                    break
            if hits:
                rows.append({"name": hits[0]["name"] or q, "subkind": level, "source": "이름검색",
                             "geometry": hit_point(hits[0])})
            else:
                print(f"  청사 못 찾음: {q}")
        # 행정복지센터: 시군구마다 이름 검색 (한 번에 최대 50건)
        for _, r in sgg.iterrows():
            gu = r["SIGUNGU_NM"].split()[-1]
            for kw in ("행정복지센터", "주민센터"):
                for h in geo.search(f"{kw} {gu}", vb, limit=50):
                    if re.search(CENTER_NAME, h.get("name", "")) and h.get("class") in ("office", "amenity"):
                        rows.append({"name": h["name"], "subkind": "읍면동", "source": "이름검색",
                                     "geometry": hit_point(h)})
    g = to_gdf(rows, crs).drop_duplicates(["name"])
    return clip_city(g, areas)


# ---------------------------------------------------------------- 보건기관
def build_health(areas, crs, geo):
    std = json.loads((FAC / "health_centers.json").read_text())
    rows = []
    for r in std:
        # 6개 도시가 속한 시도의 기관만 주소 검색한다 (나머지는 어차피 지도 범위 밖)
        if not re.search(HEALTH_SIDO, f"{r['CTPV_NM']} {r['LCTN_ROAD_NM_ADDR']}"):
            continue
        addr = r["LCTN_ROAD_NM_ADDR"] or r["LCTN_LOTNO_ADDR"]
        hits = geo.search(addr, limit=1) if addr else []
        if not hits:
            continue
        rows.append({"name": r["HT_INST_NM"], "subkind": r["HTCT_TYPE_NM"],
                     "source": "보건기관표준데이터+주소검색", "geometry": hit_point(hits[0])})
    # 표준데이터에 없는 지자체 보완: 시군구마다 '보건소' 이름 검색
    std_names = {re.sub(r"\s", "", r["HT_INST_NM"]) for r in std}
    for city, (sgg, _, vb) in areas.items():
        for _, s in sgg.iterrows():
            gu = s["SIGUNGU_NM"].split()[-1]
            hits = geo.search(f"보건소 {gu}", vb, limit=20) + geo.search(f"{gu}보건소", vb, limit=5)
            for h in hits:
                nm = h.get("name", "")
                m = re.search(HEALTH_NAME, nm)
                if m and re.sub(r"\s", "", nm) not in std_names and h.get("class") in ("amenity", "healthcare",
                                                                                     "office", "building"):
                    rows.append({"name": nm, "subkind": m.group(0), "source": "이름검색",
                                 "geometry": hit_point(h)})
    g = to_gdf(rows, crs).drop_duplicates(["name"])
    return clip_city(g, areas)


# ---------------------------------------------------------------- 고속도로 IC·노선
def env_key(name):
    """API 키: 환경변수 → .env 파일(DOTENV_PATH, 기본은 수업 코드 폴더) 순으로 찾는다."""
    if os.environ.get(name):
        return os.environ[name]
    path = Path(os.environ.get("DOTENV_PATH", DOTENV_DEFAULT))
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"\s*([A-Za-z_]+)\s*=\s*['\"]?([^'\"]*)", line)
            if m and m[1] == name:
                return m[2].strip()
    return None


def fetch_ex_units():
    """한국도로공사 영업소(IC) 위치정보 전체를 받아 ex_units.json에 저장한다."""
    if EX_CACHE.exists():
        return json.loads(EX_CACHE.read_text())
    key = env_key("EX_API_KEY")
    if not key:
        print("  EX_API_KEY 없음 → IC·고속도로 건너뜀")
        return []
    units, page = [], 1
    while True:
        q = urllib.parse.urlencode(dict(key=key, type="json", numOfRows=100, pageNo=page))
        with urllib.request.urlopen(f"{EX_API}?{q}", timeout=30) as r:
            js = json.loads(r.read())
        units += js["list"]
        if page >= js["pageSize"]:
            break
        page += 1
    EX_CACHE.write_text(json.dumps(units, ensure_ascii=False))
    return units


def build_ic(units, areas, crs):
    rows = [{"name": u["unitName"].strip(), "subkind": "IC", "route": u["routeName"].strip(),
             "source": "한국도로공사 영업소 위치정보", "geometry": Point(float(u["xValue"]), float(u["yValue"]))}
            for u in units if float(u["xValue"] or 0) > 0]
    g = to_gdf(rows, crs).drop_duplicates(["name", "route"])
    return clip_city(g, areas)


def clip_lines(g, areas):
    """선 레이어를 도시 수집 상자로 잘라 city 열을 붙인다."""
    parts = []
    for name, (_, box, _) in areas.items():
        sub = g[g.intersects(box)].copy()
        sub["geometry"] = sub.geometry.intersection(box)
        sub["city"] = name
        parts.append(sub)
    return pd.concat(parts, ignore_index=True)


def build_highway(areas, crs):
    """고속국도 실제 선형: 국토교통부 표준노드링크(MOCT_LINK) 중 ROAD_RANK 101."""
    g = gpd.read_file(FAC / "moct_expressway.gpkg").to_crs(crs)
    g = g.dissolve("ROAD_NAME").reset_index()
    g = g.rename(columns={"ROAD_NAME": "name"})[["name", "geometry"]]
    g["subkind"], g["source"] = "고속국도", "국토교통부 표준노드링크"
    return clip_lines(g, areas)


def build_rail_lines(areas, crs):
    """철도 선형: 이전 과제에서 OpenStreetMap으로 만든 transport.geojson (간략화 약 40 m)."""
    g = gpd.read_file(FAC / "osm_transport.geojson").to_crs(crs)
    g = g[g["kind"].isin(["rail", "highspeed"])].copy()
    g["name"] = g["kind"].map({"rail": "일반철도", "highspeed": "고속철도"})
    g["subkind"], g["source"] = g["name"], "OpenStreetMap (srcitymap transport.geojson)"
    return clip_lines(g[["name", "subkind", "source", "geometry"]], areas)


# ---------------------------------------------------------------- 카카오 장소 검색
class Kakao:
    """카카오 로컬 키워드 검색. 한 번에 45건까지만 주므로 범위를 4등분하며 다시 찾는다."""

    URL = "https://dapi.kakao.com/v2/local/search/keyword.json"

    def __init__(self, key):
        self.key = key
        self.cache = json.loads(KAKAO_CACHE.read_text()) if KAKAO_CACHE.exists() else {}
        self.n = 0

    def page(self, q, rect, page):
        k = f"{q}|{','.join(f'{v:.5f}' for v in rect)}|{page}"
        if k not in self.cache:
            params = urllib.parse.urlencode({"query": q, "rect": ",".join(f"{v:.6f}" for v in rect),
                                             "size": 15, "page": page})
            req = urllib.request.Request(f"{self.URL}?{params}", headers={"Authorization": f"KakaoAK {self.key}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                self.cache[k] = json.loads(r.read())
            self.n += 1
            if self.n % 100 == 0:
                self.save()
                print(f"    카카오 요청 {self.n}건...")
            time.sleep(0.05)
        return self.cache[k]

    def search(self, q, rect, keep, depth=0):
        """rect = (서, 남, 동, 북) 경위도. 첫 페이지에 원하는 장소(keep)가 있을 때만 범위를 쪼갠다
        ('시청'처럼 주변 상점까지 잡히는 검색어가 끝없이 쪼개지는 것을 막기 위해)."""
        first = self.page(q, rect, 1)
        w, s_, e, n = rect
        if (first["meta"]["total_count"] > 45 and e - w > MIN_RECT_DEG
                and any(keep(d) for d in first["documents"])):
            mx, my = (w + e) / 2, (s_ + n) / 2
            out = []
            for sub in [(w, s_, mx, my), (mx, s_, e, my), (w, my, mx, n), (mx, my, e, n)]:
                out += self.search(q, sub, keep, depth + 1)
            return out
        docs, pg, js = list(first["documents"]), 1, first
        while not js["meta"]["is_end"] and pg < 3:
            pg += 1
            js = self.page(q, rect, pg)
            docs += js["documents"]
        return docs

    def save(self):
        KAKAO_CACHE.write_text(json.dumps(self.cache, ensure_ascii=False))


def kakao_layer(kakao, areas, crs, queries, keep, subkind):
    """queries로 찾은 장소 중 keep(doc)을 통과한 것만 점 레이어로."""
    rows = []
    for city, (_, _, vb) in areas.items():
        w, n, e, s_ = vb
        for q in queries:
            for d in kakao.search(q, (w, s_, e, n), keep):
                if keep(d):
                    rows.append({"name": d["place_name"], "subkind": subkind(d), "source": "카카오 장소검색",
                                 "kakao_id": d["id"], "category": d["category_name"],
                                 "geometry": Point(float(d["x"]), float(d["y"]))})
    g = to_gdf(rows, crs).drop_duplicates("kakao_id")
    return clip_city(g, areas)


def build_kakao_layers(areas, crs, kakao):
    cat = lambda d: d["category_name"]
    hall_level = lambda d: "시·도" if re.search(r"(광역시|특별시|특별자치시|도)청$", d["place_name"].replace(" ", "")) \
        else "시군구"
    return {
        "terminal": kakao_layer(kakao, areas, crs, ["버스터미널", "시외버스터미널", "고속버스터미널"],
                                lambda d: cat(d).endswith("고속,시외버스터미널"), lambda d: "버스터미널"),
        "townhall": pd.concat([
            kakao_layer(kakao, areas, crs, ["구청", "시청", "군청", "광역시청", "도청"],
                        lambda d: cat(d).split(" > ")[-1] in ("구청", "시청", "군청", "도청")
                        and d["place_name"].replace(" ", "").endswith("청"), hall_level),  # 부서·별관·예정지 제외
            kakao_layer(kakao, areas, crs, ["행정복지센터", "주민센터", "읍사무소", "면사무소"],
                        lambda d: re.search(CENTER_NAME + "|동행정복지센터", cat(d)) is not None
                        and "분소" not in d["place_name"], lambda d: "읍면동"),
        ], ignore_index=True),
        "health": kakao_layer(kakao, areas, crs, ["보건소", "보건지소", "보건진료소"],
                              lambda d: "보건소" in cat(d) or re.search(HEALTH_NAME, cat(d)) is not None,
                              lambda d: (re.search(HEALTH_NAME, d["place_name"]) or [None])[0] or "보건소"),
    }


# ---------------------------------------------------------------- 버스터미널
def build_terminals(areas, crs, geo):
    rows = []
    for city, (_, _, vb) in areas.items():
        for q in ("버스터미널", "터미널", "시외버스터미널", "고속버스터미널"):
            for h in geo.search(q, vb, limit=50):
                nm = h.get("name", "")
                if "터미널" in nm and "여객선" not in nm and "화물" not in nm and (
                        h.get("type") == "bus_station" or h.get("class") in ("amenity", "building", "public_transport")):
                    rows.append({"name": nm, "subkind": "버스터미널", "source": "이름검색", "geometry": hit_point(h)})
    g = to_gdf(rows, crs).drop_duplicates(["name"])
    return clip_city(g, areas)


def main():
    areas, crs = city_areas()
    geo = Geocoder()
    layers = {}
    try:
        print("학교 / 철도역")
        layers["schools"] = build_schools(areas, crs)
        layers["station"] = build_rail_stations(areas, crs)
        print("도시철도 (역 이름 검색)")
        layers["urban_rail_station"], layers["urban_rail_line"] = build_urban_rail(areas, crs, geo)
        kakao_key = env_key("KAKAO_API_KEY")
        if kakao_key:
            print("청사·보건소·버스터미널 (카카오 장소검색)")
            kakao = Kakao(kakao_key)
            try:
                layers.update(build_kakao_layers(areas, crs, kakao))
            finally:
                kakao.save()
                print(f"    카카오 요청 {kakao.n}건")
        else:
            print("청사 (이름 검색)")
            layers["townhall"] = build_townhalls(areas, crs, geo)
            print("보건기관 (주소·이름 검색)")
            layers["health"] = build_health(areas, crs, geo)
            print("버스터미널 (이름 검색)")
            layers["terminal"] = build_terminals(areas, crs, geo)
        print("고속도로·철도 선형")
        layers["highway"] = build_highway(areas, crs)
        layers["rail_line"] = build_rail_lines(areas, crs)
        print("고속도로 IC (한국도로공사 API)")
        units = fetch_ex_units()
        if units:
            layers["ic"] = build_ic(units, areas, crs)

    finally:
        geo.save()

    OUT_GPKG.unlink(missing_ok=True)
    counts = {}
    for name, g in layers.items():
        if g is None or g.empty:
            continue
        g.to_file(OUT_GPKG, layer=name)
        counts[name] = g.groupby("city").size()
    print("\n도시별 개수")
    print(pd.DataFrame(counts).fillna(0).astype(int).T.to_string())
    print(f"\n→ {OUT_GPKG}")


if __name__ == "__main__":
    main()
