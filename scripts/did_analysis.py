"""
격자 인구 기반 인구집중지구(DID) 추출 및 시계열 분석

DID 정의 (일본 국세조사 방식 차용)
  1) 인구밀도가 기준값(THRESHOLDS, 명/km²) 이상인 격자를 고른다.
  2) 변을 공유하는(rook) 격자끼리 하나의 덩어리로 묶는다.
  3) 덩어리 인구 합계가 MIN_POP(5,000명) 이상인 것만 DID로 인정한다.

입력 (data/, SGIS 자료신청 원본 zip 그대로)
  - _census_reqdoc_*.zip            : 격자통계(인구) csv (연도 × 1K/500M × 도엽)
  - _grid_border_grid_2025_*.zip    : 격자경계 shp (도엽별)
  - _grid_border_urban_*.zip        : SGIS 도시화지역(격자) 경계 (도시/준도시)
  - bnd_sigungu_00_2025_2Q.zip      : 센서스용 시군구 경계

출력 (output/)
  - tables/did_summary.csv          : 연도·해상도·기준별 DID 개수/면적/인구/밀도
  - tables/did_by_sido.csv          : 주 분석 설정의 시도별 DID 면적·밀도
  - tables/did_vs_sgis_urban.csv    : SGIS 도시화지역 경계와의 겹침 비교
  - gpkg/did_polygons.gpkg          : DID 폴리곤 (레이어 = did_{해상도}_{기준}_{연도})
  - maps/*.png                      : 전국·수도권 지도, 2000→2024 변화 지도, 추세 그래프

실행
  ~/.venvs/did-analysis/bin/python scripts/did_analysis.py
"""

import io
import re
import tempfile
import warnings
import zipfile
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd
import shapely

warnings.filterwarnings("ignore", category=RuntimeWarning)

# ---------------------------------------------------------------- 설정
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output"
CACHE = OUT / "_cache"

YEARS = [2000, 2005, 2010, 2015, 2020, 2024]
RESOLUTIONS = ["500M", "1K"]
THRESHOLDS = [2000, 4000]  # 명/km²
MIN_POP = 5000

MAIN_RES = "500M"  # 지도·시도별 표에 쓰는 주 분석 설정
MAIN_THR = 2000
SGG_MIN_AREA = 5  # 시군구 비교 시 최소 DID 면적(km²). 너무 작으면 격자 몇 개로 변화율이 요동친다.

# 2015년부터 집계 대상이 내국인 → 내·외국인 전체 가구로 바뀜 (SGIS 유의사항)
DEFINITION_BREAK = (2010, 2015)
# 변화 분석 시작 연도: 집계 기준이 같은 2015→2024 구간만 비교한다 (도시 지도·변화 지도·시설 집계)
CHANGE_START = DEFINITION_BREAK[1]

SIDO = {
    "11": "서울", "21": "부산", "22": "대구", "23": "인천", "24": "광주",
    "25": "대전", "26": "울산", "29": "세종", "31": "경기", "32": "강원",
    "33": "충북", "34": "충남", "35": "전북", "36": "전남", "37": "경북",
    "38": "경남", "39": "제주",
}
CAPITAL_REGION = ["11", "23", "31"]
# 도시 확대도 대상: 2015→2024 DID 밀도 감소 Top 10 시군구 중 비수도권 6곳이 속한 도시 (순위 순)
#   대구 서구, 부산 영도구, 광주 남구, 동해시, 창원 마산합포구, 울산 울주군
# 이름 → 2025년 시군구 경계에서 해당 도시를 고르는 조건
CITY_ZOOMS = {
    "대구": lambda s: (s["sido_cd"] == "22") & (s["SIGUNGU_NM"] != "군위군"),  # 2023년 편입된 군위군 제외
    "부산": lambda s: s["sido_cd"] == "21",
    "광주": lambda s: s["sido_cd"] == "24",
    "동해": lambda s: s["SIGUNGU_NM"] == "동해시",
    "창원": lambda s: s["SIGUNGU_NM"].str.startswith("창원시"),
    "울산": lambda s: s["sido_cd"] == "26",
}
# 순위에 오른 시군구 (지도에서 따로 강조)
CITY_FOCUS = {"대구": "서구", "부산": "영도구", "광주": "남구", "동해": "동해시", "창원": "창원시 마산합포구",
              "울산": "울주군"}
# 수도권 확대 지도 범위 (EPSG:5179). 시도 경계로 잡으면 서해 섬 때문에 너무 넓어져서 고정값을 쓴다.
CAPITAL_BBOX = (900000, 1885000, 1060000, 2010000)

plt.rcParams["font.family"] = "AppleGothic"
plt.rcParams["axes.unicode_minus"] = False


# ---------------------------------------------------------------- 입력
def zip_members(zp):
    """SGIS zip은 파일명이 CP949라서 디코딩해서 (이름, ZipInfo)로 돌려준다."""
    z = zipfile.ZipFile(zp)
    for info in z.infolist():
        name = info.filename
        if not info.flag_bits & 0x800:
            name = name.encode("cp437").decode("cp949")
        yield z, name, info


def extract_shp(zp, stem, dest):
    """zip 안에서 stem.* (shp 구성 파일)만 dest에 풀고 .shp 경로를 돌려준다."""
    for z, name, info in zip_members(zp):
        if Path(name).stem == stem:
            (dest / name).write_bytes(z.read(info))
    return dest / f"{stem}.shp"


def load_population(res):
    """격자 총인구(to_in_001)를 GRID_CD × 연도 표로 만든다."""
    zp = next(DATA.glob("_census_reqdoc_*.zip"))
    pat = re.compile(rf"^(\d{{4}})년_인구_.+_{res}\.csv$")
    frames = []
    for z, name, info in zip_members(zp):
        m = pat.match(name)
        if not m or int(m.group(1)) not in YEARS:
            continue
        df = pd.read_csv(io.BytesIO(z.read(info)), header=None,
                         names=["year", "GRID_CD", "item", "pop"], encoding="cp949")
        frames.append(df[df["item"] == "to_in_001"])
    df = pd.concat(frames)
    df["pop"] = pd.to_numeric(df["pop"], errors="coerce").fillna(0)
    return df.pivot_table(index="GRID_CD", columns="year", values="pop",
                          aggfunc="sum", fill_value=0)


