"""중력형 접근성(Hansen)으로 확대 분석 지역의 1km 격자별 공공·교통시설 접근성을 구한다.

  S_k(i) = Σ_j exp(−β · t_ij),   β = ln2 / t½       (시설 유형 k별, t_ij = 도로 이동시간[분])
  A(i)   = (1/K) Σ_k S_k(i) / max S_k                 (유형별로 0~1로 맞춘 뒤 같은 비중으로 평균)

- t½(반감 시간)은 10분. 국토교통부(2018) 기초생활인프라 국가적 최저기준에서 지역거점시설
  (공공도서관 등)을 차량 10분 안에 닿도록 한 기준을 따랐다. 5분·20분으로 바꿔 민감도를 본다.
- 이동시간 = 격자 중심 → 가까운 도로(20km/h) + 도로망 최단시간 + 도로 → 시설(20km/h).
  도로망은 fetch_roads.py가 OSM에서 만든 것. 시설은 fetch_facilities.py (지역 경계 + 15km 안).
- 대상 격자: 지역 경계 안에서 2015 또는 2024년 인구가 있는 1km 격자.
- 구역: 2015·2024 도시(1,500명/km²) 격자로 유지·신규·빈 네모·도시 밖을 나눈다.

출력: output/tables/accessibility_cells.csv, accessibility_by_zone.csv, accessibility_sensitivity.csv
      output/maps/access/access_{지역}.png, access_cities.png

실행: ~/.venvs/did-analysis/bin/python scripts/accessibility.py
"""
import math

import geopandas as gpd
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
import shapely

import did_analysis as da

ROADS = da.DATA / "roads"
OUT_MAP = da.OUT / "maps" / "access"
RES, THR = da.MAIN_RES, da.MAIN_THR
Y0, Y1 = da.CHANGE_START, da.YEARS[-1]
HALF_MIN = 10
SENS_HALF_MIN = [5, 10, 20]
CONNECT_KMH = 20
CUTOFF_MIN = 120
# 시설 유형 → facilities.gpkg 레이어
TYPES = {
    "대학교": ["university"],
    "보건소": ["health"],
    "시·군·구청": ["townhall"],
    "철도역": ["station", "urban_rail_station"],
    "버스터미널": ["terminal"],
    "고속도로 IC": ["ic"],
}
CMAP = "YlGnBu"  # 구역 색(회색·주황·빨강)과 겹치지 않는 노랑→청록→남색, 진할수록 접근성 높음
ZONES = ["유지", "신규", "빈 네모", "도시 밖"]
ZONE_EDGE = {"유지": "#7f8c8d", "신규": "#e67e22", "빈 네모": "#d32f2f"}  # 도시 변화 지도와 같은 색


def load_graph(city):
    g = nx.read_graphml(ROADS / f"{city}.graphml")
    for _, d in g.nodes(data=True):
        d["x"], d["y"] = float(d["x"]), float(d["y"])
    for _, _, d in g.edges(data=True):
        d["minutes"] = float(d["minutes"])
    return g


def snapper(g):
    ids = list(g.nodes)
    pts = shapely.points([g.nodes[n]["x"] for n in ids], [g.nodes[n]["y"] for n in ids])
    tree = shapely.STRtree(pts)

    def snap(geoms):
        idx = tree.query_nearest(geoms, return_distance=True, all_matches=False)
        (gi, ni), dist = idx
        node = np.empty(len(geoms), dtype=object)
        gap = np.empty(len(geoms))
        node[gi], gap[gi] = [ids[k] for k in ni], dist
        return node, gap / 1000 / CONNECT_KMH * 60  # 분

    return snap


def travel_minutes(g, snap, cells, fac):
    """격자 × 시설 이동시간 행렬(분). 닿지 않으면 inf."""
    c_node, c_gap = snap(cells.geometry.centroid.values)
    f_node, f_gap = snap(fac.geometry.values)
    t = np.full((len(cells), len(fac)), np.inf)
    for j, (src, gap) in enumerate(zip(f_node, f_gap)):
        dist = nx.single_source_dijkstra_path_length(g, src, cutoff=CUTOFF_MIN, weight="minutes")
        t[:, j] = [dist.get(n, np.inf) for n in c_node]
        t[:, j] += c_gap + gap
    return t


def gravity(t, half_min):
    return np.exp(-math.log(2) / half_min * t).sum(axis=1)


