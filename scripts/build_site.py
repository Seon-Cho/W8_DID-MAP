"""웹페이지(W8_DID-MAP/site)용 그림과 차트 데이터를 만든다.

- output/maps의 PNG를 WebP로 줄여 site/img에 복사한다 (긴 변 2000px).
- output/tables의 결과를 차트용 site/data.js로 내보낸다.
- 재현용 코드와 결과 표를 저장소의 scripts/, results/로 복사한다 (원자료는 복사하지 않는다).

실행: ~/.venvs/did-analysis/bin/python scripts/build_site.py
"""
import json
import shutil
from pathlib import Path

import pandas as pd
from PIL import Image

import did_analysis as da

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
REPO = ROOT / "W8_DID-MAP"
SITE = REPO / "site"
MAX_SIDE = 2000
Y0, Y1 = 2015, 2024
CITIES = list(da.CITY_ZOOMS)

RES, THR = da.MAIN_RES, da.MAIN_THR
IMAGES = {
    f"maps/did_change_2015_2024_{RES}_{THR}_national.png": "did_change_national",
    f"maps/did_change_2015_2024_{RES}_{THR}_capital.png": "did_change_capital",
    f"maps/compare_overlay_2024_{RES}_national.png": "compare_sgis_2024",
    f"maps/cities/cities_change_2015_2024_{RES}_{THR}.png": "cities_change",
    "maps/migration/migration_trends.png": "migration_trends",
    "maps/migration/lost_did_destinations.png": "lost_did_destinations",
}
for c in CITIES:
    IMAGES[f"maps/cities/city_{c}_{RES}.png"] = f"city_{c}"
    IMAGES[f"maps/cities/facilities_{c}_{RES}.png"] = f"facilities_{c}"
    IMAGES[f"maps/migration/flows_{c}.png"] = f"flows_{c}"
    IMAGES[f"maps/migration/lost_did_flows_{c}.png"] = f"lost_{c}"


def images():
    dest = SITE / "img"
    dest.mkdir(parents=True, exist_ok=True)
    for src, name in IMAGES.items():
        im = Image.open(OUT / src).convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        im.save(dest / f"{name}.webp", "WEBP", quality=82, method=6)


TYPE_MIN_KM2 = 5
TYPE_BAND = 5  # 면적·밀도 변화가 모두 ±5% 안이면 정체


def unit_type(da, dd):
    if abs(da) < TYPE_BAND and abs(dd) < TYPE_BAND:
        return "정체"
    if da >= 0:
        return "확산·저밀화" if dd < 0 else "성장·고밀화"
    return "축소·공동화" if dd < 0 else "가장자리 탈락"


def typology():
    """시·군(동적 지도 단위)별 2015→2024 도시·준밀집지역 면적·밀도 변화와 유형."""
    m = SITE / "map"
    series = json.loads((m / "series.json").read_text(encoding="utf-8"))
    units = json.loads((m / "units.geojson").read_text(encoding="utf-8"))
    name = {f["properties"]["cd"]: " ".join(filter(None, [f["properties"]["sd"], f["properties"]["nm"]]))
            for f in units["features"]}
    i0, i1 = series["years"].index(Y0), series["years"].index(Y1)
    rows = []
    for cd, v in series["units"].items():
        d = v.get(str(THR))
        if not d or min(d["area"][i0], d["area"][i1]) < TYPE_MIN_KM2:
            continue
        a0, a1, p0, p1 = d["area"][i0], d["area"][i1], d["pop"][i0], d["pop"][i1]
        da, dd = 100 * (a1 / a0 - 1), 100 * ((p1 / a1) / (p0 / a0) - 1)
        rows.append({"cd": cd, "name": name.get(cd, cd), "area0": a0, "area1": a1, "da": round(da, 1),
                     "dd": round(dd, 1), "dp": round(100 * (p1 / p0 - 1), 1), "type": unit_type(da, dd)})
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "tables" / f"unit_typology_{Y0}_{Y1}.csv", index=False, encoding="utf-8-sig")
    return df.to_dict("records")


