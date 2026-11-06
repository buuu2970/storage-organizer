
'use strict';
const $=id=>document.getElementById(id);
const navs={home:['⌂','ホーム'],groups:['◎','整理候補'],capacity:['◴','容量の内訳'],files:['▦','ファイル一覧'],images:['▧','画像の整理'],settings:['⚙','設定']};
let sourceLabel='選択したファイル', scanNote='';
let rows=[],groups=[],page='home',demo=true,scenario='normal',active=0,goal=0,results=[],busy=false,progress=0,stop=false,status='',manual=false,pair=[],inspected=null;
const esc=x=>String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const size=b=>b<1024?`${b} B`:b<1048576?`${+(b/1024).toFixed(1)} KB`:`${+(b/1048576).toFixed(1)} MB`;
const sum=a=>a.reduce((v,r)=>v+r.size,0), selected=()=>rows.filter(r=>r.take&&!r.protect&&!r.done);
const button=(name,action,cls='',disabled=false)=>`<button class="${cls}" onclick="${action}" ${disabled?'disabled':''}>${name}</button>`;
const metric=(title,value,note)=>`<article class="metric"><h3>${title}</h3><strong>${value}</strong><small>${note}</small></article>`;
const locked=()=>demo&&scenario==='locked';
function sample(mode='normal'){
  release(); demo=true; scenario=mode; results=[]; manual=false; pair=[]; goal=0;inspected=null;
  rows=['ボス戦 第1回.mp4','ボス戦 第1回 (1).mp4','ボス戦 第1回 (2).mp4','配信.mp4'].map((name,i)=>({id:i,name,size:i===3?2000:3000,kind:'録画',path:i===1||i===2?'ビデオ / バックアップ':'ビデオ / 録画',take:i===1||i===2}));
  groups=mode==='empty'?[]:[rows.slice(0,3)]; if(!groups.length){rows=rows.slice(0,1);rows[0].take=false}
  status='サンプルの調査完了';active=0;go('home');
}
function release(){for(const r of rows)if(r.url)URL.revokeObjectURL(r.url)}
function go(p){if(busy)return;page=p;inspected=null;render();window.scrollTo(0,0)}
function heading(title,desc=''){return `<div class="top"><h1>${title}</h1><span class="pill">${demo?'サンプルデータ':esc(sourceLabel)}</span></div>${desc?`<p>${desc}</p>`:''}`}
function steps(n){return `<div class="steps">${['候補を確認','比較して選ぶ','最終確認'].map((t,i)=>i===n?`<b>${i+1}. ${t}</b>`:`<span>${i+1}. ${t}</span>`).join(' → ')}</div>`}
function banner(){
 if(locked())return '<div class="note warn">保護記録の読み込み失敗を再現中です。確認のみ可能です。</div>';
 if(demo&&scenario==='recovered')return '<div class="note">サンプルの保護記録をバックアップから復旧した状態です。</div>';
 return '';
}
function openDialog(html){$('dialogBody').innerHTML=html;$('dialog').showModal()}
function scope(){openDialog(`<h2>調査範囲</h2><p>${demo?'練習用の録画フォルダ':esc(sourceLabel)+' · '+rows.length+' 件'+(scanNote?'。'+esc(scanNote):'')}を表示しています。</p>`)}
function target(){return `<label>目標 <input id="target" type="number" min="0" step="any" placeholder="任意" value="${goal||''}"> GB</label> ${button('提案する','applyGoal()')}`}
function applyGoal(){const f=$('target');if(!f.reportValidity())return;goal=Number(f.value);go('groups')}
function eligible(){return groups.filter(g=>g.filter(r=>!r.done).length>1&&!g.every(r=>r.done||r.hold||r.protect||r.retained))}
function scanActions(){
 return button('ファイルを選ぶ','picker.click()')+' '+button('フォルダを調べる','folders.click()')+' '+button('PC全体を調べる','pcGuide()');
}
function pcGuide(){
 const href=location.protocol==='file:'?'https://recsort-video-review.kaihatsukyouta.chatgpt.site/RecSort-PC.zip':'./RecSort-PC.zip';
 openDialog('<h2>PC全体の録画を調べる</h2><p>Windows用ツールが固定ドライブを読み取り、同じ内容の動画を削除候補としてまとめます。</p><ol><li><a href="'+href+'" download>Windows用の調査ツール（ZIP）を保存</a></li><li>ZIPを展開し、RecSort-PC.exe を開く</li><li>対象ドライブを確認して Enter を押す</li></ol><p>完了すると、調査結果がブラウザで開きます。結果はこのPC内に保存されます。システム領域、アクセスできない場所、オンライン専用ファイルは対象外です。</p>');
}
function home(){
 const g=eligible()[0],videos=rows.filter(r=>r.kind==='録画'), imgs=rows.filter(r=>r.kind==='画像');
 return heading('ホーム','重複録画を見比べて、残す1本を選びましょう。')+`<div class="bar"><small>調査範囲　${demo?'サンプルのビデオ':esc(sourceLabel)}</small>${button('調査範囲を見る','scope()')}</div>`+banner()+`<div class="actions">${button('整理候補を見る',"go('groups')",'primary')}${scanActions()}</div>
 <section class="hero"><div class="film" aria-hidden="true"><b>▷</b></div><div><small>おすすめの整理</small><h2>${g?esc(g[0].name):'整理候補はありません'}</h2><p>${g?'完全重複 · '+g.length+' 本 ｜ 整理見込み '+size(sum(g.filter(r=>r.take&&!r.done))):'選択した範囲の確認が完了しました。'}</p></div>${g?button('比較して選ぶ →',`compare(${groups.indexOf(g)})`):''}</section>
 <section class="grid">${metric('ドライブの空き容量',demo?'8.7 GB':'取得対象外',demo?'サンプルの空き容量':'ブラウザではドライブを調査しません')}${metric('調査済みの録画',size(sum(videos)),videos.length+' 本のファイルサイズ')}<article class="metric"><h3>目標容量から整理</h3>${target()}</article>${metric('容量の内訳',videos.length?'動画':'画像',`<a href="#" onclick="go('capacity');return false">容量の内訳を見る</a>`)}${metric('完全一致の重複',size(sum(selected())),eligible().length+' グループの候補')}${metric('画像',imgs.length+' 件',size(sum(imgs)))}</section>`;
}
function compare(i){manual=false;active=i;go('compare')}
function candidates(){
 const list=eligible(); const sel=selected();
 return steps(0)+heading('候補を確認','内容が完全に一致する録画をグループごとに表示します。')+banner()+`<div class="bar">${target()}</div><div class="grid">${metric('選択中',size(sum(sel)),sel.length+' 本')}${metric('完全重複',list.length+' グループ','少なくとも1本は残します')}${metric('目標容量',goal?goal+' GB':'指定なし',goal?(sum(sel)>=goal*1e9?'目標に到達':'候補だけでは目標に届きません'):'必要に応じて指定できます')}</div>`+list.map(g=>`<section class="box list"><div class="group row"><strong>完全重複 · ${g.length} 本</strong>${button('比較して選ぶ',`compare(${groups.indexOf(g)})`)}</div>${g.filter(r=>!r.done).map(r=>`<div class="item"><input class="check" aria-label="${esc(r.name)}を候補に含める" type="checkbox" ${r.take?'checked':''} ${locked()||r.protect||!r.take&&g.filter(x=>!x.done&&!x.take).length===1?'disabled':''} onchange="toggle(${r.id},this.checked)"><button onclick="detail(${r.id})">${esc(r.name)}</button><span>${size(r.size)}</span><span>${r.protect?'保護中':r.take?'整理する候補':'残す'}</span></div>`).join('')}</section>`).join('')+(!list.length?'<div class="box empty"><h2>整理候補はありません</h2><p>保護・保留した録画は候補から外れます。</p></div>':'')+`<div class="bar"><b>選択中 ${sel.length} 本 · ${size(sum(sel))}</b>${button('最終確認へ →',"go('final')",'primary',locked()||!sel.length)}</div>`;
}
function toggle(id,v){if(locked())return;const r=rows.find(x=>x.id===id),g=groups.find(g=>g.includes(r));if(v&&(!g.some(x=>x!==r&&!x.done&&!x.take)||r.protect))return;r.take=v;render()}
function media(r){if(!r.file&&!demo)return '<div class="scene">PCで検出した録画<br>保存場所を下に表示しています</div>';if(!r.file)return '<div class="scene">サンプル録画<br>プレビューなし</div>';if(!r.url)r.url=URL.createObjectURL(r.file);return r.kind==='画像'?`<img class="picture" src="${r.url}" alt="${esc(r.name)}">`:`<video controls preload="metadata" src="${r.url}" onerror="this.replaceWith(document.createTextNode('この録画はブラウザで再生できません。'))"></video>`}
function detail(id){const r=rows.find(x=>x.id===id);openDialog(`<h2>${esc(r.name)}</h2>${media(r)}<p>${size(r.size)} · ${esc(r.path)}</p>`)}
function comparison(){
 const g=manual?pair:groups[active],action=manual?'判断を記録':'最終確認へ →';
 return steps(1)+heading(manual?'選んだ録画を比較':`完全重複を比較（${g.length} 本）`,manual?'再生して内容を確認できます。':'同じ内容の録画です。残したいファイルを選んでください。')+banner()+`<div class="cols">${g.filter(r=>!r.done).map(r=>`<article class="box ${(manual?r.manualKeep:!r.take)?'chosen':''}"><h3>${esc(r.name)}</h3><small>${r.protect?'保護中':r.hold?'保留':manual?(r.manualKeep?'残す録画':'比較対象'):r.take?'整理する候補':'残す録画'}</small>${media(r)}<p>${size(r.size)} · ${esc(r.path)}</p><div class="actions">${button('これを残す',`keep(${r.id})`,'primary',locked())}${button(r.protect?'保護を解除':'保護する',`protect(${r.id})`,'',locked())}</div></article>`).join('')}</div><div class="bar">${button('← 戻る',manual?"go('files')":"go('groups')")}<div class="actions">${button('すべて残す','defer(false)','',locked())}${button('保留','defer(true)','',locked())}${button(action,manual?"go('files')":"go('final')",'primary',locked()||!manual&&!selected().length)}</div></div>`;
}
function keep(id){if(locked())return;for(const r of manual?pair:groups[active]){r.hold=false;if(manual)r.manualKeep=r.id===id;else r.take=r.id!==id&&!r.protect&&!r.done}if(manual){status='残す判断を記録：'+rows.find(r=>r.id===id).name;go('files')}else render()}
function protect(id){if(locked())return;const r=rows.find(x=>x.id===id);r.protect=!r.protect;r.take=false;render()}
function defer(hold){if(locked())return;for(const r of manual?pair:groups[active]){r.take=false;r.hold=hold;r.retained=!hold;if(manual)r.manualKeep=!hold}go(manual?'files':'groups')}
function table(arr){return arr.length?`<div class="scroll"><table><thead><tr><th>ファイル</th><th>保存場所</th><th>容量</th></tr></thead><tbody>${arr.map(r=>`<tr><td>${esc(r.name)}</td><td>${esc(r.path)}</td><td>${size(r.size)}</td></tr>`).join('')}</tbody></table></div>`:'<p>対象はありません。</p>'}
function finalPage(){const sel=selected(),keepers=groups.filter(g=>g.some(r=>sel.includes(r))).flat().filter(r=>!r.take&&!r.done);
return steps(2)+heading('最終確認',`${sel.length} 本・合計 ${size(sum(sel))} を確認してください。`)+banner()+`<div class="cols"><div class="box"><h2>${demo?'移動対象（サンプル）':'整理する候補'}</h2>${table(sel)}</div><div class="box"><h2>残すファイル</h2>${table(keepers)}</div></div><div class="note">${demo?'サンプルのごみ箱移動を試します。':'選んだ結果をこのページ内に記録します。'}実ファイルは変更しません。</div>${busy?`<progress max="${sel.length||1}" value="${progress}"></progress><p>処理中 ${progress} / ${sel.length}</p>`:''}<div class="bar">${button('← 戻って選び直す',"go('groups')",'',busy)}${busy?button('中断','stop=true'):button(demo?'移動を試す（サンプル）':'整理結果を記録','execute()','danger',locked()||!sel.length)}</div>`}
async function execute(){if(busy||locked())return;const batch=selected();if(!batch.length)return;busy=true;stop=false;results=[];progress=0;render();
for(const r of batch){await new Promise(resolve=>setTimeout(resolve,500));if(stop)break;const failed=demo&&scenario==='failure'&&progress===0;results.push({r,text:failed?'失敗（サンプル）':demo?'移動済み（サンプル）':'判断を記録',ok:!failed});progress++;render()}
for(const x of results)if(x.ok){x.r.done=true;x.r.take=false}
busy=false;status=stop?'処理を中断しました':'整理結果を記録しました';go('result')}
function result(){return heading('整理の結果')+`<div class="note">${esc(status)} · 完了 ${results.filter(x=>x.ok).length} 件 / 失敗 ${results.filter(x=>!x.ok).length} 件</div><div class="box">${results.map(x=>`<p><strong>${esc(x.text)}</strong>　${esc(x.r.name)}</p>`).join('')||'処理したファイルはありません。'}</div><div class="bar">${button('ホームへ',"go('home')")}${button('次の候補へ',"go('groups')",'primary')}</div>`}
function library(images){const arr=rows.filter(r=>!images||r.kind==='画像');return heading(images?'画像の整理（補助）':'ファイル一覧')+`<div class="bar">${button('ファイルを選ぶ','picker.click()')} ${!images?button('選んだ2本を比較','manualCompare()','primary',pair.length!==2):''}</div><div class="box">${arr.map(r=>`<div class="item">${r.kind==='録画'&&!images?`<input type="checkbox" class="check" aria-label="${esc(r.name)}を比較" ${pair.includes(r)?'checked':''} ${!pair.includes(r)&&pair.length>=2?'disabled':''} onchange="pairCheck(${r.id},this.checked)">`:'<span></span>'}<button onclick="detail(${r.id})">${esc(r.name)}</button><span>${size(r.size)}</span><span>${r.protect?'保護中':r.hold?'保留':r.manualKeep?'残す':r.done?'整理済み':r.kind}</span></div>`).join('')||'<p class="empty">画像はありません。</p>'}</div>`}
function pairCheck(id,on){const r=rows.find(r=>r.id===id);pair=on?[...pair,r]:pair.filter(x=>x!==r);render()}
function manualCompare(){manual=true;go('compare')}
function settings(){return heading('設定')+`<section class="box"><h2>サンプルで画面を確認</h2><p>表示したい状態を選ぶと、練習用の録画で最初から試せます。</p><div class="actions">${[['normal','通常'],['empty','候補なし'],['failure','一部移動に失敗'],['recovered','保護記録を復旧'],['locked','保護記録を読めない']].map(([k,t])=>button(t,`sample('${k}')`)).join('')}</div></section><section class="box"><h2>自分のファイルで調べる</h2><p>同じ容量の録画を端末内で読み比べ、完全一致したものを候補にします。判断はページを開いている間だけ有効です。</p>${scanActions()}</section>`}
function render(){const views={home,groups:candidates,compare:comparison,final:finalPage,result,settings,files:()=>library(false),images:()=>library(true),capacity:()=>heading('容量の内訳')+`<div class="grid">${['録画','画像'].map(k=>metric(k,size(sum(rows.filter(r=>r.kind===k))),rows.filter(r=>r.kind===k).length+' 件')).join('')}${metric('整理を選択中',size(sum(selected())),selected().length+' 本')}</div>`};
 $('nav').innerHTML=Object.entries(navs).map(([p,[ic,name]])=>`<button ${busy?'disabled':''} class="${p===page||p==='groups'&&['compare','final','result'].includes(page)?'active':''}" onclick="go('${p}')"><i>${ic}</i>${name}</button>`).join('');$('screen').innerHTML=views[page]();$('status').textContent=(busy?'◌　':'✓　')+status;}
