"""Browser-only SHA256 fallback works without crypto.subtle (ordinary HTTP)."""
import base64
import hashlib
import json
from pathlib import Path
import random
import subprocess


def test_http_sha256_fallback_matches_python_for_binary_utf8_and_chunk_boundaries():
    root=Path(__file__).resolve().parents[1]
    rng=random.Random(938112)
    payloads=[b'',b'abc','中文🙂'.encode()]+[rng.randbytes(n) for n in (1,31,55,56,63,64,65,255,1024,65535,65536,65537)]
    cases=[{'data':base64.b64encode(raw).decode(),'expected':hashlib.sha256(raw).hexdigest()} for raw in payloads]
    js=r'''
const fs=require('node:fs'),vm=require('node:vm');
(async()=>{
 const cases=JSON.parse(fs.readFileSync(0,'utf8'));
 // No Node process/require and no WebCrypto in this realm: execute the actual
 // browser UMD path, not Node's native crypto shortcut.
 const realm={Uint8Array,ArrayBuffer,console};realm.window=realm;realm.self=realm;
 vm.createContext(realm);
 vm.runInContext(fs.readFileSync('web/vendor/js-sha256-1.0.0/sha256.min.js','utf8'),realm);
 vm.runInContext(fs.readFileSync('web/native-hash.js','utf8'),realm);
 for(const c of cases){
  realm.bytes=Uint8Array.from(Buffer.from(c.data,'base64'));
  const result=await vm.runInContext('nativeSha256(bytes)',realm);
  if(result!==c.expected)throw new Error('hash mismatch for '+realm.bytes.length+' bytes');
 }
 console.log(JSON.stringify({cases:cases.length,webcrypto:false,result:'passed'}));
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result=subprocess.run(['node','-e',js],cwd=root,input=json.dumps(cases),text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['cases']==len(cases)
