#!/usr/bin/env python3
"""Build the CAMELS dashboard dataset from OSFI regulatory returns.

Source: OSFI "Banks" dataset on the Government of Canada Open Government Portal
  https://open.canada.ca/data/en/dataset/91ed76b4-a1a2-4f87-9c4c-59cd64f7a9de

Returns used (all values in CAD thousands unless a ratio):
  BA  - Basel III Capital Adequacy Return (BCAR), quarterly
  LR  - Leverage Requirements Return, quarterly
  P3  - Consolidated Statement of Income, quarterly, year-to-date
  E3  - Allowances for Expected Credit Losses / impaired assets, quarterly
  M4  - Consolidated Balance Sheet, monthly

Usage:
  python3 scripts/build_osfi_data.py              # download (if not cached) and build
  python3 scripts/build_osfi_data.py --refresh    # force re-download
  python3 scripts/build_osfi_data.py --cache DIR  # where raw CSVs live (default .cache/osfi)

Outputs (relative to the dashboard folder):
  data/osfi_camels.csv   long format: metric,bank,period,value (period = 2025 or 2025Q3)
  data/osfi_inputs.csv   the raw line items behind every metric, for audit
  data/camels_data.js    the same data as a script the dashboard loads
"""
import argparse, csv, datetime, json, os, sys, urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATASET = "https://open.canada.ca/data/dataset/91ed76b4-a1a2-4f87-9c4c-59cd64f7a9de/resource"
FILES = {
    "ba": "fe7617f7-a676-4966-aae3-c4cfbab4b935/download/banks_quarterly_ba.csv",
    "lr": "817d4005-aa4a-4c4f-9dad-6ce7eca40700/download/banks_quarterly_lr.csv",
    "p3": "027ee7f8-4b87-45cd-a10f-f95d3a5d4e09/download/banks_quarterly_p3.csv",
    "e3": "1f86e088-7d29-49c2-94de-ddbfe6559725/download/banks_quarterly_e3.csv",
    "m4": "d0f6040e-671c-4301-a235-e9e7ba164604/download/banks_monthly_m4.csv",
}
USER_AGENT = "camels-dashboard-research/1.0"

BANKS = {
    "Royal Bank of Canada": "RY",
    "The Toronto-Dominion Bank": "TD",
    "The Bank of Nova Scotia": "BNS",
    "Bank of Montreal": "BMO",
    "Canadian Imperial Bank of Commerce": "CM",
    "National Bank of Canada": "NA",
}
FIRST_FY = 2009

# ---- data point addresses -------------------------------------------------
BA = {"cet1": "1000", "t1_b2": "1001", "t1_b3": "1008", "total_b3": "1009", "rwa": "1202"}
LR = {"exposure": "D1501", "tier1": "D1502"}
P3 = {"nii": "8408", "pcl": "8463", "nii_after_pcl": "8464", "rev_after_pcl": "8535",
      "nie": "1284", "ni_equity": "1292"}
E3_PRE = {"gil": "3139", "acl": ["3131", "3186", "3190"]}      # to FY2017 (incurred loss)
E3_POST = {"gil": "3418", "acl": ["3019", "3038", "3057"],     # FY2018+ (IFRS 9, stage III)
           "acl_alt": ["3333", "3350", "3384", "3401", "3435", "3452"]}  # FY2018 filed by stage x assessment
M4_LOANS = ["0524", "0526", "0534", "0540", "0542", "0572", "2057", "2067", "2117", "2310", "0608"]
M4_DEPOSITS = ["0873", "0874", "0875", "0876", "0877", "0878",        # demand and notice
               "0880", "0881", "2202", "0616", "0618", "2339"]        # fixed-term
M4_COMMON_EQ = ["2357", "0503", "2225", "2604"]   # common shares, contributed surplus, RE, AOCI
M4_PREF = "2355"
M4_ASSETS = "1045"
M4_UNINSURED = "0542"
M4_INSURED = "0540"
M4_LIQUID = ["0488", "0863", "0865", "0866", "0518", "0520"]  # cash & deposits, all securities
M4_ALL = set(M4_LOANS + M4_DEPOSITS + M4_COMMON_EQ + M4_LIQUID + [M4_PREF, M4_ASSETS])


