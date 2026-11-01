"""확대 분석 지역의 차량 도로망을 OpenStreetMap에서 만든다 (접근성 분석의 도로 이동시간용).

입력: data/roads/south-korea-latest.osm.pbf (Geofabrik 한국 추출본, https://download.geofabrik.de/asia/south-korea.html)
범위: fetch_facilities와 같다(시 경계 + 15km).
출력: data/roads/{지역}.graphml  (EPSG:5179, 간선 속성 length[m], minutes[분])

간선 속도는 OSM maxspeed가 숫자면 그 값, 없으면 도로 등급별 기본값(SPEED_KMH)을 쓴다.
일방통행은 무시하고 양방향으로 본다(1km 격자 단위 접근성에서는 영향이 작다).

실행: ~/.venvs/did-analysis/bin/python scripts/fetch_roads.py
"""
import re

import networkx as nx
import osmium
import pyproj

from did_analysis import DATA
from fetch_facilities import city_areas

ROADS = DATA / "roads"
PBF = ROADS / "south-korea-latest.osm.pbf"
# 도로 등급별 기본 속도(km/h). 도시부 제한속도(일반도로 50, 이면도로 30)와 고속도로 100을 따랐다.
SPEED_KMH = {
    "motorway": 100, "motorway_link": 50, "trunk": 80, "trunk_link": 40,
    "primary": 60, "primary_link": 40, "secondary": 50, "secondary_link": 40,
    "tertiary": 40, "tertiary_link": 30, "unclassified": 30, "residential": 30, "living_street": 20,
}


class Ways(osmium.SimpleHandler):
    """차량 도로 등급의 way를 노드 좌표와 함께 모은다. 범위 상자 중 하나에 걸치는 것만."""

    def __init__(self, boxes):
        super().__init__()
        self.boxes = boxes
        self.ways = []

    def way(self, w):
        hw = w.tags.get("highway")
        if hw not in SPEED_KMH:
            return
        try:
            pts = [(n.ref, n.lon, n.lat) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        hit = [c for c, (x0, y0, x1, y1) in self.boxes.items()
               if any(x0 <= lon <= x1 and y0 <= lat <= y1 for _, lon, lat in pts)]
        if hit:
            m = re.match(r"\d+", w.tags.get("maxspeed", ""))
            self.ways.append((hit, pts, float(m.group()) if m else SPEED_KMH[hw], hw))


def main():
    areas, crs = city_areas()
    boxes = {c: (vb[0], vb[3], vb[2], vb[1]) for c, (_, _, vb) in areas.items()}  # (서, 남, 동, 북)
    todo = {c: b for c, b in boxes.items() if not (ROADS / f"{c}.graphml").exists()}
    if not todo:
        print("모두 있음")
        return
    h = Ways(todo)
    h.apply_file(str(PBF), locations=True, idx="flex_mem")
    to5179 = pyproj.Transformer.from_crs(4326, crs, always_xy=True).transform
    for city in todo:
        g = nx.Graph(crs=str(crs))
        for hit, pts, kmh, hw in h.ways:
            if city not in hit:
                continue
            xy = [(ref, *to5179(lon, lat)) for ref, lon, lat in pts]
            for (a, ax, ay), (b, bx, by) in zip(xy[:-1], xy[1:]):
                g.add_node(a, x=ax, y=ay)
                g.add_node(b, x=bx, y=by)
                length = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
                minutes = length / 1000 / kmh * 60
                if not g.has_edge(a, b) or g[a][b]["minutes"] > minutes:
                    g.add_edge(a, b, length=length, minutes=minutes, highway=hw)
        g = g.subgraph(max(nx.connected_components(g), key=len)).copy()  # 끊어진 조각은 버린다
        nx.write_graphml(g, ROADS / f"{city}.graphml")
        print(f"{city}: 노드 {g.number_of_nodes():,}개, 간선 {g.number_of_edges():,}개")


if __name__ == "__main__":
    main()