def city_cells(cells, sgg, name, did_cells):
    city = sgg[da.CITY_ZOOMS[name](sgg)]
    geom = shapely.union_all(city.geometry.values)
    c = cells[cells.geometry.centroid.within(geom) & ((cells[Y0] > 0) | (cells[Y1] > 0))].copy()
    in0 = c["GRID_CD"].isin(did_cells[Y0]["GRID_CD"])
    in1 = c["GRID_CD"].isin(did_cells[Y1]["GRID_CD"])
    c["zone"] = np.select([in0 & in1, in1, in0], ["유지", "신규", "빈 네모"], "도시 밖")
    c["city"] = name
    return c, geom


def plot_city(ax, c, geom, name, crs):
    """도시 격자(유지·신규·빈 네모)만 잘라 접근성(중요도)을 색과 숫자로 보인다."""
    u = c[c["zone"] != "도시 밖"]
    x0, y0, x1, y1 = u.total_bounds
    cx, cy, half = (x0 + x1) / 2, (y0 + y1) / 2, max(x1 - x0, y1 - y0) / 2 + 1500
    bbox = (cx - half, cy - half, cx + half, cy + half)
    da.draw_basemap(ax, crs, bbox)
    u.plot(ax=ax, column="A_local", cmap=CMAP, vmin=0, vmax=1, alpha=0.9, edgecolor="white", linewidth=0.6, zorder=3)
    for z, col in ZONE_EDGE.items():
        sel = u[u["zone"] == z]
        if len(sel):
            gpd.GeoSeries([shapely.coverage_union_all(sel.geometry.values)], crs=crs).boundary.plot(
                ax=ax, color=col, linewidth=2.4, zorder=5)
    fs = 9 if len(u) <= 20 else 6.5
    for r in u.itertuples():
        p = r.geometry.centroid
        ax.text(p.x, p.y, f"{r.A:.2f}", ha="center", va="center", fontsize=fs, zorder=6,
                color="white" if r.A_local > 0.6 else "#222")
    gpd.GeoSeries([geom], crs=crs).boundary.plot(ax=ax, color="#555", linewidth=0.9, linestyle="--", zorder=4)
    ax.set_xlim(bbox[0], bbox[2])
    ax.set_ylim(bbox[1], bbox[3])
    ax.set_axis_off()
    ax.set_title(f"{name} · 도시 격자 {len(u)}개", fontsize=13)


def legend(fig):
    from matplotlib.lines import Line2D
    sm = plt.cm.ScalarMappable(cmap=CMAP, norm=plt.Normalize(0, 1))
    cb = fig.colorbar(sm, ax=fig.axes, shrink=0.5, pad=0.02)
    cb.set_label(f"지역 안 상대 중요도 (도시 격자 중 최저 0 ~ 최고 1)\n칸 안 숫자: 네 지역 공통 접근성 지수 A, 반감 {HALF_MIN}분")
    hs = [Line2D([], [], color=c, linewidth=2, label=f"{z} 도시 격자") for z, c in ZONE_EDGE.items()]
    hs.append(Line2D([], [], color="#555", linestyle="--", linewidth=1, label="지역 경계"))
    fig.legend(handles=hs, loc="lower center", ncol=4, frameon=False, fontsize=10)


