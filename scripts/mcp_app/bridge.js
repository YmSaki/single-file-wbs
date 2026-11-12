// WBS Viewer を MCP App（Claude などの会話の中の iframe）として動かす橋渡し。
// scripts/mcp_server.py が、配る直前に wbs_viewer.html の </body> の手前へ差し込む。
// 製品本体は変えない。ここがやるのは次の5つだけ：
//   1. ホストと話す（JSON-RPC 2.0 over postMessage・MCP Apps 2026-01-26・SDK 不使用＝依存ゼロ）
//   2. ファイルハンドルの代役を渡す（読む＝wbs_read／書く＝wbs_save のツール呼び出し）
//   3. alert / confirm / prompt の代役（iframe の sandbox はダイアログを出せない）
//   4. 大きさと表示モード（会話の中の高さ・全画面）
//   5. 外部リンクをホストに開いてもらう（sandbox は新しいタブを開けない）
// Why not File System Access API のまま: sandbox の iframe からは OS のファイルに触れない。
// Why not 本体に分岐を足す: 本体は file:// で開く 1 枚のまま保つ（ADR-0001）。ハンドルを
//   差し替えるだけなら、読込・編集・自動保存は本体のコードがそのまま動く。
(function(){
  "use strict";
  if(window.parent===window)return;             // 直接開いた時は何もしない＝ただのビューア

  const PROTOCOL="2026-01-26";
  const INLINE_HEIGHT=640;                      // 会話の中に置く時の高さ（px）
  const CONFIRM_MS=10000;                       // 確認：同じ操作のやり直しを待つ時間
  const PROMPT_MS=120000;                       // 入力：受け取った答えを覚えておく時間
  const TEARDOWN_WAIT_MS=2500;                  // 閉じる前に、書きかけの保存を待つ上限
  const $=id=>document.getElementById(id);
  const isJa=()=>!(window.__PM&&window.__PM.lang&&window.__PM.lang()==="en");
  const L=(ja,en)=>isJa()?ja:en;

  // ---------- 1. ホストと話す ----------
  let nextId=1; const waiting=new Map();
  const post=m=>window.parent.postMessage(m,"*");
  const request=(method,params)=>new Promise((resolve,reject)=>{
    const id=nextId++; waiting.set(id,{resolve,reject});
    post({jsonrpc:"2.0",id,method,params:params||{}});
  });
  const notify=(method,params)=>post({jsonrpc:"2.0",method,params:params||{}});
  const textOf=r=>((r&&r.content)||[]).filter(c=>c&&c.type==="text").map(c=>c.text).join("\n");
  async function callTool(name,args){
    const r=await request("tools/call",{name,arguments:args});
    if(r&&r.isError)throw new Error(textOf(r)||name+" failed");
    return r||{};
  }

  window.addEventListener("message",ev=>{
    if(ev.source!==window.parent)return;        // 親（ホスト）以外からの声は聞かない
    const m=ev.data;
    if(!m||m.jsonrpc!=="2.0")return;
    if(typeof m.method==="string"){
      if(m.id===undefined||m.id===null)onNotification(m.method,m.params||{});
      else onRequest(m);
      return;
    }
    const w=waiting.get(m.id); if(!w)return;
    waiting.delete(m.id);
    if(m.error)w.reject(new Error((m.error&&m.error.message)||"request failed"));
    else w.resolve(m.result);
  });

  function onRequest(m){
    const reply=result=>post({jsonrpc:"2.0",id:m.id,result});
    if(m.method==="ping"){reply({});return;}
    if(m.method==="ui/resource-teardown"){      // 閉じられる前に、書きかけを流し切る
      settleSaves().then(()=>reply({}));
      return;
    }
    post({jsonrpc:"2.0",id:m.id,error:{code:-32601,message:"Method not found: "+m.method}});
  }

  function onNotification(method,params){
    if(method==="ui/notifications/tool-input"){
      if(params.arguments&&typeof params.arguments.file==="string")hintFile=params.arguments.file;
    }else if(method==="ui/notifications/tool-result"){
      onToolResult(params);
    }else if(method==="ui/notifications/tool-cancelled"){
      say(L("表示が取り消されました。","The request was cancelled."));
    }else if(method==="ui/notifications/host-context-changed"){
      applyContext(params);
    }
  }

  // ---------- 2. ファイルハンドルの代役 ----------
  const state={file:null,version:null,size:0,readOnly:false};
  let hintFile=null, inflight=Promise.resolve();

  function makeHandle(){
    const file=state.file, name=file.split("/").pop();
    const h={
      kind:"file", name,
      // 本体は保存のたびに getFile() で更新時刻を見る。そこでは通信しない（中身を読むのは text() だけ）。
      // 外部変更の検知は更新時刻ではなく、サーバー側の版（中身のハッシュ）の突き合わせで行う。
      getFile:async()=>({
        name, size:state.size, lastModified:1,
        text:async()=>{
          const r=await callTool("wbs_read",{file});
          const txt=textOf(r);
          state.version=(r.structuredContent&&r.structuredContent.version)||null;
          state.size=txt.length;
          return txt;
        },
      }),
      queryPermission:async()=>"granted",
      requestPermission:async()=>"granted",
      isSameEntry:async other=>other===h,
    };
    if(!state.readOnly)h.createWritable=async()=>{
      let buf=null;
      return {
        write:async data=>{buf=String(data);},
        abort:async()=>{buf=null;},
        close:()=>{
          if(buf===null)return Promise.resolve();
          const p=callTool("wbs_save",{file,text:buf,baseVersion:state.version}).then(r=>{
            if(r.structuredContent&&r.structuredContent.version)state.version=r.structuredContent.version;
          });
          inflight=p.catch(()=>{});             // 失敗は本体が「保存失敗」として出す。ここでは待つだけ
          return p;
        },
      };
    };
    return h;
  }

  function onToolResult(res){
    const sc=(res&&res.structuredContent)||{};
    if(res&&res.isError){say(textOf(res)||L("開けませんでした。","Could not open the file."));return;}
    const file=typeof sc.file==="string"?sc.file:hintFile;
    if(!file){say(L("表示するファイルが分かりません。","No file to show."));return;}
    state.file=file; state.version=null; state.readOnly=!!sc.readOnly;
    document.documentElement.classList.toggle("mcp-ro",state.readOnly);
    const handle=makeHandle();
    // 本体の「ファイルを開く」は showOpenFilePicker() の返すハンドルを読む。
    // その返事を差し替えてボタンを押す＝読込から先は本体の経路（テストのフェイクハンドルと同じ入口）。
    window.showOpenFilePicker=async()=>[handle];
    $("openBtn").click();
  }

  async function settleSaves(){
    try{window.dispatchEvent(new Event("beforeunload"));}catch(_){}   // 本体の「離脱前に流す」を借りる
    await new Promise(r=>setTimeout(r,60));
    await Promise.race([inflight,new Promise(r=>setTimeout(r,TEARDOWN_WAIT_MS))]);
  }

  // ---------- 3. ダイアログの代役 ----------
  // sandbox の iframe では alert は出ず、confirm は常に false、prompt は null になる。
  // どれも同期の関数なので、画面内の帯で知らせて「同じ操作をもう一度」で確定する形にする。
  const css=document.createElement("style");
  css.textContent=
    "#openBtn{display:none}"                                              // 開くのはホスト側のツール
    +"html.mcp-ro #editBtn{display:none}"
    +"#mcpBar{position:fixed;left:50%;top:44px;transform:translateX(-50%);z-index:2147483000;"
    +"max-width:min(680px,calc(100vw - 24px));display:none;gap:8px;align-items:flex-start;"
    +"padding:8px 10px;background:#fff7d6;border:1px solid #e3d9a8;border-radius:6px;"
    +"box-shadow:0 2px 10px rgba(0,0,0,.18);color:#4a3f10;font-size:12px;line-height:1.5}"
    +"#mcpBar.show{display:flex}#mcpBar .t{white-space:pre-wrap;word-break:break-word}"
    +"#mcpBar .h{color:#8a6d00;font-weight:600}#mcpBar input{font:inherit;padding:2px 6px;min-width:180px}"
    +"#mcpBar button{font:inherit;padding:1px 8px;cursor:pointer}"
    +"#topbar #mcpFs{border-color:transparent;background:transparent;color:var(--muted)}"   // 表示設定＝#langBtn と同じ弱さ
    +"#topbar #mcpFs:hover{border-color:var(--btn-border);background:#fff}";
  document.head.appendChild(css);

  const bar=document.createElement("div"); bar.id="mcpBar"; bar.setAttribute("role","status");
  document.body.appendChild(bar);
  let barTimer=null;
  function hideBar(){clearTimeout(barTimer);bar.classList.remove("show");bar.textContent="";}
  function showBar(text,hint,extra,ms){
    clearTimeout(barTimer); bar.textContent="";
    const body=document.createElement("div");
    const t=document.createElement("div"); t.className="t"; t.textContent=text; body.appendChild(t);
    if(hint){const h=document.createElement("div"); h.className="h"; h.textContent=hint; body.appendChild(h);}
    if(extra)body.appendChild(extra);
    const x=document.createElement("button"); x.type="button"; x.textContent="✕";
    x.title=L("閉じる","Close"); x.addEventListener("click",hideBar);
    bar.appendChild(body); bar.appendChild(x); bar.classList.add("show");
    if(ms)barTimer=setTimeout(hideBar,ms);
  }
  const say=text=>showBar(String(text),null,null,0);

  let armed=null, answered=null;
  const now=()=>performance.now();
  window.alert=msg=>{say(msg);};
  window.confirm=msg=>{
    msg=String(msg);
    if(armed&&armed.msg===msg&&now()-armed.at<CONFIRM_MS){armed=null;hideBar();return true;}
    armed={msg,at:now()};
    showBar(msg,L("実行するには、同じ操作をもう一度行ってください。",
                  "Repeat the same action to confirm."),null,CONFIRM_MS);
    return false;
  };
  window.prompt=(msg,def)=>{
    msg=String(msg);
    if(answered&&answered.msg===msg&&now()-answered.at<PROMPT_MS){
      const v=answered.value; answered=null; hideBar(); return v;
    }
    const row=document.createElement("div");
    const inp=document.createElement("input"); inp.type="text"; inp.value=def==null?"":String(def);
    const ok=document.createElement("button"); ok.type="button"; ok.textContent="OK";
    const accept=()=>{
      answered={msg,value:inp.value,at:now()};
      showBar(msg+" → "+inp.value,L("受け取りました。同じ操作をもう一度行うと反映します。",
                                    "Got it. Repeat the same action to apply."),null,PROMPT_MS);
    };
    ok.addEventListener("click",accept);
    inp.addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();accept();}});
    row.appendChild(inp); row.appendChild(document.createTextNode(" ")); row.appendChild(ok);
    showBar(msg,null,row,0);
    setTimeout(()=>{try{inp.focus();inp.select();}catch(_){}} ,0);
    return null;
  };

  // 行へ飛ぶ時、本体は URL の # を書き換える。iframe の中では拒まれることがあるので、失敗しても先へ進める。
  ["pushState","replaceState"].forEach(k=>{
    const orig=history[k];
    history[k]=function(){try{return orig.apply(history,arguments);}catch(_){}};
  });

  // ---------- 4. 大きさと表示モード ----------
  let displayMode="inline", canFullscreen=false, dims={};
  function applyContext(ctx){                    // 文脈は差分で届く。覚えている値に重ねる
    if(!ctx)return;
    if(Array.isArray(ctx.availableDisplayModes))canFullscreen=ctx.availableDisplayModes.indexOf("fullscreen")>=0;
    if(typeof ctx.displayMode==="string")displayMode=ctx.displayMode;
    if(ctx.containerDimensions&&typeof ctx.containerDimensions==="object")dims=ctx.containerDimensions;
    // 高さをホストが決めていない時だけ、欲しい高さを伝える（本体は画面いっぱいに敷くレイアウト）。
    if(!("height" in dims)&&displayMode==="inline"){
      const h=dims.maxHeight?Math.min(dims.maxHeight,INLINE_HEIGHT):INLINE_HEIGHT;
      notify("ui/notifications/size-changed",{height:h});
    }
    drawFsButton();
  }
  function drawFsButton(){
    let b=$("mcpFs");
    if(!canFullscreen){if(b)b.remove();return;}
    if(!b){
      b=document.createElement("button"); b.id="mcpFs"; b.type="button";
      b.addEventListener("click",async()=>{
        try{
          const r=await request("ui/request-display-mode",{mode:displayMode==="fullscreen"?"inline":"fullscreen"});
          if(r&&typeof r.mode==="string")displayMode=r.mode;
        }catch(_){}
        drawFsButton();
      });
      $("langBtn").insertAdjacentElement("beforebegin",b);
    }
    const full=displayMode==="fullscreen";
    b.textContent=full?L("⤡ 戻す","⤡ Exit"):L("⤢ 全画面","⤢ Full screen");
  }
  $("langBtn").addEventListener("click",()=>setTimeout(drawFsButton,0));   // 言語を替えたらラベルも替える

  // ---------- 5. 外部リンク ----------
  document.addEventListener("click",e=>{
    const a=e.target&&e.target.closest&&e.target.closest("a[href]");
    if(!a||e.defaultPrevented)return;            // 本体が止めたクリック（リンクの ✕ など）は開かない
    const href=a.getAttribute("href")||"";
    if(!/^https?:\/\//i.test(href))return;       // 画面内の移動（#wbs= など）は本体に任せる
    e.preventDefault();
    request("ui/open-link",{url:href}).catch(()=>say(L("リンクを開けませんでした: ","Could not open the link: ")+href));
  });

  // ---------- 起動 ----------
  request("ui/initialize",{
    protocolVersion:PROTOCOL,
    appInfo:{name:"single-file-wbs",version:"0.1.0"},
    appCapabilities:{availableDisplayModes:["inline","fullscreen"]},
  }).then(res=>{
    notify("ui/notifications/initialized",{});
    applyContext((res&&res.hostContext)||{});
  }).catch(e=>say(L("ホストに接続できませんでした: ","Could not connect to the host: ")+e.message));
})();