def load_grid(res):
    """도엽별 격자경계를 합쳐 전국 격자 레이어를 만든다 (처음 한 번만, 이후 캐시)."""
    cache = CACHE / f"grid_{res}.gpkg"
    if cache.exists():
        return gpd.read_file(cache)
    parts = []
    with tempfile.TemporaryDirectory() as tmp:
        for zp in sorted(DATA.glob("_grid_border_grid_*.zip")):
            sheet = re.search(r"grid_([가-힣]{2})\.zip$", zp.name).group(1)
            shp = extract_shp(zp, f"grid_{sheet}_{res}", Path(tmp))
            parts.append(gpd.read_file(shp))
    grid = pd.concat(parts, ignore_index=True).drop_duplicates("GRID_CD")
    grid = gpd.GeoDataFrame(grid, geometry="geometry", crs=parts[0].crs)
    CACHE.mkdir(parents=True, exist_ok=True)
    grid.to_file(cache)
    return grid


def load_cells(res):
    grid = load_grid(res)
    pop = load_population(res)
    missing = pop.index.difference(grid["GRID_CD"])
    if len(missing):
        lost = pop.loc[missing].sum()
        print(f"  [{res}] 경계 없는 인구 격자 {len(missing)}개 (연도별 누락 인구: "
              + ", ".join(f"{y}={int(v)}" for y, v in lost.items()) + ")")
    cells = grid.merge(pop, left_on="GRID_CD", right_index=True, how="left").fillna(0)
    cells["area_km2"] = cells.geometry.area / 1e6
    return cells


def load_sigungu():
    cache = CACHE / "sigungu.gpkg"
    if cache.exists():
        return gpd.read_file(cache)
    with tempfile.TemporaryDirectory() as tmp:
        shp = extract_shp(DATA / "bnd_sigungu_00_2025_2Q.zip", "bnd_sigungu_00_2025_2Q", Path(tmp))
        sgg = gpd.read_file(shp)
    sgg["sido_cd"] = sgg["SIGUNGU_CD"].str[:2]
    sgg["geometry"] = sgg.geometry.simplify(100)
    CACHE.mkdir(parents=True, exist_ok=True)
    sgg.to_file(cache)
    return sgg


def load_sgis_urban(year):
    zp = next(DATA.glob(f"_grid_border_urban_*_{year}_*.zip"), None)
    if zp is None:
        return None
    parts = []
    with tempfile.TemporaryDirectory() as tmp:
        for kind in ["도시", "준도시"]:
            # 준도시 dbf의 지역명(URBAR_NM)이 UTF-8 중간에서 잘려 있어 이름 필드는 읽지 않는다
            g = gpd.read_file(extract_shp(zp, f"도시화경계_{year}_{kind}", Path(tmp)),
                              columns=["BASE_YEAR", "URBAR_ID"])
            g["kind"] = kind
            parts.append(g)
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)


# ---------------------------------------------------------------- DID 추출
def extract_did(cells, year, thr):
    """밀도 기준 이상 격자 → 변 공유 덩어리 → 인구 합계 기준 통과분만 DID."""
    dense = cells[cells[year] / cells["area_km2"] >= thr]
    if dense.empty:
        return gpd.GeoDataFrame(columns=["pop", "area_km2", "n_cells", "geometry"],
                                geometry="geometry", crs=cells.crs), dense.iloc[0:0]
    # 격자는 서로 겹치지 않으므로 coverage union이 빠르다. 꼭짓점만 닿는 격자는 별개 덩어리가 된다.
    merged = shapely.coverage_union_all(dense.geometry.values)
    blobs = gpd.GeoDataFrame(geometry=list(shapely.get_parts(merged)), crs=cells.crs)
    blobs["blob_id"] = range(len(blobs))

    pts = dense[["GRID_CD", year, "area_km2"]].copy()
    pts = gpd.GeoDataFrame(pts, geometry=dense.geometry.representative_point(), crs=cells.crs)
    pts = gpd.sjoin(pts, blobs, predicate="within", how="inner")
    stats = pts.groupby("blob_id").agg(pop=(year, "sum"), area_km2=("area_km2", "sum"),
                                       n_cells=("GRID_CD", "size"))
    did = blobs.join(stats, on="blob_id")
    did = did[did["pop"] >= MIN_POP].reset_index(drop=True)
    did_cells = dense[dense["GRID_CD"].isin(pts.loc[pts["blob_id"].isin(did["blob_id"]), "GRID_CD"])]
    return did, did_cells


# ---------------------------------------------------------------- 출력
def plot_did_facets(results, sido, res, thr, bbox=None, suffix="national"):
    fig, axes = plt.subplots(2, 3, figsize=(15, 11) if bbox is None else (16, 10))
    for ax, year in zip(axes.flat, YEARS):
        sido.boundary.plot(ax=ax, color="#9a9a9a", linewidth=0.4)
        did = results[(res, thr, year)]
        if len(did):
            did.plot(ax=ax, color="#c0392b", linewidth=0)
        area, n = did["area_km2"].sum(), len(did)
        dens = did["pop"].sum() / area if area else 0
        ax.set_title(f"{year}년  |  {n}개 · {area:,.0f} km² · {dens:,.0f}명/km²", fontsize=11)
        if bbox is not None:
            ax.set_xlim(bbox[0], bbox[2])
            ax.set_ylim(bbox[1], bbox[3])
        ax.set_axis_off()
    region = "전국" if bbox is None else "수도권"
    fig.suptitle(f"인구집중지구(DID) {region} · {res} 격자 · {thr:,}명/km² 이상 · 인구 {MIN_POP:,}명 이상",
                 fontsize=14)
    fig.text(0.5, 0.01, f"※ {DEFINITION_BREAK[0]}년 이전은 내국인 기준, {DEFINITION_BREAK[1]}년부터 내·외국인 전체 "
             "(SGIS 집계 기준 변경)", ha="center", fontsize=9, color="#555")
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    fig.savefig(OUT / "maps" / f"did_{res}_{thr}_{suffix}.png", dpi=200)
    plt.close(fig)


def change_layers(results, res, thr, y0, y1):
    """y0→y1 DID를 (유지, 신규, 소멸) 도형으로 나눈다."""
    a, b = results[(res, thr, y0)], results[(res, thr, y1)]
    ua = shapely.union_all(a.geometry.values) if len(a) else shapely.Polygon()
    ub = shapely.union_all(b.geometry.values) if len(b) else shapely.Polygon()
    return [
        (shapely.intersection(ua, ub), "#7f8c8d", f"{y0}·{y1} 모두 DID"),
        (shapely.difference(ub, ua), "#e67e22", f"{y1}년 새로 DID"),
        (shapely.difference(ua, ub), LOST_STYLE["edgecolor"], f"{y1}년에 DID에서 빠짐", LOST_STYLE),
    ]


# DID에서 빠진 곳: 빈 자리가 보이도록 빨간 테두리에 거의 투명한 채움
LOST_STYLE = dict(alpha=0.12, edgecolor="#d32f2f", linewidth=1.1)


def area_style(rest, default_alpha=1):
    """레이어 튜플의 넷째 값(투명도 숫자 또는 스타일 dict)을 dict로 맞춘다."""
    opt = rest[0] if rest else default_alpha
    return dict(opt) if isinstance(opt, dict) else dict(alpha=opt)


