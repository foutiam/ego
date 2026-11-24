// Tactile event review for the Ego workbench capture panel.
(function () {
 'use strict';
 const labels={contact_on:'接触响应开始',contact_off:'接触响应结束',region_on:'局部响应开始',region_off:'局部响应结束',grasp_candidate:'夹持候选 · 多指共触',release_candidate:'释放候选 · 共触结束'};
 const regions={any_taxel:'任一触点',thumb:'拇指',index:'食指',middle:'中指',ring:'无名指',pinky:'小指',palm:'掌部',thumb_and_other_finger:'拇指 + 其他手指'};
 const decisions={confirmed:'已确认',rejected:'已否定',unobservable:'画面无法判断',pending:'待确认'};
 const node=(tag,text,parent)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(parent)parent.appendChild(e);return e;};
 class TactileEventReview {
  constructor(container,onSeek,options={}){
   this.cachedState=options.cachedState;this.readOnly=!!options.readOnly;this.loadSaved=!!options.loadSaved;this.compact=!!options.compact;this.page=0;this.range=null;
   this.container=container;this.onSeek=onSeek;this.generation=0;this.time=0;this.loading=false;
   container.classList.add('tactile-event-review');
   if(!document.getElementById('eventReviewStyle')){
    const style=node('style');style.id='eventReviewStyle';style.textContent='.tactile-event-review{min-width:0;font-size:13px}.tactile-event-review canvas{display:block;width:100%;height:115px;cursor:pointer;background:#10202d;border-radius:6px}.event-review-list{max-height:250px;overflow:auto}.event-review-row{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:7px 2px;border-bottom:1px solid #61717a55}.event-review-row span{flex:1;min-width:180px}.event-review-row button{font-size:12px;padding:4px 8px;cursor:pointer}.event-review-row button[aria-pressed="true"]{outline:2px solid #37c7a5}.event-review-status{margin:6px 0;white-space:pre-wrap}.event-review-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.event-review-tools progress{width:140px}.event-review-tools[hidden]{display:none}.event-review-kind{width:auto}.event-review-detail{font-size:12px;opacity:.85;margin:7px 0}';document.head.appendChild(style);
   }
   const tools=node('div',undefined,container);tools.className='event-review-tools';
   node('strong',this.compact?'触觉事件候选':'自动接触／夹持／释放候选',tools);
   this.retry=node('button','重新检查',tools);this.retry.type='button';this.retry.onclick=()=>this.select(this.sid,{ensure:true});
   this.local=node('button',this.compact?'局部触点':'显示局部触点事件',tools);this.local.type='button';this.local.onclick=()=>{this.showLocal=!this.showLocal;this.local.textContent=this.showLocal?'仅显示主要事件':this.compact?'局部触点':'显示局部触点事件';this.local.setAttribute('aria-pressed',String(this.showLocal));this.page=0;this.renderStatus();this.renderList();this.draw();};
   const kind=node('select',undefined,tools);kind.setAttribute('aria-label','筛选事件类型');kind.className='event-review-kind';
   for(const [value,label] of [['','全部主要事件'],['contact','接触开始／结束'],['grasp','夹持候选'],['release','释放候选']]){const option=node('option',label,kind);option.value=value;}
   kind.onchange=()=>{this.kind=kind.value;this.page=0;this.renderStatus();this.renderList();this.draw();};
   this.progress=node('progress',undefined,tools);this.progress.max=3;this.progress.hidden=true;
   this.status=node('div','准备录制后可查看自动候选。',container);this.status.className='event-review-status';this.status.setAttribute('role','status');
   this.canvas=node('canvas',undefined,container);this.canvas.width=1200;this.canvas.height=160;this.canvas.setAttribute('aria-label','触觉响应与自动事件时间轴，点击跳转');
   if(this.compact){this.canvas.width=620;this.canvas.height=124;}
   this.canvas.onclick=e=>{if(!this.report)return;const r=this.canvas.getBoundingClientRect();const [start,end]=this.bounds();const t=start+Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))*(end-start);this.onSeek(t);};
   this.detail=node('div',this.compact?'点击时间轴或事件定位画面 · 按主机接收时间关联':'绿色：接触响应；黄色：夹持／释放候选。点击时间轴定位双目。按主机接收时间关联，实际曝光延迟未知。',container);this.detail.className='event-review-detail';
   if(this.compact)this.detail.title='绿色表示接触响应，黄色表示夹持或释放候选。实际曝光延迟未知。';
   const listing=options.expandedList?container:node('details',undefined,container);
   if(!options.expandedList){
    listing.open=!options.compact&&!this.readOnly;
    node('summary',this.readOnly?'展开候选事件 · 点击定位':'展开候选事件，点击对应帧确认／否定／看不清',listing);
   }
   this.list=node('div',undefined,listing);this.list.className='event-review-list';
   this.pager=node('div',undefined,listing);this.pager.className='event-review-tools';this.pager.hidden=true;
   this.prev=node('button','上一页',this.pager);this.prev.type='button';this.prev.onclick=()=>{this.page--;this.renderList();};
   this.pageLabel=node('span','',this.pager);
   this.next=node('button','下一页',this.pager);this.next.type='button';this.next.onclick=()=>{this.page++;this.renderList();};
  }
  async request(path,data){
   if(data&&!this.token){
    const health=await this.request('/api/health');
    if(health.service!=='capture-station-event-review'||!health.token)throw Error('录制服务不可用');
    this.token=health.token;
   }
   const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000);
   try{const response=await window.fetch('/ego-capture'+path,{cache:'no-store',signal:controller.signal,...(data?{method:'POST',headers:{'Content-Type':'application/json','X-Capture-Token':this.token},body:JSON.stringify(data)}:{})});const value=await response.json();if(!response.ok)throw Error(value.error||'录制服务不可用');return value;}finally{clearTimeout(timer);}
  }
  async select(sid,{ensure=!this.loadSaved}={}){
   if(!sid)return;if(this.sid!==sid){this.range=null;this.page=0;}this.sid=sid;const generation=++this.generation;clearTimeout(this.timer);this.report=null;this.review={};this.list.replaceChildren();this.pager.hidden=true;this.draw();this.loading=true;this.progress.hidden=false;this.progress.removeAttribute('value');this.status.textContent='正在检查已保存的触觉数据…';
   this.offlineSnapshot=false;
   const cached=this.cachedState?.(sid);
   if(cached?.report?.session===sid){
    this.report=cached.report;this.review=cached.decisions||{};this.offlineSnapshot=true;this.loading=false;this.progress.hidden=true;
    this.renderStatus();this.renderList();this.draw();return;
   }
   try{
    if(ensure)await this.request('/api/events/ensure',{session_id:sid});
    if(generation!==this.generation)return;
    await this.poll(generation,sid,{savedOnly:!ensure});
   }catch(error){if(generation===this.generation){this.loading=false;this.progress.hidden=true;this.status.textContent='事件尚未载入：'+error.message+'。可点击重新检查。';}}
  }
  async poll(generation,sid,{savedOnly=false}={}){
   const state=await this.request('/api/events?id='+encodeURIComponent(sid));if(generation!==this.generation)return;
   if(state.state==='failed')throw Error(state.progress?.message||'Event computation failed');
   if(state.state==='complete'){
    if(state.report.session!==sid)throw Error('Event/session identity mismatch');
    this.report=state.report;this.review=state.decisions||{};this.loading=false;this.progress.hidden=true;
    this.renderStatus();this.renderList();this.draw();return;
   }
   if(savedOnly&&!['queued','running'].includes(state.state)){
    this.loading=false;this.progress.hidden=true;
    this.status.textContent='本段暂无已整理的触觉候选。';
    return;
   }
   if(state.progress?.total){this.progress.max=state.progress.total;this.progress.value=state.progress.completed;}
   this.status.textContent='自动计算事件：'+({decode:'读取原始触觉帧',detect:'逐区域检测与帧关联',queued:'等待计算'}[state.progress?.phase]||'检查源数据');
   this.timer=setTimeout(()=>this.poll(generation,sid,{savedOnly}).catch(error=>{if(generation===this.generation){this.loading=false;this.progress.hidden=true;this.status.textContent='事件计算失败：'+error.message;}}),700);
  }
  renderStatus(){
   const report=this.report;
   if(!report)return;
   const events=this.events();
   const reviewed=events.filter(event=>(this.review[event.id]?.decision??'pending')!=='pending').length;
   const scope=this.range?'当前区间':'整条录制';
   this.status.textContent=`${scope} · 当前筛选 ${events.length} 条候选 · 已复核 ${reviewed} 条`
    +(report.camera_review_frames?'':'。整理完成后可关联复查画面')
    +(report.baseline_source==='recorded_rest_grid'?'':'。触觉基线取自录制初始窗口，静置状态待验证')
    +(report.anatomical_mapping_available?'':'。手侧与区域映射不符，多指夹持状态未知')
    +(this.offlineSnapshot?'。正在播放本地缓存，连接服务后可保存复核':'');
  }
  bounds(){return this.range||[0,Math.max(this.report?.duration_s||0,.001)];}
  setRange(range){this.range=range;this.page=0;this.renderStatus();this.renderList();this.draw();}
  events(){
   const [start,end]=this.bounds();
   return (this.report?.events||[]).filter(e=>Number.isFinite(e.t_s)&&e.t_s>=start&&(this.range?e.t_s<end:e.t_s<=end)&&
     (this.showLocal||!e.kind.startsWith('region_'))&&(!this.kind||e.kind.startsWith(this.kind)));
  }
  renderList({preserveFocus=false}={}){
   const active=preserveFocus&&this.list.contains(document.activeElement)?document.activeElement:null;
   const activeEvent=active?.closest('[data-event-id]')?.dataset.eventId;
   const activeDecision=active?.dataset.decision;
   const scrollTop=preserveFocus?this.list.scrollTop:0;
   this.list.replaceChildren();if(!this.report)return;
   const shown=this.events(), size=40, pages=Math.max(1,Math.ceil(shown.length/size));
   this.page=Math.max(0,Math.min(this.page,pages-1));
   this.pager.hidden=pages<2;
   this.prev.disabled=this.page===0;this.next.disabled=this.page===pages-1;
   this.pageLabel.textContent=`${shown.length} 条 · ${this.page+1} / ${pages}`;
   if(!shown.length){node('p','所选区间和类型没有候选。未检出不代表没有接触。',this.list);return;}
   let nextFocus;
   for(const event of shown.slice(this.page*size,(this.page+1)*size)){
    const row=node('div',undefined,this.list);row.className='event-review-row';row.dataset.eventId=event.id;
    const title=node('button',`${event.t_s.toFixed(3)}s · ${event.left_censored?'响应已存在 · 起点未知':labels[event.kind]}`,row);title.type='button';title.onclick=()=>this.onSeek(event.t_s);
    if(event.id===activeEvent&&activeDecision===undefined)nextFocus=title;
    const region=regions[event.region]||event.region,status=decisions[this.review[event.id]?.decision||'pending'];
    const frames='触觉帧 '+(event.tactile_frame_index+1)+(event.paired_frame_index==null?' · 附近无复查相机帧':
      ' · 复查帧 '+(event.paired_frame_index+1)+'（主机时间差 '+event.camera_delta_ms+'ms）');
    const info=node('span',this.compact?region+' · '+status:region+' · '+frames+' · '+status,row);
    if(this.compact)info.title=frames;
    if(this.readOnly)continue;
    for(const [decision,label] of [['confirmed','确认'],['rejected','否定'],['unobservable','看不清'],['pending','撤回']]){
     const button=node('button',label,row);button.type='button';button.disabled=!!this.offlineSnapshot;button.setAttribute('aria-pressed',String(this.review[event.id]?.decision===decision));button.onclick=()=>this.save(event.id,decision);
     button.dataset.decision=decision;
     if(event.id===activeEvent&&decision===activeDecision)nextFocus=button;
    }
   }
   this.list.scrollTop=scrollTop;
   nextFocus?.focus({preventScroll:true});
  }
  async save(eventId,decision){
   if(this.offlineSnapshot){this.status.textContent='保存复核需要连接后台；本地回放未写入任何复核结论。';return;}
   const generation=this.generation,sid=this.sid,revision=this.report.source_revision;
   try{const result=await this.request('/api/events/review',{session_id:sid,source_revision:revision,event_id:eventId,decision});if(generation!==this.generation)return;this.review=result.decisions;this.renderStatus();this.renderList({preserveFocus:true});this.draw();}
   catch(error){if(generation===this.generation)this.status.textContent='复核未保存：'+error.message;}
  }
  clear(){
   ++this.generation;clearTimeout(this.timer);
   this.sid=null;this.range=null;this.page=0;this.report=null;this.review={};this.loading=false;this.time=0;
   this.progress.hidden=true;this.list.replaceChildren();this.pager.hidden=true;
   this.status.textContent='准备录制后可查看自动候选。';
   this.draw();
  }
  setTime(time){this.time=Number(time)||0;this.draw();}
  draw(){
   const c=this.canvas.getContext('2d');if(!c)return;const w=this.canvas.width,h=this.canvas.height;c.clearRect(0,0,w,h);if(!this.report)return;
   const r=this.report,[start,end]=this.bounds(),x=t=>(t-start)/Math.max(end-start,.001)*w;
   const samples=r.samples.filter(s=>s.t_s!=null&&s.t_s>=start&&s.t_s<=end);
   const peak=samples.reduce((max,s)=>s.known?Math.max(max,s.peak):max,8);
   for(const span of r.intervals.filter(s=>s.region==='any_taxel')){
    const lo=Math.max(start,span.start_s),hi=Math.min(end,span.end_s??span.last_observed_s);
    if(hi>lo){c.fillStyle='#36c29d33';c.fillRect(x(lo),0,x(hi)-x(lo),h);}
   }
   c.strokeStyle='#55c9b5';c.beginPath();let open=false;
   for(const s of samples){if(!s.known){open=false;continue;}const y=h-25-s.peak/peak*(h-45);if(open)c.lineTo(x(s.t_s),y);else c.moveTo(x(s.t_s),y);open=true;}c.stroke();
   const columns=new Map(), step=end-start>60?10:3;
   for(const e of this.events()){const px=Math.round(x(e.t_s)/step)*step;columns.set(px,e.kind.includes('candidate')||columns.get(px));}
   c.globalAlpha=end-start>60?.45:.8;
   for(const [px,candidate] of columns){c.strokeStyle=candidate?'#f0bf62':'#7bd6ac';c.beginPath();c.moveTo(px,0);c.lineTo(px,h-20);c.stroke();}
   c.globalAlpha=1;
   if(this.time>=start&&this.time<=end){c.strokeStyle='#fff';c.beginPath();c.moveTo(x(this.time),0);c.lineTo(x(this.time),h);c.stroke();}
   c.fillStyle='#dbe8ef';c.font='16px sans-serif';c.fillText(start.toFixed(2)+'s · 峰值残差（原始计数）',6,h-5);c.fillText(end.toFixed(2)+'s',w-84,h-5);
  }
 }
 window.TactileEventReview=TactileEventReview;
})();