async function equalFiles(a,b){for(let start=0;start<a.size;start+=1024*1024){const end=start+1024*1024;const [x,y]=await Promise.all([a,b].map(f=>f.slice(start,end).arrayBuffer().then(v=>new Uint8Array(v))));if(!x.every((v,i)=>v===y[i]))return false}return true}
const videoExtensions=/\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|mpg|mpeg|ts|m2ts|mts|m2t|vob|3gp)$/i;
async function classify(file){
 if(/\.ts$/i.test(file.name)){
   const bytes=new Uint8Array(await file.slice(0,620).arrayBuffer());
   return [188,192,204].some(step=>bytes.some((v,i)=>i<step&&v===71&&bytes[i+step]===71&&bytes[i+2*step]===71))?'録画':null;
 }
 if(file.type.startsWith('video/')||videoExtensions.test(file.name))return '録画';
 return file.type.startsWith('image/')?'画像':null;
}
function useScan(fresh,sets,label,note){
 release();rows=fresh;groups=sets;demo=false;manual=false;pair=[];goal=0;scenario='normal';results=[];
 sourceLabel=label;scanNote=note;status='調査完了 · '+rows.length+' 件 · 完全重複 '+groups.length+' グループ'+(note?' ｜ '+note:'');page='home';
}
async function scanSelection(e,folder){
 if(busy)return;
 const files=Array.from(e.target.files);e.target.value='';
 if(!files.length)return;
 busy=true;document.querySelector('main').inert=true;$('nav').inert=true;
 try{
   const fresh=[],buckets=new Map();let unreadable=0;
   for(const f of files){
     let kind;try{kind=await classify(f)}catch{unreadable++;continue}
     if(!kind||(folder&&kind!=='録画'))continue;
     const rel=f.webkitRelativePath||'';
     const row={file:f,id:fresh.length,name:f.name,size:f.size,kind,path:rel?rel.slice(0,rel.lastIndexOf('/')):'選択したファイル'};
     fresh.push(row);
     if(kind==='録画'&&f.size){if(!buckets.has(f.size))buckets.set(f.size,[]);buckets.get(f.size).push(row)}
   }
   const duplicates=[];let compared=0;
   for(const bucket of buckets.values()){
     if(bucket.length<2)continue;
     const sets=[];
     for(const r of bucket){
       $('status').textContent='同じサイズの録画を照合中 · '+(++compared)+' 本';
       let same;
       try{for(const g of sets){if(await equalFiles(g[0].file,r.file)){same=g;break}}}catch{unreadable++;continue}
       if(same){same.push(r);r.take=true}else sets.push([r]);
     }
     duplicates.push(...sets.filter(g=>g.length>1));
   }
   useScan(fresh,duplicates,folder?'フォルダ：'+(files[0].webkitRelativePath||'').split('/')[0]:'選択したファイル',unreadable?'照合できなかったファイル '+unreadable+' 件':'');
 }catch{status='読み込みに失敗しました。ファイルを選び直してください。'}
 finally{busy=false;document.querySelector('main').inert=false;$('nav').inert=false;render()}
}
$('picker').onchange=e=>scanSelection(e,false);
$('folders').onchange=e=>scanSelection(e,true);
$('dialog').addEventListener('close',()=>$('dialogBody').replaceChildren());
const savedScan=$('scanData');
if(savedScan && savedScan.textContent){
 const report=JSON.parse(savedScan.textContent);
 const fresh=report.rows.map((r,id)=>({...r,id,kind:'録画',take:false}));
 const sets=report.groups.map(ids=>ids.map((id,i)=>{fresh[id].take=i>0;return fresh[id]}));
 useScan(fresh,sets,'PC全体：'+report.roots.join(' / '),'スキップ '+report.skipped+' 件');render();
}else sample();