def plot_area(ax, geom, crs, color, opt):
    """면을 칠하고, opt에 edgecolor가 있으면 테두리를 따로 진하게 긋는다."""
    if geom.is_empty:
        return
    gs = gpd.GeoSeries([geom], crs=crs)
    gs.plot(ax=ax, color=color, alpha=opt.get("alpha", 1), linewidth=0)
    if "edgecolor" in opt:
        gs.boundary.plot(ax=ax, color=opt["edgecolor"], linewidth=opt.get("linewidth", 1))


def area_handle(color, opt, label):
    from matplotlib.colors import to_rgba
    return plt.Rectangle((0, 0), 1, 1, facecolor=to_rgba(color, opt.get("alpha", 1)),
                         edgecolor=opt.get("edgecolor", "none"), linewidth=opt.get("linewidth", 0) * 1.3,
                         label=label)


def draw_layers(ax, layers, crs, clip=None, legend_loc="lower right", fontsize=10):
    """(도형, 색, 이름[, 투명도]) 목록을 그리고 범례를 단다. clip이 있으면 그 안의 면적만 범례에 적는다."""
    handles = []
    for geom, color, label, *rest in layers:
        opt = area_style(rest)
        plot_area(ax, geom, crs, color, opt)
        km2 = (shapely.intersection(geom, clip) if clip is not None else geom).area / 1e6
        handles.append(area_handle(color, opt, f"{label} ({km2:,.1f} km²)" if clip is not None
                                   else f"{label} ({km2:,.0f} km²)"))
    ax.legend(handles=handles, loc=legend_loc, fontsize=fontsize, frameon=True)


def plot_change(results, sido, res, thr, y0, y1, bbox=None, suffix="national"):
    # 전국·수도권 축척에서는 테두리가 굵으면 빠진 면적이 부풀어 보이므로 얇게 긋는다
    layers = [(g, c, lbl, *[dict(o, linewidth=0.35) if isinstance(o, dict) else o for o in rest])
              for g, c, lbl, *rest in change_layers(results, res, thr, y0, y1)]
    fig, ax = plt.subplots(figsize=(9, 11) if bbox is None else (11, 10))
    sido.boundary.plot(ax=ax, color="#9a9a9a", linewidth=0.4)
    draw_layers(ax, layers, sido.crs)
    if bbox is not None:
        ax.set_xlim(bbox[0], bbox[2])
        ax.set_ylim(bbox[1], bbox[3])
    ax.set_axis_off()
    region = "전국" if bbox is None else "수도권"
    ax.set_title(f"DID 변화 {y0}→{y1} ({region} · {res} · {thr:,}명/km²)", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUT / "maps" / f"did_change_{y0}_{y1}_{res}_{thr}_{suffix}.png", dpi=200)
    plt.close(fig)


def comparison_layers(results, res, year):
    """DID 4,000 / DID 2,000 / SGIS 도시화지역(도시)를 (이름, 도형, 색) 목록으로 돌려준다."""
    urban = load_sgis_urban(year)
    sgis = shapely.union_all(urban.loc[urban["kind"] == "도시"].geometry.values)
    hi, lo = max(THRESHOLDS), min(THRESHOLDS)
    return [
        (f"DID {hi:,}명/km²", shapely.union_all(results[(res, hi, year)].geometry.values), "#8e1b10"),
        (f"DID {lo:,}명/km²", shapely.union_all(results[(res, lo, year)].geometry.values), "#e67e22"),
        ("SGIS 도시화지역(도시)", sgis, "#2e86c1"),
    ], urban.crs


