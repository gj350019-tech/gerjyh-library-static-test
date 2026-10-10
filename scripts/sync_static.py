#!/usr/bin/env python3
"""格致數位閱讀館 V13.4.1: 從公開 JSONP API 建構純靜態 GitHub Pages 網站。

用法: python scripts/sync_static.py --api-url URL --root . --out _site
- 僅取得 Published 刊物；無需管理員憑證、GitHub PAT 或私有 Drive ID。
- 任何圖片或 PDF 下載失敗會終止程序，既有 Pages 保持上一個成功部署。
- 不會修改原始 JSON、GAS、Google Sheets 或 GitHub 儲存庫內容。
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar

API_CALLBACK = 'gjStaticSync'
UUID_RE = re.compile(r'^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$')
PDF_ID_RE = re.compile(r'^[A-Za-z0-9_-]{15,}$')
MAX_IMAGE = 1_150_000
MAX_COVER = 650_000
MAX_DOCUMENT = 35 * 1024 * 1024  # 超過此大小請改用「manual_documents.json」離線上傳
MAX_SITE = 900 * 1024 * 1024
TZ_TW = timezone(timedelta(hours=8))
LOG = logging.getLogger('static-sync')


class SyncError(RuntimeError):
    pass


def http_bytes(url, limit, tries=3):
    if not url.startswith('https://'):
        raise SyncError('只接受 HTTPS 資料來源')
    error = None
    for attempt in range(tries):
        try:
            opener = build_opener(HTTPCookieProcessor(CookieJar()))
            req = Request(url, headers={'User-Agent':'GerJyhLibrary-StaticSync/1.0', 'Accept':'*/*'})
            with opener.open(req, timeout=65) as resp:
                if not resp.geturl().startswith('https://'):
                    raise SyncError('來源重新導向至不安全的網址')
                data = resp.read(limit + 1)
                if len(data) > limit:
                    raise SyncError('檔案超過測試版大小限制，請改用手動上傳或物件儲存服務')
                if not data:
                    raise SyncError('來源回傳空白內容')
                return data
        except SyncError:
            raise
        except Exception as exc:
            error = exc
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
    raise SyncError('無法下載公開來源：' + str(error)[:180])


def api_url(base, **params):
    if not base.startswith('https://script.google.com/macros/s/') or '/exec' not in base:
        raise SyncError('公開 API 必須是 script.google.com 的正式 /exec 網址')
    base = base.split('?', 1)[0]
    return base + '?' + urlencode({'callback': API_CALLBACK, **params})


def parse_jsonp(data):
    text = data.decode('utf-8-sig').strip()
    prefix = API_CALLBACK + '('
    if not text.startswith(prefix) or not text.endswith(');'):
        raise SyncError('API 未回傳有效的 JSONP；可能被導向登入頁（請檢查「任何人」權限）')
    try:
        obj = json.loads(text[len(prefix):-2])
    except json.JSONDecodeError as exc:
        raise SyncError('JSONP 格式錯誤：' + str(exc)) from exc
    if not isinstance(obj, dict) or obj.get('ok') is not True:
        reason = obj.get('error', '未知錯誤') if isinstance(obj, dict) else '回傳不是物件'
        raise SyncError('公開 API 回報錯誤：' + str(reason)[:150])
    return obj


def api_call(base, **params):
    url = api_url(base, **params)
    return parse_jsonp(http_bytes(url, 3 * 1024 * 1024))


def decode_image(obj, max_bytes):
    if obj.get('mime') != 'image/jpeg' or not isinstance(obj.get('base64'), str):
        raise SyncError('API 圖片格式必須為 JPG')
    try:
        data = base64.b64decode(obj['base64'], validate=True)
    except Exception as exc:
        raise SyncError('圖片 Base64 格式不正確') from exc
    if not data.startswith(b'\xff\xd8\xff') or len(data) > max_bytes:
        raise SyncError('圖片格式錯誤或容量超過限制')
    return data


def normalize_issue(raw):
    text = str(raw or '')
    if re.match(r'^\d{4}-(0[1-9]|1[0-2])$', text):
        return text
    if re.match(r'^\d{4}-\d{2}-\d{2}T', text):
        try:
            d = datetime.fromisoformat(text.replace('Z','+00:00'))
            return d.astimezone(TZ_TW).strftime('%Y-%m')
        except ValueError:
            pass
    return text[:45]


def ensure_https_url(url):
    u = str(url or '').strip()
    parts = urlparse(u)
    if parts.scheme != 'https' or not parts.netloc or parts.username or parts.password:
        raise SyncError('外部網址必須是正確的 HTTPS 連結')
    return u


def document_drive_id(url):
    p = urlparse(url)
    if p.scheme != 'https' or p.hostname not in ('drive.google.com', 'drive.usercontent.google.com'):
        raise SyncError('文件來源不是已公開的 Google Drive PDF')
    match = re.search(r'/file/d/([\w-]{15,})', p.path)
    if match: return match.group(1)
    sid = parse_qs(p.query).get('id', [''])[0]
    if PDF_ID_RE.fullmatch(sid): return sid
    raise SyncError('無法識別公開 PDF 的檔案 ID')


def fetch_public_pdf(url):
    fid = document_drive_id(url)
    # 不從讀者端存取 Google Drive，這裡只在 GitHub Actions 同步工作中取回公開原檔。
    candidates = [
        'https://drive.usercontent.google.com/download?' + urlencode({'id':fid,'export':'download','confirm':'t'}),
        'https://drive.google.com/uc?' + urlencode({'export':'download','id':fid,'confirm':'t'}),
    ]
    last_error = None
    for candidate in candidates:
        try:
            data = http_bytes(candidate, MAX_DOCUMENT)
            if data[:5] == b'%PDF-':
                return data
            last_error = '取得的是 HTML 預覽或 Google 登入頁，不是 PDF 原始檔'
        except Exception as exc:
            last_error = str(exc)
    raise SyncError('Google Drive PDF 無法匿名下載（' + str(last_error)[:160] + '）；請改用 manual_documents.json')


def read_manual_overrides(root):
    cfg = root / 'manual_documents.json'
    if not cfg.exists(): return {}
    doc = json.loads(cfg.read_text(encoding='utf-8'))
    if not isinstance(doc, dict): raise SyncError('manual_documents.json 應為 JSON 物件')
    out = {}
    for docid, rel in doc.items():
        if not UUID_RE.fullmatch(docid) or not isinstance(rel, str) or not rel.startswith('assets/manual/') or not rel.endswith('.pdf'):
            raise SyncError('manual_documents.json 只允許 UUID 對應 assets/manual/ 下的 PDF')
        abs_path = (root / rel).resolve()
        if not abs_path.is_relative_to((root / 'assets' / 'manual').resolve()) or not abs_path.is_file():
            raise SyncError('找不到手動 PDF 檔案：' + rel)
        if abs_path.stat().st_size > 95 * 1024 * 1024 or abs_path.open('rb').read(5) != b'%PDF-':
            raise SyncError('手動 PDF 格式不正確或超過 95MB：' + rel)
        out[docid] = rel
    return out


def write_binary(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def maybe_custom_cover(api, item, folder):
    if not item.get('hasCustomCover'): return None
    result = api_call(api, action='cover', id=item['id'])
    data = decode_image(result, MAX_COVER)
    rel = f'assets/published/{item["id"]}/cover.jpg'
    write_binary(folder / rel, data)
    return rel


def item_base(src):
    out = {k: str(src.get(k, '') or '')[:limit] for k,limit in {
        'id':36,'title':150,'category':60,'description':500,'status':20,'mode':20}.items()}
    out['issue'] = normalize_issue(src.get('issue'))
    if not UUID_RE.fullmatch(out['id']):
        raise SyncError('公開 API 回傳了不合法的刊物 UUID')
    if not out['title']: raise SyncError('公開刊物沒有名稱：' + out['id'])
    if out['mode'] not in ('pdf','document','link'): raise SyncError('不支援的刊物模式')
    if out['status'] != 'published': raise SyncError('不得同步未發布刊物')
    return out


def make_site(root, out, api, max_workers=3, test_run=False):
    if root.resolve() == out.resolve() or root.resolve().is_relative_to(out.resolve()):
        raise SyncError('輸出資料夾不能與來源資料夾相同')
    if out.exists(): shutil.rmtree(out)
    out.mkdir(parents=True)
    for filename in ('index.html','.nojekyll'):
        src = root / filename
        if src.exists(): shutil.copy2(src, out / filename)
    if not (out / 'index.html').is_file(): raise SyncError('找不到 V13.4 測試版 index.html')
    if (root/'assets').is_dir(): shutil.copytree(root/'assets', out/'assets', dirs_exist_ok=True)
    overrides = read_manual_overrides(root)
    data = api_call(api)
    if not isinstance(data.get('items'), list): raise SyncError('公開 API 未回傳 items 陣列')
    sources = data['items']
    # 避免不小心因 API 資料不完整，把已公開刊物全部清空。
    if not sources and os.environ.get('ALLOW_EMPTY_PUBLISH') != 'yes':
        raise SyncError('API 回傳 0 筆刊物，停止部署以免清空網站。若確實要清空，設 ALLOW_EMPTY_PUBLISH=yes')
    items, ids = [], set()
    for source in sources:
        if not isinstance(source,dict): raise SyncError('刊物資料不是物件')
        if source.get('status') != 'published': continue
        item = item_base(source)
        ident = item['id']
        if ident in ids: raise SyncError('API 回傳重複刊物 ID')
        ids.add(ident)
        base_rel = f'assets/published/{ident}'
        if item['mode'] == 'pdf':
            count = int(source.get('pageCount') or 0)
            if count < 1 or count > 30: raise SyncError('翻頁書頁數必須是 1–30 頁：'+item['title'])
            LOG.info('同步翻頁刊物：%s（%d 頁）',item['title'],count)
            images = [None] * count
            def download_page(index):
                obj = api_call(api, action='page', id=ident, page=index)
                return index,decode_image(obj,MAX_IMAGE)
            with ThreadPoolExecutor(max_workers=min(max_workers,count)) as pool:
                futures = [pool.submit(download_page,i) for i in range(count)]
                for fut in as_completed(futures):
                    i,raw = fut.result()
                    rel = f'{base_rel}/page-{i+1:03d}.jpg'
                    write_binary(out/rel,raw)
                    images[i] = rel
                    LOG.info('  頁面 %s %d/%d',ident,i+1,count)
            item['pages'] = images
            item['pageCount'] = count
            item['coverUrl'] = maybe_custom_cover(api,source,out) or images[0]
        elif item['mode'] == 'document':
            count = int(source.get('pageCount') or 0)
            item['pageCount'] = count
            if ident in overrides:
                # 本地 assets/manual/ 已被上方的 copytree 複製到 _site。
                item['url'] = overrides[ident]
                LOG.info('使用手動 PDF：%s',item['title'])
            else:
                LOG.info('同步原始 PDF：%s',item['title'])
                raw = fetch_public_pdf(str(source.get('url','')))
                rel = f'{base_rel}/document.pdf'
                write_binary(out/rel, raw)
                item['url'] = rel
            item['coverUrl'] = maybe_custom_cover(api,source,out) or ''
        elif item['mode'] == 'link':
            item['url'] = ensure_https_url(source.get('url'))
            # 外部連結刊物的自訂封面，同樣轉存 GitHub 靜態檔案。
            if source.get('hasCustomCover'):
                item['coverUrl'] = maybe_custom_cover(api,source,out)
            elif source.get('coverUrl'):
                # 原本外部網址封面可能來自 Google，要避免讓 Chrome 再去讀取 GAS。
                # 不主動下載未驗證的第三方圖片，沒有封面時用文字封面。
                item['coverUrl'] = ''
            else: item['coverUrl'] = ''
        items.append(item)
    manifest = {'schemaVersion':1,'generatedAt':datetime.now(TZ_TW).isoformat(timespec='seconds'),'items':items}
    (out/'publications.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    total = sum(f.stat().st_size for f in out.rglob('*') if f.is_file())
    if total > MAX_SITE: raise SyncError('靜態網站超過 900MB，請改用物件儲存方案')
    LOG.info('成功建立靜態閱讀館：%d 本刊物，共 %.2f MB',len(items),total/1048576)
    if test_run: LOG.info('測試輸出資料夾：%s',out)
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--api-url',required=True)
    ap.add_argument('--root',type=Path,default=Path('.'))
    ap.add_argument('--out',type=Path,default=Path('_site'))
    ap.add_argument('--max-workers',type=int,default=3)
    args=ap.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    try:
        make_site(args.root,args.out,args.api_url,max_workers=max(1,min(args.max_workers,5)))
    except Exception as exc:
        LOG.error('同步失敗，停止發布，既有公開網站不會被覆蓋：%s',exc)
        sys.exit(1)

if __name__=='__main__': main()