def chart_data():
    t = OUT / "tables"
    summ = pd.read_csv(t / "did_summary.csv")
    s = summ[(summ["res"] == RES) & (summ["threshold"] == THR)].set_index("year")
    nat_density = 100 * (s.loc[Y1, "did_density"] / s.loc[Y0, "did_density"] - 1)
    nat_area = 100 * (s.loc[Y1, "did_area_km2"] / s.loc[Y0, "did_area_km2"] - 1)

    reg = pd.read_csv(t / "did_density_by_region.csv")
    reg = reg[reg["threshold"] == THR]
    series = {r: g.sort_values("year")[["year", "did_area_km2", "did_pop", "did_density"]].to_dict("list")
              for r, g in reg.groupby("region", sort=False)}

    dens = pd.read_csv(t / f"sigungu_density_change_{Y0}_{Y1}.csv").head(10)
    area = pd.read_csv(t / f"sigungu_area_change_{Y0}_{Y1}.csv")
    # 비수도권 면적 증가율 순위 (2015년에 DID가 없던 곳은 증가율을 낼 수 없어 빠진다)
    area = area[~area["sido"].isin(["서울", "경기", "인천"])].dropna(subset=["area_chg_pct"]) \
        .sort_values("area_chg_pct", ascending=False).head(10)

    # 인구감소지역(92곳)과 확대 분석 대상 표시
    decline = pd.read_csv(ROOT / "data" / "reference" / "population_decline_92.csv")
    decline = set(zip(decline["sido"], decline["name"]))
    sgg = da.load_sigungu()
    sgg["sido"] = sgg["sido_cd"].map(da.SIDO)
    focus = {(r.sido, r.SIGUNGU_NM): c for c, f in da.CITY_ZOOMS.items() for r in sgg[f(sgg)].itertuples()}
    for tbl in (dens, area):
        keys = list(zip(tbl["sido"], tbl["SIGUNGU_NM"]))
        tbl["decline92"] = [k in decline for k in keys]
        tbl["focus"] = [focus.get(k, "") for k in keys]

    # 확대 분석 지역별 면적·밀도와 시설 입지
    region = pd.read_csv(t / "did_by_sigungu.csv", dtype={"SIGUNGU_CD": str})
    cities = []
    for c, f in da.CITY_ZOOMS.items():
        (a0, d0), (a1, d1) = da.city_stats(region, sgg[f(sgg)], Y0, Y1).values()
        cities.append({"city": c, "name": " ".join(sorted({n for (_, n), cc in focus.items() if cc == c})),
                       "area0": round(a0, 1), "area1": round(a1, 1), "dens0": round(d0), "dens1": round(d1)})
    fac = pd.read_csv(t / "facilities_in_did.csv")
    zone_cols = [c for c in fac.columns if c not in ("city", "layer", "group", "total")]
    layer_nm = {"schools": "학교", "health": "보건기관", "townhall": "청사", "station": "철도역",
                "urban_rail_station": "도시철도역", "terminal": "버스터미널", "ic": "고속도로 IC"}
    fac_rows = {c: [[layer_nm.get(r.layer, r.layer) + ("" if r.group == "전체" else f" ({r.group})"), int(r.total)]
                    + [int(r[i + 4]) for i in range(len(zone_cols))]
                    for r in g.itertuples(index=False)] for c, g in fac.groupby("city", sort=False)}

    mig = pd.read_csv(t / "migration_summary.csv")
    lost = pd.read_csv(t / "lost_did_destinations.csv")
    dest = ["유지 도시화지역", "신규 도시화지역", "빈 네모", "비도시화지역", "도시권 다른 시군구", "도시권 밖"]
    lost = lost.groupby(["city", "origin"], sort=False)[dest].sum()
    lost = (100 * lost.div(lost.sum(axis=1), axis=0)).round(1).reset_index()

    data = {
        "summary": s.reset_index()[["year", "n_did", "did_area_km2", "did_pop", "did_density", "pop_share_pct"]]
        .to_dict("list"),
        "period": [Y0, Y1],
        "national": {"density_chg_pct": round(nat_density, 1), "area_chg_pct": round(nat_area, 1)},
        "regions": series,
        "top_density": dens[["sido", "SIGUNGU_NM", f"density_{Y0}", f"density_{Y1}", "density_chg_pct",
                             "area_chg_pct", "pop_chg_pct", "decline92", "focus"]].to_dict("records"),
        "top_area": area[["sido", "SIGUNGU_NM", f"area_{Y0}", f"area_{Y1}", "area_chg_km2", "area_chg_pct",
                          "density_chg_pct", "decline92", "focus"]].to_dict("records"),
        "cities": cities,
        "typology": typology(),
        "fac_cols": zone_cols,
        "fac": fac_rows,
        "migration": mig[["city", "year", "core_net_out_ring_per1000", "outward_share"]].round(1)
        .to_dict("records"),
        "lost": lost.to_dict("records"),
    }
    (SITE / "data.js").write_text("window.DID = " + json.dumps(data, ensure_ascii=False) + ";\n", encoding="utf-8")


def repo_files():
    (REPO / "scripts").mkdir(parents=True, exist_ok=True)
    for f in ["did_analysis.py", "fetch_facilities.py", "migration_od.py", "migration_analysis.py",
              "lost_did_flows.py", "export_webmap.py", "build_site.py"]:
        shutil.copy2(ROOT / "scripts" / f, REPO / "scripts" / f)
    res = REPO / "results"
    (res / "tables").mkdir(parents=True, exist_ok=True)
    for f in (OUT / "tables").glob("*.csv"):
        shutil.copy2(f, res / "tables" / f.name)
    shutil.copy2(OUT / "gpkg" / "did_polygons.gpkg", res / "did_polygons.gpkg")


if __name__ == "__main__":
    images()
    chart_data()
    repo_files()
    size = sum(p.stat().st_size for p in SITE.rglob("*") if p.is_file())
    print(f"site: {len(list((SITE / 'img').glob('*.webp')))}개 그림, {size / 1e6:.1f} MB")
