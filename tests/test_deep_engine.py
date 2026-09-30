import io, tempfile, unittest, zipfile
from pathlib import Path
from deep_engine import Engine
from engine import SOURCES, sha

def zb(files):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in files:z.writestr(name,data)
    return b.getvalue()

def leaves(path):
    out=[]
    def visit(data):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                data=z.read(info)
                if info.filename.endswith('.zip'):visit(data)
                else:out.append((info.filename,data))
    visit(path.read_bytes());return out

class DeepTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.a=self.root/'a';self.b=self.root/'b';self.a.mkdir();self.b.mkdir();self.e=Engine()
    def tearDown(self):self.tmp.cleanup()
    def put(self,folder,name,data):
        p=folder/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);return p
    def run_analysis(self):return self.e.analyze({SOURCES[0]:[self.a],SOURCES[1]:[self.b]},self.root/'out')
    def test_progressive_archives_prune_only_identical_episodes(self):
        originals=[]
        for n in (1,2,3):
            p=self.put(self.a,f'작품/1-{n}.zip',zb([(f'{i}화.smi',f'episode{i}'.encode()) for i in range(1,n+1)]));originals.append((p,sha(p)))
        r=self.run_analysis();self.assertEqual(len(r['records']),6);self.assertEqual(sum(x['delete'] for x in r['records']),3)
        self.e.apply(r);out=Path(r['run'])/(SOURCES[0]+'_cleaned.zip')
        self.assertEqual(sorted(d for _,d in leaves(out)),[b'episode1',b'episode2',b'episode3'])
        for p,h in originals:self.assertEqual(sha(p),h)
        self.assertEqual(sum(x['actual'] for x in r['records']),3)
    def test_renamed_plain_against_nested_and_sync_variants(self):
        same=b'00:00:01 --> 00:00:02\nhello'
        self.put(self.a,'작품/renamed.srt',same)
        self.put(self.b,'작품/bundle.zip',zb([('inner.zip',zb([('different.srt',same),('sync.srt',same.replace(b'01',b'03')),('text.srt',same+b'!')]))]))
        r=self.run_analysis();self.assertEqual(sum(x['delete'] for x in r['records']),1)
        self.e.apply(r)
        allfiles=[]
        for p in Path(r['run']).glob('*_cleaned.zip'):allfiles+=leaves(p)
        self.assertEqual(len(allfiles),3);self.assertEqual(sum(d==same for _,d in allfiles),1)
    def test_empty_container_removed_and_restore(self):
        data=zb([('a.smi',b'a')]);self.put(self.a,'same1.zip',data);self.put(self.a,'same2.zip',data)
        r=self.run_analysis();self.e.apply(r)
        folder=Path(r['run'])/'cleaned'/SOURCES[0];self.assertEqual(len(list(folder.glob('*.zip'))),1)
        self.e.restore(r);restored=Path(r['run'])/'restored'/SOURCES[0]
        self.assertEqual(len(list(restored.glob('*.zip'))),2)
        self.assertEqual((restored/'same1.zip').read_bytes(),data)
    def test_auxiliary_difference_is_preserved_with_shared_subtitle_removed(self):
        self.put(self.a,'a.zip',zb([('a.smi',b'sub'),('font.ttf',b'font1')]))
        self.put(self.b,'b.zip',zb([('b.smi',b'sub'),('font.ttf',b'font2')]))
        r=self.run_analysis();self.e.apply(r)
        output=[]
        for p in Path(r['run']).glob('*_cleaned.zip'):output+=leaves(p)
        self.assertEqual(sorted(d for _,d in output),[b'font1',b'font2',b'sub'])
    def test_corrupt_nested_archive_stays_opaque(self):
        self.put(self.a,'broken.zip',b'bad');self.put(self.b,'broken.zip',b'bad')
        r=self.run_analysis();self.assertEqual(sum(x['delete'] for x in r['records']),0)
        self.e.apply(r);self.assertTrue(all(not x['actual'] for x in r['records']))
    def test_reloaded_manifest_and_exact_logs(self):
        import json
        self.put(self.a,'one.zip',zb([('1.smi',b'a'),('two.smi',b'a'),('3.smi',b'c')]))
        r=self.run_analysis();r=json.loads((Path(r['run'])/'manifest.json').read_text(encoding='utf-8'));self.e.apply(r)
        # Excel is standard OOXML; check deletion count without requiring extra libraries in CI.
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(Path(r['run'])/'subtitle_cleanup_report.xlsx') as z:
            rows=ET.fromstring(z.read('xl/worksheets/sheet2.xml')).findall('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row')
            self.assertEqual(len(rows)-1,1)
    def test_same_named_version_preserved_and_reviewed(self):
        self.put(self.a,'작품/a.zip',zb([('episode.srt',b'time1')]))
        self.put(self.b,'작품/b.zip',zb([('episode.srt',b'time2')]))
        r=self.run_analysis();self.assertEqual(len(r['reviews']),2);self.assertEqual(sum(x['delete'] for x in r['records']),0)
    def test_rebuild_temp_name_cannot_remove_real_folder(self):
        self.put(self.a,'작품/bundle.zip',zb([('a.smi',b'a')]))
        self.put(self.a,'작품/bundle.zip.members/a.smi',b'unique')
        r=self.run_analysis();self.e.apply(r)
        p=Path(r['run'])/'cleaned'/SOURCES[0]/'작품/bundle.zip.members/a.smi'
        self.assertEqual(p.read_bytes(),b'unique')

if __name__=='__main__':unittest.main()
