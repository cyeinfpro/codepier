"""Execute the real JavaScript input hook against fixture-owned image bytes."""
import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from agent.native_pi_bridge import prepare_extension,prepare_shell_guard,sync_manifest,BRIDGE_SOURCE
from shared.native_cli import database
from contextlib import closing
from scripts.check_native_cli_real import png


def test_native_pi_input_hook_attaches_real_bytes_and_rejects_changed_file(tmp_path):
    directory=tmp_path/'spool';sid=uuid.uuid4().hex;fid=uuid.uuid4().hex
    extension=prepare_extension(directory)
    assert prepare_extension(directory)==extension
    folder=directory/'uploads';folder.mkdir();file=folder/(fid+'.png');raw=png();file.write_bytes(raw)
    with closing(database(directory)) as db,db:
        db.execute('INSERT INTO attachments(id,project_id,root,name,size,sha,received,ready,path) VALUES (?,?,?,?,?,?,?,?,?)',
                   (fid,'p',str(tmp_path),'owned.png',len(raw),hashlib.sha256(raw).hexdigest(),len(raw),1,str(file)))
        db.execute('INSERT INTO session_files VALUES (?,?)',(sid,fid))
        manifest=sync_manifest(directory,db,sid)
    code=r'''
const fs=require('node:fs'),crypto=require('node:crypto'),url=require('node:url');
(async()=>{
 const [extension,manifest,file,id]=process.argv.slice(1);
 process.env.CODEPIER_PI_IMAGE_MANIFEST=manifest;
 let hook,notifications=[];
 const plugin=(await import(url.pathToFileURL(extension))).default;
 plugin({on:(event,handler)=>{if(event!=='input')throw Error('unexpected hook');hook=handler;}});
 const ctx={ui:{notify:(...args)=>notifications.push(args)}};
 const untouched=await hook({text:'/model',images:[],source:'interactive'},ctx);
 if(untouched.action!=='continue')throw Error('changed native commands');
 const result=await hook({text:'Describe [[codepier-image:'+id+']]',images:[],source:'interactive'},ctx);
 if(result.action!=='transform'||result.images.length!==1)throw Error('image not attached');
 const image=result.images[0],raw=Buffer.from(image.data,'base64');
 if(image.type!=='image'||image.mimeType!=='image/png'||!raw.equals(fs.readFileSync(file)))throw Error('payload changed');
 fs.appendFileSync(file,Buffer.from('tampered'));
 const denied=await hook({text:'[[codepier-image:'+id+']]',source:'interactive'},ctx);
 if(denied.action!=='handled'||notifications.length!==1)throw Error('invalid image sent');
 console.log(JSON.stringify({nativeInputHook:true,imageBytes:raw.length,sha256:crypto.createHash('sha256').update(raw).digest('hex'),invalidBlocked:true}));
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result=subprocess.run(['node','-e',code,str(extension),str(manifest),str(file),fid],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['sha256']==hashlib.sha256(raw).hexdigest()


def test_pi_shell_guard_defaults_only_missing_bash_deadlines(tmp_path):
    extension = prepare_shell_guard(tmp_path)
    assert prepare_shell_guard(tmp_path) == extension
    code = r'''
const {pathToFileURL}=require('node:url');
(async()=>{
 let hook;
 (await import(pathToFileURL(process.argv[1]))).default({on:(name,fn)=>{
   if(name!=='tool_call')throw Error('wrong hook');hook=fn;
 }});
 const event={toolName:'bash',input:{command:'sleep 600'}};
 hook(event);
 if(event.input.timeout!==120||event.input.command!=='sleep 600')throw Error('missing default');
 for(const timeout of [0.1,1800,0,-1,null]){
   const explicit={toolName:'bash',input:{command:'test',timeout}};
   hook(explicit);
   if(explicit.input.timeout!==timeout)throw Error('changed explicit deadline');
 }
 const read={toolName:'read',input:{path:'x'}};hook(read);
 if('timeout' in read.input)throw Error('changed other tool');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', code, str(extension)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
