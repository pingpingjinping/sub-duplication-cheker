import io, json, tempfile, unittest, zipfile, threading
from pathlib import Path
from engine import Engine, SOURCES, sha, payload_signature, Cancelled

def zip_bytes(files):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
        for n,d in files:z.writestr(n,d)
    return b.getvalue()

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.out=self.root/'out'
        self.a=self.root/'a';self.b=self.root/'b';self.c=self.root/'c'
        for p in (self.a,self.b,self.c):p.mkdir()
        self.engine=Engine()
    def tearDown(self):self.tmp.cleanup()
    def put(self,p,name,data):
        f=p/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(data);return f
    def analyze(self,**kw):return self.engine.analyze({SOURCES[0]:[self.a],SOURCES[1]:[self.b],SOURCES[2]:[self.c]},self.out,**kw)
    def test_cross_renamed_and_originals_and_restore(self):
        f=self.put(self.a,'작품/1화/user/a.smi',b'hello');g=self.put(self.b,'작품/1화/other/b.smi',b'hello')
        r=self.analyze();self.assertEqual(sum(x['delete'] for x in r['records']),1)
        self.engine.apply(r);self.assertEqual(sum(x['actual'] for x in r['records']),1)
        self.assertEqual(f.read_bytes(),b'hello');self.assertEqual(g.read_bytes(),b'hello')
        for p in Path(r['run']).glob('*_cleaned.zip'):
            with zipfile.ZipFile(p) as z:self.assertIsNone(z.testzip())
        self.engine.restore(r);self.assertEqual(sum(x['actual'] for x in r['records']),0)
    def test_inner_rename_and_auxiliary_differences(self):
        self.put(self.a,'작품/one.zip',zip_bytes([('a.smi',b'sub'),('font.ttf',b'font')]))
        self.put(self.a,'작품/two.zip',zip_bytes([('renamed.smi',b'sub'),('other.ttf',b'font')]))
        self.put(self.a,'작품/three.zip',zip_bytes([('a.smi',b'sub'),('font.ttf',b'different')]))
        r=self.analyze();self.assertEqual(sum(x['delete'] for x in r['records']),1)
    def test_recursive_archive_and_multiplicity(self):
        f=self.put(self.a,'a.zip',zip_bytes([('nested.zip',zip_bytes([('a.srt',b'sub')]))]))
        g=self.put(self.a,'b.zip',zip_bytes([('b.srt',b'sub')]))
        h=self.put(self.a,'c.zip',zip_bytes([('b.srt',b'sub'),('c.srt',b'sub')]))
        self.assertEqual(payload_signature(f)[0],payload_signature(g)[0]);self.assertNotEqual(payload_signature(g)[0],payload_signature(h)[0])
    def test_preserve_versions_and_corrupt(self):
        self.put(self.a,'작품/1화/a.smi',b'version1');self.put(self.b,'작품/1화/a.smi',b'version2')
        self.put(self.a,'broken.zip',b'not zip');self.put(self.a,'copy.zip',b'not zip')
        r=self.analyze();self.assertEqual(sum(x['delete'] for x in r['records']),0);self.assertGreaterEqual(len(r['reviews']),4)
    def test_cross_off(self):
        self.put(self.a,'a.smi',b'x');self.put(self.b,'b.smi',b'x')
        r=self.analyze(cross=False);self.assertEqual(sum(x['delete'] for x in r['records']),0)
    def test_traversal_rejected(self):
        f=self.put(self.a,'part.zip',zip_bytes([('../outside.smi',b'x')]))
        with self.assertRaises(ValueError):self.engine.analyze({SOURCES[0]:[f]},self.out)
        self.assertFalse((self.root/'outside.smi').exists())
    def test_split_collision_never_overwritten_and_restore(self):
        f=self.put(self.a,'anissia_subtitles_part001.zip',zip_bytes([('anissia_subtitles/작품/a.smi',b'one')]))
        g=self.put(self.a,'anissia_subtitles_part002.zip',zip_bytes([('anissia_subtitles/작품/a.smi',b'two')]))
        r=self.engine.analyze({SOURCES[0]:[self.a]},self.out);self.assertEqual(len(r['records']),2)
        self.assertEqual({x['sha'] for x in r['records']},{__import__('hashlib').sha256(b'one').hexdigest(),__import__('hashlib').sha256(b'two').hexdigest()})
    def test_archive_origin_restore(self):
        f=self.put(self.a,'part.zip',zip_bytes([('a.smi',b'one'),('b.smi',b'one')]))
        r=self.engine.analyze({SOURCES[0]:[f]},self.out);self.engine.apply(r);self.engine.restore(r)
        self.assertEqual(len(list((Path(r['run'])/'cleaned'/SOURCES[0]).glob('*.smi'))),2)
    def test_original_change_blocks_deletion(self):
        self.put(self.a,'a.smi',b'one');f=self.put(self.a,'b.smi',b'one')
        r=self.analyze();f.write_bytes(b'changed')
        with self.assertRaises(ValueError):self.engine.apply(r)
        self.assertEqual(sum(x['actual'] for x in r['records']),0)
    def test_normal_location_wins(self):
        self.put(self.a,'잘못된작품/마슐.zip',zip_bytes([('마슐.smi',b'one')]))
        self.put(self.b,'마슐/마슐.zip',zip_bytes([('마슐.smi',b'one')]))
        r=self.analyze();winner=next(x for x in r['records'] if not x['delete']);self.assertEqual(winner['source'],SOURCES[1])
    def test_zero_files_not_deleted_and_cancel(self):
        self.put(self.a,'a.smi',b'');self.put(self.a,'b.smi',b'');r=self.analyze();self.assertEqual(sum(x['delete'] for x in r['records']),0)
        stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):Engine(stop=stop).analyze({SOURCES[0]:[self.a]},self.out)
    def test_excel_structure_and_log_reconcile(self):
        self.put(self.a,'a.smi',b'one');self.put(self.a,'b.smi',b'one');r=self.analyze();self.engine.apply(r)
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(Path(r['run'])/'subtitle_cleanup_report.xlsx') as z:
            for name in z.namelist():
                if name.endswith('.xml') or name.endswith('.rels'):ET.fromstring(z.read(name))
            ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            sheet=ET.fromstring(z.read('xl/worksheets/sheet2.xml'))
            self.assertEqual(len(sheet.findall('.//m:row',ns))-1,1)
            self.assertIsNotNone(sheet.find('m:autoFilter',ns));self.assertIsNotNone(sheet.find('.//m:pane',ns))

if __name__=='__main__':unittest.main()
