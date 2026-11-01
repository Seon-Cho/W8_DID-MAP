"""국내인구이동 OD(migration_od.py 결과)로 6개 도시의 거주 이동 방향을 읽는다.

1. MDIS 행정기관코드(연도별) → 2025 SGIS 시군구 코드로 맞춘다. 창원은 2010 통합 전후를 맞추기 위해 한 단위로 묶는다.
2. 도시마다 시청에서 반경 R km 안(인구가중 중심 기준) 시군구를 '도시권'으로 잡고,
   a. 중심 도시 → 주변 시군구 순유출 (명, 중심 도시 인구 1천 명당)
   b. 바깥쪽 이동 비율: 도시권 안 시군구 간 이동 중 도착지가 출발지보다 시청에서 1km 이상 먼 이동의 비율
   c. 중심 도시 → 주변 이동의 사유(2015~)·연령 구성
3. 흐름 지도: 시군구별 순이동률(색)과 순이동 상위 흐름(화살표), 첫해와 2024년 비교.

거리는 2020년 500M 격자 인구로 구한 시군구 인구가중 중심점 사이 직선거리다.
"""
import re
import sys
import warnings
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shapely
from matplotlib.colors import TwoSlopeNorm
from matplotlib.patches import FancyArrowPatch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import did_analysis as da  # noqa: E402

MIG = da.DATA / "migration"
OUT_MAP = da.OUT / "maps" / "migration"
OUT_TAB = da.OUT / "tables"

# MDIS 행정 시도코드 → SGIS 시도코드
SIDO_ADMIN_TO_SGIS = {
    "11": "11", "26": "21", "27": "22", "28": "23", "29": "24", "30": "25", "31": "26", "36": "29",
    "41": "31", "42": "32", "51": "32", "43": "33", "44": "34", "45": "35", "52": "35", "46": "36",
    "47": "37", "48": "38", "50": "39",
}
SIDO_PREFIX = re.compile(r"^(서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시|세종특별자치시|"
                         r"경기도|강원도|강원특별자치도|충청북도|충청남도|전라북도|전북특별자치도|전라남도|경상북도|경상남도|"
                         r"제주특별자치도|제주도)")
CHANGWON = "38110"  # 창원시(통합) 분석 단위
CHANGWON_OLD = re.compile(r"^(창원시|마산시|진해시|합포구|회원구)")  # 합포구·회원구: 2000년 마산시의 구
SPECIAL = {"청원군": "청주시청원구", "연기군": "세종시", "세종특별자치시": "세종시", "북제주군": "제주시",
           "남제주군": "서귀포시", "남구": "미추홀구", "증평(출)": "증평군"}  # 남구: 인천만 해당(아래에서 시도 확인)
BRANCH = re.compile(r"출장|\(출\)$|출$")

# 도시: (중심 도시 조건은 da.CITY_ZOOMS, 시청 위치 경위도, 도시권 반경 km)
CITIES = {
    "부산": ((129.0750, 35.1798), 40),
    "대구": ((128.6014, 35.8714), 40),
    "광주": ((126.8514, 35.1601), 35),
    "창원": ((128.6811, 35.2280), 30),
    "목포": ((126.3922, 34.8118), 30),
    "남원": ((127.3904, 35.4164), 35),
}
REASONS = {"1": "직업", "2": "가족", "3": "주택", "4": "교육", "5": "주거환경", "6": "자연환경", "9": "기타"}
OUTWARD_KM = 1.0


def norm(name):
    return SIDO_PREFIX.sub("", re.sub(r"\s+", "", str(name)))


def build_units(sigungu):
    """분석 단위: 2025 시군구, 단 창원 5개 구는 하나로."""
    s = sigungu.copy()
    s["unit"] = np.where(s["SIGUNGU_NM"].str.startswith("창원시"), CHANGWON, s["SIGUNGU_CD"])
    s["unit_nm"] = np.where(s["unit"] == CHANGWON, "창원시", s["SIGUNGU_NM"])
    units = s.dissolve(by=["unit", "unit_nm", "sido_cd"], as_index=False)[["unit", "unit_nm", "sido_cd", "geometry"]]
    return units


