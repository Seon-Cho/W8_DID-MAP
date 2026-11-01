"""웹 동적 지도(Leaflet + CARTO 배경)용 자료를 만든다. 결과는 W8_DID-MAP/site/map/.

- did_{기준}_{연도}.geojson : 연도·밀도 기준별 DID 폴리곤 (500m 격자)
- change_{기준}.geojson     : 2015→2024 유지·신규·빈 네모 구역
- sigungu.geojson           : 2025 시군구 경계 (클릭·확대용)
- series.json               : 전국과 시군구별 연도·기준별 DID 면적·인구 (그래프용)

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


def polygons(geom):
    """교차·차집합 결과에서 면만 남긴다 (선·점 잔여물 제거)."""
    return shapely.MultiPolygon([g for g in shapely.get_parts(geom) if g.geom_type == "Polygon"])


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
    size += write_geojson(sgg.assign(geometry=sgg.geometry.simplify(SGG_SIMPLIFY_M)), "sigungu.geojson", {"cd": "SIGUNGU_CD", "nm": "SIGUNGU_NM", "sd": "sido"})

    # 시군구별 시계열: DID 격자를 중심점으로 시군구에 배정 (did_analysis.region_table과 같은 방식)
    cells = da.load_cells(RES)
    summ = pd.read_csv(ROOT / "output" / "tables" / "did_summary.csv")
    summ = summ[summ["res"] == RES]
    series = {"years": da.YEARS, "national": {}, "sgg": {}}
    for thr in da.THRESHOLDS:
        s = summ[summ["threshold"] == thr].set_index("year").reindex(da.YEARS)
        series["national"][thr] = {"area": s["did_area_km2"].round(2).tolist(), "pop": s["did_pop"].tolist()}
        did_cells = {y: da.extract_did(cells, y, thr)[1] for y in da.YEARS}
        reg = da.region_table(did_cells, sgg)
        for cd, g in reg.groupby("SIGUNGU_CD"):
            g = g.set_index("year")[["did_area_km2", "did_pop"]].reindex(da.YEARS).fillna(0)
            series["sgg"].setdefault(cd, {})[thr] = {"area": g["did_area_km2"].round(2).tolist(),
                                                     "pop": g["did_pop"].astype(int).tolist()}
    path = OUT / "series.json"
    path.write_text(json.dumps(series, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size += path.stat().st_size
    print(f"map/: {len(list(OUT.iterdir()))}개 파일, {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
