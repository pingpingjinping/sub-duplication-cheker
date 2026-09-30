"""Leaf-level deduplication and reconstruction of arbitrarily nested subtitle ZIPs."""
import collections, hashlib, json, os, shutil, stat, tempfile, zipfile
from pathlib import Path
from engine import Engine as BaseEngine, SOURCES, Cancelled, sha, safe_member, metadata, report, fs_path, file_size, ensure_dir, copy2_file

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
        parts=Path(rel.replace('\\\\','/')).parts
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
            import re
            m=re.search(r'(\\d+)\\s*(?:화|회)|(?:ep|episode)[ ._-]*(\\d+)',rel,re.I)
            ep=m.group(0) if m else ''
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
                        self.check();safe_member(item.filename)
                        if item.is_dir():continue
                        if stat.S_ISLNK(item.external_attr>>16):raise ValueError('내부 ZIP 심볼릭 링크')
                        if item.flag_bits&1:raise ValueError('암호화 ZIP')
                        total[0]+=item.file_size
                        if total[0]>2*1024**3:raise ValueError('내부 ZIP 누적 해제 한도 2GiB 초과')
                        counter[0]+=1
                        temp=temp_root/f'{counter[0]:09d}'/Path(item.filename).name
                        ensure_dir(temp.parent)
                        with z.open(item) as src,open(fs_path(temp),'wb') as dst:
                            while True:
                                self.check();b=src.read(1024*1024)
                                if not b:break
                                dst.write(b)
                        child=self.expand(run,top,temp,rel+'!/'+item.filename,chain+[index],counter,total,depth+1,work,temp_root)
                        child['member']=item.filename;children.append(child)
                    if children:
                        return {'kind':'zip','original':str(path),'children':children,'comment':z.comment.hex()}
                return self.store_leaf(run,top,path,rel,chain,work,'빈 ZIP: 보존')
            except Cancelled:raise
            except Exception as e:
                # Failed archives stay as opaque files inside the same work folder.
                del run['records'][start:]
                return self.store_leaf(run,top,path,rel,chain,work,'내부 검증 불가: '+str(e))
        return self.store_leaf(run,top,path,rel,chain,work)
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
        for r in run['records']:
            self.check();p=root/r['work']
            if not os.path.exists(fs_path(p)):
                raise ValueError('해제 파일이 없습니다: '+str(p))
            if sha(p)!=r['sha']:
                raise ValueError('해제 파일 변경됨: '+str(p))

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
        if run.get('version')!=2:return super().restore(run)
        root=Path(run['run']);destination=root/'restored'
        if destination.exists():raise ValueError('restored 폴더가 이미 있습니다. 기존 복원본을 확인하세요.')
        shutil.copytree(root/'original_copy',destination)
        self.log('원래 구조 복원 완료: '+str(destination))
        # Keep cleaned/report deletion facts intact; restored is a separate original-state copy.
        run['restore_path']=str(destination);self.save(run)