def download(cache, refresh):
    os.makedirs(cache, exist_ok=True)
    for key, path in FILES.items():
        dest = os.path.join(cache, f"{key}.csv")
        if os.path.exists(dest) and not refresh:
            continue
        print(f"downloading {key} ...", file=sys.stderr)
        req = urllib.request.Request(f"{DATASET}/{path}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=900) as r, open(dest + ".part", "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(dest + ".part", dest)


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def read_quarterly(path, addresses):
    """-> {(bank, fiscal_year, quarter_no): {address: value}}"""
    out = defaultdict(dict)
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        h = next(rd)
        i_y, i_q, i_n = h.index("Fiscal Year/Année fiscale"), h.index("Fiscal Quarter"), h.index("Total All Banks/Total Industry Groups/FIs")
        i_a, i_v = h.index("Data Point Address/Adresse de point de donnée"), h.index("Measure Value/Valeur de mesure")
        for r in rd:
            b = BANKS.get(r[i_n])
            if not b or r[i_a] not in addresses:
                continue
            v = num(r[i_v])
            if v is None:
                continue
            out[(b, int(r[i_y]), int(r[i_q][1]))][r[i_a]] = v
    return out


def read_monthly(path):
    """-> {(bank, 'YYYY-MM-DD'): {address: value}}"""
    out = defaultdict(dict)
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        h = next(rd)
        i_y, i_m, i_n = h.index("Calendar Year/Année civile"), h.index("Calendar Month/Mois civil"), h.index("Total All Banks/Total Industry Groups/FIs")
        i_a, i_v = h.index("Data Point Address/Adresse de point de donnée"), h.index("Measure Value/Valeur de mesure")
        for r in rd:
            if int(r[i_y]) < FIRST_FY - 1:
                continue
            b = BANKS.get(r[i_n])
            if not b or r[i_a] not in M4_ALL:
                continue
            v = num(r[i_v])
            if v is not None:
                out[(b, r[i_m])][r[i_a]] = v
    return out


QEND_MONTH = {1: 1, 2: 4, 3: 7, 4: 10}   # fiscal quarter -> calendar month of quarter end (Oct 31 year end)


def month_end(y, m):
    nxt = datetime.date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - datetime.timedelta(days=1)).isoformat()


