"""2000→2024년에 DID에서 빠진 격자(빈 네모)의 사람들이 어디로 이사 갔는지 읍면동 이동자료로 추정한다.

이동자료(MDIS)는 읍면동 단위라 격자 하나하나를 따라갈 수 없다. 그래서
  1. 읍면동마다 그해 인구 중 빈 네모 격자에 사는 비율(빈 네모 비율)을 격자 인구로 구하고,
  2. 그 읍면동에서 다른 읍면동으로 나간 이동자 × 빈 네모 비율 = 빈 네모에서 나간 이동자(추정)로 본다
     (같은 읍면동 안에서는 어느 격자에서 나갔든 나가는 비율이 같다고 가정).
  3. 도착지는 도착 읍면동 인구의 구역 구성(유지 DID / 신규 DID / 빈 네모 / 비DID) 비율로 나누고,
     도시 밖이면 도시권 시군구 / 도시권 밖으로 나눈다.
같은 읍면동 안 이사는 뺀다(공식 이동자 수 정의와 같음). 비교 기준은 같은 도시 모든 읍면동에서 나간 이동자.

경계: vuski/admdongkor 행정동 경계(CC BY 4.0). 이동 연도별로 가까운 판을 쓰고, 코드가 판에 없으면
admdongkor의 MDIS용 중심점 좌표로 들어 있는 행정동을 찾는다.
"""
import sys
import warnings
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shapely
from matplotlib.lines import Line2D
from matplotlib.patches import FancyArrowPatch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import did_analysis as da  # noqa: E402
import migration_analysis as ma  # noqa: E402

ADM = da.DATA / "admdong"
OUT_MAP = da.OUT / "maps" / "migration"
OUT_TAB = da.OUT / "tables"
RES, THR, Y0, Y1 = da.MAIN_RES, da.MAIN_THR, da.YEARS[0], da.YEARS[-1]
# 이동 연도 → 행정동 경계 판
VERSION = {2005: "20121210", 2010: "20121210", 2015: "20160201", 2020: "20200701", 2024: "20240701"}
ZONES = ["유지 DID", "신규 DID", "빈 네모", "비DID"]
DEST = ZONES + ["도시권 다른 시군구", "도시권 밖"]
DEST_COLORS = {"유지 DID": "#7f8c8d", "신규 DID": "#e67e22", "빈 네모": "#d32f2f", "비DID": "#c5b358",
               "도시권 다른 시군구": "#5c6bc0", "도시권 밖": "#bdbdbd"}
ADM_NOTE = "행정동 경계: vuski/admdongkor (CC BY 4.0)"


def cell_zones(cells, cities):
    """격자 중심점에 2000·2024 DID 여부와 구역, 도시 이름을 붙인다 (6개 도시 안 격자만)."""
    gpkg = da.OUT / "gpkg" / "did_polygons.gpkg"
    u0 = shapely.union_all(gpd.read_file(gpkg, layer=f"did_{RES}_{THR}_{Y0}").geometry.values)
    u1 = shapely.union_all(gpd.read_file(gpkg, layer=f"did_{RES}_{THR}_{Y1}").geometry.values)
    pts = gpd.GeoDataFrame(cells.drop(columns="geometry"), geometry=cells.geometry.centroid, crs=cells.crs)
    pts = gpd.sjoin(pts, cities, predicate="within").drop(columns="index_right")
    in0 = shapely.contains_xy(u0, pts.geometry.x, pts.geometry.y)
    in1 = shapely.contains_xy(u1, pts.geometry.x, pts.geometry.y)
    pts["zone"] = np.select([in0 & in1, ~in0 & in1, in0 & ~in1], ZONES[:3], ZONES[3])
    return pts


def load_dongs(version, crs):
    g = gpd.read_file(ADM / f"HangJeongDong_ver{version}.geojson")
    if "adm_cd2" not in g:  # 옛 판은 10자리 코드가 없다 → 판 고유 키를 만들고 MDIS 코드는 중심점으로 찾는다
        g["adm_cd2"] = f"v{version}_" + g["adm_cd"].astype(str)
    return g[["adm_cd2", "adm_nm", "geometry"]].to_crs(crs)


