"""Small private records bound to caller, project mapping and managed workspace."""
from __future__ import annotations
import json,time,re
from shared.util import DevError
from shared.crypto import digest


def binding(project):
    owner=project.get('_integration_owner') or project.get('_coding_owner')
    if not isinstance(owner,str) or not owner:
        raise DevError('INTEGRATION_OWNER_REQUIRED','缺少经过认证的调用方',403)
    return {'project':project['id'],'root':project.get('_original_root',project['root']),
            'workspace':project.get('_workspace_id',''),'device':project.get('_coding_device',''), 'owner':owner}

class Records:
    def __init__(self,journal):
        self.journal=journal
        with journal.lock,journal.db:
            journal.db.execute('CREATE TABLE IF NOT EXISTS integration_records (kind TEXT NOT NULL,id TEXT NOT NULL,binding TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(kind,id))')
            journal.db.execute('CREATE INDEX IF NOT EXISTS integration_record_time ON integration_records(kind,created)')

    def save(self,kind,identifier,project,body,*,replace=False):
        if not re.fullmatch('[a-f0-9]{32}',identifier):raise DevError('INVALID_RECORD','无效记录编号')
        bound=binding(project);text=json.dumps(body,ensure_ascii=False,separators=(',',':'))
        if len(text.encode())>4*1024*1024:raise DevError('RECORD_TOO_LARGE','证据记录超过 4 MiB，未保存假完整结果')
        j=self.journal
        with j.lock,j.db:
            old=j.db.execute('SELECT binding FROM integration_records WHERE kind=? AND id=?',(kind,identifier)).fetchone()
            if old:
                actual=json.loads(old['binding'])
                if actual!=bound and not(project.get('_integration_admin') and {k:v for k,v in actual.items() if k!='owner'}=={k:v for k,v in bound.items() if k!='owner'}):
                    raise DevError('RECORD_NOT_FOUND','记录不属于当前项目和授权',404)
                if not replace:raise DevError('RECORD_EXISTS','记录已存在；读取原操作，不要重复执行',409)
                j.db.execute('UPDATE integration_records SET body=? WHERE kind=? AND id=?',(text,kind,identifier))
            else:
                count=j.db.execute('SELECT count(*) FROM integration_records').fetchone()[0]
                if count>=2000:raise DevError('RECORD_QUOTA','证据记录达到上限；先在本机归档，不会删除活跃工作目录记录')
                j.db.execute('INSERT INTO integration_records VALUES(?,?,?,?,?)',(kind,identifier,json.dumps(bound,sort_keys=True),text,time.time()))
        return body

    def load(self,kind,identifier,project):
        j=self.journal
        with j.lock: row=j.db.execute('SELECT * FROM integration_records WHERE kind=? AND id=?',(kind,identifier)).fetchone()
        if not row:raise DevError('RECORD_NOT_FOUND','记录不存在',404)
        actual=json.loads(row['binding']);expected=binding(project)
        if actual!=expected and not(project.get('_integration_admin') and {k:v for k,v in actual.items() if k!='owner'}=={k:v for k,v in expected.items() if k!='owner'}):
            raise DevError('RECORD_NOT_FOUND','记录不属于当前项目映射和授权',404)
        return json.loads(row['body'])

    def list(self,kind,project,limit=100):
        j=self.journal;bound=binding(project)
        # Filter authorization before applying the visible limit.
        with j.lock: rows=j.db.execute('SELECT * FROM integration_records WHERE kind=? ORDER BY created DESC,id DESC',(kind,)).fetchall()
        result=[]
        for row in rows:
            actual=json.loads(row['binding'])
            same=actual==bound or project.get('_integration_admin') and {k:v for k,v in actual.items() if k!='owner'}=={k:v for k,v in bound.items() if k!='owner'}
            if same:
                result.append(json.loads(row['body']))
                if len(result)>=limit:break
        return result
