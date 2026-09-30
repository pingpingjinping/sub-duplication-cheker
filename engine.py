from __future__ import annotations
import collections, csv, hashlib, io, json, os, re, shutil, stat, tempfile, threading, unicodedata, zipfile
from pathlib import Path, PurePosixPath
from datetime import datetime
from xml.sax.saxutils import escape

SOURCES = ('anissia_subtitles', 'aniall_subtitles', 'naverblog_subtitles')
SUBS = {'.smi', '.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.txt'}

class Cancelled(Exception): pass

def fs_path(path):
    """Return an absolute filesystem path with Windows extended-length prefix."""
    s = os.path.abspath(os.fspath(path))
    if os.name != 'nt' or s.startswith('\\\\?\\'):
        return s
    if s.startswith('\\\\'):
        return '\\\\?\\UNC\\' + s[2:]
    return '\\\\?\\' + s

def ensure_dir(path):
    os.makedirs(fs_path(path), exist_ok=True)

def file_size(path):
    return os.stat(fs_path(path)).st_size

def copy2_file(src, dst):
    ensure_dir(Path(dst).parent)
    try:
        return shutil.copy2(fs_path(src), fs_path(dst))
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f'{e}\n원본: {src} (길이 {len(str(src))})\n대상: {dst} (길이 {len(str(dst))})'
        ) from e