def code_to_dong(codes, dongs, centroids):
    """MDIS 읍면동 코드 → 해당 판의 adm_cd2. 판에 없으면 MDIS 중심점이 들어 있는 행정동."""
    codes = pd.Index(pd.unique(codes))
    direct = codes[codes.isin(dongs["adm_cd2"])]
    out = pd.Series(direct, index=direct)
    rest = centroids[centroids["ADMCD"].isin(codes.difference(direct))]
    if len(rest):
        p = gpd.GeoDataFrame(rest, geometry=gpd.points_from_xy(rest["X"], rest["Y"]), crs=dongs.crs)
        hit = gpd.sjoin(p, dongs[["adm_cd2", "geometry"]], predicate="within")
        out = pd.concat([out, hit.set_index("ADMCD")["adm_cd2"]])
    return out[~out.index.duplicated()]


def dong_zone_pop(pts, dongs, year):
    """행정동 × 구역 인구 (격자 인구 연도는 이동 연도와 같거나 가장 가까운 해)."""
    py = min(da.YEARS, key=lambda y: abs(y - year))
    j = gpd.sjoin(pts[["city", "zone", py, "geometry"]], dongs[["adm_cd2", "geometry"]], predicate="within")
    t = j.pivot_table(index="adm_cd2", columns="zone", values=py, aggfunc="sum", fill_value=0)
    t = t.reindex(columns=ZONES, fill_value=0)
    city = j.groupby("adm_cd2")["city"].agg(lambda s: s.mode().iloc[0])
    return t, city