def main():
    OUT_MAP.mkdir(parents=True, exist_ok=True)
    cells = da.load_cells(RES)
    sgg = da.load_sigungu()
    did_cells = {y: da.extract_did(cells, y, THR)[1] for y in (Y0, Y1)}
    fac = da.load_facilities()

    rows, geoms, raw = [], {}, {}
    for name in da.CITY_ZOOMS:
        c, geom = city_cells(cells, sgg, name, did_cells)
        g = load_graph(name)
        snap = snapper(g)
        for k, layers in TYPES.items():
            f = pd.concat([fac[l][fac[l]["city"] == name] for l in layers if l in fac], ignore_index=True)
            t = travel_minutes(g, snap, c, f) if len(f) else np.full((len(c), 0), np.inf)
            raw[(name, k)] = t
            c[f"near_{k}"] = t.min(axis=1) if t.shape[1] else np.inf
        rows.append(c)
        geoms[name] = geom
        print(f"{name}: 격자 {len(c)}개")
    allc = pd.concat(rows)

    def index(half):
        s = pd.DataFrame(index=allc.index)
        parts = []
        for name in da.CITY_ZOOMS:
            m = allc["city"] == name
            parts.append(pd.DataFrame({k: gravity(raw[(name, k)], half) for k in TYPES}, index=allc.index[m]))
        s = pd.concat(parts).loc[allc.index]
        return (s / s.max()).mean(axis=1), s

    allc = allc.reset_index(drop=True)
    sens = {}
    for h in SENS_HALF_MIN:
        a, s = index(h)
        sens[h] = a.values
        if h == HALF_MIN:
            for k in TYPES:
                allc[f"S_{k}"] = (s[k] / s[k].max()).values
    allc["A"] = sens[HALF_MIN]
    # 지도 색은 지역 도시 격자 안 상대값(최저 0, 최고 1), 숫자는 네 지역 공통 지수 A. 지역 간 비교는 A(네 지역 공통 기준)로 표에서 본다.
    urb = allc[allc["zone"] != "도시 밖"].groupby("city")["A"].agg(["min", "max"])
    lo, hi = allc["city"].map(urb["min"]), allc["city"].map(urb["max"])
    allc["A_local"] = ((allc["A"] - lo) / (hi - lo)).clip(0, 1)
    allc["pop_chg"] = allc[Y1] - allc[Y0]

    out = allc[["city", "GRID_CD", "zone", Y0, Y1, "pop_chg", "A", "A_local"] + [f"S_{k}" for k in TYPES]
               + [f"near_{k}" for k in TYPES]].rename(columns={Y0: f"pop_{Y0}", Y1: f"pop_{Y1}"})
    out.round(3).to_csv(da.OUT / "tables" / "accessibility_cells.csv", index=False, encoding="utf-8-sig")

    def zone_row(g):
        w = g[Y1].clip(lower=0)
        return pd.Series({"cells": len(g), f"pop_{Y1}": int(g[Y1].sum()), "A_mean": g["A"].mean(),
                          "A_popweighted": (g["A"] * w).sum() / w.sum() if w.sum() else np.nan,
                          "near_hall_min": g["near_시·군·구청"].median()})
    by_zone = allc.groupby(["city", "zone"]).apply(zone_row, include_groups=False).reset_index()
    by_zone["zone"] = pd.Categorical(by_zone["zone"], ZONES, ordered=True)
    by_zone = by_zone.sort_values(["city", "zone"])
    by_zone.round(3).to_csv(da.OUT / "tables" / "accessibility_by_zone.csv", index=False, encoding="utf-8-sig")
    print(by_zone.round(2).to_string(index=False))

    base = pd.Series(sens[HALF_MIN])
    top = base >= base.quantile(0.8)
    sens_rows = []
    for h in SENS_HALF_MIN:
        a = pd.Series(sens[h])
        sens_rows.append({"half_min": h, "spearman_vs_10": round(base.rank().corr(a.rank()), 3),
                          "top20_overlap_pct": round(100 * (top & (a >= a.quantile(0.8))).sum() / top.sum(), 1)})
    sens_df = pd.DataFrame(sens_rows)
    sens_df.to_csv(da.OUT / "tables" / "accessibility_sensitivity.csv", index=False, encoding="utf-8-sig")
    print(sens_df.to_string(index=False))

    gdf = gpd.GeoDataFrame(allc, geometry="geometry", crs=cells.crs)
    for name in da.CITY_ZOOMS:
        fig, ax = plt.subplots(figsize=(10, 10))
        plot_city(ax, gdf[gdf["city"] == name], geoms[name], name, cells.crs)
        legend(fig)
        fig.savefig(OUT_MAP / f"access_{name}.png", dpi=170, bbox_inches="tight")
        plt.close(fig)
    n = len(da.CITY_ZOOMS)
    fig, axes = plt.subplots(-(-n // 2), 2, figsize=(16, 7.6 * -(-n // 2)))
    for ax, name in zip(axes.flat, da.CITY_ZOOMS):
        plot_city(ax, gdf[gdf["city"] == name], geoms[name], name, cells.crs)
    for ax in list(axes.flat)[n:]:
        ax.set_axis_off()
    fig.suptitle(f"도시 격자(1,500명/km²)의 시설 접근성 (대학교·보건소·청사·철도역·버스터미널·IC, 도로 이동시간, 반감 {HALF_MIN}분)",
                 fontsize=14)
    legend(fig)
    fig.savefig(OUT_MAP / "access_cities.png", dpi=170, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    main()