def code_map(names, units):
    """(year, MDIS 시군구코드) → unit. 이름으로 맞추고, 못 맞춘 코드를 돌려준다."""
    sg = units.assign(key=units["unit_nm"].str.replace(" ", "", regex=False))
    rows, miss = [], []
    for (year, code), name in names.set_index(["year", "code"])["name"].items():
        n = norm(name)
        sido = SIDO_ADMIN_TO_SGIS.get(code[:2])
        cand = sg[sg["sido_cd"] == sido]
        unit = None
        if sido == "38" and CHANGWON_OLD.match(n):
            unit = CHANGWON
        elif BRANCH.search(n) and n not in SPECIAL:
            unit = "parent"
        else:
            if n in SPECIAL and (n != "남구" or sido == "23"):
                n = SPECIAL[n]
            for test in (lambda k: k == n,
                         lambda k: k.endswith(n),
                         lambda k: k[:-1] == n[:-1] and k[-1] in "시군" and n[-1] in "시군"):
                hit = cand[cand["key"].map(test)]
                if len(hit) == 1:
                    unit = hit["unit"].iloc[0]
                    break
            if unit is None:  # 군위군처럼 시도가 바뀐 곳: 전국에서 하나뿐이면 그것
                hit = sg[sg["key"] == n]
                if len(hit) == 1:
                    unit = hit["unit"].iloc[0]
        rows.append((int(year), code, unit))
        if unit is None:
            miss.append((year, code, name))
    m = pd.DataFrame(rows, columns=["year", "code", "unit"])
    # 출장소 → 같은 해 바로 앞 코드의 본청
    for i in m.index[m["unit"] == "parent"]:
        y, c = m.at[i, "year"], m.at[i, "code"]
        prev = m[(m["year"] == y) & (m["code"] < c) & (m["code"].str[:2] == c[:2]) & ~m["unit"].isin(["parent", None])]
        m.at[i, "unit"] = prev.sort_values("code")["unit"].iloc[-1] if len(prev) else None
    return m.dropna(subset=["unit"]), pd.DataFrame(miss, columns=["year", "code", "name"])


def unit_centers(units):
    """2020 500M 격자 인구로 단위별 인구가중 중심점과 연도별 인구를 구한다."""
    cells = da.load_cells("500M")
    pts = gpd.GeoDataFrame(cells.drop(columns="geometry"), geometry=cells.geometry.centroid, crs=cells.crs)
    pts = gpd.sjoin(pts, units[["unit", "geometry"]], predicate="within")
    years = [y for y in da.YEARS if y in pts.columns]
    pop = pts.groupby("unit")[years].sum()
    w = pts[2020]
    cx = (pts.geometry.x * w).groupby(pts["unit"]).sum() / w.groupby(pts["unit"]).sum()
    cy = (pts.geometry.y * w).groupby(pts["unit"]).sum() / w.groupby(pts["unit"]).sum()
    return pd.DataFrame({"cx": cx, "cy": cy}).join(pop)


def load_od(cmap):
    od = pd.read_csv(MIG / "od_sgg.csv", dtype={"o_sgg": str, "d_sgg": str, "reason": str})
    key = cmap.set_index(["year", "code"])["unit"]
    od["o"] = key.reindex(list(zip(od["year"], od["o_sgg"]))).values
    od["d"] = key.reindex(list(zip(od["year"], od["d_sgg"]))).values
    lost = od[od["o"].isna() | od["d"].isna()].groupby("year")["persons"].sum()
    tot = od.groupby("year")["persons"].sum()
    print("코드 못 맞춘 이동자 비율: " + ", ".join(f"{y} {100 * lost.get(y, 0) / tot[y]:.2f}%" for y in tot.index))
    return od.dropna(subset=["o", "d"])


def city_region(name, units, centers):
    (lon, lat), radius = CITIES[name]
    cbd = gpd.GeoSeries([shapely.Point(lon, lat)], crs=4326).to_crs(units.crs).iloc[0]
    c = centers.copy()
    c["dist_km"] = np.hypot(c["cx"] - cbd.x, c["cy"] - cbd.y) / 1000
    sg = da.load_sigungu()
    core_cd = sg.loc[da.CITY_ZOOMS[name](sg), "SIGUNGU_CD"]
    core = set(np.where(core_cd.str.startswith("381"), CHANGWON, core_cd))
    region = set(c.index[c["dist_km"] <= radius]) | core
    return cbd, c.loc[sorted(region)], core