def trace(od, pts, centroids, sgg_unit, regions):
    rows, pairs = [], []
    for year, version in VERSION.items():
        dongs = load_dongs(version, pts.crs)
        zp, dcity = dong_zone_pop(pts, dongs, year)
        share = zp.div(zp.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
        f = od[(od["year"] == year) & (od["o_emd"] != od["d_emd"])]
        m = code_to_dong(pd.concat([f["o_emd"], f["d_emd"]]), dongs, centroids)
        f = f.assign(o=f["o_emd"].map(m), d=f["d_emd"].map(m)).dropna(subset=["o", "d"])
        f = f[(f["o"] != f["d"]) & f["o"].isin(dcity.index)]
        f["city"] = f["o"].map(dcity)
        f["d_unit"] = f["d_emd"].str[:5].map(sgg_unit)
        f["lost_w"] = f["o"].map(share["빈 네모"])
        for city, g in f.groupby("city"):
            same = g["d"].map(dcity) == city
            in_reg = ~same & g["d_unit"].isin(regions[city])
            for origin, w in (("빈 네모", g["lost_w"]), ("도시 전체", pd.Series(1.0, index=g.index))):
                n = g["persons"] * w
                dest = share.reindex(g.loc[same, "d"]).mul(n[same].values, axis=0).sum()
                r = {"city": city, "year": year, "origin": origin, "movers": n.sum(),
                     **{z: dest.get(z, 0) for z in ZONES},
                     "도시권 다른 시군구": n[in_reg].sum(), "도시권 밖": n[~same & ~in_reg].sum()}
                rows.append(r)
            gl = g[same | in_reg].assign(est=g["persons"] * g["lost_w"])
            pairs.append(gl.groupby(["city", "o", "d"])["est"].sum().reset_index().assign(version=version))
    return pd.DataFrame(rows), pd.concat(pairs)


def plot_city(name, pairs, sigungu, results, dongs24, summary, top=18):
    from matplotlib.colors import to_rgba
    city, geom, bbox = da.city_extent(sigungu, name, results, RES, THR, (Y0, Y1))
    fig, ax = plt.subplots(figsize=(13, 11))
    da.draw_city_frame(ax, sigungu, geom, bbox)
    for g, c, lbl, *rest in da.change_layers(results, RES, THR, Y0, Y1):
        da.plot_area(ax, g, sigungu.crs, c, da.area_style(rest, default_alpha=0.5))
    p = pairs[pairs["city"] == name].copy()
    # 판이 달라도 2024 판 행정동 위치로 모은다 (코드가 바뀐 동은 가장 가까운 2024 행정동 중심)
    cen = dongs24.set_index("adm_cd2").geometry.representative_point()
    old = pd.Index(pd.unique(pd.concat([p["o"], p["d"]]))).difference(cen.index)
    if len(old):
        allv = pd.concat([load_dongs(v, sigungu.crs) for v in sorted(set(VERSION.values()))])
        allv = allv.drop_duplicates("adm_cd2").set_index("adm_cd2").geometry.representative_point()
        near = gpd.sjoin_nearest(gpd.GeoDataFrame(geometry=allv.reindex(old).dropna(), crs=sigungu.crs),
                                 gpd.GeoDataFrame({"to": cen.index}, geometry=cen.values, crs=sigungu.crs))
        remap = near["to"]
        p["o"] = p["o"].replace(remap.to_dict())
        p["d"] = p["d"].replace(remap.to_dict())
    p = p.groupby(["o", "d"])["est"].sum().sort_values(ascending=False)
    view = shapely.box(*bbox)
    p = p[[view.contains(cen[o]) and view.contains(cen[d]) for o, d in p.index]].head(top)
    wmax = p.iloc[0] if len(p) else 1
    zone_of = {}
    for (o, d), v in p.items():
        a, b = cen[o], cen[d]
        ax.add_patch(FancyArrowPatch((a.x, a.y), (b.x, b.y), connectionstyle="arc3,rad=0.2", arrowstyle="-|>",
                                     mutation_scale=9 + 9 * v / wmax, linewidth=0.7 + 5 * v / wmax,
                                     color="#4a148c", alpha=0.8, zorder=6))
    nm = dongs24.set_index("adm_cd2")["adm_nm"].str.split().str[-1]
    for code in dict.fromkeys(c for pair in p.index for c in pair):
        ax.text(cen[code].x, cen[code].y, nm.get(code, ""), fontsize=7, ha="center", va="bottom", zorder=8,
                bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.6))
    s = summary[(summary["city"] == name) & (summary["origin"] == "빈 네모")][DEST].sum()
    s = 100 * s / s.sum()
    base = summary[(summary["city"] == name) & (summary["origin"] == "도시 전체")][DEST].sum()
    base = 100 * base / base.sum()
    txt = "빈 네모에서 나간 이동자(추정)의 도착지\n" + "\n".join(
        f"  {k}: {s[k]:.0f}%  (도시 전체 {base[k]:.0f}%)" for k in DEST)
    ax.text(0.99, 0.01, txt, transform=ax.transAxes, ha="right", va="bottom", fontsize=9, zorder=10,
            bbox=dict(boxstyle="round", fc="white", ec="#999", alpha=0.9))
    handles = [plt.Rectangle((0, 0), 1, 1, color="#7f8c8d", alpha=0.5, label=f"{Y0}·{Y1} 모두 DID"),
               plt.Rectangle((0, 0), 1, 1, color="#e67e22", alpha=0.5, label=f"{Y1}년 새로 DID"),
               plt.Rectangle((0, 0), 1, 1, fc=to_rgba("#d32f2f", 0.12), ec="#d32f2f", lw=1.4,
                             label=f"{Y1}년에 DID에서 빠짐 (빈 네모)"),
               Line2D([], [], color="#4a148c", lw=3, label=f"빈 네모 동 → 도착 동 이동 상위 {top}")]
    ax.legend(handles=handles, loc="upper left", fontsize=9, frameon=True)
    ax.text(0.005, 0.03, ADM_NOTE, transform=ax.transAxes, fontsize=6.5, color="#777", zorder=10)
    ax.set_title(f"{name} · 빈 네모(DID에서 빠진 격자) 사람들은 어디로 갔나\n"
                 "2005·2010·2015·2020·2024년 읍면동 간 이동 합계, 출발 동의 빈 네모 인구 비율로 가중한 추정치",
                 fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT_MAP / f"lost_did_flows_{name}.png", dpi=200)
    plt.close(fig)


