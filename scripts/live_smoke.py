"""Optional real-provider probe. No data is added to the live project."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from orchard.providers import EuropePMC

if __name__=='__main__':
    try:
        page=EuropePMC().page('pupillometry AND "working memory"',size=1,callback=lambda event:print(json.dumps(event)))
        print(json.dumps({'status':'success','hit_count':page.total,'records_returned':len(page.records)},indent=2))
        if page.records:
            print(json.dumps(page.records[0],ensure_ascii=False,indent=2))
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)},indent=2))
        sys.exit(1)
