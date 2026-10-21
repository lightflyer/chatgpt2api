"""Check reference uploads in an isolated browser with every API mocked.

Set CHATGPT2API_UI_BASE_URL to a running frontend. No real generation is sent.
"""
import asyncio
import base64
import json
import os
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

BASE = os.environ.get("CHATGPT2API_UI_BASE_URL", "http://127.0.0.1:3001").rstrip("/")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jPRsAAAAASUVORK5CYII=")


async def check(browser, case, count=1):
    context = await browser.new_context(viewport={"width": 1400, "height": 1000})
    page = await context.new_page()
    calls, errors = [], []
    submitted = asyncio.Event()
    page.on("pageerror", lambda error: errors.append(str(error)))

    async def route_handler(route):
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if path == "/__ui_fixture":
            return await route.fulfill(content_type="text/html", body="<html></html>")
        if path.startswith("/images/context-fixture-"):
            return await route.fulfill(status=404 if case == "unavailable" else 200,
                content_type="image/png", body=PNG)
        if path == "/auth/login":
            data = {"role": "admin", "subject_id": "admin", "name": "UI fixture"}
        elif path == "/api/accounts":
            data = {"items": [{"status": "正常", "type": "pro", "quota": 10}]}
        elif path == "/v1/models":
            data = {"data": [{"id": "gpt-image-2"}]}
        elif path == "/api/settings":
            data = {"config": {"image_account_concurrency": 3, "image_poll_timeout_secs": 600}}
        elif path == "/api/image-tasks" and request.method == "GET":
            data = {"items": [], "missing_ids": parse_qs(parsed.query).get("ids", [""])[0].split(",")}
        elif path in ("/api/image-tasks/edits", "/api/image-tasks/generations") and request.method == "POST":
            if path.endswith("edits"):
                header = request.headers["content-type"]
                form = BytesParser(policy=policy.default).parsebytes(
                    f"Content-Type: {header}\r\nMIME-Version: 1.0\r\n\r\n".encode() + request.post_data_buffer)
                parts = list(form.iter_parts())
                fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                    for part in parts if not part.get_filename()}
                images = [part for part in parts if part.get_filename()]
                calls.append({"mode": "edit", "files": [part.get_filename() for part in images],
                    "correct_image_bytes": all(part.get_payload(decode=True) == PNG for part in images)})
                task_id = fields["client_task_id"].decode()
            else:
                task_id = request.post_data_json["client_task_id"]
                calls.append({"mode": "generate", "files": []})
            data = {"id": task_id, "status": "error", "error": "Diagnostic fixture complete", "data": []}
            submitted.set()
        elif path.startswith(("/api/", "/auth/", "/v1/")):
            data = {}
        else:
            return await route.continue_()
        return await route.fulfill(json=data)

    await page.route("**/*", route_handler)
    await page.goto(BASE + "/__ui_fixture")
    await page.evaluate("""async ({base, count}) => {
        const db = await new Promise((resolve, reject) => {
            const req = indexedDB.open('chatgpt2api', 1);
            req.onupgradeneeded = () => { for (const name of ['auth','image_conversations','local-forage-detect-blob-support']) req.result.createObjectStore(name); };
            req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error);
        });
        const now = new Date().toISOString();
        const conversation = {id:'context-conversation',title:'Editing regression',createdAt:now,updatedAt:now,
            turns:[{id:'previous-turn',prompt:'previous picture',model:'gpt-image-2',mode:'generate',referenceImages:[],count,
            size:'1024x1024',ratio:'1:1',tier:'1k',quality:'auto',createdAt:now,status:'success',
            images:Array.from({length:count},(_,i)=>({id:'result-'+i,status:'success',url:base+'/images/context-fixture-'+i+'.png'}))}]};
        await new Promise((resolve,reject)=>{
            const tx=db.transaction(['auth','image_conversations'],'readwrite');
            tx.objectStore('auth').put('fixture-only','chatgpt2api_auth_key');
            tx.objectStore('auth').put({key:'fixture-only',role:'admin',subjectId:'admin',name:'UI fixture'},'chatgpt2api_auth_session');
            tx.objectStore('image_conversations').put([conversation],'items'); tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);
        }); db.close();
        localStorage.setItem('chatgpt2api:image_active_conversation_id','context-conversation');
        localStorage.setItem('chatgpt2api:image_last_count','1');
    }""", {"base": BASE, "count": count})
    await page.goto(BASE + "/image", wait_until="networkidle")
    await page.locator("textarea").wait_for()
    if case == "restored":
        await page.reload(wait_until="networkidle")
    if count == 1:
        await page.get_by_role("button", name="编辑上一张", exact=True).wait_for()
        assert await page.get_by_role("button", name="编辑上一张", exact=True).get_attribute("aria-pressed") == "true"
        await page.get_by_role("button", name="预览参考图 上一张结果图.png").wait_for()
    if case == "independent":
        await page.get_by_role("button", name="独立生图", exact=True).click()
        assert await page.get_by_role("button", name="预览参考图 上一张结果图.png").count() == 0
    elif case == "manual":
        await page.locator('input[type="file"]').set_input_files({"name": "manual.png", "mimeType": "image/png", "buffer": PNG})
        await page.get_by_role("button", name="预览参考图 manual.png").wait_for()
    elif count > 1:
        assert await page.get_by_role("button", name="编辑上一张", exact=True).count() == 0
        if case == "batch-selected":
            await page.get_by_role("button", name="加入编辑", exact=True).nth(1).click()
            await page.get_by_text("已加入当前参考图，继续输入描述即可编辑").wait_for()
    await page.locator("textarea").fill("把衣服改成蓝色")
    await page.locator("textarea").press("Enter")
    if case == "unavailable":
        await page.get_by_text("读取结果图失败", exact=True).wait_for()
        assert calls == [], calls
        assert await page.locator("textarea").input_value() == "把衣服改成蓝色"
    else:
        await asyncio.wait_for(submitted.wait(), 15)
        assert len(calls) == 1, calls
        expected = "generate" if case in ("independent", "batch-independent") else "edit"
        assert calls[0]["mode"] == expected, calls
        if expected == "edit":
            assert len(calls[0]["files"]) == 1 and calls[0]["correct_image_bytes"], calls
        if case == "manual":
            assert calls[0]["files"] == ["manual.png"], calls
    assert errors == [], errors
    await context.close()
    return {"case": case, "passed": True, "submitted": calls}


async def main():
    async with async_playwright() as playwright:
        options = {"headless": True, "chromium_sandbox": os.environ.get("CHATGPT2API_BROWSER_NO_SANDBOX") != "1"}
        if os.environ.get("CHATGPT2API_BROWSER_EXECUTABLE"):
            options["executable_path"] = os.environ["CHATGPT2API_BROWSER_EXECUTABLE"]
        browser = await playwright.chromium.launch(**options)
        results = []
        for case in ("restored", "independent", "manual", "batch-selected", "batch-independent", "unavailable"):
            results.append(await check(browser, case, 2 if case.startswith("batch-") else 1))
        await browser.close()
        print(json.dumps({"success": True, "cases": results}, ensure_ascii=False))


asyncio.run(main())
