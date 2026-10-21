"""Browser regression for selecting and resuming a failed image after reload.

Requires the optional browser dependency group and a running frontend.
Set CHATGPT2API_UI_BASE_URL; all API calls are mocked in a fresh browser context.
No real credentials or generation requests are sent to the backend.
"""
import asyncio, json, os
from urllib.parse import urlparse,parse_qs
from playwright.async_api import async_playwright

BASE=os.environ.get('CHATGPT2API_UI_BASE_URL', 'http://127.0.0.1:3001').rstrip('/')
async def main():
    async with async_playwright() as p:
        options={'headless':True,'chromium_sandbox':os.environ.get('CHATGPT2API_BROWSER_NO_SANDBOX')!='1'}
        if os.environ.get('CHATGPT2API_BROWSER_EXECUTABLE'):options['executable_path']=os.environ['CHATGPT2API_BROWSER_EXECUTABLE']
        browser=await p.chromium.launch(**options)
        context=await browser.new_context(viewport={'width':1400,'height':1000})
        page=await context.new_page();calls=[];state={};polls={};errors=[]
        page.on('pageerror',lambda err:errors.append(str(err)))
        async def route_handler(route):
            request=route.request;parsed=urlparse(request.url);path=parsed.path
            if path=='/__ui_fixture':return await route.fulfill(content_type='text/html',body='<html></html>')
            if path.startswith('/images/result-'):
                return await route.fulfill(content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="blue"/></svg>')
            if path=='/auth/login':data={'role':'admin','subject_id':'admin','name':'UI fixture'}
            elif path=='/api/accounts':data={'items':[{'access_token':'ui-fixture-only','status':'正常','type':'pro','quota':10,'success':0,'fail':0}]}
            elif path=='/v1/models':data={'data':[{'id':'gpt-image-2'}]}
            elif path=='/api/settings':data={'config':{'image_poll_timeout_secs':600,'image_account_concurrency':3,'image_timeout_retry_secs':30}}
            elif path.startswith('/api/image-tasks/') and path.endswith('/resume-poll'):
                task_id=path.split('/')[-2];calls.append(('resume',task_id));state[task_id]='running';polls[task_id]=0
                data={'id':task_id,'status':'running','mode':'edit','data':[]}
            elif path=='/api/image-tasks' and request.method=='GET':
                items=[]
                for task_id in parse_qs(parsed.query).get('ids',[''])[0].split(','):
                    if not task_id:continue
                    if state.get(task_id)=='running':
                        polls[task_id]+=1
                        if polls[task_id]>=2:state[task_id]='success'
                    status=state.get(task_id,'error')
                    item={'id':task_id,'status':status,'mode':'edit','conversation_id':'upstream-'+task_id,'data':[]}
                    if status=='error':item['error']='ChatGPT 生图超时（已等待 120 秒）'
                    if status=='success':item['data']=[{'url':BASE+'/images/result-'+task_id+'.png'}]
                    items.append(item)
                data={'items':items,'missing_ids':[]}
            elif path.startswith('/api/image-tasks/') and request.method=='POST':
                calls.append(('unexpected-generation',path));return await route.fulfill(status=500,json={'error':'must not generate'})
            elif path.startswith(('/api/','/auth/','/v1/')):data={}
            else:return await route.continue_()
            return await route.fulfill(json=data)
        await page.route('**/*',route_handler)
        await page.goto(BASE+'/__ui_fixture')
        await page.evaluate('''async () => {
            const db=await new Promise((resolve,reject)=>{const r=indexedDB.open('chatgpt2api',1);r.onupgradeneeded=()=>{for(const name of ['auth','image_conversations','local-forage-detect-blob-support'])r.result.createObjectStore(name);};r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
            const now=new Date().toISOString();
            const conversation={id:'local-conversation',title:'Resume regression',createdAt:now,updatedAt:now,turns:[{id:'turn',prompt:'UI fixture',model:'gpt-image-2',mode:'edit',referenceImages:[],count:2,size:'1024x1024',ratio:'1:1',tier:'1k',quality:'medium',createdAt:now,status:'error',images:['task-clicked','task-other'].map(id=>({id,taskId:id,status:'error',error:'ChatGPT 生图超时（已等待 120 秒）'}))}]};
            await new Promise((resolve,reject)=>{const tx=db.transaction(['auth','image_conversations'],'readwrite');tx.objectStore('auth').put('ui-regression','chatgpt2api_auth_key');tx.objectStore('auth').put({key:'ui-regression',role:'admin',subjectId:'admin',name:'UI fixture'},'chatgpt2api_auth_session');tx.objectStore('image_conversations').put([conversation],'items');tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);});db.close();
            localStorage.setItem('chatgpt2api:image_active_conversation_id','local-conversation');
        }''')
        await page.goto(BASE+'/image',wait_until='networkidle')
        await page.get_by_role('button',name='继续等待',exact=True).first.wait_for(timeout=20000)
        await page.reload(wait_until='networkidle')
        await page.get_by_role('button',name='继续等待',exact=True).first.click()
        result=page.locator('img[src$="result-task-clicked.png"]')
        await result.first.wait_for(timeout=20000)
        await page.wait_for_function("Array.from(document.images).some(i=>i.src.endsWith('result-task-clicked.png')&&i.complete&&i.naturalWidth>0)")
        assert calls==[('resume','task-clicked')],calls
        assert await page.get_by_role('button',name='继续等待',exact=True).count()==1
        assert not errors,errors
        print(json.dumps({'success':True,'after_reload_button_works':True,'resumed_task':'task-clicked','other_task_unchanged':state.get('task-other','error')=='error','generation_requests':0,'browser_errors':errors},ensure_ascii=False))
        await browser.close()
asyncio.run(main())
