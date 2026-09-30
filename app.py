import argparse, json, os, queue, subprocess, sys, threading, traceback
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from engine import Cancelled, SOURCES, SUBS
from deep_engine import Engine

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title('자막 중복 정리'); self.geometry('1140x790'); self.minsize(880,650)
        self.inputs={s:[] for s in SOURCES}; self.run_data=None; self.busy=False; self.events=queue.Queue(); self.stop=threading.Event()
        self.output=tk.StringVar(value=str(Path.home()/'Downloads'/'SubtitleCleanup'))
        self.cross=tk.BooleanVar(value=True); self.status=tk.StringVar(value='원본 폴더 또는 분할 ZIP을 선택하면 먼저 전체 해제·확장자 조사만 수행합니다.')
        style=ttk.Style(); style.theme_use('clam'); style.configure('TButton', padding=7)
        style.configure('Header.TLabel',font=('Malgun Gothic',17,'bold'))
        panel=ttk.Frame(self,padding=16); panel.pack(fill='both',expand=True)
        ttk.Label(panel,text='자막 중복 정리',style='Header.TLabel').pack(anchor='w')
        ttk.Label(panel,text='선택한 소스 폴더 바로 아래의 작품 폴더별로 ZIP을 전부 해제하고 확장자를 조사합니다. 자동 삭제는 하지 않으며 원본은 보존합니다.').pack(anchor='w',pady=(4,12))
        self.controls=[]; self.lists={}
        for s in SOURCES:
            row=ttk.LabelFrame(panel,text=s,padding=6); row.pack(fill='x',pady=3)
            view=tk.Listbox(row,height=2,font=('Malgun Gothic',9)); view.pack(side='left',fill='x',expand=True); self.lists[s]=view
            for label,fn in [('폴더 추가',lambda s=s:self.add_folder(s)),('ZIP 추가',lambda s=s:self.add_zip(s)),('비우기',lambda s=s:self.clear(s))]:
                b=ttk.Button(row,text=label,command=fn); b.pack(side='left',padx=3);self.controls.append(b)
        row=ttk.Frame(panel);row.pack(fill='x',pady=8)
        ttk.Label(row,text='결과 위치').pack(side='left');ttk.Entry(row,textvariable=self.output).pack(side='left',fill='x',expand=True,padx=8)
        b=ttk.Button(row,text='선택',command=self.choose_output);b.pack(side='left');self.controls.append(b)
        ttk.Checkbutton(panel,text='소스 사이의 확정 중복도 대표본 하나만 남김',variable=self.cross).pack(anchor='w')
        ttk.Label(panel,text='해제본은 expanded/소스/작품명/ 바로 아래에 모입니다. 같은 이름은 __2 식으로 보존하고, 모든 확장자를 조사합니다.').pack(anchor='w',pady=5)
        actions=ttk.Frame(panel);actions.pack(fill='x',pady=6)
        for label,fn in [('전체 압축 해제·확장자 조사',self.analyze),('무확장자 형식 판별',self.detect_extensionless),('선택 확장자 적용·중복 정리',self.choose_extensions_and_clean),('작업 불러오기',self.load),('원래 구조 복원',self.restore),('결과 열기',self.open_output)]:
            b=ttk.Button(actions,text=label,command=fn);b.pack(side='left',padx=3);self.controls.append(b)
        ttk.Button(actions,text='중지',command=self.stop.set).pack(side='right')
        self.bar=ttk.Progressbar(panel,mode='indeterminate');self.bar.pack(fill='x',pady=4)
        ttk.Label(panel,textvariable=self.status).pack(anchor='w',pady=3)
        tabs=ttk.Notebook(panel);tabs.pack(fill='both',expand=True)
        frame=ttk.Frame(tabs);tabs.add(frame,text='분석 결과')
        self.tree=ttk.Treeview(frame,columns=('group','action','path','representative','reason'),show='headings')
        for key,label,w in [('group','그룹',90),('action','처리',80),('path','파일 경로',350),('representative','보존 대표본',350),('reason','근거',250)]:
            self.tree.heading(key,text=label,command=lambda k=key:self.sort(k));self.tree.column(key,width=w)
        vs=ttk.Scrollbar(frame,orient='vertical',command=self.tree.yview);vs.pack(side='right',fill='y');self.tree.configure(yscrollcommand=vs.set)
        hs=ttk.Scrollbar(frame,orient='horizontal',command=self.tree.xview);hs.pack(side='bottom',fill='x');self.tree.configure(xscrollcommand=hs.set);self.tree.pack(fill='both',expand=True)
        self.log_widget=tk.Text(tabs,height=12,font=('Malgun Gothic',10));tabs.add(self.log_widget,text='진행 로그')
        self.after(120,self.poll);self.protocol('WM_DELETE_WINDOW',self.close)
    def add_folder(self,s):
        p=filedialog.askdirectory()
        if p:self.add(s,[p])
    def add_zip(self,s):
        ps=filedialog.askopenfilenames(filetypes=[('ZIP','*.zip')])
        self.add(s,ps)
    def add(self,s,ps):
        for p in ps:
            if p not in self.inputs[s]:self.inputs[s].append(p);self.lists[s].insert('end',p)
    def clear(self,s):self.inputs[s]=[];self.lists[s].delete(0,'end')
    def choose_output(self):
        p=filedialog.askdirectory()
        if p:self.output.set(p)
    def worker(self,fn):
        if self.busy:return
        self.busy=True;self.stop.clear();self.bar.start()
        for b in self.controls:b.configure(state='disabled')
        def work():
            try:self.events.put(('done',fn(Engine(lambda x:self.events.put(('log',x)),self.stop))))
            except Cancelled as e:self.events.put(('cancel',str(e)))
            except Exception as e:self.events.put(('error',(str(e),traceback.format_exc())))
        threading.Thread(target=work,daemon=True).start()
    def analyze(self):
        if not any(self.inputs.values()):messagebox.showinfo('입력','원본 폴더나 ZIP을 선택하세요.');return
        inputs={s:list(ps) for s,ps in self.inputs.items()}; output=self.output.get();cross=self.cross.get()
        def automatic(e):
            data=e.analyze(inputs,output,cross)
            self.events.put(('run',data))
            return data
        self.worker(automatic)
    def detect_extensionless(self):
        if not self.run_data or self.run_data.get('state')!='분석 완료':
            messagebox.showinfo('판별','먼저 전체 압축 해제·확장자 조사를 완료하거나 작업을 불러오세요.')
            return
        n=sum(not Path(r['name']).suffix for r in self.run_data.get('records',[]))
        if not n:
            messagebox.showinfo('판별','확장자 없는 파일이 없습니다.');return
        if messagebox.askyesno('무확장자 형식 판별',f'확장자 없는 파일 {n:,}개만 검사합니다.\n\n자막 형식뿐 아니라 ZIP/7z/RAR/EGG/ALZ 등 압축 형식과 흔한 이미지·폰트·PDF도 시그니처로 판별합니다.\n유효한 ZIP은 같은 작품 폴더에 바로 재귀 해제하고, 다른 압축 형식은 확장자만 붙여 보존합니다.\n\n전체 원본 재스캔은 하지 않습니다.'):
            self.worker(lambda e:e.detect_extensionless(self.run_data))

    def choose_extensions_and_clean(self):
        if not self.run_data or self.run_data.get('state')!='분석 완료':
            messagebox.showinfo('정리','먼저 전체 압축 해제·확장자 조사를 완료하거나 작업을 불러오세요.')
            return
        records=self.run_data.get('records',[])
        if any(not Path(r['name']).suffix for r in records) and not self.run_data.get('extensionless_scanned'):
            messagebox.showinfo('정리','확장자 없는 파일이 남아 있습니다. 먼저 `무확장자 형식 판별`을 실행하세요.')
            return
        if not records:
            messagebox.showinfo('정리','조사된 파일이 없습니다.');return
        counts={}
        for r in records:
            ext=Path(r['name']).suffix.lower() or '(없음)'
            counts[ext]=counts.get(ext,0)+1
        exts=sorted(counts,key=lambda x:(x=='(없음)',x))

        win=tk.Toplevel(self);win.title('남길 자막 확장자 선택');win.transient(self);win.grab_set()
        win.geometry('460x560');win.minsize(380,420)
        body=ttk.Frame(win,padding=14);body.pack(fill='both',expand=True)
        ttk.Label(body,text='남길 확장자만 선택',font=('Malgun Gothic',13,'bold')).pack(anchor='w')
        ttk.Label(body,text='선택하지 않은 확장자는 제거합니다. 미해제 압축 형식은 안전상 기본 선택되고, 자막은 작품 폴더 안에서 SHA-256이 같은 것만 하나 남깁니다.').pack(anchor='w',pady=(4,10))

        frame=ttk.Frame(body);frame.pack(fill='both',expand=True)
        lb=tk.Listbox(frame,selectmode='multiple',font=('Consolas',10),exportselection=False)
        sb=ttk.Scrollbar(frame,orient='vertical',command=lb.yview);lb.configure(yscrollcommand=sb.set)
        lb.pack(side='left',fill='both',expand=True);sb.pack(side='right',fill='y')
        defaults=set(SUBS)-{'.txt'}
        # Unresolved archives are selected by default so cleanup cannot silently delete them.
        archive_exts={'.zip','.7z','.rar','.egg','.alz','.gz','.bz2','.xz','.tar'}
        defaults |= {ext for ext in exts if ext in archive_exts or __import__('re').fullmatch(r'\.z\d\d',ext)}
        for i,ext in enumerate(exts):
            lb.insert('end',f'{ext:<10} {counts[ext]:>8,}개')
            if ext in defaults:lb.selection_set(i)

        row=ttk.Frame(body);row.pack(fill='x',pady=(10,0))
        ttk.Button(row,text='전부 선택',command=lambda:lb.selection_set(0,'end')).pack(side='left')
        ttk.Button(row,text='전부 해제',command=lambda:lb.selection_clear(0,'end')).pack(side='left',padx=5)

        def execute():
            selected=[exts[i] for i in lb.curselection()]
            if not selected:
                messagebox.showinfo('확장자','남길 확장자를 하나 이상 선택하세요.',parent=win);return
            keep=sum(counts[x] for x in selected);remove=len(records)-keep
            names=', '.join(selected)
            if not messagebox.askyesno(
                '정리 확인',
                f'남길 확장자: {names}\\n\\n선택 확장자 파일 {keep:,}개를 대상으로 작품 폴더별 SHA 중복을 제거합니다.\\n'
                f'선택하지 않은 파일 {remove:,}개도 작업 해제본에서 제거합니다.\\n\\n'
                '원본과 original_copy는 건드리지 않으며 ZIP으로 다시 압축하지 않습니다.',
                parent=win
            ):return
            win.destroy()
            self.worker(lambda e:e.clean_flat(self.run_data,selected))

        ttk.Button(row,text='정리 실행',command=execute).pack(side='right')
        ttk.Button(row,text='취소',command=win.destroy).pack(side='right',padx=5)

    def apply(self):
        if not self.run_data or self.run_data['state']!='분석 완료':messagebox.showinfo('분석','먼저 분석을 완료하세요.');return
        n=sum(r['delete'] for r in self.run_data['records']);size=sum(r['size'] for r in self.run_data['records'] if r['delete'])/1024**2
        if messagebox.askyesno('복사본 정리',f'확정 중복 {n:,}개 ({size:,.1f} MiB)를 작업 복사본에서 삭제하고 ZIP을 만들까요?\n원본은 유지됩니다. 상세 내역은 Excel에서 확인할 수 있습니다.'):
            self.worker(lambda e:(e.apply(self.run_data),self.run_data)[1])
    def restore(self):
        if not self.run_data:return
        self.worker(lambda e:(e.restore(self.run_data),self.run_data)[1])
    def load(self):
        p=filedialog.askopenfilename(filetypes=[('작업 기록','manifest.json')])
        if not p:return
        try:
            data=json.loads(Path(p).read_text(encoding='utf-8'))
            # Treat records as data, never allow loaded paths to escape the chosen run folder.
            from engine import safe_member
            data['run']=str(Path(p).resolve().parent)
            for r in data['records']:
                if r['source'] not in SOURCES:raise ValueError('소스 오류')
                for part in r['rel'].split('!/'):
                    safe_member(part)
                if r.get('work'):safe_member(r['work'])
            self.run_data=data;self.refresh()
        except Exception as e:messagebox.showerror('불러오기 실패',str(e))
    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        if not self.run_data:return
        rs=self.run_data['records']; n=sum(r['delete'] for r in rs)
        self.status.set(f'{self.run_data["state"]} | 전체 {len(rs):,}개 | 정리 대상 {n:,}개 | 실제 제거 {sum(r["actual"] for r in rs):,}개 | 검토 {len(self.run_data.get("reviews",[])):,}건')
        for r in rs:
            if r['group'] or r['error'] or r['actual']:
                self.tree.insert('','end',values=(r['group'],'삭제' if r['actual'] else ('삭제 후보' if r['delete'] else '보존'),r['source']+'/'+r['rel'],r['representative'],r['error'] or r['reason']))
    def sort(self,k):
        items=sorted((self.tree.set(i,k),i) for i in self.tree.get_children())
        for n,(_,i) in enumerate(items):self.tree.move(i,'',n)
    def open_output(self):
        p=self.run_data['run'] if self.run_data else self.output.get()
        if not Path(p).exists():return
        if os.name=='nt':os.startfile(p)
        else:subprocess.Popen(['xdg-open',p])
    def poll(self):
        while True:
            try:kind,value=self.events.get_nowait()
            except queue.Empty:break
            if kind=='run':
                self.run_data=value
            elif kind=='log':
                self.log_widget.insert('end',value+'\n');self.log_widget.see('end');self.status.set(value)
            else:
                self.busy=False;self.bar.stop()
                for b in self.controls:b.configure(state='normal')
                if kind=='done':self.run_data=value;self.refresh()
                elif kind=='error':
                    self.log_widget.insert('end',value[1]+'\n');messagebox.showerror('작업 실패',value[0]);self.refresh()
                else:self.status.set(value)
        self.after(120,self.poll)
    def close(self):
        if self.busy:
            self.stop.set();messagebox.showinfo('중지 요청','중지 후 창을 닫아주세요.');return
        self.destroy()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--smoke-test',action='store_true');args=parser.parse_args()
    app=App()
    if args.smoke_test:app.update();app.destroy();return
    app.mainloop()

if __name__=='__main__':main()