def sha(path):
    h = hashlib.sha256()
    with open(fs_path(path), 'rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''): h.update(b)
    return h.hexdigest()

def normal(s):
    return re.sub(r'[^\w]', '', unicodedata.normalize('NFKC', s).casefold())

def common(path):
    n = normal(Path(path).name)
    return Path(path).suffix.lower() in {'.ttf', '.otf', '.woff', '.woff2'} or any(x in n for x in ('폰트', 'font', 'readme', '읽어'))

def safe_member(name):
    name = name.replace('\\', '/')
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or any(':' in x for x in p.parts):
        raise ValueError('안전하지 않은 ZIP 경로: ' + name)
    if any(x.rstrip(' .') != x or re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', x) for x in p.parts):
        raise ValueError('Windows에서 사용할 수 없는 ZIP 경로: ' + name)
    return p

def payload_signature(path, check=lambda: None, max_bytes=2 * 1024**3):
    """Full recursive payload multiset. Filename-only changes allowed; multiplicity retained.
    Any differing auxiliary file also makes archives distinct. Never compare only subtitles.
    """
    leaves, subtitle_names, total = [], [], [0]
    def walk(z, prefix='', depth=0):
        if depth > 8: raise ValueError('내부 ZIP 깊이 제한 초과')
        for item in z.infolist():
            check(); safe_member(item.filename)
            if item.is_dir(): continue
            if item.flag_bits & 1: raise ValueError('암호화 ZIP')
            total[0] += item.file_size
            if total[0] > max_bytes: raise ValueError('내부 ZIP 검사 용량 제한 초과')
            with z.open(item) as f:
                if item.filename.lower().endswith('.zip'):
                    with tempfile.SpooledTemporaryFile(max_size=16*1024**2) as data:
                        while True:
                            check(); block=f.read(1024*1024)
                            if not block: break
                            data.write(block)
                        data.seek(0)
                        with zipfile.ZipFile(data) as inner: walk(inner, prefix + item.filename + '!/', depth + 1)
                else:
                    h = hashlib.sha256(); size = 0
                    for b in iter(lambda: f.read(1024 * 1024), b''):
                        check(); size += len(b); h.update(b)
                    leaves.append((size, h.hexdigest()))
                    if Path(item.filename).suffix.lower() in SUBS: subtitle_names.append(prefix + item.filename)
    with zipfile.ZipFile(path) as z: walk(z)
    if not leaves: return '', subtitle_names
    return hashlib.sha256(json.dumps(sorted(leaves), separators=(',', ':')).encode()).hexdigest(), subtitle_names

def metadata(rel):
    p = PurePosixPath(rel); parts = list(p.parts)
    while parts and parts[0].casefold() in SOURCES: parts.pop(0)
    title = parts[0] if len(parts) > 1 else ''
    episode = next((x for x in parts[1:-1] if re.search(r'\d+\s*(화|회|ep)', x, re.I)), '')
    uploader = '/'.join(parts[2:-1]) if len(parts) > 3 else ''
    season = re.search(r'(?:시즌\s*|s)(\d+)|(\d+)\s*기', title, re.I)
    return title, episode, uploader, season.group(0) if season else ''

def location_score(record):
    title = normal(record['title']); names = normal(record['name'] + ' ' + ' '.join(record.get('inner_names', [])))
    fit = 2 if title and len(title) >= 3 and title in names else 0
    return (fit, -len(record['rel'].split('/')), -len(record['rel']))

class Engine:
    def __init__(self, log=lambda x: None, stop=None):
        self.log = log; self.stop = stop or threading.Event()
    def check(self):
        if self.stop.is_set(): raise Cancelled('중지됨. 원본은 보존됩니다.')
    def save(self, run):
        p = Path(run['run']) / 'manifest.json'; tmp = p.with_suffix('.tmp')
        with open(fs_path(tmp), 'w', encoding='utf-8') as f:
            f.write(json.dumps(run, ensure_ascii=False, indent=2))
        os.replace(fs_path(tmp), fs_path(p))
    def analyze(self, inputs, output, cross=True):
        output = Path(output).resolve()
        # Output may not live under an input folder (avoids recursive self-copy).
        for source, paths in inputs.items():
            if source not in SOURCES: raise ValueError('알 수 없는 소스')
            for entry in paths:
                p = Path(entry).resolve()
                if p.is_dir() and (output == p or p in output.parents): raise ValueError('결과 폴더는 원본 폴더 밖에 지정하세요.')
                if not p.exists(): raise FileNotFoundError(p)
        inputs = {s: [str(f) for entry in paths for f in (
            sorted(Path(entry).glob('*.zip')) if Path(entry).is_dir() and
            list(Path(entry).iterdir()) and all(p.is_file() and re.match(r'(?i)(anissia_subtitles_part\d+|aniall_subtitles_part\d+|naverblog\d+)\.zip$',p.name) for p in Path(entry).iterdir())
            else [Path(entry)])] for s, paths in inputs.items()}
        root = output / ('SubtitleCleanup_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        ensure_dir(root)
        run = {'version': 1, 'run': str(root), 'records': [], 'archives': [], 'cross': cross, 'state': '분석 중'}
        self.save(run)
        try:
            for source in SOURCES:
                paths = inputs.get(source, [])
                if not paths: continue
                target = root / 'cleaned' / source; ensure_dir(target)
                imported_files = []
                origins = {}
                def destination(rel):
                    rel = str(safe_member(rel))
                    dest = target / rel
                    # No overwrite even when a split archive repeats a path.
                    if dest.exists():
                        base = dest; k = 2
                        while dest.exists():
                            dest = base.with_name(base.stem + f'__import{k}' + base.suffix); k += 1
                        rel = dest.relative_to(target).as_posix()
                    ensure_dir(dest.parent)
                    return dest, rel
                for entry in paths:
                    self.check(); p = Path(entry).resolve(); self.log(f'{source}: 가져오기 {p.name}')
                    if p.is_dir():
                        for f in sorted(p.rglob('*')):
                            self.check()
                            if f.is_symlink(): raise ValueError('심볼릭 링크는 가져올 수 없습니다: ' + str(f))
                            if not f.is_file(): continue
                            dest, rel = destination(f.relative_to(p).as_posix()); copy2_file(f, dest)
                            imported_files.append(dest)
                            origins[rel] = {'file': str(f)}
                    else:
                        if p.suffix.lower() != '.zip': raise ValueError('입력은 폴더 또는 독립 ZIP이어야 합니다.')
                        run['archives'].append({'file': str(p), 'sha': sha(p), 'size': p.stat().st_size})
                        with zipfile.ZipFile(p) as z:
                            for index, info in enumerate(z.infolist()):
                                self.check(); member = safe_member(info.filename)
                                mode = info.external_attr >> 16
                                if stat.S_ISLNK(mode): raise ValueError('ZIP 심볼릭 링크는 지원하지 않습니다.')
                                if info.is_dir(): continue
                                if info.file_size > 2 * 1024**3: raise ValueError('단일 파일 압축 해제 제한 2GiB 초과')
                                parts = list(member.parts)
                                if parts and parts[0].casefold() == source: parts.pop(0)
                                dest, rel = destination('/'.join(parts))
                                with z.open(info) as src, open(fs_path(dest), 'wb') as dst:
                                    while True:
                                        self.check(); block = src.read(1024 * 1024)
                                        if not block: break
                                        dst.write(block)
                                imported_files.append(dest)
                                origins[rel] = {'archive': str(p), 'member_index': index, 'member': info.filename}
                files = sorted(imported_files)
                self.log(f'{source}: {len(files):,}개 파일 메타데이터·해시 분석')
                for i, f in enumerate(files):
                    self.check(); rel = f.relative_to(target).as_posix(); title, ep, uploader, season = metadata(rel)
                    r = {'source': source, 'rel': rel, 'name': f.name, 'size': file_size(f),
                         'sha': sha(f), 'title': title, 'episode': ep, 'uploader': uploader, 'season': season,
                         'origin': origins[rel], 'signature': '', 'inner_names': [], 'error': '',
                         'delete': False, 'group': '', 'representative': '', 'reason': '', 'actual': False}
                    run['records'].append(r)
                    if (i+1) % 100 == 0: self.log(f'{source}: {i+1:,}/{len(files):,}개 해시 완료')
                # Calculate archive payload once per raw SHA; do not repeatedly open identical ZIPs.
                cache = {}
                for r in [r for r in run['records'] if not getattr(self, 'deep_mode', False) and r['source'] == source and r['name'].lower().endswith('.zip')]:
                    self.check()
                    if r['sha'] not in cache:
                        try: cache[r['sha']] = (*payload_signature(target / r['rel'], self.check), '')
                        except Cancelled: raise
                        except Exception as e: cache[r['sha']] = ('', [], str(e))
                    r['signature'], r['inner_names'], r['error'] = cache[r['sha']]
                self.log(f'{source}: 내부 ZIP 검사 완료')
                self.save(run)
            self.plan(run); run['state'] = '분석 완료'; self.save(run); report(run)
            return run
        except Exception:
            run['state'] = '분석 미완료'; self.save(run); raise
    def plan(self, run):
        groups = collections.defaultdict(list)
        for r in run['records']:
            if not r['size'] or r['error']: continue
            key = ('zip', r['signature']) if r['signature'] else ('raw', r['sha'])
            if not run['cross']: key += (r['source'],)
            groups[key].append(r)
        count = 0
        for key, members in sorted(groups.items(), key=lambda x: str(x[0])):
            if len(members) < 2: continue
            count += 1; gid = f'DUP-{count:06d}'
            # Normal location score wins; source order breaks ties deterministically.
            winner = sorted(members, key=lambda r: (tuple(-v for v in location_score(r)), SOURCES.index(r['source']), r['rel']))[0]
            representative = winner['source'] + '/' + winner['rel']
            for r in members:
                r['group'] = gid; r['representative'] = representative
                if r is not winner:
                    r['delete'] = True
                    r['reason'] = 'ZIP의 모든 내부 파일 내용·개수 동일 (이름 제외)' if key[0] == 'zip' else 'SHA-256 동일'
                else: r['reason'] = '확정 중복 그룹 대표본 보존'
        # Review candidates: same normalized filename, differing contents. Never deleted by name.
        candidates = collections.defaultdict(list)
        for r in run['records']:
            candidates[('name',normal(Path(r['name']).stem))].append(r)
            if r['title'] and r['episode'] and not common(r['name']):
                candidates[('episode',normal(r['title']),normal(r['episode']))].append(r)
        run['reviews'] = []
        reviewed = set()
        for name, members in candidates.items():
            if not name or len({r['signature'] or r['sha'] for r in members}) < 2: continue
            # Review only alternatives of the same work where a work is known.
            # Repeated generic names (01.smi, font.ttf) across unrelated works are not similarity evidence.
            by_work = collections.defaultdict(list)
            for m in members: by_work[normal(m['title']) or m['source']+'/'+m['rel']].append(m)
            for r in members:
                if not r['delete']:
                    if (r['source'],r['rel']) in reviewed: continue
                    alternatives=by_work[normal(r['title']) or r['source']+'/'+r['rel']]
                    distinct={m['sha']: m for m in alternatives if m['sha']!=r['sha']}
                    if not distinct: continue
                    reviewed.add((r['source'],r['rel']))
                    run['reviews'].append({'path': r['source']+'/'+r['rel'], 'reason': '파일명 유사·내용 다름',
                        'sha': r['sha'], 'others': [m['source']+'/'+m['rel'] for m in distinct.values()], 'result': '수정판·릴·번역자 차이 가능: 보존'})
        for r in run['records']:
            if r['error']:
                run['reviews'].append({'path': r['source']+'/'+r['rel'], 'reason': r['error'], 'sha': r['sha'], 'others': [], 'result': '내부 검증 불가: 보존'})
        self.log(f'확정 중복 삭제 후보 {sum(r["delete"] for r in run["records"]):,}개 / 검토 필요 {len(run["reviews"]):,}건')
    def apply(self, run):
        if run['state'] not in ('분석 완료',): raise ValueError('완료된 분석에만 정리를 적용할 수 있습니다.')
        root = Path(run['run']); records = run['records']
        self.log('정리 전 파일 무결성 확인')
        for a in run['archives']:
            self.check()
            if sha(a['file']) != a['sha']: raise ValueError('입력 ZIP 변경됨: ' + a['file'])
        for r in records:
            self.check(); p = root/'cleaned'/r['source']/r['rel']
            if sha(p) != r['sha']: raise ValueError('작업 파일 변경됨: ' + str(p))
            if 'file' in r['origin'] and sha(r['origin']['file']) != r['sha']: raise ValueError('원본 변경됨: ' + r['origin']['file'])
        # Confirm every representative is scheduled to survive before any unlink.
        keep = {r['source']+'/'+r['rel']: r for r in records if not r['delete']}
        for r in records:
            if r['delete'] and r['representative'] not in keep: raise ValueError('대표본 보존 검증 실패')
        run['state'] = '정리 중'; self.save(run)
        try:
            for i, r in enumerate(records):
                self.check()
                if r['delete']:
                    with open(root/'deletion_journal.jsonl', 'a', encoding='utf-8') as j:
                        j.write(json.dumps({'path': r['source']+'/'+r['rel'], 'intent': 'delete'}, ensure_ascii=False)+'\n'); j.flush(); os.fsync(j.fileno())
                    (root/'cleaned'/r['source']/r['rel']).unlink(); r['actual'] = True
                    if i % 100 == 0: self.save(run)
            self.log('정리 후 전체 파일·대표본 검증')
            for r in records:
                self.check(); p = root/'cleaned'/r['source']/r['rel']
                if r['actual']:
                    if p.exists(): raise ValueError('삭제 로그 불일치')
                elif sha(p) != r['sha']: raise ValueError('보존 파일 검증 실패')
            for source in SOURCES:
                expected = {r['rel']: r for r in records if r['source']==source and not r['actual']}
                folder = root/'cleaned'/source
                if not folder.exists(): continue
                actual = {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
                if actual != set(expected): raise ValueError('정리 후 파일 목록 불일치')
                out = root/(source+'_cleaned.zip'); tmp = out.with_suffix('.zip.part')
                self.log(source + ': 최종 ZIP 생성')
                with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
                    for rel in sorted(expected):
                        self.check(); z.write(folder/rel, source+'/'+rel)
                with zipfile.ZipFile(tmp) as z:
                    if z.testzip(): raise ValueError('최종 ZIP CRC 오류')
                    if len(z.infolist()) != len(expected): raise ValueError('ZIP 파일 수 불일치')
                    for item in z.infolist():
                        self.check(); h = hashlib.sha256()
                        with z.open(item) as f:
                            for b in iter(lambda:f.read(1024*1024), b''): h.update(b)
                        if h.hexdigest()!=expected[item.filename[len(source)+1:]]['sha']: raise ValueError('ZIP SHA 검증 실패')
                tmp.replace(out)
            run['state'] = '정리 완료'; self.save(run); report(run)
            self.log('정리·ZIP·Excel·전체 검증 완료')
        except Exception:
            run['state'] = '정리 미완료'; self.save(run); report(run); raise
    def restore(self, run):
        root = Path(run['run'])
        for r in run['records']:
            self.check()
            if not r['actual'] and not (r['delete'] and not (root/'cleaned'/r['source']/r['rel']).exists()): continue
            p = root/'cleaned'/r['source']/r['rel']
            if p.exists(): raise ValueError('복원 위치에 파일이 이미 있습니다: ' + str(p))
            p.parent.mkdir(parents=True, exist_ok=True); tmp = p.with_name(p.name+'.restore.tmp')
            origin = r['origin']
            if 'file' in origin: shutil.copy2(origin['file'], tmp)
            else:
                with zipfile.ZipFile(origin['archive']) as z:
                    info = z.infolist()[origin['member_index']]
                    if info.filename != origin['member']: raise ValueError('입력 ZIP 구성 변경됨')
                    with z.open(info) as src, open(tmp,'wb') as dst: shutil.copyfileobj(src, dst)
            if sha(tmp) != r['sha']:
                tmp.unlink(); raise ValueError('복원 원본 해시 불일치')
            tmp.replace(p); r['actual'] = False; self.save(run)
        run['state'] = '복원 완료'; self.save(run); report(run)
        self.log('삭제 파일 복원 완료. 기존 cleaned ZIP은 정리 당시 결과입니다.')

def xlsx(path, sheets):
    """Dependency-free Office Open XML report with typed cells, filters and frozen headers."""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    def col(n):
        s=''
        while n: n,r=divmod(n-1,26);s=chr(65+r)+s
        return s
    def clean(s): return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(s))[:32767]
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1))+'</Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,rows) in enumerate(sheets,1))+'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))+f'<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml', f'<styleSheet xmlns="{ns}"><fonts count="2"><font><sz val="11"/><name val="맑은 고딕"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="맑은 고딕"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF234A70"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf fontId="0" fillId="0" borderId="0" xfId="0"/><xf fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"><alignment vertical="center" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        for i,(name,rows) in enumerate(sheets,1):
            ncols=len(rows[0]); end=f'{col(ncols)}{len(rows)}'
            data=[f'<worksheet xmlns="{ns}"><dimension ref="A1:{end}"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>'+''.join(f'<col min="{c}" max="{c}" width="{55 if any(x in str(rows[0][c-1]) for x in ("경로","사유","근거","대상","위치")) else 23}" customWidth="1"/>' for c in range(1,ncols+1))+'</cols><sheetData>']
            for rn,row in enumerate(rows,1):
                cells=[]
                for cn,v in enumerate(row,1):
                    ref=f'{col(cn)}{rn}'; style=' s="1"' if rn==1 else ''
                    if isinstance(v,(int,float)) and not isinstance(v,bool): cells.append(f'<c r="{ref}"{style}><v>{v}</v></c>')
                    else: cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{escape(clean(v))}</t></is></c>')
                data.append(f'<row r="{rn}"'+(' ht="32" customHeight="1"' if rn==1 else '')+'>'+''.join(cells)+'</row>')
            data.append(f'</sheetData><autoFilter ref="A1:{end}"/></worksheet>');z.writestr(f'xl/worksheets/sheet{i}.xml',''.join(data))

