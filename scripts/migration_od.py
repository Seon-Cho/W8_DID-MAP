"""MDIS 국내인구이동통계 마이크로데이터 → 전출지·전입지 OD 집계.

입력 (data/migration/, MDIS에서 받은 zip 그대로)
  인구관련연간자료_*_데이터.zip : 2005·2010·2015·2020·2024 개인(세대 단위 신고) 자료, 항목명 없는 CSV
  인구이동-섬머리_*_데이터.zip  : 2000 전입 시군구 × 전출 시군구 × 성·연령 합계표
  *_r.zip                      : MDIS 읽기 스크립트(R). 열 이름과 시군구 이름표를 여기서 읽는다.

출력 (data/migration/)
  od_sgg.csv  : year, o_sgg, d_sgg, reason, age_grp, persons   (시군구 = 시도 2자리 + 시군구 3자리)
  od_emd.csv  : year, o_emd, d_emd, persons                     (읍면동 = 시군구 5자리 + 읍면동 5자리, 2005~)
  sgg_names.csv : year, code, name                              (R 스크립트의 이름표)

이동자 수는 한 신고(행)에 적힌 전입자(최대 10명) 수를 더한 것이다.
전입사유는 2013년 이후 분류(직업·가족·주택·교육·주거환경·자연환경·기타)만 쓰고, 그 전 해는 '-'로 둔다.
"""
import io
import re
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "data" / "migration"
AGE_BINS = [-1, 19, 29, 44, 64, 200]
AGE_LABELS = ["0-19", "20-29", "30-44", "45-64", "65+"]
REASON_FROM = 2013
CHUNK = 1_000_000


def zip_name(info):
    return info.filename if info.flag_bits & 0x800 else info.filename.encode("cp437").decode("cp949")


def find_zip(pattern):
    hits = sorted(MIG.glob(pattern))
    if not hits:
        sys.exit(f"{pattern} 없음 (data/migration/)")
    return hits[-1]


def read_r_scripts():
    """{year: R 스크립트 텍스트}"""
    out = {}
    for z in list(MIG.glob("인구관련연간자료_*_r.zip")) + list(MIG.glob("인구이동-섬머리_*_r.zip")):
        zf = zipfile.ZipFile(z)
        for info in zf.infolist():
            out[int(zip_name(info)[:4])] = zf.read(info).decode("cp949")
    return out


def sgg_names(year, rtext):
    """R 스크립트의 recode_factor(mdis$V2, 'code'="name") 줄에서 시군구 이름표를 뽑는다."""
    rows = re.findall(r"mdis\$V2<- recode_factor\(mdis\$V2, '(\d+)'=\"([^\"]*)\"\)", rtext)
    return pd.DataFrame([(year, c, re.sub(r"\s+", "", n)) for c, n in rows], columns=["year", "code", "name"])


def persons_by_age(df):
    """전입자1~10의 만나이 열을 (행, 연령대) 인원으로 바꾼다."""
    ages = df[[f"a{i}" for i in range(1, 11)]].apply(pd.to_numeric, errors="coerce")
    long = ages.stack()  # NaN(빈 칸)은 빠진다
    grp = pd.cut(long.values, AGE_BINS, labels=AGE_LABELS)
    return pd.DataFrame({"row": long.index.get_level_values(0), "age_grp": grp})


def micro_year(zf, info, year):
    cols = list(range(41))
    names = ["d_sido", "d_sgg", "d_emd", "y", "m", "dd", "o_sido", "o_sgg", "o_emd", "reason"]
    for i in range(1, 11):
        names += [f"r{i}", f"a{i}", f"s{i}"]
    names += ["seq"]
    keep = ["d_sido", "d_sgg", "d_emd", "o_sido", "o_sgg", "o_emd", "reason"] + [f"a{i}" for i in range(1, 11)]
    sgg_parts, emd_parts = [], []
    with zf.open(info) as f:
        reader = pd.read_csv(io.TextIOWrapper(f, encoding="cp949"), header=None, names=names, usecols=keep,
                             dtype=str, chunksize=CHUNK)
        for n, df in enumerate(reader, 1):
            df = df.reset_index(drop=True)
            df["o"] = df["o_sido"] + df["o_sgg"]
            df["d"] = df["d_sido"] + df["d_sgg"]
            df["reason"] = df["reason"].fillna("9") if year >= REASON_FROM else "-"
            p = persons_by_age(df)
            p = p.join(df[["o", "d", "reason"]], on="row")
            sgg_parts.append(p.groupby(["o", "d", "reason", "age_grp"], observed=True).size())
            df["n"] = df[[f"a{i}" for i in range(1, 11)]].notna().sum(axis=1)
            emd = df.assign(oe=df["o"] + df["o_emd"], de=df["d"] + df["d_emd"]).groupby(["oe", "de"])["n"].sum()
            emd_parts.append(emd)
            print(f"    {year}: {n * CHUNK:,}행", flush=True)
    sgg = pd.concat(sgg_parts).groupby(level=[0, 1, 2, 3], observed=True).sum().rename("persons").reset_index()
    sgg.columns = ["o_sgg", "d_sgg", "reason", "age_grp", "persons"]
    emd = pd.concat(emd_parts).groupby(level=[0, 1]).sum().rename("persons").reset_index()
    emd.columns = ["o_emd", "d_emd", "persons"]
    sgg.insert(0, "year", year)
    emd.insert(0, "year", year)
    return sgg, emd