def window(fy, q, n):
    """n month-ends ending at the close of fiscal quarter q of fiscal year fy."""
    y, m = fy, QEND_MONTH[q]
    keys = []
    for _ in range(n):
        keys.append(month_end(y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return keys[::-1]


def ssum(d, keys, require_all=False):
    vals = [d.get(k) for k in keys]
    if require_all and any(v is None for v in vals):
        return None
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def build(cache):
    ba = read_quarterly(os.path.join(cache, "ba.csv"), set(BA.values()))
    lr = read_quarterly(os.path.join(cache, "lr.csv"), set(LR.values()))
    p3 = read_quarterly(os.path.join(cache, "p3.csv"), set(P3.values()))
    e3 = read_quarterly(os.path.join(cache, "e3.csv"), {E3_PRE["gil"], E3_POST["gil"], *E3_PRE["acl"], *E3_POST["acl"], *E3_POST["acl_alt"]})
    m4 = read_monthly(os.path.join(cache, "m4.csv"))

    last_fy = max(y for (_, y, q) in p3 if q == 4)
    last_q = max((y, q) for (_, y, q) in p3)
    years = list(range(FIRST_FY, last_fy + 1))
    quarters = [(y, q) for y in range(FIRST_FY, last_q[0] + 1) for q in (1, 2, 3, 4) if (y, q) <= last_q]
    annual = defaultdict(lambda: defaultdict(dict))
    quarterly = defaultdict(lambda: defaultdict(dict))
    inputs = []

    def income(b, y, q, full_year):
        """Income items for the period. Annual = Q4 YTD; a quarter = YTD(q) - YTD(q-1)."""
        cur = p3.get((b, y, q), {})
        if full_year:
            q = 1  # use the year-to-date figure as is
        prev = p3.get((b, y, q - 1), {}) if q > 1 else {}
        out = {}
        for k in ("nii", "pcl", "nii_after_pcl", "rev_after_pcl", "nie", "ni_equity"):
            v = cur.get(P3[k])
            if q > 1:
                pv = prev.get(P3[k])
                v = None if v is None or pv is None else v - pv
            out[k] = v
        if out["pcl"] is None and out["nii"] is not None and out["nii_after_pcl"] is not None:
            out["pcl"] = out["nii"] - out["nii_after_pcl"]
        out["rev"] = out["rev_after_pcl"] + out["pcl"] if out["rev_after_pcl"] is not None and out["pcl"] is not None else None
        return out

    def compute(b, y, q, is_annual):
        put_to = annual if is_annual else quarterly
        key = y if is_annual else f"{y}Q{q}"
        ann = 1 if is_annual else 4          # annualize quarterly flows
        n_avg = 13 if is_annual else 4

        def put(m, v):
            if v is not None:
                put_to[m][b][key] = round(v, 4)

        ye = m4.get((b, month_end(y, QEND_MONTH[q])), {})
        loans = ssum(ye, M4_LOANS)
        # a bank may leave an empty deposit category blank; require the large ones
        deps = ssum(ye, M4_DEPOSITS) if all(ye.get(k) is not None for k in ("0877", "0878", "2339")) else None
        assets = ye.get(M4_ASSETS)
        liquid = ssum(ye, M4_LIQUID)

        def equity(d):
            ce = ssum(d, M4_COMMON_EQ, True)
            return None if ce is None else ce + (d.get(M4_PREF) or 0)
        avg = {}
        for name, fn in {"loans": lambda d: ssum(d, M4_LOANS), "assets": lambda d: d.get(M4_ASSETS), "equity": equity}.items():
            pts = [fn(m4.get((b, k), {})) for k in window(y, q, n_avg)]
            pts = [p for p in pts if p is not None]
            avg[name] = sum(pts) / len(pts) if len(pts) >= n_avg - 1 else None

        inc = income(b, y, q, is_annual)
        nii, pcl, nie, ni, rev = inc["nii"], inc["pcl"], inc["nie"], inc["ni_equity"], inc["rev"]

        cap = ba.get((b, y, q), {})
        cet1 = cap.get(BA["cet1"]) if y >= 2014 else None
        t1 = cap.get(BA["t1_b3"]) if y >= 2014 else (cap.get(BA["t1_b2"]) if y <= 2012 else None)
        lev = None
        lq = lr.get((b, y, q), {})
        if lq.get(LR["exposure"]) and lq.get(LR["tier1"]) is not None:
            lev = 100 * lq[LR["tier1"]] / lq[LR["exposure"]]

        e = e3.get((b, y, q), {})
        sch = E3_POST if y >= 2018 else E3_PRE
        gil = e.get(sch["gil"])
        acl = ssum(e, sch["acl"])
        if acl is None and "acl_alt" in sch:
            acl = ssum(e, sch["acl_alt"])

        put("cet1", cet1)
        put("t1", t1)
        put("lev", lev)
        put("pcl", ann * 1e4 * pcl / avg["loans"] if pcl is not None and avg["loans"] else None)
        put("gil", 1e4 * gil / loans if gil is not None and loans else None)
        put("cov", 100 * acl / gil if acl is not None and gil else None)
        put("unins", 100 * ye[M4_UNINSURED] / loans if loans and ye.get(M4_UNINSURED) is not None else None)
        put("eff", 100 * nie / rev if nie is not None and rev else None)
        put("roe", ann * 100 * ni / avg["equity"] if ni is not None and avg["equity"] else None)
        put("roa", ann * 1e4 * ni / avg["assets"] if ni is not None and avg["assets"] else None)
        put("nim", ann * 100 * nii / avg["assets"] if nii is not None and avg["assets"] else None)
        put("niish", 100 * nii / rev if nii is not None and rev else None)
        put("ltd", 100 * loans / deps if loans and deps else None)
        put("liq", 100 * liquid / assets if liquid is not None and assets else None)

        inputs.append({"bank": b, "period": f"FY{y}" if is_annual else f"FY{y} Q{q}",
                       "net_loans_end": loans, "deposits_end": deps, "total_assets_end": assets,
                       "avg_loans": avg["loans"], "avg_assets": avg["assets"], "avg_sh_equity": avg["equity"],
                       "nii": nii, "pcl": pcl, "revenue": rev, "nie": nie, "ni_to_equity_holders": ni,
                       "gross_impaired": gil, "allowance_total": acl, "uninsured_resi_mortgages": ye.get(M4_UNINSURED),
                       "insured_resi_mortgages": ye.get(M4_INSURED), "cet1_ratio": cet1, "tier1_ratio": t1, "leverage_ratio": lev})

    for b in BANKS.values():
        for y in years:
            compute(b, y, 4, True)
        for y, q in quarters:
            compute(b, y, q, False)
    return years, quarters, annual, quarterly, inputs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(ROOT, ".cache", "osfi"))
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    download(a.cache, a.refresh)
    years, quarters, annual, quarterly, inputs = build(a.cache)
    qkeys = [f"{y}Q{q}" for y, q in quarters]

    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "osfi_camels.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "bank", "period", "value"])
        for store in (annual, quarterly):
            for m in store:
                for b in store[m]:
                    for k, v in store[m][b].items():
                        w.writerow([m, b, k, v])
    with open(os.path.join(ROOT, "data", "osfi_inputs.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(inputs[0].keys()))
        w.writeheader()
        w.writerows(inputs)
    payload = {
        "source": "OSFI regulatory returns (BA, LR, P3, E3, M4), Open Government Portal dataset 91ed76b4-a1a2-4f87-9c4c-59cd64f7a9de",
        "built": datetime.date.today().isoformat(),
        "years": years,
        "metrics": {m: {b: [annual[m][b].get(y) for y in years] for b in BANKS.values()} for m in annual},
        "quarters": qkeys,
        "qmetrics": {m: {b: [quarterly[m][b].get(k) for k in qkeys] for b in BANKS.values()} for m in quarterly},
    }
    with open(os.path.join(ROOT, "data", "camels_data.js"), "w") as f:
        f.write("/* Generated by scripts/build_osfi_data.py - do not edit by hand. */\n")
        f.write("window.CAMELS_DATA = " + json.dumps(payload, separators=(",", ":")) + ";\n")
    print(f"built FY{years[0]}-FY{years[-1]} annual and {qkeys[0]}-{qkeys[-1]} quarterly: {len(annual)} metrics", file=sys.stderr)


if __name__ == "__main__":
    main()
