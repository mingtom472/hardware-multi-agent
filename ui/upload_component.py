"""Browser-to-FastAPI streaming upload without Streamlit's buffered file uploader."""

import json
from urllib.parse import urlsplit


def upload_html(api_url: str, max_upload_bytes: int | None = None) -> str:
    """Build an isolated uploader. Files stay in browser/File and are sent with XHR.

    No file contents pass through Streamlit, FileReader, base64, or Python memory.
    FastAPI checks the actual storage resources as it receives each stream.
    """
    parsed = urlsplit(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Upload API URL must be an HTTP(S) origin")
    config = json.dumps(
        {"api": api_url.rstrip("/"), "maxBytes": max_upload_bytes}, ensure_ascii=True
    ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return r"""<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8">
<style>
*{box-sizing:border-box}body{margin:0;font:14px Arial,"Microsoft YaHei",sans-serif;color:#243f3b;background:transparent}
.box{border:1px dashed #a9c8bb;border-radius:10px;background:#f6faf7;padding:20px}.title{font-weight:600;margin-bottom:8px}
.muted{font-size:12px;color:#72877d;line-height:1.7}input{max-width:100%;padding:12px 0}button{cursor:pointer;background:#17745c;color:white;border:0;border-radius:5px;padding:9px 16px;margin:8px 8px 8px 0}button.secondary{background:#e5ede7;color:#35634e}button:disabled{opacity:.5;cursor:not-allowed}progress{width:100%;height:16px;margin:10px 0}#status{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.8;max-height:160px;overflow:auto}.error{color:#ad4637}button:focus-visible,input:focus-visible{outline:3px solid #84c0ad;outline-offset:3px}
</style></head><body><div class="box"><div class="title">原始工程文件 · 浏览器直接流式上传</div>
<div class="muted">允许任意扩展名；不执行程序、不自动解压。大文件受磁盘、文件系统、网络及外部 API 的实际能力约束。上传成功不代表模型已读完文件。</div>
<label for="files">选择一个或多个文件</label><br><input id="files" type="file" multiple>
<div><button id="start">上传原件</button><button class="secondary" id="cancel" disabled>中断当前上传</button></div>
<progress id="progress" max="100" value="0" aria-label="当前文件上传进度"></progress><div id="status" role="status" aria-live="polite">等待选择文件。上传后点击工作台「刷新文件列表」。分块与断点续传属于后续版本，当前中断文件需重新上传。</div></div>
<script>
const config=__UPLOAD_CONFIG__;
const input=document.getElementById('files'),start=document.getElementById('start'),cancel=document.getElementById('cancel'),status=document.getElementById('status'),progress=document.getElementById('progress');
let current=null,aborted=false;
const size=n=>n<1024?`${n} B`:n<1048576?`${(n/1024).toFixed(1)} KiB`:`${(n/1048576).toFixed(1)} MiB`;
function append(message){status.textContent+=(status.textContent?'\n':'')+message;status.scrollTop=status.scrollHeight;}
function upload(file,index,total){return new Promise((resolve,reject)=>{
const xhr=new XMLHttpRequest();current=xhr;
xhr.open('POST',config.api+'/api/uploads?filename='+encodeURIComponent(file.name));xhr.setRequestHeader('Content-Type',file.type||'application/octet-stream');
xhr.upload.onprogress=event=>{if(event.lengthComputable){progress.value=event.loaded/event.total*100;progress.setAttribute('aria-valuetext',`${Math.round(progress.value)}%`);}};
xhr.onload=()=>{current=null;if(xhr.status>=200&&xhr.status<300){try{const record=JSON.parse(xhr.responseText);append(`✓ ${file.name} · ${size(record.size)} · SHA256 ${record.sha256}`);resolve(record);}catch{reject(new Error('上传返回了无法识别的响应'));}}else{let detail=`HTTP ${xhr.status}`;try{const body=JSON.parse(xhr.responseText);detail=typeof body.detail==='string'?body.detail:JSON.stringify(body.detail);}catch{}reject(new Error(detail));}};
xhr.onerror=()=>{current=null;reject(new Error('网络连接失败。确认本地 FastAPI 已启动，浏览器 API 地址可访问且 CORS 允许 Streamlit 的8501来源。'));};
xhr.onabort=()=>{current=null;reject(new Error('上传已中断；原始文件仍在本机，需重新上传。'));};
append(`[${index+1}/${total}] ${file.name} · ${size(file.size)}`);progress.value=0;
// Native File is streamed by the browser. Never read or base64-encode its contents.
xhr.send(file);
});}
start.onclick=async()=>{const files=Array.from(input.files||[]);if(!files.length){status.textContent='请先选择文件。';return;}aborted=false;start.disabled=true;input.disabled=true;cancel.disabled=false;status.textContent='';status.className='';try{for(let i=0;i<files.length;i++){if(aborted)break;if(config.maxBytes!==null&&files[i].size>config.maxBytes)throw new Error(`${files[i].name} 超出当前管理员配置限制 ${size(config.maxBytes)}；可在环境配置中调整。`);await upload(files[i],i,files.length);}if(!aborted)append('上传完成。点击工作台「刷新文件列表」，选择附件创建任务。');}catch(error){status.className='error';append('错误：'+error.message);}finally{start.disabled=false;input.disabled=false;cancel.disabled=true;}};
cancel.onclick=()=>{aborted=true;if(current)current.abort();};
</script></body></html>""".replace("__UPLOAD_CONFIG__", config)


def render_uploader(api_url: str, max_upload_bytes: int | None = None) -> None:
    import streamlit.components.v1 as components

    components.html(upload_html(api_url, max_upload_bytes), height=410, scrolling=True)