def summary_2000(zf, info):
    """2000 섬머리: 행 = 전입 시군구 × 신고월 × 전출 시군구, 열 = 성·5세 연령 인원. 합계 행(00/000)은 뺀다."""
    age5 = ["0-4", "5-9", "10-14", "15-19", "20-24", "25-29", "30-34", "35-39", "40-44", "45-49", "50-54",
            "55-59", "60-64", "65-69", "70-74", "75-79", "80+", "OTHER", "TOT"]
    names = ["d_sido", "d_sgg", "ym", "o_sido", "o_sgg"] + [f"M{a}" for a in age5] + [f"F{a}" for a in age5]
    with zf.open(info) as f:
        df = pd.read_csv(io.TextIOWrapper(f, encoding="cp949"), header=None, names=names, dtype=str)
    df = df[(df["o_sido"] != "00") & (df["o_sgg"] != "000")]
    df["o_sgg"] = df["o_sido"] + df["o_sgg"]
    df["d_sgg"] = df["d_sido"] + df["d_sgg"]
    grp = {"0-19": ["0-4", "5-9", "10-14", "15-19"], "20-29": ["20-24", "25-29"],
           "30-44": ["30-34", "35-39", "40-44"], "45-64": ["45-49", "50-54", "55-59", "60-64"],
           "65+": ["65-69", "70-74", "75-79", "80+"]}
    rows = []
    for g, ages in grp.items():
        cols = [f"{s}{a}" for s in "MF" for a in ages]
        v = df[cols].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1)
        rows.append(df[["o_sgg", "d_sgg"]].assign(age_grp=g, persons=v))
    out = pd.concat(rows).groupby(["o_sgg", "d_sgg", "age_grp"])["persons"].sum().reset_index()
    out = out[out["persons"] > 0]
    out.insert(0, "year", 2000)
    out.insert(3, "reason", "-")
    return out


def main():
    rtexts = read_r_scripts()
    pd.concat([sgg_names(y, t) for y, t in sorted(rtexts.items())]).to_csv(MIG / "sgg_names.csv", index=False)

    sgg_all, emd_all = [], []
    zf = zipfile.ZipFile(find_zip("인구이동-섬머리_*_데이터.zip"))
    print("2000 섬머리")
    sgg_all.append(summary_2000(zf, zf.infolist()[0]))

    zf = zipfile.ZipFile(find_zip("인구관련연간자료_*_데이터.zip"))
    for info in sorted(zf.infolist(), key=zip_name):
        year = int(zip_name(info)[:4])
        print(f"{year} 인구관련연간자료")
        s, e = micro_year(zf, info, year)
        sgg_all.append(s)
        emd_all.append(e)

    sgg = pd.concat(sgg_all)
    sgg["persons"] = sgg["persons"].astype(np.int64)
    sgg.to_csv(MIG / "od_sgg.csv", index=False)
    pd.concat(emd_all).to_csv(MIG / "od_emd.csv", index=False)
    print("\n연도별 이동자 수 (시군구 간 + 시군구 안)")
    print(sgg.groupby("year")["persons"].sum().map("{:,}".format).to_string())
    inter = sgg[sgg["o_sgg"] != sgg["d_sgg"]]
    print("\n연도별 시군구 간 이동자 수")
    print(inter.groupby("year")["persons"].sum().map("{:,}".format).to_string())


if __name__ == "__main__":
    main()