def metrics(name, od, units, centers):
    cbd, reg, core = city_region(name, units, centers)
    ring = set(reg.index) - core
    rows = []
    for year, g in od.groupby("year"):
        inter = g[(g["o"] != g["d"]) & g["o"].isin(reg.index) & g["d"].isin(reg.index)]
        dd = reg["dist_km"].reindex(inter["d"]).values - reg["dist_km"].reindex(inter["o"]).values
        moved = np.abs(dd) >= OUTWARD_KM
        out_core = g[g["o"].isin(core) & g["d"].isin(ring)]
        in_core = g[g["o"].isin(ring) & g["d"].isin(core)]
        out_all = g[g["o"].isin(core) & ~g["d"].isin(core)]
        in_all = g[~g["o"].isin(core) & g["d"].isin(core)]
        pop_year = min(da.YEARS, key=lambda y: abs(y - year))
        core_pop = centers.loc[sorted(core), pop_year].sum()
        p = inter["persons"].values
        r = dict(city=name, year=year, region_units=len(reg), ring_units=len(ring),
                 core_pop=int(core_pop),
                 core_to_ring=int(out_core["persons"].sum()), ring_to_core=int(in_core["persons"].sum()),
                 core_net_out_ring=int(out_core["persons"].sum() - in_core["persons"].sum()),
                 core_net_out_all=int(out_all["persons"].sum() - in_all["persons"].sum()),
                 inter_moves=int(p.sum()),
                 outward_share=100 * p[moved & (dd > 0)].sum() / p[moved].sum() if p[moved].sum() else np.nan,
                 mean_dkm=(p * dd).sum() / p.sum() if p.sum() else np.nan)
        r["core_net_out_ring_per1000"] = 1000 * r["core_net_out_ring"] / core_pop if core_pop else np.nan
        r["core_net_out_all_per1000"] = 1000 * r["core_net_out_all"] / core_pop if core_pop else np.nan
        for grp in ["30-44", "65+"]:
            o = out_core[out_core["age_grp"] == grp]["persons"].sum()
            i = in_core[in_core["age_grp"] == grp]["persons"].sum()
            r[f"age{grp}_share_out"] = 100 * o / out_core["persons"].sum() if len(out_core) else np.nan
            r[f"age{grp}_share_in"] = 100 * i / in_core["persons"].sum() if len(in_core) else np.nan
        if year >= 2013:
            for code, lbl in REASONS.items():
                r[f"사유_{lbl}_out"] = 100 * out_core.loc[out_core["reason"] == code, "persons"].sum() / max(
                    out_core["persons"].sum(), 1)
        rows.append(r)
    return pd.DataFrame(rows), cbd, reg, core


def net_flows(g, reg):
    """도시권 안 시군구 쌍별 순흐름 (a→b 양수만)."""
    f = g[(g["o"] != g["d"]) & g["o"].isin(reg.index) & g["d"].isin(reg.index)]
    f = f.groupby(["o", "d"])["persons"].sum()
    rev = f.copy()
    rev.index = rev.index.swaplevel().set_names(["o", "d"])  # 이름으로 정렬되므로 이름도 바꿔야 함
    net = f.sub(rev, fill_value=0)
    return net[net > 0].sort_values(ascending=False)


def plot_flows(name, od, units, centers, cbd, reg, core, years, top=15):
    OUT_MAP.mkdir(parents=True, exist_ok=True)
    u = units[units["unit"].isin(reg.index)].set_index("unit")
    x0, y0, x1, y1 = u.total_bounds
    pad = 3000
    bbox = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    fig, axes = plt.subplots(1, len(years), figsize=(7.5 * len(years) + 1.5, 7.5 * h / w + 1.2),
                             layout="constrained")
    rates = {}
    for year in years:
        g = od[od["year"] == year]
        inflow = g[g["d"].isin(reg.index) & (g["o"] != g["d"])].groupby("d")["persons"].sum()
        outflow = g[g["o"].isin(reg.index) & (g["o"] != g["d"])].groupby("o")["persons"].sum()
        pop_year = min(da.YEARS, key=lambda y: abs(y - year))
        net = inflow.sub(outflow, fill_value=0).reindex(u.index).fillna(0)
        rates[year] = 1000 * net / centers.loc[u.index, pop_year].replace(0, np.nan)
    vmax = np.nanpercentile(np.abs(pd.concat(rates.values())), 95)
    norm_ = TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)
    allnet = [net_flows(od[od["year"] == y], reg) for y in years]
    wmax = max([n.iloc[0] for n in allnet if len(n)] or [1])
    for ax, year, nf in zip(axes, years, allnet):
        ax.set_xlim(bbox[0], bbox[2]); ax.set_ylim(bbox[1], bbox[3])
        da.draw_basemap(ax, units.crs, bbox)
        uu = u.assign(rate=rates[year])
        uu.plot(ax=ax, column="rate", cmap="RdBu", norm=norm_, alpha=0.55, edgecolor="#555", linewidth=0.5)
        u.loc[sorted(core)].dissolve().boundary.plot(ax=ax, color="#111", linewidth=1.6)
        for (o, d), v in nf.head(top).items():
            a = (reg.at[o, "cx"], reg.at[o, "cy"])
            b = (reg.at[d, "cx"], reg.at[d, "cy"])
            outward = reg.at[d, "dist_km"] > reg.at[o, "dist_km"]
            ax.add_patch(FancyArrowPatch(a, b, connectionstyle="arc3,rad=0.18", arrowstyle="-|>",
                                         mutation_scale=10 + 10 * v / wmax, linewidth=0.8 + 6 * v / wmax,
                                         color="#c62828" if outward else "#1565c0", alpha=0.85, zorder=6))
        ax.scatter([cbd.x], [cbd.y], marker="*", s=220, color="#111", edgecolor="white", zorder=7)
        for unit, row in reg.iterrows():
            ax.text(row["cx"], row["cy"], u.at[unit, "unit_nm"], fontsize=6.5, ha="center",
                    va="bottom", color="#222", zorder=8,
                    bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.5))
        ax.set_xlim(bbox[0], bbox[2]); ax.set_ylim(bbox[1], bbox[3])
        ax.set_axis_off()
        ax.set_title(f"{year}년  ·  순이동 상위 {top}개 흐름", fontsize=12)
    sm = plt.cm.ScalarMappable(norm=norm_, cmap="RdBu")
    cb = fig.colorbar(sm, ax=axes, shrink=0.6, pad=0.01)
    cb.set_label("시군구 순이동률 (도시권 밖 포함, 인구 1천 명당)")
    from matplotlib.lines import Line2D
    handles = [Line2D([], [], color="#c62828", lw=3, label="시청에서 멀어지는 방향"),
               Line2D([], [], color="#1565c0", lw=3, label="시청 쪽으로 들어오는 방향"),
               Line2D([], [], marker="*", ls="", color="#111", ms=12, label="시청"),
               Line2D([], [], color="#111", lw=1.6, label="중심 도시 경계")]
    axes[0].legend(handles=handles, loc="lower left", fontsize=8.5, frameon=True)
    fig.suptitle(f"{name} 도시권 거주 이동 방향 (시군구 간 순이동, 화살표 굵기 = 순이동자 수, 시청 반경 "
                 f"{CITIES[name][1]}km)", fontsize=13)
    fig.savefig(OUT_MAP / f"flows_{name}.png", dpi=200)
    plt.close(fig)


