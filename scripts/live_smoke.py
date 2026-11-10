"""Optional real-provider probe. No data is added to the live project."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from orchard.providers import NORMALIZERS, build_providers

if __name__=='__main__':
    providers, locators = build_providers()
    failed = False
    for name, provider in providers.items():
        if not provider.configured:
            print(json.dumps({'provider': name, 'status': 'skipped', 'note': provider.note}))
            continue
        try:
            page=provider.page('pupillometry AND "working memory"',size=1,callback=lambda event:print(json.dumps(event)))
            first=NORMALIZERS[name](page.records[0]) if page.records else None
            print(json.dumps({'provider':name,'status':'success','hit_count':page.total,'records_returned':len(page.records),
                              'first':first and {k:first[k] for k in ('source_id','title','doi','pmid','pmcid')}},ensure_ascii=False))
        except Exception as exc:
            failed = True
            print(json.dumps({'provider':name,'status':'failed','error':str(exc)}))
    for locator in locators:
        if not locator.configured:
            continue
        try:
            links, ids = locator.locate({'doi':'10.3389/fnhum.2016.00240'})
            print(json.dumps({'locator':locator.name,'status':'success','links':len(links),'ids':ids}))
        except Exception as exc:
            failed = True
            print(json.dumps({'locator':locator.name,'status':'failed','error':str(exc)}))
    sys.exit(1 if failed else 0)
