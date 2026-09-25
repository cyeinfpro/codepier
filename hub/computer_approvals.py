"""Panel-only human consent codepier. No MCP tool can decide native permission."""
import time
import json
from shared.util import DevError
from hub import iam

class ComputerApprovals:
    def __init__(self, runtime):
        self.runtime = runtime
        self.pending = {}

    def changed(self, row):
        # SSE only invalidates the panel's inbox. App names, native messages,
        # session IDs and decisions stay behind the authenticated inbox API.
        self.runtime.publish('computer_approval', {'changed': True}, audience=row['_audience'])

    def remove(self, identifier):
        row = self.pending.pop(identifier, None)
        if row is not None:
            self.changed(row)
        return row

    def drop(self, connection):
        for key, row in list(self.pending.items()):
            if row['connection'] is connection:
                self.remove(key)

    def live(self, row):
        rt=self.runtime
        if (row['expires_at']<=time.time() or rt.connections.get(row['device_id']) is not row['connection']
            or not rt.connection_authorized(row['device_id'],row['connection'])):
            return False
        op=rt.store.one('SELECT * FROM operations WHERE id=?',(row['operation_id'],))
        if not op or op['state'] not in {'running','reconnecting'} or op['cancel_requested']:
            return False
        try:
            if op['grant_id'] and not rt.store.one('SELECT id FROM tokens WHERE grant_id=? AND kind IN (?,?) AND expires>? LIMIT 1',(op['grant_id'],'pat','access',time.time())):
                return False
            request=json.loads(rt.store.decrypt(op['payload']))
            return not rt.permission_error(op,request)
        except (ValueError,TypeError,KeyError):
            return False

    def receive(self, device, connection, data):
        key=data.get('request_id')
        if not isinstance(key,str) or len(key)!=32:return
        if data.get('type')=='computer_approval_closed':
            row=self.pending.get(key)
            if row and row['connection'] is connection:self.remove(key)
            return
        for field,limit in [('session_id',32),('project_id',100),('owner',300),('app',512),('operation_id',100),('message',2000)]:
            if not isinstance(data.get(field),str) or not 0<len(data[field])<=limit:return
        expiry=data.get('expires_at')
        if type(expiry) not in (int,float) or not time.time()<expiry<=time.time()+90:return
        op=self.runtime.store.one('SELECT * FROM operations WHERE id=? AND device_id=?',(data['operation_id'],device))
        if not op or op['tool'] not in {'computer_observe','computer_action'} or op['project_id']!=data['project_id']:return
        owner='grant:'+op['grant_id'] if op['grant_id'] else op['actor']
        if owner!=data['owner']:return
        if key in self.pending or len(self.pending)>=64:return
        row={k:data[k] for k in ['session_id','project_id','owner','app','operation_id','message','expires_at']}
        row.update(request_id=key,device_id=device,connection=connection,
                   _audience={'space_id':op['space_id'],'user_id':op['owner_user_id']})
        if not self.live(row):return
        self.pending[key]=row
        self.changed(row)

    def permitted(self, row, principal):
        try:
            op=self.runtime.store.one('SELECT * FROM operations WHERE id=?',(row['operation_id'],))
            current=iam.require_record(self.runtime.store,principal,op)
            self.runtime.project(row['project_id'],current)
            self.runtime.authorize(current,'computer',project_id=row['project_id'])
            return True
        except DevError:
            return False

    def list(self, principal=None):
        for key,row in list(self.pending.items()):
            if not self.live(row):self.remove(key)
        return [{k:v for k,v in row.items() if k not in {'connection','_audience'}} for row in self.pending.values() if principal is None or self.permitted(row,principal)]

    async def decide(self, identifier, action, principal):
        if action not in {'accept','decline','cancel'}:
            raise DevError('INVALID_DECISION','只支持允许、拒绝或取消')
        row=self.pending.get(identifier)
        if not row or not self.live(row):
            self.remove(identifier)
            raise DevError('APPROVAL_EXPIRED','授权请求已过期、断线或会话已结束；请重新读屏',409)
        if principal.grant_id or not self.permitted(row,principal):
            raise DevError('APPROVAL_FORBIDDEN','只能处理自己的已授权桌面请求',403)
        # Consume before awaiting send: two panel tabs cannot race to decide twice.
        self.remove(identifier)
        await row['connection'].send({'type':'computer_approval_decision','request_id':identifier,
                                       'session_id':row['session_id'],'action':action})
        self.runtime.store.audit(principal.actor,'computer.approval',identifier,detail={
            'action':action,'project_id':row['project_id'],'operation_id':row['operation_id'],'app':row['app']})
        return {'ok':True,'action':action}