def plot_bars(summary):
    s = summary.groupby(["city", "origin"], sort=False)[DEST].sum()
    s = 100 * s.div(s.sum(axis=1), axis=0)
    cities = list(dict.fromkeys(summary["city"]))
    fig, ax = plt.subplots(figsize=(12, 6))
    y = 0
    ticks, labels = [], []
    for c in cities:
        for origin in ("빈 네모", "도시 전체"):
            left = 0
            for k in DEST:
                v = s.loc[(c, origin), k]
                ax.barh(y, v, left=left, color=DEST_COLORS[k], edgecolor="white",
                        hatch="" if origin == "빈 네모" else "//", alpha=1 if origin == "빈 네모" else 0.55,
                        label=k if (c == cities[0] and origin == "빈 네모") else None)
                if v >= 6:
                    ax.text(left + v / 2, y, f"{v:.0f}", ha="center", va="center", fontsize=8)
                left += v
            ticks.append(y)
            labels.append(f"{c} · {origin}")
            y += 1
        y += 0.6
    ax.set_yticks(ticks, labels)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel("도착지 구성 (%)")
    ax.legend(ncol=6, loc="upper center", bbox_to_anchor=(0.5, 1.09), fontsize=9, frameon=False)
    ax.set_title("빈 네모에서 나간 이동자(추정)와 도시 전체 이동자의 도착지 비교 (2005~2024 5개 연도 합)",
                 fontsize=12, pad=30)
    fig.text(0.5, -0.01, "자료: 통계청 국내인구이동통계 MDIS, SGIS 격자 인구. 같은 읍면동 안 이사 제외. "
             + ADM_NOTE, ha="center", fontsize=8.5, color="#555")
    fig.tight_layout()
    fig.savefig(OUT_MAP / "lost_did_destinations.png", dpi=200, bbox_inches="tight")
    plt.close(fig)


def main():
    warnings.filterwarnings("ignore", category=UserWarning)
    sigungu = da.load_sigungu()
    cities = gpd.GeoDataFrame(
        {"city": list(da.CITY_ZOOMS)},
        geometry=[shapely.union_all(sigungu[f(sigungu)].geometry.values) for f in da.CITY_ZOOMS.values()],
        crs=sigungu.crs)
    cells = da.load_cells(RES)
    pts = cell_zones(cells, cities)

    units = ma.build_units(sigungu)
    names = pd.read_csv(ma.MIG / "sgg_names.csv", dtype=str)
    cmap, _ = ma.code_map(names, units)
    sgg_unit = cmap.drop_duplicates("code", keep="last").set_index("code")["unit"]
    centers = ma.unit_centers(units)
    regions = {c: set(ma.city_region(c, units, centers)[1].index) - ma.city_region(c, units, centers)[2]
               for c in da.CITY_ZOOMS}

    centroids = pd.read_csv(ADM / "mdis_centroids.tsv", sep="\t", dtype={"ADMCD": str})
    od = pd.read_csv(ma.MIG / "od_emd.csv", dtype={"o_emd": str, "d_emd": str})
    summary, pairs = trace(od, pts, centroids, sgg_unit, regions)
    summary.round(1).to_csv(OUT_TAB / "lost_did_destinations.csv", index=False, encoding="utf-8-sig")

    results = {}
    gpkg = da.OUT / "gpkg" / "did_polygons.gpkg"
    for y in (Y0, Y1):
        results[(RES, THR, y)] = gpd.read_file(gpkg, layer=f"did_{RES}_{THR}_{y}")
    dongs24 = load_dongs(VERSION[2024], sigungu.crs)
    for name in da.CITY_ZOOMS:
        plot_city(name, pairs, sigungu, results, dongs24, summary)
    plot_bars(summary)

    s = summary.groupby(["city", "origin"], sort=False)[["movers"] + DEST].sum()
    pct = 100 * s[DEST].div(s[DEST].sum(axis=1), axis=0)
    print(pd.concat([s["movers"].round(0), pct.round(1)], axis=1).to_string())


if __name__ == "__main__":
    main()
