"""Local-only HTTP interface; no dependency installation required."""
import argparse
import json
import os
import re
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from .service import Service, Conflict
from .providers import PDF_LIMIT

STATIC=Path(__file__).parent/'static'
CSP="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
# Browser PDF viewers run as plugins in the response's own document, so a stricter policy would blank them.
PDF_CSP="frame-ancestors 'self'"


class Handler(BaseHTTPRequestHandler):
    server_version='Orchard/0.1'

    def send(self, status, body, kind='application/json; charset=utf-8', download=None, inline=None, cache='no-store', csp=CSP):
        if not isinstance(body,bytes): body=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control',cache)
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',csp)
        if download: self.send_header('Content-Disposition','attachment; filename="'+download+'"')
        if inline: self.send_header('Content-Disposition','inline; filename="'+inline+'"')
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        expected={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        host=self.headers.get('Host','')
        if host not in expected:
            self.send(403,{'error':'Orchard accepts local-host requests only.'}); return False
        origin=self.headers.get('Origin')
        if origin and origin not in {'http://'+h for h in expected}:
            self.send(403,{'error':'Cross-origin requests are not allowed.'}); return False
        return True

    def do_GET(self):
        if not self.valid_host(): return
        try:
            self.get()
        except KeyError as e: self.send(404,{'error':str(e)})
        except ValueError as e: self.send(400,{'error':str(e)})
        except Exception as e:
            self.log_error('%s',str(e)); self.send(500,{'error':'Internal error. Inspect the local terminal log.'})

    def get(self):
        s=self.server.service
        parsed=urlparse(self.path); path=parsed.path; args={k:v[-1] for k,v in parse_qs(parsed.query).items()}
        if path=='/api/overview': return self.send(200,s.overview())
        if path=='/api/documents': return self.send(200,s.db.rows('SELECT id,name,sha256,created FROM documents ORDER BY rowid DESC'))
        if path.startswith('/api/documents/'): return self.send(200,s.document(path.split('/')[-1]))
        if path=='/api/queries': return self.send(200,s.db.rows('SELECT * FROM queries ORDER BY rowid DESC'))
        if path=='/api/runs': return self.send(200,s.db.rows('SELECT * FROM runs ORDER BY rowid DESC'))
        if path=='/api/papers': return self.send(200,s.listing(**{k:v for k,v in args.items() if k in ('q','page','status','year','source','resource_kind')}))
        if path.startswith('/api/papers/'): return self.send(200,s.paper(path.split('/')[-1]))
        if path.startswith('/api/pdfs/'):
            row,data=s.pdf_file(path.split('/')[-1])
            name=re.sub(r'[^A-Za-z0-9._-]+','_',row['name'])
            # Stored PDFs are content-addressed and never change, so the browser may keep them.
            return self.send(200,data,'application/pdf',inline=name if name.lower().endswith('.pdf') else name+'.pdf',cache='private, max-age=86400, immutable',csp=PDF_CSP)
        if path=='/api/duplicates': return self.send(200,s.db.rows('SELECT d.*,a.title title_a,b.title title_b,a.year year_a,b.year year_b,a.doi doi_a,b.doi doi_b FROM duplicates d JOIN papers a ON a.id=d.a JOIN papers b ON b.id=d.b ORDER BY d.rowid DESC'))
        if path=='/api/resources':
            return self.send(200,s.db.rows('SELECT r.*,p.title FROM resources r JOIN papers p ON p.id=r.paper_id ORDER BY r.rowid DESC'))
        if path=='/api/eyelink': return self.send(200,s.eyelink_scans())
        if path.startswith('/api/eyelink/'): return self.send(200,s.eyelink_scan(path.split('/')[-1]))
        if path=='/api/audit':
            page=max(1,int(args.get('page',1)))
            q=args.get('q','')
            return self.send(200,s.db.rows('SELECT * FROM audit WHERE event LIKE ? OR entity_id LIKE ? ORDER BY id DESC LIMIT 100 OFFSET ?',('%'+q+'%','%'+q+'%',(page-1)*100)))
        if path=='/api/export':
            if args.get('format')=='json':
                return self.send(200,json.dumps(s.export(),ensure_ascii=False,indent=2).encode(),download='orchard-export.json')
            return self.send(200,s.export_zip(),'application/zip','orchard-export.zip')
        files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if path in files:
            file=STATIC/files[path]
            kind={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'}[file.suffix]
            return self.send(200,file.read_bytes(),kind)
        self.send(404,{'error':'Not found'})

    def do_POST(self):
        if not self.valid_host(): return
        try:
            parsed=urlparse(self.path)
            if re.fullmatch(r'/api/papers/[^/]+/pdf',parsed.path):
                return self.send(200,self.upload_pdf(parsed))
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                raise ValueError('Send application/json.')
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=12_000_000: raise ValueError('Request must be between 1 byte and 12 MB.')
            data=json.loads(self.rfile.read(size))
            if not isinstance(data,dict): raise ValueError('JSON object required.')
            result=self.post(urlparse(self.path).path,data)
            self.send(200,result or {'ok':True})
        except Conflict as e: self.send(409,{'error':str(e)})
        except KeyError as e: self.send(404,{'error':str(e)})
        except (ValueError,TypeError) as e: self.send(400,{'error':str(e)})
        except Exception as e:
            self.log_error('%s',str(e)); self.send(500,{'error':'Internal error. Inspect the local terminal log.'})

    def upload_pdf(self,parsed):
        if self.headers.get('Content-Type','').split(';')[0]!='application/pdf':
            raise ValueError('Send the file as application/pdf.')
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=PDF_LIMIT: raise ValueError('PDF must be between 1 byte and 60 MB.')
        name=parse_qs(parsed.query).get('name',['article.pdf'])[-1]
        return self.server.service.store_pdf(parsed.path.split('/')[-2],self.rfile.read(size),name,'Uploaded by researcher: '+name)

    def post(self,path,b):
        s=self.server.service
        if path=='/api/project': return s.set_project(b.get('name',''),b.get('question',''))
        if path=='/api/documents': return s.import_document(b.get('name','Imported text'),b.get('text',''))
        if path=='/api/queries': return s.save_query(**b)
        if path=='/api/generate': return s.generate_query(**b)
        if path=='/api/search': return s.search(**b)
        if path=='/api/resume': return s.resume(b['id'])
        if path=='/api/records': return s.import_records(b.get('name','Imported records'),b['records'])
        if path=='/api/criteria': return s.save_criteria(b.get('inclusion',''),b.get('exclusion',''))
        if path=='/api/screen': return s.screen(**b)
        if path=='/api/assess': return s.assess(**b)
        if path=='/api/metadata': return s.update_metadata(**b)
        if path=='/api/duplicates': return s.resolve_duplicate(**b)
        if path=='/api/fulltext': return s.import_fulltext(**b)
        if path=='/api/fetch-fulltext': return s.fetch_fulltext(b['id'])
        if path=='/api/fetch-pdf': return s.fetch_pdf(b['id'])
        if path=='/api/resource': return s.resource_action(**b)
        if path=='/api/eyelink-scan': return s.scan_eyelink(b['id'],b.get('pdf_url'))
        if path=='/api/eyelink-scan-all': return s.scan_eyelink_batch(b.get('scope','unscanned'),b.get('limit',200))
        raise KeyError('Unknown API operation.')


def make_server(service,port=8765):
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.service=service
    return server


def main():
    parser=argparse.ArgumentParser(description='Orchard local literature search workspace')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir',default=os.environ.get('ORCHARD_DATA_DIR',str(Path(__file__).resolve().parent.parent/'data')))
    args=parser.parse_args()
    data=Path(args.data_dir).resolve()
    service=Service(data/'orchard.sqlite3')
    try:
        server=make_server(service,args.port)
    except OSError:
        service.close(); raise
    print(f'Orchard is running at http://127.0.0.1:{server.server_port}',flush=True)
    print(f'Database: {data / "orchard.sqlite3"}',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        server.server_close(); service.close()


if __name__=='__main__': main()
