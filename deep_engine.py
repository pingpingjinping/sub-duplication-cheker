"""Leaf-level deduplication and reconstruction of arbitrarily nested subtitle ZIPs."""
import collections, csv, hashlib, json, os, re, shutil, stat, subprocess, tempfile, zipfile
from pathlib import Path
from engine import Engine as BaseEngine, SOURCES, SUBS, Cancelled, sha, safe_member, metadata, report, fs_path, file_size, ensure_dir, copy2_file, repair_legacy_zip_name, episode_hint, canonical_work_name

ORIGINAL_ARCHIVE_LIMIT=4*1024**3

class Engine(BaseEngine):
    deep_mode = True

    def analyze(self, inputs, output, cross=True):
        run = super().analyze(inputs, output, cross)
        root=Path(run['run']); (root/'cleaned').rename(root/'original_copy')
        run['top_records']=run['records'];run['records']=[];run['trees']=[]
        run['version']=2;run['state']='작품 폴더별 압축 해제 중';self.save(run)
        counter=[0]
        temp_root=root/'_expand_tmp'
        ensure_dir(temp_root)
        try:
            for top in run['top_records']:
                self.check();p=root/'original_copy'/top['source']/top['rel']
                work=self.work_folder(top['rel'])
                self.log(f'압축 해제: {top["source"]}/{work} ← {top["rel"]}')
                tree=self.expand(run,top,p,top['rel'],[],counter,[0],0,work,temp_root)
                tree['top_source']=top['source'];tree['top_rel']=top['rel'];run['trees'].append(tree)
            self.plan(run);run['state']='분석 완료';self.save(run);report(run)
            return run
        except Exception:
            run['state']='분석 미완료';self.save(run);raise
        finally:
            shutil.rmtree(fs_path(temp_root),ignore_errors=True)

    def work_folder(self, rel):
        parts=Path(rel.replace('\\','/')).parts
        # The selected source directory is the root; its immediate children are work folders.
        return parts[0] if len(parts)>1 else '_root'

    def leaf_destination(self, root, source, work, name):
        folder=root/'expanded'/source/work
        ensure_dir(folder)
        base=folder/Path(name).name
        dest=base;k=2
        while os.path.exists(fs_path(dest)):
            dest=base.with_name(base.stem+f'__{k}'+base.suffix);k+=1
        return dest

    def store_leaf(self, run, top, path, rel, chain, work, error=''):
        root=Path(run['run'])
        name=Path(rel.split('!/')[-1]).name
        dest=self.leaf_destination(root,top['source'],work,name)
        copy2_file(path,dest)
        title,ep,uploader,season=metadata(top['rel'])
        if not ep:
            ep=episode_hint(rel)
        origin=dict(top['origin'])
        if chain:
            original=top['origin'].get('file') or top['origin']['archive']+'!/'+top['origin']['member']
            origin={'archive':original,'member':rel.split('!/',1)[1],'chain':chain}
        r={'source':top['source'],'rel':rel,'name':name,'size':file_size(dest),'sha':sha(dest),
           'title':title,'episode':ep,'uploader':uploader,'season':season,'origin':origin,
           'signature':'','inner_names':[],'error':error,'delete':False,'group':'',
           'representative':'','reason':'','actual':False,'work':str(dest.relative_to(root)),
           'work_folder':work}
        rid=len(run['records']);run['records'].append(r)
        return {'kind':'leaf','record':rid}

    def expand(self,run,top,path,rel,chain,counter,total,depth,work,temp_root):
        self.check();start=len(run['records'])
        if path.suffix.lower()=='.zip':
            try:
                if depth>8:raise ValueError('내부 ZIP 깊이 제한 초과')
                children=[]
                with zipfile.ZipFile(fs_path(path)) as z:
                    for index,item in enumerate(z.infolist()):
                        self.check();member_name=repair_legacy_zip_name(item.filename,item.flag_bits);safe_member(member_name)
                        if item.is_dir():continue
                        if stat.S_ISLNK(item.external_attr>>16):raise ValueError('내부 ZIP 심볼릭 링크')
                        if item.flag_bits&1:raise ValueError('암호화 ZIP')
                        total[0]+=item.file_size
                        if total[0]>2*1024**3:raise ValueError('내부 ZIP 누적 해제 한도 2GiB 초과')
                        counter[0]+=1
                        temp=temp_root/f'{counter[0]:09d}'/Path(member_name).name
                        ensure_dir(temp.parent)
                        with z.open(item) as src,open(fs_path(temp),'wb') as dst:
                            while True:
                                self.check();b=src.read(1024*1024)
                                if not b:break
                                dst.write(b)
                        child=self.expand(run,top,temp,rel+'!/'+member_name,chain+[index],counter,total,depth+1,work,temp_root)
                        child['member']=member_name;children.append(child)
                    if children:
                        return {'kind':'zip','original':str(path),'children':children,'comment':z.comment.hex()}
                return self.store_leaf(run,top,path,rel,chain,work,'빈 ZIP: 보존')
            except Cancelled:raise
            except Exception as e:
                # Failed archives stay as opaque files inside the same work folder.
                del run['records'][start:]
                return self.store_leaf(run,top,path,rel,chain,work,'내부 검증 불가: '+str(e))
        return self.store_leaf(run,top,path,rel,chain,work)
    def detect_extensionless(self, run):
        """Classify extensionless files by signature/content; expand verified ZIPs in-place."""
        if run.get('version')!=2:
            raise ValueError('작품 폴더별 해제 작업에서만 사용할 수 있습니다.')
        if run.get('state')!='분석 완료':
            raise ValueError('압축 해제·확장자 조사를 먼저 완료하세요.')
        root=Path(run['run'])
        targets=[r for r in run['records'] if not Path(r['name']).suffix]
        self.log(f'무확장자 파일 형식 판별 시작: {len(targets):,}개')

        def decode_candidates(data):
            texts=[]
            for enc in ('utf-8-sig','utf-16','cp949','euc-kr','shift_jis'):
                try:t=data.decode(enc)
                except (UnicodeDecodeError,LookupError):continue
                if t not in texts:texts.append(t)
            return texts

        def binary_type(data):
            # Archive/container signatures first.
            if data.startswith(b'7z\xbc\xaf\x27\x1c'):return '.7z','archive','7z 시그니처'
            if data.startswith(b'Rar!\x1a\x07\x00') or data.startswith(b'Rar!\x1a\x07\x01\x00'):return '.rar','archive','RAR 시그니처'
            if data.startswith(b'EGGA'):return '.egg','archive','EGG 시그니처'
            if data.startswith(b'ALZ\x01'):return '.alz','archive','ALZ 시그니처'
            if data.startswith(b'\x1f\x8b'):return '.gz','archive','GZIP 시그니처'
            if data.startswith(b'BZh'):return '.bz2','archive','BZIP2 시그니처'
            if data.startswith(b'\xfd7zXZ\x00'):return '.xz','archive','XZ 시그니처'
            if len(data)>=262 and data[257:262]==b'ustar':return '.tar','archive','TAR ustar 헤더'

            # Common non-subtitle binary attachments.
            if data.startswith(b'\x89PNG\r\n\x1a\n'):return '.png','binary','PNG 시그니처'
            if data.startswith(b'\xff\xd8\xff'):return '.jpg','binary','JPEG 시그니처'
            if data.startswith((b'GIF87a',b'GIF89a')):return '.gif','binary','GIF 시그니처'
            if data.startswith(b'%PDF-'):return '.pdf','binary','PDF 시그니처'
            if data.startswith(b'OTTO'):return '.otf','binary','OpenType 폰트 시그니처'
            if data.startswith((b'\x00\x01\x00\x00',b'true')):return '.ttf','binary','TrueType 폰트 시그니처'
            if data.startswith(b'wOFF'):return '.woff','binary','WOFF 폰트 시그니처'
            if data.startswith(b'wOF2'):return '.woff2','binary','WOFF2 폰트 시그니처'
            return '','',''

        def text_type(data):
            for text in decode_candidates(data):
                head=text[:200000];low=head.casefold()
                stripped=head.lstrip('\ufeff\x00 \t\r\n')
                if stripped.upper().startswith('WEBVTT'):return '.vtt','subtitle','WEBVTT 헤더'
                if re.search(r'<\s*sami\b',low) or re.search(r'<\s*sync\b[^>]*\bstart\s*=',low):
                    return '.smi','subtitle','SAMI/SYNC 태그'
                if '[script info]' in low and '[events]' in low:
                    if '[v4 styles]' in low and '[v4+ styles]' not in low:
                        return '.ssa','subtitle','SSA Script Info/Events'
                    return '.ass','subtitle','ASS Script Info/Events'
                if re.search(r'(?m)^\s*\d+\s*\r?\n\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}',head):
                    return '.srt','subtitle','SRT 번호+타임코드'
                if len(re.findall(r'(?m)^\{\d+\}\{\d+\}',head))>=2:
                    return '.sub','subtitle','MicroDVD 프레임 자막'
                if low.lstrip().startswith('# vobsub index file') or ('timestamp:' in low and 'filepos:' in low):
                    return '.idx','subtitle','VobSub 인덱스'
                if re.search(r'<\s*!doctype\s+html\b|<\s*html\b',low):
                    return '.html','web','HTML 문서'
                if re.match(r'(?is)^\s*https?://',head):
                    return '','web','URL/웹 응답 텍스트'
            return '','','판별 불가'

        def collision_name(old,ext):
            new=old.with_name(old.name+ext);k=2
            while os.path.exists(fs_path(new)):
                new=old.with_name(old.name+f'__{k}'+ext);k+=1
            return new

        rows=[];renamed=0;unknown=0;missing=0;zip_expanded=0;zip_failed=0
        kind_counts=collections.Counter()
        counter=[0];temp_root=root/'_extensionless_zip_tmp';ensure_dir(temp_root)
        try:
            for i,r in enumerate(targets,1):
                self.check();old=root/r['work'];old_work=r['work'];old_display=str(old_work).replace('\\\\','/')
                if not os.path.exists(fs_path(old)):
                    missing+=1
                    rows.append([r['source'],r.get('work_folder',''),old_display,'missing','','',old_display,'파일 없음'])
                    continue
                if sha(old)!=r['sha']:raise ValueError('해제 파일 변경됨: '+str(old))
                size=file_size(old)
                if size==0:
                    ext='';kind='empty';reason='0바이트'
                else:
                    # zipfile.is_zipfile validates ZIP structure even when the filename has no extension.
                    if zipfile.is_zipfile(fs_path(old)):
                        ext='.zip';kind='archive';reason='유효한 ZIP 구조'
                    else:
                        with open(fs_path(old),'rb') as fh:data=fh.read(min(size,2*1024*1024))
                        ext,kind,reason=binary_type(data)
                        if not kind:ext,kind,reason=text_type(data)
                kind=kind or 'unknown';kind_counts[kind]+=1

                if not ext:
                    unknown+=1
                    r['detected_extension']='';r['detected_kind']=kind;r['extension_detection']=reason
                    rows.append([r['source'],r.get('work_folder',''),old_display,kind,'','',old_display,reason])
                    continue

                new=collision_name(old,ext)
                os.replace(fs_path(old),fs_path(new))
                r['name']=new.name;r['work']=str(new.relative_to(root))
                r['detected_extension']=ext;r['detected_kind']=kind;r['extension_detection']=reason
                renamed+=1
                action='확장자 부여'

                # Verified ZIPs are the only newly detected archive type we can safely recurse
                # with the built-in extractor. Other archive formats are labeled and preserved.
                if ext=='.zip':
                    before=len(run['records'])
                    try:
                        with zipfile.ZipFile(fs_path(new)) as z:
                            infos=[x for x in z.infolist() if not x.is_dir()]
                            if not infos:
                                raise ValueError('빈 ZIP')
                            if any(x.flag_bits&1 for x in infos):
                                raise ValueError('암호화 ZIP')
                        tree=self.expand(run,r,new,r['rel'],[],counter,[0],0,r.get('work_folder','_root'),temp_root)
                        added=len(run['records'])-before
                        if tree.get('kind')=='zip' and added>0:
                            zip_expanded+=1;action=f'ZIP 재귀 해제 + {added:,}개 추가'
                            r['archive_extracted']=True
                        else:
                            raise ValueError('해제 가능한 내부 파일 없음')
                    except Cancelled:raise
                    except Exception as e:
                        # Keep only the renamed original archive; do not duplicate it on failure.
                        del run['records'][before:]
                        zip_failed+=1;action='ZIP 보존(해제 불가)'
                        r['archive_extracted']=False
                        r['extension_detection']=reason+' / 해제 보류: '+str(e)

                rows.append([r['source'],r.get('work_folder',''),old_display,kind,ext,action,str(r['work']).replace('\\\\','/'),r['extension_detection']])
                if i%100==0:self.log(f'무확장자 판별: {i:,}/{len(targets):,}')
        finally:
            shutil.rmtree(fs_path(temp_root),ignore_errors=True)

        # Recalculate duplicate candidates after newly extracted ZIP leaves are added.
        for r in run['records']:
            r['delete']=False;r['actual']=False;r['group']='';r['representative']='';r['reason']=''
        self.plan(run)
        run['extensionless_scanned']=True
        run['extensionless_detection']=[
            {'source':x[0],'work_folder':x[1],'old_path':x[2],'kind':x[3],
             'detected_extension':x[4],'action':x[5],'new_path':x[6],'reason':x[7]} for x in rows
        ]
        out=root/'extensionless_detection.csv'
        with open(fs_path(out),'w',encoding='utf-8-sig',newline='') as fh:
            w=csv.writer(fh)
            w.writerow(['소스','작품 폴더','원래 작업 경로','판별 종류','판별 확장자','처리','현재 작업 경로','판별 근거'])
            w.writerows(rows)
        self.save(run);report(run)
        counts=', '.join(f'{k} {v:,}' for k,v in sorted(kind_counts.items()))
        self.log(f'무확장자 판별 완료: {counts} / ZIP 자동 해제 {zip_expanded:,}개 / ZIP 보존 {zip_failed:,}개 / 판별 불가·무확장 {unknown:,}개')
        return run

    def remaining_archive_candidates(self, run):
        """Archives that still need external extraction. Split ZIP/font bundles are intentionally skipped."""
        top_exts={'.7z','.rar','.egg','.alz'}
        out=[]
        for r in run.get('records',[]):
            ext=Path(r.get('name','')).suffix.lower()
            if ext not in top_exts or r.get('actual') or r.get('external_archive_extracted'):
                continue
            if re.search(r'폰트|fonts?',r.get('name',''),re.I):
                r['external_archive_skip']='font'
                continue
            out.append(r)
        return out

    def find_bandizip(self, explicit=None):
        candidates=[]
        if explicit:candidates.append(str(explicit))
        found=shutil.which('bz.exe') or shutil.which('bz')
        if found:candidates.append(found)
        for env in ('ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA'):
            base=os.environ.get(env)
            if base:
                candidates.append(str(Path(base)/'Bandizip'/'bz.exe'))
        seen=set()
        for p in candidates:
            if not p or p in seen:continue
            seen.add(p)
            if os.path.isfile(p):return p
        return ''

    def _bandizip_extract(self, executable, archive, destination):
        executable=self.find_bandizip(executable)
        if not executable:
            raise ValueError('Bandizip bz.exe를 찾지 못했습니다.')
        archive=str(Path(archive).resolve());destination=str(Path(destination).resolve())
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        common={'stdout':subprocess.PIPE,'stderr':subprocess.STDOUT,'text':True,
                'encoding':'utf-8','errors':'replace','creationflags':flags}
        test=subprocess.run([executable,'t','-consolemode:utf8',archive],**common)
        if test.returncode!=0:
            msg=(test.stdout or '').strip()[-1200:]
            raise ValueError('압축 테스트 실패'+(': '+msg if msg else ''))
        extract=subprocess.run([executable,'x','-y','-aoa','-consolemode:utf8',f'-o:{destination}',archive],**common)
        if extract.returncode!=0:
            msg=(extract.stdout or '').strip()[-1200:]
            raise ValueError('압축 해제 실패'+(': '+msg if msg else ''))

    def _register_external_leaf(self, run, parent, source_file, member):
        root=Path(run['run']);work=parent.get('work_folder','_root')
        name=Path(member).name
        dest=self.leaf_destination(root,parent['source'],work,name)
        copy2_file(source_file,dest)
        ep=parent.get('episode','') or episode_hint(member)
        rel=parent.get('rel',parent['name'])+'!/'+member.replace('\\','/')
        r={'source':parent['source'],'rel':rel,'name':dest.name,'size':file_size(dest),'sha':sha(dest),
           'title':parent.get('title',''),'episode':ep,'uploader':parent.get('uploader',''),
           'season':parent.get('season',''),'origin':{'archive':str(root/parent['work']),'member':member},
           'signature':'','inner_names':[],'error':'','delete':False,'group':'',
           'representative':'','reason':'','actual':False,'work':str(dest.relative_to(root)),
           'work_folder':work,'external_archive_depth':parent.get('external_archive_depth',0)+1}
        run['records'].append(r)
        return r

    def extract_remaining_archives(self, run, bandizip=None, extractor=None):
        """Use Bandizip for RAR/7z/EGG/ALZ, flatten leaves into each work folder, and register them."""
        if run.get('version')!=2:
            raise ValueError('작품 폴더별 해제 작업에서만 사용할 수 있습니다.')
        if run.get('state')!='분석 완료':
            raise ValueError('완료된 분석 작업에서만 남은 압축파일을 해제할 수 있습니다.')
        root=Path(run['run'])
        queue=list(self.remaining_archive_candidates(run))
        if not queue:
            self.log('해제할 남은 압축파일이 없습니다.')
            return run
        executable='' if extractor else self.find_bandizip(bandizip)
        if not extractor and not executable:
            raise ValueError('Bandizip bz.exe를 찾지 못했습니다. 설치 경로의 bz.exe를 선택하세요.')

        initial=len(queue);success=0;failed=0;added_total=0;skipped_nested=0
        rows=[];seen=set()
        nested_exts={'.zip','.7z','.rar','.egg','.alz'}
        self.log(f'남은 압축파일 해제 시작: {initial:,}개')
        while queue:
            self.check();r=queue.pop(0)
            work_key=r.get('work','')
            if not work_key or work_key in seen:continue
            seen.add(work_key)
            depth=int(r.get('external_archive_depth',0))
            archive=root/work_key
            display=str(work_key).replace('\\','/')
            if depth>8:
                skipped_nested+=1
                r['external_archive_error']='재귀 깊이 제한 초과'
                rows.append([r['source'],r.get('work_folder',''),display,r['name'],'건너뜀',0,'재귀 깊이 제한 초과'])
                continue
            if re.search(r'폰트|fonts?',r.get('name',''),re.I):
                r['external_archive_skip']='font'
                rows.append([r['source'],r.get('work_folder',''),display,r['name'],'폰트 압축 제외',0,''])
                continue
            if not os.path.exists(fs_path(archive)):
                failed+=1;r['external_archive_error']='파일 없음'
                rows.append([r['source'],r.get('work_folder',''),display,r['name'],'실패',0,'파일 없음'])
                continue
            if sha(archive)!=r['sha']:
                raise ValueError('해제 대상 압축파일이 변경되었습니다: '+str(archive))

            before=len(run['records']);created=[]
            temp=Path(tempfile.mkdtemp(prefix='subtitle-bz-'))
            try:
                if extractor:
                    extractor(archive,temp)
                else:
                    self._bandizip_extract(executable,archive,temp)
                files=sorted((p for p in temp.rglob('*') if p.is_file()),key=lambda p:p.as_posix().casefold())
                if not files:raise ValueError('압축 해제 결과가 비어 있습니다.')
                total=sum(p.stat().st_size for p in files)
                if total>2*1024**3:raise ValueError('압축 해제 결과 2GiB 제한 초과')
                for p in files:
                    self.check()
                    member=p.relative_to(temp).as_posix()
                    nr=self._register_external_leaf(run,r,p,member)
                    created.append(nr)
                r['external_archive_extracted']=True
                r.pop('external_archive_error',None)
                r['external_archive_added']=len(created)
                success+=1;added_total+=len(created)
                rows.append([r['source'],r.get('work_folder',''),display,r['name'],'성공',len(created),''])
                for nr in created:
                    ext=Path(nr['name']).suffix.lower()
                    if ext in nested_exts:
                        if re.search(r'폰트|fonts?',nr['name'],re.I):
                            nr['external_archive_skip']='font';skipped_nested+=1
                        else:
                            queue.append(nr)
                self.log(f'압축 해제: {r["source"]}/{r.get("work_folder","")} / {r["name"]} → {len(created):,}개')
            except Cancelled:
                for nr in created:
                    try:os.unlink(fs_path(root/nr['work']))
                    except FileNotFoundError:pass
                del run['records'][before:]
                raise
            except Exception as e:
                for nr in created:
                    try:os.unlink(fs_path(root/nr['work']))
                    except FileNotFoundError:pass
                del run['records'][before:]
                failed+=1;r['external_archive_error']=str(e)
                rows.append([r['source'],r.get('work_folder',''),display,r['name'],'실패',0,str(e)])
                self.log(f'압축 해제 실패: {r["name"]} / {e}')
            finally:
                shutil.rmtree(fs_path(temp),ignore_errors=True)

        # Recalculate survey groups after newly extracted leaves are registered.
        for r in run['records']:
            r['delete']=False;r['actual']=False;r['group']='';r['representative']='';r['reason']=''
        self.plan(run)
        run['remaining_archive_extraction']=[
            {'source':x[0],'work_folder':x[1],'archive_path':x[2],'archive_name':x[3],
             'result':x[4],'added_files':x[5],'error':x[6]} for x in rows
        ]
        run['remaining_archives_scanned']=True
        out=root/'remaining_archive_extraction.csv'
        with open(fs_path(out),'w',encoding='utf-8-sig',newline='') as fh:
            w=csv.writer(fh)
            w.writerow(['소스','작품 폴더','압축파일 작업 경로','압축파일명','결과','추가 파일 수','오류'])
            w.writerows(rows)
        self.save(run);report(run)
        self.log(f'남은 압축파일 해제 완료: 성공 {success:,}개 / 실패 {failed:,}개 / 추가 파일 {added_total:,}개 / 폰트·깊이 제외 {skipped_nested:,}개')
        return run

    def filename_repair_candidates(self, run):
        """Return surviving flattened records whose mojibake name can be safely recovered as CP949."""
        if run.get('version')!=2:return []
        root=Path(run['run']);out=[]
        for r in run.get('records',[]):
            if r.get('actual'):continue
            old=root/r.get('work','')
            if not os.path.exists(fs_path(old)):continue
            new_name=repair_legacy_zip_name(r.get('name',old.name),0)
            if new_name!=r.get('name',old.name):
                out.append((r,new_name))
        return out

    def repair_broken_filenames(self, run):
        """Repair already-expanded CP949 ZIP member names without rescanning the original sources."""
        if run.get('version')!=2:
            raise ValueError('작품 폴더별 해제 작업에서만 사용할 수 있습니다.')
        if run.get('state') not in ('분석 완료','정리 완료'):
            raise ValueError('분석 완료 또는 평탄화 정리 완료 작업에서만 사용할 수 있습니다.')
        root=Path(run['run']);targets=self.filename_repair_candidates(run)
        rows=[];renamed=0
        for r,new_name in targets:
            self.check();old=root/r['work']
            if sha(old)!=r['sha']:raise ValueError('해제 파일 변경됨: '+str(old))
            dest=old.with_name(new_name);base=dest;k=2
            while os.path.exists(fs_path(dest)) and os.path.normcase(str(dest))!=os.path.normcase(str(old)):
                dest=base.with_name(base.stem+f'__{k}'+base.suffix);k+=1
            os.replace(fs_path(old),fs_path(dest))
            before=r['name'];r['name']=dest.name;r['work']=str(dest.relative_to(root))
            repaired_ep=episode_hint(dest.name)
            if repaired_ep:r['episode']=repaired_ep
            r['filename_repaired_from']=before
            rows.append([r['source'],r.get('work_folder',''),before,dest.name,r.get('episode',''),r['work']])
            renamed+=1
        run['filename_repairs']=[
            {'source':x[0],'work_folder':x[1],'old_name':x[2],'new_name':x[3],'episode':x[4],'work':x[5]} for x in rows
        ]
        out=root/'filename_repair.csv'
        with open(fs_path(out),'w',encoding='utf-8-sig',newline='') as fh:
            w=csv.writer(fh);w.writerow(['소스','작품 폴더','깨진 파일명','복구 파일명','회차 보조값','현재 작업 경로']);w.writerows(rows)
        self.save(run);report(run)
        self.log(f'깨진 ZIP 파일명 복구 완료: {renamed:,}개')
        return run

    def recover_names_from_original(self, run, original_root, source, bandizip=None, extractor=None):
        """Recover flattened filenames by exact content match against the original source tree and archives.

        ZIPs are read directly. RAR/7z/EGG/ALZ, including extensionless files recognized by
        signature, are temporarily extracted with Bandizip (or a test extractor).
        """
        if run.get('version')!=2:
            raise ValueError('작품 폴더별 해제 작업에서만 사용할 수 있습니다.')
        if run.get('state') not in ('분석 완료','정리 완료'):
            raise ValueError('분석 완료 또는 평탄화 정리 완료 작업에서만 사용할 수 있습니다.')
        if source not in SOURCES:
            raise ValueError('알 수 없는 소스입니다: '+str(source))
        original_root=Path(original_root).resolve()
        if not original_root.is_dir():
            raise ValueError('원본 소스 폴더를 선택하세요.')

        root=Path(run['run'])
        targets=[r for r in run.get('records',[]) if r.get('source')==source and not r.get('actual')]
        if not targets:
            raise ValueError('선택한 소스의 현재 expanded 파일이 없습니다.')

        wanted=collections.defaultdict(set)
        current=collections.defaultdict(list)
        for r in targets:
            work=r.get('work_folder','_root')
            wanted[work].add(int(r.get('size',0)))
            current[(work,int(r.get('size',0)),r.get('sha',''))].append(r)

        candidates=collections.defaultdict(set)
        scan_errors=[]
        loose_checked=0;zip_checked=0;external_checked=0;archive_leaf_checked=0
        executable='' if extractor else self.find_bandizip(bandizip)
        external_exts={'.7z','.rar','.egg','.alz'}
        self.log(f'원본 비교 이름 복구: {source} / 현재 파일 {len(targets):,}개')

        def remember(work,size,digest,name,origin):
            if (work,size,digest) not in current:
                return
            base=Path(name).name
            if not base:
                return
            candidates[(work,size,digest)].add((base,origin))

        def hash_stream(fh):
            h=hashlib.sha256()
            while True:
                self.check();block=fh.read(1024*1024)
                if not block:break
                h.update(block)
            return h.hexdigest()

        def signature_kind(data):
            if data.startswith(b'7z\xbc\xaf\x27\x1c'):return '.7z'
            if data.startswith(b'Rar!\x1a\x07\x00') or data.startswith(b'Rar!\x1a\x07\x01\x00'):return '.rar'
            if data.startswith(b'EGGA'):return '.egg'
            if data.startswith(b'ALZ\x01'):return '.alz'
            return ''

        def disk_archive_kind(path):
            suffix=Path(path).suffix.lower()
            if zipfile.is_zipfile(fs_path(path)):
                return '.zip'
            if suffix in external_exts:
                return suffix
            if not suffix:
                try:
                    with open(fs_path(path),'rb') as fh:
                        return signature_kind(fh.read(16))
                except OSError:
                    return ''
            return ''

        def extract_external(archive,destination):
            if extractor:
                extractor(archive,destination)
                return
            if not executable:
                raise ValueError('원본의 RAR/7z/EGG/ALZ 내부 이름 비교에 Bandizip bz.exe가 필요합니다.')
            self._bandizip_extract(executable,archive,destination)

        def walk_external(archive,work,origin,depth=0,budget=None):
            nonlocal external_checked,archive_leaf_checked
            if budget is None:budget=[0]
            if depth>8:
                raise ValueError('중첩 압축 깊이 제한 초과')
            external_checked+=1
            temp=Path(tempfile.mkdtemp(prefix='subtitle-origin-archive-'))
            try:
                extract_external(archive,temp)
                files=sorted((p for p in temp.rglob('*') if p.is_file()),key=lambda p:p.as_posix().casefold())
                for p in files:
                    self.check()
                    member=p.relative_to(temp).as_posix()
                    size=file_size(p)
                    budget[0]+=size
                    if budget[0]>ORIGINAL_ARCHIVE_LIMIT:
                        raise ValueError('원본 압축 비교 개별 압축 4GiB 제한 초과')
                    kind=disk_archive_kind(p)
                    if kind=='.zip':
                        with zipfile.ZipFile(fs_path(p)) as z:
                            walk_zip(z,work,origin+'!/'+member,depth+1,budget)
                        continue
                    if kind in external_exts:
                        walk_external(p,work,origin+'!/'+member,depth+1,budget)
                        continue
                    if size not in wanted.get(work,set()):
                        continue
                    digest=sha(p);archive_leaf_checked+=1
                    remember(work,size,digest,Path(member).name,origin+'!/'+member)
            finally:
                shutil.rmtree(fs_path(temp),ignore_errors=True)

        def walk_zip(z,work,origin,depth=0,budget=None):
            nonlocal zip_checked,archive_leaf_checked
            if budget is None:budget=[0]
            if depth>8:
                raise ValueError('중첩 압축 깊이 제한 초과')
            zip_checked+=1
            for info in z.infolist():
                self.check()
                member_name=repair_legacy_zip_name(info.filename,info.flag_bits)
                safe_member(member_name)
                if info.is_dir():continue
                if stat.S_ISLNK(info.external_attr>>16) or info.flag_bits&1:
                    continue
                budget[0]+=info.file_size
                if budget[0]>ORIGINAL_ARCHIVE_LIMIT:
                    raise ValueError('원본 압축 비교 개별 압축 4GiB 제한 초과')
                suffix=Path(member_name).suffix.lower()
                with z.open(info) as src:
                    if suffix=='.zip':
                        with tempfile.SpooledTemporaryFile(max_size=32*1024**2) as data:
                            while True:
                                self.check();block=src.read(1024*1024)
                                if not block:break
                                data.write(block)
                            data.seek(0)
                            try:
                                with zipfile.ZipFile(data) as inner:
                                    walk_zip(inner,work,origin+'!/'+member_name,depth+1,budget)
                            except (zipfile.BadZipFile,OSError):
                                pass
                        continue

                    # External archive with an extension, or an extensionless member whose
                    # signature says RAR/7z/EGG/ALZ: materialize only that member temporarily.
                    if suffix in external_exts or not suffix:
                        with tempfile.SpooledTemporaryFile(max_size=32*1024**2) as data:
                            while True:
                                self.check();block=src.read(1024*1024)
                                if not block:break
                                data.write(block)
                            data.seek(0)
                            head=data.read(16);data.seek(0)
                            external_kind=suffix if suffix in external_exts else signature_kind(head)
                            if external_kind:
                                temp=Path(tempfile.mkdtemp(prefix='subtitle-origin-member-'))
                                try:
                                    archive=temp/('member'+external_kind)
                                    with open(fs_path(archive),'wb') as dst:
                                        shutil.copyfileobj(data,dst)
                                    walk_external(archive,work,origin+'!/'+member_name,depth+1,budget)
                                finally:
                                    shutil.rmtree(fs_path(temp),ignore_errors=True)
                                continue
                            if info.file_size in wanted.get(work,set()):
                                digest=hash_stream(data);archive_leaf_checked+=1
                                remember(work,info.file_size,digest,member_name,origin+'!/'+member_name)
                        continue

                    if info.file_size not in wanted.get(work,set()):
                        continue
                    digest=hash_stream(src);archive_leaf_checked+=1
                    remember(work,info.file_size,digest,member_name,origin+'!/'+member_name)

        # Same rule as analysis: the source root's immediate children are work folders.
        for p in sorted(original_root.rglob('*'),key=lambda x:x.as_posix().casefold()):
            self.check()
            if p.is_symlink() or not p.is_file():
                continue
            rel=p.relative_to(original_root)
            work=rel.parts[0] if len(rel.parts)>1 else '_root'
            if work not in wanted:
                continue
            try:
                kind=disk_archive_kind(p)
                if kind=='.zip':
                    with zipfile.ZipFile(fs_path(p)) as z:
                        walk_zip(z,work,str(rel).replace('\\','/'),0,[0])
                    continue
                if kind in external_exts:
                    walk_external(p,work,str(rel).replace('\\','/'),0,[0])
                    continue
                size=file_size(p)
                if size not in wanted[work]:
                    continue
                loose_checked+=1
                remember(work,size,sha(p),p.name,str(rel).replace('\\','/'))
            except Cancelled:
                raise
            except Exception as e:
                scan_errors.append({'path':str(rel).replace('\\','/'),'error':str(e)})

        rows=[];renamed=0;already=0;ambiguous=0;missing=0
        for r in targets:
            self.check()
            key=(r.get('work_folder','_root'),int(r.get('size',0)),r.get('sha',''))
            found=candidates.get(key,set())
            names=sorted({name for name,_ in found},key=str.casefold)
            origins=sorted({origin for _,origin in found},key=str.casefold)
            old=root/r['work']
            if len(names)==0:
                missing+=1
                rows.append([source,r.get('work_folder',''),r['name'],'','미매칭','',r.get('sha','')])
                continue
            if len(names)>1:
                ambiguous+=1
                rows.append([source,r.get('work_folder',''),r['name'],' | '.join(names),'복수 원본명: 보존',' | '.join(origins),r.get('sha','')])
                continue
            original_desired=names[0]
            desired=original_desired
            # If the original source itself had no extension, do not undo an extension that
            # was already identified from subtitle content during the extensionless scan.
            subtitle_exts=set(SUBS)|{'.jmk'}
            detected_ext=str(r.get('detected_extension','')).lower()
            current_ext=Path(r.get('name','')).suffix.lower()
            keep_ext=detected_ext if detected_ext in subtitle_exts else (current_ext if current_ext in subtitle_exts else '')
            if not Path(desired).suffix and keep_ext:
                desired=desired+keep_ext
            if desired==r['name']:
                already+=1
                result='이미 동일'
                if desired!=original_desired:
                    result=f'이미 동일 (원본 무확장, 판별 확장자 {keep_ext} 유지)'
                rows.append([source,r.get('work_folder',''),r['name'],original_desired,result,' | '.join(origins),r.get('sha','')])
                continue
            if not os.path.exists(fs_path(old)) or sha(old)!=r['sha']:
                raise ValueError('expanded 파일이 변경되었습니다: '+str(old))
            dest=old.with_name(desired);base=dest;k=2
            while os.path.exists(fs_path(dest)) and os.path.normcase(str(dest))!=os.path.normcase(str(old)):
                dest=base.with_name(base.stem+f'__{k}'+base.suffix);k+=1
            os.replace(fs_path(old),fs_path(dest))
            before=r['name'];r['name']=dest.name;r['work']=str(dest.relative_to(root))
            if '/' in r.get('rel',''):
                r['rel']=r['rel'].rsplit('/',1)[0]+'/'+dest.name
            else:
                r['rel']=dest.name
            hint=episode_hint(dest.name)
            if hint:r['episode']=hint
            r['original_name_recovered_from']=before
            r['original_name_sources']=origins
            renamed+=1
            result='복구'
            if desired!=original_desired:
                result=f'복구 (원본 무확장, 판별 확장자 {keep_ext} 유지)'
            rows.append([source,r.get('work_folder',''),before,original_desired,result,' | '.join(origins),r.get('sha','')])

        run['original_name_recovery']=[
            {'source':x[0],'work_folder':x[1],'old_name':x[2],'original_name':x[3],
             'result':x[4],'original_path':x[5],'sha':x[6]} for x in rows
        ]
        run['original_name_recovery_source']=str(original_root)
        run['original_name_recovery_errors']=scan_errors
        run['original_name_recovery_archive_stats']={
            'zip_archives':zip_checked,'external_archives':external_checked,
            'archive_leaf_candidates':archive_leaf_checked,'loose_files':loose_checked,
            'errors':len(scan_errors)
        }
        out=root/'original_name_recovery.csv'
        with open(fs_path(out),'w',encoding='utf-8-sig',newline='') as fh:
            w=csv.writer(fh)
            w.writerow(['소스','작품 폴더','현재 파일명','원본 파일명','처리','원본 위치','SHA-256'])
            w.writerows(rows)
        self.save(run);report(run)
        self.log(
            f'원본 비교 이름 복구 완료: 복구 {renamed:,}개 / 이미 동일 {already:,}개 / '
            f'복수 후보 {ambiguous:,}개 / 미매칭 {missing:,}개 / '
            f'원본 일반파일 {loose_checked:,}개·ZIP {zip_checked:,}개·RAR/7z/EGG/ALZ {external_checked:,}개·'
            f'압축 내부 후보 {archive_leaf_checked:,}개 / 오류 {len(scan_errors):,}건'
        )
        return run

    def cross_source_cleanup(self, inputs, output):
        """Create a safe final copy and remove exact cross-source duplicates only for matched works."""
        output=Path(output).resolve()
        active={s:[Path(p).resolve() for p in inputs.get(s,[]) if str(p)] for s in SOURCES}
        active={s:ps for s,ps in active.items() if ps}
        if len(active)<2:raise ValueError('교차 중복 정리는 최소 2개 소스 폴더가 필요합니다.')
        if any(len(paths)!=1 for paths in active.values()):
            raise ValueError('세 소스 최종 교차중복은 소스별로 정리 끝난 폴더를 하나씩 선택하세요.')
        for source,paths in active.items():
            for p in paths:
                if not p.is_dir():raise ValueError(f'{source}: 폴더를 선택하세요: {p}')
                if output==p or p in output.parents:raise ValueError('결과 폴더는 입력 폴더 밖에 지정하세요.')

        from datetime import datetime
        root=output/('CrossSourceCleanup_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        ensure_dir(root)
        run={'version':3,'run':str(root),'records':[],'archives':[],'cross':True,
             'cleanup_mode':'cross_source_final','state':'교차 중복 분석 중','reviews':[],
             'source_priority':list(SOURCES)}
        self.save(run)
        allowed=set(SUBS)|{'.jmk'}
        try:
            for source in SOURCES:
                paths=active.get(source,[])
                if not paths:continue
                target=root/'cross_cleaned'/source;ensure_dir(target)
                for selected in paths:
                    base=(selected/source) if (selected/source).is_dir() else selected
                    files=sorted(p for p in base.rglob('*') if p.is_file())
                    self.log(f'{source}: 최종 정리본 {len(files):,}개 복사·SHA 계산')
                    for i,p in enumerate(files,1):
                        self.check()
                        if p.is_symlink():raise ValueError('심볼릭 링크는 가져올 수 없습니다: '+str(p))
                        rel=p.relative_to(base).as_posix();parts=Path(rel).parts
                        work=parts[0] if len(parts)>1 else '_root'
                        dest=target/rel;ensure_dir(dest.parent);copy2_file(p,dest)
                        title=work if work!='_root' else ''
                        ep=episode_hint(p.name)
                        r={'source':source,'rel':rel,'name':p.name,'size':file_size(dest),'sha':sha(dest),
                           'title':title,'episode':ep,'uploader':'','season':'','origin':{'file':str(p)},
                           'signature':'','inner_names':[],'error':'','delete':False,'group':'',
                           'representative':'','reason':'','actual':False,
                           'work':str(dest.relative_to(root)),'work_folder':work,
                           'work_key':canonical_work_name(work),'cross_eligible':p.suffix.lower() in allowed}
                        run['records'].append(r)
                        if i%1000==0:self.log(f'{source}: {i:,}/{len(files):,}개')
                self.save(run)

            # Exact SHA is mandatory; work folders must also match after conservative normalization.
            groups=collections.defaultdict(list)
            by_sha=collections.defaultdict(list)
            for r in run['records']:
                if not r['cross_eligible'] or not r['size']:continue
                groups[(r['work_key'],r['sha'])].append(r);by_sha[r['sha']].append(r)

            gid=0
            for (work_key,digest),members in sorted(groups.items(),key=lambda x:str(x[0])):
                sources={r['source'] for r in members}
                if not work_key or len(sources)<2:continue
                gid+=1;group=f'CROSS-{gid:06d}'
                winner=sorted(members,key=lambda r:(SOURCES.index(r['source']),r['rel'].casefold()))[0]
                rep=winner['source']+'/'+winner['rel']
                for r in members:
                    r['group']=group;r['representative']=rep
                    if r is winner:r['reason']='교차 소스 동일 SHA 대표본 보존'
                    else:r['delete']=True;r['reason']='매칭 작품의 교차 소스 SHA-256 동일'

            # Same bytes across differently named works are surfaced, never auto-deleted.
            reviews=[];seen=set()
            for digest,members in by_sha.items():
                if len({r['source'] for r in members})<2:continue
                keys={r['work_key'] for r in members if r['work_key']}
                if len(keys)<=1:continue
                token=(digest,tuple(sorted(keys)))
                if token in seen:continue
                seen.add(token)
                reviews.append({'sha':digest,'reason':'SHA 동일하지만 작품 폴더명이 달라 자동 삭제 안 함',
                                'paths':[r['source']+'/'+r['rel'] for r in members],
                                'works':[r.get('work_folder','') for r in members]})
            run['cross_source_review']=reviews
            run['state']='교차 정리 중';self.save(run)

            # Verify the copy first, then delete only the planned duplicates from the safe copy.
            for r in run['records']:
                self.check();p=root/r['work']
                if sha(p)!=r['sha']:raise ValueError('교차 정리 작업본 변경됨: '+str(p))
            journal=root/'cross_deletion_journal.jsonl'
            for i,r in enumerate(run['records'],1):
                self.check()
                if not r['delete']:continue
                p=root/r['work']
                with open(fs_path(journal),'a',encoding='utf-8') as j:
                    j.write(json.dumps({'path':r['work'],'reason':r['reason'],'representative':r['representative']},ensure_ascii=False)+'\n')
                    j.flush();os.fsync(j.fileno())
                os.unlink(fs_path(p));r['actual']=True
                if i%250==0:self.save(run)

            # Verify surviving copy and original inputs.
            for r in run['records']:
                self.check();p=root/r['work']
                if r['actual']:
                    if p.exists():raise ValueError('교차 중복 삭제 검증 실패: '+str(p))
                elif sha(p)!=r['sha']:raise ValueError('교차 중복 보존 파일 SHA 불일치: '+str(p))
                if sha(r['origin']['file'])!=r['sha']:raise ValueError('입력 원본 변경됨: '+r['origin']['file'])

            rows=[]
            for r in run['records']:
                if r['group']:
                    rows.append([r['group'],r['source'],r['work_folder'],r['name'],r['sha'],
                                 '삭제' if r['actual'] else '대표본 보존',r['representative']])
            with open(fs_path(root/'cross_source_cleanup.csv'),'w',encoding='utf-8-sig',newline='') as fh:
                w=csv.writer(fh);w.writerow(['그룹','소스','작품 폴더','파일명','SHA-256','처리','대표본']);w.writerows(rows)
            with open(fs_path(root/'cross_source_review.csv'),'w',encoding='utf-8-sig',newline='') as fh:
                w=csv.writer(fh);w.writerow(['SHA-256','사유','작품 폴더','경로'])
                for q in reviews:w.writerow([q['sha'],q['reason'],' | '.join(q['works']),' | '.join(q['paths'])])
            run['state']='교차 정리 완료';self.save(run);report(run)
            self.log(f'세 소스 교차 중복 정리 완료: 삭제 {sum(r["actual"] for r in run["records"]):,}개 / 검토 {len(reviews):,}그룹')
            return run
        except Exception:
            run['state']='교차 정리 미완료';self.save(run)
            try:report(run)
            except Exception:pass
            raise

    def clean_flat(self, run, extensions):
        """Filter the flattened expanded tree and remove exact duplicates per work folder."""
        if run.get('version')!=2:
            raise ValueError('작품 폴더별 해제 작업에서만 사용할 수 있습니다.')
        if run.get('state')!='분석 완료':
            raise ValueError('압축 해제·확장자 조사를 먼저 완료하세요.')
        selected={str(x).lower() for x in extensions}
        if not selected:
            raise ValueError('남길 확장자를 하나 이상 선택하세요.')
        root=Path(run['run'])
        self.log('선택 확장자 적용·작품 폴더별 SHA 중복 계산')

        # The earlier survey plan may contain global duplicate candidates.
        # Final cleanup intentionally recalculates only within each immediate work folder.
        for r in run['records']:
            r['delete']=False;r['actual']=False;r['group']='';r['representative']='';r['reason']=''

        keep_candidates=[]
        for r in run['records']:
            ext=Path(r['name']).suffix.lower() or '(없음)'
            if ext not in selected:
                r['delete']=True
                r['reason']='선택되지 않은 확장자 제거'
            else:
                keep_candidates.append(r)

        groups=collections.defaultdict(list)
        for r in keep_candidates:
            groups[(r['source'],r.get('work_folder','_root'),r['sha'])].append(r)

        duplicate_groups=0
        for (_,_,_), members in sorted(groups.items(), key=lambda x: str(x[0])):
            if len(members)<2:continue
            duplicate_groups+=1;gid=f'FOLDER-DUP-{duplicate_groups:06d}'
            winner=sorted(members,key=lambda r:r['work'])[0]
            representative=winner['work']
            for r in members:
                r['group']=gid;r['representative']=representative
                if r is winner:
                    r['reason']='작품 폴더 내 동일 SHA 대표본 보존'
                else:
                    r['delete']=True;r['reason']='같은 작품 폴더 내 SHA-256 동일'

        # Verify the flattened survey copy before deleting anything.
        # A changed non-selected attachment is still going to be removed by explicit
        # extension filtering, so it must not block subtitle cleanup. Kept files and
        # SHA-based duplicate deletions remain strict.
        changed_unselected=0
        for r in run['records']:
            self.check();p=root/r['work']
            if not os.path.exists(fs_path(p)):
                raise ValueError('해제 파일이 없습니다: '+str(p))
            actual_sha=sha(p)
            if actual_sha!=r['sha']:
                if r['reason']=='선택되지 않은 확장자 제거':
                    changed_unselected+=1
                    r['sha_before_cleanup']=r['sha']
                    r['sha']=actual_sha
                    r['size']=file_size(p)
                    r['reason']='선택되지 않은 확장자 제거 (작업본 내용 변경 감지)'
                    continue
                raise ValueError('해제 파일 변경됨: '+str(p))
        if changed_unselected:
            self.log(f'비선택 확장자 작업본 변경 감지: {changed_unselected:,}개 / 어차피 제거 대상이라 계속 진행')

        run['selected_extensions']=sorted(selected)
        run['cleanup_mode']='flat_whitelist'
        run['state']='정리 중';self.save(run)
        journal=root/'deletion_journal.jsonl'
        try:
            for i,r in enumerate(run['records']):
                self.check()
                if not r['delete']:continue
                p=root/r['work']
                with open(fs_path(journal),'a',encoding='utf-8') as j:
                    j.write(json.dumps({'path':r['work'],'reason':r['reason']},ensure_ascii=False)+'\\n')
                    j.flush();os.fsync(j.fileno())
                os.unlink(fs_path(p));r['actual']=True
                if (i+1)%100==0:self.save(run)

            # Empty work folders may remain. Avoid a second recursive filesystem walk here:
            # very long Windows paths are already represented safely by the manifest.

            for r in run['records']:
                self.check();p=root/r['work']
                exists=os.path.exists(fs_path(p))
                if r['actual']:
                    if exists:raise ValueError('삭제 대상이 남아 있습니다: '+str(p))
                else:
                    if not exists or sha(p)!=r['sha']:
                        raise ValueError('보존 파일 검증 실패: '+str(p))

            run['state']='정리 완료';self.save(run);report(run)
            kept=sum(not r['actual'] for r in run['records'])
            removed=sum(r['actual'] for r in run['records'])
            self.log(f'정리 완료: 보존 {kept:,}개 / 제거 {removed:,}개 / ZIP 재생성 없음')
            return run
        except Exception:
            run['state']='정리 미완료';self.save(run);report(run);raise

    def materialize(self,run,node,destination):
        """False means no surviving leaf; empty original ZIPs are opaque kept leaves."""
        self.check();root=Path(run['run'])
        if node['kind']=='leaf':
            r=run['records'][node['record']]
            if r['delete']:return False
            ensure_dir(destination.parent);copy2_file(root/r['work'],destination);return True
        # Rebuild children on disk instead of storing large archive payloads in RAM.
        temporary=Path(tempfile.mkdtemp(prefix='subtitle-members-',dir=root));members=[]
        try:
            for i,child in enumerate(node['children']):
                p=temporary/f'{i:09d}'
                if self.materialize(run,child,p):members.append((child['member'],p))
            if not members:return False
            ensure_dir(destination.parent)
            with zipfile.ZipFile(fs_path(destination),'w',zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
                for name,p in members:self.check();z.write(p,name)
                z.comment=bytes.fromhex(node.get('comment',''))
            return True
        finally:shutil.rmtree(temporary)

    def verify_node(self,run,node,file):
        self.check()
        if node['kind']=='leaf':
            r=run['records'][node['record']]
            if r['delete']:raise ValueError('삭제 대상이 결과에 남음')
            if sha(file)!=r['sha']:raise ValueError('보존 자막 SHA 불일치: '+r['rel'])
            return
        children=[c for c in node['children'] if self.survives(run,c)]
        with zipfile.ZipFile(fs_path(file)) as z:
            infos=z.infolist()
            if len(infos)!=len(children):raise ValueError('내부 ZIP 개수 불일치')
            for child,info in zip(children,infos):
                if child['member']!=info.filename:raise ValueError('내부 ZIP 경로 불일치')
                import tempfile
                with tempfile.TemporaryDirectory() as d:
                    p=Path(d)/'verify'
                    with z.open(info) as src,open(fs_path(p),'wb') as dst:shutil.copyfileobj(src,dst)
                    self.verify_node(run,child,p)

    def survives(self,run,node):
        if node['kind']=='leaf':return not run['records'][node['record']]['delete']
        return any(self.survives(run,c) for c in node['children'])

    def apply(self,run):
        if run.get('version')!=2:return super().apply(run)
        if run['state']!='분석 완료':raise ValueError('완료된 분석이 필요합니다.')
        root=Path(run['run']);keep={r['source']+'/'+r['rel'] for r in run['records'] if not r['delete']}
        self.log('원본·해제 파일·대표본 무결성 확인')
        for a in run['archives']:
            self.check()
            if sha(a['file'])!=a['sha']:raise ValueError('입력 ZIP 변경됨')
        for top in run['top_records']:
            self.check()
            if sha(root/'original_copy'/top['source']/top['rel'])!=top['sha']:raise ValueError('작업 원본 변경됨')
            if 'file' in top['origin'] and sha(top['origin']['file'])!=top['sha']:raise ValueError('입력 원본 변경됨')
        for r in run['records']:
            self.check()
            if sha(root/r['work'])!=r['sha']:raise ValueError('해제 파일 변경됨')
            if r['delete'] and r['representative'] not in keep:raise ValueError('대표본이 남지 않는 중복 그룹')
        stage=root/'rebuild';stage.mkdir();run['state']='정리 중';self.save(run)
        try:
            for source in SOURCES:
                if (root/'original_copy'/source).exists():(stage/source).mkdir()
            for tree in run['trees']:
                self.check();dest=stage/tree['top_source']/tree['top_rel']
                if self.materialize(run,tree,dest):self.verify_node(run,tree,dest)
            self.log('원래 ZIP 구조 재구성·내부 자막 SHA 검증 완료')
            run['containers']=[]
            def container_log(node,path):
                if node['kind']!='zip':return
                run['containers'].append({'path':path,'result':'보존/재구성' if self.survives(run,node) else '모든 내부 파일 중복: 빈 ZIP 제거'})
                for child in node['children']:container_log(child,path+'!/'+child['member'])
            for tree in run['trees']:container_log(tree,tree['top_source']+'/'+tree['top_rel'])
            for source in SOURCES:
                folder=stage/source
                if not folder.exists():continue
                out=stage/(source+'_cleaned.zip')
                with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
                    for p in sorted(folder.rglob('*')):
                        self.check()
                        if p.is_file():z.write(p,source+'/'+p.relative_to(folder).as_posix())
                with zipfile.ZipFile(out) as z:
                    if z.testzip():raise ValueError('최종 ZIP 손상')
                    expected={source+'/'+p.relative_to(folder).as_posix():p for p in folder.rglob('*') if p.is_file()}
                    if set(z.namelist())!=set(expected):raise ValueError('최종 ZIP 목록 불일치')
                    for info in z.infolist():
                        self.check();h=hashlib.sha256()
                        with z.open(info) as f:
                            for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
                        if h.hexdigest()!=sha(expected[info.filename]):raise ValueError('최종 ZIP SHA 불일치')
            # Publish only fully reconstructed and verified copies. Original-copy and expanded
            # data stay available for recovery; no source file is ever unlinked.
            for p in list(stage.glob('*_cleaned.zip')):p.replace(root/p.name)
            stage.rename(root/'cleaned')
            for r in run['records']:r['actual']=r['delete']
            run['state']='정리 완료';self.save(run);report(run);self.log('자동 정리·Excel·ZIP 생성 완료')
        except Exception:
            run['state']='정리 미완료';self.save(run);report(run);raise

    def restore(self,run):
        if run.get('version')==3:
            raise ValueError('교차 정리본은 입력 원본을 수정하지 않았습니다. 복원이 필요하지 않습니다.')
        if run.get('version')!=2:return super().restore(run)
        root=Path(run['run']);destination=root/'restored'
        if destination.exists():raise ValueError('restored 폴더가 이미 있습니다. 기존 복원본을 확인하세요.')
        shutil.copytree(root/'original_copy',destination)
        self.log('원래 구조 복원 완료: '+str(destination))
        # Keep cleaned/report deletion facts intact; restored is a separate original-state copy.
        run['restore_path']=str(destination);self.save(run)
