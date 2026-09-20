"""Exercise the production HTTP helper, not the component transport stub."""
from pathlib import Path
from tests.test_chat_browser import chat_page

ROOT=Path(__file__).resolve().parents[1]


def production_api(page):
    source=(ROOT/'web/app.js').read_text()
    body=source[source.index('async function api('):source.index('\nconst post=')]
    # Return undefined: Playwright invokes a returned function expression.
    # Register the helper without accidentally issuing a request before mocks.
    page.evaluate('() => {window.productionAPI=('+body+');}')


def test_catalog_abort_reaches_fetch_without_retrying(chat_page):
    p=chat_page;production_api(p)
    result=p.evaluate('''async () => {
      const original=fetch,controller=new AbortController();let calls=0,aborted=false;
      window.fetch=(url,options)=>new Promise((resolve,reject)=>{
        calls++;options.signal.addEventListener('abort',()=>{aborted=true;reject(new DOMException('cancel','AbortError'));});
      });
      try {
        const pending=productionAPI('/api/test',{method:'POST',retrySafe:true,retryDelays:[],signal:controller.signal});
        controller.abort();
        try{await pending;return {unexpected:true};}catch(e){return {calls,aborted,name:e.name};}
      } finally {window.fetch=original;}
    }''')
    assert result=={'calls':1,'aborted':True,'name':'AbortError'}


def test_catalog_http_error_has_no_mutation_backoff(chat_page):
    p=chat_page;production_api(p)
    result=p.evaluate('''async () => {
      const original=fetch;let calls=0;
      window.fetch=async()=>{calls++;return new Response(JSON.stringify({error:{code:'CLI_CATALOG_TIMEOUT',message:'Catalog timed out'}}),{status:409});};
      try {
        try{await productionAPI('/api/native/chat_catalog',{method:'POST',retrySafe:true,retryDelays:[]});}
        catch(e){return {calls,code:e.code,status:e.status};}
      } finally {window.fetch=original;}
    }''')
    assert result=={'calls':1,'code':'CLI_CATALOG_TIMEOUT','status':409}


def test_non_retryable_mutation_is_not_automatically_resubmitted(chat_page):
    p=chat_page;production_api(p)
    result=p.evaluate('''async () => {
      const original=fetch;let calls=0;
      window.fetch=async()=>{calls++;throw new TypeError('Connection lost');};
      try {
        try{await productionAPI('/api/mutation',{method:'POST'});}
        catch(e){return {calls,code:e.code};}
      } finally {window.fetch=original;}
    }''')
    assert result=={'calls':1,'code':'NETWORK_UNCERTAIN'}
