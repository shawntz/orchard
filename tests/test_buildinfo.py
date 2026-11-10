"""The running version and commit SHA are reported for deployment checks."""
import json, tempfile, threading, unittest, urllib.request
from pathlib import Path
from orchard import __version__
from orchard.buildinfo import build_info, git_commit
from orchard.server import make_server
from orchard.service import Service

A='a'*40
B='b'*40


class GitCommit(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.git=self.root/'.git'; self.git.mkdir()

    def tearDown(self): self.tmp.cleanup()

    def test_branch_loose_ref(self):
        (self.git/'HEAD').write_text('ref: refs/heads/main\n')
        (self.git/'refs/heads').mkdir(parents=True); (self.git/'refs/heads/main').write_text(A+'\n')
        self.assertEqual(git_commit(self.root),A)

    def test_branch_packed_ref(self):
        (self.git/'HEAD').write_text('ref: refs/heads/main\n')
        (self.git/'packed-refs').write_text(f'# pack-refs with: peeled fully-peeled sorted\n{B} refs/heads/other\n{A} refs/heads/main\n')
        self.assertEqual(git_commit(self.root),A)

    def test_detached_head(self):
        (self.git/'HEAD').write_text(B+'\n')
        self.assertEqual(git_commit(self.root),B)

    def test_worktree_gitdir_file(self):
        main=self.root/'main'; wt=main/'.git/worktrees/wt'; wt.mkdir(parents=True)
        (wt/'HEAD').write_text('ref: refs/heads/feature\n'); (wt/'commondir').write_text('../..\n')
        (main/'.git/refs/heads').mkdir(parents=True); (main/'.git/refs/heads/feature').write_text(B)
        checkout=self.root/'checkout'; checkout.mkdir(); (checkout/'.git').write_text(f'gitdir: {wt}\n')
        self.assertEqual(git_commit(checkout),B)

    def test_missing_or_garbage(self):
        self.assertIsNone(git_commit(self.root/'nowhere'))
        (self.git/'HEAD').write_text('ref: refs/heads/missing\n')
        self.assertIsNone(git_commit(self.root))
        (self.git/'HEAD').write_text('not a sha\n')
        self.assertIsNone(git_commit(self.root))

    def test_env_override(self):
        (self.git/'HEAD').write_text(B)
        self.assertEqual(build_info(self.root,{'ORCHARD_COMMIT':A.upper()}),{'version':__version__,'commit':A,'commit_short':'aaaaaaa'})
        self.assertEqual(build_info(self.root,{})['commit'],B)
        self.assertIsNone(build_info(self.root/'nowhere',{'ORCHARD_COMMIT':'<script>'})['commit'])


class VersionEndpoint(unittest.TestCase):
    def test_api_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Service(Path(tmp)/'orchard.sqlite3'); server=make_server(s,0)
            t=threading.Thread(target=server.serve_forever,daemon=True); t.start()
            try:
                req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/version',headers={'Host':f'127.0.0.1:{server.server_port}'})
                with urllib.request.urlopen(req) as r: body=json.load(r)
            finally:
                server.shutdown(); server.server_close(); t.join(); s.close()
        self.assertEqual(body['version'],__version__)
        self.assertEqual(set(body),{'version','commit','commit_short'})


if __name__=='__main__': unittest.main()