def report(run):
    records=run['records']; full=lambda r:r['source']+'/'+r['rel']; groups=collections.defaultdict(list)
    for r in records:
        if r['group']: groups[r['group']].append(r)
    summary=[['항목','값'],['상태',run['state']],['소스 간 중복 정리', '사용' if run['cross'] else '사용 안 함']]
    for source in SOURCES:
        rs=[r for r in records if r['source']==source]; keep=[r for r in rs if not r['actual']]
        summary += [[source+' 원본 파일 수',len(rs)],[source+' 원본 용량(Bytes)',sum(r['size'] for r in rs)], [source+' 현재 파일 수',len(keep)],[source+' 현재 용량(Bytes)',sum(r['size'] for r in keep)]]
    summary += [['확정 중복 그룹 수',len(groups)],['확정 중복 삭제 후보',sum(r['delete'] for r in records)],['실제 삭제 파일 수',sum(r['actual'] for r in records)],['총 절약 용량(Bytes)',sum(r['size'] for r in records if r['actual'])],['검토 필요 수',len(run.get('reviews',[]))],['교차 중복 그룹 수',sum(len({r['source'] for r in rs})>1 for rs in groups.values())],['오배치 자동 삭제','하지 않음: 제목만으로 타 작품을 단정하지 않음'],['원본 보존','입력 폴더·ZIP을 수정하지 않음'],['ZIP 비교 기준','모든 내부 파일의 SHA-256·크기·중복 개수 비교. 파일명은 무시. 부가 파일도 포함.']]
    if run.get('version')==2:
        summary[-1]=['ZIP 비교 기준','내부 ZIP 재귀 해제 후 개별 파일 SHA-256 비교. 부분 중복도 제거. 싱크·문자·인코딩 차이는 보존.']
        summary += [['파일 수·용량 기준','개별 해제 파일 기준. 검증 불가 ZIP은 보존된 ZIP 자체 크기로 계산.'],['절약 용량 기준','최종 결과에서 제거된 개별 파일의 비압축 바이트. 원본·복구용 복사본 보존으로 디스크 사용량 감소를 뜻하지 않음.']]
        for source in SOURCES:
            orig=[r for r in run.get('top_records',[]) if r['source']==source]
            folder=Path(run['run'])/'cleaned'/source
            summary += [[source+' 원래 상위 파일 수',len(orig)],[source+' 원래 상위 용량(Bytes)',sum(r['size'] for r in orig)],
                        [source+' 결과 상위 파일 수',sum(p.is_file() for p in folder.rglob('*')) if folder.exists() else 0],
                        [source+' 결과 실제 용량(Bytes)',sum(p.stat().st_size for p in folder.rglob('*') if p.is_file()) if folder.exists() else 0]]
    deleted=[['소스','원래 전체 경로','작업 경로','파일명','크기(Bytes)','크기(MiB)','SHA-256','분류','삭제 사유','보존된 대표 파일 경로','중복 그룹 ID','실제 삭제 여부']]
    kept=[['소스','최종 경로','파일명','크기(Bytes)','SHA-256','작품명','회차','업로더/번역자/릴','보존 사유','중복 그룹 ID']]
    misplaced=[['소스','현재 작품','현재 회차','원래 경로','파일명','추정 작품','판단 근거','처리 결과']]
    for r in records:
        if r['delete']:
            origin=r['origin']; original=origin.get('file') or origin['archive']+'!/'+origin['member']
            deleted.append([r['source'],original,full(r),r['name'],r['size'],round(r['size']/1024**2,3),r['sha'],'반복 공용 첨부' if common(r['name']) else '확정 중복',r['reason'],r['representative'],r['group'],'삭제' if r['actual'] else '미삭제'])
        if not r['actual']: kept.append([r['source'],full(r),r['name'],r['size'],r['sha'],r['title'],r['episode'],r['uploader'],r['reason'] or ('검증 불가: 보존' if r['error'] else '고유 파일 또는 내용 차이: 보존'),r['group']])
        if r['title'] and r.get('inner_names') and len(normal(r['title']))>=3 and normal(r['title']) not in normal(' '.join(r['inner_names'])+' '+r['name']) and not common(r['name']):
            misplaced.append([r['source'],r['title'],r['episode'],full(r),r['name'],'자동 추정 안 함','현재 작품명이 내부 자막명에 없음. 별칭·영문명 가능','확정 중복으로 삭제' if r['actual'] else '검토 필요: 보존'])
    reviews=[['소스','현재 경로','파일명','비교 대상','의심 사유','SHA-256','내용 비교 결과','자동 삭제하지 않은 이유']]
    for q in run.get('reviews',[]):
        reviews.append([q['path'].split('/')[0],q['path'],q['path'].split('/')[-1],'\n'.join(q['others']),q['reason'],q['sha'],q['result'],'완전 동일 확인 불가'])
    shared=[['파일명','SHA-256','개별 크기(Bytes)','발견 복사본 수','발견 경로','삭제 복사본 수','보존 위치','절약 용량(Bytes)','중복 그룹 ID']]
    cross=[['Anissia 경로','Aniall 경로','Naverblog 경로','작품명','시즌','회차','SHA-256 동일 여부','ZIP 내부 전체 내용 동일 여부','번역자/릴 차이','판정','처리 결과','중복 그룹 ID']]
    for gid,rs in groups.items():
        winner=next(r for r in rs if not r['delete'])
        if any(common(r['name']) for r in rs):
            for r in rs: shared.append([r['name'],r['sha'],r['size'],len(rs),full(r),sum(m['actual'] for m in rs),full(winner),sum(m['size'] for m in rs if m['actual']),gid])
        if len({r['source'] for r in rs})>1:
            for r in rs:
                cross.append([full(r) if r['source']==SOURCES[0] else '',full(r) if r['source']==SOURCES[1] else '',full(r) if r['source']==SOURCES[2] else '',r['title'],r['season'],r['episode'],'동일' if r['sha']==winner['sha'] else '다름','동일' if r['signature'] and r['signature']==winner['signature'] else '해당 없음',r['uploader'],'확정 교차 중복','삭제' if r['actual'] else ('삭제 후보' if r['delete'] else '대표본 보존'),gid])
    # Extension audit intentionally includes every extracted leaf, not only known subtitle types.
    extension_groups=collections.defaultdict(list)
    for r in records:
        ext=Path(r['name']).suffix.lower() or '(없음)'
        extension_groups[ext].append(r)
    extension_rows=[['확장자','파일 수','총 용량(Bytes)','총 용량(MiB)','소스별 파일 수','예시 경로(최대 5개)']]
    for ext,rs in sorted(extension_groups.items(), key=lambda x:(x[0]=='(없음)',x[0])):
        by_source=', '.join(f'{source}: {sum(r["source"]==source for r in rs):,}' for source in SOURCES if any(r['source']==source for r in rs))
        examples='\n'.join(full(r) for r in rs[:5])
        extension_rows.append([ext,len(rs),sum(r['size'] for r in rs),round(sum(r['size'] for r in rs)/1024**2,3),by_source,examples])
    summary += [['발견 확장자 종류 수',len(extension_groups)],['확장자 조사','모든 재귀 해제 파일 포함. 삭제/필터링 전 조사 결과']]
    csv_path=Path(run['run'])/'extension_inventory.csv'
    with open(csv_path,'w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f)
        w.writerows(extension_rows)
    # Long path groups are row-based so no group list is silently truncated in a cell.
    sheets=[('요약',summary),('확장자조사',extension_rows),('삭제내역',deleted),('보존파일',kept),('검토필요',reviews),('오배치',misplaced),('공용첨부',shared),('교차비교',cross)]
    if run.get('version')==2:
        sheets.append(('ZIP재구성',[['원래 ZIP 경로','처리 결과']]+[[x['path'],x['result']] for x in run.get('containers',[])]))
    tmp=Path(run['run'])/'subtitle_cleanup_report.tmp.xlsx'; xlsx(tmp,sheets);tmp.replace(Path(run['run'])/'subtitle_cleanup_report.xlsx')
