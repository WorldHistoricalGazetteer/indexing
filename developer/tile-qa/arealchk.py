import json, subprocess
PW = open("/ix1/ishi/es/config/elastic.password").read().strip()
G = "http://gazetteer.crcd.pitt.edu:9200"
NS = ["alc","chgis","dgsd","dp","gb","gn","iv","ofs","og","pl","tgn","tm","wd","hgis"]
def q(body):
    out = subprocess.run(["curl","-s","-m","90","-u","elastic:"+PW,
        G+"/places/_count","-H","Content-Type: application/json",
        "-d",json.dumps(body)],capture_output=True,text=True).stdout
    try: return json.loads(out).get("count",-1)
    except Exception: return -1
print("  %-8s %12s %12s  %s" % ("ns","areal","total","needs labels?"))
need=[]
for ns in NS:
    tot=q({"query":{"term":{"namespace":ns}}})
    ar=q({"query":{"bool":{"filter":[{"term":{"namespace":ns}},
        {"nested":{"path":"geometries","query":{"term":{"geometries.geom_class":"area"}}}}]}}})
    flag = "YES - rebuild" if ar>0 else "no"
    if ar>0: need.append(ns)
    print("  %-8s %12s %12s  %s" % (ns, "{:,}".format(ar), "{:,}".format(tot), flag))
print("\n  buckets needing a labelled rebuild:", " ".join(need) if need else "(none)")