def plot_compare_panels(results, sido, res, year, bbox=None, suffix="national"):
    layers, crs = comparison_layers(results, res, year)
    fig, axes = plt.subplots(1, 3, figsize=(18, 8) if bbox is None else (19, 6.6))
    for ax, (label, geom, color) in zip(axes, layers):
        sido.boundary.plot(ax=ax, color="#9a9a9a", linewidth=0.4)
        gpd.GeoSeries([geom], crs=crs).plot(ax=ax, color=color, linewidth=0)
        ax.set_title(f"{label}  ·  {geom.area / 1e6:,.0f} km²", fontsize=13)
        if bbox is not None:
            ax.set_xlim(bbox[0], bbox[2])
            ax.set_ylim(bbox[1], bbox[3])
        ax.set_axis_off()
    region = "전국" if bbox is None else "수도권"
    fig.suptitle(f"{year}년 도시지역 정의 비교 ({region}) · DID는 {res} 격자·인구 {MIN_POP:,}명 이상, "
                 "SGIS 도시화지역은 1km 격자 기준", fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(OUT / "maps" / f"compare_{year}_{res}_{suffix}.png", dpi=200)
    plt.close(fig)


def plot_compare_overlay(results, sido, res, year, bbox=None, suffix="national"):
    """세 정의를 한 장에 겹친다. 넓은 것(SGIS)을 아래에, 좁은 것(DID 4,000)을 위에 그린다."""
    layers, crs = comparison_layers(results, res, year)
    fig, ax = plt.subplots(figsize=(9, 11) if bbox is None else (11, 10))
    sido.boundary.plot(ax=ax, color="#9a9a9a", linewidth=0.4)
    handles = []
    for label, geom, color in reversed(layers):
        alpha = 0.35 if label.startswith("SGIS") else 1
        gpd.GeoSeries([geom], crs=crs).plot(ax=ax, color=color, alpha=alpha, linewidth=0)
        handles.append(plt.Rectangle((0, 0), 1, 1, color=color, alpha=alpha,
                                     label=f"{label} ({geom.area / 1e6:,.0f} km²)"))
    ax.legend(handles=handles[::-1], loc="lower right", fontsize=10, frameon=True)
    if bbox is not None:
        ax.set_xlim(bbox[0], bbox[2])
        ax.set_ylim(bbox[1], bbox[3])
    ax.set_axis_off()
    region = "전국" if bbox is None else "수도권"
    ax.set_title(f"{year}년 도시지역 정의 겹쳐보기 ({region} · DID {res})", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUT / "maps" / f"compare_overlay_{year}_{res}_{suffix}.png", dpi=200)
    plt.close(fig)


def city_extent(sigungu, name, results, res, thr, years):
    """도시 시군구 경계(합친 것)와 지도 범위를 돌려준다.
    범위는 시 경계가 아니라 시 안의 DID(years 중 하나라도) 범위에 맞춘다. 남원처럼 시 면적에 비해
    시가지가 작은 도시도 시가지가 크게 보이게 하기 위해서다. 한 변은 최소 MIN_SPAN m."""
    city = sigungu[CITY_ZOOMS[name](sigungu)]
    geom = shapely.union_all(city.geometry.values)
    did = shapely.union_all([shapely.union_all(results[(res, thr, y)].geometry.values) for y in years])
    x0, y0, x1, y1 = shapely.intersection(did, geom).bounds
    pad = max(3000, 0.08 * max(x1 - x0, y1 - y0))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = max(x1 - x0 + 2 * pad, y1 - y0 + 2 * pad, MIN_SPAN) / 2
    return city, geom, (cx - half, cy - half, cx + half, cy + half)


MIN_SPAN = 12000


def city_stats(region, city, y0, y1):
    r = region[region["SIGUNGU_CD"].isin(city["SIGUNGU_CD"])].groupby("year")[
        ["did_area_km2", "did_pop"]].sum()
    out = {}
    for y in (y0, y1):
        area, pop = r.loc[y] if y in r.index else (0, 0)
        out[y] = (area, pop / area if area else 0)
    return out


BASEMAP_NOTE = "배경: Esri World Topographic Map (녹지·수계), Esri World Hillshade (지형 음영)"
HILLSHADE_URL = ("https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/World_Hillshade/MapServer/tile/"
                 "{z}/{y}/{x}")


def draw_basemap(ax, crs, bbox):
    """연한 배경지도: Esri 지형도(숲·물·도로)를 흐리게 깔고 지형 음영을 얇게 겹친다.
    타일은 output/_cache/tiles에 저장한다. 인터넷이 안 되면 배경 없이 그린다."""
    try:
        import contextily as ctx
        import xyzservices
        ctx.set_cache_dir(OUT / "_cache" / "tiles")
        ctx.add_basemap(ax, crs=crs, source=ctx.providers.Esri.WorldTopoMap, alpha=0.45, zorder=0,
                        attribution=False)
        shade = xyzservices.TileProvider(name="Esri.WorldHillshade", url=HILLSHADE_URL, attribution="Esri")
        ctx.add_basemap(ax, crs=crs, source=shade, alpha=0.25, zorder=0, attribution=False)
        ax.text(0.005, 0.005, BASEMAP_NOTE, transform=ax.transAxes, fontsize=6.5, color="#777", zorder=10)
    except Exception as e:  # 배경지도는 없어도 분석에는 지장 없음
        warnings.warn(f"배경지도를 못 불러옴: {e}")
    ax.set_xlim(bbox[0], bbox[2])
    ax.set_ylim(bbox[1], bbox[3])


def draw_city_frame(ax, sigungu, city_geom, bbox, name=None):
    ax.set_xlim(bbox[0], bbox[2])
    ax.set_ylim(bbox[1], bbox[3])
    draw_basemap(ax, sigungu.crs, bbox)
    sigungu.boundary.plot(ax=ax, color="#8a8a8a", linewidth=0.4)
    gpd.GeoSeries([city_geom], crs=sigungu.crs).boundary.plot(ax=ax, color="#222", linewidth=1.1)
    focus = CITY_FOCUS.get(name)
    city = sigungu[CITY_ZOOMS[name](sigungu)] if name else None
    if focus and city is not None and len(city) > 1:  # 순위에 오른 구·군을 보라 점선으로 강조
        sel = city[city["SIGUNGU_NM"] == focus]
        sel.boundary.plot(ax=ax, color="#6a1b9a", linewidth=2.2, linestyle=(0, (4, 2)), zorder=4)
        pt = sel.geometry.iloc[0].representative_point()
        ax.text(pt.x, pt.y, focus.replace("창원시 ", ""), color="#6a1b9a", fontsize=10, fontweight="bold",
                ha="center", zorder=9, bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.75))
    ax.set_xlim(bbox[0], bbox[2])
    ax.set_ylim(bbox[1], bbox[3])
    ax.set_axis_off()


