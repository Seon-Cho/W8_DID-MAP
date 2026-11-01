"""웹 동적 지도(Leaflet + CARTO 배경)용 자료를 만든다. 결과는 W8_DID-MAP/site/map/.

- did_{기준}_{연도}.geojson : 연도·밀도 기준별 DID 폴리곤 (500m 격자)
- change_{기준}.geojson     : 2015→2024 유지·신규·빈 네모 구역
- units.geojson             : 시·군 경계 (특별시·광역시·특별자치시는 하나로, 일반구는 시로 합침. 클릭·확대용)
- series.json               : 전국과 시·군별 연도·기준별 DID 면적·인구 (그래프용)

실행: ~/.venvs/did-analysis/bin/python scripts/export_webmap.py
"""
import json
import warnings
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

import did_analysis as da

ROOT = Path(__file__).resolve().parents[1]
GPKG = ROOT / "output" / "gpkg" / "did_polygons.gpkg"
OUT = ROOT / "W8_DID-MAP" / "site" / "map"
RES = da.MAIN_RES
Y0, Y1 = da.CHANGE_START, da.YEARS[-1]
SGG_SIMPLIFY_M = 100
GAP_M = 200
METRO = {"11": "서울특별시", "21": "부산광역시", "22": "대구광역시", "23": "인천광역시", "24": "광주광역시",
         "25": "대전광역시", "26": "울산광역시", "29": "세종특별자치시"}


def add_units(sgg):
    """시군구 → 지도 단위. 특별·광역시는 시도 전체, 도는 시·군 (수원시 장안구 → 수원시)."""
    metro = sgg["sido_cd"].isin(METRO)
    city = sgg["SIGUNGU_NM"].str.split(" ").str[0]
    sgg["unit_cd"] = sgg["sido_cd"].where(metro, sgg["sido_cd"] + "_" + city)
    sgg["unit_nm"] = sgg["sido_cd"].map(METRO).where(metro, city)
    sgg["unit_sd"] = sgg["sido"].where(~metro, "")
    return sgg


def polygons(geom):
    """교차·차집합 결과에서 면만 남긴다 (선·점 잔여물 제거)."""
    return shapely.MultiPolygon([g for g in shapely.get_parts(geom) if g.geom_type == "Polygon"])


def drop_holes(geom, min_km2=2):
    """합칠 때 생긴 작은 구멍을 지운다."""
    keep = lambda p: shapely.Polygon(p.exterior, [r for r in p.interiors if shapely.Polygon(r).area >= min_km2 * 1e6])
    return shapely.MultiPolygon([keep(p) for p in shapely.get_parts(geom)])


def round_coords(c, nd=5):
    return round(c, nd) if isinstance(c, float) else [round_coords(x, nd) for x in c]


def write_geojson(gdf, name, props):
    gdf = gdf[~gdf.geometry.is_empty].to_crs(4326)
    feats = []
    for r in gdf.itertuples():
        geom = json.loads(shapely.to_geojson(r.geometry))
        geom["coordinates"] = round_coords(geom["coordinates"])
        feats.append({"type": "Feature", "geometry": geom,
                      "properties": {k: getattr(r, c) for k, c in props.items()}})
    path = OUT / name
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False,
                               separators=(",", ":")), encoding="utf-8")
    return path.stat().st_size


def main():
    warnings.filterwarnings("ignore", category=UserWarning)
    OUT.mkdir(parents=True, exist_ok=True)
    size = 0

    results = {}
    for thr in da.THRESHOLDS:
        for year in da.YEARS:
            did = gpd.read_file(GPKG, layer=f"did_{RES}_{thr}_{year}")
            did["density"] = (did["pop"] / did["area_km2"]).round(0).astype(int)
            did["pop"] = did["pop"].astype(int)
            results[(RES, thr, year)] = did
            size += write_geojson(did, f"did_{thr}_{year}.geojson",
                                  {"pop": "pop", "a": "area_km2", "d": "density"})
        layers = da.change_layers(results, RES, thr, Y0, Y1)
        zones = gpd.GeoDataFrame({"z": ["kept", "new", "lost"]}, geometry=[polygons(l[0]) for l in layers],
                                 crs=results[(RES, thr, Y0)].crs)
        zones = zones.explode(index_parts=False)
        zones["geometry"] = shapely.set_precision(zones.geometry.values, 1)  # 1m 격자에 맞춰 잔조각 정리
        zones["a"] = (zones.area / 1e6).round(2)
        size += write_geojson(zones[zones["a"] >= 0.01], f"change_{thr}.geojson", {"z": "z", "a": "a"})

    sgg = da.load_sigungu()
    sgg["sido"] = sgg["sido_cd"].map(da.SIDO)
    sgg = add_units(sgg)
    units = sgg.dissolve("unit_cd", aggfunc="first").reset_index()
    # 시군구 경계는 이미 따로 단순화돼 있어 합친 경계 안에 가는 틈이 남는다. 넓혔다 줄여 메운다.
    units["geometry"] = units.geometry.buffer(GAP_M).buffer(-GAP_M).map(drop_holes).simplify(SGG_SIMPLIFY_M)
    size += write_geojson(units, "units.geojson", {"cd": "unit_cd", "nm": "unit_nm", "sd": "unit_sd"})

    # 시·군별 시계열: DID 격자를 중심점으로 시군구에 배정한 뒤(did_analysis.region_table과 같은 방식) 단위로 합친다
    cells = da.load_cells(RES)
    summ = pd.read_csv(ROOT / "output" / "tables" / "did_summary.csv")
    summ = summ[summ["res"] == RES]
    series = {"years": da.YEARS, "national": {}, "units": {}}
    unit_of = sgg.set_index("SIGUNGU_CD")["unit_cd"]
    for thr in da.THRESHOLDS:
        s = summ[summ["threshold"] == thr].set_index("year").reindex(da.YEARS)
        series["national"][thr] = {"area": s["did_area_km2"].round(2).tolist(), "pop": s["did_pop"].tolist()}
        did_cells = {y: da.extract_did(cells, y, thr)[1] for y in da.YEARS}
        reg = da.region_table(did_cells, sgg)
        reg["unit_cd"] = reg["SIGUNGU_CD"].map(unit_of)
        reg = reg.groupby(["unit_cd", "year"])[["did_area_km2", "did_pop"]].sum().reset_index()
        for cd, g in reg.groupby("unit_cd"):
            g = g.set_index("year")[["did_area_km2", "did_pop"]].reindex(da.YEARS).fillna(0)
            series["units"].setdefault(cd, {})[thr] = {"area": g["did_area_km2"].round(2).tolist(),
                                                     "pop": g["did_pop"].astype(int).tolist()}
    path = OUT / "series.json"
    path.write_text(json.dumps(series, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size += path.stat().st_size
    print(f"map/: {len(list(OUT.iterdir()))}개 파일, {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
