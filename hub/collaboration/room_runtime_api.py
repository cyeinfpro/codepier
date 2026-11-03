"""Room timeline and channel routes share existing panel authentication."""
from __future__ import annotations

from fastapi import Request, Response


def register(router, service, auth):
    store=service.store

    @router.get('/rooms/{conversation_id}/messages')
    async def messages(request:Request,conversation_id:str,cursor:str='',limit:int=50,client_message_id:str='',message_id:str=''):
        principal=await store.run(auth.admin,request,request.method != 'GET')
        return await store.run(lambda:service.room_runtime.read(conversation_id,principal,cursor=cursor,limit=limit,
            client_message_id=client_message_id,message_id=message_id))

    @router.get('/rooms/{conversation_id}/work')
    async def works(request:Request,response:Response,conversation_id:str,cursor:str='',limit:int=50):
        response.headers['Cache-Control']='private, no-store'
        principal=await store.run(auth.admin,request,False)
        return await store.run(lambda:service.room_runtime.work_page(conversation_id,principal,cursor=cursor,limit=limit))

    @router.get('/work/{work_id}')
    async def work(request:Request,response:Response,work_id:str):
        response.headers['Cache-Control']='private, no-store'
        principal=await store.run(auth.admin,request,False)
        return await store.run(service.room_runtime.work_read,work_id,principal)

    @router.post('/rooms/{conversation_id}/messages')
    async def message(request:Request,conversation_id:str,body:dict):
        principal=await store.run(auth.admin,request,request.method != 'GET')
        return await store.run(service.room_runtime.send,{**body,'conversation_id':conversation_id},principal)

    @router.post('/work/{work_id}/control')
    async def control(request:Request,work_id:str,body:dict):
        principal=await store.run(auth.admin,request,request.method != 'GET')
        return await store.run(service.room_runtime.control,{**body,'work_id':work_id},principal)

    @router.post('/rooms/{conversation_id}/channels/weixin/connect')
    async def connect(request:Request,conversation_id:str,body:dict):
        principal=await store.run(auth.admin,request,request.method != 'GET')
        return await store.run(service.room_channels.create,{**body,'conversation_id':conversation_id},principal)

    @router.post('/rooms/{conversation_id}/legacy-channels/{binding_id}/pause')
    async def pause_legacy_channel(request:Request,conversation_id:str,binding_id:str,body:dict):
        principal=await store.run(auth.admin,request,True)
        return await store.run(service.room_channels.pause_legacy,conversation_id,binding_id,body,principal)

    @router.get('/channel-connect-flows/{flow_id}')
    async def flow(request:Request,flow_id:str):
        principal=await store.run(auth.admin,request,request.method != 'GET')
        return await store.run(service.room_channels.read,flow_id,principal)

    @router.post('/channel-connect-flows/{flow_id}/actions')
    async def flow_action(request:Request,flow_id:str,body:dict):
        principal=await store.run(auth.admin,request,request.method != 'GET')
        return await service.room_channels.action({**body,'flow_id':flow_id},principal)
