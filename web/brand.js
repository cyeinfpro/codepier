'use strict';
/* One-way preference and receipt migration. Not a credential store. */
(function initCodePierBrand(window){
  const exact=new Map([
    ['relay-appearance','codepier-appearance'],['relay-chat-appearance','codepier-chat-appearance'],
    ['relay-operation','codepier-operation'],['relay-task-submission','codepier-task-submission'],
  ]);
  function migrate(storage){
    const result={moved:0,conflicts:0,unavailable:false};
    try{
      const keys=Array.from({length:storage.length},(_,i)=>storage.key(i));
      for(const old of keys){
        const key=exact.get(old)||(/^relay-agent-lifecycle:[A-Za-z0-9_-]{1,100}:[a-z_]+$/.test(old)?'codepier'+old.slice(5):null);
        if(!key)continue;
        const value=storage.getItem(old),current=storage.getItem(key);
        if(value===null)continue;
        if(current!==null&&current!==value){result.conflicts++;continue;}
        if(current===null)storage.setItem(key,value);
        if(storage.getItem(key)===value){storage.removeItem(old);result.moved++;}
      }
    }catch{result.unavailable=true;}
    return result;
  }
  const storage=[];
  for(const name of ['localStorage','sessionStorage']){
    try{storage.push({store:name,...migrate(window[name])});}
    catch{storage.push({store:name,unavailable:true});}
  }
  window.CodePierBrand=Object.freeze({name:'CodePier',title:'CodePier · 码头',
    tagline:'AI 与本地代码对接、任务停靠的地方',storageMigration:storage,migrateStorage:migrate});
})(window);
