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
  data/osfi_camels.csv   long format: metric,bank,year,value
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


def month_ends(fy):
    """13 month-ends from Oct 31 of fy-1 to Oct 31 of fy (keys as OSFI writes them)."""
    keys = []
    for k in range(13):
        m = 10 + k
        y = fy - 1 + (m - 1) // 12
        m = (m - 1) % 12 + 1
        nxt = datetime.date(y + (m == 12), m % 12 + 1, 1)
        keys.append((nxt - datetime.timedelta(days=1)).isoformat())
    return keys


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
    years = list(range(FIRST_FY, last_fy + 1))
    metrics = defaultdict(lambda: defaultdict(dict))
    inputs = []

    def put(m, b, y, v):
        if v is not None:
            metrics[m][b][y] = round(v, 4)

    for b in BANKS.values():
        for y in years:
            q4 = (b, y, 4)
            # ---- M4 balance sheet: year-end and 13-month averages
            ye = m4.get((b, f"{y}-10-31"), {})
            loans = ssum(ye, M4_LOANS)
            # a bank may leave an empty deposit category blank; require the large ones
            deps = ssum(ye, M4_DEPOSITS) if all(ye.get(k) is not None for k in ("0877", "0878", "2339")) else None
            assets = ye.get(M4_ASSETS)
            liquid = ssum(ye, M4_LIQUID)
            avg = {}
            for name, fn in {
                "loans": lambda d: ssum(d, M4_LOANS),
                "assets": lambda d: d.get(M4_ASSETS),
                "equity": lambda d: (ssum(d, M4_COMMON_EQ, True) or 0) + (d.get(M4_PREF) or 0) if ssum(d, M4_COMMON_EQ, True) is not None else None,
            }.items():
                pts = [fn(m4.get((b, k), {})) for k in month_ends(y)]
                pts = [p for p in pts if p is not None]
                avg[name] = sum(pts) / len(pts) if len(pts) >= 12 else None

            # ---- P3 income statement (Q4 year-to-date = fiscal year)
            inc = p3.get(q4, {})
            nii, pcl, nie, ni = inc.get(P3["nii"]), inc.get(P3["pcl"]), inc.get(P3["nie"]), inc.get(P3["ni_equity"])
            if pcl is None and inc.get(P3["nii"]) is not None and inc.get(P3["nii_after_pcl"]) is not None:
                pcl = inc[P3["nii"]] - inc[P3["nii_after_pcl"]]
            rev = inc.get(P3["rev_after_pcl"]) + pcl if inc.get(P3["rev_after_pcl"]) is not None and pcl is not None else None

            # ---- capital
            cap = ba.get(q4, {})
            cet1 = cap.get(BA["cet1"]) if y >= 2014 else None
            t1 = cap.get(BA["t1_b3"]) if y >= 2014 else (cap.get(BA["t1_b2"]) if y <= 2012 else None)
            lev = None
            l4 = lr.get(q4, {})
            if l4.get(LR["exposure"]) and l4.get(LR["tier1"]) is not None:
                lev = 100 * l4[LR["tier1"]] / l4[LR["exposure"]]

            # ---- impaired loans
            e = e3.get(q4, {})
            sch = E3_POST if y >= 2018 else E3_PRE
            gil = e.get(sch["gil"])
            acl = ssum(e, sch["acl"])
            if acl is None and "acl_alt" in sch:
                acl = ssum(e, sch["acl_alt"])

            put("cet1", b, y, cet1)
            put("t1", b, y, t1)
            put("lev", b, y, lev)
            put("pcl", b, y, 1e4 * pcl / avg["loans"] if pcl is not None and avg["loans"] else None)
            put("gil", b, y, 1e4 * gil / loans if gil is not None and loans else None)
            put("cov", b, y, 100 * acl / gil if acl is not None and gil else None)
            put("unins", b, y, 100 * ye[M4_UNINSURED] / loans if loans and ye.get(M4_UNINSURED) is not None else None)
            put("eff", b, y, 100 * nie / rev if nie is not None and rev else None)
            put("roe", b, y, 100 * ni / avg["equity"] if ni is not None and avg["equity"] else None)
            put("roa", b, y, 1e4 * ni / avg["assets"] if ni is not None and avg["assets"] else None)
            put("nim", b, y, 100 * nii / avg["assets"] if nii is not None and avg["assets"] else None)
            put("niish", b, y, 100 * nii / rev if nii is not None and rev else None)
            put("ltd", b, y, 100 * loans / deps if loans and deps else None)
            put("liq", b, y, 100 * liquid / assets if liquid is not None and assets else None)

            inputs.append({"bank": b, "fy": y, "net_loans_ye": loans, "deposits_ye": deps, "total_assets_ye": assets,
                           "avg_loans_13m": avg["loans"], "avg_assets_13m": avg["assets"], "avg_sh_equity_13m": avg["equity"],
                           "nii": nii, "pcl": pcl, "revenue": rev, "nie": nie, "ni_to_equity_holders": ni,
                           "gross_impaired": gil, "allowance_total": acl, "uninsured_resi_mortgages": ye.get(M4_UNINSURED),
                           "insured_resi_mortgages": ye.get(M4_INSURED), "cet1_ratio": cet1, "tier1_ratio": t1, "leverage_ratio": lev})
    return years, metrics, inputs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(ROOT, ".cache", "osfi"))
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    download(a.cache, a.refresh)
    years, metrics, inputs = build(a.cache)

    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "osfi_camels.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "bank", "year", "value"])
        for m in metrics:
            for b in metrics[m]:
                for y, v in sorted(metrics[m][b].items()):
                    w.writerow([m, b, y, v])
    with open(os.path.join(ROOT, "data", "osfi_inputs.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(inputs[0].keys()))
        w.writeheader()
        w.writerows(inputs)
    payload = {
        "source": "OSFI regulatory returns (BA, LR, P3, E3, M4), Open Government Portal dataset 91ed76b4-a1a2-4f87-9c4c-59cd64f7a9de",
        "built": datetime.date.today().isoformat(),
        "years": years,
        "metrics": {m: {b: [metrics[m][b].get(y) for y in years] for b in BANKS.values()} for m in metrics},
    }
    with open(os.path.join(ROOT, "data", "camels_data.js"), "w") as f:
        f.write("/* Generated by scripts/build_osfi_data.py - do not edit by hand. */\n")
        f.write("window.CAMELS_DATA = " + json.dumps(payload, separators=(",", ":")) + ";\n")
    print(f"built FY{years[0]}-FY{years[-1]}: {len(metrics)} metrics", file=sys.stderr)


if __name__ == "__main__":
    main()
