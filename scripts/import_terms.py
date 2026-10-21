"""Import a document locally and optionally draft Eyeris searches; never execute them."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from orchard.service import Service

parser=argparse.ArgumentParser()
parser.add_argument('file',type=Path)
parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parents[1]/'data')
parser.add_argument('--eyeris-drafts',action='store_true')
args=parser.parse_args()
s=Service(args.data_dir/'orchard.sqlite3')
try:
    doc=s.document(s.import_document(args.file.name,args.file.read_text(encoding='utf-8'))['id'])
    print(f"Imported {len(doc['concepts'])} concept groups; original text retained.")
    if args.eyeris_drafts:
        by_label={c['label']:c for c in doc['concepts']}
        pupil=by_label.get('Measurement')
        if pupil:
            for label in ('Working and short-term memory','Episodic and recognition memory','Sustained attention and mind wandering','Emotion and affect','Mental effort and workload'):
                domain=by_label.get(label)
                if not domain: continue
                existing=s.db.one('SELECT id FROM queries WHERE document_id=? AND label=?',(doc['id'],label+' — broad'))
                if existing: continue
                s.generate_query([[t['id'] for t in pupil['terms']],[t['id'] for t in domain['terms']]],doc['id'],label+' — broad')
        print('Drafts created only for matching source headings. Review and approve in the app before searching.')
finally: s.close()
