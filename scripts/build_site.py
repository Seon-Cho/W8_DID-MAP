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

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
REPO = ROOT / "W8_DID-MAP"
SITE = REPO / "site"
MAX_SIDE = 2000
CITIES = ["부산", "대구", "광주", "창원", "목포", "남원"]

IMAGES = {
    "maps/did_500M_2000_national.png": "did_national_facets",
    "maps/did_change_2000_2024_500M_2000_national.png": "did_change_national",
    "maps/did_change_2000_2024_500M_2000_capital.png": "did_change_capital",
    "maps/compare_overlay_2024_500M_national.png": "compare_sgis_2024",
    "maps/cities/cities_change_2000_2024_500M_2000.png": "cities_change",
    "maps/migration/migration_trends.png": "migration_trends",
    "maps/migration/lost_did_destinations.png": "lost_did_destinations",
}
for c in CITIES:
    IMAGES[f"maps/cities/city_{c}_500M.png"] = f"city_{c}"
    IMAGES[f"maps/cities/facilities_{c}_500M.png"] = f"facilities_{c}"
    IMAGES[f"maps/migration/flows_{c}.png"] = f"flows_{c}"
    IMAGES[f"maps/migration/lost_did_flows_{c}.png"] = f"lost_{c}"


def images():
    dest = SITE / "img"
    dest.mkdir(parents=True, exist_ok=True)
    for src, name in IMAGES.items():
        im = Image.open(OUT / src).convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        im.save(dest / f"{name}.webp", "WEBP", quality=82, method=6)


def chart_data():
    t = OUT / "tables"
    summ = pd.read_csv(t / "did_summary.csv")
    s = summ[(summ["res"] == "500M") & (summ["threshold"] == 2000)].set_index("year")
    nat_density = 100 * (s["did_density"].iloc[-1] / s["did_density"].iloc[0] - 1)
    nat_area = 100 * (s["did_area_km2"].iloc[-1] / s["did_area_km2"].iloc[0] - 1)

    reg = pd.read_csv(t / "did_density_by_region.csv")
    reg = reg[reg["threshold"] == 2000]
    series = {r: g.sort_values("year")[["year", "did_area_km2", "did_pop", "did_density"]].to_dict("list")
              for r, g in reg.groupby("region", sort=False)}

    dens = pd.read_csv(t / "sigungu_density_change_2000_2024.csv").head(10)
    area = pd.read_csv(t / "sigungu_area_change_2000_2024.csv")
    area = area[~area["sido"].isin(["서울", "경기"])].head(10)

    mig = pd.read_csv(t / "migration_summary.csv")
    lost = pd.read_csv(t / "lost_did_destinations.csv")
    dest = ["유지 DID", "신규 DID", "빈 네모", "비DID", "도시권 다른 시군구", "도시권 밖"]
    lost = lost.groupby(["city", "origin"], sort=False)[dest].sum()
    lost = (100 * lost.div(lost.sum(axis=1), axis=0)).round(1).reset_index()

    data = {
        "summary": s.reset_index()[["year", "n_did", "did_area_km2", "did_pop", "did_density", "pop_share_pct"]]
        .to_dict("list"),
        "national": {"density_chg_pct": round(nat_density, 1), "area_chg_pct": round(nat_area, 1)},
        "regions": series,
        "top_density": dens[["sido", "SIGUNGU_NM", "density_2000", "density_2024", "density_chg_pct",
                             "area_chg_pct", "pop_chg_pct"]].to_dict("records"),
        "top_area": area[["sido", "SIGUNGU_NM", "area_2000", "area_2024", "area_chg_km2", "area_chg_pct",
                          "density_chg_pct"]].to_dict("records"),
        "migration": mig[["city", "year", "core_net_out_ring_per1000", "outward_share"]].round(1)
        .to_dict("records"),
        "lost": lost.to_dict("records"),
    }
    (SITE / "data.js").write_text("window.DID = " + json.dumps(data, ensure_ascii=False) + ";\n", encoding="utf-8")


def repo_files():
    (REPO / "scripts").mkdir(parents=True, exist_ok=True)
    for f in ["did_analysis.py", "fetch_facilities.py", "migration_od.py", "migration_analysis.py",
              "lost_did_flows.py", "build_site.py"]:
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