def plot_city_zoom(results, sigungu, region, name, res, thr, y0, y1):
    """도시 확대도: 왼쪽 y0→y1 DID 변화, 오른쪽 y1년 DID 4,000 / 2,000 / SGIS 도시 겹쳐보기."""
    city, geom, bbox = city_extent(sigungu, name, results, res, thr, (y0, y1))
    st = city_stats(region, city, y0, y1)
    fig, axes = plt.subplots(1, 2, figsize=(17, 8.6))

    draw_city_frame(axes[0], sigungu, geom, bbox, name)
    draw_layers(axes[0], change_layers(results, res, thr, y0, y1), sigungu.crs, clip=geom, fontsize=9)
    axes[0].set_title(f"DID 변화 {y0}→{y1} ({thr:,}명/km² 기준)", fontsize=12)

    draw_city_frame(axes[1], sigungu, geom, bbox, name)
    cmp, _ = comparison_layers(results, res, y1)
    layers = [(g, c, lbl, 0.35 if lbl.startswith("SGIS") else 1) for lbl, g, c in reversed(cmp)]
    draw_layers(axes[1], layers, sigungu.crs, clip=geom, fontsize=9)
    axes[1].set_title(f"{y1}년 도시지역 정의 비교", fontsize=12)

    (a0, d0), (a1, d1) = st[y0], st[y1]
    chg = f"{100 * (d1 / d0 - 1):+.1f}%" if d0 else "-"
    fig.suptitle(f"{name}  ·  DID({thr:,}명/km²) 면적 {a0:,.1f}→{a1:,.1f} km², "
                 f"평균 밀도 {d0:,.0f}→{d1:,.0f}명/km² ({chg})  ·  굵은 선: 시 경계, 범례 면적은 시 경계 안",
                 fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(OUT / "maps" / "cities" / f"city_{name}_{res}.png", dpi=200)
    plt.close(fig)


def plot_city_grid(results, sigungu, region, res, thr, y0, y1):
    """6개 도시 변화 지도를 한 장에 모은다."""
    names = list(CITY_ZOOMS)
    ncol = 3
    nrow = -(-len(names) // ncol)
    fig, axes = plt.subplots(nrow, ncol, figsize=(18, 6.4 * nrow))
    for ax, name in zip(axes.flat, names):
        city, geom, bbox = city_extent(sigungu, name, results, res, thr, (y0, y1))
        draw_city_frame(ax, sigungu, geom, bbox, name)
        draw_layers(ax, change_layers(results, res, thr, y0, y1), sigungu.crs, clip=geom, fontsize=8)
        (a0, d0), (a1, d1) = city_stats(region, city, y0, y1).values()
        ax.set_title(f"{name}  ·  밀도 {d0:,.0f}→{d1:,.0f}명/km² ({100 * (d1 / d0 - 1):+.1f}%)",
                     fontsize=12)
    for ax in list(axes.flat)[len(names):]:
        ax.set_axis_off()
    fig.suptitle(f"도시별 DID 변화 {y0}→{y1} ({res} 격자 · {thr:,}명/km² · 인구 {MIN_POP:,}명 이상)",
                 fontsize=14)
    fig.tight_layout(rect=(0, 0, 1, 0.96), h_pad=4)
    fig.savefig(OUT / "maps" / "cities" / f"cities_change_{y0}_{y1}_{res}_{thr}.png", dpi=200)
    plt.close(fig)


FACILITY_GPKG = DATA / "facilities" / "facilities.gpkg"
# 점 레이어 그리기 스타일: (레이어, 조건, 범례 이름, matplotlib 스타일)
SCHOOL_COLOR = "#00897b"
FACILITY_STYLES = [
    ("schools", lambda g: (g["subkind"] == "초등학교") & (g["found_year"] < 2000), "초등학교",
     dict(marker="o", s=16, facecolor="white", edgecolor=SCHOOL_COLOR, linewidth=0.9)),
    ("schools", lambda g: (g["subkind"] == "중학교") & (g["found_year"] < 2000), "중학교",
     dict(marker="^", s=20, facecolor="white", edgecolor=SCHOOL_COLOR, linewidth=0.9)),
    ("schools", lambda g: (g["subkind"] == "고등학교") & (g["found_year"] < 2000), "고등학교",
     dict(marker="s", s=16, facecolor="white", edgecolor=SCHOOL_COLOR, linewidth=0.9)),
    ("schools", lambda g: (g["subkind"] == "초등학교") & (g["found_year"] >= 2000), "초등학교 (2000년 이후 설립)",
     dict(marker="o", s=18, facecolor=SCHOOL_COLOR, edgecolor="white", linewidth=0.4)),
    ("schools", lambda g: (g["subkind"] == "중학교") & (g["found_year"] >= 2000), "중학교 (2000년 이후 설립)",
     dict(marker="^", s=22, facecolor=SCHOOL_COLOR, edgecolor="white", linewidth=0.4)),
    ("schools", lambda g: (g["subkind"] == "고등학교") & (g["found_year"] >= 2000), "고등학교 (2000년 이후 설립)",
     dict(marker="s", s=18, facecolor=SCHOOL_COLOR, edgecolor="white", linewidth=0.4)),
    ("health", lambda g: g["subkind"].isin(["보건소", "보건의료원"]), "보건소·보건의료원",
     dict(marker="P", s=80, facecolor="#d81b60", edgecolor="white", linewidth=0.6)),
    ("health", lambda g: g["subkind"].isin(["보건지소", "보건진료소"]), "보건지소·보건진료소",
     dict(marker="P", s=28, facecolor="#f48fb1", edgecolor="white", linewidth=0.4)),
    ("townhall", lambda g: g["subkind"] == "읍면동", "읍면동 행정복지센터",
     dict(marker="s", s=14, facecolor="#37474f", edgecolor="white", linewidth=0.3)),
    ("townhall", lambda g: g["subkind"].isin(["시군구", "시·도"]), "시·도청, 시·구·군청",
     dict(marker="*", s=230, facecolor="#111", edgecolor="white", linewidth=0.8)),
    ("urban_rail_station", lambda g: g.index == g.index, "도시철도역",
     dict(marker="o", s=14, facecolor="#6a1b9a", edgecolor="white", linewidth=0.3)),
    ("station", lambda g: g.index == g.index, "철도역 (국가철도)",
     dict(marker="D", s=70, facecolor="#1a237e", edgecolor="white", linewidth=0.8)),
    ("terminal", lambda g: g.index == g.index, "버스터미널",
     dict(marker="h", s=90, facecolor="#ef6c00", edgecolor="white", linewidth=0.7)),
    ("ic", lambda g: g.index == g.index, "고속도로 IC (영업소)",
     dict(marker="X", s=70, facecolor="#2e7d32", edgecolor="white", linewidth=0.6)),
]
# 선 레이어: (레이어, 범례 이름, matplotlib 스타일)
FACILITY_LINES = [
    ("highway", "고속도로", dict(color="#2e7d32", linewidth=2.0, linestyle="-", alpha=0.85)),
    ("rail_line", "철도 (국가철도)", dict(color="#263238", linewidth=1.5, linestyle=(0, (7, 2)), alpha=0.85)),
    ("urban_rail_line", "도시철도 (역을 이은 개략선)", dict(color="#6a1b9a", linewidth=1.3, linestyle=(0, (4, 2)),
                                                       alpha=0.8)),
]


def load_facilities():
    if not FACILITY_GPKG.exists():
        return None
    import pyogrio
    return {name: gpd.read_file(FACILITY_GPKG, layer=name)
            for name, _ in pyogrio.list_layers(FACILITY_GPKG)}


def plot_city_facilities(results, sigungu, region, fac, name, res, thr, y0, y1):
    """도시 확대도 위에 공공시설·교통시설을 얹는다. 배경은 y0→y1 DID 변화."""
    from matplotlib.lines import Line2D
    city, geom, bbox = city_extent(sigungu, name, results, res, thr, (y0, y1))
    fig, ax = plt.subplots(figsize=(15.5, 11))
    draw_city_frame(ax, sigungu, geom, bbox, name)
    handles = []
    for g, c, lbl, *rest in change_layers(results, res, thr, y0, y1):
        opt = area_style(rest, default_alpha=0.55)
        plot_area(ax, g, sigungu.crs, c, opt)
        handles.append(area_handle(c, opt, lbl))

    view = shapely.box(*bbox)
    for layer, label, style in FACILITY_LINES:
        lines = fac.get(layer)
        if lines is None:
            continue
        sel = lines[(lines["city"] == name) & lines.intersects(view)]
        if len(sel):
            sel.plot(ax=ax, **style)
            handles.append(Line2D([], [], label=label, **style))
    for layer, cond, label, style in FACILITY_STYLES:
        g = fac.get(layer)
        if g is None:
            continue
        g = g[(g["city"] == name) & g.within(view)]
        g = g[cond(g)]
        if g.empty:
            continue
        ax.scatter(g.geometry.x, g.geometry.y, zorder=5, **style)
        handles.append(Line2D([], [], linestyle="", marker=style["marker"],
                              markersize=(style["s"] ** 0.5) * 0.9, markerfacecolor=style["facecolor"],
                              markeredgecolor=style["edgecolor"], label=f"{label} ({len(g)})"))
    ax.set_xlim(bbox[0], bbox[2])
    ax.set_ylim(bbox[1], bbox[3])
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=9.5, frameon=False,
              title="범례 (괄호: 지도 범위 안 개수)", title_fontsize=10, alignment="left")
    st = city_stats(region, city, y0, y1)
    (a0, d0), (a1, d1) = st[y0], st[y1]
    ax.set_title(f"{name} · DID 변화 {y0}→{y1}({thr:,}명/km²)와 공공·교통시설 입지\n"
                 f"DID 면적 {a0:,.1f}→{a1:,.1f} km², 평균 밀도 {d0:,.0f}→{d1:,.0f}명/km²  ·  "
                 "시설 위치는 2026년 기준, 굵은 선은 시 경계", fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "maps" / "cities" / f"facilities_{name}_{res}.png", dpi=200)
    plt.close(fig)


def facilities_in_did(results, sigungu, fac, res, thr, y0, y1):
    """시 경계 안 시설이 DID 변화 구역(유지·신규·소멸·비DID) 중 어디에 있는지 센다."""
    rows = []
    for name in CITY_ZOOMS:
        city, geom, _ = city_extent(sigungu, name, results, res, thr, (y0, y1))
        zones = {lbl.split(" (")[0]: g for g, _, lbl, *_ in change_layers(results, res, thr, y0, y1)}
        for layer in ["schools", "health", "townhall", "station", "urban_rail_station", "terminal", "ic"]:
            g = fac.get(layer)
            if g is None:
                continue
            g = g[(g["city"] == name) & g.within(geom)]
            groups = [("전체", g)]
            if layer == "schools":
                groups.append(("2000년 이후 설립", g[g["found_year"] >= 2000]))
            for sub, gg in groups:
                if gg.empty:
                    continue
                row = {"city": name, "layer": layer, "group": sub, "total": len(gg)}
                inside = pd.Series(False, index=gg.index)
                for zlabel, zg in zones.items():
                    hit = gg.within(zg) if not zg.is_empty else pd.Series(False, index=gg.index)
                    row[zlabel] = int(hit.sum())
                    inside |= hit
                row["DID 밖"] = int((~inside).sum())
                rows.append(row)
    return pd.DataFrame(rows)


TREND_REGIONS = {
    "national": ("전국", lambda t: t.index == t.index),
    "noncapital": ("비수도권 (서울·경기·인천 제외)", lambda t: ~t["sido_cd"].isin(CAPITAL_REGION)),
    "capital": ("수도권 (서울·경기·인천)", lambda t: t["sido_cd"].isin(CAPITAL_REGION)),
}


def region_density(reg, cond):
    g = reg[cond(reg)].groupby("year")[["did_area_km2", "did_pop"]].sum()
    return g["did_pop"] / g["did_area_km2"]


def plot_density_compare(regions, keys=("capital", "noncapital")):
    """수도권과 비수도권 DID 밀도를 한 그래프에 비교. 색 = 권역, 선 모양 = 밀도 기준."""
    colors = {"capital": "#1f5fa8", "noncapital": "#c0392b"}
    styles = {min(THRESHOLDS): "-", max(THRESHOLDS): "--"}
    y0, yb, y1 = YEARS[0], DEFINITION_BREAK[1], YEARS[-1]
    fig, ax = plt.subplots(figsize=(10, 6.4))
    for key in keys:
        label, cond = TREND_REGIONS[key]
        for thr, reg in regions.items():
            dens = region_density(reg, cond)
            solid = styles[thr] == "-"
            ax.plot(dens.index, dens.values, marker="o" if solid else "s", markersize=5 if solid else 4,
                    color=colors[key], linestyle=styles[thr], linewidth=2 if solid else 1.4,
                    alpha=1 if solid else 0.75, label=f"{label.split(' (')[0]} · {thr:,}명/km² 기준")
            if solid:  # 숫자 라벨은 주 분석 기준에만 단다
                for y, v in dens.items():
                    ax.annotate(f"{v:,.0f}", (y, v), textcoords="offset points", xytext=(0, 8),
                                ha="center", fontsize=8.5, color=colors[key])
            ax.text(1.01, dens[y1], f"{100 * (dens[y1] / dens[y0] - 1):+.1f}% · "
                                    f"{100 * (dens[y1] / dens[yb] - 1):+.1f}%",
                    transform=ax.get_yaxis_transform(), va="center", fontsize=8.5, color=colors[key],
                    alpha=1 if solid else 0.75)
    ax.text(1.01, 1.02, f"{y0}→{y1} · {yb}→{y1}", transform=ax.transAxes, fontsize=8.5, color="#555")
    ax.axvspan(*DEFINITION_BREAK, color="#ddd", alpha=0.6, lw=0)
    ax.text(sum(DEFINITION_BREAK) / 2, 0.02, "집계 기준 변경\n(내국인→내·외국인)", ha="center",
            transform=ax.get_xaxis_transform(), fontsize=8, color="#666")
    ax.set_xticks(YEARS)
    ax.grid(alpha=0.3)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.set_ylabel("DID 평균 인구밀도 (명/km²)")
    ax.set_title(f"DID 인구밀도 변화 — 수도권 vs 비수도권\n({MAIN_RES} 격자, 인구 {MIN_POP:,}명 이상 덩어리, "
                 "수도권 = 서울·경기·인천)", fontsize=12)
    ax.legend(loc="upper right", fontsize=8.5, ncol=2)
    fig.tight_layout()
    fig.subplots_adjust(right=0.82)
    fig.savefig(OUT / "maps" / "did_density_trend_capital_vs_noncapital.png", dpi=200)
    plt.close(fig)


def plot_density_trend_regions(regions):
    """권역별 DID 평균 인구밀도 시계열 (기준별 선 하나씩). 권역마다 그림 한 장."""
    colors = {2000: "#c0392b", 4000: "#2c3e50"}
    rows = []
    for key, (label, cond) in TREND_REGIONS.items():
        fig, ax = plt.subplots(figsize=(9, 5.6))
        for thr, reg in regions.items():
            g = reg[cond(reg)].groupby("year")[["did_area_km2", "did_pop"]].sum()
            dens = g["did_pop"] / g["did_area_km2"]
            ax.plot(dens.index, dens.values, marker="o", color=colors.get(thr), linewidth=2,
                    label=f"{thr:,}명/km² 기준")
            for y, v in dens.items():
                ax.annotate(f"{v:,.0f}", (y, v), textcoords="offset points", xytext=(0, 8),
                            ha="center", fontsize=8.5, color=colors.get(thr))
            y0, yb, y1 = YEARS[0], DEFINITION_BREAK[1], YEARS[-1]
            ax.text(1.01, dens[y1], f"{y0}→{y1} {100 * (dens[y1] / dens[y0] - 1):+.1f}%\n"
                                    f"{yb}→{y1} {100 * (dens[y1] / dens[yb] - 1):+.1f}%",
                    transform=ax.get_yaxis_transform(), va="center", fontsize=9, color=colors.get(thr))
            for y in YEARS:
                rows.append({"region": label, "threshold": thr, "year": y,
                             "did_area_km2": g.loc[y, "did_area_km2"], "did_pop": int(g.loc[y, "did_pop"]),
                             "did_density": round(dens[y], 0)})
        ax.axvspan(*DEFINITION_BREAK, color="#ddd", alpha=0.6, lw=0)
        ax.text(sum(DEFINITION_BREAK) / 2, 0.02, "집계 기준 변경\n(내국인→내·외국인)", ha="center",
                transform=ax.get_xaxis_transform(), fontsize=8, color="#666")
        ax.set_xticks(YEARS)
        ax.grid(alpha=0.3)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))
        ax.set_ylabel("DID 평균 인구밀도 (명/km²)")
        ax.set_title(f"DID 인구밀도 변화 — {label}\n({MAIN_RES} 격자, 인구 {MIN_POP:,}명 이상 덩어리, "
                     "DID 인구 ÷ DID 면적)", fontsize=12)
        ax.legend(loc="upper right", fontsize=9)
        fig.tight_layout()
        fig.subplots_adjust(right=0.80)
        fig.savefig(OUT / "maps" / f"did_density_trend_{key}.png", dpi=200)
        plt.close(fig)
    pd.DataFrame(rows).to_csv(OUT / "tables" / "did_density_by_region.csv", index=False, encoding="utf-8-sig")