def plot_trends(summary):
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    for name, g in summary.groupby("city", sort=False):
        g = g.sort_values("year")
        axes[0].plot(g["year"], g["core_net_out_ring_per1000"], marker="o", label=name)
        axes[1].plot(g["year"], g["outward_share"], marker="o", label=name)
    axes[0].axhline(0, color="#999", lw=0.8)
    axes[0].set_title("중심 도시 → 주변 시군구 순유출 (중심 도시 인구 1천 명당)")
    axes[1].axhline(50, color="#999", lw=0.8)
    axes[1].set_title("도시권 안 시군구 간 이동 중 시청에서 멀어지는 이동 비율 (%)")
    for ax in axes:
        ax.set_xticks(sorted(summary["year"].unique()))
        ax.grid(alpha=0.3)
    axes[1].legend(ncol=2, fontsize=9)
    fig.text(0.5, -0.02, "자료: 통계청 국내인구이동통계 MDIS (2000 서머리, 2005~ 인구관련 연간자료). "
             "2000년은 서머리 집계라 다른 해와 산출 방식이 다름", ha="center", fontsize=9, color="#555")
    fig.tight_layout()
    fig.savefig(OUT_MAP / "migration_trends.png", dpi=200, bbox_inches="tight")
    plt.close(fig)


def main():
    warnings.filterwarnings("ignore", category=UserWarning)
    OUT_MAP.mkdir(parents=True, exist_ok=True)
    units = build_units(da.load_sigungu())
    names = pd.read_csv(MIG / "sgg_names.csv", dtype=str)
    cmap, miss = code_map(names, units)
    if len(miss):
        print("이름으로 못 맞춘 시군구 코드:", "; ".join(f"{y} {c} {n}" for y, c, n in miss.values))
    od = load_od(cmap)
    centers = unit_centers(units)
    frames = []
    for name in CITIES:
        m, cbd, reg, core = metrics(name, od, units, centers)
        frames.append(m)
        years = [min(od["year"]), max(od["year"])]
        plot_flows(name, od, units, centers, cbd, reg, core, years)
        print(f"{name}: 도시권 {len(reg)}개 시군구")
    summary = pd.concat(frames)
    summary.round(2).to_csv(OUT_TAB / "migration_summary.csv", index=False, encoding="utf-8-sig")
    plot_trends(summary)
    cols = ["city", "year", "core_net_out_ring", "core_net_out_ring_per1000", "core_net_out_all_per1000",
            "outward_share", "mean_dkm"]
    print(summary[cols].round(1).to_string(index=False))


if __name__ == "__main__":
    main()