def plot_trend(summary, res):
    s = summary[summary["res"] == res]
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.8))
    metrics = [("did_area_km2", "DID 면적 (km²)"), ("did_density", "DID 평균 인구밀도 (명/km²)"),
               ("pop_share_pct", "전체 인구 중 DID 거주 비율 (%)")]
    colors = {2000: "#c0392b", 4000: "#2c3e50"}
    for ax, (col, label) in zip(axes, metrics):
        for thr, g in s.groupby("threshold"):
            ax.plot(g["year"], g[col], marker="o", color=colors.get(thr), label=f"{thr:,}명/km² 기준")
        ax.axvspan(*DEFINITION_BREAK, color="#ddd", alpha=0.6, lw=0)
        ax.set_title(label, fontsize=12)
        ax.set_xticks(YEARS)
        ax.grid(alpha=0.3)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    axes[0].legend(fontsize=9)
    fig.suptitle(f"DID 시계열 ({res} 격자, 인구 {MIN_POP:,}명 이상)  ·  회색 구간: 집계 기준 변경 "
                 f"({DEFINITION_BREAK[0]} 내국인 → {DEFINITION_BREAK[1]} 내·외국인)", fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "maps" / f"did_trend_{res}.png", dpi=200)
    plt.close(fig)


def region_table(did_cells_main, sigungu):
    """DID 격자를 중심점 기준으로 2025년 시군구에 배정해 시군구별 DID 면적·인구를 집계한다.
    격자 위치가 고정이라 모든 연도를 같은 시군구 경계로 비교할 수 있다."""
    rows = []
    for year, cells in did_cells_main.items():
        pts = gpd.GeoDataFrame(cells[["GRID_CD", year, "area_km2"]],
                               geometry=cells.geometry.representative_point(), crs=cells.crs)
        pts = gpd.sjoin(pts, sigungu[["SIGUNGU_CD", "SIGUNGU_NM", "sido_cd", "geometry"]],
                        predicate="within", how="left")
        g = pts.groupby(["sido_cd", "SIGUNGU_CD", "SIGUNGU_NM"]).agg(
            did_pop=(year, "sum"), did_area_km2=("area_km2", "sum"))
        g["year"] = year
        rows.append(g.reset_index())
    t = pd.concat(rows)
    t["sido"] = t["sido_cd"].map(SIDO)
    return t


def sido_table(region):
    t = region.groupby(["year", "sido_cd", "sido"], as_index=False)[["did_area_km2", "did_pop"]].sum()
    t["did_density"] = (t["did_pop"] / t["did_area_km2"]).round(0)
    return t


def sigungu_area_table(region, y0, y1):
    """시군구별 DID 면적 증감 (km²) 순위. 2000년에 DID가 없던 곳도 포함한다."""
    w = region.pivot_table(index=["sido", "SIGUNGU_CD", "SIGUNGU_NM"], columns="year",
                           values=["did_area_km2", "did_pop"], aggfunc="sum").fillna(0)
    t = pd.DataFrame(index=w.index)
    for y in (y0, y1):
        t[f"area_{y}"] = w[("did_area_km2", y)]
        t[f"density_{y}"] = (w[("did_pop", y)] / t[f"area_{y}"]).where(t[f"area_{y}"] > 0).round(0)
    t["area_chg_km2"] = (t[f"area_{y1}"] - t[f"area_{y0}"]).round(2)
    t["area_chg_pct"] = (100 * (t[f"area_{y1}"] / t[f"area_{y0}"] - 1)).where(t[f"area_{y0}"] > 0).round(1)
    t["density_chg_pct"] = (100 * (t[f"density_{y1}"] / t[f"density_{y0}"] - 1)).round(1)
    t["share_of_total_chg_pct"] = (100 * t["area_chg_km2"] / t["area_chg_km2"].sum()).round(1)
    t = t.sort_values("area_chg_km2", ascending=False).reset_index()
    t.insert(0, "rank", range(1, len(t) + 1))
    return t


def sigungu_change_table(region, summary, y0, y1, min_area=SGG_MIN_AREA):
    """시군구별 DID 밀도 변화. 두 해 모두 DID 면적이 min_area km² 이상인 곳만 비교한다."""
    w = region.pivot_table(index=["sido", "SIGUNGU_CD", "SIGUNGU_NM"], columns="year",
                           values=["did_area_km2", "did_pop"], aggfunc="sum")
    t = pd.DataFrame(index=w.index)
    for y in (y0, y1):
        t[f"area_{y}"] = w[("did_area_km2", y)]
        t[f"pop_{y}"] = w[("did_pop", y)]
        t[f"density_{y}"] = (t[f"pop_{y}"] / t[f"area_{y}"]).round(0)
    t = t[(t[f"area_{y0}"] >= min_area) & (t[f"area_{y1}"] >= min_area)].copy()
    t["area_chg_pct"] = (100 * (t[f"area_{y1}"] / t[f"area_{y0}"] - 1)).round(1)
    t["pop_chg_pct"] = (100 * (t[f"pop_{y1}"] / t[f"pop_{y0}"] - 1)).round(1)
    t["density_chg_pct"] = (100 * (t[f"density_{y1}"] / t[f"density_{y0}"] - 1)).round(1)

    nat = summary[(summary["res"] == MAIN_RES) & (summary["threshold"] == MAIN_THR)].set_index("year")
    nat_chg = 100 * (nat.loc[y1, "did_density"] / nat.loc[y0, "did_density"] - 1)
    t["vs_national_pp"] = (t["density_chg_pct"] - nat_chg).round(1)
    t = t.sort_values("density_chg_pct").reset_index()
    t.insert(0, "rank", range(1, len(t) + 1))
    return t, nat.loc[y0, "did_density"], nat.loc[y1, "did_density"], nat_chg


def compare_sgis_urban(results, res, thr):
    rows = []
    for year in YEARS:
        urban = load_sgis_urban(year)
        if urban is None:
            continue
        did = results[(res, thr, year)]
        ud = shapely.union_all(did.geometry.values)
        for kind, sel in [("도시", urban["kind"] == "도시"), ("도시+준도시", slice(None))]:
            uu = shapely.union_all(urban.loc[sel].geometry.values)
            inter = shapely.intersection(ud, uu).area
            union = shapely.union(ud, uu).area
            rows.append({"year": year, "res": res, "threshold": thr, "sgis_urban": kind,
                         "did_area_km2": round(ud.area / 1e6, 1),
                         "sgis_area_km2": round(uu.area / 1e6, 1),
                         "overlap_km2": round(inter / 1e6, 1),
                         "did_inside_sgis_pct": round(100 * inter / ud.area, 1),
                         "iou": round(inter / union, 3)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 실행
def main():
    for d in ["tables", "gpkg", "maps"]:
        (OUT / d).mkdir(parents=True, exist_ok=True)
    gpkg = OUT / "gpkg" / "did_polygons.gpkg"
    gpkg.unlink(missing_ok=True)

    sigungu = load_sigungu()
    sido = sigungu.dissolve("sido_cd").reset_index()
    capital = CAPITAL_BBOX

    results, did_cells_by_thr, rows = {}, {}, []
    for res in RESOLUTIONS:
        print(f"[{res}] 격자 불러오는 중...")
        cells = load_cells(res)
        for year in YEARS:
            total_pop = cells[year].sum()
            for thr in THRESHOLDS:
                did, did_cells = extract_did(cells, year, thr)
                results[(res, thr, year)] = did
                if res == MAIN_RES:
                    did_cells_by_thr.setdefault(thr, {})[year] = did_cells
                area, pop = did["area_km2"].sum(), did["pop"].sum()
                rows.append({"year": year, "res": res, "threshold": thr, "n_did": len(did),
                             "did_area_km2": round(area, 2), "did_pop": int(pop),
                             "did_density": round(pop / area, 0) if area else None,
                             "total_pop": int(total_pop),
                             "pop_share_pct": round(100 * pop / total_pop, 2)})
                did.drop(columns="blob_id").to_file(gpkg, layer=f"did_{res}_{thr}_{year}")
                print(f"  {year} {thr:>5,}명/km²: DID {len(did):>4}개, {area:>8,.1f} km², "
                      f"{pop / area if area else 0:>7,.0f}명/km²")

    summary = pd.DataFrame(rows)
    summary.to_csv(OUT / "tables" / "did_summary.csv", index=False, encoding="utf-8-sig")
    regions = {thr: region_table(did_cells_by_thr[thr], sigungu) for thr in THRESHOLDS}
    region = regions[MAIN_THR]
    plot_density_trend_regions(regions)
    plot_density_compare(regions)
    sido_table(region).to_csv(OUT / "tables" / "did_by_sido.csv", index=False, encoding="utf-8-sig")
    region.to_csv(OUT / "tables" / "did_by_sigungu.csv", index=False, encoding="utf-8-sig")
    for y0 in (YEARS[0], DEFINITION_BREAK[1]):
        chg, d0, d1, nat_chg = sigungu_change_table(region, summary, y0, YEARS[-1])
        chg.to_csv(OUT / "tables" / f"sigungu_density_change_{y0}_{YEARS[-1]}.csv",
                   index=False, encoding="utf-8-sig")
        sigungu_area_table(region, y0, YEARS[-1]).to_csv(
            OUT / "tables" / f"sigungu_area_change_{y0}_{YEARS[-1]}.csv", index=False, encoding="utf-8-sig")
        print(f"\n[시군구 DID 밀도 감소 Top 10, {y0}→{YEARS[-1]}] 전국 DID 밀도 "
              f"{d0:,.0f}→{d1:,.0f}명/km² ({nat_chg:+.1f}%), 비교 대상 {len(chg)}개 시군구")
        print(chg.head(10)[["rank", "sido", "SIGUNGU_NM", f"density_{y0}", f"density_{YEARS[-1]}",
                            "density_chg_pct", "vs_national_pp", "area_chg_pct", "pop_chg_pct"]]
              .to_string(index=False))
    pd.concat([compare_sgis_urban(results, MAIN_RES, thr) for thr in THRESHOLDS]).to_csv(
        OUT / "tables" / "did_vs_sgis_urban.csv", index=False, encoding="utf-8-sig")

    print("지도 그리는 중...")
    for res in RESOLUTIONS:
        plot_trend(summary, res)
    for thr in THRESHOLDS:
        plot_did_facets(results, sido, MAIN_RES, thr)
        plot_did_facets(results, sido, MAIN_RES, thr, bbox=capital, suffix="capital")
    plot_change(results, sido, MAIN_RES, MAIN_THR, YEARS[0], YEARS[-1])
    plot_change(results, sido, MAIN_RES, MAIN_THR, CHANGE_START, YEARS[-1])
    plot_change(results, sido, MAIN_RES, MAIN_THR, CHANGE_START, YEARS[-1], bbox=capital, suffix="capital")
    # DID 두 기준과 SGIS 도시화지역(도시) 비교 — SGIS 경계가 있는 연도만
    for year in [y for y in YEARS if load_sgis_urban(y) is not None]:
        for bbox, suffix in [(None, "national"), (capital, "capital")]:
            plot_compare_panels(results, sido, MAIN_RES, year, bbox, suffix)
            plot_compare_overlay(results, sido, MAIN_RES, year, bbox, suffix)
    (OUT / "maps" / "cities").mkdir(exist_ok=True)
    for name in CITY_ZOOMS:
        plot_city_zoom(results, sigungu, region, name, MAIN_RES, MAIN_THR, CHANGE_START, YEARS[-1])
    plot_city_grid(results, sigungu, region, MAIN_RES, MAIN_THR, CHANGE_START, YEARS[-1])
    # 공공·교통시설 레이어가 있으면 (scripts/fetch_facilities.py) 시설 지도와 집계표도 만든다
    fac = load_facilities()
    if fac:
        for name in CITY_ZOOMS:
            plot_city_facilities(results, sigungu, region, fac, name, MAIN_RES, MAIN_THR, CHANGE_START, YEARS[-1])
        facilities_in_did(results, sigungu, fac, MAIN_RES, MAIN_THR, CHANGE_START, YEARS[-1]).to_csv(
            OUT / "tables" / "facilities_in_did.csv", index=False, encoding="utf-8-sig")
    print(f"완료 → {OUT}")


if __name__ == "__main__":
    main()
